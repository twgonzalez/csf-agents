#!/usr/bin/env python3
"""
newsletter_writer.py — Local Control Intelligence
California Stewardship Fund

Reads tracked_bills.json (output of bill_tracker + housing_analyzer), calls Claude
to generate narrative content, and renders a polished inline-styled HTML newsletter.

Pipeline position:
    bill_tracker.py → tracked_bills.json → housing_analyzer.py → newsletter_writer.py

Usage:
    .venv/bin/python agents/newsletter/newsletter_writer.py            # dry-run (default)
    .venv/bin/python agents/newsletter/newsletter_writer.py --dry-run
    .venv/bin/python agents/newsletter/newsletter_writer.py --bills path/to/bills.json
    .venv/bin/python agents/newsletter/newsletter_writer.py --lookback 7

Output:
    outputs/newsletter/newsletter_YYYY-WNN.html

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
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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
OUTPUT_DIR   = PROJECT_ROOT / "outputs" / "newsletter"

# ---------------------------------------------------------------------------
# Design constants
# Inline styles only — Gmail and Outlook strip <style> blocks entirely.
# ---------------------------------------------------------------------------

_SERIF  = "font-family:Georgia,'Times New Roman',Times,serif;"
_SANS   = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;"
_NAVY   = "#1a3a5c"   # primary heading / masthead
_RED    = "#b03a2e"   # highest-risk bill flag in watch list
_ORANGE = "#c0522a"   # reveal line in three-beat stacked headings (burnt orange)
_GOLD   = "#c9a227"   # section labels
_SAND   = "#faf8f4"   # page background
_INK    = "#1c1c1e"   # body text
_MID    = "#555555"   # muted / secondary text
_RULE   = "#ddd8ce"   # horizontal rule

# ---------------------------------------------------------------------------
# Bill selection
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
    max_watch: int = 5,
    max_new: int = 4,
) -> dict:
    """Return the three bill sets that drive newsletter content.

    watch_list       — bills with strong/moderate on 2+ of 4 criteria, ranked by
                       (hearing soon, risk count, strong count)
    new_bills        — bills first_seen within lookback window, ≥1 risk signal
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

        # Watch list: flagged on 2+ criteria
        if risk_count >= 2:
            watch_list.append((bill, risk_count, strong_count))

        # New bills: recently seen with at least one risk signal
        if bill.get("first_seen") and risk_count >= 1:
            try:
                first_seen = datetime.fromisoformat(bill["first_seen"]).date()
                if first_seen >= cutoff:
                    new_bills.append(bill)
            except ValueError:
                pass

        # Upcoming hearings
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
You are the editorial voice of the California Stewardship Fund — a policy organization \
whose core belief is that the best decisions come from people closest to them.

You write the weekly "Local Control Intelligence" newsletter. It reaches city council \
members, mayors, neighborhood advocacy leaders, and major donors who support protecting \
local government authority from state preemption of land-use decisions.

VOICE: Think sharp political journalism — confident, direct, insider tone. Not alarmist. \
Not academic. Write for a busy executive who will give you 90 seconds. \
Every sentence must earn its place. Name problems directly. Give the reader \
intelligence they can't get anywhere else.

FORMAT: The newsletter is prose-first. No bullet points in the main story. \
No report-style headers. Each story section uses a three-beat stacked heading: \
  line1  — the fact (2–5 words)
  line2  — the tension (2–5 words)
  reveal — the stakes (2–6 words, displayed in italic burnt orange)
Example: line1="Not zoning.", line2="Not design review.", reveal="your city's infrastructure budget."
The heading should make the reader want to read the paragraph below it.

FRAMING: Sacramento is advancing legislation that preempts local zoning authority, \
removes discretionary review, mandates development patterns, and shifts infrastructure \
costs to cities. The central message is: "This isn't about stopping housing — \
it's about who decides."

USE THESE TERMS: "preempts local authority", "removes discretionary review", \
"state mandate", "infrastructure cost-shifting", "local control"
AVOID: "controversial", "opponents say", "some argue", false balance

THREE AUDIENCES read this newsletter simultaneously:
- Local electeds: need intelligence to act on before hearings
- Neighborhood advocates: need coordinated framing and mobilization signal
- Major donors: need to understand why sustained investment matters, not just crisis response

Write so that all three feel the newsletter was written for them.\
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


def _generate_content(bill_set: dict, client: anthropic.Anthropic) -> dict:
    """Single Claude call returning all newsletter content as a structured dict.

    Returned keys:
        subject       — email subject line
        preview_text  — inbox preview snippet (~85 chars)
        dek           — standfirst paragraph (italic, above the story)
        story         — list of 4 dicts: {line1, line2, reveal, body}
        watch_items   — list of dicts: {bill_number, author, label, one_line, flag}
        call_to_action — dict: {heading, body}
        close          — dict: {heading, body}
    """
    watch_ctx = "\n\n".join(_build_bill_context(b) for b in bill_set["watch_list"])
    new_ctx   = "\n\n".join(_build_bill_context(b) for b in bill_set["new_bills"])

    user_prompt = f"""\
Here is this week's bill intelligence. Write the newsletter as specified.

== HIGH-RISK WATCH LIST (strong/moderate on 2+ criteria) ==
{watch_ctx}

== NEW BILLS THIS WEEK (recently tracked, at least 1 risk signal) ==
{new_ctx if new_ctx else "(No new high-risk bills this week)"}

---

Return a JSON object with exactly these keys:

"subject": A single compelling email subject line. \
Formula: [specific threat or number] + [implication or tension]. \
Make it the most alarming true thing from this issue. 8–14 words. \
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
Examples: "your city's infrastructure budget." / "who controls California's land." / "closed in this package."
  "body": One paragraph (3–5 sentences) of narrative prose. No bullet points. \
Weave specific bill numbers and risk details into the prose naturally.

The 4 paragraphs must tell a coherent story arc:
  [0] = What happened this week and why it matters
  [1] = How these specific bills work together mechanically
  [2] = The deeper threat (usually the fee / budget angle)
  [3] = Connects to the broader session pattern and CSF's mission

"watch_items": Array of objects for each watch-list bill, with:
  "bill_number": e.g. "AB1751"
  "author": last name only
  "label": bill number + short title (e.g. "AB1751 — Missing Middle Townhome Act")
  "one_line": One direct sentence. What it does + why it threatens local control. No hedging.
  "flag": true for the single highest-priority bill this week, false for all others.

"call_to_action": Object with:
  "heading": Short editorial statement (5–8 words). Frame the action, don't just describe it.
  "body": 2–3 sentences. One specific, time-bound action that serves all three audiences. \
Include the framing line: "This isn't about stopping housing — it's about who decides."

"close": Object with:
  "heading": 3–5 words. Tone: strategic confidence, not alarm.
  "body": 2–3 sentences. Connect this week to the longer arc. End with resolve, not anxiety.

Return ONLY valid JSON. No markdown fences. No commentary outside the JSON object.
"""

    log.info("→ Calling Claude to generate newsletter content...")
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=3000,
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
# HTML rendering helpers
# ---------------------------------------------------------------------------

def _rule(margin_top: int = 32, margin_bottom: int = 32) -> str:
    return (
        f'<table width="100%" cellpadding="0" cellspacing="0" '
        f'style="margin:{margin_top}px 0 {margin_bottom}px;">'
        f'<tr><td style="border-top:1px solid {_RULE};font-size:0;">&nbsp;</td></tr>'
        f'</table>'
    )


def _label(text: str) -> str:
    return (
        f'<div style="{_SERIF}color:{_GOLD};font-size:11px;font-weight:700;'
        f'font-style:italic;margin-bottom:14px;">'
        f'{text}</div>'
    )


def _preview_html(text: str) -> str:
    """Hidden div that controls the inbox preview snippet in Gmail / Apple Mail / Outlook.

    Without this, clients grab the first visible text — usually the masthead name
    and date, which tell the reader nothing. The &nbsp; padding prevents clients
    from pulling in additional body text after the preview snippet ends.
    """
    pad = "&nbsp;" * 90
    return (
        f'<div style="display:none;max-height:0;overflow:hidden;'
        f'font-size:1px;line-height:1px;color:{_SAND};mso-hide:all;">'
        f'{text}{pad}'
        f'</div>'
    )


def _dek_html(text: str) -> str:
    """Standfirst paragraph — above-the-fold hook, below the masthead rule.

    Answers "why does this matter to me right now?" before the first heading lands.
    Italic, slightly muted — sets the stage without stealing from the story.
    """
    return (
        f'<p style="{_SERIF}color:{_NAVY};font-size:17px;font-weight:400;'
        f'font-style:italic;line-height:1.65;margin:0 0 36px;padding:0;'
        f'opacity:0.85;">'
        f'{text}'
        f'</p>'
    )


def _graf3(line1: str, line2: str, reveal: str, body: str) -> str:
    """Three-beat stacked heading above a body paragraph.

    line1  — the fact      (bold navy)
    line2  — the tension   (bold navy)
    reveal — the stakes    (bold italic burnt orange — the gut-punch)
    body   — the paragraph (16px Georgia)

    Color arc: cool navy → warm orange. Stakes escalate visually as the eye
    descends through the three lines.
    """
    return (
        f'<p style="margin:0 0 32px;">'
        # Line 1 — the fact
        f'<span style="{_SERIF}display:block;color:{_NAVY};font-size:22px;'
        f'font-weight:700;line-height:1.15;margin-bottom:3px;">'
        f'{line1}</span>'
        # Line 2 — the tension
        f'<span style="{_SERIF}display:block;color:{_NAVY};font-size:22px;'
        f'font-weight:700;line-height:1.15;margin-bottom:11px;">'
        f'{line2}</span>'
        # Reveal — the stakes (italic, burnt orange)
        f'<span style="{_SERIF}display:block;color:{_ORANGE};font-size:22px;'
        f'font-weight:700;font-style:italic;line-height:1.15;margin-bottom:13px;">'
        f'{reveal}</span>'
        # Body
        f'<span style="{_SERIF}color:{_INK};font-size:16px;line-height:1.85;">'
        f'{body}</span>'
        f'</p>'
    )


def _watch_item(item: dict) -> str:
    flag  = item.get("flag", False)
    color = _RED if flag else _NAVY
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

def _build_html(content: dict, bill_set: dict, week_label: str) -> str:
    """Assemble the full inline-styled HTML email from generated content + bill data."""

    # Inject leginfo URLs into Claude's watch_items (Claude doesn't have these)
    all_bills  = bill_set["watch_list"] + bill_set["new_bills"]
    url_map    = {b["bill_number"]: b.get("text_url", "#") for b in all_bills}
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
        )
        for p in content.get("story", [])
    )

    # Render watch list rows
    watch_rows = "".join(_watch_item(item) for item in watch_items)

    # Render call to action
    cta = content.get("call_to_action", {})
    cta_html = (
        f'<p style="margin:0 0 20px;">'
        f'<span style="{_SERIF}display:block;color:{_NAVY};font-size:20px;'
        f'font-weight:700;line-height:1.25;margin-bottom:9px;">'
        f'{cta.get("heading", "")}</span>'
        f'<span style="{_SERIF}color:{_INK};font-size:16px;line-height:1.85;">'
        f'{cta.get("body", "")}</span>'
        f'</p>'
    )

    # Render close
    close = content.get("close", {})
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
    dek_para    = _dek_html(content.get("dek", ""))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Local Control Intelligence — {week_label}</title>
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
            <div style="{_SERIF}color:{_NAVY};font-size:26px;font-weight:700;
                        letter-spacing:-0.4px;line-height:1;">
              Local Control Intelligence
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
        <tr><td style="border-top:1px solid {_NAVY};font-size:0;">&nbsp;</td></tr>
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
      {_label("What to Watch")}
      <table width="100%" cellpadding="0" cellspacing="0">
        <tbody>{watch_rows}</tbody>
      </table>

      {_rule(28, 28)}

      <!-- CALL TO ACTION -->
      {_label("Before You Close This")}
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
        California Stewardship Fund
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

def _send_email(html: str, subject: str) -> bool:
    """Send the newsletter via Gmail SMTP with STARTTLS.

    Reads credentials from environment variables (set in .env or GitHub Secrets):
        EMAIL_USER            — SMTP username / sending address
        EMAIL_PASSWORD        — Gmail App Password (16-char; NOT your account password)
                                Generate at myaccount.google.com → Security → App passwords
        NEWSLETTER_RECIPIENTS — comma-separated list of subscriber addresses

    Returns True on success, False on any failure (logs error, does not raise).
    Safe to call from GitHub Actions — a send failure will not crash the pipeline.
    """
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

    # Plain text fallback for clients that don't render HTML
    plain = (
        f"Local Control Intelligence\n\n"
        f"{subject}\n\n"
        f"Open this email in a browser to read the full newsletter."
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Local Control Intelligence <{smtp_user}>"
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
        description="Generate the Local Control Intelligence newsletter.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Generate HTML only (dry-run — default, no email sent)
  python agents/newsletter/newsletter_writer.py

  # Generate and send to NEWSLETTER_RECIPIENTS
  python agents/newsletter/newsletter_writer.py --send

  # Override lookback window or bill data source
  python agents/newsletter/newsletter_writer.py --lookback 7
  python agents/newsletter/newsletter_writer.py --bills data/bills/tracked_bills.json
        """,
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

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set. Add it to .env or your environment.")
        sys.exit(1)

    print("\n  Local Control Intelligence — Newsletter Writer")
    print("  " + "─" * 47)

    # ── Load bill data ──────────────────────────────────────────────────────
    bills_path = args.bills or BILLS_FILE
    log.info(f"→ Loading {bills_path.name}...")
    data  = json.loads(bills_path.read_text())
    bills = data["bills"]
    log.info(f"   {len(bills)} bills loaded")

    # ── Select bills for this issue ─────────────────────────────────────────
    log.info("→ Selecting bills for this issue...")
    bill_set = _select_bills(bills, lookback_days=args.lookback)
    log.info(f"   Watch list:        {len(bill_set['watch_list'])} bills")
    log.info(f"   New this week:     {len(bill_set['new_bills'])} bills")
    log.info(f"   Upcoming hearings: {len(bill_set['upcoming_hearings'])}")

    # ── Generate content via Claude ─────────────────────────────────────────
    client  = anthropic.Anthropic(api_key=api_key)
    content = _generate_content(bill_set, client)
    log.info("   ✓ Content generated")

    # ── Print email metadata for clipboard use ──────────────────────────────
    subject      = content.get("subject", "Local Control Intelligence")
    preview_text = content.get("preview_text", "")
    print(f"\n  Subject:      {subject}")
    print(f"  Preview text: {preview_text[:85]}{'…' if len(preview_text) > 85 else ''}")

    # ── Render and write HTML ───────────────────────────────────────────────
    log.info("→ Rendering HTML...")
    week_label = "WEEK OF " + date.today().strftime("%B %-d, %Y").upper()
    html       = _build_html(content, bill_set, week_label)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    iso_week = date.today().strftime("%Y-W%W")
    out_path = OUTPUT_DIR / f"newsletter_{iso_week}.html"
    out_path.write_text(html, encoding="utf-8")

    print(f"\n  ✓ Written to: {out_path.relative_to(PROJECT_ROOT)}")

    # ── Send or report dry-run status ───────────────────────────────────────
    if args.send:
        ok = _send_email(html, subject)
        if not ok:
            sys.exit(1)
    else:
        print(f"\n  (Dry-run — HTML only. Use --send to email NEWSLETTER_RECIPIENTS.)")
        print(f"\n  Open in browser:\n    file://{out_path}\n")


if __name__ == "__main__":
    main()
