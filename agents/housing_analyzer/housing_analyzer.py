#!/usr/bin/env python3
"""
Housing Policy Bill Analyzer — Local Control Risk Edition
==========================================================
Agent 2 of the California Stewardship Fund Intelligence System.

Reads tracked CA housing bills from the bill tracker's data file and scores
each bill against four local control risk criteria aligned with CSF's mission:

  A. Local Control Override       — State preemption of local zoning/planning
  B. Removes Discretionary Review — Eliminates CEQA, design review, hearings
  C. Mandates Development         — Forces density/quotas beyond local choice
  D. Infrastructure & Capacity Burden — Fee restrictions, improvement limits, capacity cost-shifting

Analysis results are stored back into tracked_bills.json (one "analysis" block
per bill). The agent runs incrementally — only newly added bills or bills whose
status has changed since last analysis are re-evaluated, keeping API costs low.

Pipeline:  load → screen → analyze → store → report

Outputs:
  data/bills/tracked_bills.json             — updated with analysis fields
  outputs/analysis/housing_policy_analysis_YYYY-MM-DD.md   — full report
  outputs/analysis/housing_policy_summary_YYYY-MM-DD.md    — weekly digest section

Usage:
    python agents/housing_analyzer/housing_analyzer.py
    python agents/housing_analyzer/housing_analyzer.py --force
    python agents/housing_analyzer/housing_analyzer.py --summary-only
    python agents/housing_analyzer/housing_analyzer.py --bill AB1751
    python agents/housing_analyzer/housing_analyzer.py --help

Environment variables:
    ANTHROPIC_API_KEY   Required. Your Anthropic API key.

All credentials can be stored in a .env file at the project root.
Copy .env.example to .env and fill in your values.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]
import requests
import yaml
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Path bootstrap — add project root to sys.path before importing agents.shared
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agents.shared.utils import (
    ensure_dir,
    load_json,
    save_json,
    setup_logging,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = Path(__file__).parent / "config.yaml"
DEFAULT_MODEL = "claude-sonnet-4-6"
RATE_LIMIT_DELAY = 1.0   # seconds between Anthropic API calls
TEXT_FETCH_DELAY = 2.0   # seconds between leginfo page fetches

# Retry / backoff settings for transient Anthropic API errors (429, 500, 503, 529)
MAX_RETRIES      = 5      # maximum number of retry attempts after initial failure
RETRY_BASE_DELAY = 5.0   # seconds — doubles each attempt (exponential backoff)
RETRY_MAX_DELAY  = 120.0  # seconds — cap so we never wait longer than 2 minutes

SCORE_LABELS = {
    "strong":   "🔴 Strong Risk",
    "moderate": "🟠 Moderate Risk",
    "indirect": "🟡 Indirect Risk",
    "none":     "⬜ None",
}

# Criteria short labels for column headers
CRITERIA = {
    "pro_housing_production": "A: Local Control Override",
    "densification":          "B: Removes Disc. Review",
    "reduce_discretion":      "C: Mandates Development",
    "cost_to_cities":         "D: Infra & Capacity Burden",
}

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
You are a California policy analyst for the California Stewardship Fund (CSF).

CSF's core belief: the best decisions come from the people closest to them.
Local communities — cities, counties, and their residents — are best positioned
to determine what kind of housing, density, and development suits their neighborhoods.
State preemption of local planning authority removes accountability from the people
who will live with the consequences of those decisions.

Your task is to analyze California legislative bills for their RISK TO LOCAL CONTROL —
how much each bill undermines cities' and counties' authority over their own
planning, zoning, and land use decisions.

RISK CRITERIA:

A. Local Control Override
   Does the bill preempt, override, or restrict city/county zoning and land use authority?
   STRONG: Explicit state preemption — cities must approve projects regardless of local zoning
   MODERATE: Conditional or partial limitation on local authority
   INDIRECT: Implied erosion of local discretion (e.g., compliance pressure, unfunded mandates
             that force policy adoption)
   NONE: No impact on local zoning or planning authority
   Examples of STRONG: ministerial by-right approvals that override local zoning, explicit
   preemption of local development standards, Housing Crisis Act anti-downzoning provisions,
   state mandate to allow uses cities have not chosen to permit.

B. Removes Discretionary Review
   Does the bill strip cities of discretionary approval processes — CEQA review, design
   review, public hearings, conditional use permits, or similar local deliberation?
   STRONG: Mandates ministerial (no-discretion) approval, eliminates CEQA applicability,
           removes all public hearing or design review rights for qualifying projects
   MODERATE: Significantly limits conditions cities may apply, restricts hearing rights,
             or removes specific review categories
   INDIRECT: Reduces review scope or compresses timelines without full elimination
   NONE: No impact on local review authority
   Examples of STRONG: "ministerial approval" language, "exempt from CEQA" provisions,
   limits on objective standards cities may apply.

C. Mandates Development
   Does the bill force cities to approve density, housing types, or development volumes
   beyond what the community would choose through local deliberation?
   STRONG: Explicit mandates — cities must approve qualifying projects; builder's remedy
           provisions; required density increases; quotas that cities face penalties for
           not meeting
   MODERATE: Strong incentives or requirements that override local preference (density
             bonus expansions, RHNA increases, streamlining that is effectively mandatory)
   INDIRECT: Some pressure toward higher density without explicit mandate
   NONE: No mandate on what or how much cities must approve

D. Infrastructure & Capacity Burden
   Does the bill prevent cities from requiring developers to fund infrastructure, restrict
   local impact fee authority, shift capacity costs to existing residents, or mandate
   development that exceeds existing infrastructure capacity?
   STRONG: Explicitly eliminates or caps impact fees (water, sewer, traffic, schools); mandates
           approval on sites without capacity review; directly restricts improvement conditions
           cities may require; shifts substantial infrastructure costs to existing taxpayers
   MODERATE: Limits infrastructure conditions cities may impose; ministerial approvals that
             prevent requiring utility or frontage improvements; significant unfunded staffing
             or compliance obligations not offset by state funding
   INDIRECT: Some infrastructure cost pressure without explicit restriction (e.g., RHNA
             compliance costs, increased permitting load from mandated approvals)
   NONE: No meaningful infrastructure or capacity burden on local governments

SCORING SCALE:
  "strong"   — explicit and central; this is a major local control risk in the bill
  "moderate" — significant risk provision, but secondary to the bill's main purpose
  "indirect" — implied or likely risk, not explicit in bill language
  "none"     — no relevant risk identified

Be accurate. If a bill is genuinely low-risk to local control (e.g., a state program
with no local mandates, an insurance regulation, a workforce bill), score it "none"
across all criteria. Not every bill is a threat.

COMMS BRIEF: For bills scoring on 2 or more criteria at "strong" or "moderate",
write a comms_brief in the following format — plain text, no markdown headers:

[One sentence: what the bill does mechanically from a local control risk perspective.]
• [Specific local control risk 1]
• [Specific local control risk 2]
• [Specific local control risk 3]
• [Optional: additional risk if significant]
Recommended: [Specific action CSF should take — oppose, seek amendments, monitor, testify, etc.]

For bills scoring on 0–1 criteria at strong/moderate, comms_brief should be an empty string.
"""


# ---------------------------------------------------------------------------
# Tool definition for structured scoring output
# ---------------------------------------------------------------------------
SCORE_TOOL = {
    "name": "score_bill",
    "description": (
        "Record the housing policy scores for a California legislative bill. "
        "Call this tool once with the final assessment for all four criteria."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pro_housing_production": {
                "type": "string",
                "enum": ["strong", "moderate", "indirect", "none"],
                "description": (
                    "Risk score for Criterion A: Local Control Override. "
                    "How much does this bill preempt or restrict city/county zoning and "
                    "land use authority? strong=explicit state preemption, moderate=partial "
                    "limitation, indirect=implied erosion, none=no impact."
                ),
            },
            "densification": {
                "type": "string",
                "enum": ["strong", "moderate", "indirect", "none"],
                "description": (
                    "Risk score for Criterion B: Removes Discretionary Review. "
                    "Does this bill eliminate or limit CEQA review, public hearings, design "
                    "review, or conditional use permits? strong=ministerial/by-right mandate "
                    "removing all discretion, moderate=limits conditions or review scope, "
                    "indirect=compresses timelines or scope, none=no impact."
                ),
            },
            "reduce_discretion": {
                "type": "string",
                "enum": ["strong", "moderate", "indirect", "none"],
                "description": (
                    "Risk score for Criterion C: Mandates Development. "
                    "Does this bill force cities to approve density, housing types, or volumes "
                    "beyond local choice? strong=explicit mandate/builder's remedy/quota, "
                    "moderate=strong incentive effectively overriding local preference, "
                    "indirect=pressure without explicit mandate, none=no mandate."
                ),
            },
            "cost_to_cities": {
                "type": "string",
                "enum": ["strong", "moderate", "indirect", "none"],
                "description": (
                    "Risk score for Criterion D: Infrastructure & Capacity Burden. "
                    "Does this bill prevent cities from requiring infrastructure improvements, "
                    "restrict impact fee authority, shift capacity costs to existing residents, "
                    "or mandate development that exceeds infrastructure capacity without local "
                    "mitigation authority? strong=explicit fee elimination or improvement "
                    "restriction, moderate=limits infrastructure conditions or significant "
                    "unfunded compliance burden, indirect=minor capacity pressure, none=no burden."
                ),
            },
            "notes": {
                "type": "string",
                "description": (
                    "1–2 sentence technical summary explaining the local control risk scores. "
                    "Focus on the specific mechanism or language that drives each non-'none' "
                    "score. Written from a local control risk perspective."
                ),
            },
            "comms_brief": {
                "type": "string",
                "description": (
                    "For bills scoring on 2+ criteria at strong/moderate: write a comms_brief "
                    "in this exact plain-text format (no markdown headers):\n"
                    "[One sentence: what the bill does mechanically, local control risk lens.]\n"
                    "• [Specific local control risk 1]\n"
                    "• [Specific local control risk 2]\n"
                    "• [Specific local control risk 3]\n"
                    "Recommended: [Specific action CSF should take]\n\n"
                    "For bills scoring on 0–1 criteria at strong/moderate, return empty string."
                ),
            },
            "fetch_full_text": {
                "type": "boolean",
                "description": (
                    "Set true if the summary is too short or ambiguous to score confidently "
                    "and fetching the full bill text would materially change the analysis."
                ),
            },
        },
        "required": [
            "pro_housing_production",
            "densification",
            "reduce_discretion",
            "cost_to_cities",
            "notes",
            "comms_brief",
            "fetch_full_text",
        ],
    },
}


# ===========================================================================
# HousingAnalyzer
# ===========================================================================

class HousingAnalyzer:
    """
    Housing policy bill analyzer agent.

    Orchestrates the five-stage pipeline:

      Stage 1 — load:     Read all tracked bills from tracked_bills.json.
      Stage 2 — screen:   Identify which bills need (re-)analysis.
      Stage 3 — analyze:  Score each bill using Claude AI; fetch full text
                          if the model requests it.
      Stage 4 — store:    Write analysis results back to tracked_bills.json.
      Stage 5 — report:   Generate full analysis report + weekly summary.

    Analysis state is stored in each bill's "analysis" object within
    tracked_bills.json. The agent is incremental: bills already scored
    with the current status are skipped unless --force is passed.
    """

    def __init__(self, config_path: Path = DEFAULT_CONFIG):
        self.config = self._load_config(config_path)
        self._setup_paths()
        self.logger = setup_logging(
            name="housing_analyzer",
            level=self.config["logging"]["level"],
            log_file=self._log_file,
        )
        self._anthropic = (
            anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
            if anthropic is not None
            else None
        )
        self._model = self.config.get("model", DEFAULT_MODEL)

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
        self.analysis_dir: Path = root / self.config["paths"]["analysis_dir"]

        log_file = self.config["logging"].get("file")
        self._log_file: Optional[Path] = (root / log_file) if log_file else None

        ensure_dir(self.analysis_dir)
        if self._log_file:
            ensure_dir(self._log_file.parent)

    # -----------------------------------------------------------------------
    # Main entry point
    # -----------------------------------------------------------------------

    def run(
        self,
        force: bool = False,
        summary_only: bool = False,
        single_bill: Optional[str] = None,
    ) -> dict[str, Path]:
        """
        Run the full pipeline. Returns paths to generated output files.

        Args:
            force:        If True, re-analyze all bills (ignore cached analysis).
            summary_only: If True, skip analysis; regenerate reports from existing
                          analysis data only.
            single_bill:  If provided, analyze only this bill number (e.g. "AB1751").
        """
        self.logger.info("=" * 60)
        self.logger.info("CSF Housing Policy Analyzer — pipeline start")
        self.logger.info(f"Timestamp : {datetime.now().isoformat()}")
        mode = "SUMMARY-ONLY" if summary_only else ("FORCE" if force else "INCREMENTAL")
        self.logger.info(f"Mode      : {mode}")
        if single_bill:
            self.logger.info(f"Single    : {single_bill}")

        # ------------------------------------------------------------------
        # Stage 1: Load
        # ------------------------------------------------------------------
        data = load_json(self.bills_path, self.logger)
        if not data:
            self.logger.error(f"No data found at {self.bills_path}. Run bill_tracker first.")
            sys.exit(1)

        bills: dict = data.get("bills", {})
        self.logger.info(f"Loaded    : {len(bills)} bills from {self.bills_path}")

        if not summary_only:
            # ------------------------------------------------------------------
            # Stage 2: Screen — find bills needing analysis
            # ------------------------------------------------------------------
            to_analyze = self._bills_needing_analysis(bills, force, single_bill)
            self.logger.info(
                f"To analyze: {len(to_analyze)} bills "
                f"({'all' if force else 'new/changed/unanalyzed'})"
            )

            # ------------------------------------------------------------------
            # Stage 3 + 4: Analyze + Store (incremental)
            # ------------------------------------------------------------------
            newly_analyzed: list[str] = []
            for i, bill_num in enumerate(to_analyze, 1):
                bill = bills[bill_num]
                self.logger.info(
                    f"[{i}/{len(to_analyze)}] Analyzing {bill_num}: {bill.get('title', '')[:60]}"
                )
                try:
                    analysis = self._analyze_bill(bill)
                    bills[bill_num]["analysis"] = analysis
                    newly_analyzed.append(bill_num)

                    # Save incrementally after each bill
                    data["bills"] = bills
                    data["last_updated"] = datetime.now().isoformat()
                    save_json(data, self.bills_path, self.logger)

                    time.sleep(RATE_LIMIT_DELAY)
                except Exception as exc:
                    self.logger.error(f"Failed to analyze {bill_num}: {exc}")
                    # Continue with remaining bills rather than aborting
        else:
            newly_analyzed = []
            self.logger.info("Summary-only mode — skipping analysis")

        # ------------------------------------------------------------------
        # Stage 5: Report
        # ------------------------------------------------------------------
        date_str = datetime.now().strftime("%Y-%m-%d")
        analyzed_bills = {
            k: v for k, v in bills.items() if "analysis" in v
        }
        self.logger.info(
            f"Reporting : {len(analyzed_bills)} bills with analysis "
            f"({len(newly_analyzed)} newly analyzed this run)"
        )

        full_report_path = self._generate_full_report(bills, date_str)
        summary_path = self._generate_weekly_summary(bills, date_str, newly_analyzed)

        self.logger.info(f"Full report : {full_report_path}")
        self.logger.info(f"Weekly summary: {summary_path}")
        self.logger.info("CSF Housing Policy Analyzer — pipeline complete")
        self.logger.info("=" * 60)

        return {
            "full_report": full_report_path,
            "weekly_summary": summary_path,
        }

    # -----------------------------------------------------------------------
    # Stage 2: Screen
    # -----------------------------------------------------------------------

    def _bills_needing_analysis(
        self,
        bills: dict,
        force: bool,
        single_bill: Optional[str],
    ) -> list[str]:
        """
        Return list of bill numbers that need (re-)analysis.

        A bill needs analysis if:
          - force=True (re-analyze everything), OR
          - it has no "analysis" block yet, OR
          - its current status differs from status_at_analysis (status change
            may mean amendments or new committee text was added).
        """
        if single_bill:
            normalized = single_bill.replace(" ", "").upper()
            # Try both "AB1751" and "AB 1751" key formats
            key = next(
                (k for k in bills if k.replace(" ", "").upper() == normalized),
                None,
            )
            if not key:
                self.logger.warning(f"Bill {single_bill} not found in tracked bills")
                return []
            return [key]

        result = []
        for bill_num, bill in bills.items():
            if force:
                result.append(bill_num)
                continue

            existing = bill.get("analysis")
            if not existing:
                result.append(bill_num)
                continue

            # Re-analyze if status changed since last analysis
            current_status = bill.get("status", "")
            analyzed_status = existing.get("status_at_analysis", "")
            if current_status != analyzed_status:
                self.logger.debug(
                    f"{bill_num}: status changed {analyzed_status!r} → {current_status!r}, "
                    f"queueing for re-analysis"
                )
                result.append(bill_num)

        return result

    # -----------------------------------------------------------------------
    # Stage 3: Analyze
    # -----------------------------------------------------------------------

    def _analyze_bill(self, bill: dict) -> dict:
        """
        Score a single bill using the Claude API.

        Makes up to two API calls:
          1. First pass using title + summary.
          2. Second pass (if model requests it) using fetched full bill text.

        Returns the analysis dict to be stored in the bill record.
        """
        bill_num = bill.get("bill_number", "unknown")
        summary = bill.get("summary", "") or ""
        text_url = bill.get("text_url", "")

        # Build first-pass prompt
        user_prompt = self._build_prompt(bill, full_text=None)

        # First call: title + summary
        result = self._call_claude(user_prompt)

        # If model wants full text and we have a URL, fetch and re-analyze
        if result.get("fetch_full_text") and text_url:
            self.logger.info(f"  → Fetching full text for {bill_num} from leginfo")
            full_text = self._fetch_bill_text(text_url)
            if full_text:
                user_prompt_with_text = self._build_prompt(bill, full_text=full_text)
                result = self._call_claude(user_prompt_with_text)
                result["full_text_fetched"] = True
            else:
                self.logger.warning(f"  → Could not fetch full text for {bill_num}")
                result["full_text_fetched"] = False
        else:
            result["full_text_fetched"] = False

        # Add metadata
        result.pop("fetch_full_text", None)
        result["analyzed_date"] = datetime.now().strftime("%Y-%m-%d")
        result["status_at_analysis"] = bill.get("status", "")
        result["model"] = self._model

        return result

    def _build_prompt(self, bill: dict, full_text: Optional[str]) -> str:
        """Build the user-facing analysis prompt for a single bill."""
        lines = [
            f"Analyze this California legislative bill:",
            f"",
            f"Bill Number : {bill.get('bill_number', 'N/A')}",
            f"Title       : {bill.get('title', 'N/A')}",
            f"Author      : {bill.get('author', 'N/A')}",
            f"Status      : {bill.get('status', 'N/A')}",
            f"Session     : {bill.get('session', 'N/A')}",
            f"",
            f"Summary:",
            bill.get("summary", "(no summary available)") or "(no summary available)",
        ]

        if bill.get("actions"):
            lines += [
                "",
                "Recent Actions:",
            ]
            for action in bill["actions"][-5:]:  # Last 5 actions
                lines.append(f"  {action.get('date', '')} [{action.get('chamber', '')}] {action.get('description', '')}")

        if full_text:
            # Trim to avoid hitting token limits; first ~4000 chars of digest is usually enough
            trimmed = full_text[:4000]
            if len(full_text) > 4000:
                trimmed += "\n[... text truncated ...]"
            lines += [
                "",
                "Full Bill Text (digest):",
                trimmed,
            ]
        else:
            summary_len = len(bill.get("summary", "") or "")
            if summary_len < 100:
                lines += [
                    "",
                    f"NOTE: Summary is very short ({summary_len} chars). Consider requesting "
                    f"full text if the title suggests the bill may be relevant.",
                ]

        lines += [
            "",
            "Score this bill on all four criteria and provide concise notes. "
            "Use the score_bill tool to record your assessment.",
        ]

        return "\n".join(lines)

    def _call_claude(self, user_prompt: str) -> dict:
        """
        Call the Anthropic API with the scoring tool. Returns the tool input dict.

        Automatically retries on transient server-side errors (HTTP 429, 500, 503,
        529 Overloaded) using exponential backoff with ±20 % jitter.  After
        MAX_RETRIES exhausted the final exception is re-raised so the caller can
        log and skip to the next bill.
        """
        if self._anthropic is None:
            raise RuntimeError(
                "anthropic package is not installed. Run: pip install anthropic"
            )

        # Status codes that are safe to retry (transient server/capacity errors)
        RETRYABLE = {429, 500, 503, 529}

        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self._anthropic.messages.create(
                    model=self._model,
                    max_tokens=512,
                    system=SYSTEM_PROMPT,
                    tools=[SCORE_TOOL],
                    tool_choice={"type": "tool", "name": "score_bill"},
                    messages=[{"role": "user", "content": user_prompt}],
                )

                # Extract tool use block
                tool_use_block = next(
                    (b for b in response.content if b.type == "tool_use"),
                    None,
                )
                if not tool_use_block:
                    raise ValueError("Claude did not return a tool_use block")

                return dict(tool_use_block.input)

            except anthropic.APIStatusError as exc:
                if exc.status_code in RETRYABLE and attempt < MAX_RETRIES:
                    # Exponential backoff: 5s, 10s, 20s, 40s, 80s  (capped at 120s)
                    delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
                    # ±20 % jitter so parallel runs don't all retry simultaneously
                    delay *= 0.8 + 0.4 * random.random()
                    self.logger.warning(
                        f"  ↻ Claude API {exc.status_code} (attempt {attempt + 1}/"
                        f"{MAX_RETRIES + 1}) — retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
                    last_exc = exc
                else:
                    raise

        # All retries exhausted — surface the last error to the caller
        raise last_exc  # type: ignore[misc]

    # -----------------------------------------------------------------------
    # Full text fetching
    # -----------------------------------------------------------------------

    def _fetch_bill_text(self, text_url: str) -> Optional[str]:
        """
        Fetch and extract the bill digest/text from a leginfo.legislature.ca.gov page.

        Returns extracted text, or None on failure.
        """
        time.sleep(TEXT_FETCH_DELAY)
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (compatible; CSF-HousingAnalyzer/1.0; "
                    "+https://github.com/twgonzalez/csf-agents)"
                )
            }
            resp = requests.get(text_url, headers=headers, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            self.logger.warning(f"Failed to fetch {text_url}: {exc}")
            return None

        soup = BeautifulSoup(resp.text, "lxml")

        # Try common leginfo content containers in priority order
        for selector in [
            "#bill_all",
            ".bill-digest",
            "#bill_digest",
            "div.bill-text",
            "div#content",
        ]:
            el = soup.select_one(selector)
            if el:
                return el.get_text(separator="\n", strip=True)

        # Fallback: grab the largest <div> by text length
        divs = soup.find_all("div")
        if divs:
            best = max(divs, key=lambda d: len(d.get_text()))
            text = best.get_text(separator="\n", strip=True)
            if len(text) > 200:
                return text

        return None

    # -----------------------------------------------------------------------
    # Stage 5: Reports
    # -----------------------------------------------------------------------

    def _generate_full_report(self, bills: dict, date_str: str) -> Path:
        """
        Write the full analysis markdown report.

        Includes: executive summary, full scoring matrix (all bills),
        detailed write-ups for high-interest bills, per-criterion lists,
        and a watch list.
        """
        path = self.analysis_dir / f"housing_policy_analysis_{date_str}.md"

        analyzed = {k: v for k, v in bills.items() if "analysis" in v}
        unanalyzed_count = len(bills) - len(analyzed)

        # Sort by number of criteria met (descending), then bill number
        def score_count(bill_item):
            analysis = bill_item[1].get("analysis", {})
            return sum(
                1 for key in CRITERIA
                if analysis.get(key) in ("strong", "moderate")
            )

        sorted_bills = sorted(analyzed.items(), key=score_count, reverse=True)

        # Classify bills
        high_interest = [
            (num, b) for num, b in sorted_bills
            if score_count((num, b)) >= 2
        ]
        by_criterion: dict[str, list] = {k: [] for k in CRITERIA}
        for bill_num, bill in sorted_bills:
            analysis = bill.get("analysis", {})
            for crit_key in CRITERIA:
                if analysis.get(crit_key) in ("strong", "moderate"):
                    by_criterion[crit_key].append((bill_num, bill))

        watch_list = [
            (num, b) for num, b in sorted_bills
            if b.get("analysis", {}).get("cost_to_cities") in ("strong", "moderate")
            and score_count((num, b)) < 2
        ]

        lines = [
            "# CSF Housing Policy Bill Analysis",
            f"*Generated: {date_str}*  ",
            f"*Bills analyzed: {len(analyzed)} of {len(bills)} tracked "
            f"({'%d unanalyzed' % unanalyzed_count if unanalyzed_count else 'all analyzed'})*",
            "",
            "---",
            "",
            "## Executive Summary",
            "",
        ]

        # Executive summary bullets
        total_relevant = len([
            b for _, b in sorted_bills
            if score_count((_, b)) >= 1
        ])
        lines.append(
            f"- **{len(analyzed)}** bills analyzed; "
            f"**{total_relevant}** scored on at least one housing policy criterion."
        )
        lines.append(
            f"- **{len(high_interest)}** bills scored on 2 or more criteria "
            f"(high-interest for CSF advocacy)."
        )
        for crit_key, crit_label in CRITERIA.items():
            count = len(by_criterion[crit_key])
            if count > 0:
                lines.append(f"- **{count}** bills touch {crit_label}.")
        if watch_list:
            lines.append(
                f"- **{len(watch_list)}** bills flagged on the Watch List "
                f"(cost shifts or restrictions without offsetting housing gains)."
            )
        lines += [
            "",
            "---",
            "",
            "## Scoring Matrix",
            "",
            "| Bill | Title | Author | A: Pro-Housing | B: Density | C: Reduce Discretion | D: Cost to Cities | Notes |",
            "|------|-------|--------|:-:|:-:|:-:|:-:|-------|",
        ]

        for bill_num, bill in sorted_bills:
            analysis = bill.get("analysis", {})
            title = (bill.get("title") or "")[:50]
            if len(bill.get("title", "") or "") > 50:
                title += "…"
            author = bill.get("author", "")
            text_url = bill.get("text_url", "")
            bill_link = f"[{bill_num}]({text_url})" if text_url else bill_num

            scores = [
                SCORE_LABELS.get(analysis.get(k, "none"), "❌ None")
                for k in CRITERIA
            ]
            notes = (analysis.get("notes", "") or "").replace("|", "—")[:120]
            lines.append(
                f"| {bill_link} | {title} | {author} | "
                + " | ".join(scores)
                + f" | {notes} |"
            )

        # Unanalyzed bills (if any)
        if unanalyzed_count > 0:
            lines += [
                "",
                f"*{unanalyzed_count} bills not yet analyzed — run without --summary-only to analyze.*",
            ]

        lines += [
            "",
            "**Legend:**",
            "✅ Strong — explicit, primary purpose of the bill  ",
            "✅ Moderate — significant provision, secondary to main purpose  ",
            "⚠️ Indirect — implied or likely consequence, not explicit  ",
            "❌ None — no relevant language found",
            "",
            "---",
            "",
            "## Bills of High Interest (2+ Criteria)",
            "",
        ]

        if high_interest:
            for bill_num, bill in high_interest:
                analysis = bill.get("analysis", {})
                text_url = bill.get("text_url", "")
                bill_link = f"[{bill_num}]({text_url})" if text_url else bill_num
                lines += [
                    f"### {bill_link} — {bill.get('title', '')}",
                    f"**Author:** {bill.get('author', '')}  ",
                    f"**Status:** {bill.get('status', '')}  ",
                    "",
                    "| Criterion | Score |",
                    "|-----------|-------|",
                ]
                for crit_key, crit_label in CRITERIA.items():
                    score = SCORE_LABELS.get(analysis.get(crit_key, "none"), "❌ None")
                    lines.append(f"| {crit_label} | {score} |")
                comms = analysis.get("comms_brief", "") or ""
                notes = analysis.get("notes", "") or ""
                lines += [""]
                if comms:
                    lines += [f"**Strategic Brief:** {comms}", ""]
                if notes:
                    lines += [f"*Scoring rationale: {notes}*", ""]
                lines.append("")
        else:
            lines.append("*No bills scored on 2 or more criteria in this analysis.*")
            lines.append("")

        lines += [
            "---",
            "",
            "## Bills by Criterion",
            "",
        ]

        for crit_key, crit_label in CRITERIA.items():
            lines += [
                f"### {crit_label}",
                "",
            ]
            criterion_bills = by_criterion[crit_key]
            if criterion_bills:
                for bill_num, bill in criterion_bills:
                    analysis = bill.get("analysis", {})
                    score = SCORE_LABELS.get(analysis.get(crit_key, "none"), "❌ None")
                    text_url = bill.get("text_url", "")
                    bill_link = f"[{bill_num}]({text_url})" if text_url else bill_num
                    notes = analysis.get("notes", "") or ""
                    # One-sentence rationale: use notes up to the first sentence
                    rationale = notes.split(".")[0].strip() + "." if notes else ""
                    lines.append(
                        f"- **{bill_link}** ({bill.get('author', '')}) — {bill.get('title', '')}  "
                        f"*{score}* — {rationale}"
                    )
            else:
                lines.append("*No bills scored on this criterion.*")
            lines.append("")

        lines += [
            "---",
            "",
            "## Watch List — Potential Concern",
            "",
            "Bills that impose costs on cities or may restrict housing in unexpected ways.",
            "",
        ]

        if watch_list:
            for bill_num, bill in watch_list:
                analysis = bill.get("analysis", {})
                text_url = bill.get("text_url", "")
                bill_link = f"[{bill_num}]({text_url})" if text_url else bill_num
                lines += [
                    f"- **{bill_link}** — {bill.get('title', '')} ({bill.get('author', '')})  ",
                    f"  {analysis.get('notes', '')}",
                    "",
                ]
        else:
            lines.append("*No bills flagged on the watch list.*")

        lines += [
            "",
            "---",
            f"*Report generated by CSF Housing Policy Analyzer on {date_str}*",
        ]

        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def _generate_weekly_summary(
        self,
        bills: dict,
        date_str: str,
        newly_analyzed: list[str],
    ) -> Path:
        """
        Generate a concise weekly summary section for inclusion in the weekly report.

        This is intentionally short — 1–2 pages maximum — covering only the
        most policy-relevant findings. Designed to be embedded into the
        legislative weekly digest.
        """
        path = self.analysis_dir / f"housing_policy_summary_{date_str}.md"

        analyzed = {k: v for k, v in bills.items() if "analysis" in v}

        def score_count(bill_item):
            analysis = bill_item[1].get("analysis", {})
            return sum(
                1 for key in CRITERIA
                if analysis.get(key) in ("strong", "moderate")
            )

        sorted_bills = sorted(analyzed.items(), key=score_count, reverse=True)
        high_interest = [(n, b) for n, b in sorted_bills if score_count((n, b)) >= 2]

        # Strong bills per criterion
        strong_by_crit: dict[str, list] = {k: [] for k in CRITERIA}
        for bill_num, bill in sorted_bills:
            analysis = bill.get("analysis", {})
            for crit_key in CRITERIA:
                if analysis.get(crit_key) == "strong":
                    strong_by_crit[crit_key].append((bill_num, bill))

        total_relevant = sum(1 for item in sorted_bills if score_count(item) >= 1)

        lines = [
            "## Housing Policy Analysis",
            f"*{date_str} | {len(analyzed)} bills analyzed*",
            "",
        ]

        # Quick stats row
        lines += [
            "| | |",
            "|---|---|",
            f"| Bills analyzed | **{len(analyzed)}** |",
            f"| Bills relevant to CSF mission | **{total_relevant}** |",
            f"| High-interest bills (2+ criteria) | **{len(high_interest)}** |",
            f"| Newly analyzed this run | **{len(newly_analyzed)}** |",
            "",
        ]

        if high_interest:
            lines += [
                "### Key Bills to Watch",
                "",
            ]
            for bill_num, bill in high_interest[:10]:  # Cap at 10 for brevity
                analysis = bill.get("analysis", {})
                text_url = bill.get("text_url", "")
                bill_link = f"[{bill_num}]({text_url})" if text_url else bill_num

                # Build compact criteria badges
                badges = []
                for crit_key, crit_short in [
                    ("pro_housing_production", "A"),
                    ("densification", "B"),
                    ("reduce_discretion", "C"),
                    ("cost_to_cities", "D"),
                ]:
                    score = analysis.get(crit_key, "none")
                    if score == "strong":
                        badges.append(f"**{crit_short}**")
                    elif score == "moderate":
                        badges.append(crit_short)
                criteria_str = " ".join(badges) if badges else "—"

                lines += [
                    f"**{bill_link}** — {bill.get('title', '')}  ",
                    f"*{bill.get('author', '')} | {bill.get('status', '')} | Criteria: {criteria_str}*  ",
                    f"{analysis.get('notes', '')}",
                    "",
                ]

        # Per-criterion highlights (strong scores only, max 5 per criterion)
        lines += [
            "### By Criterion (Strong scores)",
            "",
        ]
        any_strong = False
        for crit_key, crit_label in CRITERIA.items():
            strong_bills = strong_by_crit[crit_key][:5]
            if strong_bills:
                any_strong = True
                lines.append(f"**{crit_label}:**")
                for bill_num, bill in strong_bills:
                    text_url = bill.get("text_url", "")
                    bill_link = f"[{bill_num}]({text_url})" if text_url else bill_num
                    lines.append(
                        f"- {bill_link} — {bill.get('title', '')} ({bill.get('author', '')})"
                    )
                lines.append("")

        if not any_strong:
            lines.append("*No bills received a Strong score on any criterion this cycle.*")
            lines.append("")

        # Newly analyzed callout
        if newly_analyzed:
            lines += [
                "### Newly Analyzed This Run",
                "",
                f"{len(newly_analyzed)} bills were analyzed for the first time "
                f"(or re-analyzed due to status change):  ",
                ", ".join(newly_analyzed[:20]),
            ]
            if len(newly_analyzed) > 20:
                lines.append(f"… and {len(newly_analyzed) - 20} more.")
            lines.append("")

        lines += [
            "---",
            f"*Full analysis: `outputs/analysis/housing_policy_analysis_{date_str}.md`*",
        ]

        path.write_text("\n".join(lines), encoding="utf-8")
        return path


# ===========================================================================
# CLI entry point
# ===========================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="CSF Housing Policy Bill Analyzer — analyzes tracked CA bills "
                    "against four housing policy criteria using Claude AI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python agents/housing_analyzer/housing_analyzer.py
  python agents/housing_analyzer/housing_analyzer.py --force
  python agents/housing_analyzer/housing_analyzer.py --summary-only
  python agents/housing_analyzer/housing_analyzer.py --bill AB1751
        """,
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-analyze all bills, ignoring existing analysis data.",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Skip analysis; regenerate reports from existing analysis data only.",
    )
    parser.add_argument(
        "--bill",
        metavar="BILL_NUMBER",
        help="Analyze a single bill (e.g. AB1751 or 'AB 1751').",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=str(DEFAULT_CONFIG),
        help=f"Path to config YAML (default: {DEFAULT_CONFIG})",
    )
    args = parser.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY") and not args.summary_only:
        print(
            "ERROR: ANTHROPIC_API_KEY environment variable is not set.\n"
            "Set it in your .env file or export it before running:\n"
            "  export ANTHROPIC_API_KEY=sk-ant-...\n"
            "Or use --summary-only to regenerate reports without calling the API.",
            file=sys.stderr,
        )
        sys.exit(1)

    analyzer = HousingAnalyzer(config_path=Path(args.config))
    analyzer.run(
        force=args.force,
        summary_only=args.summary_only,
        single_bill=args.bill,
    )


if __name__ == "__main__":
    main()
