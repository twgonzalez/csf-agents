#!/usr/bin/env python3
"""
legislative_intel.py — Weekly Legislative Intelligence Layer
California Stewardship Fund

Sits between housing_analyzer and newsletter_writer in the pipeline.
Answers "what did the legislature actually DO this week?" so the newsletter
writer can lead with real legislative action rather than a static bill list.

Pipeline position:
    bill_tracker.py → housing_analyzer.py → legislative_intel.py → newsletter_writer.py

Reads:
    data/bills/tracked_bills.json          — bill data with risk scores
    outputs/clients/<slug>/newsletter/*.html — last week's newsletter (anti-repetition)

Writes:
    data/legislative/action_digest.json

Claude call (1):
    - Gut-and-amend detection (CA tactic: complete bill content replacement)
    - Spot bill detection (placeholder/boilerplate bills awaiting activation)
    - week_summary paragraph (factual anchor for the newsletter story arc)

Pure-logic buckets (no API cost):
    - urgent:   bills with hearing eligibility dates within N days
    - moving:   bills that advanced a legislative stage this week
    - amended:  bills with amendment actions in the lookback window
    - stalled:  high-risk bills (2+ criteria) with no movement in 30+ days

Usage:
    python agents/legislative/legislative_intel.py
    python agents/legislative/legislative_intel.py --client cma
    python agents/legislative/legislative_intel.py --lookahead 7
    python agents/legislative/legislative_intel.py --no-claude    # pure logic only
    python agents/legislative/legislative_intel.py --lookback 7

Requires:
    ANTHROPIC_API_KEY  (only needed for Claude call; --no-claude skips it)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path bootstrap — add project root before any intra-package imports
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_PROJECT_ROOT / ".env", override=True)
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="  %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = _PROJECT_ROOT
BILLS_FILE   = PROJECT_ROOT / "data" / "bills" / "tracked_bills.json"
DIGEST_DIR   = PROJECT_ROOT / "data" / "legislative"
DIGEST_FILE  = DIGEST_DIR / "action_digest.json"
CLIENTS_DIR  = PROJECT_ROOT / "clients"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_CLIENT      = "csf"
HEARING_LOOKAHEAD   = 14    # days: flag hearings this far out
AMENDMENT_LOOKBACK  = 14    # days: flag amendments this recent
STALL_THRESHOLD     = 30    # days: flag high-risk bills with no movement
MIN_RISK_FOR_STALL  = 2     # minimum criteria (strong/moderate) to be "stalled"

# Risk analysis criterion keys — matches housing_analyzer + bill_utils
_CRIT_KEYS = {
    "A": "pro_housing_production",
    "B": "densification",
    "C": "reduce_discretion",
    "D": "cost_to_cities",
}

# Regex: parse "May be heard in committee March 7" from action text
_HEARING_RE = re.compile(
    r"may be heard in committee\s+([A-Za-z]+ \d{1,2})",
    re.IGNORECASE,
)
# Regex: parse "May be acted upon on or after March 16" from action text
_ACTION_DATE_RE = re.compile(
    r"may be acted upon on or after\s+([A-Za-z]+ \d{1,2})",
    re.IGNORECASE,
)

# Action description keywords indicating legislative stage advancement
_ADVANCE_KEYWORDS = [
    "do pass",
    "pass as amended",
    "do pass as amended",
    "read third time",
    "ordered to the senate",
    "ordered to the assembly",
    "in senate. read first time",
    "in assembly. read first time",
    "signed by governor",
    "chaptered",
]

# Action description keywords indicating amendments
_AMEND_KEYWORDS = ["amended", "amend,", "amendment"]

# Stage ordering for sorting and filtering (higher = further along)
_STAGE_ORDER = {
    "introduced":         0,
    "committee_referral": 1,
    "committee_passage":  2,
    "floor_progression":  3,
    "floor_reading":      4,
    "floor_passage":      5,
    "cross_chamber":      6,
    "enacted":            7,
    "other":             -1,
}

# Cross-chamber / routing-step statuses — don't flag as "stalled"
_ROUTING_PATTERNS = [
    "in senate",
    "in assembly",
    "to com. on rls. for assignment",
    "read first time. to com",
    "to com. on rls",
]


# ---------------------------------------------------------------------------
# Helper: parse CA legislative date string
# ---------------------------------------------------------------------------

def _parse_ca_date(date_str: str) -> Optional[date]:
    """
    Parse a California legislative date string like "March 7" or "January 14".

    Assumes current year. If the result is more than 30 days in the past
    (e.g., a December date encountered in January), tries next year.

    Returns None on parse failure.
    """
    if not date_str:
        return None
    date_str = date_str.strip().rstrip(".")
    today = date.today()

    # Include the year in the parse string to avoid Python 3.15 ambiguity warning
    for fmt in ("%Y %B %d", "%Y %b %d"):
        try:
            candidate = datetime.strptime(f"{today.year} {date_str}", fmt).date()
            # If the date is more than 30 days in the past, try next year
            if candidate < today - timedelta(days=30):
                candidate = datetime.strptime(f"{today.year + 1} {date_str}", fmt).date()
            return candidate
        except ValueError:
            continue

    return None


# ---------------------------------------------------------------------------
# Helper: count risk signals on a bill
# ---------------------------------------------------------------------------

def _risk_count(bill: dict) -> int:
    """Count how many criteria are strong or moderate on a bill's analysis."""
    a = bill.get("analysis", {})
    return sum(1 for v in _CRIT_KEYS.values() if a.get(v) in ("strong", "moderate"))


# ---------------------------------------------------------------------------
# Helper: classify an action description into a stage label
# ---------------------------------------------------------------------------

def _classify_stage(description: str) -> str:
    """Return a stage label for an action description."""
    dl = description.lower()
    if "signed by governor" in dl or "chaptered" in dl:
        return "enacted"
    if "ordered to the senate" in dl or ("in senate" in dl and "read first time" in dl):
        return "cross_chamber"
    if "ordered to the assembly" in dl or ("in assembly" in dl and "read first time" in dl):
        return "cross_chamber"
    if "read third time" in dl and "passed" in dl:
        return "floor_passage"
    if "read third time" in dl:
        return "floor_reading"
    if "ordered to third reading" in dl:
        return "floor_reading"
    if "read second time" in dl:
        return "floor_progression"
    if "do pass" in dl or "pass as amended" in dl:
        return "committee_passage"
    if "referred to com" in dl or "referred to coms" in dl:
        return "committee_referral"
    if "introduced" in dl or "read first time" in dl:
        return "introduced"
    return "other"


def _is_routing_action(description: str) -> bool:
    """Return True if this action is a standard routing step (not a real stall)."""
    dl = description.lower()
    return any(p in dl for p in _ROUTING_PATTERNS)


# ---------------------------------------------------------------------------
# Pure-logic bucket: URGENT
# ---------------------------------------------------------------------------

def _find_urgent(bills: dict, lookahead: int = HEARING_LOOKAHEAD) -> list[dict]:
    """
    Bills with hearing eligibility dates within `lookahead` days.

    Two sources (in priority order):
      1. Structured upcoming_hearings[] (populated later in session by LegiScan)
      2. "May be heard in committee [date]" parsed from action text

    The CA 30-day rule date is the *earliest* a bill can be heard — not a
    confirmed hearing date. Still the best proxy available early in session.

    Sorted by eligible_date ascending (soonest first).
    """
    today    = date.today()
    deadline = today + timedelta(days=lookahead)
    urgent   = []

    for bn, bill in bills.items():
        earliest: Optional[date] = None
        source = "parsed"

        # 1. Structured upcoming_hearings (preferred — more accurate)
        for h in bill.get("upcoming_hearings", []):
            try:
                d = date.fromisoformat(h["date"])
                if today <= d <= deadline:
                    earliest = d
                    source   = "calendar"
                    break
            except (KeyError, ValueError):
                pass

        # 2. Parse from action text
        if not earliest:
            for action in bill.get("actions", []):
                m = _HEARING_RE.search(action.get("description", ""))
                if m:
                    parsed = _parse_ca_date(m.group(1))
                    if parsed and today <= parsed <= deadline:
                        earliest = parsed
                        source   = "parsed"
                        break

        if earliest:
            urgent.append({
                "bill_number":   bn,
                "title":         bill.get("title", ""),
                "author":        bill.get("author", ""),
                "status":        bill.get("status", ""),
                "eligible_date": str(earliest),
                "days_until":    (earliest - today).days,
                "date_source":   source,
                "committees":    bill.get("committees", []),
                "text_url":      bill.get("text_url", ""),
                "analysis":      bill.get("analysis", {}),
                "watchlist":     bill.get("watchlist", False),
                "watchlist_note": bill.get("watchlist_note", ""),
                "risk_count":    _risk_count(bill),
            })

    urgent.sort(key=lambda x: (x["eligible_date"], -x["risk_count"]))
    return urgent


# ---------------------------------------------------------------------------
# Pure-logic bucket: MOVING
# ---------------------------------------------------------------------------

def _find_moving(bills: dict, lookback: int = AMENDMENT_LOOKBACK) -> list[dict]:
    """
    Bills that advanced a meaningful legislative stage in the last `lookback` days.

    "Meaningful" = committee passage or later (cross-chamber, floor, enacted).
    Referrals and introductions are normal noise and excluded.

    Sorted by: risk_count desc, stage_order desc.
    """
    today  = date.today()
    cutoff = today - timedelta(days=lookback)
    moving = []

    for bn, bill in bills.items():
        advance_actions: list[dict] = []

        for action in bill.get("actions", []):
            try:
                action_date = date.fromisoformat(action.get("date", ""))
            except ValueError:
                continue
            if action_date < cutoff:
                continue

            stage = _classify_stage(action.get("description", ""))
            if _STAGE_ORDER.get(stage, -1) >= _STAGE_ORDER["committee_passage"]:
                advance_actions.append({
                    "date":        str(action_date),
                    "description": action.get("description", ""),
                    "stage":       stage,
                    "chamber":     action.get("chamber", ""),
                })

        if advance_actions:
            advance_actions.sort(key=lambda x: x["date"], reverse=True)
            moving.append({
                "bill_number":     bn,
                "title":           bill.get("title", ""),
                "author":          bill.get("author", ""),
                "status":          bill.get("status", ""),
                "current_stage":   advance_actions[0]["stage"],
                "advance_actions": advance_actions,
                "text_url":        bill.get("text_url", ""),
                "analysis":        bill.get("analysis", {}),
                "watchlist":       bill.get("watchlist", False),
                "watchlist_note":  bill.get("watchlist_note", ""),
                "risk_count":      _risk_count(bill),
            })

    moving.sort(
        key=lambda x: (-x["risk_count"], -_STAGE_ORDER.get(x["current_stage"], 0))
    )
    return moving


# ---------------------------------------------------------------------------
# Pure-logic bucket: AMENDED
# ---------------------------------------------------------------------------

def _find_amended(bills: dict, lookback: int = AMENDMENT_LOOKBACK) -> list[dict]:
    """
    Bills with amendment actions in the last `lookback` days.

    One entry per bill (most recent amendment action).
    Sorted by: risk_count desc, amendment_date desc.
    """
    today  = date.today()
    cutoff = today - timedelta(days=lookback)
    amended = []

    for bn, bill in bills.items():
        for action in bill.get("actions", []):
            desc = action.get("description", "")
            if not any(kw in desc.lower() for kw in _AMEND_KEYWORDS):
                continue
            try:
                action_date = date.fromisoformat(action.get("date", ""))
            except ValueError:
                continue
            if action_date >= cutoff:
                amended.append({
                    "bill_number":           bn,
                    "title":                 bill.get("title", ""),
                    "author":                bill.get("author", ""),
                    "amendment_date":        str(action_date),
                    "amendment_description": desc[:250],
                    "text_url":              bill.get("text_url", ""),
                    "analysis":              bill.get("analysis", {}),
                    "watchlist":             bill.get("watchlist", False),
                    "risk_count":            _risk_count(bill),
                })
                break  # one entry per bill (first recent amendment)

    amended.sort(key=lambda x: (-x["risk_count"], x["amendment_date"]), reverse=False)
    amended.sort(key=lambda x: (-x["risk_count"],))
    return amended


# ---------------------------------------------------------------------------
# Pure-logic bucket: STALLED
# ---------------------------------------------------------------------------

def _find_stalled(bills: dict, threshold: int = STALL_THRESHOLD) -> list[dict]:
    """
    High-risk bills (≥ MIN_RISK_FOR_STALL criteria) with no status movement
    in `threshold` days.

    Excludes bills whose most recent action is a standard routing step
    (cross-chamber introduction, Rules Committee assignment) — those are
    normal waiting patterns, not true stalls.

    Sorted by: days_stalled desc (most stalled first).
    """
    today  = date.today()
    cutoff = today - timedelta(days=threshold)
    stalled = []

    for bn, bill in bills.items():
        if _risk_count(bill) < MIN_RISK_FOR_STALL:
            continue

        sd_str = bill.get("status_date", "")
        try:
            sd = date.fromisoformat(sd_str)
        except ValueError:
            continue

        if sd >= cutoff:
            continue  # recently updated — not stalled

        # Check most recent action — skip routing steps
        actions = bill.get("actions", [])
        if actions:
            latest_desc = actions[-1].get("description", "")
            if _is_routing_action(latest_desc):
                continue  # in another chamber's queue — not a real stall

        days = (today - sd).days
        stalled.append({
            "bill_number":  bn,
            "title":        bill.get("title", ""),
            "author":       bill.get("author", ""),
            "status":       bill.get("status", ""),
            "status_date":  sd_str,
            "days_stalled": days,
            "analysis":     bill.get("analysis", {}),
            "risk_count":   _risk_count(bill),
        })

    stalled.sort(key=lambda x: -x["days_stalled"])
    return stalled


# ---------------------------------------------------------------------------
# Last issue extraction
# ---------------------------------------------------------------------------

def _extract_last_issue(client_id: str = DEFAULT_CLIENT) -> dict:
    """
    Parse the most recent newsletter HTML for the given client.

    Extracts:
      - subject: from the <title> tag
      - preview_text: from the hidden preview div
      - story_beats: the three-beat stacked headings (line1, line2, reveal)
        identified by consecutive short-line groups followed by a long body

    Returns an empty dict if no newsletter is found or parsing fails.
    """
    newsletter_dir = PROJECT_ROOT / "outputs" / "clients" / client_id / "newsletter"
    if not newsletter_dir.exists():
        log.debug(f"Newsletter directory not found: {newsletter_dir}")
        return {}

    htmls = sorted(
        newsletter_dir.glob("newsletter_*.html"),
        key=lambda p: p.name,
        reverse=True,
    )
    if not htmls:
        log.debug(f"No newsletter HTML files found in {newsletter_dir}")
        return {}

    try:
        from bs4 import BeautifulSoup
        html     = htmls[0].read_text(encoding="utf-8")
        soup     = BeautifulSoup(html, "lxml")
        filename = htmls[0].name

        # Subject from <title>: "Local Control Intelligence — WEEK OF MARCH 1, 2026"
        title_tag = soup.find("title")
        subject   = title_tag.text.strip() if title_tag else ""

        # Preview text — the hidden div right after <body>
        preview = ""
        hidden = soup.find("div", style=lambda s: s and "display:none" in s)
        if hidden:
            preview = hidden.get_text(strip=True)

        # Story beats: three-beat stacked headings are short consecutive lines
        # followed by a longer body paragraph. We stop collecting at section headers.
        _STOP_WORDS = {"What to Watch", "Before You Close This"}
        all_text = [
            t.strip()
            for t in soup.get_text(separator="\n", strip=True).split("\n")
            if t.strip()
        ]

        story_beats: list[str] = []
        i = 0
        while i < len(all_text) - 3:
            t = all_text[i]
            if t in _STOP_WORDS:
                break  # reached the watch-list section — stop

            # Three-beat pattern: three short lines followed by one long line
            if (
                2 <= len(t) <= 60
                and not t.startswith("http")
                and i + 1 < len(all_text)
                and 2 <= len(all_text[i + 1]) <= 60
                and i + 2 < len(all_text)
                and 2 <= len(all_text[i + 2]) <= 60
                and i + 3 < len(all_text)
                and len(all_text[i + 3]) > 80  # long body paragraph follows
            ):
                story_beats.extend([t, all_text[i + 1], all_text[i + 2]])
                i += 4
                continue
            i += 1

        log.debug(f"Extracted {len(story_beats)} story beats from {filename}")
        return {
            "subject":     subject,
            "preview_text": preview,
            "story_beats": story_beats,
            "source_file": filename,
        }

    except Exception as exc:
        log.warning(f"Could not extract last issue from {htmls[0].name}: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Claude call: gut-and-amend detection + spot bills + week_summary
# ---------------------------------------------------------------------------

def _call_claude(
    bills:      dict,
    moving:     list[dict],
    amended:    list[dict],
    urgent:     list[dict],
    last_issue: dict,
) -> dict:
    """
    Single Claude call producing:
      - gut_and_amend: list of bills with CA gut-and-amend signals
      - spot_bills:    list of placeholder/boilerplate bills
      - week_summary:  1–2 sentence factual anchor for the newsletter

    Falls back to empty results with a warning if ANTHROPIC_API_KEY is unset
    or the call fails. The digest is still written — pure-logic buckets remain.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        log.warning(
            "ANTHROPIC_API_KEY not set — skipping Claude call. "
            "Digest will have empty gut_and_amend, spot_bills, and week_summary."
        )
        return {"gut_and_amend": [], "spot_bills": [], "week_summary": ""}

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    # --- Build moving context ---
    moving_lines = []
    for m in moving[:10]:
        moving_lines.append(f"- {m['bill_number']}: {m['title'][:65]}")
        moving_lines.append(f"  Stage reached: {m['current_stage']}")
        for adv in m.get("advance_actions", [])[:2]:
            moving_lines.append(f"  [{adv['date']}] {adv['description'][:100]}")
    moving_ctx = (
        "MOVING BILLS (advanced a stage this lookback window):\n"
        + "\n".join(moving_lines)
        if moving_lines else "(No bills advanced a stage this week)"
    )

    # --- Build amended context ---
    amended_lines = []
    for am in amended[:8]:
        summary = bills.get(am["bill_number"], {}).get("summary", "")[:150]
        amended_lines.append(f"- {am['bill_number']}: {am['title'][:65]}")
        amended_lines.append(f"  [{am['amendment_date']}] {am['amendment_description'][:120]}")
        if summary:
            amended_lines.append(f"  Summary: {summary}")
    amended_ctx = (
        "AMENDED BILLS:\n" + "\n".join(amended_lines)
        if amended_lines else "(No bills amended this lookback window)"
    )

    # --- Build urgent context ---
    urgent_lines = []
    for u in urgent[:8]:
        urgent_lines.append(
            f"- {u['bill_number']}: {u['title'][:65]} "
            f"[eligible: {u['eligible_date']}, {u['days_until']} days]"
        )
    urgent_ctx = (
        f"URGENT HEARINGS (within {HEARING_LOOKAHEAD} days):\n" + "\n".join(urgent_lines)
        if urgent_lines else f"(No hearings in the next {HEARING_LOOKAHEAD} days)"
    )

    # --- Build spot bill candidates ---
    spot_candidates = []
    for bn, bill in list(bills.items())[:80]:
        summary = (bill.get("summary") or "").strip()
        title   = bill.get("title", "")
        actions = bill.get("actions", [])
        if len(summary) < 40 or not summary:
            last_act = actions[-1].get("description", "")[:70] if actions else ""
            spot_candidates.append(
                f"[CANDIDATE] {bn}: {title[:60]} | summary: '{summary}' | last: {last_act}"
            )
    spot_ctx = (
        "POTENTIAL SPOT BILL CANDIDATES (short/empty summaries):\n"
        + "\n".join(spot_candidates[:30])
        if spot_candidates else "(No obvious spot bill candidates)"
    )

    # --- Anti-repetition context ---
    last_beats = last_issue.get("story_beats", [])[:9]
    anti_rep_ctx = ""
    if last_beats:
        anti_rep_ctx = (
            "\nLAST WEEK'S NEWSLETTER FRAMINGS (do NOT repeat these patterns):\n"
            + "\n".join(f"  - {b}" for b in last_beats)
        )

    system_prompt = """\
You are a California legislative intelligence analyst for a local government advocacy
organization that monitors housing preemption bills threatening city authority.

Key California legislative concepts you must know:
- Gut-and-amend: A CA tactic where an entire bill's content is replaced with new,
  often unrelated content mid-session. Signal: author's amendments that fundamentally
  change the subject matter; language like "strike out all" or replacement of all
  operative sections; a bill whose title says "planning and zoning" that suddenly
  addresses a completely different topic post-amendment.
- Spot bills: Placeholder bills with boilerplate or empty language ("An act to amend
  Section X of the Government Code, relating to land use") waiting to be activated
  with substantive content later in session. Signal: very short or non-substantive
  summary; generic title; action history showing introduction with no committee hearing.

Your audience: sophisticated city officials and advocates who already understand the
threat of preemption. They need factual operational intelligence, not education.

Respond with ONLY valid JSON (no markdown fences, no commentary).\
"""

    user_prompt = f"""\
Analyze this California legislative data and return a JSON object.

{moving_ctx}

{amended_ctx}

{urgent_ctx}

{spot_ctx}
{anti_rep_ctx}

Return a JSON object with exactly these three keys:

"gut_and_amend": Array of objects. Include ONLY bills with clear gut-and-amend signals
  (fundamental content replacement, subject divergence post-amendment, or "strike out all"
  language). Empty array [] if none detected. Each object:
  {{
    "bill_number": "AB1234",
    "why": "One specific sentence describing the gut-and-amend signal."
  }}

"spot_bills": Array of objects. Include ONLY bills that appear to be placeholder/boilerplate
  awaiting activation. Empty array [] if none found. Each object:
  {{
    "bill_number": "SB567",
    "why": "One specific sentence explaining the spot bill signal."
  }}

"week_summary": A 2-3 sentence paragraph stating EXACTLY what the California Legislature
  did this week on housing preemption. Be operationally specific: name the bills, name
  the stages reached, name the vote counts where available. This is the factual spine
  of the newsletter — readers know why preemption is bad; they need to know what moved,
  what got amended, and what hearing is coming. Avoid vague language like "several bills
  moved forward." Start with the most significant thing that happened.
  MUST NOT repeat any of these framings from last week: {last_beats[:4]}

Return ONLY valid JSON. No markdown, no preamble.\
"""

    log.info("→ Calling Claude for legislative intelligence (gut-and-amend + week_summary)...")
    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown fences if Claude added them
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            raw = raw.rsplit("```", 1)[0].strip()

        result = json.loads(raw)
        log.info(
            f"   ✓ gut_and_amend: {len(result.get('gut_and_amend', []))} | "
            f"spot_bills: {len(result.get('spot_bills', []))} | "
            f"week_summary: {len(result.get('week_summary', ''))} chars"
        )
        return result

    except json.JSONDecodeError as exc:
        log.warning(f"Claude returned invalid JSON: {exc} — returning empty intelligence")
        return {"gut_and_amend": [], "spot_bills": [], "week_summary": ""}
    except Exception as exc:
        log.warning(f"Claude call failed: {exc} — returning empty intelligence")
        return {"gut_and_amend": [], "spot_bills": [], "week_summary": ""}


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run(
    client_id:        str  = DEFAULT_CLIENT,
    lookback:         int  = AMENDMENT_LOOKBACK,
    hearing_lookahead: int = HEARING_LOOKAHEAD,
    no_claude:        bool = False,
) -> Path:
    """
    Build and write the action digest. Returns the path to the written file.

    Args:
        client_id:        Client slug — determines which newsletter to read
                          for last_issue anti-repetition (default: "csf")
        lookback:         Days to look back for moving and amended buckets
        hearing_lookahead: Days to look ahead for urgent hearings
        no_claude:        If True, skip Claude call — pure-logic buckets only
    """
    today = date.today()

    log.info("=" * 58)
    log.info("CSF Legislative Intelligence — digest start")
    log.info(f"  Date:             {today.isoformat()}")
    log.info(f"  Client:           {client_id}")
    log.info(f"  Lookback:         {lookback} days")
    log.info(f"  Hearing lookahead: {hearing_lookahead} days")
    log.info(f"  Claude call:      {'disabled (--no-claude)' if no_claude else 'enabled'}")

    # --- Load bill data ---
    if not BILLS_FILE.exists():
        log.error(f"Bill data not found: {BILLS_FILE}")
        sys.exit(1)

    log.info(f"→ Loading {BILLS_FILE.name}...")
    bills_data = json.loads(BILLS_FILE.read_text(encoding="utf-8"))
    bills      = bills_data.get("bills", {})
    log.info(f"   {len(bills)} bills loaded")

    # --- Pure-logic buckets ---
    log.info("→ Building intelligence buckets...")

    urgent  = _find_urgent(bills, lookahead=hearing_lookahead)
    moving  = _find_moving(bills, lookback=lookback)
    amended = _find_amended(bills, lookback=lookback)
    stalled = _find_stalled(bills)

    log.info(f"   Urgent (hearings ≤ {hearing_lookahead} days):   {len(urgent)}")
    log.info(f"   Moving (stage advancement):    {len(moving)}")
    log.info(f"   Amended:                       {len(amended)}")
    log.info(f"   Stalled (≥ {STALL_THRESHOLD} days, 2+ criteria): {len(stalled)}")

    # --- Extract last issue for anti-repetition ---
    log.info("→ Extracting last issue for anti-repetition...")
    last_issue = _extract_last_issue(client_id)
    if last_issue:
        n_beats = len(last_issue.get("story_beats", []))
        log.info(f"   Found: {last_issue['source_file']} ({n_beats} story beats)")
    else:
        log.info("   No previous newsletter found — skipping anti-repetition")

    # --- Claude call (optional) ---
    claude_result = {"gut_and_amend": [], "spot_bills": [], "week_summary": ""}
    if not no_claude:
        claude_result = _call_claude(bills, moving, amended, urgent, last_issue)

    # --- Build digest ---
    week_str = today.strftime("%G-W%V")
    digest = {
        "generated":      datetime.now().isoformat(),
        "week":           week_str,
        "bills_analyzed": len(bills),
        "urgent":         urgent,
        "moving":         moving,
        "amended":        amended,
        "stalled":        stalled,
        "gut_and_amend":  claude_result.get("gut_and_amend", []),
        "spot_bills":     claude_result.get("spot_bills", []),
        "last_issue":     last_issue,
        "week_summary":   claude_result.get("week_summary", ""),
    }

    # --- Write digest ---
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    DIGEST_FILE.write_text(
        json.dumps(digest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info(f"→ Digest written → {DIGEST_FILE.relative_to(PROJECT_ROOT)}")

    # --- Terminal summary ---
    print(f"\n{'=' * 58}")
    print(f"  CSF Legislative Intelligence — {week_str}")
    print(f"{'=' * 58}")
    print(f"  Bills analyzed:          {len(bills)}")
    print(f"  Urgent (≤{hearing_lookahead} days):        {len(urgent)}")
    if urgent:
        print(f"    → soonest: {urgent[0]['bill_number']} on {urgent[0]['eligible_date']}")
    print(f"  Moving this lookback:    {len(moving)}")
    if moving:
        print(f"    → top: {moving[0]['bill_number']} ({moving[0]['current_stage']})")
    print(f"  Amended:                 {len(amended)}")
    print(f"  Stalled (≥{STALL_THRESHOLD} days):      {len(stalled)}")
    print(f"  Gut-and-amend detected:  {len(digest['gut_and_amend'])}")
    print(f"  Spot bills detected:     {len(digest['spot_bills'])}")
    print(f"  Last issue extracted:    {'yes → ' + last_issue.get('source_file', '') if last_issue else 'no'}")
    if digest["week_summary"]:
        print(f"  Week summary:            {digest['week_summary'][:75]}...")
    print(f"  Digest written to:       {DIGEST_FILE.relative_to(PROJECT_ROOT)}")
    print(f"{'=' * 58}\n")

    return DIGEST_FILE


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Generate the weekly legislative intelligence digest. "
            "Reads tracked_bills.json, produces data/legislative/action_digest.json."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python agents/legislative/legislative_intel.py
  python agents/legislative/legislative_intel.py --client cma
  python agents/legislative/legislative_intel.py --no-claude       # pure logic only
  python agents/legislative/legislative_intel.py --lookahead 7     # hearings ≤7 days out
  python agents/legislative/legislative_intel.py --lookback 7      # 7-day window for moved/amended
        """,
    )
    parser.add_argument(
        "--client", type=str, default=DEFAULT_CLIENT,
        help=f"Client slug for last_issue extraction (default: {DEFAULT_CLIENT})",
    )
    parser.add_argument(
        "--lookback", type=int, default=AMENDMENT_LOOKBACK,
        help=f"Days to look back for moving/amended bills (default: {AMENDMENT_LOOKBACK})",
    )
    parser.add_argument(
        "--lookahead", type=int, default=HEARING_LOOKAHEAD,
        help=f"Days to look ahead for urgent hearings (default: {HEARING_LOOKAHEAD})",
    )
    parser.add_argument(
        "--no-claude", action="store_true", default=False,
        help="Skip Claude API call — write pure-logic buckets only (no API cost)",
    )

    args = parser.parse_args()
    run(
        client_id=args.client,
        lookback=args.lookback,
        hearing_lookahead=args.lookahead,
        no_claude=args.no_claude,
    )


if __name__ == "__main__":
    main()
