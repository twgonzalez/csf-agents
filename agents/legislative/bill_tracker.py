#!/usr/bin/env python3
"""
California Legislative Bill Tracker
=====================================
Agent 1 of the California Stewardship Fund Intelligence System.

Monitors CA Legislature for housing-related bills and generates
weekly intelligence digests.

Pipeline:  fetch → process → store → report [→ email]

Data sources (tried in priority order):
  1. LegiScan API      (primary)   — 30K free queries/month, NPO discount available
     https://legiscan.com
  2. OpenStates API v3 (secondary) — 500 free queries/day, free key required
     https://v3.openstates.org
  3. CA LegInfo scraper (fallback) — no key required, less reliable
     https://leginfo.legislature.ca.gov

Usage:
    python agents/legislative/bill_tracker.py
    python agents/legislative/bill_tracker.py --demo
    python agents/legislative/bill_tracker.py --email
    python agents/legislative/bill_tracker.py --demo --email
    python agents/legislative/bill_tracker.py --config path/to/config.yaml
    python agents/legislative/bill_tracker.py --help

Environment variables:
    LEGISCAN_API_KEY     Free key from https://legiscan.com/legiscan (primary)
    OPENSTATES_API_KEY   Free key from https://openstates.org/accounts/signup/ (secondary)
    LEGISCAN_USER        legiscan.com login email (enables auto-download of weekly dataset ZIPs)
    LEGISCAN_PASSWORD    legiscan.com password   (enables auto-download of weekly dataset ZIPs)
    EMAIL_USER           Sending address (e.g. you@gmail.com)
    EMAIL_PASSWORD       Gmail App Password or SMTP password

    All credentials can be stored in a .env file at the project root.
    Copy .env.example to .env and fill in your values.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
import yaml
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
    load_dotenv()  # Load .env file if present (silently ignored if dotenv not installed)
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Path bootstrap — add project root to sys.path before importing agents.shared
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agents.shared.utils import (
    ensure_dir,
    http_get_with_retry,
    load_json,
    save_json,
    setup_logging,
)
from agents.legislative.email_sender import build_and_send_email, build_status_page

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = Path(__file__).parent / "config.yaml"
LEGISCAN_BASE_URL = "https://api.legiscan.com/"
OPENSTATES_BASE_URL = "https://v3.openstates.org"
LEGINFO_SEARCH_URL = "https://leginfo.legislature.ca.gov/faces/billSearchClient.xhtml"

# LegiScan numeric status codes → human-readable labels
LEGISCAN_STATUS = {
    1: "Introduced",
    2: "Engrossed",
    3: "Enrolled",
    4: "Passed",
    5: "Vetoed",
    6: "Failed",
    7: "Override",
    8: "Chaptered",
    9: "Refer",
    10: "Report Pass",
    11: "Report DNP",
    12: "Draft",
}


# ===========================================================================
# BillTracker
# ===========================================================================

class BillTracker:
    """
    Legislative bill tracking agent.

    Orchestrates the four-stage pipeline:

      Stage 1 — fetch:   Pull housing bills from the CA Legislature.
      Stage 2 — process: Detect new bills and status changes vs. stored data.
      Stage 3 — store:   Persist updated bills to JSON (read by other agents).
      Stage 4 — report:  Write a weekly markdown intelligence digest.

    Designed to run weekly (cron or manual). Safe to run multiple times;
    duplicate bills are deduped by bill number and history is preserved.

    Other agents consume data/bills/tracked_bills.json directly.
    """

    def __init__(self, config_path: Path = DEFAULT_CONFIG):
        self.config = self._load_config(config_path)
        self._setup_paths()
        self.logger = setup_logging(
            name="bill_tracker",
            level=self.config["logging"]["level"],
            log_file=self._log_file,
        )

    # -----------------------------------------------------------------------
    # Setup
    # -----------------------------------------------------------------------

    def _load_config(self, config_path: Path) -> dict:
        """Load and return YAML configuration."""
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _setup_paths(self) -> None:
        """Resolve all paths relative to project root; create directories."""
        root = PROJECT_ROOT
        self.bills_path: Path = root / self.config["paths"]["bills_file"]
        self.reports_dir: Path = root / self.config["paths"]["reports_dir"]

        log_file = self.config["logging"].get("file")
        self._log_file: Optional[Path] = (root / log_file) if log_file else None

        ensure_dir(self.bills_path.parent)
        ensure_dir(self.reports_dir)
        if self._log_file:
            ensure_dir(self._log_file.parent)

    # -----------------------------------------------------------------------
    # Main entry point
    # -----------------------------------------------------------------------

    def run(self, demo: bool = False, send_email: bool = False) -> Path:
        """
        Run the full pipeline. Returns the path to the generated report.

        Args:
            demo:       If True, skip live API calls and use built-in sample data.
            send_email: If True, send the HTML digest via SMTP after generating
                        the report. Requires EMAIL_USER and EMAIL_PASSWORD env vars,
                        plus recipients configured in config.yaml email.recipients.
        """
        self.logger.info("=" * 60)
        self.logger.info("CSF Legislative Bill Tracker — pipeline start")
        self.logger.info(f"Timestamp : {datetime.now().isoformat()}")
        self.logger.info(f"Mode      : {'DEMO' if demo else 'LIVE'}")
        self.logger.info(f"Bills file: {self.bills_path}")

        # ------------------------------------------------------------------
        # Stage 1: Fetch
        # ------------------------------------------------------------------
        if demo:
            fetched = self._demo_bills()
            self.logger.info(f"Demo mode: loaded {len(fetched)} sample bills")
        else:
            fetched = self._fetch()

        self.logger.info(f"Stage 1 complete — {len(fetched)} bills fetched")

        # ------------------------------------------------------------------
        # Stage 2: Process (diff against stored data)
        # ------------------------------------------------------------------
        stored = self._load_stored()
        new_bills, changed_bills, merged = self._process(fetched, stored)

        self.logger.info(
            f"Stage 2 complete — new: {len(new_bills)}, "
            f"changed: {len(changed_bills)}, total tracked: {len(merged)}"
        )

        # ------------------------------------------------------------------
        # Stage 3: Store
        # ------------------------------------------------------------------
        self._store(merged)
        self.logger.info("Stage 3 complete — bill data persisted")

        # ------------------------------------------------------------------
        # Stage 4: Report
        # ------------------------------------------------------------------
        report_path = self._report(new_bills, changed_bills, merged)
        self.logger.info(f"Stage 4 complete — report: {report_path}")

        # ------------------------------------------------------------------
        # Stage 5: Email (optional)
        # ------------------------------------------------------------------
        email_sent = False
        if send_email:
            self.logger.info("Stage 5: sending email digest")
            email_sent = build_and_send_email(
                new_bills=new_bills,
                changed_bills=changed_bills,
                all_bills=merged,
                config=self.config,
                logger=self.logger,
            )

        # ------------------------------------------------------------------
        # Stage 6: Build web status page (docs/index.html for GitHub Pages)
        # ------------------------------------------------------------------
        page_path = self._build_status_page(new_bills, changed_bills, merged)
        if page_path:
            self.logger.info(f"Stage 6 complete — status page: {page_path}")

        self.logger.info("=" * 60)

        # Print summary for the terminal
        print(f"\n{'=' * 55}")
        print(f"  CSF Legislative Tracker — scan complete")
        print(f"{'=' * 55}")
        print(f"  New bills      : {len(new_bills)}")
        print(f"  Status changes : {len(changed_bills)}")
        print(f"  Total tracked  : {len(merged)}")
        print(f"  Report         : {report_path.relative_to(PROJECT_ROOT)}")
        print(f"  Bill data      : {self.bills_path.relative_to(PROJECT_ROOT)}")
        if page_path:
            print(f"  Status page    : {page_path.relative_to(PROJECT_ROOT)}")
        if send_email:
            status = "sent" if email_sent else "FAILED (check logs)"
            env_recip = os.environ.get("EMAIL_RECIPIENTS", "").strip()
            if env_recip:
                recipients = [r.strip() for r in env_recip.split(",") if r.strip()]
            else:
                recipients = [r for r in self.config["email"].get("recipients", []) if r]
            print(f"  Email          : {status} → {', '.join(recipients)}")
        print(f"{'=' * 55}\n")

        return report_path

    # -----------------------------------------------------------------------
    # Stage 1: Fetch
    # -----------------------------------------------------------------------

    def _fetch(self) -> list[dict]:
        """
        Fetch housing bills from the CA Legislature.

        Tries data sources in priority order, falling through on failure:
          1. LegiScan API         — primary (30K free queries/month, most reliable)
          2. OpenStates API v3    — secondary (500 free queries/day)
          3. LegiScan Dataset ZIP — bridge (weekly ZIP from legiscan.com/CA/datasets,
                                    only requires login not approved API key)
          4. leginfo scraper      — last resort (no key, very limited metadata)
        """
        # --- LegiScan API (primary) ---
        legiscan_key = (
            os.environ.get("LEGISCAN_API_KEY")
            or self.config["data_source"].get("legiscan_api_key", "")
        ).strip()

        if legiscan_key:
            self.logger.info("Fetching via LegiScan API (primary)")
            try:
                bills = self._fetch_legiscan(legiscan_key)
                self.logger.info(f"LegiScan returned {len(bills)} bills")
                return bills
            except Exception as exc:
                self.logger.warning(
                    f"LegiScan fetch failed: {exc}. Trying OpenStates next."
                )
        else:
            self.logger.info(
                "No LEGISCAN_API_KEY set — skipping LegiScan API. "
                "Get a free key at https://legiscan.com/legiscan"
            )

        # --- OpenStates API (secondary) ---
        openstates_key = (
            os.environ.get("OPENSTATES_API_KEY")
            or self.config["data_source"].get("openstates_api_key", "")
        ).strip()

        if openstates_key:
            self.logger.info("Fetching via OpenStates API (secondary)")
            try:
                bills = self._fetch_openstates(openstates_key)
                self.logger.info(f"OpenStates returned {len(bills)} bills")
                return bills
            except Exception as exc:
                self.logger.warning(
                    f"OpenStates fetch failed: {exc}. Trying LegiScan dataset ZIP next."
                )
        else:
            self.logger.info(
                "No OPENSTATES_API_KEY set — skipping OpenStates. "
                "Get a free key at https://openstates.org/accounts/signup/"
            )

        # --- LegiScan Dataset ZIP (bridge — no approved API key needed) ---
        # Try to download the latest ZIP automatically if credentials are in .env
        self._download_latest_zip()
        zip_path = self._find_dataset_zip()
        if zip_path:
            self.logger.info(f"Fetching via LegiScan dataset ZIP: {zip_path.name}")
            try:
                bills = self._fetch_legiscan_dataset(zip_path)
                self.logger.info(f"LegiScan dataset returned {len(bills)} bills")
                return bills
            except Exception as exc:
                self.logger.warning(
                    f"LegiScan dataset ZIP failed: {exc}. Falling back to leginfo scraper."
                )
        else:
            self.logger.info(
                "No LegiScan dataset ZIP found — skipping. "
                "Download a ZIP from https://legiscan.com/CA/datasets "
                "(free login required) and place it in data/legiscan/"
            )

        # --- leginfo scraper (last resort — disabled by default) ---
        # leginfo returns all session bills regardless of keyword GET params (JSF site).
        # Even with post-filtering it produces unreliable results with no status_date,
        # causing all results to appear as "new" every run.
        # Only enable explicitly via config: data_source.use_leginfo_fallback: true
        if self.config["data_source"].get("use_leginfo_fallback", False):
            self.logger.info("Fetching via leginfo.legislature.ca.gov scraper (last resort)")
            return self._fetch_leginfo()

        self.logger.warning(
            "No data source available (LegiScan API key not set, no dataset ZIP found). "
            "Options:\n"
            "  1. Add LEGISCAN_USER + LEGISCAN_PASSWORD to .env for auto-download\n"
            "  2. Manually download a ZIP from https://legiscan.com/CA/datasets "
            "and place it in data/legiscan/\n"
            "  3. Set LEGISCAN_API_KEY once approved\n"
            "Skipping fetch — existing tracked bills unchanged."
        )
        return []

    # ------------------------------------------------------------------
    # LegiScan API
    # ------------------------------------------------------------------

    def _fetch_legiscan(self, api_key: str) -> list[dict]:
        """
        Fetch bills from LegiScan API v1.

        Query strategy (minimises API quota usage):
          1. getSessionList  — find the current CA session_id (1 query)
          2. getMasterList   — fetch all bills with title + status_date (1 query)
          3. Filter locally  — date window AND keyword match against title
          4. getBill         — only for filtered bills (N queries, typically 20-60)

        This means a full weekly run consumes ~2 + N queries, well within
        the 30,000/month free tier.

        LegiScan API docs: https://legiscan.com/legiscan
        Free tier: 30,000 queries/month; NPO discount available on paid tiers.
        """
        http_cfg = self.config["http"]
        lookback_days = self.config["legislative"]["lookback_days"]
        since_date = (datetime.now() - timedelta(days=lookback_days)).date()
        keywords = [kw.lower() for kw in self.config["keywords"]["housing"]]

        def legiscan_get(op: str, extra_params: dict = {}) -> dict:
            resp = http_get_with_retry(
                LEGISCAN_BASE_URL,
                params={"key": api_key, "op": op, **extra_params},
                timeout=http_cfg["timeout"],
                max_retries=http_cfg["max_retries"],
                retry_delay=http_cfg["retry_delay"],
                logger=self.logger,
            )
            data = resp.json()
            if data.get("status") != "OK":
                raise RuntimeError(
                    f"LegiScan {op} returned status: {data.get('status')} — "
                    f"{data.get('alert', {}).get('message', '')}"
                )
            return data

        # Step 1: find the current CA session
        self.logger.debug("LegiScan: fetching CA session list")
        session_data = legiscan_get("getSessionList", {"state": "CA"})
        sessions = session_data.get("sessions", [])

        # Current session: active=1, or the most recent one by year
        active = [s for s in sessions if s.get("active") == 1]
        if not active:
            active = sorted(sessions, key=lambda s: s.get("year_start", 0), reverse=True)
        if not active:
            raise RuntimeError("LegiScan: no CA sessions found")
        session_id = active[0]["session_id"]
        session_name = active[0].get("session_name", "")
        self.logger.debug(f"LegiScan: using session {session_id} ({session_name})")

        # Step 2: fetch full masterlist (one query for all CA bills)
        self.logger.debug("LegiScan: fetching masterlist")
        master_data = legiscan_get("getMasterList", {"id": session_id})
        masterlist: dict = master_data.get("masterlist", {})

        # Step 3: filter locally — date + keyword against title
        candidate_ids: list[int] = []
        for key, entry in masterlist.items():
            if key == "session" or not isinstance(entry, dict):
                continue

            # Date filter
            status_date_str = entry.get("status_date") or entry.get("last_action_date", "")
            if status_date_str:
                try:
                    entry_date = datetime.strptime(status_date_str, "%Y-%m-%d").date()
                    if entry_date < since_date:
                        continue
                except ValueError:
                    pass

            # Keyword filter against title (case-insensitive)
            title_lower = (entry.get("title") or "").lower()
            last_action_lower = (entry.get("last_action") or "").lower()
            if any(kw in title_lower or kw in last_action_lower for kw in keywords):
                bill_id = entry.get("bill_id")
                if bill_id:
                    candidate_ids.append(int(bill_id))

        self.logger.debug(
            f"LegiScan: masterlist has {len(masterlist) - 1} bills; "
            f"{len(candidate_ids)} match date + keyword filters"
        )

        # Step 4: getBill for each candidate
        bills: list[dict] = []
        for bill_id in candidate_ids:
            try:
                bill_data = legiscan_get("getBill", {"id": bill_id})
                raw = bill_data.get("bill", {})
                bill = self._normalize_legiscan(raw, session_name)
                if bill["bill_number"]:
                    bills.append(bill)
                time.sleep(0.2)  # be a polite API consumer
            except Exception as exc:
                self.logger.warning(f"LegiScan getBill failed for ID {bill_id}: {exc}")

        return bills

    def _normalize_legiscan(self, raw: dict, session_name: str = "") -> dict:
        """
        Map a LegiScan bill object to the canonical CSF bill schema.

        LegiScan provides richer hearing/calendar data than OpenStates,
        and the state_link field points directly to the authoritative
        leginfo.legislature.ca.gov URL.
        """
        # Primary sponsor (sponsor_type_id == 1)
        sponsors = raw.get("sponsors", [])
        primary = next((s["name"] for s in sponsors if s.get("sponsor_type_id") == 1), "")
        if not primary and sponsors:
            primary = sponsors[0].get("name", "")

        # Prefer state_link (leginfo URL) over legiscan URL for text_url
        state_link = raw.get("state_link", "")
        texts = raw.get("texts", [])
        if not state_link and texts:
            state_link = texts[-1].get("state_link", "") or texts[-1].get("url", "")

        # History → actions (last 10, most recent first from LegiScan)
        history = raw.get("history", [])
        recent_actions = [
            {
                "date": h.get("date", ""),
                "description": h.get("action", ""),
                "chamber": "Assembly" if h.get("chamber") == "H" else
                           "Senate" if h.get("chamber") == "S" else
                           h.get("chamber", ""),
            }
            for h in history[-10:]
        ]

        # Introduced date = first history entry
        introduced_date = history[0].get("date", "") if history else ""

        # Committees: current committee + referral history
        committees: list[str] = []
        committee = raw.get("committee", {})
        if isinstance(committee, dict) and committee.get("name"):
            committees.append(committee["name"])
        for ref in raw.get("referrals", []):
            name = ref.get("name", "")
            if name and name not in committees:
                committees.append(name)

        # Upcoming hearings from calendar entries
        today = datetime.now().date()
        upcoming_hearings = []
        for event in raw.get("calendar", []):
            event_date_str = event.get("date", "")
            try:
                event_date = datetime.strptime(event_date_str, "%Y-%m-%d").date()
                if event_date >= today:
                    upcoming_hearings.append({
                        "date": event_date_str,
                        "committee": event.get("description", ""),
                        "location": event.get("location", ""),
                    })
            except ValueError:
                pass

        # Subjects
        subjects = [s["subject_name"] for s in raw.get("subjects", [])]

        # Status: use last_action text (human-readable) with status code as fallback
        status = raw.get("last_action", "") or LEGISCAN_STATUS.get(raw.get("status", 0), "")

        return {
            "bill_number": raw.get("bill_number", ""),
            "session": session_name or raw.get("session", {}).get("session_name", ""),
            "title": raw.get("title", ""),
            "author": primary,
            "status": status,
            "status_date": raw.get("status_date", ""),
            "introduced_date": introduced_date,
            "last_updated": raw.get("status_date", ""),
            "text_url": state_link,
            "summary": (raw.get("description") or "")[:600],
            "subjects": subjects,
            "committees": committees,
            "upcoming_hearings": upcoming_hearings,
            "actions": recent_actions,
            "source": "legiscan",
            "source_id": str(raw.get("bill_id", "")),
        }

    # ------------------------------------------------------------------
    # LegiScan Dataset ZIP (bridge — no approved API key needed)
    # ------------------------------------------------------------------

    def _find_dataset_zip(self) -> Optional[Path]:
        """
        Locate a LegiScan CA dataset ZIP to process.

        Search order:
          1. Explicit path in config: data_source.legiscan_dataset_zip
          2. Auto-discover: newest CA_*.zip in data/legiscan/
        """
        # 1. Explicit config path
        cfg_path = self.config["data_source"].get("legiscan_dataset_zip", "").strip()
        if cfg_path:
            p = Path(cfg_path)
            if not p.is_absolute():
                p = PROJECT_ROOT / p
            if p.exists() and p.is_file():
                return p
            self.logger.warning(f"legiscan_dataset_zip configured but not found: {p}")

        # 2. Auto-discover newest CA_*.zip in data/legiscan/
        legiscan_dir = PROJECT_ROOT / "data" / "legiscan"
        if legiscan_dir.is_dir():
            zips = sorted(
                legiscan_dir.glob("CA_*.zip"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if zips:
                return zips[0]

        return None

    def _download_latest_zip(self) -> Optional[Path]:
        """
        Attempt to download the latest CA dataset ZIP from legiscan.com.

        Requires LEGISCAN_USER and LEGISCAN_PASSWORD in a .env file (or environment).
        Silently returns None if credentials are missing or the download fails,
        so the caller can fall back to any cached ZIP already on disk.

        Flow:
          1. POST login to legiscan.com (Drupal form, CSRF tokens extracted automatically)
          2. GET legiscan.com/CA/datasets — find the current session ZIP link
          3. Skip download if we already have that exact file
          4. Stream-download to data/legiscan/ (typically ~20-50 MB)
        """
        user = os.environ.get("LEGISCAN_USER", "").strip()
        password = os.environ.get("LEGISCAN_PASSWORD", "").strip()

        if not user or not password:
            return None  # credentials not configured — silent skip

        legiscan_dir = PROJECT_ROOT / "data" / "legiscan"
        ensure_dir(legiscan_dir)

        try:
            sess = requests.Session()
            sess.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/121.0.0.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            })

            # Step 1: GET login page to harvest Drupal CSRF tokens
            # Brief pause helps avoid Cloudflare rate limiting on cloud IPs
            self.logger.info("LegiScan auto-download: authenticating")
            time.sleep(2)
            resp = sess.get("https://legiscan.com/user/login", timeout=30)
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "lxml")
            form = (
                soup.find("form", {"id": "user-login-form"})
                or soup.find("form", id=lambda x: x and "login" in x.lower())
                or soup.find("form")
            )
            if not form:
                raise RuntimeError("Login form not found on legiscan.com/user/login")

            # Collect all hidden fields (Drupal form_build_id, form_token, etc.)
            post_data: dict = {}
            for inp in form.find_all("input"):
                name = inp.get("name", "")
                value = inp.get("value", "")
                if name:
                    post_data[name] = value

            post_data["name"] = user
            post_data["pass"] = password
            post_data["op"] = "Log in"

            # Step 2: POST login
            action = form.get("action") or "/user/login"
            if not action.startswith("http"):
                action = "https://legiscan.com" + action

            resp = sess.post(action, data=post_data, timeout=30, allow_redirects=True)
            resp.raise_for_status()

            # Verify login — a failed login stays on the login page
            if "/user/login" in resp.url:
                raise RuntimeError(
                    "Login failed — verify LEGISCAN_USER and LEGISCAN_PASSWORD in your .env file"
                )

            self.logger.info("LegiScan auto-download: authenticated, scanning datasets page")

            # Step 3: GET the CA datasets page
            resp = sess.get("https://legiscan.com/CA/datasets", timeout=30)
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "lxml")

            # Step 4: Find the ZIP download link
            # Pattern A — direct ZIP link:  href contains "CA_" and ".zip"
            # Pattern B — redirect link:     href is "/CA/dataset/…" or similar
            zip_url: Optional[str] = None
            zip_name: Optional[str] = None

            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "CA_" in href and ".zip" in href:
                    # Direct link — extract filename immediately
                    zip_url = href if href.startswith("http") else "https://legiscan.com" + href
                    zip_name = Path(href.split("?")[0]).name
                    break
                if "/CA/dataset/" in href:
                    # Indirect/redirect link — filename resolved after following
                    zip_url = href if href.startswith("http") else "https://legiscan.com" + href
                    break

            if not zip_url:
                raise RuntimeError(
                    "Could not find a CA dataset download link on legiscan.com/CA/datasets"
                )

            # Step 5: Skip if we already have this file
            if zip_name:
                dest_path = legiscan_dir / zip_name
                if dest_path.exists():
                    self.logger.info(
                        f"LegiScan auto-download: already have {zip_name}, skipping"
                    )
                    return dest_path

            # Step 6: Stream-download the ZIP
            self.logger.info("LegiScan auto-download: downloading ZIP (this may take a moment)")
            with sess.get(zip_url, stream=True, timeout=180) as dl_resp:
                dl_resp.raise_for_status()

                # Resolve filename if we didn't get it from the link href
                if not zip_name:
                    cd = dl_resp.headers.get("Content-Disposition", "")
                    if "filename=" in cd:
                        zip_name = cd.split("filename=")[-1].strip('"; ')
                    else:
                        zip_name = Path(dl_resp.url.split("?")[0]).name or "CA_dataset.zip"

                dest_path = legiscan_dir / zip_name

                # Skip if already on disk (resolved after following redirect)
                if dest_path.exists():
                    self.logger.info(
                        f"LegiScan auto-download: already have {zip_name}, skipping"
                    )
                    return dest_path

                with open(dest_path, "wb") as f:
                    for chunk in dl_resp.iter_content(chunk_size=1024 * 1024):  # 1 MB chunks
                        f.write(chunk)

            size_mb = dest_path.stat().st_size / (1024 * 1024)
            self.logger.info(
                f"LegiScan auto-download: saved {zip_name} ({size_mb:.1f} MB)"
            )
            return dest_path

        except Exception as exc:
            self.logger.warning(
                f"LegiScan auto-download failed: {exc} — using cached ZIP if available"
            )
            return None

    def _fetch_legiscan_dataset(self, zip_path: Path) -> list[dict]:
        """
        Process a LegiScan weekly dataset ZIP downloaded from legiscan.com/CA/datasets.

        This is the bridge method for when you have a free LegiScan account (login
        required to download) but your API key hasn't been approved yet.

        How to get the ZIP:
          1. Create a free account at https://legiscan.com/legiscan
          2. Go to https://legiscan.com/CA/datasets
          3. Download the current session ZIP (e.g. CA_2025-2026_XXXXXX.zip)
          4. Place it in data/legiscan/ — auto-discovered on next run
          5. Re-download each Sunday for fresh data

        ZIP structure (LegiScan format):
          CA_2025-2026_XXXXXX/
            bill/
              AB1.json    ← same format as getBill API response
              SB1.json
              ...

        Filtering:
          - Same keyword + date window logic as the API path
          - Uses the existing _normalize_legiscan() for schema consistency
        """
        lookback_days = self.config["legislative"]["lookback_days"]
        since_date = (datetime.now() - timedelta(days=lookback_days)).date()
        keywords = [kw.lower() for kw in self.config["keywords"]["housing"]]
        session_name = self.config["legislative"]["session"]

        bills: list[dict] = []
        processed = 0
        matched = 0

        with zipfile.ZipFile(zip_path, "r") as zf:
            bill_entries = [n for n in zf.namelist() if "/bill/" in n and n.endswith(".json")]
            self.logger.debug(f"Dataset ZIP '{zip_path.name}': {len(bill_entries)} bill JSON files")

            for entry in bill_entries:
                try:
                    wrapper = json.loads(zf.read(entry))
                    raw = wrapper.get("bill") if isinstance(wrapper, dict) else None
                    if not raw or not isinstance(raw, dict):
                        continue

                    processed += 1

                    # Date filter
                    date_str = raw.get("status_date") or raw.get("last_action_date", "")
                    if date_str:
                        try:
                            if datetime.strptime(date_str, "%Y-%m-%d").date() < since_date:
                                continue
                        except ValueError:
                            pass

                    # Keyword filter against title + last_action + description
                    title_lower = (raw.get("title") or "").lower()
                    last_action_lower = (raw.get("last_action") or "").lower()
                    description_lower = (raw.get("description") or "").lower()
                    if not any(
                        kw in title_lower or kw in last_action_lower or kw in description_lower
                        for kw in keywords
                    ):
                        continue

                    bill = self._normalize_legiscan(raw, session_name)
                    if bill["bill_number"]:
                        bills.append(bill)
                        matched += 1

                except Exception as exc:
                    self.logger.debug(f"Skipping ZIP entry '{entry}': {exc}")

        self.logger.debug(
            f"Dataset ZIP: processed {processed} bills, "
            f"{matched} matched date + keyword filters"
        )
        return bills

    # ------------------------------------------------------------------
    # OpenStates API
    # ------------------------------------------------------------------

    def _fetch_openstates(self, api_key: str) -> list[dict]:
        """
        Fetch bills from OpenStates API v3.

        Searches each configured keyword and paginates through results.
        Deduplicates by bill number before returning.

        API docs: https://docs.openstates.org/api-v3/
        Rate limits: ~1000 requests/day on free tier.
        """
        cfg = self.config
        lookback_days = cfg["legislative"]["lookback_days"]
        since = (
            datetime.now(timezone.utc) - timedelta(days=lookback_days)
        ).strftime("%Y-%m-%dT00:00:00Z")

        keywords: list[str] = cfg["keywords"]["housing"]
        http_cfg = cfg["http"]
        headers = {"X-API-KEY": api_key}

        seen: dict[str, dict] = {}  # bill_number → normalized bill

        for keyword in keywords:
            self.logger.debug(f"OpenStates query: '{keyword}' (since {since[:10]})")
            page = 1

            while True:
                params = {
                    "jurisdiction": "ca",
                    "updated_since": since,
                    "q": keyword,
                    "per_page": 20,
                    "page": page,
                    "include": ["abstracts", "actions", "sponsorships", "sources"],
                }

                try:
                    resp = http_get_with_retry(
                        f"{OPENSTATES_BASE_URL}/bills",
                        params=params,
                        headers=headers,
                        timeout=http_cfg["timeout"],
                        max_retries=http_cfg["max_retries"],
                        retry_delay=http_cfg["retry_delay"],
                        logger=self.logger,
                    )
                    data = resp.json()
                except Exception as exc:
                    self.logger.warning(f"OpenStates failed for '{keyword}' page {page}: {exc}")
                    break

                for raw in data.get("results", []):
                    bill = self._normalize_openstates(raw)
                    num = bill["bill_number"]
                    if num and num not in seen:
                        seen[num] = bill

                pagination = data.get("pagination", {})
                if page >= pagination.get("max_page", 1):
                    break
                page += 1
                time.sleep(0.3)  # be a polite API consumer

        self.logger.debug(f"OpenStates: {len(seen)} unique bills after deduplication")
        return list(seen.values())

    def _normalize_openstates(self, raw: dict) -> dict:
        """
        Map an OpenStates API bill object to the canonical CSF bill schema.

        The CSF schema is shared across all intelligence agents so that
        downstream agents (pattern analyzer, newsletter composer, etc.)
        can read from tracked_bills.json without format translation.

        Schema fields:
            bill_number       str   "AB 1234"
            session           str   "2025-2026"
            title             str   Bill title
            author            str   Primary sponsor name
            status            str   Latest action description
            status_date       str   Date of latest action (ISO date)
            introduced_date   str   Date first created (ISO date)
            last_updated      str   ISO datetime of last API update
            text_url          str   URL to full bill text on leginfo
            summary           str   Bill abstract (truncated to 600 chars)
            subjects          list  Subject tags from OpenStates
            committees        list  Committee names extracted from actions
            upcoming_hearings list  [{date, committee, location}, ...]
            actions           list  Last 10 actions [{date, description, chamber}]
            source            str   "openstates" | "leginfo" | "demo"
            source_id         str   Original ID in the source system
            first_seen        str   ISO datetime this bill entered our system
        """
        sponsorships = raw.get("sponsorships", [])
        primary_author = next(
            (s["name"] for s in sponsorships if s.get("primary")), ""
        )
        if not primary_author and sponsorships:
            primary_author = sponsorships[0].get("name", "")

        actions: list[dict] = raw.get("actions", [])
        recent_actions = [
            {
                "date": a.get("date", ""),
                "description": a.get("description", ""),
                "chamber": (
                    a.get("organization", {}).get("name", "")
                    if isinstance(a.get("organization"), dict)
                    else ""
                ),
            }
            for a in actions[-10:]
        ]

        # Extract committees from action related_entities
        committees: list[str] = []
        for action in actions:
            for entity in action.get("related_entities", []):
                if entity.get("type") == "committee":
                    name = entity.get("name", "")
                    if name and name not in committees:
                        committees.append(name)

        sources = raw.get("sources", [])
        text_url = sources[0]["url"] if sources else ""

        abstracts = raw.get("abstracts", [])
        summary = abstracts[0].get("abstract", "") if abstracts else ""

        return {
            "bill_number": raw.get("identifier", ""),
            "session": raw.get("session", ""),
            "title": raw.get("title", ""),
            "author": primary_author,
            "status": raw.get("latest_action_description", ""),
            "status_date": raw.get("latest_action_date", ""),
            "introduced_date": (raw.get("created_at") or "")[:10],
            "last_updated": raw.get("updated_at", ""),
            "text_url": text_url,
            "summary": summary[:600] if summary else "",
            "subjects": raw.get("subject", []),
            "committees": committees,
            "upcoming_hearings": [],   # not always available via API
            "actions": recent_actions,
            "source": "openstates",
            "source_id": raw.get("id", ""),
        }

    # ------------------------------------------------------------------
    # leginfo scraper (fallback)
    # ------------------------------------------------------------------

    def _fetch_leginfo(self) -> list[dict]:
        """
        Scrape bills from leginfo.legislature.ca.gov.

        Uses the bill text search endpoint which accepts keyword params via GET.
        Less reliable than OpenStates (site structure may change), but requires
        no API key.

        Note: The CA Legislature site uses JavaServer Faces (JSF). This scraper
        targets the text search page which is more amenable to simple GET requests.
        If the structure changes, update _parse_leginfo_results().
        """
        self.logger.info("Scraping leginfo.legislature.ca.gov (fallback mode)")

        keywords: list[str] = self.config["keywords"]["housing"]
        session_year = self.config["legislative"]["session"].replace("-", "")  # "20252026"
        http_cfg = self.config["http"]

        seen: dict[str, dict] = {}

        for keyword in keywords[:8]:  # Limit to avoid overloading the public server
            self.logger.debug(f"leginfo search: '{keyword}'")
            try:
                resp = http_get_with_retry(
                    LEGINFO_SEARCH_URL,
                    params={
                        "keywords": keyword,
                        "session_year": session_year,
                    },
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (compatible; CSF-LegTracker/1.0; "
                            "+https://github.com/california-stewardship-fund)"
                        )
                    },
                    timeout=http_cfg["timeout"],
                    max_retries=http_cfg["max_retries"],
                    retry_delay=http_cfg["retry_delay"],
                    logger=self.logger,
                )
                bills = self._parse_leginfo_results(resp.text, session_year)
                for bill in bills:
                    num = bill["bill_number"]
                    if num and num not in seen:
                        seen[num] = bill
                self.logger.debug(f"leginfo '{keyword}': {len(bills)} bills")
            except Exception as exc:
                self.logger.warning(f"leginfo scrape failed for '{keyword}': {exc}")

            time.sleep(1.0)  # be respectful of the public server

        # leginfo's JSF form ignores GET keyword params — it returns all session bills.
        # Apply the same keyword filter we use for the ZIP and API paths.
        keywords_lower = [kw.lower() for kw in keywords]
        filtered = {
            num: bill for num, bill in seen.items()
            if any(
                kw in (bill.get("title", "") + " " + bill.get("status", "")).lower()
                for kw in keywords_lower
            )
        }
        self.logger.info(
            f"leginfo scraper: {len(seen)} raw bills, "
            f"{len(filtered)} after keyword filter"
        )
        return list(filtered.values())

    def _parse_leginfo_results(self, html: str, session_year: str) -> list[dict]:
        """
        Parse bill rows from leginfo HTML search results.

        The leginfo search results page renders a table of bills. We look for
        anchor tags pointing to bill detail pages, then extract surrounding cells.
        This parsing is intentionally defensive — returns empty list on failure.
        """
        bills = []
        soup = BeautifulSoup(html, "lxml")

        # Find all links to bill pages (href contains "bill_id=")
        bill_links = soup.find_all(
            "a", href=lambda h: h and "bill_id=" in str(h)
        )

        for link in bill_links:
            try:
                bill_num = link.get_text(strip=True)
                if not bill_num:
                    continue

                href = link.get("href", "")
                text_url = (
                    f"https://leginfo.legislature.ca.gov{href}"
                    if href.startswith("/")
                    else href
                )

                # Walk up to the containing row to get other cells
                row = link.find_parent("tr")
                if row:
                    cells = row.find_all("td")
                    author = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                    title = cells[2].get_text(strip=True) if len(cells) > 2 else ""
                    status = cells[3].get_text(strip=True) if len(cells) > 3 else ""
                else:
                    author = title = status = ""

                session_display = f"{session_year[:4]}-{session_year[4:]}"

                bills.append({
                    "bill_number": bill_num,
                    "session": session_display,
                    "title": title,
                    "author": author,
                    "status": status,
                    "status_date": "",
                    "introduced_date": "",
                    "last_updated": datetime.now().isoformat(),
                    "text_url": text_url,
                    "summary": "",
                    "subjects": [],
                    "committees": [],
                    "upcoming_hearings": [],
                    "actions": [],
                    "source": "leginfo",
                    "source_id": bill_num,
                })
            except Exception:
                continue

        return bills

    # -----------------------------------------------------------------------
    # Stage 2: Process
    # -----------------------------------------------------------------------

    def _process(
        self,
        fetched: list[dict],
        stored: dict,
    ) -> tuple[list[dict], list[dict], dict]:
        """
        Compare freshly fetched bills against stored bills.

        Logic:
          - Bill in fetched but NOT in stored  → new_bill
          - Bill in both, status changed       → changed_bill (adds _prev_status key)
          - Merge all into updated dict        → merged (preserves first_seen)

        Args:
            fetched: Bills returned from the fetch stage.
            stored:  Dict loaded from tracked_bills.json.

        Returns:
            (new_bills, changed_bills, merged_dict)
        """
        existing = stored.get("bills", {})
        new_bills: list[dict] = []
        changed_bills: list[dict] = []
        merged = dict(existing)  # start with everything we already have

        for bill in fetched:
            num = bill["bill_number"]
            if not num:
                continue

            if num not in existing:
                new_bills.append(bill)
                self.logger.info(f"[NEW]     {num}: {bill.get('title', '')[:70]}")
            else:
                old_status = existing[num].get("status", "")
                new_status = bill.get("status", "")
                if old_status != new_status and new_status:
                    changed_bills.append({**bill, "_prev_status": old_status})
                    self.logger.info(
                        f"[CHANGED] {num}: '{old_status}' → '{new_status}'"
                    )

            # Always update with latest data, but preserve first_seen
            merged[num] = {
                **bill,
                "first_seen": existing.get(num, {}).get(
                    "first_seen", datetime.now().isoformat()
                ),
            }

        return new_bills, changed_bills, merged

    # -----------------------------------------------------------------------
    # Stage 3: Store
    # -----------------------------------------------------------------------

    def _load_stored(self) -> dict:
        """Load existing bill data from JSON. Returns empty structure if missing."""
        if not self.bills_path.exists():
            self.logger.info("No existing bill data — starting fresh")
            return {"last_updated": None, "bills": {}}

        data = load_json(self.bills_path, logger=self.logger)
        n = len(data.get("bills", {}))
        self.logger.info(f"Loaded {n} stored bills from {self.bills_path.name}")
        return data

    def _store(self, merged: dict) -> None:
        """Persist all bill data to JSON storage."""
        payload = {
            "last_updated": datetime.now().isoformat(),
            "agent": "legislative_tracker",
            "schema_version": "1.0",
            "description": (
                "CA Legislature housing bills tracked by CSF Legislative Tracker. "
                "Read by pattern_analyzer, newsletter_composer, and other agents."
            ),
            "total_bills": len(merged),
            "bills": merged,
        }
        save_json(payload, self.bills_path, logger=self.logger)
        self.logger.info(f"Saved {len(merged)} bills → {self.bills_path.name}")

    # -----------------------------------------------------------------------
    # Stage 4: Report
    # -----------------------------------------------------------------------

    def _report(
        self,
        new_bills: list[dict],
        changed_bills: list[dict],
        all_bills: dict,
    ) -> Path:
        """Generate markdown report and write to outputs/weekly_reports/."""
        date_str = datetime.now().strftime("%Y-%m-%d")
        content = self._render_report(date_str, new_bills, changed_bills, all_bills)

        path = self.reports_dir / f"legislative_{date_str}.md"
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        return path

    def _render_report(
        self,
        date_str: str,
        new_bills: list[dict],
        changed_bills: list[dict],
        all_bills: dict,
    ) -> str:
        """
        Render the full weekly markdown report.

        Report sections:
          1. Header + At a Glance table
          2. New Bills This Week (detailed)
          3. Status Changes (before/after)
          4. Upcoming Hearings
          5. Full Bill Index (table)
        """
        lookback = self.config["legislative"]["lookback_days"]
        lines: list[str] = []

        # ---- Header --------------------------------------------------------
        lines += [
            "# California Housing Policy — Legislative Intelligence Digest",
            f"## Week of {date_str}",
            "",
            (
                f"> **Prepared by:** California Stewardship Fund Legislative Tracker  \n"
                f"> **Scan completed:** {datetime.now().strftime('%Y-%m-%d %H:%M')} PST  \n"
                f"> **Lookback window:** {lookback} days  \n"
                f"> **Total bills tracked:** {len(all_bills)}"
            ),
            "",
            "---",
            "",
        ]

        # ---- At a Glance ---------------------------------------------------
        lines += [
            "## At a Glance",
            "",
            "| | Count |",
            "|---|:---:|",
            f"| 🆕 New bills this week | **{len(new_bills)}** |",
            f"| 🔄 Status changes | **{len(changed_bills)}** |",
            f"| 📋 Total bills tracked | **{len(all_bills)}** |",
            "",
        ]

        # ---- New Bills -----------------------------------------------------
        lines += ["---", "", f"## New Bills This Week ({len(new_bills)})", ""]

        if new_bills:
            for bill in sorted(new_bills, key=lambda b: b.get("bill_number", "")):
                lines += self._render_bill_block(bill)
        else:
            lines += [
                f"*No new housing-related bills found in the last {lookback} days.*",
                "",
            ]

        # ---- Status Changes ------------------------------------------------
        lines += ["---", "", f"## Status Changes ({len(changed_bills)})", ""]

        if changed_bills:
            for bill in changed_bills:
                prev = bill.get("_prev_status") or "—"
                lines += [
                    f"### {bill['bill_number']} — {bill.get('title', '')}",
                    "",
                    f"- **Author:** {bill.get('author', 'N/A')}",
                    f"- **Previous status:** ~~{prev}~~",
                    f"- **New status:** {bill.get('status', '—')}",
                ]
                if bill.get("status_date"):
                    lines.append(f"- **Date:** {bill['status_date']}")
                if bill.get("text_url"):
                    lines.append(f"- **Text:** [{bill['text_url']}]({bill['text_url']})")
                lines.append("")
        else:
            lines += ["*No status changes on tracked bills this week.*", ""]

        # ---- Upcoming Hearings ---------------------------------------------
        hearings: list[dict] = []
        for num, bill in all_bills.items():
            for h in bill.get("upcoming_hearings", []):
                hearings.append({
                    **h,
                    "bill_number": num,
                    "bill_title": bill.get("title", ""),
                })
        hearings.sort(key=lambda h: h.get("date", "9999-99-99"))

        lines += ["---", "", f"## Upcoming Hearings ({len(hearings)})", ""]

        if hearings:
            for h in hearings[:15]:  # cap at 15 to keep report readable
                committee = h.get("committee", "Committee TBD")
                location = f" — {h['location']}" if h.get("location") else ""
                lines.append(
                    f"- **{h.get('date', 'TBD')}** | "
                    f"{h['bill_number']}: {h['bill_title'][:55]} | "
                    f"{committee}{location}"
                )
            lines.append("")
        else:
            lines += ["*No upcoming hearings found in tracking data.*", ""]

        # ---- Full Bill Index -----------------------------------------------
        lines += [
            "---",
            "",
            "## Full Bill Index",
            "",
            "| Bill | Author | Status | Title |",
            "|------|--------|--------|-------|",
        ]

        for num in sorted(all_bills.keys()):
            b = all_bills[num]
            url = b.get("text_url", "")
            bill_cell = f"[{num}]({url})" if url else num
            author = (b.get("author") or "")[:22]
            status = (b.get("status") or "")[:38]
            title = (b.get("title") or "")[:52]
            lines.append(f"| {bill_cell} | {author} | {status} | {title} |")

        lines += [
            "",
            "---",
            "",
            "*This report was automatically generated by the CSF Legislative Tracker.*  ",
            f"*Data: LegiScan (legiscan.com) | Generated: {datetime.now().isoformat()}*",
            "",
        ]

        return "\n".join(lines)

    def _render_bill_block(self, bill: dict) -> list[str]:
        """Render a detailed markdown block for a single bill."""
        lines = [
            f"### {bill.get('bill_number', '')} — {bill.get('title', '')}",
            "",
        ]

        # Key metadata fields
        for label, key in [
            ("Author", "author"),
            ("Introduced", "introduced_date"),
            ("Status", "status"),
            ("Session", "session"),
        ]:
            val = bill.get(key, "")
            if val:
                lines.append(f"**{label}:** {val}  ")

        if bill.get("committees"):
            lines.append(f"**Committees:** {', '.join(bill['committees'])}  ")

        lines.append("")

        if bill.get("summary"):
            lines.append(f"> {bill['summary'][:400]}")
            lines.append("")

        if bill.get("subjects"):
            tags = " · ".join(bill["subjects"][:6])
            lines.append(f"*Subjects: {tags}*  ")

        if bill.get("text_url"):
            lines.append(f"[View Full Text]({bill['text_url']})")

        lines.append("")
        return lines

    # -----------------------------------------------------------------------
    # Stage 6: Web status page
    # -----------------------------------------------------------------------

    def _build_status_page(
        self,
        new_bills: list[dict],
        changed_bills: list[dict],
        all_bills: dict,
    ) -> Optional[Path]:
        """
        Build docs/index.html — a public status dashboard for GitHub Pages.

        Written every run so the page always reflects the latest data.
        GitHub Pages serves it at: https://<user>.github.io/<repo>/

        To enable GitHub Pages:
          Settings → Pages → Source: Deploy from a branch →
          Branch: main, Folder: /docs → Save

        Returns the output Path on success, None on failure.
        """
        output_path = PROJECT_ROOT / "docs" / "index.html"
        try:
            page_path = build_status_page(
                new_bills=new_bills,
                changed_bills=changed_bills,
                all_bills=all_bills,
                config=self.config,
                output_path=output_path,
            )
            self.logger.info(
                f"Status page written → {page_path.relative_to(PROJECT_ROOT)}"
            )
            return page_path
        except Exception as exc:
            self.logger.warning(f"Status page generation failed: {exc}")
            return None

    # -----------------------------------------------------------------------
    # Demo data
    # -----------------------------------------------------------------------

    def _demo_bills(self) -> list[dict]:
        """
        Return realistic sample housing bills for demo/testing.

        Covers a representative range:
          - Brand new bill (day 0)
          - Bill in committee (day 5)
          - Bill with a hearing scheduled
          - Bill that has passed a floor vote (would trigger status change on re-run)
        """
        today = datetime.now()

        def d(days_ago: int) -> str:
            return (today - timedelta(days=days_ago)).strftime("%Y-%m-%d")

        def future(days: int) -> str:
            return (today + timedelta(days=days)).strftime("%Y-%m-%d")

        return [
            {
                "bill_number": "AB 1234",
                "session": "2025-2026",
                "title": "Residential Zoning: By-Right Approval for Multifamily Housing",
                "author": "Wicks",
                "status": "Introduced",
                "status_date": d(0),
                "introduced_date": d(0),
                "last_updated": today.isoformat(),
                "text_url": (
                    "https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml"
                    "?bill_id=202520260AB1234"
                ),
                "summary": (
                    "Would require cities and counties to approve by-right any housing "
                    "development on sites already zoned for multifamily residential use. "
                    "Prohibits imposition of subjective design standards or additional "
                    "environmental review beyond what is required by state law. "
                    "Applies to projects with at least 10% affordable units."
                ),
                "subjects": ["Housing", "Zoning", "Land Use", "Local Government"],
                "committees": ["Assembly Housing and Community Development Committee"],
                "upcoming_hearings": [
                    {
                        "date": future(14),
                        "committee": "Assembly Housing and Community Development Committee",
                        "location": "State Capitol, Room 447",
                    }
                ],
                "actions": [
                    {"date": d(0), "description": "Introduced", "chamber": "Assembly"},
                ],
                "source": "demo",
                "source_id": "demo_ab1234",
            },
            {
                "bill_number": "SB 567",
                "session": "2025-2026",
                "title": "ADU Development: Streamlined Permitting and Fee Caps",
                "author": "Wiener",
                "status": "Referred to Committee on Housing",
                "status_date": d(2),
                "introduced_date": d(6),
                "last_updated": today.isoformat(),
                "text_url": (
                    "https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml"
                    "?bill_id=202520260SB567"
                ),
                "summary": (
                    "Streamlines ADU permitting by capping impact fees at $1,000, "
                    "requiring ministerial approval within 30 days for all qualifying "
                    "ADU applications, and prohibiting owner-occupancy requirements "
                    "for properties with ADU rentals."
                ),
                "subjects": ["ADU", "Housing", "Permitting", "Fees"],
                "committees": ["Senate Committee on Housing"],
                "upcoming_hearings": [],
                "actions": [
                    {"date": d(6), "description": "Introduced", "chamber": "Senate"},
                    {
                        "date": d(2),
                        "description": "Referred to Committee on Housing",
                        "chamber": "Senate",
                    },
                ],
                "source": "demo",
                "source_id": "demo_sb567",
            },
            {
                "bill_number": "AB 890",
                "session": "2025-2026",
                "title": "Housing Element Compliance: Enhanced State Enforcement",
                "author": "Alvarez",
                "status": "Introduced",
                "status_date": d(3),
                "introduced_date": d(3),
                "last_updated": today.isoformat(),
                "text_url": (
                    "https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml"
                    "?bill_id=202520260AB890"
                ),
                "summary": (
                    "Strengthens enforcement mechanisms for cities and counties that fail "
                    "to comply with state housing element law. Imposes escalating financial "
                    "penalties on non-compliant jurisdictions and authorizes the state to "
                    "override local zoning where a jurisdiction is out of compliance with "
                    "its RHNA allocation."
                ),
                "subjects": ["Housing", "Local Government", "Enforcement", "RHNA"],
                "committees": [],
                "upcoming_hearings": [],
                "actions": [
                    {
                        "date": d(3),
                        "description": "Introduced",
                        "chamber": "Assembly",
                    },
                ],
                "source": "demo",
                "source_id": "demo_ab890",
            },
            {
                "bill_number": "SB 234",
                "session": "2025-2026",
                "title": "Density Bonus Law: Expansion of Affordability Incentives",
                "author": "Caballero",
                "status": "Passed Assembly Housing Committee — Ayes 8, Noes 2",
                "status_date": d(1),
                "introduced_date": d(45),
                "last_updated": today.isoformat(),
                "text_url": (
                    "https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml"
                    "?bill_id=202520260SB234"
                ),
                "summary": (
                    "Expands California's Density Bonus Law to increase the maximum density "
                    "bonus from 50% to 100% for projects where at least 24% of units are "
                    "deed-restricted affordable. Removes local design standard requirements "
                    "that conflict with density bonus entitlements."
                ),
                "subjects": ["Density Bonus", "Affordable Housing", "Zoning", "Land Use"],
                "committees": [
                    "Senate Committee on Housing",
                    "Assembly Housing and Community Development Committee",
                    "Assembly Appropriations Committee",
                ],
                "upcoming_hearings": [
                    {
                        "date": future(7),
                        "committee": "Assembly Appropriations Committee",
                        "location": "State Capitol, Room 4202",
                    }
                ],
                "actions": [
                    {"date": d(45), "description": "Introduced", "chamber": "Senate"},
                    {
                        "date": d(35),
                        "description": "Passed Senate Committee on Housing — Ayes 7, Noes 1",
                        "chamber": "Senate",
                    },
                    {
                        "date": d(22),
                        "description": "Passed Senate Floor — Ayes 28, Noes 10",
                        "chamber": "Senate",
                    },
                    {
                        "date": d(14),
                        "description": "Referred to Assembly Housing Committee",
                        "chamber": "Assembly",
                    },
                    {
                        "date": d(1),
                        "description": "Passed Assembly Housing Committee — Ayes 8, Noes 2",
                        "chamber": "Assembly",
                    },
                ],
                "source": "demo",
                "source_id": "demo_sb234",
            },
            {
                "bill_number": "AB 456",
                "session": "2025-2026",
                "title": "Local Control: Ministerial Approval for Infill Housing",
                "author": "Lee",
                "status": "Introduced",
                "status_date": d(4),
                "introduced_date": d(4),
                "last_updated": today.isoformat(),
                "text_url": (
                    "https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml"
                    "?bill_id=202520260AB456"
                ),
                "summary": (
                    "Requires ministerial, non-discretionary approval for qualifying infill "
                    "housing projects in urbanized areas. Prohibits local governments from "
                    "applying design review, conditional use permits, or variances to "
                    "projects meeting objective standards."
                ),
                "subjects": ["Housing", "Infill", "Ministerial Approval", "Local Control"],
                "committees": [],
                "upcoming_hearings": [],
                "actions": [
                    {"date": d(4), "description": "Introduced", "chamber": "Assembly"},
                ],
                "source": "demo",
                "source_id": "demo_ab456",
            },
        ]


# ===========================================================================
# CLI entry point
# ===========================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "CSF Legislative Bill Tracker — monitors CA Legislature housing bills "
            "and generates weekly intelligence digests."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python agents/legislative/bill_tracker.py
  python agents/legislative/bill_tracker.py --demo
  python agents/legislative/bill_tracker.py --config agents/legislative/config.yaml

environment variables:
  OPENSTATES_API_KEY   Free API key from https://openstates.org/accounts/signup/
        """,
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run with sample data instead of live API calls (no API key needed)",
    )
    parser.add_argument(
        "--email",
        action="store_true",
        help=(
            "Send the HTML digest via email after generating the report. "
            "Requires EMAIL_USER and EMAIL_PASSWORD env vars and recipients "
            "configured in config.yaml."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Path to config YAML file (default: {DEFAULT_CONFIG})",
    )
    args = parser.parse_args()

    tracker = BillTracker(config_path=args.config)
    tracker.run(demo=args.demo, send_email=args.email)


if __name__ == "__main__":
    main()
