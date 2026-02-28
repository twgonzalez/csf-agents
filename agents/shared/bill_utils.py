"""
bill_utils.py — Shared bill selection and context formatting utilities.

Provides _CRIT_KEYS, _select_bills, and _build_bill_context.

Shared by all agents that process tracked_bills.json.
Default caps (max_watch=3, max_new=3) suit the social writer; the newsletter
writer passes explicit values (max_watch=5, max_new=4) at its call site.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta


# ---------------------------------------------------------------------------
# Risk analysis criteria keys
# ---------------------------------------------------------------------------

_CRIT_KEYS = {
    "A": "pro_housing_production",
    "B": "densification",
    "C": "reduce_discretion",
    "D": "cost_to_cities",
}


# ---------------------------------------------------------------------------
# Bill selection
# ---------------------------------------------------------------------------

def _select_bills(
    bills: dict,
    lookback_days: int = 14,
    hearing_lookahead: int = 7,
    max_watch: int = 3,
    max_new: int = 3,
) -> dict:
    """Return bill sets for content generation.

    watch_list        — top high-risk bills (2+ criteria strong/moderate), ranked
    new_bills         — recently tracked bills with at least 1 risk signal
    upcoming_hearings — bills with hearings in the next hearing_lookahead days
    """
    today    = date.today()
    cutoff   = today - timedelta(days=lookback_days)
    hear_end = today + timedelta(days=hearing_lookahead)

    watch_list:        list[tuple] = []
    new_bills:         list[dict]  = []
    upcoming_hearings: list[dict]  = []

    for bill in bills.values():
        analysis     = bill.get("analysis", {})
        risk_scores  = {k: analysis.get(v, "none") for k, v in _CRIT_KEYS.items()}
        risk_count   = sum(1 for s in risk_scores.values() if s in ("strong", "moderate"))
        strong_count = sum(1 for s in risk_scores.values() if s == "strong")

        if risk_count >= 2:
            watch_list.append((bill, risk_count, strong_count))

        if bill.get("first_seen") and risk_count >= 1:
            try:
                first_seen = datetime.fromisoformat(bill["first_seen"]).date()
                if first_seen >= cutoff:
                    new_bills.append(bill)
            except ValueError:
                pass

        for h in bill.get("upcoming_hearings", []):
            try:
                hdate = date.fromisoformat(h["date"])
                if today <= hdate <= hear_end:
                    upcoming_hearings.append({**h, "_bill": bill})
            except (KeyError, ValueError):
                pass

    def _has_hearing(item: tuple) -> bool:
        b, _, _ = item
        return any(
            today <= date.fromisoformat(h["date"]) <= hear_end
            for h in b.get("upcoming_hearings", [])
            if h.get("date")
        )

    watch_list.sort(key=lambda x: (-int(_has_hearing(x)), -x[1], -x[2]))

    return {
        "watch_list":        [b for b, _, _ in watch_list[:max_watch]],
        "new_bills":         new_bills[:max_new],
        "upcoming_hearings": sorted(upcoming_hearings, key=lambda h: h["date"]),
    }


# ---------------------------------------------------------------------------
# Bill context formatting (for Claude prompts)
# ---------------------------------------------------------------------------

def _build_bill_context(bill: dict) -> str:
    """Format a bill dict into a Claude-readable context block."""
    a = bill.get("analysis", {})
    lines = [
        f"BILL: {bill['bill_number']} — {bill['title']}",
        f"Author: {bill.get('author', 'Unknown')}",
        f"Status: {bill.get('status', '')} ({bill.get('status_date', '')})",
    ]
    if bill.get("summary"):
        lines.append(f"Summary: {bill['summary'][:400]}")
    if a:
        scores = ", ".join(f"{k}={a.get(v, 'none')}" for k, v in _CRIT_KEYS.items())
        lines.append(f"Risk scores: {scores}")
        if a.get("notes"):
            lines.append(f"Analysis notes: {a['notes']}")
        if a.get("comms_brief"):
            lines.append(f"Comms brief: {a['comms_brief']}")
    return "\n".join(lines)
