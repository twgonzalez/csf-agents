"""Tests: legislative_intel.py — pure-logic bucket functions.

All tests are deterministic and use freeze_time("2026-03-01") so hearing
dates and stall calculations are stable regardless of when the suite runs.

No API calls. No file I/O (all functions accept raw dicts).
"""
import pytest
from datetime import date
from freezegun import freeze_time

from agents.legislative.legislative_intel import (
    _parse_ca_date,
    _risk_count,
    _classify_stage,
    _is_routing_action,
    _find_urgent,
    _find_moving,
    _find_amended,
    _find_stalled,
)
from agents.newsletter.newsletter_writer import (
    _build_digest_context,
    _build_anti_repetition_block,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def high_risk_bill_with_hearing():
    """High-risk bill (4 criteria) with hearing eligibility March 7 — 6 days from 2026-03-01."""
    return {
        "bill_number": "AB1710",
        "title": "Housing developments: ordinances, policies, and standards",
        "author": "Carrillo",
        "status": "Referred to Coms.",
        "status_date": "2026-02-23",
        "analysis": {
            "pro_housing_production": "strong",
            "densification": "strong",
            "reduce_discretion": "strong",
            "cost_to_cities": "strong",
        },
        "upcoming_hearings": [],
        "actions": [
            {"date": "2026-02-04", "description": "Read first time. To print.", "chamber": "Assembly"},
            {"date": "2026-02-05", "description": "From printer. May be heard in committee March 7.", "chamber": "Assembly"},
            {"date": "2026-02-23", "description": "Referred to Coms. on H. & C.D. and L. GOV.", "chamber": "Assembly"},
        ],
        "watchlist": True,
        "watchlist_note": "Staff-identified",
    }


@pytest.fixture
def high_risk_bill_no_hearing():
    """High-risk bill (3 criteria) with no upcoming hearing."""
    return {
        "bill_number": "AB9999",
        "title": "Zoning: preemption test bill",
        "author": "Test Author",
        "status": "Introduced",
        "status_date": "2026-02-15",
        "analysis": {
            "pro_housing_production": "strong",
            "densification": "strong",
            "reduce_discretion": "strong",
            "cost_to_cities": "none",
        },
        "upcoming_hearings": [],
        "actions": [
            {"date": "2026-02-15", "description": "Introduced.", "chamber": "Assembly"},
        ],
        "watchlist": False,
        "watchlist_note": "",
    }


@pytest.fixture
def low_risk_bill_with_hearing():
    """Low-risk bill (0 criteria) with a hearing eligibility date."""
    return {
        "bill_number": "SB8888",
        "title": "Office of Youth Homelessness Prevention",
        "author": "Smith",
        "status": "Introduced",
        "status_date": "2026-02-12",
        "analysis": {
            "pro_housing_production": "none",
            "densification": "none",
            "reduce_discretion": "none",
            "cost_to_cities": "none",
        },
        "upcoming_hearings": [],
        "actions": [
            {"date": "2026-02-12", "description": "Read first time. To print.", "chamber": "Assembly"},
            {"date": "2026-02-13", "description": "From printer. May be heard in committee March 15.", "chamber": "Assembly"},
        ],
        "watchlist": False,
        "watchlist_note": "",
    }


@pytest.fixture
def moving_bill():
    """Bill that passed a committee within the last 14 days of 2026-03-01."""
    return {
        "bill_number": "SB100",
        "title": "Transit-oriented development expansion",
        "author": "Wiener",
        "status": "In Assembly. Read first time.",
        "status_date": "2026-02-26",
        "analysis": {
            "pro_housing_production": "strong",
            "densification": "strong",
            "reduce_discretion": "moderate",
            "cost_to_cities": "none",
        },
        "upcoming_hearings": [],
        "actions": [
            {"date": "2026-01-26", "description": "Read third time. Passed. (Ayes 24. Noes 10.)", "chamber": "Senate"},
            {"date": "2026-01-26", "description": "Ordered to the Assembly.", "chamber": "Senate"},
            {"date": "2026-02-20", "description": "From committee: Do pass. (Ayes 8. Noes 0.)", "chamber": "Assembly"},
            {"date": "2026-02-26", "description": "In Assembly. Read first time.", "chamber": "Assembly"},
        ],
        "watchlist": False,
        "watchlist_note": "",
    }


@pytest.fixture
def amended_bill():
    """Bill that received amendments within the last 14 days of 2026-03-01."""
    return {
        "bill_number": "AB200",
        "title": "Housing: objective standards",
        "author": "Jones",
        "status": "Amended",
        "status_date": "2026-02-20",
        "analysis": {
            "pro_housing_production": "strong",
            "densification": "moderate",
            "reduce_discretion": "none",
            "cost_to_cities": "none",
        },
        "upcoming_hearings": [],
        "actions": [
            {"date": "2026-01-10", "description": "Introduced.", "chamber": "Assembly"},
            {"date": "2026-02-20", "description": "From committee chair, with author's amendments: Amend, and re-refer.", "chamber": "Assembly"},
        ],
        "watchlist": False,
        "watchlist_note": "",
    }


@pytest.fixture
def stalled_high_risk_bill():
    """High-risk bill (2 criteria) with no movement in 35 days (stale as of 2026-03-01)."""
    return {
        "bill_number": "AB300",
        "title": "Planning: ministerial approval expansion",
        "author": "Lee",
        "status": "In committee",
        "status_date": "2026-01-20",   # 40 days before 2026-03-01
        "analysis": {
            "pro_housing_production": "strong",
            "densification": "strong",
            "reduce_discretion": "none",
            "cost_to_cities": "none",
        },
        "upcoming_hearings": [],
        "actions": [
            {"date": "2026-01-10", "description": "Introduced.", "chamber": "Assembly"},
            {"date": "2026-01-20", "description": "Referred to Com. on H. & C.D.", "chamber": "Assembly"},
        ],
        "watchlist": False,
        "watchlist_note": "",
    }


@pytest.fixture
def cross_chamber_bill():
    """High-risk bill (3 criteria) in Senate queue — should NOT be flagged as stalled."""
    return {
        "bill_number": "AB400",
        "title": "Density bonus: statewide expansion",
        "author": "Garcia",
        "status": "In Senate. Read first time.",
        "status_date": "2026-01-27",   # 33 days before 2026-03-01
        "analysis": {
            "pro_housing_production": "strong",
            "densification": "strong",
            "reduce_discretion": "strong",
            "cost_to_cities": "none",
        },
        "upcoming_hearings": [],
        "actions": [
            {"date": "2026-01-26", "description": "Read third time. Passed. (Ayes 75. Noes 0.)", "chamber": "Assembly"},
            {"date": "2026-01-27", "description": "In Senate. Read first time. To Com. on RLS. for assignment.", "chamber": "Senate"},
        ],
        "watchlist": False,
        "watchlist_note": "",
    }


# ---------------------------------------------------------------------------
# _parse_ca_date
# ---------------------------------------------------------------------------

@freeze_time("2026-03-01")
def test_parse_ca_date_full_month_name():
    d = _parse_ca_date("March 7")
    assert d == date(2026, 3, 7)


@freeze_time("2026-03-01")
def test_parse_ca_date_padded_day():
    d = _parse_ca_date("March 15")
    assert d == date(2026, 3, 15)


@freeze_time("2026-03-01")
def test_parse_ca_date_abbreviated_month():
    d = _parse_ca_date("Mar 7")
    assert d == date(2026, 3, 7)


@freeze_time("2026-03-01")
def test_parse_ca_date_trailing_period():
    """Trailing period is stripped before parsing."""
    d = _parse_ca_date("March 7.")
    assert d == date(2026, 3, 7)


@freeze_time("2026-03-01")
def test_parse_ca_date_empty_string():
    assert _parse_ca_date("") is None


@freeze_time("2026-03-01")
def test_parse_ca_date_invalid():
    assert _parse_ca_date("not a date") is None


# ---------------------------------------------------------------------------
# _risk_count
# ---------------------------------------------------------------------------

def test_risk_count_all_strong():
    bill = {
        "analysis": {
            "pro_housing_production": "strong",
            "densification": "strong",
            "reduce_discretion": "strong",
            "cost_to_cities": "strong",
        }
    }
    assert _risk_count(bill) == 4


def test_risk_count_mixed():
    bill = {
        "analysis": {
            "pro_housing_production": "strong",
            "densification": "moderate",
            "reduce_discretion": "none",
            "cost_to_cities": "none",
        }
    }
    assert _risk_count(bill) == 2


def test_risk_count_all_none():
    bill = {
        "analysis": {
            "pro_housing_production": "none",
            "densification": "none",
            "reduce_discretion": "none",
            "cost_to_cities": "none",
        }
    }
    assert _risk_count(bill) == 0


def test_risk_count_no_analysis():
    assert _risk_count({}) == 0


# ---------------------------------------------------------------------------
# _classify_stage
# ---------------------------------------------------------------------------

def test_classify_stage_committee_passage():
    assert _classify_stage("From committee: Do pass. (Ayes 8. Noes 0.)") == "committee_passage"


def test_classify_stage_do_pass_as_amended():
    assert _classify_stage("Do pass as amended and re-refer.") == "committee_passage"


def test_classify_stage_floor_passage():
    assert _classify_stage("Read third time. Passed. (Ayes 75. Noes 0.)") == "floor_passage"


def test_classify_stage_cross_chamber_senate():
    assert _classify_stage("In Senate. Read first time. To Com. on RLS.") == "cross_chamber"


def test_classify_stage_cross_chamber_assembly():
    assert _classify_stage("In Assembly. Read first time. Held at Desk.") == "cross_chamber"


def test_classify_stage_enacted():
    assert _classify_stage("Chaptered by Secretary of State.") == "enacted"


def test_classify_stage_committee_referral():
    assert _classify_stage("Referred to Coms. on H. & C.D. and L. GOV.") == "committee_referral"


def test_classify_stage_introduced():
    assert _classify_stage("Introduced. Read first time. To print.") == "introduced"


# ---------------------------------------------------------------------------
# _is_routing_action
# ---------------------------------------------------------------------------

def test_is_routing_action_senate_queue():
    assert _is_routing_action("In Senate. Read first time. To Com. on RLS. for assignment.")


def test_is_routing_action_assembly_queue():
    assert _is_routing_action("In Assembly. Read first time. Held at Desk.")


def test_is_routing_action_committee_hearing_not_routing():
    assert not _is_routing_action("From committee: Do pass. (Ayes 8. Noes 0.)")


def test_is_routing_action_floor_passage_not_routing():
    assert not _is_routing_action("Read third time. Passed. (Ayes 75. Noes 0.)")


# ---------------------------------------------------------------------------
# _find_urgent — frozen at 2026-03-01
# ---------------------------------------------------------------------------

@freeze_time("2026-03-01")
def test_urgent_includes_hearing_within_window(high_risk_bill_with_hearing):
    """AB1710 has 'May be heard in committee March 7' — 6 days out, within 14-day window."""
    bills = {"AB1710": high_risk_bill_with_hearing}
    result = _find_urgent(bills, lookahead=14)
    assert len(result) == 1
    assert result[0]["bill_number"] == "AB1710"
    assert result[0]["eligible_date"] == "2026-03-07"
    assert result[0]["days_until"] == 6


@freeze_time("2026-03-01")
def test_urgent_respects_lookahead_window():
    """March 15 is 14 days out — just within the 14-day window."""
    bill = {
        "bill_number": "AB999",
        "title": "Test",
        "author": "Test",
        "status": "Introduced",
        "status_date": "2026-02-13",
        "analysis": {},
        "upcoming_hearings": [],
        "actions": [
            {"date": "2026-02-13", "description": "From printer. May be heard in committee March 15.", "chamber": "Assembly"},
        ],
        "watchlist": False,
        "watchlist_note": "",
    }
    result = _find_urgent({"AB999": bill}, lookahead=14)
    assert len(result) == 1

    result_narrow = _find_urgent({"AB999": bill}, lookahead=7)
    assert len(result_narrow) == 0


@freeze_time("2026-03-01")
def test_urgent_includes_low_risk_bills(low_risk_bill_with_hearing):
    """Low-risk bills are included — the caller filters; urgent covers all."""
    bills = {"SB8888": low_risk_bill_with_hearing}
    result = _find_urgent(bills, lookahead=14)
    assert len(result) == 1
    assert result[0]["risk_count"] == 0


@freeze_time("2026-03-01")
def test_urgent_excludes_no_hearing_bill(high_risk_bill_no_hearing):
    """Bills with no hearing eligibility date are not in urgent."""
    bills = {"AB9999": high_risk_bill_no_hearing}
    result = _find_urgent(bills, lookahead=14)
    assert len(result) == 0


@freeze_time("2026-03-01")
def test_urgent_sorted_soonest_first(high_risk_bill_with_hearing, low_risk_bill_with_hearing):
    """Urgent list is sorted by eligible_date ascending."""
    bills = {
        "SB8888": low_risk_bill_with_hearing,  # March 15
        "AB1710": high_risk_bill_with_hearing,  # March 7
    }
    result = _find_urgent(bills, lookahead=14)
    assert result[0]["bill_number"] == "AB1710"   # soonest first
    assert result[1]["bill_number"] == "SB8888"


@freeze_time("2026-03-01")
def test_urgent_prefers_structured_calendar_over_parsed():
    """Structured upcoming_hearings[] takes priority over parsed action text."""
    bill = {
        "bill_number": "AB500",
        "title": "Test",
        "author": "Test",
        "status": "In Committee",
        "status_date": "2026-02-20",
        "analysis": {},
        "upcoming_hearings": [
            {"date": "2026-03-05", "committee": "Assembly Housing", "location": "Sacramento"},
        ],
        "actions": [
            {"date": "2026-02-20", "description": "From printer. May be heard in committee March 12.", "chamber": "Assembly"},
        ],
        "watchlist": False,
        "watchlist_note": "",
    }
    result = _find_urgent({"AB500": bill}, lookahead=14)
    assert len(result) == 1
    assert result[0]["eligible_date"] == "2026-03-05"   # from structured calendar, not March 12
    assert result[0]["date_source"] == "calendar"


# ---------------------------------------------------------------------------
# _find_moving — frozen at 2026-03-01
# ---------------------------------------------------------------------------

@freeze_time("2026-03-01")
def test_moving_includes_committee_passage_in_window(moving_bill):
    """SB100 passed a committee on 2026-02-20 — 9 days before 2026-03-01."""
    bills = {"SB100": moving_bill}
    result = _find_moving(bills, lookback=14)
    assert len(result) == 1
    assert result[0]["bill_number"] == "SB100"
    assert result[0]["current_stage"] in ("committee_passage", "cross_chamber")


@freeze_time("2026-03-01")
def test_moving_excludes_old_committee_passage():
    """Committee passage more than 14 days ago is not in moving."""
    bill = {
        "bill_number": "AB600",
        "title": "Old mover",
        "author": "Old",
        "status": "In Assembly",
        "status_date": "2026-01-15",
        "analysis": {"pro_housing_production": "strong", "densification": "none",
                     "reduce_discretion": "none", "cost_to_cities": "none"},
        "upcoming_hearings": [],
        "actions": [
            {"date": "2026-01-14", "description": "From committee: Do pass. (Ayes 11. Noes 0.)", "chamber": "Assembly"},
        ],
        "watchlist": False,
        "watchlist_note": "",
    }
    result = _find_moving({"AB600": bill}, lookback=14)
    assert len(result) == 0


@freeze_time("2026-03-01")
def test_moving_excludes_introductions_and_referrals(high_risk_bill_no_hearing):
    """Introductions and referrals are not stage advancements."""
    bills = {"AB9999": high_risk_bill_no_hearing}
    result = _find_moving(bills, lookback=14)
    assert len(result) == 0


@freeze_time("2026-03-01")
def test_moving_sorted_by_risk_desc(moving_bill, high_risk_bill_no_hearing):
    """Moving list is sorted by risk_count descending."""
    # moving_bill has 3 criteria, add a 1-criteria mover for comparison
    low_risk_mover = {
        "bill_number": "SB200",
        "title": "Low-risk mover",
        "author": "Someone",
        "status": "In Senate",
        "status_date": "2026-02-25",
        "analysis": {"pro_housing_production": "moderate", "densification": "none",
                     "reduce_discretion": "none", "cost_to_cities": "none"},
        "upcoming_hearings": [],
        "actions": [
            {"date": "2026-02-25", "description": "From committee: Do pass. (Ayes 7. Noes 0.)", "chamber": "Senate"},
        ],
        "watchlist": False,
        "watchlist_note": "",
    }
    bills = {"SB100": moving_bill, "SB200": low_risk_mover}
    result = _find_moving(bills, lookback=14)
    assert len(result) == 2
    assert result[0]["risk_count"] >= result[1]["risk_count"]


# ---------------------------------------------------------------------------
# _find_amended — frozen at 2026-03-01
# ---------------------------------------------------------------------------

@freeze_time("2026-03-01")
def test_amended_includes_recent_amendment(amended_bill):
    """AB200 received amendments on 2026-02-20 — 9 days before 2026-03-01."""
    bills = {"AB200": amended_bill}
    result = _find_amended(bills, lookback=14)
    assert len(result) == 1
    assert result[0]["bill_number"] == "AB200"
    assert result[0]["amendment_date"] == "2026-02-20"


@freeze_time("2026-03-01")
def test_amended_excludes_old_amendments():
    """Amendments older than lookback are not in amended."""
    bill = {
        "bill_number": "SB700",
        "title": "Old amendment",
        "author": "Old",
        "status": "Amended",
        "status_date": "2026-01-05",
        "analysis": {"pro_housing_production": "strong", "densification": "none",
                     "reduce_discretion": "none", "cost_to_cities": "none"},
        "upcoming_hearings": [],
        "actions": [
            {"date": "2026-01-05", "description": "Author's amendments: Amend, and re-refer.", "chamber": "Assembly"},
        ],
        "watchlist": False,
        "watchlist_note": "",
    }
    result = _find_amended({"SB700": bill}, lookback=14)
    assert len(result) == 0


@freeze_time("2026-03-01")
def test_amended_one_entry_per_bill():
    """Each bill appears at most once in the amended list."""
    bill = {
        "bill_number": "AB800",
        "title": "Multiple amendments",
        "author": "Someone",
        "status": "Amended",
        "status_date": "2026-02-25",
        "analysis": {"pro_housing_production": "strong", "densification": "none",
                     "reduce_discretion": "none", "cost_to_cities": "none"},
        "upcoming_hearings": [],
        "actions": [
            {"date": "2026-02-20", "description": "First amendments: Amend.", "chamber": "Assembly"},
            {"date": "2026-02-25", "description": "Second amendments: Amend, and re-refer.", "chamber": "Assembly"},
        ],
        "watchlist": False,
        "watchlist_note": "",
    }
    result = _find_amended({"AB800": bill}, lookback=14)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# _find_stalled — frozen at 2026-03-01
# ---------------------------------------------------------------------------

@freeze_time("2026-03-01")
def test_stalled_includes_high_risk_no_movement(stalled_high_risk_bill):
    """AB300 is high-risk (2 criteria) and stalled since 2026-01-20 (40 days)."""
    bills = {"AB300": stalled_high_risk_bill}
    result = _find_stalled(bills, threshold=30)
    assert len(result) == 1
    assert result[0]["bill_number"] == "AB300"
    assert result[0]["days_stalled"] == 40


@freeze_time("2026-03-01")
def test_stalled_excludes_low_risk_bills(low_risk_bill_with_hearing):
    """Low-risk bills (0 criteria) are never flagged as stalled."""
    # Make it old
    low_risk_bill_with_hearing["status_date"] = "2026-01-01"
    bills = {"SB8888": low_risk_bill_with_hearing}
    result = _find_stalled(bills, threshold=30)
    assert len(result) == 0


@freeze_time("2026-03-01")
def test_stalled_excludes_cross_chamber_bills(cross_chamber_bill):
    """Bills waiting in the other chamber's Rules queue are NOT stalled."""
    bills = {"AB400": cross_chamber_bill}
    result = _find_stalled(bills, threshold=30)
    assert len(result) == 0


@freeze_time("2026-03-01")
def test_stalled_excludes_recently_updated_bills(stalled_high_risk_bill):
    """Bills updated within threshold are not stalled."""
    stalled_high_risk_bill["status_date"] = "2026-02-25"  # 4 days ago
    bills = {"AB300": stalled_high_risk_bill}
    result = _find_stalled(bills, threshold=30)
    assert len(result) == 0


@freeze_time("2026-03-01")
def test_stalled_sorted_most_stalled_first():
    """Stalled list is sorted by days_stalled descending."""
    bill_a = {
        "bill_number": "AB001",
        "title": "Stalled A",
        "author": "A",
        "status": "In committee",
        "status_date": "2026-01-01",  # 59 days
        "analysis": {"pro_housing_production": "strong", "densification": "strong",
                     "reduce_discretion": "none", "cost_to_cities": "none"},
        "upcoming_hearings": [],
        "actions": [{"date": "2026-01-01", "description": "Referred to Com.", "chamber": "Assembly"}],
        "watchlist": False,
        "watchlist_note": "",
    }
    bill_b = {
        "bill_number": "AB002",
        "title": "Stalled B",
        "author": "B",
        "status": "In committee",
        "status_date": "2026-01-20",  # 40 days
        "analysis": {"pro_housing_production": "strong", "densification": "strong",
                     "reduce_discretion": "none", "cost_to_cities": "none"},
        "upcoming_hearings": [],
        "actions": [{"date": "2026-01-20", "description": "Referred to Com.", "chamber": "Assembly"}],
        "watchlist": False,
        "watchlist_note": "",
    }
    result = _find_stalled({"AB001": bill_a, "AB002": bill_b}, threshold=30)
    assert len(result) == 2
    assert result[0]["bill_number"] == "AB001"   # 59 days — most stalled
    assert result[1]["bill_number"] == "AB002"


# ---------------------------------------------------------------------------
# _build_digest_context — format smoke tests
# ---------------------------------------------------------------------------

@freeze_time("2026-03-01")
def test_digest_context_empty_on_empty_digest():
    assert _build_digest_context({}) == ""


@freeze_time("2026-03-01")
def test_digest_context_includes_week_summary():
    digest = {
        "week_summary": "This week AB1710 became eligible for its first hearing.",
        "urgent": [],
        "moving": [],
        "amended": [],
        "gut_and_amend": [],
        "spot_bills": [],
    }
    ctx = _build_digest_context(digest)
    assert "AB1710" in ctx
    assert "WHAT HAPPENED THIS WEEK" in ctx


@freeze_time("2026-03-01")
def test_digest_context_includes_urgent_high_risk(high_risk_bill_with_hearing):
    """High-risk urgent bills appear in the digest context."""
    digest = {
        "week_summary": "",
        "urgent": [
            {
                "bill_number": "AB1710",
                "title": high_risk_bill_with_hearing["title"],
                "author": "Carrillo",
                "status": "Referred",
                "eligible_date": "2026-03-07",
                "days_until": 6,
                "date_source": "parsed",
                "committees": ["Assembly Housing"],
                "text_url": "",
                "analysis": high_risk_bill_with_hearing["analysis"],
                "watchlist": True,
                "watchlist_note": "Staff-identified",
                "risk_count": 4,
            }
        ],
        "moving": [],
        "amended": [],
        "gut_and_amend": [],
        "spot_bills": [],
    }
    ctx = _build_digest_context(digest)
    assert "AB1710" in ctx
    assert "URGENT" in ctx
    assert "STAFF WATCHLIST" in ctx


@freeze_time("2026-03-01")
def test_digest_context_includes_spot_bills():
    digest = {
        "week_summary": "",
        "urgent": [],
        "moving": [],
        "amended": [],
        "gut_and_amend": [],
        "spot_bills": [
            {"bill_number": "AB1234", "why": "Empty summary at printer stage."}
        ],
    }
    ctx = _build_digest_context(digest)
    assert "SPOT BILL" in ctx
    assert "AB1234" in ctx


# ---------------------------------------------------------------------------
# _build_anti_repetition_block
# ---------------------------------------------------------------------------

def test_anti_repetition_empty_on_no_last_issue():
    assert _build_anti_repetition_block({}) == ""


def test_anti_repetition_includes_story_beats():
    digest = {
        "last_issue": {
            "subject": "Four bills in seven days.",
            "story_beats": ["Four bills.", "Seven days.", "one target: your planning commission."],
            "source_file": "newsletter_2026-W09.html",
        }
    }
    block = _build_anti_repetition_block(digest)
    assert "Four bills." in block
    assert "Seven days." in block
    assert "ANTI-REPETITION" in block
    assert "DO NOT REPEAT" in block


def test_anti_repetition_includes_source_file():
    digest = {
        "last_issue": {
            "subject": "Some subject",
            "story_beats": ["Line one."],
            "source_file": "newsletter_2026-W09.html",
        }
    }
    block = _build_anti_repetition_block(digest)
    assert "newsletter_2026-W09.html" in block
