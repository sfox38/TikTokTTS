"""Select entities for TikTok TTS - language, voice, and device dropdowns.

Singleton pattern
-----------------
The language, voice, and device select entities are shared across all config
entries (proxy and direct). They are created exactly once - when the first
config entry loads - and skipped for any subsequent entries. This means:

  select.tiktoktts_language  (one, shared)
  select.tiktoktts_voice     (one, shared)
  select.tiktoktts_device    (one, shared)

A flag in hass.data[DOMAIN] tracks whether the shared entities exist so that
reloads and multiple config entries don't create duplicates.

Language / Voice filtering
--------------------------
Selecting a language automatically rebuilds the voice dropdown to show only
voices for that language group. The voice state is a friendly label; the raw
API code is exposed via the 'code' state attribute for automation use.

Random Voice support
--------------------
When the user configures a random voice pool (via the dashboard card's dice
button), a "Random Voice" language option appears at the top of the language
dropdown. Selecting it sets the voice dropdown to a single "Random Voice"
option. The actual voice is resolved at speak time by tts.py.

The language entity stores the random voice pool in hass.data[DOMAIN] and
exposes it via the 'random_voice_languages' state attribute so the dashboard
card can read the current pool to pre-check the language checkboxes.

The language entity is stored in hass.data[DOMAIN][HASS_DATA_LANGUAGE_ENTITY]
so the set_random_voices service handler in __init__.py can call
async_refresh_random_voice_option() directly without going through the
unreliable entity_components lookup.

Restore-on-restart
------------------
All three entities use RestoreEntity to persist selections across HA restarts.
The language entity defers voice entity notification via async_create_task with
a retry loop to avoid a RuntimeError when the voice entity hasn't been added
to hass yet at the time the language entity's async_added_to_hass runs.

Device select
-------------
Populated dynamically from hass.states at startup. Shows HA friendly names
in the dropdown. The raw entity_id is exposed via the 'code' state attribute.
"""
from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    ALL_VOICES,
    CONF_VOICE,
    DEFAULT_LANG,
    DEFAULT_VOICE,
    DOMAIN,
    ENTITY_ID_DEVICE,
    ENTITY_ID_LANGUAGE,
    ENTITY_ID_VOICE,
    ENTITY_NAME_DEVICE,
    ENTITY_NAME_LANGUAGE,
    ENTITY_NAME_VOICE,
    HASS_DATA_LANGUAGE_ENTITY,
    HASS_DATA_RANDOM_LANGS,
    HASS_DATA_SELECT_CREATED,
    HASS_DATA_SHARED_OWNER,
    LANGUAGE_ALL_CODE,
    LANGUAGE_ALL_NAME,
    LANGUAGE_NAMES,
    LOGGER,
    PLACEHOLDER_LOADING,
    RANDOM_VOICE_CODE,
    RANDOM_VOICE_LANG_NAME,
    RANDOM_VOICE_NAME,
    SUPPORTED_LANGUAGES,
    UNIQUE_ID_DEVICE,
    UNIQUE_ID_LANGUAGE,
    UNIQUE_ID_VOICE,
    VOICE_NAMES,
    VOICES_BY_LANGUAGE,
)


def _lang_to_name(code: str) -> str:
    """Convert a language code to its friendly display name."""
    if code == LANGUAGE_ALL_CODE:
        return LANGUAGE_ALL_NAME
    if code == RANDOM_VOICE_CODE:
        return RANDOM_VOICE_LANG_NAME
    return LANGUAGE_NAMES.get(code, code)


def _name_to_lang(name: str) -> str:
    """Convert a friendly language name back to its API code."""
    if name == LANGUAGE_ALL_NAME:
        return LANGUAGE_ALL_CODE
    if name == RANDOM_VOICE_LANG_NAME:
        return RANDOM_VOICE_CODE
    for code, friendly in LANGUAGE_NAMES.items():
        if friendly == name:
            return code
    return name


def _voice_to_name(code: str) -> str:
    """Convert a voice API code to its friendly display name.

    e.g. "en_male_narration" -> "Story Teller"

    The raw API code is exposed separately via the 'code' state attribute
    and displayed below the dropdown in the custom Lovelace card.
    """
    return VOICE_NAMES.get(code, code)


def _sort_voices(codes: list[str]) -> dict[str, str]:
    """Return an ordered {friendly_label: voice_code} map, sorted by label.

    Using a mapping instead of two index-aligned lists keeps each label coupled
    to its API code structurally, so a future edit cannot silently misalign them.
    The insertion order is the case-insensitive friendly-name order, which is what
    the dropdown displays.
    """
    paired = sorted(
        ((c, _voice_to_name(c)) for c in codes),
        key=lambda x: x[1].lower(),
    )
    return {name: code for code, name in paired}


def _usable(state: State | None) -> bool:
    """Return True if a media_player state can be used as a TTS target."""
    return state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create shared select entities - only on the first config entry load.

    Subsequent config entries (e.g. adding direct mode after proxy mode)
    skip creation entirely since the shared entities already exist.
    The hass.data[DOMAIN] dict tracks whether they have been created.
    """
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    if hass.data[DOMAIN].get(HASS_DATA_SELECT_CREATED):
        LOGGER.debug(
            "Shared select entities already exist - skipping for entry %s",
            config_entry.entry_id,
        )
        return

    hass.data[DOMAIN][HASS_DATA_SELECT_CREATED] = True
    # Record which entry owns the shared entities so async_unload_entry knows
    # whether unloading this entry should re-home them to a surviving entry.
    hass.data[DOMAIN][HASS_DATA_SHARED_OWNER] = config_entry.entry_id

    default_voice = config_entry.data.get(CONF_VOICE, DEFAULT_VOICE)

    language_entity = LanguageSelectEntity(default_voice)
    voice_entity = VoiceSelectEntity(language_entity, default_voice)
    device_entity = DeviceSelectEntity()

    hass.data[DOMAIN][HASS_DATA_LANGUAGE_ENTITY] = language_entity

    async_add_entities([language_entity, voice_entity, device_entity])
    LOGGER.debug("Shared select entities created for entry %s", config_entry.entry_id)


class LanguageSelectEntity(SelectEntity, RestoreEntity):
    """Shared language dropdown - one instance for the whole integration.

    Displays friendly names like "🇺🇸 English (US)".
    Changing language triggers VoiceSelectEntity to rebuild its options.
    The raw language code is exposed via the 'code' state attribute.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:translate"
    _attr_unique_id = UNIQUE_ID_LANGUAGE
    _attr_name = ENTITY_NAME_LANGUAGE

    def __init__(self, default_voice: str) -> None:
        """Initialise with language derived from the configured default voice."""
        self.entity_id = ENTITY_ID_LANGUAGE
        self._attr_options = [LANGUAGE_ALL_NAME] + [_lang_to_name(c) for c in SUPPORTED_LANGUAGES]

        initial_lang = DEFAULT_LANG
        for lang, voices in VOICES_BY_LANGUAGE.items():
            if default_voice in voices:
                initial_lang = lang
                break
        self._current_code = initial_lang
        self._attr_current_option = _lang_to_name(initial_lang)
        self._voice_entity: VoiceSelectEntity | None = None

    def set_voice_entity(self, voice_entity: VoiceSelectEntity) -> None:
        """Link the paired voice entity so it updates when language changes."""
        self._voice_entity = voice_entity

    async def async_added_to_hass(self) -> None:
        """Restore the last selected language and apply random voice option if set.

        Runs async_refresh_random_voice_option first (with write_state=False) so
        the options list includes "Random Voice" before the restore check runs -
        otherwise the restored state would fail the "in options" validation.

        Always notifies the voice entity of the restored language via a deferred
        async_create_task with a retry loop. Direct notification is not possible
        here because the voice entity may not have been added to hass yet
        (async_added_to_hass runs during platform setup, and both entities are
        registered in the same async_add_entities call). The voice entity's own
        async_added_to_hass will then re-apply the restored voice name against
        the correctly-filtered options list.
        """
        await super().async_added_to_hass()
        await self.async_refresh_random_voice_option(write_state=False)
        last_state = await self.async_get_last_state()
        if last_state and last_state.state in self._attr_options:
            code = _name_to_lang(last_state.state)
            if code in (LANGUAGE_ALL_CODE, RANDOM_VOICE_CODE) or code in SUPPORTED_LANGUAGES:
                self._current_code = code
                self._attr_current_option = last_state.state
                LOGGER.debug("Language restored to: %s", last_state.state)

        if self._voice_entity is not None:
            restored_code = self._current_code
            voice_entity = self._voice_entity

            async def _notify_voice_entity() -> None:
                # Both entities are registered in one async_add_entities call with
                # no ordering guarantee, so the voice entity may not be in hass yet.
                # Wait on its readiness event rather than polling on a sleep loop.
                try:
                    await asyncio.wait_for(voice_entity.added_event.wait(), timeout=10)
                except TimeoutError:
                    LOGGER.debug(
                        "Voice entity never became ready - skipping language notify on restore"
                    )
                    return
                await voice_entity.async_on_language_changed(restored_code)

            self.hass.async_create_task(_notify_voice_entity())

    async def async_refresh_random_voice_option(self, write_state: bool = True) -> None:
        """Add or remove the Random Voice option based on current store contents.

        Called on startup (from async_added_to_hass) and whenever the random
        voice language pool is updated by the set_random_voices service handler.
        If write_state=False, skips async_write_ha_state - used during startup
        to avoid writing state before the entity is fully registered.
        If the pool becomes empty and random voice was selected, falls back to
        the default language so the entity is never left in an invalid state.
        Only notifies the voice entity when the effective language code changes,
        so toggling the pool without changing language does not reset the voice.
        """
        langs = self.hass.data.get(DOMAIN, {}).get(HASS_DATA_RANDOM_LANGS, [])
        # base_options is always rebuilt fresh, so RANDOM_VOICE_LANG_NAME is never
        # already present — the redundant membership check is omitted intentionally.
        base_options = [LANGUAGE_ALL_NAME] + [_lang_to_name(c) for c in SUPPORTED_LANGUAGES]
        if langs:
            base_options = [RANDOM_VOICE_LANG_NAME] + base_options
        self._attr_options = base_options

        prev_code = self._current_code
        if self._current_code == RANDOM_VOICE_CODE and not langs:
            self._current_code = DEFAULT_LANG
            self._attr_current_option = _lang_to_name(DEFAULT_LANG)

        if write_state:
            self.async_write_ha_state()
            # Only notify the voice entity when the language actually changed to
            # avoid resetting the voice dropdown on every pool save.
            if self._voice_entity is not None and self._current_code != prev_code:
                await self._voice_entity.async_on_language_changed(self._current_code)

    @property
    def language_code(self) -> str:
        """Return the current language as a raw API code."""
        return self._current_code

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the raw language code and random voice languages as state attributes."""
        langs = []
        if self.hass:
            langs = self.hass.data.get(DOMAIN, {}).get(HASS_DATA_RANDOM_LANGS, [])
        return {
            "code": self._current_code,
            "random_voice_languages": langs,
            # code<->name map for the real languages so the dashboard card can
            # build the random-voice checkbox list without duplicating the name
            # table (and without depending on the exact emoji in each label).
            "language_options": [
                {"code": c, "name": _lang_to_name(c)} for c in SUPPORTED_LANGUAGES
            ],
        }

    async def async_select_option(self, option: str) -> None:
        """Handle language selection and trigger voice list rebuild."""
        code = _name_to_lang(option)
        if code not in (LANGUAGE_ALL_CODE, RANDOM_VOICE_CODE) and code not in SUPPORTED_LANGUAGES:
            LOGGER.warning("Unknown language selected: %s", option)
            return
        self._current_code = code
        self._attr_current_option = option
        self.async_write_ha_state()
        if self._voice_entity is not None:
            await self._voice_entity.async_on_language_changed(code)
        LOGGER.debug("Language changed to: %s (%s)", option, code)


class VoiceSelectEntity(SelectEntity, RestoreEntity):
    """Shared voice dropdown - one instance for the whole integration.

    Displays friendly names like "Story Teller" in the dropdown.
    The raw API code is stored internally and exposed via the 'code'
    state attribute, which button.py reads when calling tts.speak.
    Options are filtered to the currently selected language group.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:microphone"
    _attr_unique_id = UNIQUE_ID_VOICE
    _attr_name = ENTITY_NAME_VOICE

    def __init__(
        self,
        language_entity: LanguageSelectEntity,
        default_voice: str,
    ) -> None:
        """Initialise with voices for the starting language."""
        self.entity_id = ENTITY_ID_VOICE
        language_entity.set_voice_entity(self)

        initial_lang = language_entity.language_code

        # {friendly_label: voice_code} for the current language group.
        self._voice_codes: dict[str, str] = {}
        # Set once this entity is added to hass, so the language entity can push
        # the restored language without polling for readiness (see R2 / startup).
        self._added_event = asyncio.Event()

        # Pending voice to apply on the next async_on_language_changed call.
        # Seeded below with the configured default so the language entity's
        # one-shot startup notify re-applies the default instead of resetting
        # to the first voice of the language. async_added_to_hass overrides
        # this with a restored value when one exists. Consumed (cleared) by
        # the first async_on_language_changed call.
        self._pending_restore_voice: str | None = None

        if initial_lang == RANDOM_VOICE_CODE:
            self._set_random_voice_options()
            return

        voice_codes = (
            ALL_VOICES
            if initial_lang == LANGUAGE_ALL_CODE
            else VOICES_BY_LANGUAGE.get(initial_lang, [DEFAULT_VOICE])
        )

        self._voice_codes = _sort_voices(voice_codes)
        self._attr_options = list(self._voice_codes)

        default_label = self._label_for_code(default_voice)
        if default_label is not None:
            self._current_code = default_voice
            self._attr_current_option = default_label
        else:
            self._attr_current_option = self._attr_options[0]
            self._current_code = self._voice_codes[self._attr_current_option]

        # Preserve the configured default across the startup language notify.
        self._pending_restore_voice = self._attr_current_option

    @property
    def added_event(self) -> asyncio.Event:
        """Event set once this entity has been added to hass (startup coordination)."""
        return self._added_event

    def _set_random_voice_options(self) -> None:
        """Collapse the dropdown to the single Random Voice option."""
        self._voice_codes         = {RANDOM_VOICE_NAME: RANDOM_VOICE_CODE}
        self._attr_options        = [RANDOM_VOICE_NAME]
        self._current_code        = RANDOM_VOICE_CODE
        self._attr_current_option = RANDOM_VOICE_NAME

    def _label_for_code(self, code: str) -> str | None:
        """Return the friendly label currently mapped to a voice code, if any."""
        for label, mapped in self._voice_codes.items():
            if mapped == code:
                return label
        return None

    async def async_added_to_hass(self) -> None:
        """Store the last selected voice name for deferred re-application.

        The language entity will call async_on_language_changed shortly after
        with the restored language code, rebuilding the options list. At that
        point _pending_restore_voice is consumed to re-apply the correct voice
        against the new options. We do not apply the restore here directly
        because the options list may not yet reflect the restored language.
        """
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state:
            if last_state.state == RANDOM_VOICE_NAME:
                # Random voice restore is handled immediately — it does not depend
                # on the options list since there is only one random option.
                self._set_random_voice_options()
                LOGGER.debug("Voice restored to: %s", RANDOM_VOICE_NAME)
            else:
                # Defer non-random restore until async_on_language_changed fires
                # with the correct language's options list.
                self._pending_restore_voice = last_state.state
                LOGGER.debug("Voice restore deferred: %s", last_state.state)

        # Signal the language entity (which may run its startup notify before or
        # after us) that we are now in hass and ready to receive the language.
        self._added_event.set()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the raw API code for use in button.py and automations."""
        return {"code": self._current_code}

    async def async_select_option(self, option: str) -> None:
        """Handle voice selection - option is a friendly label."""
        if option not in self._voice_codes:
            LOGGER.warning("Voice '%s' not available for current language.", option)
            return
        self._current_code = self._voice_codes[option]
        self._attr_current_option = option
        self.async_write_ha_state()
        LOGGER.debug("Voice changed to: %s (%s)", option, self._current_code)

    async def async_on_language_changed(self, new_language_code: str) -> None:
        """Rebuild options list when the language changes.

        Called by LanguageSelectEntity. Resets to the first voice in the new
        language group, unless a pending restore voice is set (from a restart)
        in which case re-applies that voice if it exists in the new options list.
        """
        if new_language_code == RANDOM_VOICE_CODE:
            self._set_random_voice_options()
            self._pending_restore_voice = None
            self.async_write_ha_state()
            LOGGER.debug("Voice options set to Random Voice")
            return

        raw_codes = (
            ALL_VOICES
            if new_language_code == LANGUAGE_ALL_CODE
            else VOICES_BY_LANGUAGE.get(new_language_code, [DEFAULT_VOICE])
        )
        self._voice_codes = _sort_voices(raw_codes)
        self._attr_options = list(self._voice_codes)

        # Re-apply a pending restore if the voice exists in the new options list.
        # This handles the case where language restores to a non-default value:
        # the voice entity deferred its restore until the correct options are ready.
        pending = self._pending_restore_voice
        self._pending_restore_voice = None
        if pending and pending in self._voice_codes:
            self._current_code = self._voice_codes[pending]
            self._attr_current_option = pending
            LOGGER.debug("Voice restored (deferred): %s (%s)", pending, self._current_code)
        else:
            self._attr_current_option = self._attr_options[0]
            self._current_code = self._voice_codes[self._attr_current_option]

        self.async_write_ha_state()
        LOGGER.debug(
            "Voice options updated for '%s', selected '%s' (%s)",
            new_language_code,
            self._attr_current_option,
            self._current_code,
        )


class DeviceSelectEntity(SelectEntity, RestoreEntity):
    """Shared media player dropdown - one instance for the whole integration.

    Shows HA friendly names (e.g. "Kitchen Speaker") in the dropdown.
    The raw media_player entity_id is stored internally and exposed via
    the 'code' state attribute for use in button.py and automations.
    Refreshes automatically once HA fires homeassistant_started.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:speaker"
    _attr_unique_id = UNIQUE_ID_DEVICE
    _attr_name = ENTITY_NAME_DEVICE

    def __init__(self) -> None:
        """Initialise with a placeholder until hass.states is available."""
        self.entity_id = ENTITY_ID_DEVICE
        self._attr_options = [PLACEHOLDER_LOADING]
        self._attr_current_option = PLACEHOLDER_LOADING
        self._device_names: list[str] = []
        self._device_ids: list[str] = []
        self._current_device_id: str = ""

    async def async_added_to_hass(self) -> None:
        """Scan for media players and keep the list updated continuously.

        Three mechanisms ensure the device list is always current:

        1. Initial scan at registration time - catches devices already loaded.
        2. homeassistant_started listener - catches devices loaded after our
           integration but before HA reports fully started.
        3. Persistent state_changed listener on media_player entities -
           catches devices that register late (e.g. browser_mod, mobile app)
           or transition from unavailable to available after startup. This
           also handles devices that disappear and reappear.

        Also restores the previously selected device from the HA state database
        so the user's selection persists across restarts.
        """
        await super().async_added_to_hass()

        # Restore previously selected device_id before refreshing the list
        last_state = await self.async_get_last_state()
        if last_state and last_state.attributes.get("code"):
            self._current_device_id = last_state.attributes["code"]
            LOGGER.debug("Device restored to: %s", self._current_device_id)

        # Initial scan
        await self._async_refresh_devices()

        # Re-scan once HA reports fully started
        @callback
        def _on_started(_event: Event) -> None:
            self.hass.async_create_task(self._async_refresh_devices())

        self.async_on_remove(
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)
        )

        # Listen for media_player state changes so we catch late-registering
        # integrations (browser_mod, mobile app, etc.) and availability changes.
        # The listener is removed automatically on unload via async_on_remove.
        @callback
        def _on_state_changed(event: Event) -> None:
            """Refresh only when a media player's usability actually flips.

            A global state_changed listener is unavoidable for dynamic discovery,
            but we ignore routine attribute ticks (volume, position) that fire
            constantly while a player is active, and react only when a player
            appears, disappears, or changes availability.
            """
            if not event.data.get("entity_id", "").startswith("media_player."):
                return
            if _usable(event.data.get("old_state")) == _usable(event.data.get("new_state")):
                return
            self.hass.async_create_task(self._async_refresh_devices())

        self.async_on_remove(
            self.hass.bus.async_listen("state_changed", _on_state_changed)
        )

    async def _async_refresh_devices(self) -> None:
        """Rebuild the options list from current hass.states media players.

        Shows HA friendly names in the dropdown while storing entity_ids
        internally. Must be async so async_write_ha_state() is always
        called on the event loop.
        """
        # Filter out unavailable and unknown devices - they have no friendly_name
        # attribute and cannot be used as a TTS target anyway.
        all_players = sorted(
            self.hass.states.async_all("media_player"),
            key=lambda s: s.entity_id,
        )
        player_states = [s for s in all_players if _usable(s)]

        if not player_states:
            return

        new_names = [
            s.attributes.get("friendly_name", s.entity_id)
            for s in player_states
        ]
        new_ids = [s.entity_id for s in player_states]

        # Nothing changed: same players available and the current selection still
        # resolves. Skip the rebuild and state write entirely.
        if (
            new_ids == self._device_ids
            and new_names == self._device_names
            and self._current_device_id in new_ids
        ):
            return

        # Only mutate after the early-returns so the lists are never left in a
        # partially-updated state if we bail out below.
        self._device_names = new_names
        self._device_ids = new_ids
        self._attr_options = self._device_names

        if self._current_device_id in self._device_ids:
            # Restored device is available - select it
            idx = self._device_ids.index(self._current_device_id)
            self._attr_current_option = self._device_names[idx]
        elif not self._current_device_id:
            # No restored value at all - default to first available device
            self._current_device_id = self._device_ids[0]
            self._attr_current_option = self._device_names[0]
        else:
            # We have a restored device_id but it isn't available yet
            # (e.g. browser_mod hasn't registered yet). Keep the restored
            # value and do NOT overwrite with the first device - the
            # state_changed listener will call us again when it appears.
            return

        self.async_write_ha_state()
        LOGGER.debug("TikTokTTS device list: %d media player(s) found", len(self._device_ids))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the raw media_player entity_id for use in button.py."""
        return {"code": self._current_device_id}

    async def async_select_option(self, option: str) -> None:
        """Handle device selection - option is a friendly name."""
        if option not in self._attr_options:
            LOGGER.warning("Unknown device selected: %s", option)
            return
        idx = self._attr_options.index(option)
        self._current_device_id = self._device_ids[idx]
        self._attr_current_option = option
        self.async_write_ha_state()
        LOGGER.debug("Output device: %s (%s)", option, self._current_device_id)