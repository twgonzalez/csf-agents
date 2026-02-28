"""Shared pytest fixtures for CSF agent test suite.

All client fixtures are in-memory dicts whose values exactly match
clients/csf/client.yml and clients/cma/client.yml, so that loading
tests can verify the real files round-trip correctly.

Bill fixtures use dates compatible with freeze_time("2026-02-27"):
  - first_seen "2026-02-14" = 13 days ago  → within 14-day lookback
  - hearing    "2026-03-01" = 2 days ahead  → within 7-day lookahead
"""
import pytest


# ---------------------------------------------------------------------------
# Client identity fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def csf_client():
    return {
        "client_id": "csf",
        "client_name": "California Stewardship Fund",
        "slug": "csf",
        "identity": {
            "org_description": (
                "A policy organization whose core belief is that the best decisions "
                "come from people closest to them."
            ),
            "audience": (
                "City council members, planning commissioners, local government staff, "
                "neighborhood advocates, major donors, and engaged citizens who care about "
                "protecting local government authority from state preemption."
            ),
        },
        "colors": {
            "background": {"hex": "#1a3a5c", "name": "deep navy blue"},
            "text":       {"hex": "#ffffff", "name": "white"},
            "accent":     {"hex": "#c9a227", "name": "warm gold"},
        },
        "image": {
            "accent_stripe": "A warm gold horizontal accent stripe across the lower third",
            "style_notes": (
                "Clean, minimal, professional policy-advocacy. "
                "No photos. No people. No logos."
            ),
            "bill_context": "California legislative bill",
        },
        "proof_sheet": {"label": "California Stewardship Fund"},
        "newsletter": {"name": "Local Control Intelligence"},
        "default_voice": "default",
    }


@pytest.fixture
def cma_client():
    return {
        "client_id": "cma",
        "client_name": "City Managers Association of California",
        "slug": "cma",
        "identity": {
            "org_description": (
                "The professional association representing city managers and chief "
                "administrative officers across California, committed to effective, "
                "accountable, and fiscally sound local governance."
            ),
            "audience": (
                "City managers, chief administrative officers, city administrators, "
                "assistant city managers, and senior local government professionals "
                "across California."
            ),
        },
        "colors": {
            "background": {"hex": "#1a3d2b", "name": "deep forest green"},
            "text":       {"hex": "#ffffff", "name": "white"},
            "accent":     {"hex": "#c8941a", "name": "warm amber"},
        },
        "image": {
            "accent_stripe": "A warm amber horizontal accent stripe across the lower third",
            "style_notes": (
                "Clean, institutional, professional policy graphic. "
                "No photos. No people. No logos."
            ),
            "bill_context": "California legislative bill",
        },
        "proof_sheet": {"label": "City Managers Association of California"},
        "newsletter": {"name": "City Management Legislative Briefing"},
        "default_voice": "default",
    }


# ---------------------------------------------------------------------------
# Bill fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def high_risk_bill():
    """AB1751 — 3 strong + 1 moderate: qualifies for watch_list and new_bills."""
    return {
        "bill_number": "AB1751",
        "title": "Planning and Zoning: Impact Fees",
        "author": "Test Author",
        "status": "In Committee",
        "status_date": "2026-02-01",
        "introduced_date": "2026-01-15",
        "last_updated": "2026-02-01",
        "summary": "Prohibits local agencies from imposing impact fees on housing projects.",
        "subjects": ["Housing", "Local Government"],
        "committees": ["Housing"],
        "text_url": "https://example.com/AB1751",
        "upcoming_hearings": [
            {
                "date": "2026-03-01",
                "committee": "Assembly Housing Committee",
                "location": "Sacramento",
            }
        ],
        "actions": [],
        # 13 days before frozen date 2026-02-27 → within 14-day lookback
        "first_seen": "2026-02-14T00:00:00",
        "analysis": {
            "pro_housing_production": "strong",
            "densification": "strong",
            "reduce_discretion": "strong",
            "cost_to_cities": "moderate",
            "notes": "Eliminates local impact fee authority; direct fiscal impact on cities.",
            "comms_brief": "AB1751 eliminates cities' ability to charge impact fees.",
        },
    }


@pytest.fixture
def low_risk_bill():
    """SB1016 — all none: excluded from watch_list and new_bills."""
    return {
        "bill_number": "SB1016",
        "title": "Transitional Housing Bond",
        "author": "Another Author",
        "status": "Introduced",
        "status_date": "2026-02-10",
        "introduced_date": "2026-02-10",
        "last_updated": "2026-02-10",
        "summary": "An act relating to transitional housing.",
        "subjects": [],
        "committees": ["Rules"],
        "text_url": "https://example.com/SB1016",
        "upcoming_hearings": [],
        "actions": [],
        "first_seen": "2026-02-14T00:00:00",
        "analysis": {
            "pro_housing_production": "none",
            "densification": "none",
            "reduce_discretion": "none",
            "cost_to_cities": "none",
            "notes": "Transitional housing bond; no direct local zoning preemption.",
            "comms_brief": "",
        },
    }


@pytest.fixture
def bills_dict(high_risk_bill, low_risk_bill):
    """Bills dict keyed by bill number — matches _select_bills(bills: dict) input."""
    return {
        "AB1751": high_risk_bill,
        "SB1016": low_risk_bill,
    }


@pytest.fixture
def bill_set(high_risk_bill):
    """Pre-built bill_set for render tests (bypasses _select_bills)."""
    return {
        "watch_list": [high_risk_bill],
        "new_bills": [high_risk_bill],
        "upcoming_hearings": [
            {
                "date": "2026-03-01",
                "committee": "Assembly Housing Committee",
                "location": "Sacramento",
                "_bill": high_risk_bill,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Image brief fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def image_brief():
    return {
        "bill_number": "AB1751",
        "headline": "AB1751 Eliminates Local Impact Fees",
        "subtext": "Sacramento bans cities from funding infrastructure",
        "optional_graphic": "California state outline, minimal, faint white, bottom-right",
    }


# ---------------------------------------------------------------------------
# Synthetic Claude response fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_social_content():
    """Minimal synthetic response matching social_writer.py JSON schema."""
    return {
        "week_theme": "Sacramento moves to strip cities of impact fee authority.",
        "posts": [
            {
                "post_number": 1,
                "post_type": "bill_spotlight",
                "bill_number": "AB1751",
                "x": "AB1751 would strip cities of impact fee authority. #LocalControl",
                "x_char_count": 63,
                "facebook": "AB1751 would strip cities of the power to charge impact fees.",
                "instagram": "Your city's infrastructure is under threat.\n\n#LocalControl #AB1751",
                "hashtags": ["LocalControl", "AB1751"],
                "image_brief": {
                    "headline": "AB1751 Eliminates Local Impact Fees",
                    "subtext": "Sacramento bans infrastructure funding",
                    "optional_graphic": "California state outline",
                },
            },
            {
                "post_number": 2,
                "post_type": "action_alert",
                "bill_number": "AB1751",
                "x": "AB1751 hearing March 1. Contact your rep. #LocalControl",
                "x_char_count": 55,
                "facebook": "The Assembly Housing Committee votes on AB1751 on March 1.",
                "instagram": "March 1st. Your city's future is on the line.\n\n#LocalControl",
                "hashtags": ["LocalControl", "AB1751"],
                "image_brief": {
                    "headline": "Hearing: March 1st",
                    "subtext": "Assembly Housing Committee — AB1751",
                    "optional_graphic": "Calendar icon",
                },
            },
            {
                "post_number": 3,
                "post_type": "mission_frame",
                "bill_number": None,
                "x": "Sacramento keeps taking decisions away from cities. #LocalControl",
                "x_char_count": 64,
                "facebook": "The pattern is clear: bill after bill strips cities of authority.",
                "instagram": "Your city council exists for a reason.\n\n#LocalControl",
                "hashtags": ["LocalControl"],
                "image_brief": {
                    "headline": "Who Decides for Your City?",
                    "subtext": "Local voices matter.",
                    "optional_graphic": "California state outline with city dots",
                },
            },
        ],
    }


@pytest.fixture
def mock_newsletter_content():
    """Minimal synthetic response matching newsletter_writer.py JSON schema."""
    return {
        "subject": "Sacramento just eliminated your city's infrastructure funding.",
        "preview_text": "AB1751 removes impact fees. Once you understand the mechanism, you can't unsee it.",
        "dek": "This week Sacramento moved to strip cities of impact fee authority.",
        "story": [
            {
                "line1": "The fees are gone.",
                "line2": "The infrastructure isn't.",
                "reveal": "your city's budget.",
                "body": "AB1751 would prohibit cities from charging impact fees on new housing.",
            },
            {
                "line1": "Three bills.",
                "line2": "One objective.",
                "reveal": "local control.",
                "body": "The mechanism is simple: eliminate the fees, eliminate the discretion.",
            },
            {
                "line1": "The math doesn't work.",
                "line2": "Sacramento doesn't care.",
                "reveal": "fiscal reality.",
                "body": "Cities must fund infrastructure somehow. This bill offers no alternative.",
            },
            {
                "line1": "This is the pattern.",
                "line2": "We've seen it before.",
                "reveal": "who decides.",
                "body": "Every session brings new attempts to shift decisions from cities to Sacramento.",
            },
        ],
        "watch_items": [
            {
                "bill_number": "AB1751",
                "author": "TestAuthor",
                "label": "AB1751 — Impact Fee Elimination Act",
                "one_line": "Eliminates city impact fees and all local discretionary review.",
                "flag": True,
            }
        ],
        "call_to_action": {
            "heading": "Contact your Assembly member today.",
            "body": "AB1751 is in committee. Call your rep and tell them to vote no.",
        },
        "close": {
            "heading": "Stay focused.",
            "body": "This session is long. The pattern is clear. We're watching.",
        },
    }


# ---------------------------------------------------------------------------
# Voice text fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def csf_voice_text():
    return "Voice: Default CSF advocacy tone. Expose, Outrage, Activate, Change."


@pytest.fixture
def cma_voice_text():
    return "Voice: CMA professional peer tone. Inform, Connect, Coordinate, Sustain."
