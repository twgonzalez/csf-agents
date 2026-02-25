#!/usr/bin/env python3
"""
social_writer.py — Weekly Social Media Content Generator
California Stewardship Fund

Reads tracked_bills.json (output of bill_tracker + housing_analyzer), calls Claude
to generate 3 social media posts per week with platform variants for X, Facebook,
and Instagram, plus an image brief for each post.

Pipeline position:
    bill_tracker.py → tracked_bills.json → housing_analyzer.py → social_writer.py

Usage:
    .venv/bin/python agents/social/social_writer.py            # default (dry-run)
    .venv/bin/python agents/social/social_writer.py --bills path/to/bills.json
    .venv/bin/python agents/social/social_writer.py --lookback 7

Output:
    outputs/social/social_YYYY-WNN.md

Requires:
    ANTHROPIC_API_KEY environment variable (or .env file at project root)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Load .env before importing anthropic so the key is available
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env", override=True)

import anthropic

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
OUTPUT_DIR   = PROJECT_ROOT / "outputs" / "social"

# ---------------------------------------------------------------------------
# Bill selection (mirrors newsletter_writer.py logic)
# ---------------------------------------------------------------------------

_CRIT_KEYS = {
    "A": "pro_housing_production",
    "B": "densification",
    "C": "reduce_discretion",
    "D": "cost_to_cities",
}


def _select_bills(
    bills: dict,
    lookback_days: int = 14,
    hearing_lookahead: int = 7,
    max_watch: int = 3,
    max_new: int = 3,
) -> dict:
    """Return bill sets for social content generation.

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
# Claude content generation
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are the social media voice of the California Stewardship Fund — a policy organization \
whose core belief is that the best decisions come from people closest to them.

Your job is to write 3 social media posts per week based on the latest California housing \
bill intelligence. These posts reach city council members, neighborhood advocates, major \
donors, and engaged citizens who care about protecting local government authority from \
state preemption.

VOICE: Sharp, direct, credible. Never alarmist or hyperbolic. Think: a smart local \
elected official explaining a real threat to their constituents — confident, factual, \
and clear about what's at stake.

FRAMING: The central message is always: "This isn't about stopping housing — it's about \
who decides." Sacramento bills preempt local zoning authority, remove discretionary review, \
mandate development patterns, and shift infrastructure costs to cities.

USE THESE TERMS: "preempts local authority", "removes discretionary review", "state mandate", \
"local control", "who decides", "your city council", "infrastructure cost-shifting"
AVOID: "controversial", "opponents say", "some argue", false balance, hyperbole

PLATFORM RULES:
- X: Hard 280 character limit — count every character including spaces and punctuation. \
  Lead with the hook. Bill number early. One specific CTA. 1-2 hashtags only. \
  No URLs (they count as 23 chars each if included — leave them out for copy-paste).
- Facebook: 150-250 words. Build the argument with context. Include the bill number and \
  what it specifically does. End with a call to action. No hashtags needed.
- Instagram: 2-3 short punchy paragraphs (not bullet points). End with 8-12 relevant \
  hashtags on their own line, preceded by a blank line. Write "🔗 Link in bio" as the CTA — \
  never paste a URL in Instagram copy.

IMAGE BRIEF RULES:
- Keep it achievable: text card graphics that any staff member can build in Canva in 5 min
- Headline: 6-10 words max — the single most alarming true fact about this bill/topic
- Subtext: 8-12 words — the specific mechanism or risk
- Colors follow CSF brand: deep navy #1a3a5c background, white text, gold #c9a227 accent
- Suggest the bill number as a large typographic element for bill-specific posts
- Always specify both square and landscape sizes\
"""


def _build_bill_context(bill: dict) -> str:
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


def _format_hearing(h: dict) -> str:
    bill = h.get("_bill", {})
    return (
        f"  {bill.get('bill_number', '?')} — {bill.get('title', '')} | "
        f"Date: {h.get('date', '?')} | Committee: {h.get('committee', '?')} | "
        f"Location: {h.get('location', 'TBD')}"
    )


def _generate_content(bill_set: dict, client: anthropic.Anthropic) -> dict:
    """Single Claude call returning all 3 posts as a structured dict."""
    watch_ctx   = "\n\n".join(_build_bill_context(b) for b in bill_set["watch_list"])
    new_ctx     = "\n\n".join(_build_bill_context(b) for b in bill_set["new_bills"])
    hearing_ctx = "\n".join(_format_hearing(h) for h in bill_set["upcoming_hearings"])

    user_prompt = f"""\
Here is this week's bill intelligence. Write exactly 3 social media posts as specified.

== HIGH-RISK WATCH LIST (top bills, 2+ criteria strong/moderate) ==
{watch_ctx if watch_ctx else "(No high-risk bills with complete analysis this week)"}

== NEW BILLS THIS WEEK (recently tracked, at least 1 risk signal) ==
{new_ctx if new_ctx else "(No newly tracked bills this week)"}

== UPCOMING HEARINGS (next 7 days) ==
{hearing_ctx if hearing_ctx else "(No hearings scheduled this week)"}

---

Produce exactly 3 posts with this assignment:

POST 1 — Bill Spotlight
  Feature the single most important bill from the watch list.
  All 3 platform variants focus on this one bill: what it does + the specific local control threat.
  Be concrete — name the mechanism (preempts zoning, removes CEQA review, mandates by-right, etc.)

POST 2 — Action Alert
  If there is an upcoming hearing: focus on that hearing — date, committee, what's at stake,
  and a specific action (show up / submit public comment by [date]).
  If no hearings: pick the #2 watch-list bill and write a "contact your rep" alert —
  bill number, what it does, and a direct ask.

POST 3 — Mission Frame
  Zoom out. Connect this week's legislative activity to the bigger "who decides" question.
  Do not spotlight a single bill — synthesize the pattern across 2-3 bills if possible.
  Write for the donor and advocate audience: WHY does this matter strategically, not just what.
  Close with resolve, not alarm.

---

Return a JSON object with exactly this structure:

{{
  "week_theme": "One sentence (12-18 words): the single most important development this week",
  "posts": [
    {{
      "post_number": 1,
      "post_type": "bill_spotlight",
      "bill_number": "AB1234",
      "x": "<X post — HARD 280 char limit, count every character>",
      "x_char_count": 0,
      "facebook": "<Facebook post, 150-250 words, no hashtags>",
      "instagram": "<Instagram caption: 2-3 short paragraphs, then blank line, then hashtags>",
      "hashtags": ["LocalControl", "CaliforniaHousing", "AB1234", "LocalGovernment"],
      "image_brief": {{
        "headline": "<6-10 words — the most alarming true fact>",
        "subtext": "<8-12 words — the specific mechanism or risk>",
        "background_color": "#1a3a5c",
        "text_color": "#ffffff",
        "accent_color": "#c9a227",
        "typographic_element": "<e.g. 'Bill number AB1234 as oversized display type, upper-left'>",
        "optional_graphic": "<e.g. 'California Capitol silhouette, faint, bottom-right' or 'none'>",
        "sizes": ["1080x1080 (Instagram/Facebook square)", "1600x900 (X/Facebook landscape)"]
      }}
    }},
    {{
      "post_number": 2,
      "post_type": "action_alert",
      "bill_number": "<bill number, or null if synthesized from multiple>",
      "x": "<X post ≤280 chars>",
      "x_char_count": 0,
      "facebook": "<Facebook post>",
      "instagram": "<Instagram caption + hashtags>",
      "hashtags": ["..."],
      "image_brief": {{
        "headline": "...",
        "subtext": "...",
        "background_color": "#1a3a5c",
        "text_color": "#ffffff",
        "accent_color": "#c9a227",
        "typographic_element": "...",
        "optional_graphic": "...",
        "sizes": ["1080x1080 (Instagram/Facebook square)", "1600x900 (X/Facebook landscape)"]
      }}
    }},
    {{
      "post_number": 3,
      "post_type": "mission_frame",
      "bill_number": null,
      "x": "<X post ≤280 chars>",
      "x_char_count": 0,
      "facebook": "<Facebook post>",
      "instagram": "<Instagram caption + hashtags>",
      "hashtags": ["..."],
      "image_brief": {{
        "headline": "...",
        "subtext": "...",
        "background_color": "#1a3a5c",
        "text_color": "#ffffff",
        "accent_color": "#c9a227",
        "typographic_element": "...",
        "optional_graphic": "...",
        "sizes": ["1080x1080 (Instagram/Facebook square)", "1600x900 (X/Facebook landscape)"]
      }}
    }}
  ]
}}

Return ONLY valid JSON. No markdown fences. No commentary outside the JSON object.
Count X characters carefully — every space and punctuation mark counts toward the 280 limit.
"""

    log.info("→ Calling Claude to generate social media content...")
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = message.content[0].text.strip()
    # Strip markdown fences if Claude wraps the response
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]

    return json.loads(raw)


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_POST_TYPE_LABELS = {
    "bill_spotlight": "Bill Spotlight",
    "action_alert":   "Action Alert",
    "mission_frame":  "Mission Frame",
}

_POST_TYPE_ICONS = {
    "bill_spotlight": "&#9888;",   # warning triangle
    "action_alert":   "&#128226;", # megaphone
    "mission_frame":  "&#127919;", # target
}

# ---------------------------------------------------------------------------
# Markdown output
# ---------------------------------------------------------------------------


def _render_markdown(content: dict, bill_set: dict) -> str:
    """Render the full content package as a copy-paste-ready markdown file.

    Structured so staff can work top-to-bottom: read the post, check char counts,
    copy each platform variant directly into Buffer/Hootsuite/native composer,
    and hand the image brief to a designer or open Canva.
    """
    today = date.today()
    lines = [
        f"# CSF Social Media — Week of {today.strftime('%B %-d, %Y')}",
        "",
        f"*3 posts · Meta platforms + X · Generated {today.isoformat()}*",
        "",
        f"> **This week:** {content.get('week_theme', '')}",
        "",
        "---",
        "",
    ]

    for post in content.get("posts", []):
        num      = post.get("post_number", "?")
        ptype    = post.get("post_type", "")
        label    = _POST_TYPE_LABELS.get(ptype, ptype.replace("_", " ").title())
        bill_num = post.get("bill_number")

        title_line = f"## Post {num} — {label}"
        if bill_num:
            title_line += f": {bill_num}"

        x_text      = post.get("x", "")
        x_chars     = len(x_text)
        # Recount chars (Claude's count may differ)
        actual_chars = len(x_text)
        if actual_chars <= 280:
            x_status = f"✓ {actual_chars}/280"
        else:
            x_status = f"⚠ OVER LIMIT — {actual_chars}/280 ({actual_chars - 280} chars over, trim before posting)"

        hashtags = " ".join(f"#{h.lstrip('#')}" for h in post.get("hashtags", []))
        ib       = post.get("image_brief", {})

        lines += [
            title_line,
            "",
            "### X",
            f"*{x_status}*",
            "",
            x_text,
            "",
            "---",
            "",
            "### Facebook",
            "",
            post.get("facebook", ""),
            "",
            "---",
            "",
            "### Instagram",
            "",
            post.get("instagram", ""),
            "",
            "---",
            "",
            "### Hashtags (all platforms)",
            "",
            hashtags,
            "",
            "---",
            "",
            "### Image Brief",
            "",
            "| Field | Spec |",
            "|-------|------|",
            f"| **Headline** | {ib.get('headline', '')} |",
            f"| **Subtext** | {ib.get('subtext', '')} |",
            f"| **Background** | `{ib.get('background_color', '#1a3a5c')}` — deep navy |",
            f"| **Text** | `{ib.get('text_color', '#ffffff')}` — white |",
            f"| **Accent** | `{ib.get('accent_color', '#c9a227')}` — gold |",
            f"| **Typographic element** | {ib.get('typographic_element', 'None')} |",
            f"| **Optional graphic** | {ib.get('optional_graphic', 'None')} |",
        ]

        for sz in ib.get("sizes", []):
            lines.append(f"| **Size** | {sz} |")

        lines += ["", "---", ""]

    # Source data footer
    watch_bills = ", ".join(b["bill_number"] for b in bill_set["watch_list"])
    lines += [
        "---",
        "",
        "## Source Data",
        "",
        f"- **Watch list bills:** {watch_bills or 'None (run housing_analyzer.py first)'}",
        f"- **Upcoming hearings:** {len(bill_set['upcoming_hearings'])}",
        f"- **New bills this week:** {len(bill_set['new_bills'])}",
        f"- **Generated:** {today.isoformat()}",
        "",
        "*Generated by `agents/social/social_writer.py` — California Stewardship Fund*",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML output (shareable preview)
# ---------------------------------------------------------------------------

_SANS  = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;"
_SERIF = "font-family:Georgia,'Times New Roman',Times,serif;"
_NAVY  = "#1a3a5c"
_GOLD  = "#c9a227"
_SAND  = "#f5f2ed"
_INK   = "#1c1c1e"
_MID   = "#666666"
_RULE  = "#ddd8ce"
_WHITE = "#ffffff"

_PLATFORM_COLORS = {
    "x":        ("#000000", "#ffffff"),   # bg, text
    "facebook": ("#1877f2", "#ffffff"),
    "instagram": ("#833ab4", "#ffffff"),
}

_PLATFORM_LABELS = {
    "x":         "X (Twitter)",
    "facebook":  "Facebook",
    "instagram": "Instagram",
}


def _esc(text: str) -> str:
    """Minimal HTML escaping for user-generated content."""
    return (text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br>"))


def _platform_badge(platform: str) -> str:
    bg, fg = _PLATFORM_COLORS.get(platform, ("#555", "#fff"))
    label  = _PLATFORM_LABELS.get(platform, platform.title())
    return (
        f'<span style="{_SANS}background:{bg};color:{fg};font-size:11px;'
        f'font-weight:700;padding:3px 10px;border-radius:20px;'
        f'letter-spacing:0.5px;display:inline-block;margin-bottom:10px;">'
        f'{label}</span>'
    )


def _char_count_badge(text: str) -> str:
    n = len(text)
    ok = n <= 280
    bg = "#e8f5e9" if ok else "#ffebee"
    fg = "#2e7d32" if ok else "#c62828"
    label = f"{n}/280 ✓" if ok else f"{n}/280 — {n-280} over limit"
    return (
        f'<span style="{_SANS}background:{bg};color:{fg};font-size:11px;'
        f'font-weight:600;padding:2px 8px;border-radius:10px;margin-left:8px;">'
        f'{label}</span>'
    )


def _render_post_card(post: dict, index: int) -> str:
    num      = post.get("post_number", index + 1)
    ptype    = post.get("post_type", "")
    label    = _POST_TYPE_LABELS.get(ptype, ptype.replace("_", " ").title())
    icon     = _POST_TYPE_ICONS.get(ptype, "")
    bill_num = post.get("bill_number")
    ib       = post.get("image_brief", {})
    hashtags = " ".join(f"#{h.lstrip('#')}" for h in post.get("hashtags", []))

    bill_badge = ""
    if bill_num:
        bill_badge = (
            f'<span style="{_SANS}background:{_GOLD};color:{_NAVY};font-size:11px;'
            f'font-weight:700;padding:3px 10px;border-radius:20px;margin-left:8px;">'
            f'{bill_num}</span>'
        )

    # Build platform blocks
    platform_blocks = ""
    for platform in ("x", "facebook", "instagram"):
        text = post.get(platform, "")
        if not text:
            continue
        char_info = _char_count_badge(text) if platform == "x" else ""
        platform_blocks += f"""
        <div style="margin-bottom:20px;">
          <div style="margin-bottom:6px;">
            {_platform_badge(platform)}{char_info}
          </div>
          <div style="{_SANS}background:#f8f8f8;border:1px solid {_RULE};border-radius:8px;
                      padding:16px 20px;font-size:14px;line-height:1.7;color:{_INK};
                      white-space:pre-wrap;">{_esc(text)}</div>
        </div>"""

    # Hashtag block
    hashtag_block = ""
    if hashtags:
        hashtag_block = f"""
        <div style="margin-top:4px;margin-bottom:20px;">
          <div style="{_SANS}font-size:11px;font-weight:700;color:{_MID};
                      text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;">
            Hashtags (all platforms)
          </div>
          <div style="{_SANS}font-size:13px;color:{_NAVY};line-height:1.9;">
            {hashtags}
          </div>
        </div>"""

    # Image brief block
    brief_rows = ""
    for field, key in [
        ("Headline",           "headline"),
        ("Subtext",            "subtext"),
        ("Typographic element","typographic_element"),
        ("Optional graphic",   "optional_graphic"),
    ]:
        val = ib.get(key, "")
        if val and val.lower() != "none":
            brief_rows += (
                f'<tr>'
                f'<td style="{_SANS}font-size:12px;font-weight:700;color:{_MID};'
                f'padding:6px 12px 6px 0;white-space:nowrap;vertical-align:top;">{field}</td>'
                f'<td style="{_SANS}font-size:13px;color:{_INK};padding:6px 0;">{_esc(val)}</td>'
                f'</tr>'
            )

    # Color swatches
    color_row = ""
    for label_c, key_c in [("Background", "background_color"), ("Text", "text_color"), ("Accent", "accent_color")]:
        hex_val = ib.get(key_c, "")
        if hex_val:
            color_row += (
                f'<span style="display:inline-block;margin-right:12px;">'
                f'<span style="display:inline-block;width:16px;height:16px;'
                f'background:{hex_val};border:1px solid {_RULE};border-radius:3px;'
                f'vertical-align:middle;margin-right:4px;"></span>'
                f'<span style="{_SANS}font-size:12px;color:{_MID};vertical-align:middle;">'
                f'{label_c} {hex_val}</span></span>'
            )

    sizes = " &nbsp;·&nbsp; ".join(ib.get("sizes", []))
    image_brief_block = f"""
        <div style="background:#fafaf7;border:1px solid {_RULE};border-left:3px solid {_GOLD};
                    border-radius:8px;padding:16px 20px;margin-top:4px;">
          <div style="{_SANS}font-size:11px;font-weight:700;color:{_GOLD};
                      text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;">
            Image Brief
          </div>
          <table cellpadding="0" cellspacing="0" style="width:100%;margin-bottom:12px;">
            {brief_rows}
          </table>
          <div style="margin-bottom:8px;">{color_row}</div>
          {f'<div style="{_SANS}font-size:12px;color:{_MID};">Sizes: {sizes}</div>' if sizes else ""}
        </div>"""

    return f"""
  <!-- Post {num} -->
  <div style="background:{_WHITE};border:1px solid {_RULE};border-radius:12px;
              margin-bottom:32px;overflow:hidden;">

    <!-- Post header -->
    <div style="background:{_NAVY};padding:20px 28px;">
      <div style="{_SANS}font-size:13px;font-weight:700;color:{_GOLD};
                  text-transform:uppercase;letter-spacing:1.5px;margin-bottom:6px;">
        Post {num}
      </div>
      <div style="{_SANS}font-size:22px;font-weight:700;color:{_WHITE};line-height:1.2;">
        {icon} {label}{bill_badge}
      </div>
    </div>

    <!-- Post body -->
    <div style="padding:24px 28px;">
      {platform_blocks}
      {hashtag_block}
      {image_brief_block}
    </div>
  </div>"""


def _render_html(content: dict, bill_set: dict) -> str:
    """Render the full content package as a shareable inline-styled HTML page."""
    today     = date.today()
    week_str  = today.strftime("Week of %B %-d, %Y")
    theme     = _esc(content.get("week_theme", ""))
    watch_str = ", ".join(b["bill_number"] for b in bill_set["watch_list"])

    post_cards = "".join(
        _render_post_card(post, i)
        for i, post in enumerate(content.get("posts", []))
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>CSF Social Media — {week_str}</title>
</head>
<body style="margin:0;padding:0;background:{_SAND};">

<!-- Outer wrapper -->
<table width="100%" cellpadding="0" cellspacing="0" style="background:{_SAND};padding:40px 16px;">
<tr><td align="center">

<!-- Container -->
<table width="860" cellpadding="0" cellspacing="0"
       style="max-width:860px;width:100%;">

  <!-- MASTHEAD -->
  <tr>
    <td style="padding-bottom:28px;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td>
            <div style="{_SANS}color:{_NAVY};font-size:13px;font-weight:700;
                        text-transform:uppercase;letter-spacing:2px;margin-bottom:6px;
                        opacity:0.7;">California Stewardship Fund</div>
            <div style="{_SERIF}color:{_NAVY};font-size:28px;font-weight:700;
                        letter-spacing:-0.3px;line-height:1.1;">
              Social Media — {week_str}
            </div>
          </td>
          <td align="right" valign="bottom">
            <div style="{_SANS}color:{_MID};font-size:12px;">
              Generated {today.isoformat()}
            </div>
          </td>
        </tr>
      </table>
      <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:14px;">
        <tr><td style="border-top:2px solid {_NAVY};font-size:0;">&nbsp;</td></tr>
      </table>
    </td>
  </tr>

  <!-- WEEK THEME -->
  <tr>
    <td style="padding-bottom:32px;">
      <div style="background:{_NAVY};border-radius:10px;padding:20px 28px;">
        <div style="{_SANS}font-size:11px;font-weight:700;color:{_GOLD};
                    text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px;">
          This Week
        </div>
        <div style="{_SERIF}font-size:17px;color:{_WHITE};line-height:1.6;
                    font-style:italic;">
          {theme}
        </div>
        <div style="{_SANS}font-size:12px;color:rgba(255,255,255,0.5);margin-top:12px;">
          Watch list bills: {watch_str} &nbsp;·&nbsp;
          Upcoming hearings: {len(bill_set['upcoming_hearings'])} &nbsp;·&nbsp;
          New bills: {len(bill_set['new_bills'])}
        </div>
      </div>
    </td>
  </tr>

  <!-- POSTS -->
  <tr>
    <td>
      {post_cards}
    </td>
  </tr>

  <!-- FOOTER -->
  <tr>
    <td style="padding:16px 0 8px;text-align:center;">
      <div style="{_SANS}color:#aaa;font-size:11px;line-height:1.8;">
        Generated by <code>agents/social/social_writer.py</code>
        &nbsp;·&nbsp; California Stewardship Fund
        &nbsp;·&nbsp; Content only — no posts have been published
      </div>
    </td>
  </tr>

</table>
</td></tr>
</table>

</body>
</html>"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate weekly social media content for CSF.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Generate markdown output (default)
  python agents/social/social_writer.py

  # Override lookback window for "new bills" detection
  python agents/social/social_writer.py --lookback 7

  # Use a different bill data source
  python agents/social/social_writer.py --bills data/bills/tracked_bills.json
        """,
    )
    p.add_argument(
        "--bills", type=Path, default=None,
        help="Path to tracked_bills.json (default: data/bills/tracked_bills.json)",
    )
    p.add_argument(
        "--lookback", type=int, default=14,
        help="Days to look back when identifying new bills (default: 14)",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set. Add it to .env or your environment.")
        sys.exit(1)

    print("\n  CSF Social Media Writer")
    print("  " + "─" * 30)

    # ── Load bill data ──────────────────────────────────────────────────────
    bills_path = args.bills or BILLS_FILE
    log.info(f"→ Loading {bills_path.name}...")
    data  = json.loads(bills_path.read_text())
    bills = data["bills"]
    log.info(f"   {len(bills)} bills loaded")

    # ── Select bills ─────────────────────────────────────────────────────────
    log.info("→ Selecting bills for this week's posts...")
    bill_set = _select_bills(bills, lookback_days=args.lookback)
    log.info(f"   Watch list:        {len(bill_set['watch_list'])} bills")
    log.info(f"   New this week:     {len(bill_set['new_bills'])} bills")
    log.info(f"   Upcoming hearings: {len(bill_set['upcoming_hearings'])}")

    if not bill_set["watch_list"] and not bill_set["new_bills"]:
        log.warning("No analyzed bills with risk signals found.")
        log.warning("Run housing_analyzer.py first to populate analysis data.")
        sys.exit(0)

    # ── Generate content via Claude ─────────────────────────────────────────
    client  = anthropic.Anthropic(api_key=api_key)
    content = _generate_content(bill_set, client)
    posts   = content.get("posts", [])
    log.info(f"   ✓ {len(posts)} posts generated")

    # ── Print summary ───────────────────────────────────────────────────────
    print(f"\n  Week theme: {content.get('week_theme', '')}\n")
    for post in posts:
        num      = post.get("post_number", "?")
        label    = _POST_TYPE_LABELS.get(post.get("post_type", ""), "Post")
        bill     = post.get("bill_number", "n/a")
        x_chars  = len(post.get("x", ""))
        over_msg = f" ⚠ OVER by {x_chars - 280}" if x_chars > 280 else ""
        print(f"  Post {num} ({label:14s})  X: {x_chars}/280{over_msg}  bill: {bill}")

    # ── Write output ─────────────────────────────────────────────────────────
    log.info("→ Rendering outputs...")
    markdown = _render_markdown(content, bill_set)
    html     = _render_html(content, bill_set)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    iso_week  = date.today().strftime("%Y-W%W")
    md_path   = OUTPUT_DIR / f"social_{iso_week}.md"
    html_path = OUTPUT_DIR / f"social_{iso_week}.html"
    md_path.write_text(markdown,  encoding="utf-8")
    html_path.write_text(html,    encoding="utf-8")

    print(f"\n  ✓ Markdown: {md_path.relative_to(PROJECT_ROOT)}")
    print(f"  ✓ HTML:     {html_path.relative_to(PROJECT_ROOT)}")
    print(f"\n  Share preview: file://{html_path}")
    print(f"  Copy-paste:    file://{md_path}\n")


if __name__ == "__main__":
    main()
