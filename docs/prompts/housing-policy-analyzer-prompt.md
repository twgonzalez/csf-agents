# Housing Policy Bill Analyzer — Agent Prompt

## Context

You are working inside the **CSF Legislative Tracker** project at:
`/Users/twgonzalez/Dropbox/Code Projects/csf/csf-agents`

This is a Python project that tracks California housing-related bills via LegiScan.
The primary data file is:
`data/bills/tracked_bills.json`

**Schema:** The JSON has a top-level `bills` dict keyed by bill number (e.g. `"AB 1234"`).
Each bill has:
- `bill_number`, `title`, `author`, `session`
- `status`, `status_date`, `introduced_date`
- `summary` — short LegiScan digest (~100–230 chars, sometimes sparse)
- `text_url` — URL to the full bill text on leginfo.legislature.ca.gov
- `actions` — list of `{date, description, chamber}` legislative history entries
- `committees`, `upcoming_hearings`

**Important:** Summaries are short. For substantive analysis, fetch the full bill text
from `text_url` using WebFetch. Prioritize bills whose summaries or titles suggest
relevance to the analysis criteria below before fetching full text.

---

## Your Task

Analyze all 124 tracked bills against four policy criteria relevant to the
**California Stewardship Fund's** housing advocacy mission. For each bill, determine
whether it contains language — directly or indirectly — that:

### Criteria

**A. Pro-Housing Production**
Language that encourages, requires, or streamlines the production of more housing units.
Examples: by-right approval, RHNA compliance mandates, streamlining permits,
removing barriers to construction, state override of local housing denials,
funding for housing construction, inclusionary requirements tied to new development.

**B. Densification**
Language that increases allowable density or enables more units on existing parcels.
Examples: upzoning, ADU/JADU expansion, missing middle (duplexes/fourplexes by-right),
transit-oriented density bonuses, density bonus law changes, removal of parking minimums,
height limit increases, minimum lot size reductions.

**C. Reduction of Local Discretionary Approval**
Language that limits or removes city/county discretionary authority over housing and
planning decisions.
Examples: ministerial approval requirements, by-right entitlement, restrictions on
design review, removal of conditional use permits for housing, limits on CEQA
applicability to housing, state preemption of local zoning, anti-NIMBY enforcement,
Housing Accountability Act strengthening.

**D. Cost Shifts to Cities / Local Governments**
Language that imposes new financial obligations, penalties, or liability on cities
and counties related to housing.
Examples: fines for RHNA non-compliance, mandatory relocation assistance funded by
local government, fee caps that reduce city revenue, litigation exposure for housing
denials, state clawback of funds for non-compliant jurisdictions, mandatory subsidies
or programs cities must fund.

---

## Output Format

Produce a Markdown report saved to:
`outputs/analysis/housing_policy_analysis_YYYY-MM-DD.md`

Structure the report as follows:

```
# CSF Housing Policy Bill Analysis
*Generated: [date]*
*Bills analyzed: [N]*

## Executive Summary
- [3–5 bullet points on key findings and notable bills]

## Scoring Matrix

| Bill | Title | Author | A: Pro-Housing | B: Density | C: Reduce Discretion | D: Cost to Cities | Notes |
|------|-------|--------|:-:|:-:|:-:|:-:|-------|
| AB 123 | ... | ... | ✅ Strong | ✅ Moderate | ⚠️ Indirect | ❌ None | ... |
...

Legend:
✅ Strong — explicit, primary purpose of the bill
✅ Moderate — significant provision, but secondary to bill's main purpose
⚠️ Indirect — implied or likely consequence, not explicit
❌ None — no relevant language found

## Bills of High Interest (2+ criteria)
[For each bill scoring on 2+ criteria, write a 3–5 sentence analysis covering:
which criteria it meets, the specific language or mechanism, and the practical
effect on cities and housing production]

## Bills by Criterion

### A — Pro-Housing Production
[List all bills that scored Strong or Moderate, with 1-sentence rationale]

### B — Densification
[same]

### C — Reduction of Local Discretionary Approval
[same]

### D — Cost Shifts to Cities
[same]

## Watch List — Potential Concern
[Bills that shift costs to cities or restrict housing in unexpected ways — flag
any bills that may appear housing-related but actually limit production or
increase barriers]
```

---

## Implementation Notes

1. **Start with the data**: Read `data/bills/tracked_bills.json` to load all bills.

2. **First pass — title/summary screening**: For each bill, score it based on
   title + summary alone. Mark bills as `FETCH_NEEDED` if:
   - The summary is under 100 chars OR
   - The title suggests clear relevance but the summary is ambiguous

3. **Second pass — full text fetch**: For `FETCH_NEEDED` bills, use WebFetch on
   `text_url` to retrieve the leginfo page and extract the bill digest/text.
   Rate-limit to ~1 request per 2 seconds to be respectful of the public server.

4. **Analysis**: Apply the four criteria to each bill. A bill can score on
   multiple criteria simultaneously.

5. **Write incrementally**: Save progress periodically; don't wait until all
   124 bills are analyzed to start writing.

6. **Be conservative**: If a bill's language is ambiguous, score it `⚠️ Indirect`
   rather than `✅ Strong`. Note the ambiguity in the Notes column.
