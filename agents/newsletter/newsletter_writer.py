#!/usr/bin/env python3
"""
newsletter_writer.py — Weekly Legislative Intelligence Newsletter
California Stewardship Fund

Reads tracked_bills.json (output of bill_tracker + housing_analyzer), calls Claude
to generate narrative content, and renders a polished inline-styled HTML newsletter.

Pipeline position:
    bill_tracker.py → tracked_bills.json → housing_analyzer.py → newsletter_writer.py

Client system:
    Each run targets a client from clients/<id>/client.yml.
    The client file defines brand identity (org, audience, colors, newsletter name).
    Voice files in clients/<id>/voices/ control tone and framing.

    Default client: clients/csf/  (California Stewardship Fund)
    Add a client:   create clients/<slug>/client.yml + clients/<slug>/voices/default.md
    Select client:  --client <slug>
    List clients:   --list-clients

Usage:
    .venv/bin/python agents/newsletter/newsletter_writer.py              # csf, dry-run
    .venv/bin/python agents/newsletter/newsletter_writer.py --client cma
    .venv/bin/python agents/newsletter/newsletter_writer.py --send       # send to recipients
    .venv/bin/python agents/newsletter/newsletter_writer.py --list-clients
    .venv/bin/python agents/newsletter/newsletter_writer.py --bills path/to/bills.json
    .venv/bin/python agents/newsletter/newsletter_writer.py --lookback 7

Output:
    outputs/clients/<slug>/newsletter/newsletter_YYYY-WNN.html

Requires:
    ANTHROPIC_API_KEY environment variable (or .env file at project root)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import sys
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# Load .env before importing anthropic so the key is available
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Ensure project root is on sys.path so intra-package imports work when the
# script is run directly (python agents/newsletter/newsletter_writer.py).
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env", override=True)

import anthropic

from agents.shared.client_utils import (
    CLIENTS_DIR, DEFAULT_CLIENT, DEFAULT_VOICE,
    _load_client, _list_clients, _load_voice, _list_voices,
)
from agents.shared.bill_utils import _CRIT_KEYS, _select_bills, _build_bill_context

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
DIGEST_FILE    = PROJECT_ROOT / "data" / "legislative" / "action_digest.json"
ARCHIVE_DIR    = PROJECT_ROOT / "docs" / "newsletters"
ARCHIVE_JSON   = ARCHIVE_DIR / "archive.json"
# CLIENTS_DIR, DEFAULT_CLIENT, DEFAULT_VOICE — imported from agents.shared.client_utils

# ---------------------------------------------------------------------------
# Design constants
# Inline styles only — Gmail and Outlook strip <style> blocks entirely.
# Brand colors (navy, gold) come from client config at runtime.
# ---------------------------------------------------------------------------

_SERIF  = "font-family:Georgia,'Times New Roman',Times,serif;"
_SANS   = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;"
_RED    = "#b03a2e"   # highest-risk bill flag in watch list
_ORANGE = "#c0522a"   # reveal line in three-beat stacked headings (burnt orange)
_SAND   = "#faf8f4"   # page background
_INK    = "#1c1c1e"   # body text
_MID    = "#555555"   # muted / secondary text
_RULE   = "#ddd8ce"   # horizontal rule




# ---------------------------------------------------------------------------
# Newsletter archive (docs/newsletters/ → GitHub Pages)
# ---------------------------------------------------------------------------

def _build_archive_index(entries: list[dict]) -> None:
    """
    Generate docs/newsletters/index.html from archive metadata.

    Organises issues by client (alphabetical), newest first within each.
    Designed for GitHub Pages hosting alongside the bill-tracker dashboard.
    """
    _SERIF = "font-family:Georgia,'Times New Roman',Times,serif;"
    _SANS  = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;"
    navy   = "#1a3a5c"
    gold   = "#c9a227"
    sand   = "#faf8f4"
    mid    = "#666666"
    rule   = "#ddd8ce"

    # Group entries by client_id, sorted newest first within each
    from itertools import groupby
    by_client: dict[str, list[dict]] = {}
    for entry in entries:
        cid = entry.get("client_id", "unknown")
        by_client.setdefault(cid, [])
        by_client[cid].append(entry)
    for issues in by_client.values():
        issues.sort(key=lambda e: e.get("date", ""), reverse=True)

    client_sections: list[str] = []
    for cid in sorted(by_client):
        issues      = by_client[cid]
        client_name = issues[0].get("client_name", cid)
        rows        = []
        for issue in issues:
            path  = issue.get("path", "#")
            week  = issue.get("week", "")
            dt    = issue.get("date", "")
            subj  = issue.get("subject", "(no subject)")
            short = subj[:95] + ("…" if len(subj) > 95 else "")
            rows.append(
                f"<tr>"
                f'<td style="padding:12px 14px 12px 0;border-bottom:1px solid {rule};'
                f'{_SANS}font-size:12px;color:{mid};white-space:nowrap;">{week}</td>'
                f'<td style="padding:12px 14px 12px 0;border-bottom:1px solid {rule};'
                f'{_SANS}font-size:12px;color:{mid};white-space:nowrap;">{dt}</td>'
                f'<td style="padding:12px 0;border-bottom:1px solid {rule};">'
                f'<a href="{path}" style="{_SERIF}font-size:14px;color:{navy};'
                f'text-decoration:none;line-height:1.5;">{short}</a></td>'
                f'<td style="padding:12px 0 12px 16px;border-bottom:1px solid {rule};'
                f'text-align:right;white-space:nowrap;">'
                f'<a href="{path}" style="{_SANS}font-size:11px;font-weight:700;'
                f'color:{gold};text-decoration:none;text-transform:uppercase;'
                f'letter-spacing:0.5px;">Read →</a></td>'
                f"</tr>"
            )
        rows_html = "\n        ".join(rows)
        client_sections.append(
            f'<div style="margin-bottom:40px;">'
            f'<div style="{_SERIF}color:{navy};font-size:13px;font-weight:700;'
            f'text-transform:uppercase;letter-spacing:1px;'
            f'border-bottom:2px solid {gold};padding-bottom:8px;margin-bottom:0;">'
            f'{client_name}</div>'
            f'<table width="100%" cellpadding="0" cellspacing="0">'
            f'<tbody>{rows_html}</tbody></table></div>'
        )

    content = "\n".join(client_sections) if client_sections else (
        f'<p style="{_SANS}color:{mid};font-size:14px;">No newsletters archived yet.</p>'
    )
    total = len(entries)

    index_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Newsletter Archive — CSF Legislative Intelligence</title>
</head>
<body style="margin:0;padding:0;background:{sand};">

<table width="100%" cellpadding="0" cellspacing="0"
       style="background:{navy};padding:32px 16px;">
<tr><td align="center">
  <table width="680" cellpadding="0" cellspacing="0" style="max-width:680px;width:100%;">
  <tr><td>
    <div style="{_SANS}color:{gold};font-size:11px;font-weight:700;
                text-transform:uppercase;letter-spacing:2px;margin-bottom:8px;">
      Legislative Intelligence Network
    </div>
    <div style="{_SERIF}color:#ffffff;font-size:28px;font-weight:700;
                line-height:1.2;margin-bottom:8px;">
      Newsletter Archive
    </div>
    <div style="{_SANS}color:rgba(255,255,255,0.6);font-size:13px;">
      {total} issue{"s" if total != 1 else ""} archived
      &nbsp;·&nbsp;
      <a href="../index.html" style="color:rgba(255,255,255,0.6);text-decoration:none;">
        ← Back to Dashboard
      </a>
    </div>
  </td></tr>
  </table>
</td></tr>
</table>

<table width="100%" cellpadding="0" cellspacing="0"
       style="background:{sand};padding:32px 16px 48px;">
<tr><td align="center">
  <table width="680" cellpadding="0" cellspacing="0" style="max-width:680px;width:100%;">
  <tr><td style="background:#ffffff;padding:36px 40px;">
    {content}
  </td></tr>
  <tr><td style="padding:20px 0;text-align:center;">
    <div style="{_SANS}color:#aaa;font-size:10px;
                text-transform:uppercase;letter-spacing:0.8px;line-height:2;">
      California Stewardship Fund
      &nbsp;·&nbsp;
      <a href="../index.html" style="color:#aaa;text-decoration:none;">Dashboard</a>
      &nbsp;·&nbsp;
      <a href="https://github.com/twgonzalez/csf-agents"
         style="color:#aaa;text-decoration:none;">GitHub</a>
    </div>
  </td></tr>
  </table>
</td></tr>
</table>

</body>
</html>"""

    (ARCHIVE_DIR / "index.html").write_text(index_html, encoding="utf-8")


def _archive_newsletter(
    html:        str,
    subject:     str,
    client_id:   str,
    client_name: str,
    week_str:    str,
    week_date:   str,
    filename:    str,
) -> None:
    """
    Archive a completed newsletter to docs/newsletters/ for GitHub Pages hosting.

    Copies the HTML file, updates docs/newsletters/archive.json, and regenerates
    docs/newsletters/index.html so past issues are browsable at:
        https://<user>.github.io/<repo>/newsletters/

    Silently skips if the docs/ directory doesn't exist (e.g., fresh checkouts).

    Args:
        html:        Full newsletter HTML string.
        subject:     Newsletter subject line (shown in archive index).
        client_id:   Client slug (e.g. "csf").
        client_name: Full client name (e.g. "California Stewardship Fund").
        week_str:    ISO week string (e.g. "2026-W09").
        week_date:   ISO date string for the week (e.g. "2026-03-01").
        filename:    Newsletter filename (e.g. "newsletter_2026-W09.html").
    """
    if not ARCHIVE_DIR.parent.exists():
        log.debug("Archive skipped — docs/ directory not found")
        return

    # Copy HTML to per-client archive directory
    client_dir = ARCHIVE_DIR / client_id
    client_dir.mkdir(parents=True, exist_ok=True)
    dest = client_dir / filename
    dest.write_text(html, encoding="utf-8")
    log.info(f"   Archived → {dest.relative_to(PROJECT_ROOT)}")

    # Load existing archive metadata
    entries: list[dict] = []
    if ARCHIVE_JSON.exists():
        try:
            entries = json.loads(ARCHIVE_JSON.read_text(encoding="utf-8"))
        except Exception:
            entries = []

    # Upsert: remove any existing entry for the same week + client, then append
    entries = [
        e for e in entries
        if not (e.get("week") == week_str and e.get("client_id") == client_id)
    ]
    entries.append({
        "week":        week_str,
        "date":        week_date,
        "client_id":   client_id,
        "client_name": client_name,
        "subject":     subject,
        "filename":    filename,
        "path":        f"{client_id}/{filename}",
    })
    entries.sort(key=lambda e: (e.get("date", ""), e.get("week", "")), reverse=True)

    # Write updated archive JSON
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_JSON.write_text(
        json.dumps(entries, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Regenerate the archive index page
    _build_archive_index(entries)
    log.info(f"   Archive index → {(ARCHIVE_DIR / 'index.html').relative_to(PROJECT_ROOT)}")


# ---------------------------------------------------------------------------
# Action digest loader (legislative_intel.py output)
# ---------------------------------------------------------------------------

def _load_digest() -> dict:
    """
    Load data/legislative/action_digest.json produced by legislative_intel.py.

    Returns an empty dict if the file is absent — the newsletter writer falls
    back to its pre-digest behavior, so the pipeline is backward-compatible.
    """
    if not DIGEST_FILE.exists():
        log.info("   No action_digest.json found — running without legislative intelligence layer")
        return {}
    try:
        digest = json.loads(DIGEST_FILE.read_text(encoding="utf-8"))
        log.info(
            f"   Digest loaded: {digest.get('week', '?')} | "
            f"urgent={len(digest.get('urgent', []))} | "
            f"moving={len(digest.get('moving', []))} | "
            f"amended={len(digest.get('amended', []))} | "
            f"spot_bills={len(digest.get('spot_bills', []))}"
        )
        return digest
    except Exception as exc:
        log.warning(f"   Could not load action_digest.json: {exc} — proceeding without digest")
        return {}


def _build_digest_context(digest: dict) -> str:
    """
    Format the action_digest.json into a Claude-readable context block.

    Injected as the first section of the user prompt so Claude anchors the
    newsletter story to actual legislative activity rather than static bill lists.

    Only includes non-empty buckets — keeps the prompt concise.
    """
    if not digest:
        return ""

    lines: list[str] = [
        "== THIS WEEK'S LEGISLATIVE ACTIVITY (factual spine — anchor your story here) =="
    ]

    # Week summary (Claude-written in legislative_intel.py)
    week_summary = digest.get("week_summary", "").strip()
    if week_summary:
        lines += ["", "WHAT HAPPENED THIS WEEK:", week_summary]

    # Urgent hearings — only high-risk bills (2+ criteria) get prominent billing;
    # lower-risk bills with hearings are noted briefly.
    urgent = digest.get("urgent", [])
    high_risk_urgent = [u for u in urgent if u.get("risk_count", 0) >= 2]
    low_risk_urgent  = [u for u in urgent if u.get("risk_count", 0) < 2]

    if high_risk_urgent:
        lines += ["", f"URGENT HEARINGS — HIGH-RISK BILLS (within {len(urgent)} days window):"]
        for u in high_risk_urgent[:6]:
            wl = " [STAFF WATCHLIST]" if u.get("watchlist") else ""
            lines.append(
                f"  • {u['bill_number']}{wl}: {u['title'][:65]}"
            )
            lines.append(
                f"    Eligible: {u['eligible_date']} ({u['days_until']} days) | "
                f"risk criteria: {u['risk_count']}/4"
            )
            committees = u.get("committees", [])
            if committees:
                lines.append(f"    Committee(s): {', '.join(committees[:2])}")
    elif urgent:
        lines += [
            "",
            f"UPCOMING HEARINGS: {len(urgent)} bills eligible within {len(urgent)} days "
            "(none rated high-risk this window).",
        ]

    if low_risk_urgent and high_risk_urgent:
        lines += [
            f"  (plus {len(low_risk_urgent)} lower-risk bills with upcoming eligibility dates)",
        ]

    # Moving bills
    moving = digest.get("moving", [])
    if moving:
        lines += ["", "MOVING BILLS (advanced a legislative stage this week):"]
        for m in moving[:6]:
            wl = " [STAFF WATCHLIST]" if m.get("watchlist") else ""
            stage = m.get("current_stage", "unknown").replace("_", " ")
            lines.append(f"  • {m['bill_number']}{wl}: {m['title'][:65]}")
            lines.append(f"    Stage: {stage} | risk criteria: {m['risk_count']}/4")
            for adv in m.get("advance_actions", [])[:1]:
                lines.append(f"    [{adv['date']}] {adv['description'][:100]}")

    # Amended bills
    amended = digest.get("amended", [])
    if amended:
        lines += ["", "AMENDED BILLS (received author's amendments this week):"]
        for am in amended[:4]:
            wl = " [STAFF WATCHLIST]" if am.get("watchlist") else ""
            lines.append(f"  • {am['bill_number']}{wl}: {am['title'][:65]}")
            lines.append(f"    [{am['amendment_date']}] {am['amendment_description'][:120]}")

    # Gut-and-amend alerts
    gut = digest.get("gut_and_amend", [])
    if gut:
        lines += ["", "⚠️  GUT-AND-AMEND ALERTS (entire bill content may have been replaced):"]
        for g in gut:
            lines.append(f"  • {g['bill_number']}: {g['why']}")

    # Spot bill alerts
    spot = digest.get("spot_bills", [])
    if spot:
        lines += [
            "",
            f"SPOT BILL ALERT ({len(spot)} placeholder bills — substantive content could be "
            "dropped in before their hearing eligibility dates):",
        ]
        for s in spot[:5]:
            lines.append(f"  • {s['bill_number']}: {s['why']}")

    lines += ["", "=" * 68]
    return "\n".join(lines)


def _build_anti_repetition_block(digest: dict) -> str:
    """
    Build the anti-repetition instruction block from the last issue's story beats.

    Injected at the end of the user prompt so Claude actively avoids reusing
    the same structural framings as last week.
    """
    last_issue = digest.get("last_issue", {})
    if not last_issue:
        return ""

    beats  = last_issue.get("story_beats", [])
    subj   = last_issue.get("subject", "")
    source = last_issue.get("source_file", "last week")

    if not beats and not subj:
        return ""

    lines = [
        "",
        "== ANTI-REPETITION: DO NOT REPEAT LAST WEEK'S FRAMINGS ==",
        f"(Source: {source})",
    ]
    if subj:
        lines.append(f"Last subject line: \"{subj}\"")
    if beats:
        lines.append("Last week's three-beat headings (avoid all of these patterns):")
        for b in beats:
            lines.append(f"  - \"{b}\"")
    lines += [
        "",
        "REQUIREMENT: The story arc headings (line1/line2/reveal) for this week MUST be "
        "structurally and factually different from those above. Do not reuse the same "
        "numerical framing, the same first word, or the same reveal angle.",
        "Anchor paragraph [0] in the WEEK SUMMARY above — not in a pattern used last week.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude content generation
# ---------------------------------------------------------------------------

# Structural format rules — newsletter layout, JSON schema. Never changes.
_NEWSLETTER_FORMAT = """\
FORMAT: The newsletter is prose-first. No bullet points in the main story. \
No report-style headers. Each story section uses a three-beat stacked heading: \
  line1  — the fact (2–5 words)
  line2  — the tension (2–5 words)
  reveal — the stakes (2–6 words, displayed in italic burnt orange)
Example: line1="Not zoning.", line2="Not design review.", reveal="your city's infrastructure budget."
The heading should make the reader want to read the paragraph below it.

USE THESE TERMS: "preempts local authority", "removes discretionary review", \
"state mandate", "infrastructure cost-shifting", "local control"
AVOID: "controversial", "opponents say", "some argue", false balance\
"""


def _build_system_prompt(client: dict, voice_text: str = "") -> str:
    """Build the system prompt from client config + voice file.

    Client config provides: org identity, audience, newsletter name.
    Voice file adds: tone, framing, content pattern.
    Newsletter format rules are structural and stay hardcoded.
    """
    org_name       = client["client_name"]
    org_desc       = client["identity"]["org_description"].strip()
    audience       = client["identity"]["audience"].strip()
    newsletter_name = client.get("newsletter", {}).get("name", f"{org_name} Legislative Intelligence")

    base = (
        f"You are the editorial voice of {org_name} — {org_desc}\n\n"
        f"You write the weekly \"{newsletter_name}\" newsletter. It reaches {audience}\n\n"
        f"VOICE: Think sharp political journalism — confident, direct, insider tone. "
        f"Not alarmist. Not academic. Write for a busy executive who will give you 90 seconds. "
        f"Every sentence must earn its place. Name problems directly. Give the reader "
        f"intelligence they can't get anywhere else.\n\n"
        f"{_NEWSLETTER_FORMAT}"
    )

    if voice_text:
        return f"{base}\n\n---\n\n## VOICE & TONE\n\n{voice_text}"
    return base



def _generate_content(
    bill_set:         dict,
    anthropic_client: anthropic.Anthropic,
    client_cfg:       dict,
    voice_text:       str  = "",
    digest:           dict = None,
) -> dict:
    """Single Claude call returning all newsletter content as a structured dict.

    Args:
        bill_set:         Bills selected by _select_bills() — watch_list, new_bills, etc.
        anthropic_client: Anthropic API client.
        client_cfg:       Client brand + identity config.
        voice_text:       Voice file content for tone/framing.
        digest:           Action digest from legislative_intel.py (optional — falls back
                          gracefully to pre-digest behavior if None or empty).
    """
    if digest is None:
        digest = {}

    watch_ctx     = "\n\n".join(_build_bill_context(b) for b in bill_set["watch_list"])
    new_ctx       = "\n\n".join(_build_bill_context(b) for b in bill_set["new_bills"])
    watchlist_ctx = "\n\n".join(
        _build_bill_context(b)
        + (f"\nStaff note: {b['watchlist_note']}" if b.get("watchlist_note") else "")
        for b in bill_set.get("watchlist_bills", [])
    )

    # Build digest and anti-repetition context blocks (empty strings if no digest)
    digest_ctx      = _build_digest_context(digest)
    anti_rep_block  = _build_anti_repetition_block(digest)

    # Story arc guidance — updated when digest is present to anchor [0] on real activity
    arc_guidance = (
        "The 4 paragraphs must tell a coherent story arc:\n"
        "  [0] = START HERE: use the WEEK SUMMARY above as your factual anchor. What actually\n"
        "        happened in the legislature this week + why it matters RIGHT NOW to your reader.\n"
        "        Lead with the most urgent concrete fact — a hearing date, a bill that passed,\n"
        "        an imminent eligibility deadline. Not a general preemption recap.\n"
        "  [1] = How the specific bills listed above work together mechanically\n"
        "  [2] = The deeper threat (usually the fee / budget / fiscal authority angle)\n"
        "  [3] = Connects to the broader session pattern and the organization's mission"
    ) if digest else (
        "The 4 paragraphs must tell a coherent story arc:\n"
        "  [0] = What happened this week and why it matters\n"
        "  [1] = How these specific bills work together mechanically\n"
        "  [2] = The deeper threat (usually the fee / budget angle)\n"
        "  [3] = Connects to the broader session pattern and the organization's mission"
    )

    # Digest section (injected first, before bill lists, so Claude uses it as the spine)
    digest_section = (
        f"{digest_ctx}\n\n" if digest_ctx else ""
    )

    user_prompt = f"""\
Here is this week's legislative intelligence. Write the newsletter as specified.

{digest_section}\
== HIGH-RISK WATCH LIST (strong/moderate on 2+ criteria) ==
{watch_ctx if watch_ctx else "(No bills currently scored high-risk)"}

== NEW BILLS THIS WEEK (recently tracked, at least 1 risk signal) ==
{new_ctx if new_ctx else "(No new high-risk bills this week)"}

== STAFF WATCHLIST (staff-curated bills — bypass discovery filters) ==
These are two-year bills and staff-identified bills that may not appear above because \
they lack AI risk scores. Weave them into the narrative where substantively relevant, \
especially when they reinforce the broader session pattern.
{watchlist_ctx if watchlist_ctx else "(No staff watchlist bills this week)"}
{anti_rep_block}
---

Return a JSON object with exactly these keys:

"subject": A single compelling email subject line. \
Formula: [specific threat or number] + [implication or tension]. \
Make it the most alarming true thing from this issue. 8–14 words. \
Ground it in this week's concrete activity (hearing date, bill passage, amendment). \
Example: "Sacramento just introduced its opening argument. Your city is the rebuttal."

"preview_text": ~85 characters shown in Gmail/Apple Mail after the subject. \
Complement — don't repeat — the subject. Create curiosity about what's inside. \
Example: "Five bills. Three share the same objective. Once you see the pattern, you can't unsee it."

"dek": A 2–3 sentence standfirst paragraph. Answers "why does this matter to me right now?" \
Sets the stakes before a single heading is read. Italic editorial voice. Not a summary — a hook. \
Must compel the reader to continue.

"story": An array of exactly 4 objects, each with:
  "line1": First short declarative (2–5 words). The fact.
  "line2": Second short declarative (2–5 words). The tension.
  "reveal": Third line (2–6 words). The stakes — what it all means. \
This is displayed in italic burnt orange. Make it the gut-punch. \
Examples: "your city's infrastructure budget." / "who controls California's land." / "six days."
  "body": One paragraph (3–5 sentences) of narrative prose. No bullet points. \
Weave specific bill numbers and risk details into the prose naturally.

{arc_guidance}

"watch_items": Array of objects for each watch-list bill, with:
  "bill_number": e.g. "AB1751"
  "author": last name only
  "label": bill number + short title (e.g. "AB1751 — Missing Middle Townhome Act")
  "one_line": One direct sentence. What it does + why it threatens local control. No hedging.
  "flag": true for the single highest-priority bill this week, false for all others.

"call_to_action": Object with:
  "heading": Short editorial statement (5–8 words). Frame the action, don't just describe it.
  "body": 2–3 sentences. One specific, time-bound action that serves all audiences. \
Include the framing line: "This isn't about stopping housing — it's about who decides."

"close": Object with:
  "heading": 3–5 words. Tone: strategic confidence, not alarm.
  "body": 2–3 sentences. Connect this week to the longer arc. End with resolve, not anxiety.

Return ONLY valid JSON. No markdown fences. No commentary outside the JSON object.
"""

    system_prompt = _build_system_prompt(client_cfg, voice_text)
    log.info("→ Calling Claude to generate newsletter content...")
    message = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4500,   # increased from 3000 — digest context grows the response
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = message.content[0].text.strip()
    # Strip markdown fences (Claude sometimes wraps JSON in ```json ... ```)
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        raw = raw.rsplit("```", 1)[0].strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error(
            f"Claude returned invalid JSON (stop_reason={message.stop_reason}): {exc}\n"
            f"Raw response (first 500 chars): {raw[:500]}"
        )
        raise


# ---------------------------------------------------------------------------
# HTML rendering helpers
# ---------------------------------------------------------------------------

def _rule(margin_top: int = 32, margin_bottom: int = 32) -> str:
    return (
        f'<table width="100%" cellpadding="0" cellspacing="0" '
        f'style="margin:{margin_top}px 0 {margin_bottom}px;">'
        f'<tr><td style="border-top:1px solid {_RULE};font-size:0;">&nbsp;</td></tr>'
        f'</table>'
    )


def _label(text: str, gold: str) -> str:
    return (
        f'<div style="{_SERIF}color:{gold};font-size:11px;font-weight:700;'
        f'font-style:italic;margin-bottom:14px;">'
        f'{text}</div>'
    )


def _preview_html(text: str) -> str:
    """Hidden div that controls the inbox preview snippet in Gmail / Apple Mail / Outlook."""
    pad = "&nbsp;" * 90
    return (
        f'<div style="display:none;max-height:0;overflow:hidden;'
        f'font-size:1px;line-height:1px;color:{_SAND};mso-hide:all;">'
        f'{text}{pad}'
        f'</div>'
    )


def _dek_html(text: str, navy: str) -> str:
    """Standfirst paragraph — above-the-fold hook, below the masthead rule."""
    return (
        f'<p style="{_SERIF}color:{navy};font-size:17px;font-weight:400;'
        f'font-style:italic;line-height:1.65;margin:0 0 36px;padding:0;'
        f'opacity:0.85;">'
        f'{text}'
        f'</p>'
    )


def _graf3(line1: str, line2: str, reveal: str, body: str, navy: str) -> str:
    """Three-beat stacked heading above a body paragraph."""
    return (
        f'<p style="margin:0 0 32px;">'
        f'<span style="{_SERIF}display:block;color:{navy};font-size:22px;'
        f'font-weight:700;line-height:1.15;margin-bottom:3px;">'
        f'{line1}</span>'
        f'<span style="{_SERIF}display:block;color:{navy};font-size:22px;'
        f'font-weight:700;line-height:1.15;margin-bottom:11px;">'
        f'{line2}</span>'
        f'<span style="{_SERIF}display:block;color:{_ORANGE};font-size:22px;'
        f'font-weight:700;font-style:italic;line-height:1.15;margin-bottom:13px;">'
        f'{reveal}</span>'
        f'<span style="{_SERIF}color:{_INK};font-size:16px;line-height:1.85;">'
        f'{body}</span>'
        f'</p>'
    )


def _watch_item(item: dict, navy: str) -> str:
    flag  = item.get("flag", False)
    color = _RED if flag else navy
    url   = item.get("url", "#")
    new   = item.get("new", False)

    new_badge = (
        f'<span style="{_SANS}font-size:9px;font-weight:700;color:#d35400;'
        f'text-transform:uppercase;letter-spacing:1px;margin-left:8px;'
        f'background:#fff4e6;padding:2px 6px;border-radius:3px;">New</span>'
        if new else ""
    )

    return (
        f'<tr>'
        f'<td style="padding:13px 0;border-bottom:1px solid {_RULE};">'
        f'<div style="{_SERIF}font-weight:700;font-size:14px;color:{color};'
        f'line-height:1.3;margin-bottom:4px;">'
        f'<a href="{url}" style="color:{color};text-decoration:none;">'
        f'{item.get("label", item.get("bill_number", ""))}</a>'
        f'{new_badge}</div>'
        f'<div style="{_SERIF}font-size:11px;color:#999;font-style:italic;margin-bottom:5px;">'
        f'{item.get("author", "")}</div>'
        f'<div style="{_SERIF}font-size:14px;color:{_MID};line-height:1.6;">'
        f'{item.get("one_line", item.get("line", ""))}</div>'
        f'</td>'
        f'</tr>'
    )


# ---------------------------------------------------------------------------
# HTML assembler
# ---------------------------------------------------------------------------

def _build_html(content: dict, bill_set: dict, week_label: str, client_cfg: dict) -> str:
    """Assemble the full inline-styled HTML email from generated content + bill data."""
    navy            = client_cfg.get("colors", {}).get("background", {}).get("hex", "#1a3a5c")
    gold            = client_cfg.get("colors", {}).get("accent",     {}).get("hex", "#c9a227")
    newsletter_name = client_cfg.get("newsletter", {}).get("name", "Legislative Intelligence")
    client_name     = client_cfg.get("client_name", "")
    footer_label    = client_cfg.get("proof_sheet", {}).get("label", client_name)

    # Inject leginfo URLs into Claude's watch_items
    all_bills   = bill_set["watch_list"] + bill_set["new_bills"]
    url_map     = {b["bill_number"]: b.get("text_url", "#") for b in all_bills}
    watch_items = content.get("watch_items", [])
    for item in watch_items:
        if not item.get("url"):
            item["url"] = url_map.get(item.get("bill_number", ""), "#")

    # Render story paragraphs
    story_html = "".join(
        _graf3(
            p.get("line1",  ""),
            p.get("line2",  ""),
            p.get("reveal", ""),
            p.get("body",   ""),
            navy,
        )
        for p in content.get("story", [])
    )

    # Render watch list rows
    watch_rows = "".join(_watch_item(item, navy) for item in watch_items)

    # Render call to action
    # Guard: Claude occasionally returns a list instead of a dict — take first element.
    cta = content.get("call_to_action", {})
    if isinstance(cta, list):
        cta = cta[0] if cta else {}
    cta_html = (
        f'<p style="margin:0 0 20px;">'
        f'<span style="{_SERIF}display:block;color:{navy};font-size:20px;'
        f'font-weight:700;line-height:1.25;margin-bottom:9px;">'
        f'{cta.get("heading", "")}</span>'
        f'<span style="{_SERIF}color:{_INK};font-size:16px;line-height:1.85;">'
        f'{cta.get("body", "")}</span>'
        f'</p>'
    )

    # Render close
    close = content.get("close", {})
    if isinstance(close, list):
        close = close[0] if close else {}
    close_html = (
        f'<p style="margin:0;">'
        f'<span style="{_SERIF}display:block;color:{_MID};font-size:17px;'
        f'font-weight:700;line-height:1.25;margin-bottom:8px;">'
        f'{close.get("heading", "")}</span>'
        f'<span style="{_SERIF}color:{_MID};font-size:14px;line-height:1.8;'
        f'font-style:italic;">{close.get("body", "")}</span>'
        f'</p>'
    )

    preview_div = _preview_html(content.get("preview_text", ""))
    dek_para    = _dek_html(content.get("dek", ""), navy)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{newsletter_name} — {week_label}</title>
</head>
<body style="margin:0;padding:0;background:{_SAND};">
{preview_div}
<!-- Outer wrapper -->
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:{_SAND};padding:40px 16px;">
<tr><td align="center">

<!-- Inner container: 600px — standard email width -->
<table width="600" cellpadding="0" cellspacing="0"
       style="max-width:600px;width:100%;">

  <!-- MASTHEAD -->
  <tr>
    <td style="padding-bottom:28px;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td>
            <div style="{_SERIF}color:{navy};font-size:26px;font-weight:700;
                        letter-spacing:-0.4px;line-height:1;">
              {newsletter_name}
            </div>
          </td>
          <td align="right" valign="middle">
            <div style="{_SERIF}color:{_MID};font-size:12px;font-style:italic;">
              {week_label}
            </div>
          </td>
        </tr>
      </table>
      <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:12px;">
        <tr><td style="border-top:1px solid {navy};font-size:0;">&nbsp;</td></tr>
      </table>
    </td>
  </tr>

  <!-- BODY -->
  <tr>
    <td style="background:#ffffff;padding:32px 40px;">

      <!-- DEK: above-the-fold hook, answers "why read this now?" -->
      {dek_para}

      <!-- STORY: 4 paragraphs, each with three-beat stacked heading -->
      {story_html}

      {_rule(28, 28)}

      <!-- WATCH LIST -->
      {_label("What to Watch", gold)}
      <table width="100%" cellpadding="0" cellspacing="0">
        <tbody>{watch_rows}</tbody>
      </table>

      {_rule(28, 28)}

      <!-- CALL TO ACTION -->
      {_label("Before You Close This", gold)}
      {cta_html}

      {_rule(28, 24)}

      <!-- CLOSE -->
      {close_html}

    </td>
  </tr>

  <!-- FOOTER -->
  <tr>
    <td style="padding:20px 0;text-align:center;">
      <div style="{_SANS}color:#aaa;font-size:10px;line-height:1.8;
                  text-transform:uppercase;letter-spacing:0.8px;">
        {footer_label}
        &nbsp;·&nbsp;
        <a href="https://twgonzalez.github.io/csf-agents/"
           style="color:#aaa;text-decoration:none;">Full Dashboard</a>
        &nbsp;·&nbsp;
        <a href="#" style="color:#aaa;text-decoration:none;">Unsubscribe</a>
      </div>
    </td>
  </tr>

</table>
</td></tr>
</table>

</body>
</html>"""


# ---------------------------------------------------------------------------
# Email delivery
# ---------------------------------------------------------------------------

def _send_email(html: str, subject: str, newsletter_name: str = "Newsletter") -> bool:
    """Send the newsletter via Gmail SMTP with STARTTLS."""
    smtp_user      = os.environ.get("EMAIL_USER",            "").strip()
    smtp_pass      = os.environ.get("EMAIL_PASSWORD",        "").strip()
    recipients_raw = os.environ.get("NEWSLETTER_RECIPIENTS", "").strip()

    if not smtp_user:
        log.error("Email not sent: EMAIL_USER not set.")
        return False
    if not smtp_pass:
        log.error("Email not sent: EMAIL_PASSWORD not set.")
        return False
    if not recipients_raw:
        log.error("Email not sent: NEWSLETTER_RECIPIENTS not set.")
        return False

    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]

    plain = (
        f"{newsletter_name}\n\n"
        f"{subject}\n\n"
        f"Open this email in a browser to read the full newsletter."
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{newsletter_name} <{smtp_user}>"
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html,  "html",  "utf-8"))

    smtp_host = "smtp.gmail.com"
    smtp_port = 587

    try:
        log.info(f"→ Sending to {len(recipients)} recipient(s) via {smtp_host}:{smtp_port}...")
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, recipients, msg.as_bytes())
        log.info(f"   ✓ Sent to: {', '.join(recipients)}")
        return True
    except smtplib.SMTPAuthenticationError:
        log.error(
            "Authentication failed. For Gmail, use an App Password — "
            "not your account password. "
            "Generate one at: myaccount.google.com → Security → App passwords"
        )
        return False
    except Exception as exc:
        log.error(f"Failed to send email: {exc}")
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate the weekly legislative intelligence newsletter for a configured client.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Generate HTML only for CSF (dry-run — default)
  python agents/newsletter/newsletter_writer.py

  # Generate for a specific client
  python agents/newsletter/newsletter_writer.py --client cma

  # Generate and send to NEWSLETTER_RECIPIENTS
  python agents/newsletter/newsletter_writer.py --client csf --send

  # List all configured clients
  python agents/newsletter/newsletter_writer.py --list-clients

  # List voices available for a client
  python agents/newsletter/newsletter_writer.py --client csf --list-voices

  # Override lookback window or bill data source
  python agents/newsletter/newsletter_writer.py --lookback 7
  python agents/newsletter/newsletter_writer.py --bills data/bills/tracked_bills.json
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
            "Defaults to the client's default_voice setting."
        ),
    )
    p.add_argument(
        "--list-voices", action="store_true", default=False,
        help="Print all available voice names for the selected client and exit.",
    )
    p.add_argument(
        "--send", action="store_true", default=False,
        help=(
            "Send the newsletter to NEWSLETTER_RECIPIENTS via Gmail SMTP. "
            "Requires EMAIL_USER, EMAIL_PASSWORD, and NEWSLETTER_RECIPIENTS "
            "environment variables. Without this flag, generates HTML only."
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
    client_cfg      = _load_client(args.client)
    client_id       = client_cfg.get("slug", args.client)
    client_name     = client_cfg.get("client_name", client_id)
    newsletter_name = client_cfg.get("newsletter", {}).get("name", f"{client_name} Legislative Intelligence")

    print(f"\n  {newsletter_name} — Newsletter Writer")
    print("  " + "─" * (len(newsletter_name) + 20))

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

    # ── Load bill data ──────────────────────────────────────────────────────
    bills_path = args.bills or BILLS_FILE
    log.info(f"→ Loading {bills_path.name}...")
    data  = json.loads(bills_path.read_text())
    bills = data["bills"]
    log.info(f"   {len(bills)} bills loaded")

    # ── Select bills for this issue ─────────────────────────────────────────
    log.info("→ Selecting bills for this issue...")
    bill_set = _select_bills(bills, lookback_days=args.lookback, max_watch=5, max_new=4)
    log.info(f"   Watch list:        {len(bill_set['watch_list'])} bills")
    log.info(f"   New this week:     {len(bill_set['new_bills'])} bills")
    log.info(f"   Upcoming hearings: {len(bill_set['upcoming_hearings'])}")
    log.info(f"   Staff watchlist:   {len(bill_set.get('watchlist_bills', []))} bills")

    # ── Load legislative intelligence digest (optional — graceful fallback) ─
    log.info("→ Loading legislative intelligence digest...")
    digest = _load_digest()

    # ── Generate content via Claude ─────────────────────────────────────────
    anthropic_client = anthropic.Anthropic(api_key=api_key)
    content = _generate_content(bill_set, anthropic_client, client_cfg, voice_text, digest)
    log.info("   ✓ Content generated")

    # ── Print email metadata ─────────────────────────────────────────────────
    subject      = content.get("subject", newsletter_name)
    preview_text = content.get("preview_text", "")
    print(f"\n  Client:       {client_name}")
    print(f"  Subject:      {subject}")
    print(f"  Preview text: {preview_text[:85]}{'…' if len(preview_text) > 85 else ''}")

    # ── Render and write HTML ───────────────────────────────────────────────
    log.info("→ Rendering HTML...")
    week_label = "WEEK OF " + date.today().strftime("%B %-d, %Y").upper()
    html       = _build_html(content, bill_set, week_label, client_cfg)

    output_dir = PROJECT_ROOT / "outputs" / "clients" / client_id / "newsletter"
    output_dir.mkdir(parents=True, exist_ok=True)
    iso_week = date.today().strftime("%G-W%V")
    out_path = output_dir / f"newsletter_{iso_week}.html"
    out_path.write_text(html, encoding="utf-8")

    print(f"\n  ✓ Written to: {out_path.relative_to(PROJECT_ROOT)}")

    # ── Archive to docs/newsletters/ (GitHub Pages) ─────────────────────────
    log.info("→ Archiving to docs/newsletters/...")
    week_date = date.today().isoformat()
    _archive_newsletter(
        html=html,
        subject=subject,
        client_id=client_id,
        client_name=client_name,
        week_str=iso_week,
        week_date=week_date,
        filename=out_path.name,
    )

    # ── Send or report dry-run status ───────────────────────────────────────
    if args.send:
        ok = _send_email(html, subject, newsletter_name)
        if not ok:
            sys.exit(1)
    else:
        print(f"\n  (Dry-run — HTML only. Use --send to email NEWSLETTER_RECIPIENTS.)")
        print(f"\n  Open in browser:\n    file://{out_path}\n")


if __name__ == "__main__":
    main()
