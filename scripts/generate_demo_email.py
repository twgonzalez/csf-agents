#!/usr/bin/env python3
"""
Generate a demo HTML email for stakeholder review.

Shows what the weekly digest looks like when bills are actively
moving through the CA Legislature — committee hearings, floor votes,
status changes, and upcoming dates.

Usage:
    .venv/bin/python scripts/generate_demo_email.py

Output:
    outputs/demo_email.html   (open in any browser)
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

# Bootstrap path so we can import from agents/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agents.legislative.email_sender import _build_html

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

today = datetime.now()

def d(days_ago: int) -> str:
    return (today - timedelta(days=days_ago)).strftime("%Y-%m-%d")

def future(days: int) -> str:
    return (today + timedelta(days=days)).strftime("%Y-%m-%d")

def leginfo(bill_id: str) -> str:
    return f"https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml?bill_id={bill_id}"

# ---------------------------------------------------------------------------
# New bills this week
# ---------------------------------------------------------------------------

NEW_BILLS = [
    {
        "bill_number": "AB 1421",
        "session": "2025-2026",
        "title": "Affordable Housing: Streamlined Permitting for 100% Affordable Projects",
        "author": "Wicks",
        "status": "Introduced",
        "status_date": d(2),
        "introduced_date": d(2),
        "last_updated": today.isoformat(),
        "text_url": leginfo("202520260AB1421"),
        "summary": (
            "Would require local agencies to provide a ministerial, streamlined "
            "approval process for 100% affordable housing projects on sites zoned "
            "for any residential or mixed use. Prohibits agencies from imposing "
            "design review, conditional use permits, or variances beyond objective "
            "zoning standards. Requires approval or denial within 60 days."
        ),
        "subjects": ["Affordable Housing", "Permitting", "Zoning", "Ministerial Approval"],
        "committees": ["Assembly Housing and Community Development Committee"],
        "upcoming_hearings": [],
        "actions": [
            {"date": d(2), "description": "Introduced", "chamber": "Assembly"},
        ],
        "source": "demo",
        "source_id": "demo_ab1421",
    },
    {
        "bill_number": "SB 892",
        "session": "2025-2026",
        "title": "Tenant Protection Act: Just Cause Eviction Expansion",
        "author": "Skinner",
        "status": "Introduced",
        "status_date": d(3),
        "introduced_date": d(3),
        "last_updated": today.isoformat(),
        "text_url": leginfo("202520260SB892"),
        "summary": (
            "Expands the Tenant Protection Act of 2019 to cover all rental units "
            "in California, removing the exemption for single-family homes and "
            "condominiums. Adds harassment, failure to maintain habitability, and "
            "owner move-in fraud as actionable just-cause violations. Increases "
            "relocation assistance to three months' rent for no-fault evictions."
        ),
        "subjects": ["Tenant Protection", "Eviction", "Rent Control", "Housing"],
        "committees": ["Senate Judiciary Committee"],
        "upcoming_hearings": [],
        "actions": [
            {"date": d(3), "description": "Introduced", "chamber": "Senate"},
        ],
        "source": "demo",
        "source_id": "demo_sb892",
    },
    {
        "bill_number": "AB 980",
        "session": "2025-2026",
        "title": "Missing Middle Housing: Fourplexes By-Right on Single-Family Lots",
        "author": "Haney",
        "status": "Introduced",
        "status_date": d(1),
        "introduced_date": d(1),
        "last_updated": today.isoformat(),
        "text_url": leginfo("202520260AB980"),
        "summary": (
            "Requires ministerial approval for up to four residential units on any "
            "parcel currently zoned for single-family use. Prohibits local agencies "
            "from applying parking requirements for projects within half a mile of "
            "transit. Allows buildings up to three stories where zoning permits "
            "two stories for single-family."
        ),
        "subjects": ["Missing Middle", "Zoning", "Single-Family", "Density", "Infill"],
        "committees": [],
        "upcoming_hearings": [],
        "actions": [
            {"date": d(1), "description": "Introduced", "chamber": "Assembly"},
        ],
        "source": "demo",
        "source_id": "demo_ab980",
    },
    {
        "bill_number": "SB 1103",
        "session": "2025-2026",
        "title": "Housing Element: RHNA Accountability and State Override Authority",
        "author": "Wiener",
        "status": "Introduced",
        "status_date": d(4),
        "introduced_date": d(4),
        "last_updated": today.isoformat(),
        "text_url": leginfo("202520260SB1103"),
        "summary": (
            "Strengthens state enforcement of the Regional Housing Needs Allocation "
            "process by authorizing HCD to override local zoning in jurisdictions "
            "that are 25% or more behind their RHNA allocation after three years. "
            "Imposes escalating fines of $10,000–$50,000 per month on "
            "non-compliant cities and counties."
        ),
        "subjects": ["RHNA", "Housing Element", "Enforcement", "Local Government", "HCD"],
        "committees": ["Senate Housing Committee"],
        "upcoming_hearings": [],
        "actions": [
            {"date": d(4), "description": "Introduced", "chamber": "Senate"},
        ],
        "source": "demo",
        "source_id": "demo_sb1103",
    },
]

# ---------------------------------------------------------------------------
# Bills with status changes this week
# ---------------------------------------------------------------------------

CHANGED_BILLS = [
    {
        "bill_number": "SB 567",
        "session": "2025-2026",
        "title": "ADU Development: Streamlined Permitting and Fee Caps",
        "author": "Wiener",
        "status": "Passed Senate Housing Committee — Ayes 7, Noes 2",
        "status_date": d(1),
        "introduced_date": d(42),
        "last_updated": today.isoformat(),
        "text_url": leginfo("202520260SB567"),
        "summary": "Streamlines ADU permitting by capping impact fees at $1,000.",
        "subjects": ["ADU", "Housing", "Permitting"],
        "committees": ["Senate Housing Committee", "Senate Appropriations Committee"],
        "upcoming_hearings": [
            {"date": future(18), "committee": "Senate Appropriations Committee", "location": "State Capitol, Room 4202"},
        ],
        "actions": [],
        "source": "demo",
        "source_id": "demo_sb567",
        "_prev_status": "Referred to Senate Housing Committee",
    },
    {
        "bill_number": "AB 1234",
        "session": "2025-2026",
        "title": "Residential Zoning: By-Right Approval for Multifamily Housing",
        "author": "Wicks",
        "status": "Passed Assembly Housing Committee — Ayes 9, Noes 1",
        "status_date": d(2),
        "introduced_date": d(35),
        "last_updated": today.isoformat(),
        "text_url": leginfo("202520260AB1234"),
        "summary": "Would require cities and counties to approve by-right any housing development on sites already zoned for multifamily.",
        "subjects": ["Housing", "Zoning", "By-Right", "Multifamily"],
        "committees": [
            "Assembly Housing and Community Development Committee",
            "Assembly Appropriations Committee",
        ],
        "upcoming_hearings": [
            {"date": future(10), "committee": "Assembly Appropriations Committee", "location": "State Capitol, Room 4202"},
        ],
        "actions": [],
        "source": "demo",
        "source_id": "demo_ab1234",
        "_prev_status": "Heard — Assembly Housing and Community Development Committee",
    },
    {
        "bill_number": "SB 234",
        "session": "2025-2026",
        "title": "Density Bonus Law: Expansion of Affordability Incentives",
        "author": "Caballero",
        "status": "Passed Senate Floor — Ayes 29, Noes 9",
        "status_date": d(3),
        "introduced_date": d(65),
        "last_updated": today.isoformat(),
        "text_url": leginfo("202520260SB234"),
        "summary": "Expands California's Density Bonus Law to increase the maximum density bonus from 50% to 100%.",
        "subjects": ["Density Bonus", "Affordable Housing", "Zoning"],
        "committees": [
            "Senate Housing Committee",
            "Assembly Housing and Community Development Committee",
        ],
        "upcoming_hearings": [
            {"date": future(7), "committee": "Assembly Housing and Community Development Committee", "location": "State Capitol, Room 447"},
        ],
        "actions": [],
        "source": "demo",
        "source_id": "demo_sb234",
        "_prev_status": "Passed Senate Appropriations Committee — Ayes 12, Noes 3",
    },
    {
        "bill_number": "AB 456",
        "session": "2025-2026",
        "title": "Local Control: Ministerial Approval for Infill Housing",
        "author": "Lee",
        "status": "Referred to Assembly Housing and Community Development Committee",
        "status_date": d(5),
        "introduced_date": d(22),
        "last_updated": today.isoformat(),
        "text_url": leginfo("202520260AB456"),
        "summary": "Requires ministerial, non-discretionary approval for qualifying infill housing projects in urbanized areas.",
        "subjects": ["Housing", "Infill", "Ministerial Approval"],
        "committees": ["Assembly Housing and Community Development Committee"],
        "upcoming_hearings": [
            {"date": future(21), "committee": "Assembly Housing and Community Development Committee", "location": "State Capitol, Room 447"},
        ],
        "actions": [],
        "source": "demo",
        "source_id": "demo_ab456",
        "_prev_status": "Introduced",
    },
    {
        "bill_number": "SB 711",
        "session": "2025-2026",
        "title": "Homelessness Prevention: Emergency Rental Assistance Program",
        "author": "Durazo",
        "status": "Amended in Senate — substantive amendments to eligibility criteria",
        "status_date": d(1),
        "introduced_date": d(50),
        "last_updated": today.isoformat(),
        "text_url": leginfo("202520260SB711"),
        "summary": "Establishes a permanent state Emergency Rental Assistance Program funded by a 0.1% real estate transfer tax.",
        "subjects": ["Homelessness", "Rental Assistance", "Eviction Prevention"],
        "committees": ["Senate Housing Committee", "Senate Appropriations Committee"],
        "upcoming_hearings": [],
        "actions": [],
        "source": "demo",
        "source_id": "demo_sb711",
        "_prev_status": "Passed Senate Housing Committee — Ayes 6, Noes 3",
    },
    {
        "bill_number": "AB 890",
        "session": "2025-2026",
        "title": "Housing Element Compliance: Enhanced State Enforcement",
        "author": "Alvarez",
        "status": "Referred to Assembly Housing and Community Development Committee",
        "status_date": d(4),
        "introduced_date": d(18),
        "last_updated": today.isoformat(),
        "text_url": leginfo("202520260AB890"),
        "summary": "Strengthens enforcement mechanisms for cities and counties that fail to comply with state housing element law.",
        "subjects": ["Housing Element", "Enforcement", "RHNA", "Local Government"],
        "committees": ["Assembly Housing and Community Development Committee"],
        "upcoming_hearings": [],
        "actions": [],
        "source": "demo",
        "source_id": "demo_ab890",
        "_prev_status": "Introduced",
    },
]

# ---------------------------------------------------------------------------
# Full tracked bill index (all bills — new + changed + historical)
# ---------------------------------------------------------------------------

ALL_BILLS: dict = {}

# Add new bills
for b in NEW_BILLS:
    ALL_BILLS[b["bill_number"]] = b

# Add changed bills (strip _prev_status for storage)
for b in CHANGED_BILLS:
    bill = {k: v for k, v in b.items() if k != "_prev_status"}
    ALL_BILLS[bill["bill_number"]] = bill

# Add a set of additional "in progress" bills to populate the index
ADDITIONAL_BILLS = [
    ("AB 345",  "Housing Accountability Act: Anti-NIMBY Enforcement",       "Bonta",      "Passed Assembly Floor — Ayes 51, Noes 24",                          d(8),  d(75),  leginfo("202520260AB345")),
    ("SB 101",  "Rent Stabilization: Expansion to Post-1995 Buildings",     "Allen",      "Heard — Senate Judiciary Committee",                                 d(6),  d(55),  leginfo("202520260SB101")),
    ("AB 612",  "Transit-Oriented Development: Density Near Rail Stations",  "Chiu",       "Passed Assembly Appropriations Committee — Ayes 14, Noes 5",         d(9),  d(80),  leginfo("202520260AB612")),
    ("SB 338",  "Inclusionary Zoning: 20% Affordable Mandate",              "Skinner",    "Referred to Senate Housing Committee",                               d(7),  d(30),  leginfo("202520260SB338")),
    ("AB 777",  "ADU: Legalization of Unpermitted Units",                    "Quirk-Silva","Introduced",                                                         d(9),  d(9),   leginfo("202520260AB777")),
    ("SB 488",  "Density Bonus: Childcare Facilities Incentive",            "Hurtado",    "Passed Senate Housing Committee — Ayes 8, Noes 1",                   d(10), d(60),  leginfo("202520260SB488")),
    ("AB 1001", "Upzoning: Commercial to Residential Conversion",           "Friedman",   "Amended — substantive fiscal amendments",                            d(11), d(45),  leginfo("202520260AB1001")),
    ("SB 629",  "Accessory Dwelling Units: Junior ADU Expansion",           "Eggman",     "Enrolled — sent to Governor",                                        d(6),  d(120), leginfo("202520260SB629")),
    ("AB 1155", "Eviction Moratorium: Just Cause for Seniors and Disabled", "Kalra",      "Introduced",                                                         d(8),  d(8),   leginfo("202520260AB1155")),
    ("SB 775",  "Housing Finance: Social Housing Authority",                "Kamlager",   "Referred to Senate Housing Committee",                               d(12), d(40),  leginfo("202520260SB775")),
    ("AB 222",  "Mixed-Use Development: Streamlined Approval",              "Santiago",   "Passed Assembly Housing Committee — Ayes 10, Noes 0",                d(13), d(70),  leginfo("202520260AB222")),
    ("SB 944",  "Rent Control: Repeal of Costa-Hawkins Act",                "Grayson",    "Held in Senate Appropriations Committee — 2-year bill",              d(14), d(90),  leginfo("202520260SB944")),
    ("AB 388",  "Local Zoning Override: Housing Crisis Act Extension",      "Ward",       "Passed Assembly Floor — Ayes 48, Noes 27",                           d(10), d(85),  leginfo("202520260AB388")),
    ("SB 512",  "Homelessness: CARE Court Housing Requirements",            "Portantino", "Referred to Senate Judiciary Committee",                             d(9),  d(35),  leginfo("202520260SB512")),
    ("AB 1311", "Infill Development: Environmental Review Exemptions",      "Boerner",    "Introduced",                                                         d(5),  d(5),   leginfo("202520260AB1311")),
    ("SB 821",  "Housing Trust Fund: Permanent Funding Mechanism",          "Wiener",     "Passed Senate Appropriations Committee — Ayes 11, Noes 4",           d(11), d(95),  leginfo("202520260SB821")),
]

for num, title, author, status, sd, introd, url in ADDITIONAL_BILLS:
    ALL_BILLS[num] = {
        "bill_number": num,
        "session": "2025-2026",
        "title": title,
        "author": author,
        "status": status,
        "status_date": sd,
        "introduced_date": introd,
        "last_updated": today.isoformat(),
        "text_url": url,
        "summary": "",
        "subjects": [],
        "committees": [],
        "upcoming_hearings": [],
        "actions": [],
        "source": "demo",
        "source_id": f"demo_{num.replace(' ', '').lower()}",
    }

# ---------------------------------------------------------------------------
# Config stub (only fields email_sender.py reads)
# ---------------------------------------------------------------------------

CONFIG = {
    "legislative": {"lookback_days": 14},
    "email": {"include_full_index": True},
    "github": {"pages_url": "https://twgonzalez.github.io/csf-agents/"},
}

# ---------------------------------------------------------------------------
# Build and write
# ---------------------------------------------------------------------------

def main() -> None:
    date_str = today.strftime("%Y-%m-%d")
    html = _build_html(
        new_bills=NEW_BILLS,
        changed_bills=CHANGED_BILLS,
        all_bills=ALL_BILLS,
        config=CONFIG,
        date_str=date_str,
    )

    output_dir = PROJECT_ROOT / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "demo_email.html"
    out_path.write_text(html, encoding="utf-8")

    print(f"\n✓ Demo email written to: {out_path.relative_to(PROJECT_ROOT)}")
    print(f"  New bills       : {len(NEW_BILLS)}")
    print(f"  Status changes  : {len(CHANGED_BILLS)}")
    print(f"  Total in index  : {len(ALL_BILLS)}")
    print(f"\n  Open in browser : file://{out_path}\n")


if __name__ == "__main__":
    main()
