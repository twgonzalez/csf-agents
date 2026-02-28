"""Tests: newsletter_writer.py — system prompt and HTML generation.

Pure functions only — no API calls, no SMTP.
"""
import pytest

from agents.newsletter.newsletter_writer import (
    _build_system_prompt,
    _build_html,
)


# ---------------------------------------------------------------------------
# _build_system_prompt — client identity injection
# ---------------------------------------------------------------------------

def test_build_system_prompt_csf_identity(csf_client):
    prompt = _build_system_prompt(csf_client)
    assert "best decisions come from people closest to them" in prompt


def test_build_system_prompt_cma_identity(cma_client):
    prompt = _build_system_prompt(cma_client)
    assert "city managers" in prompt.lower()
    # CSF identity should NOT bleed through
    assert "California Stewardship Fund" not in prompt


# ---------------------------------------------------------------------------
# _build_html — newsletter name in masthead
# ---------------------------------------------------------------------------

def test_build_html_csf_newsletter_name(mock_newsletter_content, bill_set, csf_client):
    html = _build_html(mock_newsletter_content, bill_set, "2026-W08", csf_client)
    assert "Local Control Intelligence" in html


def test_build_html_cma_newsletter_name(mock_newsletter_content, bill_set, cma_client):
    html = _build_html(mock_newsletter_content, bill_set, "2026-W08", cma_client)
    assert "City Management Legislative Briefing" in html


# ---------------------------------------------------------------------------
# _build_html — brand hex colors in HTML
# ---------------------------------------------------------------------------

def test_build_html_csf_brand_hex(mock_newsletter_content, bill_set, csf_client):
    html = _build_html(mock_newsletter_content, bill_set, "2026-W08", csf_client)
    assert "#1a3a5c" in html


def test_build_html_cma_brand_hex(mock_newsletter_content, bill_set, cma_client):
    html = _build_html(mock_newsletter_content, bill_set, "2026-W08", cma_client)
    assert "#1a3d2b" in html


# ---------------------------------------------------------------------------
# _build_html — proof_sheet footer label
# ---------------------------------------------------------------------------

def test_build_html_csf_footer_label(mock_newsletter_content, bill_set, csf_client):
    html = _build_html(mock_newsletter_content, bill_set, "2026-W08", csf_client)
    assert "California Stewardship Fund" in html


def test_build_html_cma_footer_label(mock_newsletter_content, bill_set, cma_client):
    html = _build_html(mock_newsletter_content, bill_set, "2026-W08", cma_client)
    assert "City Managers Association of California" in html
