# California Stewardship Fund — Intelligence Agents

Multi-agent system for monitoring California housing policy, legislation, litigation, and advocacy.

## Agents

| Agent | Status | Description |
|-------|:------:|-------------|
| `agents/legislative/` | ✅ Active | CA Legislature housing bill tracker |
| `agents/courts/` | 🔜 Planned | Housing litigation docket monitor |
| `agents/movement/` | 🔜 Planned | YIMBY/advocacy group tracker |
| `agents/media/` | 🔜 Planned | News and media sentiment scanner |
| `agents/cities/` | 🔜 Planned | Local government activity monitor |

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your OpenStates API key (free at openstates.org/accounts/signup/)
export OPENSTATES_API_KEY=your_key_here

# 3. Run the legislative tracker
python agents/legislative/bill_tracker.py

# Or run in demo mode — no API key needed
python agents/legislative/bill_tracker.py --demo
```

## Project Structure

```
csf-agents/
├── agents/
│   ├── shared/                  # Utilities shared across all agents
│   │   └── utils.py             # HTTP client, logging, JSON helpers
│   │
│   └── legislative/             # Agent 1: CA Legislature bill tracker
│       ├── bill_tracker.py      # Main agent (run this)
│       ├── config.yaml          # Keywords, paths, API settings
│       └── README.md
│
├── data/
│   └── bills/
│       └── tracked_bills.json   # Persistent bill data (read by all agents)
│
├── outputs/
│   └── weekly_reports/          # Generated markdown digests
│
├── logs/                        # Runtime logs
├── requirements.txt
└── README.md
```

## Data Standards

All agents store data in `data/` using a shared JSON schema.
All agents write reports to `outputs/`.
All agents are independently runnable.
Shared utilities live in `agents/shared/`.

See each agent's README for its specific schema and usage.

## Adding to Git

```bash
git init
git add .
git commit -m "feat: add CSF intelligence agent system with legislative tracker"

# Push to GitHub (after creating a repo)
git remote add origin https://github.com/YOUR_ORG/csf-agents.git
git push -u origin main
```

## Running Weekly

See `agents/legislative/README.md` for cron setup instructions.

## Adding a New Agent

Each new agent should:
1. Live in `agents/<name>/`
2. Import utilities from `agents/shared/utils.py`
3. Store data in `data/<name>/` following the shared JSON schema pattern
4. Write reports to `outputs/weekly_reports/`
5. Accept a `--config` flag pointing to its `config.yaml`
6. Include a `README.md` with setup and usage instructions

Copy `agents/legislative/` as a starting template — the fetch → process → store → report pattern is designed to be reused.
