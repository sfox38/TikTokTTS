"""TikTok TTS - Home Assistant Custom Integration.

This is the integration entry point. HA calls async_setup once at startup,
async_setup_entry when the user adds the integration via the UI, and
async_unload_entry when they remove it.

Architecture overview
---------------------
The integration consists of these files:

  __init__.py   (this file)
    Registers the integration with HA, auto-registers the Lovelace card
    resource, forwards setup to all platforms, and installs a config-entry
    update listener so that changes saved via the options UI (gear icon)
    automatically trigger a reload - no manual HA restart required.
    Also clears singleton flags when the last config entry is removed so
    shared entities are recreated cleanly if the integration is re-added.

  config_flow.py
    Implements the UI setup wizard and the options (reconfigure) screen.
    Covers two connection modes:
      - Proxy mode:  talks to a community-run HTTP proxy that forwards
                     requests to TikTok on your behalf. No TikTok account
                     needed. Default proxy: https://tiktok-tts.weilnet.workers.dev
      - Direct mode: calls TikTok's internal API directly, using a session
                     cookie extracted from a logged-in TikTok browser session.
                     Falls back through multiple regional endpoints automatically.
    Both modes perform a live connection test before saving, so bad credentials
    or unreachable endpoints are caught at setup time rather than at runtime.

  tts.py
    Defines TikTokTTSEntity, the HA TextToSpeechEntity subclass.
    Handles voice selection, text chunking (direct mode), retries, and audio
    decoding. All config is read dynamically from entry.data on every call,
    so options changes take effect as soon as the integration reloads.

  select.py
    Defines three SHARED SelectEntity subclasses - one instance each for the
    whole integration regardless of how many config entries exist:
      - LanguageSelectEntity: dropdown of all supported language codes.
      - VoiceSelectEntity: dropdown of voices filtered to the selected language.
        State is a friendly label; raw API code exposed via 'code' attribute.
      - DeviceSelectEntity: dropdown of all available media_player entities,
        populated dynamically from hass.states. Refreshes automatically when
        new devices come online (e.g. browser_mod, mobile app).
    Created on the first config entry load, skipped for subsequent entries.
    Entity IDs: select.tiktoktts_language, select.tiktoktts_voice,
                select.tiktoktts_device

  text.py
    Defines a single SHARED MessageTextEntity - one instance for the whole
    integration. Provides a native HA text input field without requiring a
    manually created input_text helper.
    Entity ID: text.tiktoktts_message

  button.py
    Defines a single SHARED SpeakButtonEntity - one instance for the whole
    integration. When pressed it reads the current state of the language,
    voice, message, and device entities server-side and calls tts.speak
    directly in Python - no templates or scripts required.
    Entity ID: button.tiktoktts_speak

  frontend/__init__.py
    Registers the Lovelace card JS file as a static HTTP path and adds it
    to Lovelace's resource list automatically. No manual resource
    configuration required by the user.

  www/tiktoktts-card.js
    Custom Lovelace card providing a polished TTS control panel. Added to
    a dashboard with: type: custom:tiktoktts-card

  const.py
    All constants: API paths, field names, status codes, voice lists,
    language mappings, retry tuning, entity IDs/names, singleton flags,
    and attribution strings. If you need to adjust a behaviour (e.g. retry
    count, chunk size) or add a new voice, this is the only file you should
    need to touch.

Storage convention
------------------
Everything is stored in entry.data (not entry.options). This keeps a single
source of truth that both config_flow.py and tts.py read from consistently.
When the options flow saves changes it calls async_update_entry() to write
directly into entry.data, then returns an empty async_create_entry() to close
the flow. The update listener below detects the change and reloads the entry.

Singleton tracking
------------------
hass.data[DOMAIN] is used to track whether the shared select, text, and button
entities have been created. Keys defined in const.py:
  HASS_DATA_SELECT_CREATED, HASS_DATA_TEXT_CREATED, HASS_DATA_BUTTON_CREATED.
These flags are cleared when the last config entry is unloaded so that
removing and re-adding the integration recreates the entities cleanly.

Lovelace card registration
---------------------------
The custom Lovelace card is registered in async_setup (not async_setup_entry)
so it runs exactly once per HA startup regardless of how many config entries
exist. Registration is deferred until homeassistant_started to ensure Lovelace
resources are fully loaded. Requires 'frontend' and 'http' in manifest.json.

Credits
-------
Original integration: Philipp Lüttecke (https://github.com/philipp-luettecke/tiktoktts)
Community TTS proxy:  Weilbyte (https://github.com/Weilbyte/tiktok-tts)
Fork author:          Steven Fox / sfox38 (https://github.com/sfox38/tiktoktts)
"""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    HASS_DATA_LANGUAGE_ENTITY,
    HASS_DATA_RANDOM_LANGS,
    HASS_DATA_RANDOM_STORE,
    LOGGER,
    SERVICE_SET_RANDOM_VOICES,
    SUPPORTED_LANGUAGES,
)
from .frontend import JSModuleRegistration
from .shared import (
    clear_shared_forwarded,
    has_forwarded_shared,
    is_shared_owner,
    mark_shared_forwarded,
    release_shared_ownership,
    schedule_rehome_to_survivor,
)

PLATFORMS: list[Platform] = [Platform.TTS, Platform.SELECT, Platform.TEXT, Platform.BUTTON]

# TTS_PLATFORMS are per config entry - one TTS entity per proxy/direct entry.
# SHARED_PLATFORMS are singletons - created once for the whole integration
# regardless of how many config entries exist. They must only be unloaded
# when the last config entry is removed, not when a single entry is disabled.
TTS_PLATFORMS:    list[Platform] = [Platform.TTS]
SHARED_PLATFORMS: list[Platform] = [Platform.SELECT, Platform.TEXT, Platform.BUTTON]

_STORAGE_KEY     = f"{DOMAIN}_random_voices"
_STORAGE_VERSION = 1

# This integration is configured only via the UI config flow; it has no YAML
# configuration. Declaring this satisfies hassfest's requirement that an
# integration implementing async_setup defines a config schema.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the Lovelace card module and initialize the random voice store.

    This must run in async_setup (not async_setup_entry) so it executes
    exactly once per HA startup regardless of how many config entries exist.
    """
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    store = Store(hass, _STORAGE_VERSION, _STORAGE_KEY)
    hass.data[DOMAIN][HASS_DATA_RANDOM_STORE] = store

    saved = await store.async_load()
    if saved and isinstance(saved.get("languages"), list):
        langs = [lang for lang in saved["languages"] if lang in SUPPORTED_LANGUAGES]
    else:
        langs = []
    hass.data[DOMAIN][HASS_DATA_RANDOM_LANGS] = langs
    LOGGER.debug("Random voice languages loaded: %s", langs)

    async def _handle_set_random_voices(call: ServiceCall) -> None:
        languages = call.data.get("languages", [])
        valid = [lang for lang in languages if lang in SUPPORTED_LANGUAGES]
        invalid = [lang for lang in languages if lang not in SUPPORTED_LANGUAGES]
        if invalid:
            LOGGER.warning("Rejected unknown language codes from set_random_voices: %s", invalid)
        hass.data[DOMAIN][HASS_DATA_RANDOM_LANGS] = valid
        await store.async_save({"languages": valid})
        LOGGER.debug("Random voice languages saved: %s", valid)

        entity = hass.data.get(DOMAIN, {}).get(HASS_DATA_LANGUAGE_ENTITY)
        if entity and hasattr(entity, "async_refresh_random_voice_option"):
            await entity.async_refresh_random_voice_option()

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_RANDOM_VOICES,
        _handle_set_random_voices,
        schema=vol.Schema({
            vol.Required("languages"): vol.All(cv.ensure_list, [cv.string]),
        }),
    )

    # Register the Lovelace card module. The registration uses
    # frontend.add_extra_js_url, which simply adds the card to the frontend's
    # module list (read per page render), so there is nothing to wait for - no
    # deferral until homeassistant_started is needed.
    await JSModuleRegistration(hass).async_register()

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up TikTok TTS from a config entry.

    Forwards ALL platforms (TTS + shared) on setup. The shared platform
    entities (select, text, button) have their own singleton guards so they
    are only created once regardless of how many times this is called.
    On unload, only the TTS platform is torn down per entry - the shared
    platforms stay alive until the last entry is removed.
    """
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    # If hass.data[DOMAIN] was cleared (e.g. by a reload after the owning
    # entry was deleted), reload the random voice languages from the Store.
    # async_setup only runs once per HA startup so it won't re-populate this.
    if HASS_DATA_RANDOM_LANGS not in hass.data[DOMAIN]:
        store = hass.data[DOMAIN].get(HASS_DATA_RANDOM_STORE)
        if store is None:
            store = Store(hass, _STORAGE_VERSION, _STORAGE_KEY)
            hass.data[DOMAIN][HASS_DATA_RANDOM_STORE] = store
        saved = await store.async_load()
        if saved and isinstance(saved.get("languages"), list):
            langs = [lang for lang in saved["languages"] if lang in SUPPORTED_LANGUAGES]
        else:
            langs = []
        hass.data[DOMAIN][HASS_DATA_RANDOM_LANGS] = langs
        LOGGER.debug("Random voice languages reloaded in setup_entry: %s", langs)

    LOGGER.debug(
        "TikTokTTS setup_entry: entry=%s",
        entry.entry_id,
    )

    # Always set up the TTS platform for this entry.
    await hass.config_entries.async_forward_entry_setups(entry, TTS_PLATFORMS)

    # Forward the shared platforms (select, text, button) for this entry. The
    # singleton guards inside each platform's async_setup_entry prevent duplicate
    # entity creation, but HA raises a ValueError if the same platform is set up
    # for the same config entry twice, so guard the forward per entry.
    if not has_forwarded_shared(hass, entry):
        await hass.config_entries.async_forward_entry_setups(entry, SHARED_PLATFORMS)
        mark_shared_forwarded(hass, entry)

    entry.async_on_unload(
        entry.add_update_listener(_async_update_listener)
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.

    Called by HA when the user removes or disables the integration, or just
    before a reload.

    When this is the last config entry, all platforms are unloaded and
    hass.data[DOMAIN] is cleared so shared entities are recreated cleanly
    if the integration is added again.

    When other entries still exist, only the TTS platform is unloaded for
    this entry. The shared singleton flags are also cleared so that when
    async_setup_entry runs for the surviving entry (which HA calls
    automatically after a disable/reload), it recreates the shared entities
    registered under that entry's ID instead of the now-disabled entry.
    This avoids the HA "entity disabled by config entry" problem where
    entities registered under a disabled config entry cannot be re-enabled.
    """
    remaining = hass.config_entries.async_entries(DOMAIN)
    is_last_entry = len(remaining) <= 1

    # had_shared: did THIS entry forward the shared platforms? Every entry does,
    # so this is effectively always True; it decides which platforms to unload.
    # is_owner: did this entry actually CREATE the shared select/text/button
    # entities? Only the owner triggers a survivor reload - gating on had_shared
    # instead would make every non-owner unload reload the others (an endless
    # ping-pong). See shared.py for the bookkeeping details.
    platforms_to_unload = PLATFORMS if has_forwarded_shared(hass, entry) else TTS_PLATFORMS
    is_owner = is_shared_owner(hass, entry)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms_to_unload)
    if not unload_ok:
        return False

    if is_last_entry:
        LOGGER.debug("Last TikTokTTS config entry unloaded - clearing all data")
        hass.data.pop(DOMAIN, None)
        return True

    if DOMAIN in hass.data:
        # Allow a future (re)load of this entry to forward the shared platforms
        # again. Doing this for every entry (not just the owner) is what lets a
        # surviving entry re-forward the shared platforms when reloaded below.
        clear_shared_forwarded(hass, entry)

        if is_owner:
            # The entry that created the shared entities is going away while
            # others remain. Release ownership and reload a survivor so it
            # recreates the shared entities under its own (enabled) entry,
            # avoiding HA's "entity disabled by config entry" problem.
            release_shared_ownership(hass)
            LOGGER.debug(
                "TikTokTTS shared-owner entry %s unloaded - reloading a survivor "
                "to re-home the shared entities",
                entry.entry_id,
            )
            schedule_rehome_to_survivor(hass, entry, remaining)

    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up when a config entry is permanently deleted.

    Called by HA only on true removal (not on reload or disable). This is the
    correct place to remove the Lovelace card resource so it is not wiped on
    every options save or integration reload (which both trigger
    async_unload_entry but NOT this function).
    """
    remaining = hass.config_entries.async_entries(DOMAIN)
    if not remaining:
        LOGGER.debug("Last TikTokTTS entry removed - unregistering Lovelace card")
        await JSModuleRegistration(hass).async_unregister()


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when entry.data is updated.

    Triggered automatically whenever async_update_entry() is called on this
    config entry, which happens at the end of the options flow in config_flow.py.
    Reloading causes async_unload_entry + async_setup_entry to run in sequence,
    so the TikTokTTSEntity is recreated and picks up the new configuration
    values from entry.data immediately.
    """
    LOGGER.debug("Config entry updated for %s - reloading", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)