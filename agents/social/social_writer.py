#!/usr/bin/env python3
"""
social_writer.py — Weekly Social Media Content Generator
California Stewardship Fund

Reads tracked_bills.json (output of bill_tracker + housing_analyzer) and
optionally data/media/media_digest.json (output of media_scanner), then calls
Claude to generate 3 social media posts per week with platform variants for X,
Facebook, and Instagram, plus an image brief for each post.

Pipeline position:
    bill_tracker.py → tracked_bills.json → housing_analyzer.py ──┐
    media_scanner.py → data/media/media_digest.json ─────────────┤
                                                                  ↓
                                                         social_writer.py

Client system:
    Each run targets a client from clients/<id>/client.yml.
    The client file defines brand identity (org, audience, colors, image style).
    Voice files live in clients/<id>/voices/<name>.md and control tone and framing.
    Staff can add a new client by creating a directory — no code changes required.

    Default client: clients/csf/  (California Stewardship Fund)
    Add a client:   create clients/<slug>/client.yml + clients/<slug>/voices/default.md
    Select client:  --client <slug>
    List clients:   --list-clients

Voice system:
    Each run uses a voice file from clients/<id>/voices/<name>.md.
    CSF voices: default (general advocacy), coalition (broad tent), urgent (time-critical).
    List voices: --list-voices  (scoped to selected client)

Usage:
    .venv/bin/python agents/social/social_writer.py                        # csf + default voice
    .venv/bin/python agents/social/social_writer.py --client cma           # cma + default voice
    .venv/bin/python agents/social/social_writer.py --voice urgent         # csf + urgent voice
    .venv/bin/python agents/social/social_writer.py --list-clients         # show all clients
    .venv/bin/python agents/social/social_writer.py --list-voices          # show voices for client
    .venv/bin/python agents/social/social_writer.py --bills path/to/bills.json
    .venv/bin/python agents/social/social_writer.py --lookback 7
    .venv/bin/python agents/social/social_writer.py --no-media             # skip media digest

Output:
    outputs/clients/<slug>/social/social_YYYY-WNN.md           (default voice)
    outputs/clients/<slug>/social/social_YYYY-WNN_<voice>.md   (non-default voice)
    outputs/clients/<slug>/social/social_YYYY-WNN.html         (default voice)
    outputs/clients/<slug>/social/social_YYYY-WNN_<voice>.html (non-default voice)

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

# Ensure project root is on sys.path so intra-package imports work when the
# script is run directly (python agents/social/social_writer.py).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env", override=True)

import anthropic
import yaml

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="  %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT   = _PROJECT_ROOT
BILLS_FILE     = PROJECT_ROOT / "data" / "bills" / "tracked_bills.json"
MEDIA_DIGEST   = PROJECT_ROOT / "data" / "media" / "media_digest.json"
CLIENTS_DIR    = PROJECT_ROOT / "clients"

DEFAULT_CLIENT = "csf"
DEFAULT_VOICE  = "default"

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
# Media digest loader
# ---------------------------------------------------------------------------

def _load_media_digest(path: Path | None = None) -> dict | None:
    """Load media_digest.json if it exists. Returns None if absent or unreadable."""
    digest_path = path or MEDIA_DIGEST
    if not digest_path.exists():
        return None
    try:
        return json.loads(digest_path.read_text())
    except Exception as exc:
        log.warning(f"Could not load media digest: {exc}")
        return None


def _format_media_context(digest: dict | None) -> str:
    """Format media digest articles into a Claude-readable context block."""
    if not digest:
        return ""

    articles = digest.get("articles", [])[:8]
    x_posts  = digest.get("x_posts",  [])[:5]
    summary  = digest.get("summary",  {})

    if not articles and not x_posts:
        return ""

    lines = [
        "== NEWS & MEDIA CONTEXT (past 7 days — from media_scanner.py) ==",
        "Use this to make posts timely and reactive to current news. Hook into",
        "the most relevant story where it strengthens the local control framing.",
        "",
    ]

    if articles:
        lines.append("Recent news coverage:")
        for a in articles:
            score         = a.get("relevance_score", 0)
            source        = a.get("source", "")
            title         = a.get("title", "")
            pub           = a.get("published", "")
            bills         = a.get("bill_mentions", [])
            article_blurb = a.get("summary", "")[:200]

            bill_str = f"  [bills: {', '.join(bills)}]" if bills else ""
            lines.append(f"  [{score:.1f}] {source} | {pub} | {title}{bill_str}")
            if article_blurb:
                lines.append(f"       Summary: {article_blurb}")

    if x_posts:
        lines.append("")
        lines.append("Recent X/social posts:")
        for p in x_posts:
            author   = p.get("author", "")
            text     = p.get("text", "")[:200]
            bills    = p.get("bill_mentions", [])
            bill_str = f"  [bills: {', '.join(bills)}]" if bills else ""
            lines.append(f"  @{author}: {text}{bill_str}")

    top_bills = summary.get("top_bill_mentions", [])
    if top_bills:
        lines.append("")
        lines.append(f"Bills getting the most media attention: {', '.join(top_bills[:5])}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Client system
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
# Voice system
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


# ---------------------------------------------------------------------------
# Claude content generation
# ---------------------------------------------------------------------------

# Structural rules — platform mechanics and output format. Never changes.
# Client identity/audience/colors are injected by _build_base_prompt(client).
# Voice/tone/framing is loaded from clients/<id>/voices/<name>.md at runtime.
_PLATFORM_AND_IMAGE_RULES = """\
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
- Keep it achievable: text card graphics that any staff member can build in 5 min
- Headline: 6-10 words max — a direct, declarative statement of the single most alarming fact.
  Must be immediately clear at a glance. No wordplay, puns, em-dash tricks, or double meanings.
  Write as a plain active-voice sentence or a clear noun phrase. If a reader needs to stop and
  parse it, rewrite it. Bad: "Your City Can't Say No — Or Send the Bill". Good: "AB1751 Bans
  Cities from Charging Impact Fees" or "Sacramento Eliminates Local Zoning Authority".
- Subtext: 8-12 words — the specific mechanism or risk, factual and unambiguous
- Suggest the bill number as a large typographic element for bill-specific posts
- Always specify both square and landscape sizes

AI IMAGE PROMPT RULES (the "ai_image_prompt" field):
- This is a fallback prompt for manual image generation (DALL-E 3, Midjourney, Flux).
- When --images is used, Nano Banana Pro generates the complete graphic from the image brief instead.
- NEVER include text, words, letters, numbers, or typography in the prompt.
- Describe the graphic element from "optional_graphic" in visual, generator-friendly language.
- Translate brand colors into natural language: "deep navy blue", "warm gold accent", not hex codes.
- Include style direction: "minimal flat design", "bold graphic", "clean policy/advocacy aesthetic".
- Always end with: "No text. No typography. No people. No logos."
- Keep it 2-4 sentences. Ready to paste directly into an image generator.\
"""


def _build_base_prompt(client: dict) -> str:
    """Build the client-specific opening of the system prompt.

    Substitutes org name, audience, and brand colors from the client config.
    Platform rules and image brief format are universal and appended after.
    """
    org_name    = client["client_name"]
    org_desc    = client["identity"]["org_description"].strip()
    audience    = client["identity"]["audience"].strip()
    bg_hex      = client["colors"]["background"]["hex"]
    text_hex    = client["colors"]["text"]["hex"]
    accent_hex  = client["colors"]["accent"]["hex"]
    bg_name     = client["colors"]["background"]["name"]
    accent_name = client["colors"]["accent"]["name"]

    return (
        f"You are the social media content generator for {org_name} — {org_desc}\n\n"
        f"Your job is to write 3 social media posts per week based on the latest California "
        f"housing bill intelligence. These posts reach {audience}\n\n"
        f"Colors follow {org_name} brand: {bg_name} {bg_hex} background, "
        f"white {text_hex} text, {accent_name} {accent_hex} accent\n\n"
        f"{_PLATFORM_AND_IMAGE_RULES}"
    )


def _build_system_prompt(voice_text: str, client: dict) -> str:
    """Combine the client base prompt with a loaded voice file."""
    base = _build_base_prompt(client)
    if not voice_text:
        return base
    return f"{base}\n\n---\n\n## VOICE & TONE\n\n{voice_text}"


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


def _generate_content(
    bill_set: dict,
    client: anthropic.Anthropic,
    media_digest: dict | None = None,
    voice_text: str = "",
    client_cfg: dict | None = None,
) -> dict:
    """Single Claude call returning all 3 posts as a structured dict."""
    watch_ctx   = "\n\n".join(_build_bill_context(b) for b in bill_set["watch_list"])
    new_ctx     = "\n\n".join(_build_bill_context(b) for b in bill_set["new_bills"])
    hearing_ctx = "\n".join(_format_hearing(h) for h in bill_set["upcoming_hearings"])
    media_ctx   = _format_media_context(media_digest)

    # Use client brand colors for the JSON schema defaults
    cfg = client_cfg or {}
    bg_hex     = cfg.get("colors", {}).get("background", {}).get("hex", "#1a3a5c")
    text_hex   = cfg.get("colors", {}).get("text",       {}).get("hex", "#ffffff")
    accent_hex = cfg.get("colors", {}).get("accent",     {}).get("hex", "#c9a227")

    user_prompt = f"""\
Here is this week's bill intelligence. Write exactly 3 social media posts as specified.

== HIGH-RISK WATCH LIST (top bills, 2+ criteria strong/moderate) ==
{watch_ctx if watch_ctx else "(No high-risk bills with complete analysis this week)"}

== NEW BILLS THIS WEEK (recently tracked, at least 1 risk signal) ==
{new_ctx if new_ctx else "(No newly tracked bills this week)"}

== UPCOMING HEARINGS (next 7 days) ==
{hearing_ctx if hearing_ctx else "(No hearings scheduled this week)"}

{media_ctx if media_ctx else "== NEWS & MEDIA CONTEXT ==\n(No media digest available — run media_scanner.py to enable news-aware posts)"}

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
        "headline": "<6-10 words — direct declarative statement, no wordplay, immediately clear at a glance>",
        "subtext": "<8-12 words — the specific mechanism or risk>",
        "background_color": "{bg_hex}",
        "text_color": "{text_hex}",
        "accent_color": "{accent_hex}",
        "typographic_element": "<e.g. 'Bill number AB1234 as oversized display type, upper-left'>",
        "optional_graphic": "<e.g. 'California Capitol silhouette, faint, bottom-right' or 'none'>",
        "ai_image_prompt": "<2-4 sentences for AI image generator: describe background visual/graphic element, color palette in natural language, style direction. End with 'No text. No typography. No people. No logos.'>",
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
        "background_color": "{bg_hex}",
        "text_color": "{text_hex}",
        "accent_color": "{accent_hex}",
        "typographic_element": "...",
        "optional_graphic": "...",
        "ai_image_prompt": "...",
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
        "background_color": "{bg_hex}",
        "text_color": "{text_hex}",
        "accent_color": "{accent_hex}",
        "typographic_element": "...",
        "optional_graphic": "...",
        "ai_image_prompt": "...",
        "sizes": ["1080x1080 (Instagram/Facebook square)", "1600x900 (X/Facebook landscape)"]
      }}
    }}
  ]
}}

Return ONLY valid JSON. No markdown fences. No commentary outside the JSON object.
Count X characters carefully — every space and punctuation mark counts toward the 280 limit.
"""

    system_prompt = _build_system_prompt(voice_text, client_cfg or {})
    log.info("→ Calling Claude to generate social media content...")
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        system=system_prompt,
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


def _render_markdown(
    content: dict,
    bill_set: dict,
    voice_name: str = DEFAULT_VOICE,
    client_cfg: dict | None = None,
) -> str:
    """Render the full content package as a copy-paste-ready markdown file."""
    cfg         = client_cfg or {}
    client_name = cfg.get("client_name", "CSF")
    today       = date.today()
    voice_label = f" · Voice: `{voice_name}`" if voice_name != DEFAULT_VOICE else ""
    lines = [
        f"# {client_name} Social Media — Week of {today.strftime('%B %-d, %Y')}",
        "",
        f"*3 posts · Meta platforms + X · Generated {today.isoformat()}{voice_label}*",
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

        x_text       = post.get("x", "")
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
            f"| **Background** | `{ib.get('background_color', cfg.get('colors', {}).get('background', {}).get('hex', '#1a3a5c'))}` |",
            f"| **Text** | `{ib.get('text_color', cfg.get('colors', {}).get('text', {}).get('hex', '#ffffff'))}` |",
            f"| **Accent** | `{ib.get('accent_color', cfg.get('colors', {}).get('accent', {}).get('hex', '#c9a227'))}` |",
            f"| **Typographic element** | {ib.get('typographic_element', 'None')} |",
            f"| **Optional graphic** | {ib.get('optional_graphic', 'None')} |",
        ]

        for sz in ib.get("sizes", []):
            lines.append(f"| **Size** | {sz} |")

        image_paths = post.get("image_paths", {})
        if image_paths:
            lines += ["", "**Generated Images**", ""]
            for kind, rel_path in image_paths.items():
                size_label = "1080×1080 Square" if kind == "square" else "1600×900 Landscape"
                lines.append(f"- {size_label}: `{rel_path}`")
        else:
            ai_prompt = ib.get("ai_image_prompt", "")
            if ai_prompt:
                lines += [
                    "",
                    "**AI Image Prompt** *(fallback — paste into DALL-E 3 / Midjourney / Flux for manual image creation)*",
                    "",
                    f"> {ai_prompt}",
                ]

        lines += ["", "---", ""]

    # Source data footer
    watch_bills = ", ".join(b["bill_number"] for b in bill_set["watch_list"])
    client_slug = cfg.get("slug", "csf")
    lines += [
        "---",
        "",
        "## Source Data",
        "",
        f"- **Watch list bills:** {watch_bills or 'None (run housing_analyzer.py first)'}",
        f"- **Upcoming hearings:** {len(bill_set['upcoming_hearings'])}",
        f"- **New bills this week:** {len(bill_set['new_bills'])}",
        f"- **Client:** `{client_slug}` (`clients/{client_slug}/client.yml`)",
        f"- **Voice:** `{voice_name}` (`clients/{client_slug}/voices/{voice_name}.md`)",
        f"- **Generated:** {today.isoformat()}",
        "",
        f"*Generated by `agents/social/social_writer.py` — {client_name}*",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML output (shareable preview)
# ---------------------------------------------------------------------------

_SANS  = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;"
_SERIF = "font-family:Georgia,'Times New Roman',Times,serif;"
_SAND  = "#f5f2ed"
_INK   = "#1c1c1e"
_MID   = "#666666"
_RULE  = "#ddd8ce"
_WHITE = "#ffffff"

_PLATFORM_COLORS = {
    "x":        ("#000000", "#ffffff"),
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


def _img_data_uri(abs_path: Path, max_width: int) -> str:
    """Resize image to max_width px wide and return as a PNG data URI."""
    try:
        import base64
        import io as _io
        from PIL import Image
        img = Image.open(abs_path).convert("RGB")
        if img.width > max_width:
            ratio    = max_width / img.width
            new_size = (max_width, int(img.height * ratio))
            img      = img.resize(new_size, Image.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/png;base64,{b64}"
    except Exception as e:
        log.warning(f"_img_data_uri failed for {abs_path.name}: {e}")
        return ""


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
    n  = len(text)
    ok = n <= 280
    bg = "#e8f5e9" if ok else "#ffebee"
    fg = "#2e7d32" if ok else "#c62828"
    label = f"{n}/280 ✓" if ok else f"{n}/280 — {n-280} over limit"
    return (
        f'<span style="{_SANS}background:{bg};color:{fg};font-size:11px;'
        f'font-weight:600;padding:2px 8px;border-radius:10px;margin-left:8px;">'
        f'{label}</span>'
    )


def _render_post_card(post: dict, index: int, client_cfg: dict, output_dir: Path) -> str:
    num      = post.get("post_number", index + 1)
    ptype    = post.get("post_type", "")
    label    = _POST_TYPE_LABELS.get(ptype, ptype.replace("_", " ").title())
    icon     = _POST_TYPE_ICONS.get(ptype, "")
    bill_num = post.get("bill_number")
    ib       = post.get("image_brief", {})
    hashtags = " ".join(f"#{h.lstrip('#')}" for h in post.get("hashtags", []))

    navy   = client_cfg.get("colors", {}).get("background", {}).get("hex", "#1a3a5c")
    gold   = client_cfg.get("colors", {}).get("accent",     {}).get("hex", "#c9a227")

    bill_badge = ""
    if bill_num:
        bill_badge = (
            f'<span style="{_SANS}background:{gold};color:{navy};font-size:11px;'
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
          <div style="{_SANS}font-size:13px;color:{navy};line-height:1.9;">
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

    # AI image prompt — shown only when no real generated images are present
    ai_prompt      = ib.get("ai_image_prompt", "")
    image_paths    = post.get("image_paths", {})
    ai_prompt_block = ""
    if ai_prompt and not image_paths:
        ai_prompt_block = f"""
          <div style="margin-top:14px;border-top:1px dashed {_RULE};padding-top:12px;">
            <div style="{_SANS}font-size:11px;font-weight:700;color:{_MID};
                        text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">
              AI Image Prompt
              <span style="font-weight:400;text-transform:none;letter-spacing:0;
                           font-size:10px;color:{_MID};">
                — fallback for manual image creation
              </span>
            </div>
            <div style="{_SANS}font-size:13px;color:{_INK};line-height:1.6;
                        font-style:italic;background:#f0f4f8;border-radius:6px;
                        padding:10px 14px;">
              {_esc(ai_prompt)}
            </div>
          </div>"""

    # Image proof strip
    image_proof_strip = ""
    if image_paths:
        sq_rel  = image_paths.get("square", "")
        ls_rel  = image_paths.get("landscape", "")
        sq_cell = ""
        ls_cell = ""

        if sq_rel:
            sq_abs  = output_dir / sq_rel
            sq_uri  = _img_data_uri(sq_abs, max_width=200)
            sq_href = sq_rel
            sq_img  = (
                f'<img src="{sq_uri or sq_href}" width="200" height="200" '
                f'style="display:block;border-radius:8px;border:2px solid {_RULE};">'
            )
            sq_cell = f"""
              <td valign="top" style="padding-right:16px;">
                <div style="{_SANS}font-size:10px;font-weight:700;color:{_MID};
                            text-transform:uppercase;letter-spacing:0.8px;
                            margin-bottom:6px;">1080×1080 Square</div>
                <a href="{sq_href}" target="_blank" style="display:block;">{sq_img}</a>
              </td>"""

        if ls_rel:
            ls_abs  = output_dir / ls_rel
            ls_uri  = _img_data_uri(ls_abs, max_width=520)
            ls_href = ls_rel
            ls_img  = (
                f'<img src="{ls_uri or ls_href}" width="520" height="293" '
                f'style="display:block;border-radius:8px;border:2px solid {_RULE};">'
            )
            ls_cell = f"""
              <td valign="top">
                <div style="{_SANS}font-size:10px;font-weight:700;color:{_MID};
                            text-transform:uppercase;letter-spacing:0.8px;
                            margin-bottom:6px;">1600×900 Landscape</div>
                <a href="{ls_href}" target="_blank" style="display:block;">{ls_img}</a>
              </td>"""

        image_proof_strip = f"""
        <div style="background:#f0f4f8;border:1px solid {_RULE};border-radius:10px;
                    padding:16px 20px;margin-bottom:24px;">
          <div style="{_SANS}font-size:11px;font-weight:700;color:{navy};
                      text-transform:uppercase;letter-spacing:1.2px;margin-bottom:14px;">
            ✓ Generated Images &nbsp;
            <span style="font-weight:400;color:{_MID};text-transform:none;
                         letter-spacing:0;font-size:10px;">
              — click to open full size
            </span>
          </div>
          <table cellpadding="0" cellspacing="0">
            <tr>{sq_cell}{ls_cell}</tr>
          </table>
        </div>"""

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
        <div style="background:#fafaf7;border:1px solid {_RULE};border-left:3px solid {gold};
                    border-radius:8px;padding:16px 20px;margin-top:4px;">
          <div style="{_SANS}font-size:11px;font-weight:700;color:{gold};
                      text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;">
            Image Brief
          </div>
          <table cellpadding="0" cellspacing="0" style="width:100%;margin-bottom:12px;">
            {brief_rows}
          </table>
          <div style="margin-bottom:8px;">{color_row}</div>
          {f'<div style="{_SANS}font-size:12px;color:{_MID};">Sizes: {sizes}</div>' if sizes else ""}
          {ai_prompt_block}
        </div>"""

    return f"""
  <!-- Post {num} -->
  <div style="background:{_WHITE};border:1px solid {_RULE};border-radius:12px;
              margin-bottom:32px;overflow:hidden;">

    <!-- Post header -->
    <div style="background:{navy};padding:20px 28px;">
      <div style="{_SANS}font-size:13px;font-weight:700;color:{gold};
                  text-transform:uppercase;letter-spacing:1.5px;margin-bottom:6px;">
        Post {num}
      </div>
      <div style="{_SANS}font-size:22px;font-weight:700;color:{_WHITE};line-height:1.2;">
        {icon} {label}{bill_badge}
      </div>
    </div>

    <!-- Post body -->
    <div style="padding:24px 28px;">
      {image_proof_strip}
      {platform_blocks}
      {hashtag_block}
      {image_brief_block}
    </div>
  </div>"""


def _render_html(
    content: dict,
    bill_set: dict,
    voice_name: str = DEFAULT_VOICE,
    client_cfg: dict | None = None,
    output_dir: Path | None = None,
) -> str:
    """Render the full content package as a shareable inline-styled HTML page."""
    cfg         = client_cfg or {}
    client_name = cfg.get("client_name", "CSF")
    client_slug = cfg.get("slug", "csf")
    navy        = cfg.get("colors", {}).get("background", {}).get("hex", "#1a3a5c")
    gold        = cfg.get("colors", {}).get("accent",     {}).get("hex", "#c9a227")
    label       = cfg.get("proof_sheet", {}).get("label", client_name)
    out_dir     = output_dir or (PROJECT_ROOT / "outputs" / "clients" / client_slug / "social")

    today      = date.today()
    week_str   = today.strftime("Week of %B %-d, %Y")
    theme      = _esc(content.get("week_theme", ""))
    watch_str  = ", ".join(b["bill_number"] for b in bill_set["watch_list"])
    voice_label = f" &nbsp;·&nbsp; Voice: <code>{voice_name}</code>" if voice_name != DEFAULT_VOICE else ""

    post_cards = "".join(
        _render_post_card(post, i, cfg, out_dir)
        for i, post in enumerate(content.get("posts", []))
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{client_name} Social Media — {week_str}</title>
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
            <div style="{_SANS}color:{navy};font-size:13px;font-weight:700;
                        text-transform:uppercase;letter-spacing:2px;margin-bottom:6px;
                        opacity:0.7;">{label}</div>
            <div style="{_SERIF}color:{navy};font-size:28px;font-weight:700;
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
        <tr><td style="border-top:2px solid {navy};font-size:0;">&nbsp;</td></tr>
      </table>
    </td>
  </tr>

  <!-- WEEK THEME -->
  <tr>
    <td style="padding-bottom:32px;">
      <div style="background:{navy};border-radius:10px;padding:20px 28px;">
        <div style="{_SANS}font-size:11px;font-weight:700;color:{gold};
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
        &nbsp;·&nbsp; {label}
        &nbsp;·&nbsp; Content only — no posts have been published
        {voice_label}
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
        description="Generate weekly social media content for a configured client.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Generate posts for the default CSF client
  python agents/social/social_writer.py

  # Generate posts for a specific client
  python agents/social/social_writer.py --client cma

  # Use the urgent voice for CSF (hearing imminent)
  python agents/social/social_writer.py --client csf --voice urgent

  # List all configured clients
  python agents/social/social_writer.py --list-clients

  # List voices available for a client
  python agents/social/social_writer.py --client cma --list-voices

  # Override lookback window for "new bills" detection
  python agents/social/social_writer.py --lookback 7

  # Use a different bill data source
  python agents/social/social_writer.py --bills data/bills/tracked_bills.json
        """,
    )
    p.add_argument(
        "--client", type=str, default=DEFAULT_CLIENT,
        help=(
            f"Client to generate content for (default: '{DEFAULT_CLIENT}'). "
            f"Must match a directory in clients/<name>/. "
            f"Run --list-clients to see all configured clients."
        ),
    )
    p.add_argument(
        "--list-clients", action="store_true", default=False,
        help="Print all available client names and exit.",
    )
    p.add_argument(
        "--voice", type=str, default=None,
        help=(
            "Voice to use for content generation. "
            "Must match a filename in clients/<client>/voices/<name>.md. "
            "Defaults to the client's default_voice setting. "
            "Run --list-voices to see all available voices for the selected client."
        ),
    )
    p.add_argument(
        "--list-voices", action="store_true", default=False,
        help="Print all available voice names for the selected client and exit.",
    )
    p.add_argument(
        "--bills", type=Path, default=None,
        help="Path to tracked_bills.json (default: data/bills/tracked_bills.json)",
    )
    p.add_argument(
        "--lookback", type=int, default=14,
        help="Days to look back when identifying new bills (default: 14)",
    )
    p.add_argument(
        "--no-media", action="store_true", default=False,
        help="Skip media digest even if data/media/media_digest.json exists",
    )
    p.add_argument(
        "--images", action="store_true", default=False,
        help="Generate PNG images via Nano Banana Pro (gemini-3-pro-image-preview) for each post. "
             "Requires GEMINI_API_KEY in environment or .env.",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    # ── --list-clients: print available clients and exit ─────────────────────
    if args.list_clients:
        clients = _list_clients()
        if clients:
            print("\n  Available clients (clients/):\n")
            for c in clients:
                marker = " ← default" if c == DEFAULT_CLIENT else ""
                print(f"    {c}{marker}")
            print(f"\n  Usage: --client <name>   e.g. --client cma\n")
        else:
            print(f"\n  No client directories found in {CLIENTS_DIR}\n")
        sys.exit(0)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set. Add it to .env or your environment.")
        sys.exit(1)

    # ── Load client config ────────────────────────────────────────────────────
    client_cfg = _load_client(args.client)
    client_id  = client_cfg.get("slug", args.client)
    client_name = client_cfg.get("client_name", client_id)

    banner = f"{client_name} — Social Media Writer"
    print(f"\n  {banner}")
    print("  " + "─" * len(banner))

    # ── Resolve voice ─────────────────────────────────────────────────────────
    voices_dir = CLIENTS_DIR / client_id / "voices"
    voice_name = args.voice or client_cfg.get("default_voice", DEFAULT_VOICE)

    # ── --list-voices: print voices for selected client and exit ──────────────
    if args.list_voices:
        voices = _list_voices(voices_dir)
        if voices:
            default_v = client_cfg.get("default_voice", DEFAULT_VOICE)
            print(f"\n  Available voices for '{client_id}' (clients/{client_id}/voices/):\n")
            for v in voices:
                marker = " ← default" if v == default_v else ""
                print(f"    {v}{marker}")
            print(f"\n  Usage: --voice <name>   e.g. --voice urgent\n")
        else:
            print(f"\n  No voice files found in {voices_dir}\n")
        sys.exit(0)

    voice_text = _load_voice(voice_name, voices_dir)
    # Normalise to the actual loaded name in case of fallback
    if voice_text and not (voices_dir / f"{voice_name}.md").exists():
        voice_name = client_cfg.get("default_voice", DEFAULT_VOICE)

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

    # ── Load media digest (optional) ─────────────────────────────────────────
    media_digest = None
    if not args.no_media:
        media_digest = _load_media_digest()
        if media_digest:
            n = len(media_digest.get("articles", []))
            x = len(media_digest.get("x_posts", []))
            log.info(f"→ Media digest loaded: {n} articles, {x} X posts")
        else:
            log.info("→ No media digest found — posts will be bill-data only")
            log.info("   (Run media_scanner.py first to enable news-aware posts)")

    # ── Generate content via Claude ─────────────────────────────────────────
    anthropic_client = anthropic.Anthropic(api_key=api_key)
    content = _generate_content(bill_set, anthropic_client, media_digest, voice_text, client_cfg)
    posts   = content.get("posts", [])
    log.info(f"   ✓ {len(posts)} posts generated")

    # ── Print summary ───────────────────────────────────────────────────────
    print(f"\n  Client:     {client_name}")
    print(f"  Week theme: {content.get('week_theme', '')}")
    print(f"  Voice:      {voice_name}\n")
    for post in posts:
        num     = post.get("post_number", "?")
        label   = _POST_TYPE_LABELS.get(post.get("post_type", ""), "Post")
        bill    = post.get("bill_number", "n/a")
        x_chars = len(post.get("x", ""))
        over_msg = f" ⚠ OVER by {x_chars - 280}" if x_chars > 280 else ""
        print(f"  Post {num} ({label:14s})  X: {x_chars}/280{over_msg}  bill: {bill}")

    # ── Prepare output paths ──────────────────────────────────────────────────
    output_dir = PROJECT_ROOT / "outputs" / "clients" / client_id / "social"
    output_dir.mkdir(parents=True, exist_ok=True)
    iso_week     = date.today().strftime("%G-W%V")
    voice_suffix = f"_{voice_name}" if voice_name != client_cfg.get("default_voice", DEFAULT_VOICE) else ""
    md_path      = output_dir / f"social_{iso_week}{voice_suffix}.md"
    html_path    = output_dir / f"social_{iso_week}{voice_suffix}.html"

    # ── Generate images BEFORE rendering (proof sheet includes them) ──────────
    if args.images:
        from agents.social.image_generator import generate_images
        images_dir = output_dir / "images" / iso_week
        log.info("→ Generating images (Nano Banana Pro)...")
        for post in posts:
            brief = post.get("image_brief")
            if not brief:
                continue
            if "bill_number" not in brief or brief.get("bill_number") is None:
                brief = dict(brief, bill_number=post.get("bill_number") or "")
            post_slug = f"post_{post.get('post_number', 'N')}"
            paths = generate_images(brief, images_dir, post_slug, brand=client_cfg)
            if paths:
                post["image_paths"] = {
                    k: str(Path(v).relative_to(output_dir))
                    for k, v in paths.items()
                }
        log.info("→ Image generation complete")

    # ── Render and write outputs ──────────────────────────────────────────────
    log.info("→ Rendering outputs...")
    markdown = _render_markdown(content, bill_set, voice_name, client_cfg)
    html     = _render_html(content, bill_set, voice_name, client_cfg, output_dir)

    md_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(html,   encoding="utf-8")

    print(f"\n  ✓ Markdown: {md_path.relative_to(PROJECT_ROOT)}")
    print(f"  ✓ HTML:     {html_path.relative_to(PROJECT_ROOT)}")
    if args.images:
        for post in posts:
            n = post.get("post_number", "?")
            for kind, rel_path in post.get("image_paths", {}).items():
                print(f"  ✓ Image post {n} ({kind}): {output_dir.relative_to(PROJECT_ROOT) / rel_path}")
    print(f"\n  Share preview: file://{html_path}")
    print(f"  Copy-paste:    file://{md_path}\n")


if __name__ == "__main__":
    main()
