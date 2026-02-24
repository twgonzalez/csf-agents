"""
Email Sender for the CSF Legislative Bill Tracker
===================================================
Converts bill data directly to inline-styled HTML and sends via SMTP.

Why build HTML directly (instead of converting the markdown report):
  - Email clients strip <style> blocks; every element needs inline styles
  - The email layout differs from the markdown report (wider content, CTAs)
  - More control over rendering across Gmail, Outlook, and Apple Mail

Usage (called from bill_tracker.py):
    from agents.legislative.email_sender import build_and_send_email

    build_and_send_email(
        new_bills=new_bills,
        changed_bills=changed_bills,
        all_bills=all_bills,
        config=self.config,
        logger=self.logger,
    )

Environment variables required:
    EMAIL_USER        Sending address (e.g. you@gmail.com)
    EMAIL_PASSWORD    SMTP password or Gmail App Password

Gmail setup:
    1. Enable 2-Step Verification at myaccount.google.com
    2. Go to Security → App passwords
    3. Generate a password for "Mail"
    4. Use that 16-character password as EMAIL_PASSWORD
"""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_and_send_email(
    new_bills: list[dict],
    changed_bills: list[dict],
    all_bills: dict,
    config: dict,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """
    Build an HTML email from bill data and send it via SMTP.

    Returns True on success, False on failure (logs error, does not raise).
    This makes it safe to call from the pipeline without breaking the run.
    """
    log = logger or logging.getLogger(__name__)

    # Resolve credentials from environment (preferred) or config
    smtp_user = os.environ.get("EMAIL_USER") or config["email"].get("from_address", "")
    smtp_pass = os.environ.get("EMAIL_PASSWORD") or ""

    # Recipients: EMAIL_RECIPIENTS env var (comma-separated) overrides config.yaml
    env_recipients = os.environ.get("EMAIL_RECIPIENTS", "").strip()
    if env_recipients:
        recipients = [r.strip() for r in env_recipients.split(",") if r.strip()]
    else:
        recipients = [r for r in config["email"].get("recipients", []) if r]

    if not smtp_user:
        log.error("Email not sent: EMAIL_USER not set. Set it as an environment variable.")
        return False
    if not smtp_pass:
        log.error("Email not sent: EMAIL_PASSWORD not set. Set it as an environment variable.")
        return False
    if not recipients:
        log.error(
            "Email not sent: no recipients configured. "
            "Set EMAIL_RECIPIENTS in your .env file (comma-separated) "
            "or add addresses to email.recipients in config.yaml."
        )
        return False

    date_str = datetime.now().strftime("%Y-%m-%d")
    subject = config["email"]["subject_template"].format(date=date_str)

    # Build email content
    html_body = _build_html(new_bills, changed_bills, all_bills, config, date_str)
    plain_body = _build_plaintext(new_bills, changed_bills, all_bills, date_str)

    # Assemble MIME message (multipart/alternative: plain + HTML)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{config['email']['from_name']} <{smtp_user}>"
    msg["To"] = ", ".join(recipients)

    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # Send via SMTP with STARTTLS
    host = config["email"]["smtp_host"]
    port = config["email"]["smtp_port"]

    try:
        log.info(f"Sending email to {recipients} via {host}:{port}")
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, recipients, msg.as_bytes())
        log.info(f"Email sent successfully to: {', '.join(recipients)}")
        return True
    except smtplib.SMTPAuthenticationError:
        log.error(
            "Email authentication failed. For Gmail, make sure you're using an "
            "App Password (not your regular password). "
            "See: myaccount.google.com → Security → App passwords"
        )
        return False
    except Exception as exc:
        log.error(f"Failed to send email: {exc}")
        return False


def build_status_page(
    new_bills: list[dict],
    changed_bills: list[dict],
    all_bills: dict,
    config: dict,
    output_path: Path,
) -> Path:
    """
    Build a standalone HTML status page and write it to output_path.

    Designed for GitHub Pages hosting — place at docs/index.html and enable
    GitHub Pages from the /docs folder to publish at:
        https://<user>.github.io/<repo>/

    Reuses the same HTML section components as the email builder, but:
      - Wider layout (900px max-width vs 620px for email)
      - Full <style> block (not inline-only like email)
      - Adds a "Watching — No Recent Activity" section for stalled bills
      - Footer links back to the GitHub repository

    Args:
        new_bills:    Bills first seen this run.
        changed_bills: Bills whose status changed this run.
        all_bills:    Full dict of all tracked bills {bill_number: bill}.
        config:       Tracker config dict (from config.yaml).
        output_path:  Where to write the HTML file (e.g. docs/index.html).

    Returns:
        The resolved Path where the file was written.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now().strftime("%Y-%m-%d")
    lookback = config["legislative"]["lookback_days"]
    # stalled_days is intentionally shorter than lookback_days so that bills fetched
    # within the current window can still qualify as "no recent activity".
    stalled_days = config["legislative"].get("stalled_days", 7)
    repo_url = config.get("github", {}).get(
        "repo_url", "https://github.com/twgonzalez/csf-agents"
    )

    # Identify stalled bills: tracked but no status update in the last stalled_days.
    # Sorted oldest-first so the most dormant bills appear at the top.
    today = datetime.now().date()
    cutoff = today - timedelta(days=stalled_days)
    stalled: list[dict] = []
    for bill in all_bills.values():
        sd = bill.get("status_date", "")
        if sd:
            try:
                if datetime.strptime(sd, "%Y-%m-%d").date() < cutoff:
                    stalled.append(bill)
            except ValueError:
                pass
    stalled.sort(key=lambda b: b.get("status_date", ""))

    html = _build_page_html(
        new_bills=new_bills,
        changed_bills=changed_bills,
        all_bills=all_bills,
        stalled_bills=stalled,
        stalled_days=stalled_days,
        config=config,
        date_str=date_str,
        repo_url=repo_url,
    )

    output_path.write_text(html, encoding="utf-8")
    return output_path


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

# Base styles applied inline throughout the template.
# Defined as constants so they're easy to adjust in one place.
_FONT = "font-family: Arial, Helvetica, sans-serif;"
_COLOR_BG = "#f4f4f5"
_COLOR_CARD = "#ffffff"
_COLOR_ACCENT = "#1a5276"      # deep navy — primary heading color
_COLOR_ACCENT_LIGHT = "#d6e4f0"
_COLOR_GREEN = "#1e8449"       # new bill badge
_COLOR_ORANGE = "#d35400"      # changed badge
_COLOR_TEXT = "#1a1a1a"
_COLOR_MUTED = "#666666"
_COLOR_BORDER = "#e0e0e0"

# Analysis section — risk red to signal local control threat assessment
_COLOR_TEAL = "#b03a2e"
_COLOR_TEAL_LIGHT = "#fadbd8"

# Per-criterion risk colors (A=red, B=orange, C=amber, D=purple)
_CRIT_KEYS = [
    "pro_housing_production",
    "densification",
    "reduce_discretion",
    "cost_to_cities",
]
_CRIT_LETTER = {
    "pro_housing_production": "A",
    "densification":          "B",
    "reduce_discretion":      "C",
    "cost_to_cities":         "D",
}
_CRIT_LABEL = {
    "pro_housing_production": "A — Local Control Override",
    "densification":          "B — Removes Discretionary Review",
    "reduce_discretion":      "C — Mandates Development",
    "cost_to_cities":         "D — Infrastructure & Capacity Burden",
}
# strong risk: solid fill / moderate risk: lighter fill
_CRIT_STRONG_BG   = {"pro_housing_production": "#c0392b", "densification": "#d35400",
                      "reduce_discretion": "#c47600", "cost_to_cities": "#6c3483"}
_CRIT_MODERATE_BG = {"pro_housing_production": "#f1948a", "densification": "#f5cba7",
                      "reduce_discretion": "#fad7a0", "cost_to_cities": "#d2b4de"}
_CRIT_MODERATE_TX = {"pro_housing_production": "#78281f", "densification": "#784212",
                      "reduce_discretion": "#784212", "cost_to_cities": "#4a235a"}


# ---------------------------------------------------------------------------
# Analysis section helpers
# ---------------------------------------------------------------------------

def _get_analysis_data(all_bills: dict) -> dict:
    """
    Extract and rank analysis scores from the bills dict.

    Returns a dict with:
      total_analyzed  — count of bills with an analysis block
      relevant        — [(num, bill)] scored on at least 1 criterion (strong/moderate)
      high_interest   — [(num, bill)] scored on 2+ criteria
      by_crit         — {crit_key: [(num, bill), ...]} strong/moderate per criterion
      watch_list      — bills with cost_to_cities strong/moderate but <2 other criteria
    """
    scored: list[tuple] = []
    for num, bill in all_bills.items():
        a = bill.get("analysis")
        if not a:
            continue
        n_strong   = sum(1 for k in _CRIT_KEYS if a.get(k) == "strong")
        n_moderate = sum(1 for k in _CRIT_KEYS if a.get(k) == "moderate")
        n_total    = sum(1 for k in _CRIT_KEYS if a.get(k) in ("strong", "moderate"))
        scored.append((num, bill, n_strong, n_moderate, n_total))

    # Primary sort: strong count desc, secondary: moderate count desc
    scored.sort(key=lambda x: (x[2], x[3]), reverse=True)

    relevant      = [(n, b) for n, b, ns, nm, nt in scored if nt >= 1]
    high_interest = [(n, b) for n, b, ns, nm, nt in scored if nt >= 2]

    by_crit: dict[str, list] = {k: [] for k in _CRIT_KEYS}
    for num, bill, *_ in scored:
        a = bill["analysis"]
        for k in _CRIT_KEYS:
            if a.get(k) in ("strong", "moderate"):
                by_crit[k].append((num, bill))

    watch_list = [
        (n, b) for n, b, ns, nm, nt in scored
        if b["analysis"].get("cost_to_cities") in ("strong", "moderate") and nt < 2
    ]

    return {
        "total_analyzed": len(scored),
        "relevant":       relevant,
        "high_interest":  high_interest,
        "by_crit":        by_crit,
        "watch_list":     watch_list,
    }


def _score_pills(analysis: dict) -> str:
    """Render a row of colored criteria pills for a bill card."""
    pills = []
    for k in _CRIT_KEYS:
        score = analysis.get(k, "none")
        letter = _CRIT_LETTER[k]
        if score == "strong":
            bg = _CRIT_STRONG_BG[k]
            pills.append(
                f'<span style="display:inline-block; background:{bg}; color:#fff; '
                f'font-size:10px; font-weight:700; padding:2px 7px; border-radius:3px; '
                f'margin:0 3px 0 0; letter-spacing:0.5px; {_FONT}">{letter}</span>'
            )
        elif score == "moderate":
            bg = _CRIT_MODERATE_BG[k]
            tx = _CRIT_MODERATE_TX[k]
            pills.append(
                f'<span style="display:inline-block; background:{bg}; color:{tx}; '
                f'font-size:10px; font-weight:600; padding:2px 7px; border-radius:3px; '
                f'margin:0 3px 0 0; letter-spacing:0.5px; {_FONT}">{letter}</span>'
            )
    return "".join(pills) if pills else ""


def _render_comms_brief(comms_brief: str) -> str:
    """
    Parse and render the structured comms_brief into formatted HTML.

    Expected plain-text format:
      [Summary sentence]
      • [Risk point 1]
      • [Risk point 2]
      Recommended: [Action text]

    Produces:
      - Summary in slightly bolder text
      - Bullets as a table-based list (email-safe, no <ul>/<li> needed)
      - "Recommended:" as a distinct amber callout box
    """
    if not comms_brief:
        return ""

    lines = [ln.strip() for ln in comms_brief.split("\n") if ln.strip()]

    summary     = ""
    bullets: list[str] = []
    recommended = ""

    for ln in lines:
        if ln.startswith("•"):
            bullets.append(ln[1:].strip())
        elif ln.startswith("Recommended:"):
            recommended = ln[len("Recommended:"):].strip()
        elif not summary:
            summary = ln

    parts = []

    # ── Summary sentence ──────────────────────────────────────────────────────
    if summary:
        parts.append(
            f'<p style="margin:0 0 8px 0; font-size:13px; font-weight:600; '
            f'color:{_COLOR_TEXT}; line-height:1.5; {_FONT}">{summary}</p>'
        )

    # ── Bullet points (table-based for email compatibility) ───────────────────
    if bullets:
        bullet_rows = "".join(
            f'<tr>'
            f'<td width="14" style="vertical-align:top; padding:1px 6px 5px 0; '
            f'font-size:13px; color:{_COLOR_TEAL}; {_FONT}">&#8226;</td>'
            f'<td style="padding:0 0 5px 0; font-size:12px; color:{_COLOR_TEXT}; '
            f'line-height:1.5; {_FONT}">{b}</td>'
            f'</tr>'
            for b in bullets
        )
        parts.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin:0 0 6px 0;">{bullet_rows}</table>'
        )

    # ── Recommended action callout ────────────────────────────────────────────
    if recommended:
        amber = _CRIT_STRONG_BG["reduce_discretion"]   # #c47600
        parts.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin:4px 0 0 0; background:#fef9ec; '
            f'border-left:3px solid {amber}; border-radius:0 3px 3px 0;">'
            f'<tr><td style="padding:7px 10px;">'
            f'<span style="font-size:10px; font-weight:700; color:{amber}; '
            f'text-transform:uppercase; letter-spacing:0.5px; {_FONT}">Action&nbsp;&rarr;</span>'
            f'&nbsp;&nbsp;'
            f'<span style="font-size:12px; color:{_COLOR_TEXT}; {_FONT}">{recommended}</span>'
            f'</td></tr></table>'
        )

    if not parts:
        # Fallback: no structure parsed — render raw with line breaks
        safe = comms_brief.replace("\n", "<br>")
        return (
            f'<div style="font-size:13px; color:{_COLOR_TEXT}; '
            f'line-height:1.6; {_FONT}">{safe}</div>'
        )

    return (
        f'<div style="margin:0 0 8px 0; padding:10px 12px; background:#fdf5f4; '
        f'border-left:3px solid {_COLOR_TEAL_LIGHT}; border-radius:0 4px 4px 0;">'
        + "".join(parts)
        + "</div>"
    )


def _html_analysis_section(all_bills: dict) -> str:
    """
    Render the Local Control Risk Analysis section for the top of the email.

    Shows:
      - Quick-stats bar (bills with risk signals, high-risk, total analyzed)
      - Top risk bill cards (up to 8) with criteria risk pills + comms_brief
      - Per-criterion summary (strong risks only, up to 5 per criterion)
      - Watch list (financial burden bills) if any
    """
    ad = _get_analysis_data(all_bills)
    if not ad["total_analyzed"]:
        return ""   # No analysis data — skip section entirely

    high_interest = ad["high_interest"]
    by_crit       = ad["by_crit"]
    watch_list    = ad["watch_list"]

    # ── Stats bar ────────────────────────────────────────────────────────────
    def stat_cell(value: str, label: str, border: bool = True) -> str:
        border_css = f"border-right:1px solid {_COLOR_TEAL_LIGHT};" if border else ""
        return f"""
        <td width="33%" align="center"
            style="padding:16px 10px; {border_css}">
          <div style="font-size:28px; font-weight:700; color:{_COLOR_TEAL};
                      line-height:1; {_FONT}">{value}</div>
          <div style="font-size:11px; color:{_COLOR_MUTED}; margin-top:5px;
                      text-transform:uppercase; letter-spacing:0.5px; {_FONT}">
            {label}
          </div>
        </td>"""

    stats_html = f"""
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="border:1px solid {_COLOR_TEAL_LIGHT}; border-radius:0;
                background:#fdf5f4;">
    <tr>
      {stat_cell(str(len(ad["relevant"])), "Bills with Risk Signals")}
      {stat_cell(str(len(high_interest)), "High-Risk (2+ Criteria)")}
      {stat_cell(str(ad["total_analyzed"]), "Total Analyzed", border=False)}
    </tr>
  </table>"""

    # ── Key bills (top 8 high-interest) ──────────────────────────────────────
    key_bills_html = ""
    for num, bill in high_interest[:8]:
        analysis    = bill.get("analysis", {})
        url         = bill.get("text_url", "")
        title       = bill.get("title", "")
        author      = bill.get("author", "")
        status      = bill.get("status", "")
        comms_brief = analysis.get("comms_brief") or ""
        # Fall back to truncated notes if no comms_brief yet
        if not comms_brief:
            notes_raw = (analysis.get("notes") or "")
            comms_brief = notes_raw[:200] + ("…" if len(notes_raw) > 200 else "")
        brief_card  = _render_comms_brief(comms_brief)
        # Count how many criteria scored strong or moderate
        n_risk = sum(1 for k in _CRIT_KEYS if analysis.get(k) in ("strong", "moderate"))
        pills  = _score_pills(analysis)

        bill_link = (
            f'<a href="{url}" target="_blank" '
            f'style="color:{_COLOR_TEAL}; font-weight:700; font-size:14px; '
            f'text-decoration:none; {_FONT}">{num}</a>'
            if url else
            f'<strong style="color:{_COLOR_TEAL}; font-size:14px; {_FONT}">{num}</strong>'
        )
        view_link = (
            f'<a href="{url}" target="_blank" '
            f'style="color:{_COLOR_TEAL}; font-size:12px; {_FONT}">View bill →</a>'
            if url else ""
        )
        risk_badge = (
            f'<span style="display:inline-block; background:{_COLOR_TEAL}; color:#fff; '
            f'font-size:10px; font-weight:700; padding:2px 7px; border-radius:3px; '
            f'vertical-align:middle; {_FONT}">{n_risk}&nbsp;criteria</span>'
        )

        key_bills_html += _row(f"""
  <div style="padding:16px 32px 20px 32px; border-bottom:1px solid {_COLOR_TEAL_LIGHT};">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td style="vertical-align:top;">
          {bill_link}
          &nbsp;
          <span style="font-size:13px; font-weight:600; color:{_COLOR_TEXT}; {_FONT}">
            {title[:65]}{"…" if len(title) > 65 else ""}
          </span>
        </td>
        <td align="right" style="vertical-align:top; padding-left:8px; white-space:nowrap;">
          {risk_badge}
        </td>
      </tr>
    </table>
    <p style="margin:4px 0 8px 0; font-size:12px; color:{_COLOR_MUTED}; {_FONT}">
      {author} &nbsp;·&nbsp; {status}
    </p>
    <div style="margin-bottom:10px;">{pills}</div>
    {brief_card}
    {f'<p style="margin:6px 0 0 0;">{view_link}</p>' if view_link else ""}
  </div>
""", bg=_COLOR_CARD, padding="0")

    if not key_bills_html:
        key_bills_html = _row(
            f'<p style="padding:16px 32px; color:{_COLOR_MUTED}; '
            f'font-size:13px; {_FONT}">No bills scored on 2+ criteria yet.</p>',
            bg=_COLOR_CARD, padding="0",
        )

    # ── By criterion (strong scores, ≤5 per criterion) ───────────────────────
    crit_cells = ""
    legend_items = [
        f'<span style="display:inline-block; background:{_CRIT_STRONG_BG[k]}; '
        f'color:#fff; font-size:10px; font-weight:700; padding:2px 7px; '
        f'border-radius:3px; margin:0 6px 0 0; {_FONT}">{_CRIT_LETTER[k]}</span>'
        f'<span style="font-size:11px; color:{_COLOR_MUTED}; {_FONT}">'
        f'{_CRIT_LABEL[k].split(" — ", 1)[1]}</span>'
        for k in _CRIT_KEYS
    ]
    legend_html = (
        '<div style="padding:10px 32px 4px 32px;">'
        + "&nbsp;&nbsp;".join(legend_items)
        + "</div>"
    )

    for k in _CRIT_KEYS:
        strong_bills = [(n, b) for n, b in by_crit[k]
                        if b["analysis"].get(k) == "strong"][:5]
        if not strong_bills:
            continue
        bill_links = ", ".join(
            f'<a href="{b.get("text_url","")}" target="_blank" '
            f'style="color:{_CRIT_STRONG_BG[k]}; font-weight:600; {_FONT}">{n}</a>'
            if b.get("text_url") else
            f'<strong style="color:{_CRIT_STRONG_BG[k]}; {_FONT}">{n}</strong>'
            for n, b in strong_bills
        )
        letter = _CRIT_LETTER[k]
        bg     = _CRIT_STRONG_BG[k]
        crit_cells += f"""
      <tr>
        <td style="padding:6px 0; border-bottom:1px solid {_COLOR_BORDER};
                   vertical-align:top; width:30px;">
          <span style="display:inline-block; background:{bg}; color:#fff;
                       font-size:10px; font-weight:700; padding:2px 7px;
                       border-radius:3px; {_FONT}">{letter}</span>
        </td>
        <td style="padding:6px 0 6px 10px; border-bottom:1px solid {_COLOR_BORDER};
                   font-size:12px; color:{_COLOR_TEXT}; vertical-align:top; {_FONT}">
          {bill_links}
        </td>
      </tr>"""

    crit_section_html = ""
    if crit_cells:
        crit_section_html = _row(f"""
  {legend_html}
  <div style="padding:4px 32px 16px 32px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      {crit_cells}
    </table>
  </div>
""", bg=_COLOR_CARD, padding="0")

    # ── Watch list ────────────────────────────────────────────────────────────
    watch_html = ""
    if watch_list:
        watch_items = ", ".join(
            f'<a href="{b.get("text_url","")}" target="_blank" '
            f'style="color:{_COLOR_ORANGE}; {_FONT}">{n}</a>'
            if b.get("text_url") else
            f'<span style="color:{_COLOR_ORANGE}; {_FONT}">{n}</span>'
            for n, b in watch_list[:6]
        )
        watch_html = _row(f"""
  <div style="padding:10px 32px 14px 32px; background:#fff8f0;
              border-top:1px solid {_COLOR_BORDER};">
    <span style="font-size:12px; font-weight:700; color:{_COLOR_ORANGE};
                 text-transform:uppercase; letter-spacing:0.5px; {_FONT}">
      ⚠ Watch List
    </span>
    <span style="font-size:12px; color:{_COLOR_MUTED}; margin-left:8px; {_FONT}">
      Bills imposing financial obligations on cities with limited other risk signals:
    </span>
    <span style="font-size:12px; {_FONT}">&nbsp; {watch_items}</span>
  </div>
""", bg=_COLOR_CARD, padding="0")

    # ── Assemble section ──────────────────────────────────────────────────────
    section_header = f"""
{_spacer(bg=_COLOR_BG)}
{_row(f"""
  <h2 style="margin:0; padding:16px 32px 12px 32px; font-size:15px;
             font-weight:700; color:{_COLOR_TEAL}; text-transform:uppercase;
             letter-spacing:0.8px; border-bottom:2px solid {_COLOR_TEAL_LIGHT};
             {_FONT}">
    Local Control Risk Analysis
  </h2>
""", bg=_COLOR_CARD, padding="0")}"""

    # Compact inline criteria legend for the key bills sub-header
    _crit_chips = "".join(
        f'<span style="white-space:nowrap; display:inline-block; margin:0 14px 3px 0;">'
        f'<span style="display:inline-block; background:{_CRIT_STRONG_BG[k]}; color:#fff; '
        f'font-size:9px; font-weight:700; padding:1px 5px; border-radius:2px; '
        f'vertical-align:middle; {_FONT}">{_CRIT_LETTER[k]}</span>'
        f'<span style="font-size:11px; color:{_COLOR_MUTED}; margin-left:4px; {_FONT}">'
        f'{_CRIT_LABEL[k].split(" — ", 1)[1]}</span>'
        f'</span>'
        for k in _CRIT_KEYS
    )
    key_header = f"""
{_row(f"""
  <div style="padding:14px 32px 4px 32px;">
    <p style="margin:0 0 6px 0; font-size:12px; font-weight:700; color:{_COLOR_MUTED};
              text-transform:uppercase; letter-spacing:0.5px; {_FONT}">
      High-Risk Bills &mdash; {len(high_interest)} of {ad['total_analyzed']} analyzed
    </p>
    <div style="line-height:2.0;">{_crit_chips}</div>
  </div>
""", bg=_COLOR_CARD, padding="0")}"""

    crit_header = f"""
{_row(f"""
  <p style="margin:0; padding:14px 32px 4px 32px; font-size:12px;
            font-weight:700; color:{_COLOR_MUTED}; text-transform:uppercase;
            letter-spacing:0.5px; {_FONT}">
    Risk Levels by Criterion
  </p>
""", bg=_COLOR_CARD, padding="0")}""" if crit_section_html else ""

    stats_row = _row(
        f'<div style="padding:0 32px 0 32px;">{stats_html}</div>',
        bg=_COLOR_CARD,
        padding="16px 0 0 0",
    )

    return (
        section_header
        + stats_row
        + key_header
        + key_bills_html
        + crit_header
        + crit_section_html
        + watch_html
    )


def _build_html(
    new_bills: list[dict],
    changed_bills: list[dict],
    all_bills: dict,
    config: dict,
    date_str: str,
) -> str:
    """Assemble the full HTML email string."""
    lookback = config["legislative"]["lookback_days"]
    include_index = config["email"].get("include_full_index", True)
    pages_url = config.get("github", {}).get("pages_url", "")

    sections = [
        _html_header(date_str, lookback),
        _html_summary(len(new_bills), len(changed_bills), len(all_bills)),
        _html_analysis_section(all_bills),   # analysis at the top
    ]

    if new_bills:
        sections.append(_html_bill_section(
            f"New Bills This Week ({len(new_bills)})",
            new_bills,
            badge_color=_COLOR_GREEN,
            badge_label="NEW",
        ))

    if changed_bills:
        sections.append(_html_changes_section(changed_bills))

    sections += [_html_hearings_section(all_bills)]

    if include_index:
        sections.append(_html_index_section(all_bills))

    sections.append(_html_footer(date_str, pages_url=pages_url))

    body_content = "\n".join(sections)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>CA Housing Intelligence — Week of {date_str}</title>
</head>
<body style="margin:0; padding:0; background-color:{_COLOR_BG}; {_FONT}">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background-color:{_COLOR_BG};">
    <tr>
      <td align="center" style="padding: 24px 12px;">
        <!-- Outer container: max 620px for email clients -->
        <table role="presentation" width="620" cellpadding="0" cellspacing="0"
               style="max-width:620px; width:100%;">
          {body_content}
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _row(content: str, bg: str = _COLOR_CARD, padding: str = "0") -> str:
    """Wrap content in a table row (the basic email layout unit)."""
    return f"""
<tr>
  <td style="background-color:{bg}; padding:{padding};">
    {content}
  </td>
</tr>"""


def _spacer(height: int = 16, bg: str = _COLOR_BG) -> str:
    return f'<tr><td style="height:{height}px; background-color:{bg};"></td></tr>'


def _html_header(date_str: str, lookback: int) -> str:
    return f"""
{_row(f"""
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
    <tr>
      <td style="background-color:{_COLOR_ACCENT}; padding:28px 32px 24px 32px;
                 border-radius:8px 8px 0 0;">
        <p style="margin:0 0 4px 0; color:#a8c4e0; font-size:11px;
                  text-transform:uppercase; letter-spacing:1.5px; {_FONT}">
          California Stewardship Fund
        </p>
        <h1 style="margin:0 0 8px 0; color:#ffffff; font-size:22px;
                   font-weight:700; line-height:1.2; {_FONT}">
          CA Housing Policy Intelligence
        </h1>
        <p style="margin:0; color:#a8c4e0; font-size:13px; {_FONT}">
          Week of {date_str} &nbsp;·&nbsp; Weekly digest
        </p>
      </td>
    </tr>
  </table>
""", bg=_COLOR_CARD, padding="0")}"""


def _html_summary(n_new: int, n_changed: int, n_total: int) -> str:
    def stat_cell(value: str, label: str, color: str) -> str:
        return f"""
        <td width="33%" align="center"
            style="padding:20px 12px; border-right:1px solid {_COLOR_BORDER};">
          <div style="font-size:32px; font-weight:700; color:{color};
                      line-height:1; {_FONT}">{value}</div>
          <div style="font-size:12px; color:{_COLOR_MUTED}; margin-top:6px;
                      text-transform:uppercase; letter-spacing:0.5px; {_FONT}">
            {label}
          </div>
        </td>"""

    return f"""
{_spacer(bg=_COLOR_BG)}
{_row(f"""
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="border:1px solid {_COLOR_BORDER}; border-radius:0;">
    <tr>
      {stat_cell(str(n_new), "New This Week", _COLOR_GREEN)}
      {stat_cell(str(n_changed), "Status Changes", _COLOR_ORANGE)}
      <td width="33%" align="center" style="padding:20px 12px;">
        <div style="font-size:32px; font-weight:700; color:{_COLOR_ACCENT};
                    line-height:1; {_FONT}">{n_total}</div>
        <div style="font-size:12px; color:{_COLOR_MUTED}; margin-top:6px;
                    text-transform:uppercase; letter-spacing:0.5px; {_FONT}">
          Total Tracked
        </div>
      </td>
    </tr>
  </table>
""", bg=_COLOR_CARD, padding="0")}"""


def _section_header(title: str) -> str:
    return f"""
{_spacer(bg=_COLOR_BG)}
{_row(f"""
  <h2 style="margin:0; padding:16px 32px 12px 32px; font-size:15px;
             font-weight:700; color:{_COLOR_ACCENT}; text-transform:uppercase;
             letter-spacing:0.8px; border-bottom:2px solid {_COLOR_ACCENT_LIGHT};
             {_FONT}">
    {title}
  </h2>
""", bg=_COLOR_CARD, padding="0")}"""


def _html_bill_section(
    heading: str,
    bills: list[dict],
    badge_color: str,
    badge_label: str,
) -> str:
    bill_rows = "".join(_html_bill_card(b, badge_color, badge_label) for b in
                        sorted(bills, key=lambda b: b.get("bill_number", "")))
    return _section_header(heading) + bill_rows


def _html_bill_card(bill: dict, badge_color: str, badge_label: str) -> str:
    num = bill.get("bill_number", "")
    title = bill.get("title", "")
    author = bill.get("author", "")
    status = bill.get("status", "")
    summary = bill.get("summary", "")
    url = bill.get("text_url", "")
    subjects = bill.get("subjects", [])
    committees = bill.get("committees", [])
    intro_date = bill.get("introduced_date", "")

    # Subject tags
    tag_html = ""
    if subjects:
        tags = "".join(
            f'<span style="display:inline-block; background:{_COLOR_ACCENT_LIGHT}; '
            f'color:{_COLOR_ACCENT}; font-size:11px; padding:2px 8px; '
            f'border-radius:3px; margin:0 4px 4px 0; {_FONT}">{t}</span>'
            for t in subjects[:5]
        )
        tag_html = f'<div style="margin-top:10px;">{tags}</div>'

    # Summary block
    summary_html = ""
    if summary:
        short = summary[:280] + ("…" if len(summary) > 280 else "")
        summary_html = f"""
        <p style="margin:10px 0 0 0; font-size:13px; color:{_COLOR_TEXT};
                  line-height:1.6; border-left:3px solid {_COLOR_ACCENT_LIGHT};
                  padding-left:12px; {_FONT}">
          {short}
        </p>"""

    # Committee
    committee_html = ""
    if committees:
        committee_html = f"""
        <p style="margin:8px 0 0 0; font-size:12px; color:{_COLOR_MUTED}; {_FONT}">
          <strong>Committee:</strong> {', '.join(committees[:2])}
        </p>"""

    # CTA button
    cta_html = ""
    if url:
        cta_html = f"""
        <p style="margin:14px 0 0 0;">
          <a href="{url}" target="_blank"
             style="display:inline-block; background:{_COLOR_ACCENT};
                    color:#ffffff; text-decoration:none; font-size:12px;
                    font-weight:600; padding:7px 16px; border-radius:4px;
                    {_FONT}">
            View Full Text →
          </a>
        </p>"""

    return _row(f"""
  <div style="padding:20px 32px; border-bottom:1px solid {_COLOR_BORDER};">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td style="vertical-align:top;">
          <span style="display:inline-block; background:{badge_color};
                       color:#fff; font-size:10px; font-weight:700;
                       padding:2px 7px; border-radius:3px; letter-spacing:0.5px;
                       vertical-align:middle; margin-right:8px; {_FONT}">
            {badge_label}
          </span>
          <strong style="font-size:15px; color:{_COLOR_ACCENT}; {_FONT}">{num}</strong>
        </td>
        {"" if not intro_date else f'<td align="right" style="font-size:12px; color:{_COLOR_MUTED}; vertical-align:top; {_FONT}">{intro_date}</td>'}
      </tr>
    </table>
    <p style="margin:6px 0 4px 0; font-size:14px; font-weight:600;
              color:{_COLOR_TEXT}; line-height:1.3; {_FONT}">
      {title}
    </p>
    <p style="margin:0; font-size:12px; color:{_COLOR_MUTED}; {_FONT}">
      {f"<strong>Author:</strong> {author} &nbsp;·&nbsp; " if author else ""}
      <strong>Status:</strong> {status or "—"}
    </p>
    {committee_html}
    {summary_html}
    {tag_html}
    {cta_html}
  </div>
""", bg=_COLOR_CARD, padding="0")


def _html_changes_section(changed_bills: list[dict]) -> str:
    cards = ""
    for bill in changed_bills:
        num = bill.get("bill_number", "")
        title = bill.get("title", "")
        prev = bill.get("_prev_status") or "—"
        new = bill.get("status", "—")
        author = bill.get("author", "")
        url = bill.get("text_url", "")

        cta = (
            f'<a href="{url}" target="_blank" '
            f'style="color:{_COLOR_ACCENT}; font-size:12px; {_FONT}">View bill →</a>'
            if url else ""
        )

        cards += _row(f"""
  <div style="padding:18px 32px; border-bottom:1px solid {_COLOR_BORDER};">
    <span style="display:inline-block; background:{_COLOR_ORANGE};
                 color:#fff; font-size:10px; font-weight:700; padding:2px 7px;
                 border-radius:3px; letter-spacing:0.5px;
                 margin-right:8px; {_FONT}">UPDATED</span>
    <strong style="font-size:14px; color:{_COLOR_ACCENT}; {_FONT}">{num}</strong>
    <p style="margin:6px 0 4px 0; font-size:13px; font-weight:600;
              color:{_COLOR_TEXT}; {_FONT}">{title}</p>
    {"" if not author else f'<p style="margin:0 0 8px 0; font-size:12px; color:{_COLOR_MUTED}; {_FONT}">Author: {author}</p>'}
    <table role="presentation" cellpadding="0" cellspacing="0"
           style="width:100%; border:1px solid {_COLOR_BORDER}; border-radius:4px;
                  background:#fafafa; margin-top:6px;">
      <tr>
        <td style="padding:10px 14px; border-right:1px solid {_COLOR_BORDER};
                   width:50%; vertical-align:top;">
          <div style="font-size:10px; color:{_COLOR_MUTED}; text-transform:uppercase;
                      letter-spacing:0.5px; margin-bottom:4px; {_FONT}">
            Previous
          </div>
          <div style="font-size:12px; color:{_COLOR_MUTED};
                      text-decoration:line-through; {_FONT}">{prev}</div>
        </td>
        <td style="padding:10px 14px; width:50%; vertical-align:top;">
          <div style="font-size:10px; color:{_COLOR_MUTED}; text-transform:uppercase;
                      letter-spacing:0.5px; margin-bottom:4px; {_FONT}">
            Now
          </div>
          <div style="font-size:12px; color:{_COLOR_GREEN}; font-weight:600;
                      {_FONT}">{new}</div>
        </td>
      </tr>
    </table>
    {"" if not cta else f'<p style="margin:10px 0 0 0;">{cta}</p>'}
  </div>
""", bg=_COLOR_CARD, padding="0")

    return _section_header(f"Status Changes ({len(changed_bills)})") + cards


def _html_hearings_section(all_bills: dict) -> str:
    hearings = []
    for num, bill in all_bills.items():
        for h in bill.get("upcoming_hearings", []):
            hearings.append({
                **h,
                "bill_number": num,
                "bill_title": bill.get("title", ""),
            })
    hearings.sort(key=lambda h: h.get("date", "9999"))

    if not hearings:
        return ""

    rows = ""
    for h in hearings[:10]:
        committee = h.get("committee", "TBD")
        location = h.get("location", "")
        rows += f"""
        <tr>
          <td style="padding:10px 0; border-bottom:1px solid {_COLOR_BORDER};
                     vertical-align:top; width:100px;">
            <strong style="font-size:13px; color:{_COLOR_ACCENT}; {_FONT}">
              {h.get('date', 'TBD')}
            </strong>
          </td>
          <td style="padding:10px 0 10px 16px; border-bottom:1px solid {_COLOR_BORDER};
                     vertical-align:top;">
            <div style="font-size:13px; font-weight:600; color:{_COLOR_TEXT}; {_FONT}">
              {h['bill_number']}
            </div>
            <div style="font-size:12px; color:{_COLOR_MUTED}; margin-top:2px; {_FONT}">
              {committee}{"  ·  " + location if location else ""}
            </div>
            <div style="font-size:12px; color:{_COLOR_TEXT}; margin-top:2px; {_FONT}">
              {h['bill_title'][:70]}
            </div>
          </td>
        </tr>"""

    return (
        _section_header(f"Upcoming Hearings ({len(hearings)})")
        + _row(f"""
  <div style="padding:4px 32px 20px 32px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      {rows}
    </table>
  </div>
""", bg=_COLOR_CARD, padding="0")
    )


def _html_index_section(all_bills: dict) -> str:
    if not all_bills:
        return ""

    header_style = (
        f"padding:8px 10px; background:{_COLOR_ACCENT}; color:#fff; "
        f"font-size:11px; font-weight:600; text-align:left; {_FONT}"
    )
    cell_style = (
        f"padding:8px 10px; font-size:12px; color:{_COLOR_TEXT}; "
        f"border-bottom:1px solid {_COLOR_BORDER}; vertical-align:top; {_FONT}"
    )

    rows = ""
    for num in sorted(all_bills.keys()):
        b = all_bills[num]
        url = b.get("text_url", "")
        bill_cell = (
            f'<a href="{url}" style="color:{_COLOR_ACCENT}; {_FONT}">{num}</a>'
            if url else num
        )
        author = (b.get("author") or "")[:20]
        status = (b.get("status") or "")[:45]
        title = (b.get("title") or "")[:55]
        pills = _score_pills(b.get("analysis") or {})
        risk_cell = pills if pills else f'<span style="color:{_COLOR_BORDER};">—</span>'
        rows += f"""
        <tr>
          <td style="{cell_style} white-space:nowrap;">{bill_cell}</td>
          <td style="{cell_style} white-space:nowrap;">{risk_cell}</td>
          <td style="{cell_style}">{author}</td>
          <td style="{cell_style}">{status}</td>
          <td style="{cell_style}">{title}</td>
        </tr>"""

    return (
        _section_header(f"All Tracked Bills ({len(all_bills)})")
        + _row(f"""
  <div style="padding:0 32px 20px 32px; overflow-x:auto;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="border-collapse:collapse; border:1px solid {_COLOR_BORDER};">
      <thead>
        <tr>
          <th style="{header_style}">Bill</th>
          <th style="{header_style}">Risk</th>
          <th style="{header_style}">Author</th>
          <th style="{header_style}">Status</th>
          <th style="{header_style}">Title</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
""", bg=_COLOR_CARD, padding="0")
    )


def _html_footer(date_str: str, pages_url: str = "") -> str:
    dashboard_link = ""
    if pages_url:
        dashboard_link = f"""
    <p style="margin:14px 0 0 0;">
      <a href="{pages_url}" target="_blank"
         style="display:inline-block; background:{_COLOR_ACCENT};
                color:#ffffff; text-decoration:none; font-size:12px;
                font-weight:600; padding:8px 20px; border-radius:4px;
                {_FONT}">
        View Live Status Dashboard →
      </a>
    </p>"""

    return f"""
{_spacer(bg=_COLOR_BG)}
{_row(f"""
  <div style="padding:20px 32px; border-radius:0 0 8px 8px; text-align:center;">
    <p style="margin:0 0 6px 0; font-size:12px; color:{_COLOR_MUTED}; {_FONT}">
      <strong style="color:{_COLOR_ACCENT};">California Stewardship Fund</strong>
      &nbsp;·&nbsp; Legislative Intelligence Tracker
    </p>
    <p style="margin:0; font-size:11px; color:{_COLOR_MUTED}; {_FONT}">
      Generated {date_str} &nbsp;·&nbsp;
      Data: LegiScan (legiscan.com) &nbsp;·&nbsp;
      To update recipients, set <code>EMAIL_RECIPIENTS</code> in your .env file
    </p>
    {dashboard_link}
  </div>
""", bg=_COLOR_CARD, padding="0")}
{_spacer(height=24, bg=_COLOR_BG)}"""


# ---------------------------------------------------------------------------
# Status page helpers (standalone web page, not email)
# ---------------------------------------------------------------------------

def _html_stalled_section(stalled_bills: list[dict], lookback_days: int) -> str:
    """
    Render the 'Watching — No Recent Activity' table.

    Shows bills that are being tracked but have had no status update in the
    last `lookback_days` days. Intended for the status web page, not the email.
    """
    if not stalled_bills:
        return ""

    today = datetime.now().date()

    header_style = (
        f"padding:8px 10px; background:{_COLOR_ACCENT}; color:#fff; "
        f"font-size:11px; font-weight:600; text-align:left; {_FONT}"
    )
    cell_style = (
        f"padding:8px 10px; font-size:12px; color:{_COLOR_TEXT}; "
        f"border-bottom:1px solid {_COLOR_BORDER}; vertical-align:top; {_FONT}"
    )
    muted_cell = (
        f"padding:8px 10px; font-size:12px; color:{_COLOR_MUTED}; "
        f"border-bottom:1px solid {_COLOR_BORDER}; vertical-align:top; {_FONT}"
    )

    rows = ""
    for bill in stalled_bills:
        num = bill.get("bill_number", "")
        url = bill.get("text_url", "")
        bill_cell = (
            f'<a href="{url}" style="color:{_COLOR_ACCENT}; font-weight:600; '
            f'{_FONT}">{num}</a>'
            if url else f'<strong style="{_FONT}">{num}</strong>'
        )
        author = (bill.get("author") or "")[:28]
        status = (bill.get("status") or "—")[:55]
        title = (bill.get("title") or "")[:60]
        introduced = bill.get("introduced_date", "")
        sd = bill.get("status_date", "")
        pills = _score_pills(bill.get("analysis") or {})
        risk_cell = pills if pills else f'<span style="color:{_COLOR_BORDER};">—</span>'

        days_ago = sd  # fallback: show raw date
        if sd:
            try:
                bill_date = datetime.strptime(sd, "%Y-%m-%d").date()
                days = (today - bill_date).days
                days_ago = f"{days}d ago"
            except ValueError:
                pass

        rows += f"""
        <tr>
          <td style="{cell_style} white-space:nowrap;">{bill_cell}</td>
          <td style="{cell_style} white-space:nowrap;">{risk_cell}</td>
          <td style="{cell_style}">{title}</td>
          <td style="{muted_cell}">{author}</td>
          <td style="{muted_cell} white-space:nowrap;">{introduced}</td>
          <td style="{muted_cell}">{status}</td>
          <td style="{muted_cell} white-space:nowrap; text-align:right;">{days_ago}</td>
        </tr>"""

    return (
        _section_header(f"Watching — No Recent Activity ({len(stalled_bills)})")
        + _row(f"""
  <div style="padding:4px 32px 16px 32px;">
    <p style="margin:0 0 12px 0; font-size:12px; color:{_COLOR_MUTED}; {_FONT}">
      Bills with no status update in the last {lookback_days} days.
      These bills may be stalled in committee or awaiting floor action.
    </p>
    <div style="overflow-x:auto;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="border-collapse:collapse; border:1px solid {_COLOR_BORDER};">
        <thead>
          <tr>
            <th style="{header_style}">Bill</th>
            <th style="{header_style}">Risk</th>
            <th style="{header_style}">Title</th>
            <th style="{header_style}">Author</th>
            <th style="{header_style}">Introduced</th>
            <th style="{header_style}">Last Status</th>
            <th style="{header_style}; text-align:right;">Last Activity</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </div>
""", bg=_COLOR_CARD, padding="0")
    )


def _page_footer(date_str: str, repo_url: str) -> str:
    """Footer for the standalone web status page (includes GitHub repo link)."""
    return f"""
{_spacer(bg=_COLOR_BG)}
{_row(f"""
  <div style="padding:20px 32px; border-radius:0 0 8px 8px; text-align:center;">
    <p style="margin:0 0 6px 0; font-size:12px; color:{_COLOR_MUTED}; {_FONT}">
      <strong style="color:{_COLOR_ACCENT};">California Stewardship Fund</strong>
      &nbsp;·&nbsp; Legislative Intelligence Tracker
    </p>
    <p style="margin:0 0 6px 0; font-size:11px; color:{_COLOR_MUTED}; {_FONT}">
      Last updated: {date_str}
      &nbsp;·&nbsp;
      Data: <a href="https://legiscan.com" style="color:{_COLOR_ACCENT};">LegiScan</a>
    </p>
    <p style="margin:0; font-size:11px; {_FONT}">
      <a href="{repo_url}" style="color:{_COLOR_ACCENT};">
        View source on GitHub →
      </a>
    </p>
  </div>
""", bg=_COLOR_CARD, padding="0")}
{_spacer(height=24, bg=_COLOR_BG)}"""


def _build_page_html(
    new_bills: list[dict],
    changed_bills: list[dict],
    all_bills: dict,
    stalled_bills: list[dict],
    stalled_days: int,
    config: dict,
    date_str: str,
    repo_url: str,
) -> str:
    """Assemble the full standalone HTML status page string."""
    lookback = config["legislative"]["lookback_days"]

    sections = [
        _html_header(date_str, lookback),
        _html_summary(len(new_bills), len(changed_bills), len(all_bills)),
        _html_analysis_section(all_bills),   # analysis at the top
    ]

    if stalled_bills:
        sections.append(_html_stalled_section(stalled_bills, stalled_days))

    if new_bills:
        sections.append(_html_bill_section(
            f"New Bills This Week ({len(new_bills)})",
            new_bills,
            badge_color=_COLOR_GREEN,
            badge_label="NEW",
        ))

    if changed_bills:
        sections.append(_html_changes_section(changed_bills))

    sections += [_html_hearings_section(all_bills)]
    sections.append(_html_index_section(all_bills))
    sections.append(_page_footer(date_str, repo_url))

    body_content = "\n".join(sections)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>CA Housing Policy Intelligence — CSF Legislative Tracker</title>
  <style>
    body {{ margin: 0; padding: 0; background-color: {_COLOR_BG}; {_FONT} }}
    a {{ color: {_COLOR_ACCENT}; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    @media (max-width: 680px) {{
      .outer-td {{ padding: 12px 4px !important; }}
    }}
  </style>
</head>
<body>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background-color:{_COLOR_BG};">
    <tr>
      <td align="center" class="outer-td" style="padding: 24px 12px;">
        <!-- Status page: 900px wide for desktop browsers -->
        <table role="presentation" width="900" cellpadding="0" cellspacing="0"
               style="max-width:900px; width:100%;">
          {body_content}
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Plaintext fallback (for email clients that don't render HTML)
# ---------------------------------------------------------------------------

def _build_plaintext(
    new_bills: list[dict],
    changed_bills: list[dict],
    all_bills: dict,
    date_str: str,
) -> str:
    """
    Plain-text fallback for email clients that don't render HTML.
    Multipart/alternative emails include both; the client chooses which to show.
    """
    # Build analysis summary for plaintext
    ad = _get_analysis_data(all_bills)
    analysis_lines: list[str] = []
    if ad["total_analyzed"]:
        analysis_lines += [
            "=" * 50,
            "HOUSING POLICY ANALYSIS",
            "=" * 50,
            f"Bills analyzed       : {ad['total_analyzed']}",
            f"Relevant to mission  : {len(ad['relevant'])}",
            f"High-interest (2+)   : {len(ad['high_interest'])}",
            "",
            "Key Bills This Cycle:",
        ]
        for num, bill in ad["high_interest"][:8]:
            a = bill.get("analysis", {})
            crits = " ".join(
                f"[{_CRIT_LETTER[k]}]"
                for k in _CRIT_KEYS
                if a.get(k) in ("strong", "moderate")
            )
            analysis_lines.append(
                f"  {num} {crits} — {bill.get('title', '')[:60]}"
            )
        if ad["watch_list"]:
            analysis_lines += [
                "",
                "Watch List: " + ", ".join(n for n, _ in ad["watch_list"][:6]),
            ]
        analysis_lines.append("")

    lines = [
        "CA HOUSING POLICY INTELLIGENCE",
        f"Week of {date_str}",
        "California Stewardship Fund Legislative Tracker",
        "",
        f"New bills this week : {len(new_bills)}",
        f"Status changes      : {len(changed_bills)}",
        f"Total tracked       : {len(all_bills)}",
        "",
    ] + analysis_lines + [
        "=" * 50,
        "NEW BILLS",
        "=" * 50,
    ]

    if new_bills:
        for b in sorted(new_bills, key=lambda x: x.get("bill_number", "")):
            lines += [
                "",
                f"{b.get('bill_number')} — {b.get('title')}",
                f"Author: {b.get('author', 'N/A')}  |  Status: {b.get('status', '—')}",
            ]
            if b.get("summary"):
                lines.append(b["summary"][:200])
            if b.get("text_url"):
                lines.append(f"Text: {b['text_url']}")
    else:
        lines.append("No new bills this week.")

    lines += ["", "=" * 50, "STATUS CHANGES", "=" * 50]

    if changed_bills:
        for b in changed_bills:
            lines += [
                "",
                f"{b.get('bill_number')} — {b.get('title')}",
                f"Was:  {b.get('_prev_status', '—')}",
                f"Now:  {b.get('status', '—')}",
            ]
    else:
        lines.append("No status changes this week.")

    lines += [
        "",
        "=" * 50,
        "Generated by CSF Legislative Tracker",
        f"To configure: edit agents/legislative/config.yaml",
    ]

    return "\n".join(lines)
