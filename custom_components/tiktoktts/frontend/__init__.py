"""JavaScript module registration for the TikTok TTS Lovelace card.

This module handles automatic registration of the custom Lovelace card so
users do not need to manually add a resource entry in their dashboard settings.

How it works
------------
1. The www/ folder is registered as a static HTTP path so HA serves the JS
   file at /<domain>/tiktoktts-card.js.

2. The card URL is added directly to Lovelace's resource list (storage mode
   only). If Lovelace is in YAML mode the user must add it manually.

3. Registration happens in async_setup (not async_setup_entry) so it runs
   exactly once per HA startup regardless of how many config entries exist.
   It is also deferred until homeassistant_started to ensure Lovelace
   resources are fully loaded before we try to write to them.

4. The card URL includes a ?v=<version> query string so the browser cache
   is busted automatically on each integration version bump.

5. async_unregister() cleans up the resource entry when the last config
   entry is permanently deleted. It is called from async_remove_entry in
   __init__.py (not async_unload_entry, which also fires on reload/disable).

Note on imports
---------------
This subpackage does NOT import from the parent const.py. Doing so causes
a ModuleNotFoundError during HA's custom integration loading because Python
resolves relative imports differently for subpackages inside custom_components.
Instead, the domain string and logger are defined locally here.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later

_LOGGER = logging.getLogger(__name__)

# Domain and card filename defined locally to avoid relative import issues
_DOMAIN        = "tiktoktts"
_CARD_FILENAME = "tiktoktts-card.js"
_URL_BASE      = f"/{_DOMAIN}"

def _read_version() -> str:
    """Read the integration version from manifest.json.

    Called lazily inside async_register via hass.async_add_executor_job so
    the blocking file I/O does not run on the event loop at import time.
    Falls back to "0.0.0" if the manifest cannot be parsed.
    """
    try:
        manifest = json.loads((Path(__file__).parent.parent / "manifest.json").read_text())
        return manifest.get("version", "0.0.0")
    except Exception:  # noqa: BLE001
        return "0.0.0"


class JSModuleRegistration:
    """Registers the TikTokTTS Lovelace card in Home Assistant.

    Serves the JS file via a static HTTP path and adds (or updates) the
    resource entry in Lovelace's resource list so the card is available
    in the dashboard picker without any manual configuration.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise with the HomeAssistant instance."""
        self.hass = hass

    async def async_register(self) -> None:
        """Register the static path and add the resource to Lovelace.

        Always registers the HTTP path so the JS file is serveable.
        Then registers the Lovelace resource only if Lovelace is in
        storage mode — YAML mode users must add the resource manually.
        Reads the version from manifest.json via an executor job so the
        blocking file I/O does not run on the event loop.
        """
        self._version = await self.hass.async_add_executor_job(_read_version)
        await self._async_register_path()
        await self._async_wait_for_lovelace_resources()

    async def _async_register_path(self) -> None:
        """Register the www/ folder as a static HTTP path.

        Serves all files in custom_components/tiktoktts/www/ at /<domain>/.
        Catches RuntimeError silently — it means the path is already
        registered (e.g. after a config entry reload).
        """
        www_path = Path(__file__).parent.parent / "www"
        try:
            await self.hass.http.async_register_static_paths(
                [StaticPathConfig(_URL_BASE, str(www_path), cache_headers=False)]
            )
            _LOGGER.debug(
                "TikTokTTS: static path registered: %s -> %s", _URL_BASE, www_path
            )
        except RuntimeError:
            _LOGGER.debug("TikTokTTS: static path already registered: %s", _URL_BASE)

    async def _async_wait_for_lovelace_resources(self) -> None:
        """Wait for Lovelace resources to finish loading, then register.

        Lovelace resources may not be loaded immediately after
        homeassistant_started. This method retries every 5 seconds up to
        _MAX_RETRIES times, then gives up with a warning.
        """
        _MAX_RETRIES = 12
        attempts = {"n": 0}

        async def _check(_now: Any) -> None:
            attempts["n"] += 1
            if attempts["n"] > _MAX_RETRIES:
                _LOGGER.warning(
                    "TikTokTTS: gave up waiting for Lovelace resources after %d attempts. "
                    "If using YAML mode, add the card resource manually.",
                    _MAX_RETRIES,
                )
                return

            try:
                lovelace = self.hass.data.get("lovelace")
                if lovelace is None:
                    _LOGGER.debug("TikTokTTS: Lovelace not available yet — retrying in 5s")
                    async_call_later(self.hass, 5, _check)
                    return

                resources = getattr(lovelace, "resources", None)
                if resources is None:
                    _LOGGER.debug("TikTokTTS: Lovelace resources not available — retrying in 5s")
                    async_call_later(self.hass, 5, _check)
                    return

                if not resources.loaded:
                    _LOGGER.debug("TikTokTTS: Lovelace resources not yet loaded — retrying in 5s")
                    async_call_later(self.hass, 5, _check)
                    return

                await self._async_register_module(resources)

            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("TikTokTTS: error registering Lovelace card: %s", err)

        await _check(None)

    async def _async_register_module(self, resources) -> None:
        """Add or update the card resource entry in Lovelace.

        Creates the resource if it doesn't exist. If it already exists but
        with a different version string, updates it so the new JS file is
        loaded. If the version matches, does nothing.
        """
        url      = f"{_URL_BASE}/{_CARD_FILENAME}"
        full_url = f"{url}?v={self._version}"

        # Match on the exact base URL (before any ?v= query string) so we
        # don't accidentally match other resources that merely start with
        # the same path prefix.
        existing = [
            r for r in resources.async_items()
            if r["url"].split("?")[0] == url
        ]

        if not existing:
            await resources.async_create_item(
                {"res_type": "module", "url": full_url}
            )
            _LOGGER.info("TikTokTTS: Lovelace card registered (%s)", full_url)
            return

        resource        = existing[0]
        current_version = (
            resource["url"].split("?v=")[-1]
            if "?v=" in resource["url"]
            else "0"
        )

        if current_version != self._version:
            await resources.async_update_item(
                resource["id"],
                {"res_type": "module", "url": full_url},
            )
            _LOGGER.info(
                "TikTokTTS: Lovelace card updated %s -> %s",
                current_version, self._version,
            )
        else:
            _LOGGER.debug(
                "TikTokTTS: Lovelace card already registered at version %s", self._version
            )

    async def async_unregister(self) -> None:
        """Remove the card resource entry from Lovelace.

        Called when the integration is removed. Only operates in storage
        mode — YAML mode users manage resources manually.
        """
        lovelace  = self.hass.data.get("lovelace")
        resources = getattr(lovelace, "resources", None) if lovelace else None
        if not resources:
            return

        url = f"{_URL_BASE}/{_CARD_FILENAME}"
        for resource in resources.async_items():
            if resource["url"].split("?")[0] == url:
                await resources.async_delete_item(resource["id"])
                _LOGGER.debug("TikTokTTS: Lovelace card resource removed")