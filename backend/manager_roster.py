"""Sales-manager lookup, sourced from a manually-uploaded CSV export.

Originally designed around a live "publish to web" Google Sheet fetch, but the org's
Google Workspace policy blocks external/link-based sharing entirely (confirmed 2026-08-14
— there is no "Anyone with the link" option, only Restricted / within-organization). So
the roster owner exports the sheet ("ID-User Master", tab "List") to CSV by hand and
uploads it via `POST /api/manager-roster` — roughly monthly, per the plan — rather than
this app fetching it itself.

`ASM` is each salesperson's direct manager. Matched against a task/opportunity's Owner
display name — not every owner in this app's data is present in that sheet (people
outside the tab's particular org slice), and some who ARE present have a blank ASM
because they sit at the top of that sheet's hierarchy. Both cases fall back to
"No Manager" rather than crashing or silently dropping the row.

`Email` powers the per-viewer visibility rule (see main.py's resolve_viewer_scope): the
SSO-forwarded email is looked up here to find the viewer's own roster Name, which is then
used to decide whether they see only their own tasks or their team's. `ASM` and `RSM`
hold the same value for every row, so a direct-manager match only needs one hop off
`ASM`. `Sales Head` is a separate, higher hierarchy level (confirmed 2026-08-20: a Sales
Head like Muhammad Yan Mallino has ~7 people under him whose `ASM`/`RSM` point at a
different, lower manager, not at him) — someone at that level is recognized as managing a
team by matching `Sales Head` directly rather than walking the full ASM->RSM->Sales Head
chain, since only two levels are observed in practice so far.
"""
from __future__ import annotations

import csv
import io

NO_MANAGER = "No Manager"

# Columns the parser depends on. Checked explicitly on upload so a sheet restructure
# (column renamed/removed) fails loudly with a clear message instead of silently
# producing an empty or wrong roster — this is the main thing a monthly re-upload is
# meant to catch. Email and Sales Head are required now that they drive per-viewer
# visibility, not just the Manager column.
REQUIRED_COLUMNS = ("Name", "ASM", "Email", "Sales Head")

# Manual overrides, given directly by the user (2026-08-14) for people who ARE in the
# roster but whose ASM column is blank there — they sit at the top of that sheet's
# hierarchy, so the sheet has no "manager" for them even though one exists in practice.
# Checked before the roster lookup so a blank ASM doesn't shadow this.
MANAGER_OVERRIDES = {
    "Cendanawangi Lumoindong": "Region Head",
    "Eleonora Irsya Listianingrum": "Cendanawangi Lumoindong",
    "Januar Aden Nugroho": "Muhammad Yan Mallino",
    "Muhammad Yan Mallino": "Region Head",
    "Willa Rivoni": "Cendanawangi Lumoindong",
}


class ManagerRosterFormatError(ValueError):
    """Raised when an uploaded CSV is missing a column this app depends on."""


def parse_manager_roster_csv(text: str) -> list[dict]:
    """Parse an uploaded CSV into [{"name", "manager", "email", "sales_head"}, ...].
    Raises ManagerRosterFormatError if the expected columns aren't present — never
    returns a silently-empty/wrong roster."""
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
    if missing:
        raise ManagerRosterFormatError(
            f"CSV is missing required columns: {', '.join(missing)}. "
            f"Columns found: {', '.join(fieldnames) or '(none)'}"
        )

    rows: list[dict] = []
    seen: set[str] = set()
    for row in reader:
        name = (row.get("Name") or "").strip()
        asm = (row.get("ASM") or "").strip()
        if asm in ("", "-"):
            asm = ""
        email = (row.get("Email") or "").strip()
        sales_head = (row.get("Sales Head") or "").strip()
        if sales_head in ("", "-"):
            sales_head = ""
        if name and name not in seen:
            seen.add(name)
            rows.append({"name": name, "manager": asm, "email": email, "sales_head": sales_head})
    return rows


def get_manager(owner_name: str | None, roster: dict[str, str]) -> str:
    """Resolve an owner's manager: override first, then the roster, else "No Manager"."""
    if not owner_name:
        return NO_MANAGER
    if owner_name in MANAGER_OVERRIDES:
        return MANAGER_OVERRIDES[owner_name]
    manager = roster.get(owner_name)
    return manager or NO_MANAGER
