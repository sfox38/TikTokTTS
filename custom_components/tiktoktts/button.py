"""Button entity for TikTok TTS - the Speak button.

Singleton pattern
-----------------
One shared button entity is created for the whole integration:

  button.tiktoktts_speak

When pressed it reads the current state of the other shared entities:
  - text.tiktoktts_message    - the message to speak
  - select.tiktoktts_voice    - the voice API code (via 'code' attribute)
  - select.tiktoktts_device   - the media_player entity_id (via 'code' attribute)

It then calls tts.speak directly in Python - no templates, no scripts,
no YAML gymnastics required. The dashboard card becomes simply:

  type: entities
  title: TikTok TTS
  entities:
    - select.tiktoktts_language
    - select.tiktoktts_voice
    - text.tiktoktts_message
    - select.tiktoktts_device
    - button.tiktoktts_speak

TTS entity selection
--------------------
The button picks the correct TTS entity (proxy or direct) by inspecting
loaded config entries via ConfigEntryState.LOADED rather than checking
hass.states. This avoids a bug where stale state machine entries from a
previously removed config entry would cause the button to target a TTS
entity that no longer exists. Proxy mode is preferred over direct mode
when both are configured.

Random voice support
--------------------
When the selected voice code is RANDOM_VOICE_CODE ("random"), the button:
  1. Sets cache=False so HA does not serve stale audio from the file cache.
  2. Passes a unique _random_seed value (UUID) in the tts.speak options dict.
     HA includes all supported_options keys in the TTS cache key hash, so a
     unique seed guarantees a cache miss and forces async_get_tts_audio in
     tts.py to be called on every press regardless of message text.
"""
from __future__ import annotations

from uuid import uuid4

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    API_MODE_DIRECT,
    API_MODE_PROXY,
    CONF_API_MODE,
    DOMAIN,
    ENTITY_ID_DEVICE,
    ENTITY_ID_MESSAGE,
    ENTITY_ID_SPEAK,
    ENTITY_ID_TTS_DIRECT,
    ENTITY_ID_TTS_PROXY,
    ENTITY_ID_VOICE,
    ENTITY_NAME_SPEAK,
    HASS_DATA_BUTTON_CREATED,
    LOGGER,
    PLACEHOLDER_LOADING,
    RANDOM_SEED_KEY,
    RANDOM_VOICE_CODE,
    TTS_SERVICE_DOMAIN,
    TTS_SERVICE_FIELD_CACHE,
    TTS_SERVICE_FIELD_MESSAGE,
    TTS_SERVICE_FIELD_OPTIONS,
    TTS_SERVICE_FIELD_PLAYER,
    TTS_SERVICE_FIELD_VOICE,
    TTS_SERVICE_SPEAK,
    UNIQUE_ID_SPEAK,
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the shared Speak button - only on the first config entry load."""
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    if hass.data[DOMAIN].get(HASS_DATA_BUTTON_CREATED):
        LOGGER.debug(
            "Shared button entity already exists - skipping for entry %s",
            config_entry.entry_id,
        )
        return

    hass.data[DOMAIN][HASS_DATA_BUTTON_CREATED] = True
    async_add_entities([SpeakButtonEntity()])
    LOGGER.debug("Shared button entity created for entry %s", config_entry.entry_id)


class SpeakButtonEntity(ButtonEntity):
    """Speak button - reads the shared helper entities and calls tts.speak.

    Handles everything server-side in Python, avoiding the HA frontend
    limitation where button cards cannot evaluate templates in data fields.
    """

    _attr_has_entity_name = False
    _attr_icon = "mdi:microphone"
    _attr_unique_id = UNIQUE_ID_SPEAK
    _attr_name = ENTITY_NAME_SPEAK

    def __init__(self) -> None:
        """Initialise the speak button."""
        self.entity_id = ENTITY_ID_SPEAK

    async def async_press(self) -> None:
        """Handle button press - read helper entities and call tts.speak.

        Reads current values from:
          text.tiktoktts_message  -> state value  (the message text)
          select.tiktoktts_voice  -> 'code' attr  (the raw API voice code)
          select.tiktoktts_device -> 'code' attr  (the raw media_player entity_id)

        Voice and device use the 'code' attribute rather than state because
        their dropdowns display friendly names while storing API values in
        the attribute.

        When random voice is selected, caching is disabled and a unique
        _random_seed is passed in the options to force a cache miss.
        """
        # Strip whitespace - the card sends a single space when the textarea
        # is empty (HA text entity rejects truly empty strings), so we must
        # strip before checking to avoid speaking a silent space.
        message = self._get_state(ENTITY_ID_MESSAGE).strip()
        voice   = self._get_code(ENTITY_ID_VOICE)
        device  = self._get_code(ENTITY_ID_DEVICE)

        if not message:
            LOGGER.warning(
                "TikTokTTS Speak button pressed but message is empty. "
                "Type a message in the TikTokTTS Message field first."
            )
            return

        if not voice or voice == PLACEHOLDER_LOADING:
            LOGGER.warning(
                "TikTokTTS Speak button pressed but no voice is selected."
            )
            return

        if not device or device == PLACEHOLDER_LOADING:
            LOGGER.warning(
                "TikTokTTS Speak button pressed but no output device is selected."
            )
            return

        tts_entity = self._pick_tts_entity()
        if not tts_entity:
            LOGGER.error(
                "TikTokTTS Speak button pressed but no TikTokTTS TTS entity "
                "was found. Make sure the integration is configured."
            )
            return

        use_cache = voice != RANDOM_VOICE_CODE
        options_dict = {TTS_SERVICE_FIELD_VOICE: voice}
        if voice == RANDOM_VOICE_CODE:
            options_dict[RANDOM_SEED_KEY] = str(uuid4())

        LOGGER.debug(
            "Speak button: entity=%s device=%s voice=%s cache=%s message=%s",
            tts_entity, device, voice, use_cache,
            message[:50] + "..." if len(message) > 50 else message,
        )

        await self.hass.services.async_call(
            domain=TTS_SERVICE_DOMAIN,
            service=TTS_SERVICE_SPEAK,
            service_data={
                TTS_SERVICE_FIELD_PLAYER:  device,
                TTS_SERVICE_FIELD_MESSAGE: message,
                TTS_SERVICE_FIELD_CACHE:   use_cache,
                TTS_SERVICE_FIELD_OPTIONS: options_dict,
            },
            target={"entity_id": tts_entity},
        )

    def _get_state(self, entity_id: str) -> str:
        """Return the current state of an entity, or empty string if unavailable.

        Filters out STATE_UNKNOWN and STATE_UNAVAILABLE so callers never
        accidentally speak the literal string "unavailable" or "unknown".
        """
        state = self.hass.states.get(entity_id)
        if state is None:
            LOGGER.warning("TikTokTTS: entity %s not found in hass.states", entity_id)
            return ""
        if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return ""
        return state.state

    def _get_code(self, entity_id: str) -> str:
        """Return the 'code' attribute of an entity - the underlying API value.

        Voice and device selects display friendly names as their state but
        store the raw API code (voice ID or media_player entity_id) in the
        'code' attribute. This method reads that attribute directly.
        """
        state = self.hass.states.get(entity_id)
        if state is None:
            LOGGER.warning("TikTokTTS: entity %s not found in hass.states", entity_id)
            return ""
        code = state.attributes.get("code", "")
        if not code:
            LOGGER.warning(
                "TikTokTTS: entity %s has no 'code' attribute - "
                "it may not have finished loading yet.", entity_id
            )
        return code

    def _pick_tts_entity(self) -> str | None:
        """Pick the TTS entity to use - proxy preferred over direct.

        Checks loaded config entries only via ConfigEntryState.LOADED.
        Does not rely on hass.states which may contain stale entries from
        previously removed config entries, causing the button to target a
        TTS entity that no longer exists.
        """
        entries = self.hass.config_entries.async_entries(DOMAIN)
        loaded = [e for e in entries if e.state == ConfigEntryState.LOADED]

        for entry in loaded:
            if entry.data.get(CONF_API_MODE, API_MODE_PROXY) == API_MODE_PROXY:
                return ENTITY_ID_TTS_PROXY

        for entry in loaded:
            if entry.data.get(CONF_API_MODE) == API_MODE_DIRECT:
                return ENTITY_ID_TTS_DIRECT

        return None