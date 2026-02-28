"""Tests: client and voice loading, discovery, and fallback behavior.

Uses real files on disk (clients/csf/ and clients/cma/) to verify that
_load_client() and _load_voice() parse the actual config files correctly.
"""
import pytest

from agents.social.social_writer import (
    CLIENTS_DIR,
    DEFAULT_CLIENT,
    DEFAULT_VOICE,
    _list_clients,
    _load_client,
    _list_voices,
    _load_voice,
)


# ---------------------------------------------------------------------------
# Client loading — required fields
# ---------------------------------------------------------------------------

def test_load_client_csf_required_fields():
    cfg = _load_client("csf")
    for key in ("slug", "client_name", "colors", "identity", "newsletter", "proof_sheet"):
        assert key in cfg, f"clients/csf/client.yml missing key: {key}"


def test_load_client_csf_brand_values():
    cfg = _load_client("csf")
    assert cfg["colors"]["background"]["hex"] == "#1a3a5c"
    assert cfg["colors"]["accent"]["hex"] == "#c9a227"


def test_load_client_cma_required_fields():
    cfg = _load_client("cma")
    for key in ("slug", "client_name", "colors", "identity", "newsletter", "proof_sheet"):
        assert key in cfg, f"clients/cma/client.yml missing key: {key}"


def test_load_client_cma_brand_differs_from_csf():
    csf = _load_client("csf")
    cma = _load_client("cma")
    assert cma["colors"]["background"]["hex"] != csf["colors"]["background"]["hex"]
    assert cma["colors"]["accent"]["hex"] != csf["colors"]["accent"]["hex"]


# ---------------------------------------------------------------------------
# Client loading — unknown client fallback
# ---------------------------------------------------------------------------

def test_load_client_unknown_falls_back_to_csf():
    cfg = _load_client("nonexistent_client_xyz")
    assert cfg["client_id"] == DEFAULT_CLIENT


# ---------------------------------------------------------------------------
# Client discovery
# ---------------------------------------------------------------------------

def test_list_clients_includes_both():
    clients = _list_clients()
    assert "csf" in clients
    assert "cma" in clients


# ---------------------------------------------------------------------------
# Voice loading — CSF
# ---------------------------------------------------------------------------

def test_load_voice_csf_default():
    voices_dir = CLIENTS_DIR / "csf" / "voices"
    text = _load_voice("default", voices_dir)
    assert text  # non-empty


def test_load_voice_csf_coalition():
    voices_dir = CLIENTS_DIR / "csf" / "voices"
    default_text  = _load_voice("default",   voices_dir)
    coalition_text = _load_voice("coalition", voices_dir)
    assert coalition_text           # non-empty
    assert coalition_text != default_text  # distinct content


def test_load_voice_csf_urgent():
    voices_dir = CLIENTS_DIR / "csf" / "voices"
    text = _load_voice("urgent", voices_dir)
    assert text  # non-empty


# ---------------------------------------------------------------------------
# Voice loading — CMA
# ---------------------------------------------------------------------------

def test_load_voice_cma_default():
    voices_dir = CLIENTS_DIR / "cma" / "voices"
    text = _load_voice("default", voices_dir)
    assert text  # non-empty


# ---------------------------------------------------------------------------
# Voice loading — fallback
# ---------------------------------------------------------------------------

def test_load_voice_nonexistent_falls_back_to_default():
    voices_dir = CLIENTS_DIR / "csf" / "voices"
    default_text  = _load_voice("default",              voices_dir)
    fallback_text = _load_voice("nonexistent_voice_xyz", voices_dir)
    assert fallback_text == default_text


# ---------------------------------------------------------------------------
# Voice discovery
# ---------------------------------------------------------------------------

def test_list_voices_csf_has_three():
    voices_dir = CLIENTS_DIR / "csf" / "voices"
    voices = _list_voices(voices_dir)
    assert len(voices) == 3
    assert set(voices) == {"default", "urgent", "coalition"}


def test_list_voices_cma_has_one():
    voices_dir = CLIENTS_DIR / "cma" / "voices"
    voices = _list_voices(voices_dir)
    assert len(voices) == 1
    assert "default" in voices
