"""Prism registry configuration management.

Handles multi-registry CRUD operations on ~/.prism/registries.json,
auto-migration from config.json registry_url, and token generation.
"""

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional

from .config import PRISM_HOME, get_config


REGISTRIES_PATH = PRISM_HOME / "registries.json"
CACHE_DIR = PRISM_HOME / "cache"
CACHE_TTL = 86400  # 24 hours in seconds

# Kebab-case validation: lowercase alphanumeric with hyphens, no leading/trailing hyphen
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


def load_registries() -> dict:
    """Load registries.json, auto-migrating from config.json if needed.

    Migration: if registries.json does not exist but config.json has registry_url,
    create a "default" registry entry with that URL (D-02).

    The migration uses atomic write (temp + rename). If two processes race on migration,
    both write the same content (same source registry_url), so the race is benign.
    """
    if REGISTRIES_PATH.exists():
        try:
            with open(REGISTRIES_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"registries": [], "default": None}

    # Migration: config.json registry_url -> registries.json
    config = get_config()
    registry_url = config.get("registry_url", "")
    if registry_url:
        registries = {
            "registries": [{
                "name": "default",
                "url": registry_url.rstrip("/"),
                "token": "",
                "writable": True,
            }],
            "default": "default",
        }
        save_registries(registries)
        return registries

    return {"registries": [], "default": None}


def save_registries(data: dict) -> None:
    """Atomic write of registries.json with 0o600 permissions (T-04-01).

    Tokens are stored in plaintext, so file permissions must restrict access
    to the owning user only.
    """
    REGISTRIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(REGISTRIES_PATH) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.rename(tmp, str(REGISTRIES_PATH))
    os.chmod(str(REGISTRIES_PATH), 0o600)


def add_registry(name: str, url: str, token: str = "", writable: bool = True) -> None:
    """Add a registry entry to registries.json.

    Validates name is kebab-case (T-04-02). Raises ValueError if name already exists
    or name format is invalid. Sets as default if no default exists yet.
    """
    # Validate name format (T-04-02: prevent injection)
    if not name or (len(name) > 1 and not _NAME_RE.match(name)):
        # Allow single-char names like 'a' as special case
        if len(name) == 1 and not name.isalnum():
            raise ValueError(
                f"Invalid registry name '{name}'. Use kebab-case: [a-z0-9][a-z0-9-]*[a-z0-9]"
            )
        if len(name) > 1 and not _NAME_RE.match(name):
            raise ValueError(
                f"Invalid registry name '{name}'. Use kebab-case: [a-z0-9][a-z0-9-]*[a-z0-9]"
            )

    data = load_registries()

    # Check for duplicate
    for entry in data.get("registries", []):
        if entry["name"] == name:
            raise ValueError(f"Registry '{name}' already exists. Remove it first or use a different name.")

    entry = {
        "name": name,
        "url": url.rstrip("/"),
        "token": token,
        "writable": writable,
    }
    data.setdefault("registries", []).append(entry)

    # Set as default if no default yet
    if not data.get("default"):
        data["default"] = name

    save_registries(data)


def remove_registry(name: str) -> None:
    """Remove a registry entry by name. Raises ValueError if not found.

    If the removed entry was the default, sets default to first remaining
    registry or None if empty.
    """
    data = load_registries()
    registries = data.get("registries", [])
    original_len = len(registries)

    data["registries"] = [r for r in registries if r["name"] != name]

    if len(data["registries"]) == original_len:
        raise ValueError(f"Registry '{name}' not found.")

    # Update default if removed entry was the default
    if data.get("default") == name:
        if data["registries"]:
            data["default"] = data["registries"][0]["name"]
        else:
            data["default"] = None

    save_registries(data)


def list_registries() -> list:
    """List all configured registries with masked tokens.

    Returns list of dicts with keys: name, url, token (masked), writable, is_default.
    Token masking: first 8 chars + "..." if len > 8, else "***" if non-empty, else "".
    """
    data = load_registries()
    default_name = data.get("default")
    result = []

    for entry in data.get("registries", []):
        token_raw = entry.get("token", "")
        if token_raw:
            if len(token_raw) > 8:
                masked = token_raw[:8] + "..."
            else:
                masked = "***"
        else:
            masked = ""

        result.append({
            "name": entry["name"],
            "url": entry.get("url", ""),
            "token": masked,
            "writable": entry.get("writable", True),
            "is_default": entry["name"] == default_name,
        })

    return result


def set_default_registry(name: str) -> None:
    """Set the default write-target registry. Raises ValueError if name not found."""
    data = load_registries()

    found = any(r["name"] == name for r in data.get("registries", []))
    if not found:
        raise ValueError(f"Registry '{name}' not found.")

    data["default"] = name
    save_registries(data)


def get_registry(name: str) -> dict:
    """Get a registry entry by name. Raises ValueError if not found."""
    data = load_registries()
    for entry in data.get("registries", []):
        if entry["name"] == name:
            return entry
    raise ValueError(f"Registry '{name}' not found.")


def get_default_registry() -> Optional[dict]:
    """Get the default registry entry, or None if no default is set."""
    data = load_registries()
    default_name = data.get("default")
    if not default_name:
        return None
    for entry in data.get("registries", []):
        if entry["name"] == default_name:
            return entry
    return None


def generate_token() -> str:
    """Generate a cryptographically secure API token (T-04-03).

    Uses secrets.token_hex(32) for 64 hex chars of entropy, prefixed with 'prism_'
    for identifiability. Total length: 69 chars.
    """
    return "prism_" + secrets.token_hex(32)


def resolve_token(registry: dict) -> str:
    """Resolve the API token for a registry entry.

    Checks REGISTRY_TOKEN env var first (backward compat with Phase 3),
    then falls back to the token stored in registries.json.
    """
    env_token = os.environ.get("REGISTRY_TOKEN", "")
    if env_token:
        return env_token
    return registry.get("token", "")
