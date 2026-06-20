"""Lifecycle bookkeeping for the shared singleton entities.

The select / text / button platforms create exactly one set of entities for the
whole integration, no matter how many config entries exist (proxy, direct, or
both). That convenience requires a small amount of cross-entry state, which is
centralised here so __init__.py's setup/unload methods stay readable:

  * which entries have forwarded the shared platforms (so unload tears down the
    right set), tracked per entry, and
  * which entry actually CREATED the shared entities - the "owner". Only the
    owner needs to re-home the entities to a survivor when it is unloaded while
    other entries remain, otherwise the entities would be left registered under a
    disabled config entry and could not be re-enabled.

None of these functions create or destroy entities themselves; they only manage
the flags in hass.data[DOMAIN] that the platform guards and __init__.py read.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    HASS_DATA_BUTTON_CREATED,
    HASS_DATA_LANGUAGE_ENTITY,
    HASS_DATA_SELECT_CREATED,
    HASS_DATA_SHARED_OWNER,
    HASS_DATA_TEXT_CREATED,
)


def _forwarded_key(entry_id: str) -> str:
    """Return the per-entry hass.data flag key recording shared-platform setup."""
    return f"shared_platforms_setup_{entry_id}"


def has_forwarded_shared(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Return True if this entry has already forwarded the shared platforms."""
    return hass.data.get(DOMAIN, {}).get(_forwarded_key(entry.entry_id), False)


def mark_shared_forwarded(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Record that this entry forwarded the shared platforms."""
    hass.data[DOMAIN][_forwarded_key(entry.entry_id)] = True


def clear_shared_forwarded(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget that this entry forwarded the shared platforms (after unload)."""
    hass.data[DOMAIN].pop(_forwarded_key(entry.entry_id), None)


def is_shared_owner(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Return True if this entry is the one that created the shared entities."""
    return hass.data.get(DOMAIN, {}).get(HASS_DATA_SHARED_OWNER) == entry.entry_id


def release_shared_ownership(hass: HomeAssistant) -> None:
    """Clear the singleton-creation flags so a survivor recreates the entities.

    Called when the owning entry is unloaded while others remain. Clearing the
    *_CREATED flags and the owner pointer lets the next entry to load (the
    survivor reloaded below) take ownership and recreate the shared entities
    under its own enabled config entry.
    """
    data = hass.data.get(DOMAIN, {})
    for key in (
        HASS_DATA_SELECT_CREATED,
        HASS_DATA_TEXT_CREATED,
        HASS_DATA_BUTTON_CREATED,
        HASS_DATA_LANGUAGE_ENTITY,
        HASS_DATA_SHARED_OWNER,
    ):
        data.pop(key, None)


def schedule_rehome_to_survivor(
    hass: HomeAssistant, entry: ConfigEntry, remaining: list[ConfigEntry]
) -> None:
    """Reload one surviving entry so it re-homes the shared entities.

    Only one reload is needed: the survivors' singleton guards stop the others
    from duplicating the entities. The reload is deferred via call_soon so it
    runs after the current unload fully completes.
    """
    survivors = [e for e in remaining if e.entry_id != entry.entry_id]
    if not survivors:
        return
    new_owner_id = survivors[0].entry_id
    hass.loop.call_soon(
        lambda: hass.async_create_task(
            hass.config_entries.async_reload(new_owner_id)
        )
    )
