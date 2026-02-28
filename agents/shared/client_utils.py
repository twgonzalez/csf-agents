"""
client_utils.py — Shared client and voice loading utilities.

Provides _load_client, _list_clients, _load_voice, _list_voices, CLIENTS_DIR,
DEFAULT_CLIENT, and DEFAULT_VOICE.

Shared by all agents that support the clients/<slug>/ system.
Agents import this module; adding a new client requires only a new directory —
no changes to this file or any agent.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths and defaults
# ---------------------------------------------------------------------------

# Path resolves correctly regardless of where this module is imported from:
#   agents/shared/../../..  ==  project root
_PROJECT_ROOT  = Path(__file__).resolve().parent.parent.parent
CLIENTS_DIR    = _PROJECT_ROOT / "clients"

DEFAULT_CLIENT = "csf"
DEFAULT_VOICE  = "default"


# ---------------------------------------------------------------------------
# Client loading
# ---------------------------------------------------------------------------

def _list_clients() -> list[str]:
    """Return sorted list of available client slugs (subdirectory names)."""
    if not CLIENTS_DIR.exists():
        return []
    return sorted(
        p.name for p in CLIENTS_DIR.iterdir()
        if p.is_dir() and (p / "client.yml").exists()
    )


def _load_client(name: str = DEFAULT_CLIENT) -> dict:
    """Load a client config from clients/<name>/client.yml.

    Falls back to DEFAULT_CLIENT if the named client doesn't exist.
    Raises SystemExit if neither the named client nor the default can be loaded.
    """
    path = CLIENTS_DIR / name / "client.yml"
    if path.exists():
        log.info(f"→ Client: '{name}' ({path})")
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    if name != DEFAULT_CLIENT:
        log.warning(f"Client '{name}' not found at {path}. Falling back to '{DEFAULT_CLIENT}'.")
        default_path = CLIENTS_DIR / DEFAULT_CLIENT / "client.yml"
        if default_path.exists():
            log.info(f"→ Client: '{DEFAULT_CLIENT}' (fallback)")
            return yaml.safe_load(default_path.read_text(encoding="utf-8"))

    log.error(f"No client config found for '{name}' and no '{DEFAULT_CLIENT}' fallback.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Voice loading
# ---------------------------------------------------------------------------

def _list_voices(voices_dir: Path) -> list[str]:
    """Return sorted list of available voice names for a client."""
    if not voices_dir.exists():
        return []
    return sorted(p.stem for p in voices_dir.glob("*.md"))


def _load_voice(name: str = DEFAULT_VOICE, voices_dir: Path | None = None) -> str:
    """Load a voice file by name from the client's voices directory.

    Returns the file content as a string. Falls back to DEFAULT_VOICE if the
    named voice doesn't exist, and returns an empty string if nothing is found.
    """
    if voices_dir is None:
        voices_dir = CLIENTS_DIR / DEFAULT_CLIENT / "voices"

    path = voices_dir / f"{name}.md"

    if path.exists():
        log.info(f"→ Voice: '{name}' ({path.name})")
        return path.read_text(encoding="utf-8").strip()

    if name != DEFAULT_VOICE:
        log.warning(f"Voice '{name}' not found at {path}. Falling back to '{DEFAULT_VOICE}'.")
        default_path = voices_dir / f"{DEFAULT_VOICE}.md"
        if default_path.exists():
            log.info(f"→ Voice: '{DEFAULT_VOICE}' (fallback)")
            return default_path.read_text(encoding="utf-8").strip()

    log.warning(f"No voice file found in {voices_dir}. Proceeding without voice guidance.")
    return ""
