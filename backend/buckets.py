"""Pure bucket logic for the Indonesia task monitor — no I/O, no CRM, no DB.

Everything here is a plain function over a task dict so it can be unit-tested directly
(see test_buckets.py). Two CRM quirks drive most of the code:

  1. `due_date` arrives in TWO shapes. Native 2026 tasks send a full ISO timestamp with a
     Z suffix ("2026-08-14T12:12:00Z"); legacy Salesforce rows send a bare date
     ("2025-12-31"). A bare date is treated as END of that day in Jakarta — a task due
     "31 Dec" is not overdue at 09:00 on 31 Dec.
  2. "Never updated" has three different spellings in the data: `updated_at` is null
     (native, never touched), `updated_at == created_at` (legacy import), or
     `version == 1`. Any one of them counts.

All comparisons happen in Asia/Jakarta. This matters: a task due "2026-08-14T12:12:00Z"
is due 19:12 WIB, so it is NOT overdue during the Jakarta morning even though the UTC
clock has passed noon.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

JAKARTA = ZoneInfo("Asia/Jakarta")

# Statuses that mean "no longer needs attention". The CRM picklist documents
# Not Started / In Progress / Completed / Deferred, but the live data also contains
# "Open" (an undocumented Salesforce-era value) — so never assume the picklist is
# exhaustive; treat anything not listed here as still open.
CLOSED_STATUSES = {"completed"}


def parse_crm_datetime(value, tz: ZoneInfo = JAKARTA) -> datetime | None:
    """Parse a CRM date/datetime into a tz-aware datetime, or None if absent/unparseable.

    Bare dates ("2025-12-31") become 23:59:59 local — the end of the due day.
    Timestamps keep their real instant and are converted into `tz`.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(tz)
    if isinstance(value, date):
        return datetime.combine(value, time(23, 59, 59), tzinfo=tz)

    text = str(value).strip()
    if not text:
        return None

    # Date-only -> end of that day, local.
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        try:
            d = date.fromisoformat(text)
        except ValueError:
            return None
        return datetime.combine(d, time(23, 59, 59), tzinfo=tz)

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def is_open(task: dict) -> bool:
    """True when the task still needs attention (i.e. isn't Completed)."""
    status = (task.get("status") or "").strip().lower()
    return status not in CLOSED_STATUSES


def last_touched_at(task: dict, tz: ZoneInfo = JAKARTA) -> datetime | None:
    """When the task was last meaningfully written, falling back to creation time."""
    return parse_crm_datetime(task.get("updated_at"), tz) or parse_crm_datetime(
        task.get("created_at"), tz
    )


def never_updated(task: dict, tz: ZoneInfo = JAKARTA) -> bool:
    """True when nothing has been written to the task since it was created."""
    if task.get("version") == 1:
        return True
    updated = parse_crm_datetime(task.get("updated_at"), tz)
    if updated is None:
        return True
    created = parse_crm_datetime(task.get("created_at"), tz)
    return created is not None and updated == created


def compute_flags(task: dict, now: datetime, stale_days: int = 14) -> dict:
    """Return the bucket flags plus the numbers the UI shows alongside them.

    Buckets are NOT mutually exclusive — a task can be overdue AND never updated. Only
    open tasks are ever flagged; a Completed task gets all-False so it can still be
    listed without polluting the counts.
    """
    tz = now.tzinfo if isinstance(now.tzinfo, ZoneInfo) else JAKARTA
    open_task = is_open(task)

    due = parse_crm_datetime(task.get("due_date"), tz)
    created = parse_crm_datetime(task.get("created_at"), tz)
    touched = last_touched_at(task, tz)
    fresh = never_updated(task, tz)

    days_since_touch = (now - touched).days if touched else None

    # A due date earlier than the creation date is impossible as real work. In the live
    # data it clusters at ~730 days (the year typed as 2024 instead of 2026) — a
    # data-entry artifact, not late work, so it's split out and excluded from `overdue`
    # rather than inflating that count.
    bad_due_date = bool(open_task and due is not None and created is not None and due < created)
    overdue = bool(open_task and due is not None and due < now and not bad_due_date)
    days_overdue = (now - due).days if overdue else None

    # "Stale" deliberately excludes never-updated tasks so the two cards don't
    # double-count the same row; the UI reads them as distinct populations.
    stale = bool(
        open_task
        and not fresh
        and days_since_touch is not None
        and days_since_touch > stale_days
    )

    return {
        "never_updated": bool(open_task and fresh),
        "stale": stale,
        "overdue": overdue,
        "no_due_date": bool(open_task and due is None),
        "bad_due_date": bad_due_date,
        "days_overdue": days_overdue,
        "days_since_touch": days_since_touch,
        "is_open": open_task,
        "due_at": due.isoformat() if due else None,
        "last_touched_at": touched.isoformat() if touched else None,
    }


def summarize(tasks: list[dict], now: datetime, stale_days: int = 14) -> dict:
    """Roll a list of already-flagged tasks up into the dashboard counters."""
    counts = {
        "total": len(tasks),
        "open": 0,
        "never_updated": 0,
        "stale": 0,
        "overdue": 0,
        "no_due_date": 0,
        "bad_due_date": 0,
    }
    for task in tasks:
        flags = task.get("flags") or compute_flags(task, now, stale_days)
        if flags["is_open"]:
            counts["open"] += 1
        for key in ("never_updated", "stale", "overdue", "no_due_date", "bad_due_date"):
            if flags[key]:
                counts[key] += 1
    return counts


def now_jakarta() -> datetime:
    return datetime.now(JAKARTA)
