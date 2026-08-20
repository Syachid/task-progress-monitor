"""OceanBase (MySQL-wire) storage layer, via asyncmy.

Ported 2026-08-20 from the local SQLite verification build — every function here is now
async and uses `%s` placeholders, per the Substrait deploy contract. Schema lives
entirely in Flyway migrations (`backend/resources/db/migration/V*.sql`); this module
never issues DDL.

Tasks are stored with their raw CRM payload in `raw` (JSON) so the UI can surface fields
we didn't think to promote into columns, without a re-sync.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

import asyncmy

_pool = None


def _dsn() -> dict:
    # DATABASE_URL looks like: mysql://user%40tenant:password@host:2881/dbname
    u = urlparse(os.environ["DATABASE_URL"])
    return {
        "host": u.hostname,
        "port": u.port or 2881,
        "user": unquote(u.username or ""),
        "password": unquote(u.password or ""),
        "db": (u.path or "/").lstrip("/"),
    }


async def init_db() -> None:
    global _pool
    if _pool is None:
        _pool = await asyncmy.create_pool(**_dsn(), autocommit=True)


async def close_db() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        await _pool.wait_closed()
        _pool = None


TASK_COLUMNS = (
    "id", "subject", "description", "status", "priority", "task_type", "due_date",
    "created_at", "updated_at", "version", "created_by", "owner_id", "owner_name",
    "assigned_to_id", "related_object_type", "related_record_id",
)


def _row_to_task(row: tuple) -> dict:
    task = dict(zip(TASK_COLUMNS, row[: len(TASK_COLUMNS)]))
    raw = row[len(TASK_COLUMNS)]
    if raw:
        # The raw CRM payload isn't surfaced by any endpoint today, but keeping it
        # parsed (not just the JSON string) matches what a future column-promotion
        # would expect to read.
        task["raw"] = json.loads(raw)
    return task


async def replace_tasks(tasks: list[dict]) -> int:
    """Swap the whole task table for a fresh pull.

    Full replace rather than upsert because the working set is tiny (~600 rows) and a
    replace also drops rows that were deleted or re-typed in CRM since the last sync.
    """
    rows = [
        tuple(task.get(col) for col in TASK_COLUMNS) + (json.dumps(task, default=str),)
        for task in tasks
    ]
    placeholders = ",".join(["%s"] * (len(TASK_COLUMNS) + 1))
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM tasks")
        if rows:
            await cur.executemany(
                f"INSERT INTO tasks ({','.join(TASK_COLUMNS)}, raw) VALUES ({placeholders})",
                rows,
            )
        await cur.execute(
            "INSERT INTO meta (`key`, value) VALUES ('last_sync_at', %s) "
            "ON DUPLICATE KEY UPDATE value = VALUES(value)",
            (datetime.now(timezone.utc).isoformat(),),
        )
    return len(rows)


async def all_tasks() -> list[dict]:
    cols = ",".join(TASK_COLUMNS)
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(f"SELECT {cols}, raw FROM tasks")
        rows = await cur.fetchall()
    return [_row_to_task(r) for r in rows]


async def task_count() -> int:
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM tasks")
        (count,) = await cur.fetchone()
    return count


async def save_opportunities(records: list[dict]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            r.get("id"), r.get("name"), r.get("account_name"), r.get("stage"),
            r.get("owner_name"), r.get("account_id"), r.get("lead_source_detail"), now,
        )
        for r in records
    ]
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO opportunities "
            "(id, name, account_name, stage, owner_name, account_id, lead_source_detail, fetched_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
            "name=VALUES(name), account_name=VALUES(account_name), "
            "stage=VALUES(stage), owner_name=VALUES(owner_name), "
            "account_id=VALUES(account_id), lead_source_detail=VALUES(lead_source_detail), "
            "fetched_at=VALUES(fetched_at)",
            rows,
        )


async def opportunities_by_id() -> dict[int, dict]:
    cols = ("id", "name", "account_name", "stage", "owner_name", "account_id", "lead_source_detail", "fetched_at")
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(f"SELECT {','.join(cols)} FROM opportunities")
        rows = await cur.fetchall()
    return {r[0]: dict(zip(cols, r)) for r in rows}


async def known_opportunity_ids() -> set[int]:
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT id FROM opportunities")
        rows = await cur.fetchall()
    return {r[0] for r in rows}


async def replace_account_tags(tags_by_account: dict[int, list[str]]) -> None:
    """Full replace, same reasoning as `replace_tasks` — the tagged set is small (~30
    accounts) and can shrink (an account losing its tag shouldn't leave a stale row)."""
    rows = [(account_id, ",".join(tags)) for account_id, tags in tags_by_account.items()]
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM account_tags")
        if rows:
            await cur.executemany(
                "INSERT INTO account_tags (account_id, tags) VALUES (%s, %s)", rows
            )


async def account_tags_by_id() -> dict[int, list[str]]:
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT account_id, tags FROM account_tags")
        rows = await cur.fetchall()
    return {r[0]: (r[1].split(",") if r[1] else []) for r in rows}


async def replace_manager_roster(rows: list[dict]) -> None:
    """`rows` = [{"name", "manager", "email", "sales_head"}, ...] from
    parse_manager_roster_csv."""
    values = [
        (r["name"], r["manager"], r["email"] or None, r.get("sales_head") or None)
        for r in rows
    ]
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM manager_roster")
        if values:
            await cur.executemany(
                "INSERT INTO manager_roster (name, manager, email, sales_head) VALUES (%s, %s, %s, %s)",
                values,
            )


async def manager_roster() -> dict[str, str]:
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT name, manager FROM manager_roster")
        rows = await cur.fetchall()
    return {r[0]: r[1] for r in rows}


async def manager_roster_email_map() -> dict[str, str]:
    """Lowercased email -> roster Name, for resolving an SSO-forwarded email to a
    person's Owner display name (see main.py's resolve_viewer_scope)."""
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT name, email FROM manager_roster WHERE email IS NOT NULL AND email != ''")
        rows = await cur.fetchall()
    return {email.strip().lower(): name for name, email in rows}


async def manager_roster_sales_head() -> dict[str, str]:
    """Name -> Sales Head, the second hierarchy level checked by
    main.py's resolve_viewer_scope alongside the ASM-based `manager_roster()`."""
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT name, sales_head FROM manager_roster WHERE sales_head IS NOT NULL AND sales_head != ''")
        rows = await cur.fetchall()
    return {r[0]: r[1] for r in rows}


async def manager_roster_names_without_email() -> list[str]:
    """Roster rows with a Name but no Email — these people can't be matched to an SSO
    login, so the per-viewer visibility rule can't restrict them. Surfaced so the roster
    owner can fill the gap rather than it silently granting them full access forever."""
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT name FROM manager_roster WHERE email IS NULL OR TRIM(email) = ''"
        )
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def delete_marked_ids() -> set[int]:
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT task_id FROM delete_marks")
        rows = await cur.fetchall()
    return {r[0] for r in rows}


async def set_delete_mark(task_id: int, marked_by: str | None) -> None:
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO delete_marks (task_id, marked_by, marked_at) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE marked_by = VALUES(marked_by), marked_at = VALUES(marked_at)",
            (task_id, marked_by, datetime.now(timezone.utc).isoformat()),
        )


async def clear_delete_mark(task_id: int) -> None:
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM delete_marks WHERE task_id = %s", (task_id,))


async def get_meta(key: str) -> str | None:
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT value FROM meta WHERE `key` = %s", (key,))
        row = await cur.fetchone()
    return row[0] if row else None


async def set_meta(key: str, value: str) -> None:
    async with _pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO meta (`key`, value) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE value = VALUES(value)",
            (key, value),
        )
