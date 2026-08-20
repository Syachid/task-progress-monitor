"""Tests for the bucket logic — the part that decides what Sales Ops actually chases.

The fixtures use shapes taken from real CRM rows (see the plan's data findings): native
2026 tasks with `updated_at: null` and a Z-suffixed due date, and legacy Salesforce rows
with `updated_at == created_at` and a bare date.
"""
from datetime import datetime, timedelta

import pytest

from buckets import (
    JAKARTA,
    compute_flags,
    is_open,
    never_updated,
    parse_crm_datetime,
    summarize,
)

NOW = datetime(2026, 8, 14, 9, 0, 0, tzinfo=JAKARTA)  # 09:00 WIB


# ------------------------------------------------------------------ parse_crm_datetime


def test_parses_bare_date_as_end_of_day_jakarta():
    """A task due "31 Dec" isn't overdue at breakfast on 31 Dec."""
    dt = parse_crm_datetime("2025-12-31")
    assert (dt.year, dt.month, dt.day) == (2025, 12, 31)
    assert (dt.hour, dt.minute) == (23, 59)
    assert dt.tzinfo is JAKARTA


def test_parses_z_suffixed_timestamp_into_jakarta():
    dt = parse_crm_datetime("2026-08-14T12:12:00Z")
    assert (dt.hour, dt.minute) == (19, 12)  # 12:12 UTC == 19:12 WIB


@pytest.mark.parametrize("value", [None, "", "not-a-date", "2026-13-45"])
def test_unparseable_values_become_none(value):
    assert parse_crm_datetime(value) is None


# ------------------------------------------------------------------------ never_updated


def test_never_updated_when_updated_at_is_null():
    """Native tasks arrive with updated_at null until someone edits them."""
    assert never_updated({"created_at": "2026-08-01T03:10:38Z", "updated_at": None, "version": 1})


def test_never_updated_when_updated_equals_created():
    """Legacy imported rows carry updated_at == created_at."""
    task = {
        "created_at": "2026-04-28T14:41:36+00:00",
        "updated_at": "2026-04-28T14:41:36+00:00",
        "version": 2,
    }
    assert never_updated(task)


def test_edited_task_is_not_never_updated():
    task = {
        "created_at": "2026-08-10T03:15:04Z",
        "updated_at": "2026-08-10T03:18:16Z",
        "version": 4,
    }
    assert not never_updated(task)


# -------------------------------------------------------------------------- is_open


@pytest.mark.parametrize("status", ["Not Started", "In Progress", "Open", "Deferred", None])
def test_non_completed_statuses_are_open(status):
    """`Open` is a real value in the data even though describe_object omits it."""
    assert is_open({"status": status})


def test_completed_is_closed():
    assert not is_open({"status": "Completed"})


# ---------------------------------------------------------------------- compute_flags


def test_due_later_today_in_wib_is_not_overdue_yet():
    """12:12Z is 19:12 WIB — still ahead of a 09:00 WIB "now"."""
    flags = compute_flags(
        {"status": "Not Started", "due_date": "2026-08-14T12:12:00Z",
         "created_at": "2026-08-14T04:12:42Z", "updated_at": None, "version": 1},
        NOW,
    )
    assert flags["overdue"] is False
    assert flags["never_updated"] is True


def test_past_due_date_is_overdue_with_day_count():
    flags = compute_flags(
        {"status": "Open", "due_date": "2026-07-31",
         "created_at": "2026-04-28T14:41:36Z", "updated_at": "2026-04-28T14:41:36Z", "version": 1},
        NOW,
    )
    assert flags["overdue"] is True
    assert flags["days_overdue"] == 13  # 31 Jul 23:59 -> 14 Aug 09:00


def test_missing_due_date_flags_no_due_date_and_not_overdue():
    flags = compute_flags(
        {"status": "Not Started", "created_at": "2026-08-14T03:10:38Z",
         "updated_at": None, "version": 1},
        NOW,
    )
    assert flags["no_due_date"] is True
    assert flags["overdue"] is False


def test_completed_task_is_never_flagged():
    flags = compute_flags(
        {"status": "Completed", "due_date": "2020-01-01",
         "created_at": "2019-01-01T00:00:00Z", "updated_at": None, "version": 1},
        NOW,
    )
    assert not any(
        flags[k] for k in ("never_updated", "stale", "overdue", "no_due_date", "bad_due_date")
    )


# --------------------------------------------------------------------- bad due date


def test_due_date_before_created_is_bad_due_date_not_overdue():
    """The live data's real failure mode: due date typed with the year one off (2024
    instead of 2026), landing 730 days before the task was even created. This is a
    data-entry artifact, not a task someone let slip — it must not inflate `overdue`."""
    flags = compute_flags(
        {"status": "Not Started", "due_date": "2024-07-22",
         "created_at": "2026-07-22T08:55:18Z", "updated_at": None, "version": 1},
        NOW,
    )
    assert flags["bad_due_date"] is True
    assert flags["overdue"] is False
    assert flags["days_overdue"] is None


def test_due_date_after_created_and_past_is_ordinary_overdue():
    """Sanity check the two flags aren't accidentally coupled the other way."""
    flags = compute_flags(
        {"status": "Not Started", "due_date": "2026-07-31",
         "created_at": "2026-04-28T14:41:36Z", "updated_at": None, "version": 1},
        NOW,
    )
    assert flags["bad_due_date"] is False
    assert flags["overdue"] is True


def test_completed_task_with_backwards_due_date_is_not_flagged():
    """Bad-due-date detection still respects the open/closed gate like every other flag."""
    flags = compute_flags(
        {"status": "Completed", "due_date": "2024-07-22",
         "created_at": "2026-07-22T08:55:18Z", "updated_at": None, "version": 1},
        NOW,
    )
    assert flags["bad_due_date"] is False


# ------------------------------------------------------------------- stale boundary


def _edited_task(days_ago_updated: int):
    """An edited task (so it isn't 'never updated') last touched N days before NOW."""
    updated = NOW - timedelta(days=days_ago_updated)
    return {
        "status": "Not Started",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": updated.isoformat(),
        "version": 3,
    }


def test_exactly_at_threshold_is_not_stale():
    """`> stale_days`, not `>=` — 14 days on the dot is still inside the window."""
    assert compute_flags(_edited_task(14), NOW, stale_days=14)["stale"] is False


def test_one_day_past_threshold_is_stale():
    flags = compute_flags(_edited_task(15), NOW, stale_days=14)
    assert flags["stale"] is True
    assert flags["days_since_touch"] == 15


def test_never_updated_task_is_not_also_counted_as_stale():
    """The two cards must not double-count the same row."""
    flags = compute_flags(
        {"status": "Not Started", "created_at": "2026-01-01T00:00:00Z",
         "updated_at": None, "version": 1},
        NOW,
        stale_days=14,
    )
    assert flags["never_updated"] is True
    assert flags["stale"] is False


# ------------------------------------------------------------------------- summarize


def test_summarize_counts_each_bucket_independently():
    tasks = [
        {"status": "Not Started", "created_at": "2026-01-01T00:00:00Z",
         "updated_at": None, "version": 1},                              # never + no_due
        {"status": "Open", "due_date": "2026-07-01",
         "created_at": "2026-06-01T00:00:00Z",
         "updated_at": "2026-06-02T00:00:00Z", "version": 2},            # overdue + stale
        {"status": "Completed", "created_at": "2026-01-01T00:00:00Z",
         "updated_at": None, "version": 1},                              # ignored
    ]
    counts = summarize(tasks, NOW, stale_days=14)
    assert counts == {
        "total": 3, "open": 2, "never_updated": 1,
        "stale": 1, "overdue": 1, "no_due_date": 1, "bad_due_date": 0,
    }
