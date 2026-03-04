#!/usr/bin/env python3
"""
oped_writer.py — Op-Ed and Letter to Editor Writer
California Stewardship Fund

Reads tracked_bills.json (output of bill_tracker + housing_analyzer) and calls
Claude to generate a publishable op-ed draft or letter to editor anchored to
the week's highest-risk California housing bill.

Pipeline position:
    bill_tracker.py → tracked_bills.json → housing_analyzer.py ──┐
    media_scanner.py → data/media/media_digest.json ─────────────┤
                                                                  ↓
                                                         oped_writer.py

Client system:
    Each run targets a client from clients/<id>/client.yml.
    The client file defines brand identity (org, audience, colors).
    Voice files live in clients/<id>/voices/<name>.md and control the author's
    persuasion arc, signature language, and format calibration.

    Default client: clients/csf/  (California Stewardship Fund)
    Add a client:   create clients/<slug>/client.yml + clients/<slug>/voices/
    Select client:  --client <slug>
    List clients:   --list-clients

Voice system:
    Default CSF op-ed voice: oped_mike_lewis (captures the author's writing style)
    List voices: --list-voices  (scoped to selected client)

Format options:
    --format oped    — 700-900 word op-ed column (default)
    --format letter  — 250-450 word letter to editor

Target publication options:
    --target calmatters     — CalMatters (accessible, California-specific)
    --target wsj            — Wall Street Journal / national (financial angle)
    --target capitol-weekly — Capitol Weekly (insider, political)
    --target local          — Local newspapers (community stakes)

Usage:
    .venv/bin/python agents/opeds/oped_writer.py                           # csf + default voice + oped + calmatters
    .venv/bin/python agents/opeds/oped_writer.py --format letter           # letter to editor format
    .venv/bin/python agents/opeds/oped_writer.py --target wsj              # Wall Street Journal calibration
    .venv/bin/python agents/opeds/oped_writer.py --bill AB1751             # anchor to a specific bill
    .venv/bin/python agents/opeds/oped_writer.py --peg "WSJ condo bust"   # add a news peg for letters
    .venv/bin/python agents/opeds/oped_writer.py --dry-run                 # preview without writing files
    .venv/bin/python agents/opeds/oped_writer.py --list-voices             # list available voices
    .venv/bin/python agents/opeds/oped_writer.py --list-clients            # list configured clients

Output (default):   outputs/clients/csf/opeds/oped_YYYY-WNN.md + .html
Output (letter):    outputs/clients/csf/opeds/oped_YYYY-WNN_letter.md + .html
Output (non-default voice): filename includes voice suffix, e.g. oped_YYYY-WNN_coalition.md
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

import anthropic
from dotenv import load_dotenv

# Resolve project root and add to path so shared utilities can be imported
# regardless of the working directory the script is called from.
_HERE        = Path(__file__).resolve().parent
PROJECT_ROOT = _HERE.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agents.shared.bill_utils import _select_bills, _build_bill_context, _CRIT_KEYS
from agents.shared.client_utils import (
    _load_client,
    _list_clients,
    _load_voice,
    _list_voices,
    CLIENTS_DIR,
    DEFAULT_CLIENT,
    DEFAULT_VOICE,
)

load_dotenv(PROJECT_ROOT / ".env", override=True)

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

BILLS_FILE   = PROJECT_ROOT / "data" / "bills" / "tracked_bills.json"
MEDIA_DIGEST = PROJECT_ROOT / "data" / "media" / "media_digest.json"

_OPED_DEFAULT_VOICE = "oped_mike_lewis"

_VALID_FORMATS = ("oped", "letter")
_VALID_TARGETS = ("calmatters", "wsj", "capitol-weekly", "local")

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(level: str = "INFO") -> None:
    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(logs_dir / "oped_writer.log", encoding="utf-8"),
        ],
    )


# ---------------------------------------------------------------------------
# Media digest loader (same pattern as social_writer)
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
    """Format media digest into a Claude-readable context block for op-ed framing."""
    if not digest:
        return ""

    articles = digest.get("articles", [])[:6]
    summary  = digest.get("summary", {})

    if not articles:
        return ""

    lines = [
        "== NEWS & MEDIA CONTEXT (past 7 days) ==",
        "Use these stories as potential news pegs or evidence for the argument.",
        "Hook into the most relevant story if it strengthens local control framing.",
        "",
    ]
    for a in articles:
        score        = a.get("relevance_score", 0)
        source       = a.get("source", "")
        title        = a.get("title", "")
        pub          = a.get("published", "")
        bills        = a.get("bill_mentions", [])
        blurb        = a.get("summary", "")[:200]
        bill_str     = f"  [bills: {', '.join(bills)}]" if bills else ""
        lines.append(f"  [{score:.1f}] {source} | {pub} | {title}{bill_str}")
        if blurb:
            lines.append(f"       Summary: {blurb}")

    top_bills = summary.get("top_bill_mentions", [])
    if top_bills:
        lines.append("")
        lines.append(f"Bills getting the most media attention: {', '.join(top_bills[:5])}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Format and target rules (structural prompt constants)
# ---------------------------------------------------------------------------

_OPED_STRUCTURE_RULES = """\
WRITING FORMAT: Standard Op-Ed (700–900 words)

STRUCTURE — Write exactly these sections:

hook
  1-2 sentences. Open with a paradox, surprising convergence between unlikely
  parties, or a "curious thing" observation. Do NOT open with alarm or outrage.
  Open with a puzzle that earns the reader's intellectual attention before the
  argument begins.

thesis
  1-2 sentences. The argument in plain language. No hedging. Often a clean pivot
  from the hook: "This is no longer ideological. It's an evidence problem."

argument_1
  1-2 paragraphs. Name the mechanism — strip away the language of good intentions
  and say what the bill actually does. Then name the cost transfer: who profits
  (developers, institutional investors, build-to-rent platforms), and who pays
  (local taxpayers, existing residents, cities absorbing infrastructure costs).

argument_2
  1-2 paragraphs. Connect to the pattern. This is never just one bill. Show how
  it fits Sacramento's escalation from persuasion to mandate to penalty to
  preemption. Reference supporting bills as evidence of the pattern.

argument_3
  1-2 paragraphs. The civic stakes. Communities are not modular — they are more
  like redwood groves than empty lots: slow to grow, deeply rooted, impossible
  to replace once cut down. The permanence argument: housing policy locks in
  outcomes for generations. When forced growth fails to deliver affordability,
  the damage is lived and lasting.

concession
  1 paragraph. Steelman the opposition. "Supporters of this approach argue..."
  Then rebut cleanly: "But..." One acknowledgment sentence, then the rebuttal.
  Do not argue against a strawman. Do not apologize for the argument.

close
  2-4 sentences. End on civic/democratic values — not policy mechanics. The
  closing question is always some version of: who decides, and who loses standing
  when Sacramento overrides them? Do not end with a policy recommendation. End
  with a moral frame, an unanswered question, or the permanence of what is being
  lost.

WORD COUNT TARGET: 700–900 words across all sections combined.\
"""

_LETTER_STRUCTURE_RULES = """\
WRITING FORMAT: Letter to Editor (250–450 words)

STRUCTURE — Write exactly these sections:

hook
  1-2 sentences referencing the specific article, report, or news event being
  responded to. Name the publication and describe what the piece got right —
  then name what it missed.

thesis
  1 sentence. The pivot: what the piece's own evidence actually shows, or what
  the missing context reveals.

argument_1
  2-3 tight paragraphs. The argument. No warm-up. No padding. Every sentence
  earns its place or it goes.

close
  1 sentence. A moral frame, a hard question, or the permanence of what is at
  stake. No policy recommendation. End with weight.

LEAVE BLANK (null): argument_2, argument_3, concession.
These are full op-ed sections — letters do not include them.

WORD COUNT TARGET: 250–450 words across all sections combined.\
"""

_TARGET_CALIBRATION = {
    "calmatters": (
        "TARGET: CalMatters\n"
        "Lead with California-specific evidence and outcomes. Accessible but not "
        "dumbed down — this audience follows Sacramento but also cares about their "
        "own community. Name specific bills and their real-world effects. "
        "Statewide stakes are the frame. Tone: confident, direct, evidence-first."
    ),
    "wsj": (
        "TARGET: Wall Street Journal / National Outlet\n"
        "Lead with the financial or market angle. Frame local control as a property "
        "rights and governance efficiency question. This audience is policy-literate "
        "and skeptical of state intervention — but also skeptical of developer capture. "
        "The financialization argument lands well here. Tone: measured, analytical, "
        "insider-to-insider. Avoid California-only references; generalize where possible."
    ),
    "capitol-weekly": (
        "TARGET: Capitol Weekly\n"
        "Insider tone. Name the players — legislators, advocacy groups, key votes — "
        "early. Shorter setup before the argument; this audience already knows the "
        "terrain. Reference specific bill sections and legislative history where useful. "
        "Tone: sharp, political, assumes deep Sacramento fluency."
    ),
    "local": (
        "TARGET: Local Newspaper\n"
        "Lead with local stakes — what happens to this city, this neighborhood, this "
        "tax base. Explain mechanisms in plain language before naming them. Say 'the "
        "bill would require your city to approve new apartments without a public "
        "hearing' before citing the bill number. Tone: neighbor-to-neighbor, alarmed "
        "but not alarmist. Make the argument without jargon."
    ),
}


# ---------------------------------------------------------------------------
# Claude content generation
# ---------------------------------------------------------------------------

def _build_base_prompt(client: dict, format_type: str, target: str) -> str:
    """Build the client- and format-specific opening of the system prompt."""
    org_name = client["client_name"]
    org_desc = client["identity"]["org_description"].strip()

    format_rules = (
        _OPED_STRUCTURE_RULES if format_type == "oped" else _LETTER_STRUCTURE_RULES
    )
    target_rules = _TARGET_CALIBRATION.get(target, _TARGET_CALIBRATION["calmatters"])

    return (
        f"You are an op-ed writer for {org_name} — {org_desc}\n\n"
        f"Your task is to write a publishable opinion piece anchored to California "
        f"housing legislation and its threat to local government authority.\n\n"
        f"This piece will appear under a named author's byline and must read as "
        f"individual voice, not organizational messaging. Confident, evidence-grounded, "
        f"willing to name what is actually happening.\n\n"
        f"{format_rules}\n\n"
        f"---\n\n"
        f"{target_rules}"
    )


def _build_system_prompt(voice_text: str, client: dict, format_type: str, target: str) -> str:
    """Combine client base prompt with the loaded voice file."""
    base = _build_base_prompt(client, format_type, target)
    if not voice_text:
        return base
    return f"{base}\n\n---\n\n## VOICE, STYLE & PERSUASION ARC\n\n{voice_text}"


def _generate_content(
    anchor_bill:      dict,
    supporting_bills: list[dict],
    anthropic_client: anthropic.Anthropic,
    media_digest:     dict | None = None,
    voice_text:       str = "",
    client_cfg:       dict | None = None,
    format_type:      str = "oped",
    target:           str = "calmatters",
    news_peg:         str = "",
    model:            str = "claude-sonnet-4-6",
) -> dict:
    """Single Claude call returning the full draft as a structured dict."""
    anchor_ctx     = _build_bill_context(anchor_bill)
    supporting_ctx = "\n\n".join(_build_bill_context(b) for b in supporting_bills)
    media_ctx      = _format_media_context(media_digest)

    # Upcoming hearing urgency signal for anchor bill
    today         = date.today()
    hearing_lines = []
    for h in anchor_bill.get("upcoming_hearings", []):
        try:
            from datetime import date as _date
            hdate = _date.fromisoformat(h["date"])
            days_out = (hdate - today).days
            if 0 <= days_out <= 14:
                hearing_lines.append(
                    f"  UPCOMING: {h.get('date')} — {h.get('committee', 'Unknown Committee')} "
                    f"({days_out} days away)"
                )
        except (KeyError, ValueError):
            pass
    hearing_urgency = (
        "\n== UPCOMING HEARING — POTENTIAL NEWS PEG ==\n" + "\n".join(hearing_lines)
        if hearing_lines else ""
    )

    # News peg block (manual --peg override)
    peg_block = ""
    if news_peg:
        peg_block = (
            f"\n== NEWS PEG (use this as the hook reference) ==\n"
            f"{news_peg}\n"
            f"For a letter to editor: reference this article in the opening sentence.\n"
            f"For an op-ed: use this as context for the hook or anchor the piece to it.\n"
        )

    # JSON schema — letter format omits argument_2, argument_3, concession
    if format_type == "letter":
        schema = """\
{
  "headline_options": [
    "<headline option 1 — 8-14 words, direct and declarative>",
    "<headline option 2 — different angle>",
    "<headline option 3 — different angle>"
  ],
  "dek": "<subheadline/deck — 15-25 words summarizing the argument>",
  "hook": "<1-2 sentences — names article being responded to; what it missed>",
  "thesis": "<1 sentence — the pivot: what the evidence actually shows>",
  "argument_1": "<2-3 tight paragraphs — the argument>",
  "argument_2": null,
  "argument_3": null,
  "concession": null,
  "close": "<1 sentence — moral frame, hard question, or permanence>",
  "word_count": 0,
  "anchor_bill": "<bill number>",
  "pitch_note": "<75-100 words — editor pitch: why this piece now, what it argues, why this author>"
}"""
    else:
        schema = """\
{
  "headline_options": [
    "<headline option 1 — 8-14 words, direct and declarative>",
    "<headline option 2 — different angle on the same argument>",
    "<headline option 3 — different angle>"
  ],
  "dek": "<subheadline/deck — 15-25 words summarizing the argument>",
  "hook": "<1-2 sentences — paradox, surprising convergence, or curious observation>",
  "thesis": "<1-2 sentences — the argument in plain language, no hedging>",
  "argument_1": "<1-2 paragraphs — the mechanism + who profits + who pays>",
  "argument_2": "<1-2 paragraphs — the pattern: this bill as symptom of Sacramento's escalation>",
  "argument_3": "<1-2 paragraphs — the civic stakes: permanence, communities as redwood groves>",
  "concession": "<1 paragraph — steelman then rebut: 'Supporters argue... But...'>",
  "close": "<2-4 sentences — civic/democratic values, not policy mechanics>",
  "word_count": 0,
  "anchor_bill": "<bill number>",
  "pitch_note": "<75-100 words — editor pitch: why this piece now, what it argues, why this author>"
}"""

    user_prompt = f"""\
Here is this week's bill intelligence. Write a publishable opinion piece as specified.

== ANCHOR BILL (build the piece around this) ==
{anchor_ctx}
{hearing_urgency}

== SUPPORTING BILLS (cite as pattern evidence — not the main argument) ==
{supporting_ctx if supporting_ctx else "(No supporting bills available — focus the piece on the anchor bill alone)"}

{peg_block}
{media_ctx if media_ctx else "== NEWS & MEDIA CONTEXT ==\n(No media digest available)"}

---

Write the piece now. Return a JSON object with exactly this structure:

{schema}

CRITICAL RULES:
- Return ONLY valid JSON. No markdown fences. No commentary outside the JSON object.
- The word_count field should reflect your actual count of all prose sections combined.
- Headline options must be genuinely different angles — not slight rewrites of each other.
- The pitch_note is for an editor's inbox: one sentence on the news hook, one on the
  argument, one on why this author is credible. Businesslike, not promotional.
- Do NOT use any of these words: stakeholders, framework, pathway, impacts, aims to,
  seeks to, going forward, at the end of the day, moving the needle.
- One rhetorical question maximum across the entire piece. Make it count.
- Do not open with alarm or outrage. Open with a puzzle.
"""

    system_prompt = _build_system_prompt(voice_text, client_cfg or {}, format_type, target)

    log.info(f"→ Calling Claude ({model}) to draft {format_type} for {anchor_bill['bill_number']}...")
    message = anthropic_client.messages.create(
        model=model,
        max_tokens=3000,
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
# Prose assembly helpers
# ---------------------------------------------------------------------------

def _assemble_prose(content: dict) -> str:
    """Assemble the section dict into flowing prose paragraphs."""
    sections = []
    for key in ("hook", "thesis", "argument_1", "argument_2", "argument_3",
                "concession", "close"):
        val = content.get(key)
        if val:
            sections.append(val.strip())
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Markdown output
# ---------------------------------------------------------------------------

def _render_markdown(
    content:          dict,
    anchor_bill:      dict,
    supporting_bills: list[dict],
    voice_name:       str = _OPED_DEFAULT_VOICE,
    format_type:      str = "oped",
    target:           str = "calmatters",
    client_cfg:       dict | None = None,
    news_peg:         str = "",
) -> str:
    """Render the op-ed draft as a copy-paste-ready markdown file."""
    cfg         = client_cfg or {}
    client_name = cfg.get("client_name", "CSF")
    client_slug = cfg.get("slug", "csf")
    today       = date.today()

    headlines = content.get("headline_options", [])
    dek       = content.get("dek", "")
    pitch     = content.get("pitch_note", "")
    wc        = content.get("word_count", 0)
    anchor    = anchor_bill.get("bill_number", "")
    prose     = _assemble_prose(content)

    format_label = "Op-Ed" if format_type == "oped" else "Letter to Editor"
    target_labels = {
        "calmatters":     "CalMatters",
        "wsj":            "Wall Street Journal / National",
        "capitol-weekly": "Capitol Weekly",
        "local":          "Local Newspaper",
    }
    target_label = target_labels.get(target, target)

    lines = [
        f"# {client_name} — {format_label} Draft",
        f"**Anchor bill:** {anchor}  |  **Target:** {target_label}  |  "
        f"**Generated:** {today.isoformat()}  |  **Word count:** ~{wc}",
        "",
        "---",
        "",
        "## Headline Options",
        "",
    ]
    for i, h in enumerate(headlines, 1):
        lines.append(f"{i}. {h}")
    lines += [
        "",
        f"**Dek:** {dek}",
        "",
        "---",
        "",
        "## Full Draft",
        "",
    ]

    # Render prose with section labels as comments for editorial reference
    section_order = [
        ("hook",        "HOOK"),
        ("thesis",      "THESIS"),
        ("argument_1",  "ARGUMENT 1 — Mechanism & Cost Transfer"),
        ("argument_2",  "ARGUMENT 2 — The Pattern"),
        ("argument_3",  "ARGUMENT 3 — Civic Stakes"),
        ("concession",  "CONCESSION & REBUTTAL"),
        ("close",       "CLOSE"),
    ]
    for key, label in section_order:
        val = content.get(key)
        if val:
            lines += [
                f"<!-- {label} -->",
                val.strip(),
                "",
            ]

    lines += [
        "---",
        "",
        "## Editor Pitch Note",
        "",
        f"> {pitch}",
        "",
        "---",
        "",
        "## Submission Notes",
        "",
        f"- **Format:** {format_label}",
        f"- **Target publication:** {target_label}",
        f"- **Word count:** ~{wc} words",
        f"- **Anchor bill:** {anchor}",
    ]

    if supporting_bills:
        supp = ", ".join(b["bill_number"] for b in supporting_bills)
        lines.append(f"- **Supporting bills:** {supp}")
    if news_peg:
        lines.append(f"- **News peg:** {news_peg}")
    lines += [
        f"- **Voice:** `{voice_name}` (`clients/{client_slug}/voices/{voice_name}.md`)",
        f"- **Client:** `{client_slug}` (`clients/{client_slug}/client.yml`)",
        "",
        "> ⚠️  This is an AI-generated first draft. Review carefully before submission.",
        "> Edit in Word or Google Docs; do not auto-submit.",
        "",
        f"*Generated by `agents/opeds/oped_writer.py` — {client_name}*",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML output (shareable proof sheet)
# ---------------------------------------------------------------------------

_SANS  = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;"
_SERIF = "font-family:Georgia,'Times New Roman',Times,serif;"
_SAND  = "#f5f2ed"
_INK   = "#1c1c1e"
_MID   = "#666666"
_RULE  = "#ddd8ce"
_WHITE = "#ffffff"
_WARN  = "#7f4f00"
_WARN_BG = "#fff3cd"


def _esc(text: str) -> str:
    """Minimal HTML escaping for user content."""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n\n", "</p><p>")
            .replace("\n", "<br>"))


def _render_html(
    content:          dict,
    anchor_bill:      dict,
    supporting_bills: list[dict],
    voice_name:       str = _OPED_DEFAULT_VOICE,
    format_type:      str = "oped",
    target:           str = "calmatters",
    client_cfg:       dict | None = None,
    news_peg:         str = "",
) -> str:
    """Render the op-ed draft as an inline-styled HTML proof sheet."""
    cfg         = client_cfg or {}
    client_name = cfg.get("client_name", "CSF")
    client_slug = cfg.get("slug", "csf")
    navy        = cfg.get("colors", {}).get("background", {}).get("hex", "#1a3a5c")
    gold        = cfg.get("colors", {}).get("accent",     {}).get("hex", "#c9a227")
    today       = date.today()

    headlines   = content.get("headline_options", [])
    dek         = content.get("dek", "")
    pitch       = content.get("pitch_note", "")
    wc          = content.get("word_count", 0)
    anchor      = anchor_bill.get("bill_number", "")

    format_label = "Op-Ed" if format_type == "oped" else "Letter to Editor"
    target_labels = {
        "calmatters":     "CalMatters",
        "wsj":            "Wall Street Journal",
        "capitol-weekly": "Capitol Weekly",
        "local":          "Local Newspaper",
    }
    target_label = target_labels.get(target, target)

    # Headline options block
    headline_items = "".join(
        f'<div style="{_SANS}padding:10px 16px;border-bottom:1px solid {_RULE};'
        f'font-size:15px;font-weight:600;color:{_INK};line-height:1.4;">'
        f'<span style="color:{gold};font-weight:700;margin-right:8px;">{i}.</span>'
        f'{_esc(h)}</div>'
        for i, h in enumerate(headlines, 1)
    )

    # Dek
    dek_block = (
        f'<div style="{_SANS}font-size:14px;color:{_MID};font-style:italic;'
        f'padding:14px 16px;background:#f8f7f4;border-bottom:1px solid {_RULE};">'
        f'<strong style="color:{navy};font-style:normal;">Dek:</strong> {_esc(dek)}</div>'
        if dek else ""
    )

    # Section-by-section prose blocks
    section_order = [
        ("hook",        "Hook"),
        ("thesis",      "Thesis"),
        ("argument_1",  "Argument 1 — Mechanism &amp; Cost Transfer"),
        ("argument_2",  "Argument 2 — The Pattern"),
        ("argument_3",  "Argument 3 — Civic Stakes"),
        ("concession",  "Concession &amp; Rebuttal"),
        ("close",       "Close"),
    ]
    prose_blocks = ""
    for key, label in section_order:
        val = content.get(key)
        if not val:
            continue
        prose_blocks += (
            f'<div style="{_SERIF}font-size:16px;color:{_INK};line-height:1.8;'
            f'margin-bottom:1.4em;">'
            f'<p style="margin:0;">{_esc(val.strip())}</p></div>'
        )

    # Supporting bills row
    supp_str = ", ".join(b["bill_number"] for b in supporting_bills) if supporting_bills else "None"
    peg_row  = (
        f'<tr><td style="{_SANS}font-size:12px;font-weight:700;color:{_MID};'
        f'padding:4px 12px 4px 0;white-space:nowrap;">News peg</td>'
        f'<td style="{_SANS}font-size:13px;color:{_INK};padding:4px 0;">{_esc(news_peg)}</td></tr>'
    ) if news_peg else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{client_name} — {format_label} Draft: {anchor}</title>
</head>
<body style="margin:0;padding:0;background:{_SAND};">

<!-- Outer wrapper -->
<table width="100%" cellpadding="0" cellspacing="0" style="background:{_SAND};padding:40px 16px;">
<tr><td align="center">

<!-- Container -->
<table width="780" cellpadding="0" cellspacing="0" style="max-width:780px;width:100%;">

  <!-- MASTHEAD -->
  <tr>
    <td style="padding-bottom:28px;">
      <div style="{_SANS}color:{navy};font-size:11px;font-weight:700;
                  text-transform:uppercase;letter-spacing:2px;margin-bottom:6px;
                  opacity:0.7;">{client_name}</div>
      <div style="{_SERIF}color:{navy};font-size:26px;font-weight:700;
                  letter-spacing:-0.3px;line-height:1.1;margin-bottom:4px;">
        {format_label} Draft
      </div>
      <div style="{_SANS}color:{_MID};font-size:13px;">
        Anchor: <strong>{anchor}</strong>
        &nbsp;·&nbsp; Target: <strong>{target_label}</strong>
        &nbsp;·&nbsp; ~{wc} words
        &nbsp;·&nbsp; Generated {today.isoformat()}
      </div>
      <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:14px;">
        <tr><td style="border-top:2px solid {navy};font-size:0;">&nbsp;</td></tr>
      </table>
    </td>
  </tr>

  <!-- WARNING BANNER -->
  <tr>
    <td style="padding-bottom:20px;">
      <div style="background:{_WARN_BG};border-left:4px solid {_WARN};
                  border-radius:6px;padding:12px 18px;">
        <span style="{_SANS}font-size:13px;color:{_WARN};font-weight:700;">
          ⚠ AI-Generated Draft
        </span>
        <span style="{_SANS}font-size:13px;color:{_WARN};">
          — Review carefully before submission. Edit in Word or Google Docs.
          Do not auto-submit.
        </span>
      </div>
    </td>
  </tr>

  <!-- HEADLINE OPTIONS -->
  <tr>
    <td style="padding-bottom:28px;">
      <div style="background:{_WHITE};border:1px solid {_RULE};border-radius:10px;
                  overflow:hidden;">
        <div style="background:{navy};padding:14px 20px;">
          <div style="{_SANS}font-size:11px;font-weight:700;color:{gold};
                      text-transform:uppercase;letter-spacing:1.5px;">
            Headline Options
          </div>
        </div>
        {headline_items}
        {dek_block}
      </div>
    </td>
  </tr>

  <!-- FULL DRAFT -->
  <tr>
    <td style="padding-bottom:28px;">
      <div style="background:{_WHITE};border:1px solid {_RULE};border-radius:10px;
                  overflow:hidden;">
        <div style="background:{navy};padding:14px 20px;">
          <div style="{_SANS}font-size:11px;font-weight:700;color:{gold};
                      text-transform:uppercase;letter-spacing:1.5px;">
            Full Draft
          </div>
        </div>
        <div style="padding:28px 32px;">
          {prose_blocks}
        </div>
      </div>
    </td>
  </tr>

  <!-- EDITOR PITCH NOTE -->
  <tr>
    <td style="padding-bottom:28px;">
      <div style="background:{_WHITE};border:1px solid {_RULE};
                  border-left:4px solid {gold};border-radius:10px;padding:20px 24px;">
        <div style="{_SANS}font-size:11px;font-weight:700;color:{gold};
                    text-transform:uppercase;letter-spacing:1.2px;margin-bottom:10px;">
          Editor Pitch Note
        </div>
        <div style="{_SERIF}font-size:15px;color:{_INK};line-height:1.7;
                    font-style:italic;">
          {_esc(pitch)}
        </div>
      </div>
    </td>
  </tr>

  <!-- SUBMISSION METADATA -->
  <tr>
    <td style="padding-bottom:28px;">
      <div style="background:#f8f7f4;border:1px solid {_RULE};border-radius:10px;
                  padding:20px 24px;">
        <div style="{_SANS}font-size:11px;font-weight:700;color:{navy};
                    text-transform:uppercase;letter-spacing:1.2px;margin-bottom:12px;">
          Submission Notes
        </div>
        <table cellpadding="0" cellspacing="0">
          <tr>
            <td style="{_SANS}font-size:12px;font-weight:700;color:{_MID};
                        padding:4px 12px 4px 0;white-space:nowrap;">Format</td>
            <td style="{_SANS}font-size:13px;color:{_INK};padding:4px 0;">{format_label}</td>
          </tr>
          <tr>
            <td style="{_SANS}font-size:12px;font-weight:700;color:{_MID};
                        padding:4px 12px 4px 0;white-space:nowrap;">Target</td>
            <td style="{_SANS}font-size:13px;color:{_INK};padding:4px 0;">{target_label}</td>
          </tr>
          <tr>
            <td style="{_SANS}font-size:12px;font-weight:700;color:{_MID};
                        padding:4px 12px 4px 0;white-space:nowrap;">Word count</td>
            <td style="{_SANS}font-size:13px;color:{_INK};padding:4px 0;">~{wc} words</td>
          </tr>
          <tr>
            <td style="{_SANS}font-size:12px;font-weight:700;color:{_MID};
                        padding:4px 12px 4px 0;white-space:nowrap;">Anchor bill</td>
            <td style="{_SANS}font-size:13px;color:{_INK};padding:4px 0;">{anchor}</td>
          </tr>
          <tr>
            <td style="{_SANS}font-size:12px;font-weight:700;color:{_MID};
                        padding:4px 12px 4px 0;white-space:nowrap;">Supporting bills</td>
            <td style="{_SANS}font-size:13px;color:{_INK};padding:4px 0;">{supp_str}</td>
          </tr>
          {peg_row}
          <tr>
            <td style="{_SANS}font-size:12px;font-weight:700;color:{_MID};
                        padding:4px 12px 4px 0;white-space:nowrap;">Voice</td>
            <td style="{_SANS}font-size:13px;color:{_INK};padding:4px 0;">
              <code>{voice_name}</code>
            </td>
          </tr>
        </table>
      </div>
    </td>
  </tr>

  <!-- FOOTER -->
  <tr>
    <td style="padding:8px 0 16px;text-align:center;">
      <div style="{_SANS}color:#aaa;font-size:11px;line-height:1.8;">
        Generated by <code>agents/opeds/oped_writer.py</code>
        &nbsp;·&nbsp; {client_name}
        &nbsp;·&nbsp; Draft only — not submitted
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
        description="Generate a publishable op-ed or letter to editor from this week's bill intelligence.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Generate op-ed anchored to top watch-list bill (default: csf + oped_mike_lewis + calmatters)
  python agents/opeds/oped_writer.py

  # Letter to editor format
  python agents/opeds/oped_writer.py --format letter

  # Target Wall Street Journal
  python agents/opeds/oped_writer.py --target wsj

  # Anchor to a specific bill
  python agents/opeds/oped_writer.py --bill AB1751

  # Add a news peg (for letter format — reference the article being responded to)
  python agents/opeds/oped_writer.py --format letter --peg "WSJ condo bust story, Jan 1"

  # Capitol Weekly insider piece
  python agents/opeds/oped_writer.py --target capitol-weekly --voice oped_mike_lewis

  # Preview output without writing files
  python agents/opeds/oped_writer.py --dry-run

  # List all configured clients
  python agents/opeds/oped_writer.py --list-clients

  # List voices available for a client
  python agents/opeds/oped_writer.py --client csf --list-voices
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
            "Defaults to 'oped_mike_lewis' for op-ed format. "
            "Run --list-voices to see all available voices."
        ),
    )
    p.add_argument(
        "--list-voices", action="store_true", default=False,
        help="Print all available voice names for the selected client and exit.",
    )
    p.add_argument(
        "--format", type=str, default="oped", choices=_VALID_FORMATS,
        help=(
            "Output format: 'oped' (700-900 words, default) or "
            "'letter' (250-450 word letter to editor)."
        ),
    )
    p.add_argument(
        "--target", type=str, default="calmatters", choices=_VALID_TARGETS,
        help=(
            "Target publication calibration: calmatters (default), wsj, "
            "capitol-weekly, or local."
        ),
    )
    p.add_argument(
        "--bill", type=str, default=None,
        help=(
            "Bill number to use as the anchor (e.g. AB1751). "
            "If not provided, the top watch-list bill is selected automatically."
        ),
    )
    p.add_argument(
        "--peg", type=str, default="",
        help=(
            "News peg description (e.g. 'WSJ condo bust story, Jan 1'). "
            "Used as the hook reference for letter format; context for op-ed format. "
            "Optional."
        ),
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
        "--dry-run", action="store_true", default=False,
        help="Generate content and print to stdout but do not write output files.",
    )
    return p.parse_args()


def main() -> None:
    _setup_logging()
    args = _parse_args()

    # ── --list-clients ────────────────────────────────────────────────────────
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

    # ── API key check ─────────────────────────────────────────────────────────
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set. Add it to .env or your environment.")
        sys.exit(1)

    # ── Load client config ────────────────────────────────────────────────────
    client_cfg  = _load_client(args.client)
    client_id   = client_cfg.get("slug", args.client)
    client_name = client_cfg.get("client_name", client_id)

    banner = f"{client_name} — Op-Ed Writer"
    print(f"\n  {banner}")
    print("  " + "─" * len(banner))

    # ── Resolve voice ─────────────────────────────────────────────────────────
    voices_dir = CLIENTS_DIR / client_id / "voices"
    voice_name = args.voice or _OPED_DEFAULT_VOICE

    # ── --list-voices ─────────────────────────────────────────────────────────
    if args.list_voices:
        voices = _list_voices(voices_dir)
        if voices:
            print(f"\n  Available voices for '{client_id}' (clients/{client_id}/voices/):\n")
            for v in voices:
                marker = " ← default (op-ed)" if v == _OPED_DEFAULT_VOICE else ""
                print(f"    {v}{marker}")
            print(f"\n  Usage: --voice <name>   e.g. --voice oped_mike_lewis\n")
        else:
            print(f"\n  No voice files found in {voices_dir}\n")
        sys.exit(0)

    voice_text = _load_voice(voice_name, voices_dir)
    if voice_text and not (voices_dir / f"{voice_name}.md").exists():
        voice_name = _OPED_DEFAULT_VOICE

    # ── Load bill data ────────────────────────────────────────────────────────
    bills_path = args.bills or BILLS_FILE
    log.info(f"→ Loading {bills_path.name}...")
    data  = json.loads(bills_path.read_text())
    bills = data["bills"]
    log.info(f"   {len(bills)} bills loaded")

    # ── Select bills ──────────────────────────────────────────────────────────
    log.info("→ Selecting bills for this week's op-ed...")
    bill_set = _select_bills(
        bills,
        lookback_days=args.lookback,
        max_watch=5,   # Pull more so we can pick anchor + supporting
        max_new=3,
    )

    # Resolve anchor bill — explicit --bill flag or top watch-list
    anchor_bill: dict | None = None
    if args.bill:
        key = args.bill.upper().replace(" ", "")
        anchor_bill = bills.get(key)
        if not anchor_bill:
            log.error(f"Bill '{args.bill}' not found in {bills_path.name}.")
            sys.exit(1)
        log.info(f"   Anchor bill (explicit): {anchor_bill['bill_number']}")
    elif bill_set["watch_list"]:
        anchor_bill = bill_set["watch_list"][0]
        log.info(f"   Anchor bill (auto-selected): {anchor_bill['bill_number']}")
    else:
        log.error("No high-risk bills found. Run housing_analyzer.py first.")
        sys.exit(1)

    # Supporting bills — up to 2 from watch list, excluding the anchor
    anchor_num = anchor_bill["bill_number"]
    supporting_bills = [
        b for b in bill_set["watch_list"]
        if b["bill_number"] != anchor_num
    ][:2]
    log.info(
        f"   Supporting bills: "
        f"{', '.join(b['bill_number'] for b in supporting_bills) or 'None'}"
    )

    # ── Load media digest (optional) ──────────────────────────────────────────
    media_digest = None
    if not args.no_media:
        media_digest = _load_media_digest()
        if media_digest:
            n = len(media_digest.get("articles", []))
            log.info(f"→ Media digest loaded: {n} articles")
        else:
            log.info("→ No media digest found — proceeding without news context")

    # ── Generate content via Claude ───────────────────────────────────────────
    anthropic_client = anthropic.Anthropic(api_key=api_key)
    content = _generate_content(
        anchor_bill      = anchor_bill,
        supporting_bills = supporting_bills,
        anthropic_client = anthropic_client,
        media_digest     = media_digest,
        voice_text       = voice_text,
        client_cfg       = client_cfg,
        format_type      = args.format,
        target           = args.target,
        news_peg         = args.peg,
    )
    log.info("   ✓ Draft generated")

    # ── Print summary ─────────────────────────────────────────────────────────
    headlines = content.get("headline_options", [])
    wc        = content.get("word_count", "?")
    print(f"\n  Anchor bill:  {anchor_bill['bill_number']} — {anchor_bill.get('title', '')[:55]}")
    print(f"  Format:       {args.format.upper()}")
    print(f"  Target:       {args.target}")
    print(f"  Voice:        {voice_name}")
    print(f"  Word count:   ~{wc}")
    print(f"\n  Headline options:")
    for i, h in enumerate(headlines, 1):
        print(f"    {i}. {h}")
    pitch = content.get("pitch_note", "")
    if pitch:
        print(f"\n  Pitch note:\n    {pitch[:160]}{'...' if len(pitch) > 160 else ''}")

    # ── Build output ──────────────────────────────────────────────────────────
    md_text   = _render_markdown(
        content, anchor_bill, supporting_bills,
        voice_name, args.format, args.target, client_cfg, args.peg,
    )
    html_text = _render_html(
        content, anchor_bill, supporting_bills,
        voice_name, args.format, args.target, client_cfg, args.peg,
    )

    if args.dry_run:
        print("\n  [--dry-run] Skipping file output. Draft preview:\n")
        print(md_text)
        return

    # ── Write files ───────────────────────────────────────────────────────────
    output_dir = PROJECT_ROOT / "outputs" / "clients" / client_id / "opeds"
    output_dir.mkdir(parents=True, exist_ok=True)

    iso_week      = date.today().strftime("%G-W%V")
    format_suffix = f"_{args.format}" if args.format != "oped" else ""
    target_suffix = f"_{args.target}" if args.target != "calmatters" else ""
    voice_suffix  = f"_{voice_name}" if voice_name != _OPED_DEFAULT_VOICE else ""
    stem          = f"oped_{iso_week}{format_suffix}{target_suffix}{voice_suffix}"

    md_path   = output_dir / f"{stem}.md"
    html_path = output_dir / f"{stem}.html"

    md_path.write_text(md_text,   encoding="utf-8")
    html_path.write_text(html_text, encoding="utf-8")

    print(f"\n  ✓ Markdown:  {md_path.relative_to(PROJECT_ROOT)}")
    print(f"  ✓ HTML:      {html_path.relative_to(PROJECT_ROOT)}")
    print()


if __name__ == "__main__":
    main()
