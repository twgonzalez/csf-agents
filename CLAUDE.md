# CLAUDE.md — CSF Intelligence Agents
## Codebase snapshot for AI-assisted development

**Last updated:** 2026-02-24
**Repo:** https://github.com/twgonzalez/csf-agents
**Live dashboard:** https://twgonzalez.github.io/csf-agents/

---

## What This System Does

Multi-agent Python system for the **California Stewardship Fund (CSF)** — a conservative-leaning policy organization whose core belief is that **the best decisions come from people closest to them (local control)**. The system monitors California housing legislation and assesses bills for their risk to local government authority.

**Two active agents:**
1. **`agents/legislative/`** — Tracks 124+ CA housing bills weekly via LegiScan, detects new bills and status changes, generates markdown reports and HTML email digests
2. **`agents/housing_analyzer/`** — Analyzes bills against CSF's 4-criterion local control risk framework using Claude (Anthropic API); stores results back into `tracked_bills.json`

**Automation:** GitHub Actions (`weekly_tracker.yml`) runs every Monday at 6 AM PT, pulls fresh LegiScan data, commits updated `tracked_bills.json` + `docs/index.html`, and emails the digest.

---

## Project Structure

```
csf-agents/
├── agents/
│   ├── legislative/
│   │   ├── bill_tracker.py        # Main tracker agent — fetch/process/store/report
│   │   ├── email_sender.py        # HTML email + GitHub Pages status page builder
│   │   └── config.yaml            # Keywords, lookback window, paths, SMTP settings
│   ├── housing_analyzer/
│   │   ├── housing_analyzer.py    # Claude-powered bill risk analyzer
│   │   └── config.yaml            # Model, paths, logging
│   └── shared/
│       └── utils.py               # HTTP client, logging helpers
│
├── data/
│   └── bills/
│       └── tracked_bills.json     # Single source of truth — all 124 bills + analysis
│
├── docs/
│   └── index.html                 # GitHub Pages status dashboard (auto-generated)
│
├── outputs/
│   ├── analysis/                  # housing_analyzer markdown reports
│   └/weekly_reports/              # bill_tracker markdown weekly digests
│
├── scripts/
│   └── generate_demo_email.py     # Builds demo HTML for stakeholder review
│
├── .github/workflows/
│   └── weekly_tracker.yml         # GitHub Actions weekly automation
│
├── .env.example                   # Credential template (copy to .env)
└── requirements.txt
```

---

## The Risk Analysis Framework

### CSF's 4 Criteria (stored in `tracked_bills.json` under `analysis`)

| Key | Label | Color | What it flags |
|-----|-------|-------|---------------|
| `pro_housing_production` | **A — Local Control Override** | Red `#c0392b` | State preemption of local zoning/general plans |
| `densification` | **B — Removes Discretionary Review** | Orange `#d35400` | Eliminates CEQA, design review, public hearings |
| `reduce_discretion` | **C — Mandates Development** | Amber `#c47600` | Forces density/quotas beyond local choice |
| `cost_to_cities` | **D — Infrastructure & Capacity Burden** | Purple `#6c3483` | ADU fee caps, improvement condition restrictions, cost-shifting |

### Score levels
- `strong` — direct, explicit risk (solid pill badge)
- `moderate` — meaningful risk with some conditions (lighter pill badge)
- `indirect` — tangential risk (no badge shown, recorded in notes)
- `none` — no risk signal

### Current analysis state (as of 2026-02-24)
- **124 bills tracked**, all 124 analyzed
- **33 high-risk bills** (scoring strong/moderate on 2+ criteria)
- Criterion A: 17 strong, 22 moderate
- Criterion B: 11 strong, 4 moderate
- Criterion C: 17 strong, 14 moderate
- Criterion D: 6 strong, 15 moderate

---

## Bill Data Schema

Every bill in `tracked_bills.json["bills"]` follows this schema:

```json
{
  "bill_number":       "AB1751",
  "session":           "2025-2026",
  "title":             "...",
  "author":            "Wicks",
  "status":            "Enrolled",
  "status_date":       "2026-02-18",
  "introduced_date":   "2026-01-06",
  "last_updated":      "2026-02-23T17:38:05",
  "text_url":          "https://leginfo.legislature.ca.gov/...",
  "summary":           "...",
  "subjects":          ["Housing", "ADU"],
  "committees":        ["Assembly Housing and Community Development Committee"],
  "upcoming_hearings": [{"date": "...", "committee": "...", "location": "..."}],
  "actions":           [{"date": "...", "description": "...", "chamber": "Assembly"}],
  "source":            "legiscan",
  "source_id":         "...",
  "first_seen":        "2026-02-21T10:00:00",
  "analysis": {
    "pro_housing_production": "strong",
    "densification":          "moderate",
    "reduce_discretion":      "indirect",
    "cost_to_cities":         "strong",
    "notes":                  "Technical scoring rationale (1-2 sentences each criterion)",
    "comms_brief":            "Summary sentence.\n• Risk 1\n• Risk 2\nRecommended: Action",
    "analyzed_date":          "2026-02-23",
    "model":                  "claude-sonnet-4-6"
  }
}
```

**Important:** The `comms_brief` field uses a plain-text structured format parsed by `_render_comms_brief()`:
- Line 1: Summary sentence (bold in email)
- `• bullet` lines: rendered as a table-based list with red dot
- `Recommended: text` line: rendered as an amber callout box

---

## Email / HTML Architecture

`email_sender.py` builds **inline-styled** HTML only — no `<style>` blocks, because Gmail/Outlook strip them.

### Key functions

| Function | Purpose |
|----------|---------|
| `build_and_send_email()` | Public API — builds + sends the weekly email digest |
| `build_status_page()` | Builds `docs/index.html` for GitHub Pages |
| `_build_html()` | Assembles the full email HTML (620px wide) |
| `_build_page_html()` | Assembles the full status page HTML (900px wide) |
| `_html_analysis_section()` | Local control risk section — stats bar, key bill cards, criterion summary |
| `_html_index_section()` | "All Tracked Bills" table — includes Risk column with A/B/C/D pills |
| `_html_stalled_section()` | "Watching — No Recent Activity" table — includes Risk column |
| `_score_pills()` | Renders colored A/B/C/D pill badges from an `analysis` dict |
| `_render_comms_brief()` | Parses plain-text comms_brief into structured HTML sections |
| `_get_analysis_data()` | Computes ranked analysis stats (high_interest, by_crit, watch_list) |

### Color constants

```python
_COLOR_TEAL       = "#b03a2e"   # Risk red — section headers, bill links
_COLOR_TEAL_LIGHT = "#fadbd8"   # Light pink — section borders, backgrounds
_COLOR_ACCENT     = "#1a5276"   # Deep navy — primary heading color
_COLOR_GREEN      = "#1e8449"   # New bill badge
_COLOR_ORANGE     = "#d35400"   # Changed badge / watch list

# Criterion colors
_CRIT_STRONG_BG  = {A: "#c0392b", B: "#d35400", C: "#c47600", D: "#6c3483"}
_CRIT_MODERATE_BG = {A: "#f1948a", B: "#f5cba7", C: "#fad7a0", D: "#d2b4de"}
```

> **Note:** `_COLOR_TEAL` is misnamed — it's actually risk red. Renaming is a potential cleanup task. The name was kept to minimize diff surface area during the analysis reframe.

---

## Running Things Locally

### Weekly tracker (full pipeline)
```bash
.venv/bin/python agents/legislative/bill_tracker.py --email
```

### Housing analyzer (re-analyze bills)
```bash
# Analyze only new/changed bills (incremental)
.venv/bin/python agents/housing_analyzer/housing_analyzer.py

# Force re-analyze all 124 bills
.venv/bin/python agents/housing_analyzer/housing_analyzer.py --force

# Analyze a single bill
.venv/bin/python agents/housing_analyzer/housing_analyzer.py --bill AB1751
```

### Regenerate docs/index.html (no email, no data fetch)
```bash
.venv/bin/python - <<'EOF'
import json, sys
from pathlib import Path
from datetime import datetime, timedelta
sys.path.insert(0, ".")
from agents.legislative.email_sender import build_status_page

data = json.loads(Path("data/bills/tracked_bills.json").read_text())
config = {
    "legislative": {"lookback_days": 7, "stalled_days": 7},
    "email": {"include_full_index": True},
    "github": {
        "repo_url": "https://github.com/twgonzalez/csf-agents",
        "pages_url": "https://twgonzalez.github.io/csf-agents/",
    },
}
build_status_page(new_bills=[], changed_bills=[], all_bills=data["bills"],
                  config=config, output_path=Path("docs/index.html"))
print("Done")
EOF
```

### Generate stakeholder demo email
```bash
.venv/bin/python scripts/generate_demo_email.py
# Output: outputs/demo_email.html
```

### Local email preview (for any template change)
Use the same script as "Regenerate docs/index.html" above but write to
`outputs/analysis/email_preview_YYYY-MM-DD.html` — that path is gitignored.

---

## GitHub Actions

**Workflow:** `.github/workflows/weekly_tracker.yml`
**Trigger:** Every Monday 6 AM PT (`cron: '0 14 * * 1'`) + manual `workflow_dispatch`
**Required secrets** (Settings → Secrets and variables → Actions):

| Secret | Purpose |
|--------|---------|
| `LEGISCAN_USER` | legiscan.com login email |
| `LEGISCAN_PASSWORD` | legiscan.com password |
| `EMAIL_USER` | Gmail sending address |
| `EMAIL_PASSWORD` | Gmail App Password (16-char) |
| `EMAIL_RECIPIENTS` | Comma-separated recipient list |
| `ANTHROPIC_API_KEY` | Claude API key (added 2026-02-24) |

**Optional secrets:**
`LEGISCAN_API_KEY`, `OPENSTATES_API_KEY` (real-time data sources — use ZIP download until approved)

**What the workflow commits back:**
- `data/bills/tracked_bills.json` — updated bill statuses
- `outputs/weekly_reports/` — new markdown digest
- `docs/index.html` — rebuilt GitHub Pages dashboard

> **Merge conflict risk:** If you push analysis changes to `tracked_bills.json` at the same time the Monday workflow runs, you'll get a conflict. Resolution: take the bot's version as base (`git checkout --ours`), then re-inject analysis blocks programmatically (see git history for the merge script pattern).

---

## Known Issues / Tech Debt

1. **`_COLOR_TEAL` misnaming** — Constants are named `_COLOR_TEAL` / `_COLOR_TEAL_LIGHT` but are actually risk red/pink since the local control reframe. Safe to rename in a dedicated cleanup PR.

2. **`housing_analyzer` not wired into GitHub Actions** — The analyzer runs locally on-demand, but the weekly workflow doesn't invoke it. New bills added by the tracker won't be auto-analyzed until someone runs the analyzer manually. Wiring it in would require `ANTHROPIC_API_KEY` in the workflow env (secret already added to repo).

3. **`analysis` blocks survive tracker re-runs** — The tracker never touches the `analysis` sub-key, so scores persist. This is intentional but means stale analysis won't auto-update if a bill's scope changes.

4. **Watch list logic uses `cost_to_cities` (Criterion D)** — `_get_analysis_data()` defines `watch_list` as bills where D is strong/moderate but total criteria count < 2. If the criteria keys are ever renamed, this hardcoded reference needs updating.

5. **`generate_demo_email.py` uses synthetic bill numbers** — Demo bills like "AB 1421" may collide with real bill numbers in future sessions. Demo data is clearly labeled `"source": "demo"`.

---

## Planned Agents (not yet built)

| Agent | Directory | Description |
|-------|-----------|-------------|
| Courts monitor | `agents/courts/` | Housing litigation docket tracker |
| Movement tracker | `agents/movement/` | YIMBY/advocacy group activity |
| Media scanner | `agents/media/` | News and media sentiment |
| Cities monitor | `agents/cities/` | Local government activity |

All agents should follow the same pattern: fetch → process → store (`data/<name>/`) → report (`outputs/`). Use `agents/legislative/` as the template.

---

## Potential Next Features

- **Wire housing_analyzer into GitHub Actions** — Add a step after the tracker run that calls `housing_analyzer.py` (without `--force`) to analyze only newly added bills. Cost: ~$0.01–0.05/run for a few new bills.
- **comms_brief editing UI** — A lightweight web form to edit/approve AI-generated comms_briefs before they go into the email (currently edit-in-JSON only).
- **Per-criterion filtering** — Let email recipients filter the index table by which criteria they care about (requires JavaScript, currently pure server-rendered HTML).
- **Bill detail pages** — Instead of linking to leginfo, generate per-bill HTML pages at `docs/bills/AB1751.html` so CSF staff can share a cleaner URL.
- **Historical score tracking** — Store analysis score history so changes in risk level over time are visible (e.g., if a bill is amended to remove a preemption provision).
- **Slack/Teams notification** — Post the watch list and high-risk summary to a channel instead of / in addition to email.
