"""Tests: social_writer.py — prompt builders, bill selection, and rendering.

Pure functions only — no API calls.
Dates frozen to 2026-02-27 for _select_bills() lookback/lookahead logic.
"""
import pytest
from freezegun import freeze_time

from agents.social.social_writer import (
    _build_base_prompt,
    _build_system_prompt,
    _select_bills,
    _render_markdown,
    _render_html,
)


# ---------------------------------------------------------------------------
# _build_base_prompt — client identity injection
# ---------------------------------------------------------------------------

def test_build_base_prompt_csf_org_description(csf_client):
    prompt = _build_base_prompt(csf_client)
    assert "best decisions come from people closest to them" in prompt


def test_build_base_prompt_cma_org_description(cma_client):
    prompt = _build_base_prompt(cma_client)
    assert "city managers" in prompt.lower()
    # CSF identity should NOT bleed through
    assert "California Stewardship Fund" not in prompt


def test_build_base_prompt_clients_differ(csf_client, cma_client):
    assert _build_base_prompt(csf_client) != _build_base_prompt(cma_client)


# ---------------------------------------------------------------------------
# _build_system_prompt — voice text included
# ---------------------------------------------------------------------------

def test_build_system_prompt_includes_voice_text(csf_client, csf_voice_text):
    prompt = _build_system_prompt(csf_voice_text, csf_client)
    assert csf_voice_text in prompt


# ---------------------------------------------------------------------------
# _select_bills — watch list (risk_count >= 2)
# ---------------------------------------------------------------------------

@freeze_time("2026-02-27")
def test_select_bills_high_risk_in_watch_list(bills_dict):
    result = _select_bills(bills_dict)
    watch_numbers = [b["bill_number"] for b in result["watch_list"]]
    assert "AB1751" in watch_numbers


@freeze_time("2026-02-27")
def test_select_bills_low_risk_excluded_from_watch_list(bills_dict):
    result = _select_bills(bills_dict)
    watch_numbers = [b["bill_number"] for b in result["watch_list"]]
    assert "SB1016" not in watch_numbers


# ---------------------------------------------------------------------------
# _select_bills — new_bills (first_seen within lookback AND risk_count >= 1)
# ---------------------------------------------------------------------------

@freeze_time("2026-02-27")
def test_select_bills_new_bill_in_new_bills(bills_dict):
    # high_risk_bill first_seen = 2026-02-14 (13 days ago, within 14-day default lookback)
    result = _select_bills(bills_dict)
    new_numbers = [b["bill_number"] for b in result["new_bills"]]
    assert "AB1751" in new_numbers


@freeze_time("2026-02-27")
def test_select_bills_old_bill_excluded_from_new_bills():
    old_high_risk = {
        "bill_number": "AB9999",
        "title": "Old High Risk Bill",
        "status": "In Committee",
        "status_date": "2026-01-01",
        "introduced_date": "2026-01-01",
        "last_updated": "2026-01-01",
        "summary": "An old bill.",
        "upcoming_hearings": [],
        "actions": [],
        # 57 days before frozen date 2026-02-27 → outside 14-day lookback
        "first_seen": "2026-01-01T00:00:00",
        "analysis": {
            "pro_housing_production": "strong",
            "densification": "strong",
            "reduce_discretion": "none",
            "cost_to_cities": "none",
        },
    }
    result = _select_bills({"AB9999": old_high_risk})
    new_numbers = [b["bill_number"] for b in result["new_bills"]]
    assert "AB9999" not in new_numbers


# ---------------------------------------------------------------------------
# _select_bills — upcoming_hearings (within lookahead window)
# ---------------------------------------------------------------------------

@freeze_time("2026-02-27")
def test_select_bills_upcoming_hearing(bills_dict):
    # high_risk_bill hearing = 2026-03-01 (2 days out, within 7-day default lookahead)
    result = _select_bills(bills_dict)
    hearing_bill_numbers = [h["_bill"]["bill_number"] for h in result["upcoming_hearings"]]
    assert "AB1751" in hearing_bill_numbers


# ---------------------------------------------------------------------------
# _render_markdown — client name in header
# ---------------------------------------------------------------------------

@freeze_time("2026-02-27")
def test_render_markdown_csf_client_name(mock_social_content, bill_set, csf_client):
    md = _render_markdown(mock_social_content, bill_set, "default", csf_client)
    assert "California Stewardship Fund" in md


@freeze_time("2026-02-27")
def test_render_markdown_cma_client_name(mock_social_content, bill_set, cma_client):
    md = _render_markdown(mock_social_content, bill_set, "default", cma_client)
    assert "City Managers Association" in md


# ---------------------------------------------------------------------------
# _render_html — brand hex colors in HTML
# ---------------------------------------------------------------------------

@freeze_time("2026-02-27")
def test_render_html_csf_brand_hex(mock_social_content, bill_set, csf_client, tmp_path):
    html = _render_html(mock_social_content, bill_set, "default", csf_client, tmp_path)
    assert "#1a3a5c" in html


@freeze_time("2026-02-27")
def test_render_html_cma_brand_hex(mock_social_content, bill_set, cma_client, tmp_path):
    html = _render_html(mock_social_content, bill_set, "default", cma_client, tmp_path)
    assert "#1a3d2b" in html


@freeze_time("2026-02-27")
def test_render_html_csf_masthead_label(mock_social_content, bill_set, csf_client, tmp_path):
    html = _render_html(mock_social_content, bill_set, "default", csf_client, tmp_path)
    assert "California Stewardship Fund" in html
