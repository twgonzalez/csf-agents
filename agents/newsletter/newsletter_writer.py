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
    bill_set: dict,
    anthropic_client: anthropic.Anthropic,
    client_cfg: dict,
    voice_text: str = "",
) -> dict:
    """Single Claude call returning all newsletter content as a structured dict."""
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
  [3] = Connects to the broader session pattern and the organization's mission

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
        model="claude-sonnet-4-5",
        max_tokens=3000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = message.content[0].text.strip()
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
    cta = content.get("call_to_action", {})
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

    # ── Generate content via Claude ─────────────────────────────────────────
    anthropic_client = anthropic.Anthropic(api_key=api_key)
    content = _generate_content(bill_set, anthropic_client, client_cfg, voice_text)
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
