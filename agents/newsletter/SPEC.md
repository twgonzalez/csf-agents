# Newsletter Writer — Technical & Content Spec
## CSF Intelligence Newsletter: "Local Control Intelligence"
**Agent:** `agents/newsletter/newsletter_writer.py`
**Last updated:** 2026-02-25
**Status:** Implemented — `newsletter_writer.py` is production code wired into `weekly_tracker.yml`

---

## 1. Purpose & Position in the Pipeline

The newsletter writer is a **post-analysis output agent** that reads fully-analyzed bill data
and uses Claude to write a narrative, stakeholder-facing newsletter. It does not fetch data
or produce new analysis — it consumes the outputs of the two existing agents.

```
bill_tracker.py
      ↓
tracked_bills.json  ←──────────────────────────────┐
      ↓                                              │
housing_analyzer.py  (adds analysis blocks)          │ read-only
      ↓                                              │
newsletter_writer.py  ──────────────────────────────┘
      ↓
outputs/newsletter/YYYY-WW-newsletter.html   (reviewer preview)
docs/newsletters/YYYY-WW.html               (GitHub Pages shareable)
Email → reviewer list → (approve) → full subscriber list
```

The newsletter is **entirely separate from the weekly digest** (`email_sender.py`).
The digest is operational/internal. The newsletter is external and stakeholder-facing.

---

## 2. Target Audiences

Three segments receive this newsletter. Content is written to serve all three in a single
edition (near-term). Segmented editions (Action Brief / Investor Brief) are a medium-term
upgrade once subscriber tracking exists.

| Segment | Who | What they need | Desired action |
|---|---|---|---|
| **Local electeds** | City council members, mayors, candidates | Bill-level intelligence before hearings, defensive language, early warning | Show up to committee, adopt toolkit language, contact their rep |
| **Neighborhood advocates** | HOA leaders, neighborhood council chairs, local advocacy group leaders | Coordinated framing, mobilization signal, specific bills to oppose | Forward to members, mobilize for a vote, submit public comment |
| **HNW donors** | Major individual donors, foundation officers | Threat-landscape overview, evidence that investment is strategic | Attend briefing/house party, connect a peer, write a check |

---

## 3. Editorial Voice & Framing

### Voice
Authoritative, confident, insider-briefing tone. Not alarmist. Not academic.
Think: **policy legal memo meets Axios smart brevity**.

- Names problems directly without hand-wringing
- Cites specific bills and specific risks
- Empowers the reader — they have information others don't
- Zero false balance: this is a movement newsletter with a clear POV

### Canonical Framing (use consistently)
- ✅ "preempts local zoning authority"
- ✅ "removes discretionary review"
- ✅ "shifts infrastructure costs to cities"
- ✅ "developer-driven mandate"
- ✅ "state overreach into local land-use authority"
- ❌ "controversial bill" / "some critics argue" / "opponents say"

### The Rhetorical Anchor
From the strategic documents: **"This isn't about stopping housing — it's about who decides."**
Claude should weave this framing naturally, especially in the opening and closing sections.

### Editorialization Level
Claude should editorialize **firmly within the established risk framework**. The four criteria
already encode the organization's POV. Claude should reflect that POV in the narrative voice,
grounded always in the factual bill text and analysis notes already computed.

---

## 4. Newsletter Structure (6 Sections)

Target length: **~450 words of prose** + structured sections. Genuine 2-minute read.

### Section 1 — Opening (Claude-written prose)
- 3–4 sentences
- The week's single most important development: biggest threat advancing, new preemption bill,
  or notable win
- Sets the frame for everything that follows
- Uses the movement's voice from the first sentence
- Example lead: *"Sacramento moved three housing mandates closer to your city council this week..."*

### Section 2 — Watch List (Claude-written, structured)
- 3–5 bills scoring **strong or moderate on 2+ criteria** (high-risk threshold)
- Ranked by: (1) upcoming hearing in next 7 days, (2) total criteria count, (3) score weight
- Per bill:
  - Bill number + title (linked to leginfo)
  - Risk pills (A/B/C/D, reuse `_score_pills()` logic from email_sender.py)
  - 2-sentence Claude summary: what it does + why local control is threatened
  - Hearing date/committee if applicable
- Section header: **"Bills Requiring Your Attention This Week"**

### Section 3 — This Week in Committee (data-driven)
- Sourced directly from `upcoming_hearings` in tracked_bills.json — no Claude generation
- Only bills with hearings in the next 7 days
- Per hearing row: Date | Bill | Committee | Location | One-line risk summary
- Includes a direct action line: *"Submit opposition comment by [date] →  [leginfo URL]"*
- Section header: **"Sacramento Calendar — Next 7 Days"**
- Omit section entirely if no upcoming hearings

### Section 4 — Emerging Threats (Claude-written)
- Bills added in the last `lookback_days` (detected via `first_seen` field)
- 1–2 sentences per bill: what it is and why it's being tracked
- Framed as early warning: *"New this week — bills to monitor before they gain momentum"*
- Max 4 bills; if more than 4 new bills, list top 4 by risk score + note "X additional new bills tracked"
- Omit section if no new bills

### Section 5 — What You Can Do (Claude-written, audience-aware)
- One clear, time-bound ask per segment — written as 3 short bullets
- **For electeds:** specific hearing to attend or ordinance language to adopt
- **For advocates:** specific mobilization ask (forward this, show up, submit comment)
- **For donors:** connection to strategic impact (framed as investment, not charity)
- Section header: **"Your Move This Week"**

### Section 6 — The Bigger Picture (Claude-written)
- 2–3 sentences only
- Connects the week's specific bills to the broader mission
- Closes with forward momentum — not alarm, but strategic confidence
- Should not repeat the opening; should resolve it

---

## 5. File Structure

```
agents/newsletter/
├── newsletter_writer.py    # Main module (described below)
├── config.yaml             # Newsletter-specific config
└── SPEC.md                 # This file

outputs/newsletter/
└── YYYY-WW-newsletter.html # Reviewer preview (gitignored pattern)

docs/newsletters/
└── YYYY-WW.html            # Published GitHub Pages version (committed)
```

---

## 6. `newsletter_writer.py` — Key Functions

```python
# Public API
def build_newsletter(config: dict, bills_data: dict, ...) -> NewsletterResult
def send_preview(result: NewsletterResult, config: dict) -> None
def send_newsletter(result: NewsletterResult, config: dict) -> None

# Internal — content selection
def _select_bills(bills: list, lookback_days: int) -> NewsletterBillSet
    # Returns: watch_list, new_bills, upcoming_hearings
    # watch_list: bills with strong/moderate on 2+ criteria, ranked
    # new_bills: first_seen within lookback window
    # upcoming_hearings: bills with hearings in next 7 days

# Internal — Claude content generation
def _build_content(bill_set: NewsletterBillSet, config: dict) -> NewsletterContent
    # Single Claude call with all 6 sections requested via tool_use
    # Returns structured content dict keyed by section name

def _build_claude_prompt(bill_set: NewsletterBillSet) -> str
    # Constructs the user prompt with bill data formatted for Claude

# Internal — HTML/text assembly
def _build_email_html(content: NewsletterContent, bill_set: NewsletterBillSet) -> str
    # 620px wide, fully inline-styled (same pattern as email_sender.py)
    # Reuses _score_pills() logic for criterion badges

def _build_pages_html(content: NewsletterContent, bill_set: NewsletterBillSet) -> str
    # 900px wide, shareable web version with navigation header

def _build_plaintext(content: NewsletterContent, bill_set: NewsletterBillSet) -> str
    # Plain text fallback for government email clients

# Internal — delivery
def _send_email(html: str, plaintext: str, recipients: list, config: dict) -> None
    # Reuses SMTP logic from email_sender.py

def _write_outputs(html_email, html_page, plaintext, config) -> Path
    # Writes to outputs/newsletter/ and docs/newsletters/
```

---

## 7. Claude Prompt Design

### Approach
A **single Claude API call** requesting all generated sections at once via `tool_use`,
similar to how `housing_analyzer.py` uses structured tool responses. This is more
efficient than multiple calls and ensures sections are coherent with each other.

### System Prompt (draft)
```
You are the editorial voice of the California Stewardship Fund — a policy organization
whose core belief is that the best decisions come from people closest to them.

You write the weekly "Local Control Intelligence" newsletter, which goes to city council
members, neighborhood advocacy leaders, and major donors who support protecting local
government authority from state preemption.

VOICE: Authoritative, confident, insider-briefing. Not alarmist. Not academic.
Think policy legal memo meets Axios smart brevity. You name problems directly,
cite specific bills and specific risks, and empower the reader.

FRAMING: This newsletter has a clear POV. Sacramento is advancing legislation that
preempts local zoning authority, removes discretionary review, mandates development,
and shifts infrastructure costs to cities. The central message is:
"This isn't about stopping housing — it's about who decides."

USE THESE TERMS: "preempts local authority", "removes discretionary review",
"state mandate", "developer-driven", "infrastructure cost-shifting"
AVOID: "controversial", "opponents say", "some argue", false balance language

The three audiences reading this newsletter need different things:
- Local electeds: specific intelligence to act on before hearings
- Neighborhood advocates: coordinated framing and mobilization signal
- Major donors: strategic threat landscape that justifies sustained investment

Write for all three simultaneously. Every section should carry meaning for each audience.
```

### User Prompt Structure
```
Here is this week's bill data. Write the newsletter sections as specified.

## HIGH-RISK WATCH LIST (strong/moderate on 2+ criteria)
[For each bill: number, title, author, status, status_date, upcoming hearings,
analysis scores, notes, comms_brief]

## NEW BILLS THIS WEEK (first_seen within lookback window)
[For each bill: number, title, author, subjects, summary]

## UPCOMING HEARINGS (next 7 days)
[For each: bill, date, committee, location]

---
Write the following sections:

1. OPENING (3-4 sentences) — headline threat or development this week, movement voice
2. WATCH_LIST_SUMMARIES — for each watch list bill: 2-sentence summary (what + why it threatens local control)
3. EMERGING_THREATS — for each new bill: 1-2 sentence early warning
4. WHAT_YOU_CAN_DO — 3 bullets, one per audience segment, specific and time-bound
5. BIGGER_PICTURE — 2-3 sentences connecting to mission, forward momentum not alarm

Return as a JSON tool response with keys: opening, watch_list_summaries (dict keyed by
bill_number), emerging_threats (dict keyed by bill_number), what_you_can_do (list of
3 strings), bigger_picture.
```

---

## 8. Bill Selection Logic (`_select_bills`)

```python
def _select_bills(bills, lookback_days=14):
    today = date.today()
    cutoff = today - timedelta(days=lookback_days)
    hearing_cutoff = today + timedelta(days=7)

    watch_list = []
    new_bills = []
    upcoming_hearings = []

    for bill in bills:
        analysis = bill.get("analysis", {})

        # Count strong/moderate scores
        risk_count = sum(
            1 for k in ["pro_housing_production", "densification",
                        "reduce_discretion", "cost_to_cities"]
            if analysis.get(k) in ("strong", "moderate")
        )

        # Watch list: 2+ criteria with strong/moderate
        if risk_count >= 2:
            watch_list.append((bill, risk_count))

        # New bills: first_seen within lookback window
        if bill.get("first_seen"):
            first_seen = datetime.fromisoformat(bill["first_seen"]).date()
            if first_seen >= cutoff:
                new_bills.append(bill)

        # Upcoming hearings
        for hearing in bill.get("upcoming_hearings", []):
            hearing_date = date.fromisoformat(hearing["date"])
            if today <= hearing_date <= hearing_cutoff:
                upcoming_hearings.append({**hearing, "bill": bill})

    # Rank watch list: hearings first, then risk_count desc, then score weight
    watch_list.sort(key=lambda x: (
        -any(today <= date.fromisoformat(h["date"]) <= hearing_cutoff
             for h in x[0].get("upcoming_hearings", [])),
        -x[1]
    ))

    return {
        "watch_list": [b for b, _ in watch_list[:5]],
        "new_bills": new_bills[:4],
        "upcoming_hearings": sorted(upcoming_hearings, key=lambda h: h["date"])
    }
```

---

## 9. HTML Design Guidelines

Follow `email_sender.py` patterns exactly:
- Fully inline styles — no `<style>` blocks (Gmail/Outlook strip them)
- 620px max-width for email version
- 900px max-width for web/pages version
- Reuse color constants from email_sender.py (or import them)
- Reuse `_score_pills()` logic for A/B/C/D criterion badges
- Section headers use the existing `_COLOR_ACCENT` deep navy (`#1a5276`)

### Newsletter-specific design additions
- **Masthead:** "LOCAL CONTROL INTELLIGENCE" wordmark + "Week of [date]" + CSF tagline
- **Opening block:** slightly larger font (16px), light teal/navy left border
- **Watch list cards:** same card style as high-risk bills in email_sender.py
- **Hearing calendar:** table with colored date chip (amber if hearing within 3 days, standard otherwise)
- **"Your Move" section:** three distinct audience callout boxes (electeds = navy, advocates = orange, donors = purple)
- **Footer:** unsubscribe placeholder, CSF contact, link to full web version

---

## 10. `config.yaml` Schema

```yaml
# agents/newsletter/config.yaml

model: claude-sonnet-4-6      # Generation model. Consider claude-opus for higher quality.

newsletter:
  name: "Local Control Intelligence"
  subject_template: "Local Control Intelligence — Week of {date}"
  from_name: "Saving California"
  high_risk_threshold: 2      # Min criteria count (strong/moderate) for watch list
  max_watch_list: 5           # Max bills in the watch list section
  max_new_bills: 4            # Max bills in emerging threats section
  hearing_lookahead_days: 7   # How far ahead to look for upcoming hearings

approval:
  enabled: true
  reviewer_emails: []         # Set via NEWSLETTER_REVIEWER_EMAILS env var
  window_hours: 3             # Hours to wait for approval before auto-hold

paths:
  bills_file: data/bills/tracked_bills.json
  output_dir: outputs/newsletter
  pages_dir: docs/newsletters

email:
  smtp_host: smtp.gmail.com
  smtp_port: 587
  from_address: ""            # Set via NEWSLETTER_EMAIL_USER env var
  recipients:
    all: []                   # Set via NEWSLETTER_RECIPIENTS env var
    # Future: segment by audience type
    # electeds: []
    # advocates: []
    # donors: []

logging:
  level: INFO
  file: logs/newsletter_writer.log
```

---

## 11. Approval Workflow

### MVP (Phase 1)
1. `newsletter_writer.py` generates the newsletter and writes to `outputs/newsletter/YYYY-WW-newsletter.html`
2. Sends a **preview email** to `approval.reviewer_emails` with subject:
   `[REVIEW NEEDED] Local Control Intelligence — Week of {date}`
3. Preview email includes:
   - Full rendered newsletter inline
   - Two links at the top: **[Approve & Send]** | **[Edit in JSON]**
4. Reviewer replies "send" or runs `newsletter_writer.py --send` manually to deliver
5. Final send writes to `docs/newsletters/YYYY-WW.html` and emails subscriber list

### Phase 2 (future)
- Lightweight web form at `docs/newsletter-review/` for inline editing of Claude-generated copy
- This is the same "comms_brief editing UI" listed in CLAUDE.md Potential Next Features
- One UI serves both newsletter editing and comms_brief approval

---

## 12. GitHub Actions Integration

Add two steps to `weekly_tracker.yml` after the existing tracker step:

```yaml
# Step 4b: Run housing analyzer (existing tech debt item #2)
- name: Run housing analyzer (new bills only)
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
  run: |
    python agents/housing_analyzer/housing_analyzer.py
    # No --force flag: only analyzes bills without existing analysis blocks

# Step 4c: Generate and send newsletter preview
- name: Generate newsletter and send for review
  env:
    ANTHROPIC_API_KEY:          ${{ secrets.ANTHROPIC_API_KEY }}
    NEWSLETTER_EMAIL_USER:      ${{ secrets.EMAIL_USER }}
    NEWSLETTER_EMAIL_PASSWORD:  ${{ secrets.EMAIL_PASSWORD }}
    NEWSLETTER_RECIPIENTS:      ${{ secrets.NEWSLETTER_RECIPIENTS }}
    NEWSLETTER_REVIEWER_EMAILS: ${{ secrets.NEWSLETTER_REVIEWER_EMAILS }}
  run: |
    python agents/newsletter/newsletter_writer.py --preview
    # --preview: generates + sends to reviewers only, does not send to full list
    # Reviewer manually runs --send after approval (or approval UI handles it)
```

Add to the commit step:
```yaml
git add docs/newsletters/
```

New secrets required:
| Secret | Purpose |
|---|---|
| `NEWSLETTER_RECIPIENTS` | Comma-separated full subscriber list |
| `NEWSLETTER_REVIEWER_EMAILS` | Comma-separated reviewer list (approval step) |

`ANTHROPIC_API_KEY` already exists (added 2026-02-24 per CLAUDE.md).

---

## 13. CLI Interface

```bash
# Generate preview only (no email)
python agents/newsletter/newsletter_writer.py --dry-run

# Generate + send to reviewers
python agents/newsletter/newsletter_writer.py --preview

# Generate + send directly to full list (skip approval — use carefully)
python agents/newsletter/newsletter_writer.py --send

# Regenerate pages version only (no email)
python agents/newsletter/newsletter_writer.py --pages-only

# Override bill data source
python agents/newsletter/newsletter_writer.py --bills path/to/tracked_bills.json
```

---

## 14. Implementation Order

1. **`_select_bills()`** — Bill selection logic (no Claude dependency, testable immediately)
2. **`_build_plaintext()`** — Plain text assembly (validates structure before HTML)
3. **`_build_content()`** — Claude integration (single call, tool_use response)
4. **`_build_email_html()`** — Email HTML (model on email_sender.py patterns)
5. **`_send_email()`** + **`--dry-run`** CLI — Delivery + local testing
6. **`_build_pages_html()`** + GitHub Pages output — Shareable web version
7. **`--preview`** + reviewer send flow — Approval workflow
8. **GitHub Actions** step — Wire into weekly pipeline
9. **Approval UI** — Phase 2, after newsletter is stable

---

## 15. Cost Estimate

Per weekly run:
- ~5 watch list bills × ~500 tokens of bill data = ~2,500 input tokens
- ~4 new bills × ~300 tokens = ~1,200 input tokens
- System prompt + structure = ~800 tokens
- **Total input: ~4,500 tokens**
- **Total output: ~600 tokens** (all 6 sections)

At claude-sonnet-4-6 pricing: **~$0.02–0.04 per newsletter run**

Comparable to the housing analyzer per-bill cost. Negligible at this scale.

---

*This spec was written against codebase snapshot 2026-02-24. See CLAUDE.md for full architecture context.*
