# Legislative Bill Tracker

> Agent 1 of the California Stewardship Fund Intelligence System

Monitors the California Legislature for housing-related bills and produces weekly intelligence digests.

## What It Does

1. **Fetches** housing-related bills from the CA Legislature
2. **Filters** by housing keywords (zoning, ADU, density, YIMBY, affordability, and more)
3. **Detects** new bills and status changes since the last run
4. **Stores** structured bill data in `data/bills/tracked_bills.json`
5. **Generates** a weekly markdown digest in `outputs/weekly_reports/`

## Quick Start

### 1. Install dependencies

```bash
# From the project root
pip install -r requirements.txt
```

### 2. Configure credentials

Copy the template and fill in your values:

```bash
cp .env.example .env
# Then edit .env
```

Your `.env` file is excluded from version control. See `.env.example` for all available settings.

### 3. Get a data source (four options, in priority order)

The tracker tries data sources in order and uses the first one that works.

#### Option A — LegiScan API (best quality, free, ~1-2 day approval)
1. Register at https://legiscan.com/legiscan (free account)
2. Wait for API key approval (requires human review)
3. Add `LEGISCAN_API_KEY=your_key` to your `.env` file

#### Option B — OpenStates API (good quality, free, ~1-2 day approval)
1. Sign up at https://openstates.org/accounts/signup/
2. Copy your API key from your profile page
3. Add `OPENSTATES_API_KEY=your_key` to your `.env` file

#### Option C — LegiScan Dataset ZIP with auto-download (recommended bridge)
LegiScan publishes a free weekly ZIP of all CA bills. The tracker can download it
automatically each run using your legiscan.com login credentials.

**Auto-download setup (once):**
1. Create a free account at https://legiscan.com/legiscan (instant signup)
2. Add your credentials to `.env`:
   ```
   LEGISCAN_USER=you@example.com
   LEGISCAN_PASSWORD=your_password
   ```
3. Run the tracker — it will download the latest ZIP automatically before processing.

**Manual download fallback:** If you prefer to download manually (or auto-download fails):
1. Log in at https://legiscan.com and go to **https://legiscan.com/CA/datasets**
2. Download the current session ZIP (e.g. `CA_2025-2026_XXXXXX.zip`) — do **not** extract it
3. Place it in `data/legiscan/` — auto-discovered on the next run

ZIPs are updated every Sunday. The tracker skips re-downloading if the current file is already on disk.

#### Option D — No setup required (very limited)
If no API key or ZIP is configured, the tracker falls back to scraping leginfo.legislature.ca.gov directly. Results are minimal (title and status only, no sponsors or committee data).

### 4. Run the tracker

```bash
# Live mode — auto-downloads latest ZIP if LEGISCAN_USER/LEGISCAN_PASSWORD are set
python agents/legislative/bill_tracker.py

# Demo mode — uses sample data, no credentials needed
python agents/legislative/bill_tracker.py --demo

# Custom config file
python agents/legislative/bill_tracker.py --config path/to/config.yaml
```

## Output Files

| File | Description |
|------|-------------|
| `data/bills/tracked_bills.json` | Persistent bill storage — read by other CSF agents |
| `outputs/weekly_reports/legislative_YYYY-MM-DD.md` | Weekly markdown digest |
| `logs/legislative_tracker.log` | Run history, errors, and debug info |

## Configuration

Edit `agents/legislative/config.yaml`:

| Setting | Description | Default |
|---------|-------------|---------|
| `legislative.lookback_days` | Scan bills updated in last N days | `7` |
| `legislative.session` | CA legislative session identifier | `2025-2026` |
| `keywords.housing` | List of keywords (ANY match = bill tracked) | See config |
| `logging.level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` | `INFO` |

**Adding keywords:** Open `config.yaml` and add entries to `keywords.housing`. Multi-word phrases should be quoted.

**Changing lookback window:** Set `legislative.lookback_days` to `14` for bi-weekly runs, `30` for monthly.

## Bill Data Schema

Each bill in `tracked_bills.json` follows this schema. All CSF agents use the same schema so they can share data without translation.

```json
{
  "bill_number":     "AB 1234",
  "session":         "2025-2026",
  "title":           "Residential Zoning: By-Right Approval...",
  "author":          "Wicks",
  "status":          "Referred to Committee on Housing",
  "status_date":     "2025-02-18",
  "introduced_date": "2025-02-14",
  "last_updated":    "2025-02-21T10:00:00",
  "text_url":        "https://leginfo.legislature.ca.gov/...",
  "summary":         "Would require cities and counties to...",
  "subjects":        ["Housing", "Zoning", "Land Use"],
  "committees":      ["Assembly Housing and Community Development Committee"],
  "upcoming_hearings": [
    {"date": "2025-03-07", "committee": "...", "location": "..."}
  ],
  "actions": [
    {"date": "2025-02-14", "description": "Introduced", "chamber": "Assembly"}
  ],
  "source":     "openstates",
  "source_id":  "ocd-bill/...",
  "first_seen": "2025-02-21T10:00:00"
}
```

## Running Weekly with Cron

Add to your crontab (`crontab -e`) to run every Monday at 7 AM:

```cron
0 7 * * 1 cd /path/to/csf-agents && OPENSTATES_API_KEY=your_key python agents/legislative/bill_tracker.py >> logs/cron.log 2>&1
```

Or with a `.env` file approach (see project root README).

## Code Architecture

```
bill_tracker.py
├── BillTracker                 Main agent class
│   ├── __init__                Load config, resolve paths, init logger
│   ├── run(demo)               Orchestrate the 4-stage pipeline
│   │
│   ├── _fetch()                Stage 1: pull from data source
│   │   ├── _fetch_openstates() → OpenStates API v3 (primary)
│   │   └── _fetch_leginfo()    → leginfo.ca.gov scraper (fallback)
│   │
│   ├── _process()              Stage 2: detect new/changed vs stored
│   ├── _load_stored()          Stage 3a: read tracked_bills.json
│   ├── _store()                Stage 3b: write tracked_bills.json
│   │
│   ├── _report()               Stage 4: generate markdown report
│   └── _render_report()        Markdown rendering logic
│
└── main()                      CLI argument parsing → BillTracker.run()
```

## Data Sources

| Priority | Source | Key required | Update frequency | Data quality |
|----------|--------|-------------|-----------------|--------------|
| 1 | LegiScan API | Yes (free, ~1-2 day review) | Real-time | Excellent |
| 2 | OpenStates API | Yes (free, ~1-2 day review) | Real-time | Excellent |
| 3 | LegiScan Dataset ZIP | Login only (instant) | Weekly (Sundays) | Excellent |
| 4 | CA LegInfo scraper | None | Real-time | Poor |

### LegiScan API (priority 1)
- **URL:** https://api.legiscan.com
- **Key:** Free at https://legiscan.com/legiscan (requires human review)
- **Free tier:** 30,000 queries/month
- **Data:** Full bill text, sponsors, committee history, hearing dates

### OpenStates API (priority 2)
- **URL:** https://v3.openstates.org
- **Key:** Free at https://openstates.org/accounts/signup/ (requires human review)
- **Free tier:** ~500 requests/day
- **Data:** Excellent — structured JSON with sponsors, actions, committees

### LegiScan Dataset ZIP (priority 3 — bridge while waiting for API approval)
- **Download:** https://legiscan.com/CA/datasets (free login, no API key needed)
- **Format:** ZIP file — do not extract; place as-is in `data/legiscan/`
- **Updated:** Every Sunday morning
- **Usage:** Newest `CA_*.zip` in `data/legiscan/` is auto-discovered on each run

### CA LegInfo Scraper (priority 4 — last resort)
- **URL:** https://leginfo.legislature.ca.gov
- **Key:** None required
- **Reliability:** Low — JSF-based site, GET requests return very limited data

## Troubleshooting

**No bills returned:**
- Check `OPENSTATES_API_KEY` is set correctly
- Try `--demo` mode to verify the pipeline works
- Set `logging.level: DEBUG` in config.yaml for verbose output

**leginfo scraper returns nothing:**
- The CA Legislature site uses complex JSF forms; scraping is fragile
- Get a free OpenStates API key for reliable results

**Report already exists for today:**
- The tracker will overwrite the same-day report (safe to re-run)

**Rate limit errors:**
- OpenStates free tier: 1,000 requests/day
- If exceeded, wait until midnight UTC or reduce keywords in config
