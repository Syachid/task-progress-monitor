"""FastAPI app: Indonesia task progress monitor. Deployed on Substrait (`/health`,
everything else under `/api`, port 8000; OceanBase/MySQL via store.py, Flyway
migrations in resources/db/migration/).

Read-only end to end (against CRM): it pulls Tasks from CRM, caches them in the
database, and computes the overdue / stale / never-updated / no-due-date buckets
locally (the CRM API only supports equality filters, so date logic cannot be pushed
down into the query).
"""
from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, Response  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import buckets  # noqa: E402
import manager_roster as manager_roster_module  # noqa: E402
import store  # noqa: E402
from crm_client import CrmClient, opportunity_deep_link  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
DEFAULT_STALE_DAYS = int(os.getenv("STALE_DAYS", "14"))
# Enriching Opportunity names costs one request per distinct opportunity. Capped so a
# first run can't turn into a thousand serial calls; the rest fill in on later syncs.
MAX_ENRICH_PER_SYNC = int(os.getenv("MAX_ENRICH_PER_SYNC", "400"))
# MUST WIN is a straight substring match on lead_source_detail, same rule as the
# validated build_report.ps1 (case-insensitive, tolerant of "Must Win" vs "MustWin").
MUST_WIN_PATTERN = re.compile(r"must\s*win", re.IGNORECASE)
ACCOUNT_TAG_QUERIES = ("Hypercare", "Strategic")
# Who may upload/replace the manager roster. Checked against X-Forwarded-Email; absent
# header (local dev, no SSO) is allowed through so testing doesn't need a fake header.
ROSTER_ADMIN_EMAILS = {
    e.strip().lower() for e in os.getenv("ROSTER_ADMIN_EMAILS", "").split(",") if e.strip()
}

crm = CrmClient()
_sync_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.init_db()
    # Only auto-sync on a cold cache, so restarting during UI work doesn't re-hit CRM.
    if crm.configured and await store.task_count() == 0:
        try:
            await run_sync()
        except Exception as exc:  # noqa: BLE001 - surfaced via /api/summary instead
            await store.set_meta("last_sync_error", str(exc))
    yield
    await store.close_db()


app = FastAPI(title="Task Progress Monitor — Indonesia", lifespan=lifespan)


@app.middleware("http")
async def no_store_api_cache(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store"
    return response


# --------------------------------------------------------------------------- helpers


def creator_names(tasks: list[dict]) -> dict[int, str]:
    """Map CRM user id -> display name.

    The list endpoint returns `owner_name` but never `created_by_name`, so we harvest
    names from rows where the owner IS the creator (true for every native task observed)
    and reuse that map for `created_by` everywhere else.
    """
    names: dict[int, str] = {}
    for task in tasks:
        owner_id, created_by = task.get("owner_id"), task.get("created_by")
        name = task.get("owner_name")
        if name and owner_id is not None:
            names.setdefault(owner_id, name)
        if name and owner_id == created_by and created_by is not None:
            names[created_by] = name
    return names


def derive_tags(opp: dict, account_tags: dict[int, list[str]]) -> list[str]:
    """MUST WIN from the Opportunity's own lead_source_detail; HYPERCARE/STRATEGIC from
    the cached Account tag lookup (see enrich_account_tags). Order matches the validated
    build_report.ps1 output: MUST WIN, then HYPERCARE, then STRATEGIC."""
    tags: list[str] = []
    if opp and MUST_WIN_PATTERN.search(opp.get("lead_source_detail") or ""):
        tags.append("MUST WIN")
    for tag in account_tags.get(opp.get("account_id"), []) if opp else []:
        if tag not in tags:
            tags.append(tag)
    return tags


async def decorate(tasks: list[dict], stale_days: int) -> list[dict]:
    """Attach bucket flags, creator name, tags, manager and CRM deep link to each row."""
    now = buckets.now_jakarta()
    names = creator_names(tasks)
    opps = await store.opportunities_by_id()
    account_tags = await store.account_tags_by_id()
    roster = await store.manager_roster()
    marked_ids = await store.delete_marked_ids()

    decorated = []
    for task in tasks:
        row = dict(task)
        row.pop("raw", None)
        flags = buckets.compute_flags(task, now, stale_days)
        created_by = task.get("created_by")
        owner_name = task.get("owner_name")
        opp = opps.get(task.get("related_record_id")) or {}
        row["flags"] = flags
        row["created_by_name"] = names.get(created_by) or (
            f"User #{created_by}" if created_by is not None else "—"
        )
        row["opportunity_name"] = opp.get("name")
        row["account_name"] = opp.get("account_name")
        row["opportunity_stage"] = opp.get("stage")
        row["tags"] = derive_tags(opp, account_tags)
        row["manager"] = manager_roster_module.get_manager(owner_name, roster)
        row["delete_marked"] = task.get("id") in marked_ids
        row["crm_link"] = (
            opportunity_deep_link(task.get("related_record_id"))
            if task.get("related_object_type") == "Opportunity"
            else None
        )
        decorated.append(row)
    return decorated


async def resolve_viewer_scope(email: str | None) -> dict:
    """Decide which Owner names `email` (the SSO-forwarded X-Forwarded-Email header) may
    see, per the rule the roster owner set on 2026-08-20:

      - Not matched in the roster at all (no SSO, or a genuine non-Sales viewer like
        Legal/Admin) -> full access, unfiltered.
      - Matched, and their own Name appears as someone else's `ASM` OR `Sales Head`
        (i.e. they manage people, at either hierarchy level) -> their team's tasks plus
        their own.
      - Matched, but nobody's `ASM`/`Sales Head` points at them -> only their own tasks.

    `ASM`/`RSM` hold the same value for every roster row, so a direct-manager match only
    needs one hop off `ASM`. `Sales Head` is checked separately as its own hop — a Sales
    Head's reports point at a different, lower `ASM`, not at the Sales Head directly (see
    manager_roster.py's module docstring; confirmed 2026-08-20 with Muhammad Yan Mallino,
    whose 7-person team only showed up once `Sales Head` was matched too).

    Every name comparison here is case-insensitive. The roster is hand-maintained and
    already has at least one row (Willa Rivoni, uploaded 2026-08-20) whose own Name is
    typo'd in casing relative to how CRM spells it on tasks and how the sheet's own ASM
    column spells it for her reports — an exact-match comparison would silently lock that
    person out of every one of their own tasks rather than degrading gracefully. Worth
    fixing at the source (the roster CSV) too, but this app shouldn't depend on it.
    """
    if not email:
        return {"mode": "all", "owner_names": None, "viewer_name": None}
    email_map = await store.manager_roster_email_map()
    viewer_name = email_map.get(email.strip().lower())
    if not viewer_name:
        return {"mode": "all", "owner_names": None, "viewer_name": None}

    roster = await store.manager_roster()
    sales_head = await store.manager_roster_sales_head()
    viewer_key = viewer_name.casefold()
    managed = {
        name for name in roster
        if manager_roster_module.get_manager(name, roster).casefold() == viewer_key
    }
    managed |= {
        name for name, head in sales_head.items()
        if head.casefold() == viewer_key
    }
    if managed:
        return {"mode": "team", "owner_names": managed | {viewer_name}, "viewer_name": viewer_name}
    return {"mode": "self", "owner_names": {viewer_name}, "viewer_name": viewer_name}


def is_roster_admin(email: str | None) -> bool:
    """Whether `email` may upload/replace the manager roster. No header at all (local
    dev, or SSO off) is treated as allowed — same "unrestricted without SSO" convention
    as resolve_viewer_scope — so this only actually restricts anything once deployed
    behind Substrait's Google SSO with ROSTER_ADMIN_EMAILS set.
    """
    if not email:
        return True
    return email.strip().lower() in ROSTER_ADMIN_EMAILS


def filter_by_scope(rows: list[dict], scope: dict) -> list[dict]:
    if scope["mode"] == "all":
        return rows
    names = {n.casefold() for n in scope["owner_names"]}
    return [r for r in rows if (r.get("owner_name") or "").casefold() in names]


async def enrich_opportunities(tasks: list[dict]) -> int:
    """Cache Opportunity name/account for the tasks we just synced.

    Runs after the task sync so the main list is usable immediately; only fetches ids we
    don't already hold, so repeat syncs are nearly free.
    """
    wanted = {
        t.get("related_record_id")
        for t in tasks
        if t.get("related_object_type") == "Opportunity" and t.get("related_record_id")
    }
    missing = sorted(wanted - await store.known_opportunity_ids())[:MAX_ENRICH_PER_SYNC]
    if not missing:
        return 0

    fetched: list[dict] = []
    semaphore = asyncio.Semaphore(6)

    async def one(opp_id):
        async with semaphore:
            try:
                record = await crm.get_opportunity(opp_id)
            except Exception:  # noqa: BLE001 - enrichment is best-effort
                return
            if record:
                fetched.append(
                    {
                        "id": opp_id,
                        "name": record.get("name"),
                        "account_name": record.get("account_name"),
                        "stage": record.get("stage"),
                        "owner_name": record.get("owner_name"),
                        "account_id": record.get("account_id"),
                        "lead_source_detail": record.get("lead_source_detail"),
                    }
                )

    await asyncio.gather(*(one(i) for i in missing))
    if fetched:
        await store.save_opportunities(fetched)
    return len(fetched)


async def enrich_account_tags() -> int:
    """Rebuild the Strategic/Hypercare account-tag cache from CRM Account search.

    Two searches (one per keyword) find the root accounts whose own
    `customer_success_manager` text carries the tag; children inherit their tag through
    `parent_account_id` (only walked one level deep — the only depth observed in this
    data. See crm_client.search_accounts for the "live REST API must support ?search="
    assumption this depends on).
    """
    root_tags: dict[int, set[str]] = {}
    for query in ACCOUNT_TAG_QUERIES:
        accounts = await crm.search_accounts(query)
        tag = query.upper()
        for acc in accounts:
            acc_id = acc.get("id")
            csm = acc.get("customer_success_manager") or ""
            if acc_id is not None and tag in csm.upper():
                root_tags.setdefault(acc_id, set()).add(tag)

    all_tags: dict[int, set[str]] = {k: set(v) for k, v in root_tags.items()}
    for root_id, tags in root_tags.items():
        children = await crm.get_children_accounts(root_id)
        for child in children:
            child_id = child.get("id")
            if child_id is not None:
                all_tags.setdefault(child_id, set()).update(tags)

    ordered = {acc_id: sorted(tags, key=("HYPERCARE", "STRATEGIC").index) for acc_id, tags in all_tags.items()}
    await store.replace_account_tags(ordered)
    return len(ordered)


async def run_sync() -> dict:
    """Pull native Indonesia tasks from CRM, then enrich opportunities/tags.

    The manager roster is NOT touched here — it has no live source (the org's Workspace
    policy blocks external sheet sharing entirely), so it only changes via the manual
    `POST /api/manager-roster` upload. A CRM sync leaves whatever roster is already
    cached untouched.
    """
    if not crm.configured:
        raise HTTPException(
            status_code=503,
            detail="CRM not configured — set CRM_API_BASE_URL and CRM_API_KEY in backend/.env",
        )
    async with _sync_lock:
        tasks = await crm.fetch_indonesia_tasks()
        count = await store.replace_tasks(tasks)
        await store.set_meta("last_sync_error", "")
        enriched = await enrich_opportunities(tasks)

        tagged_accounts = 0
        try:
            tagged_accounts = await enrich_account_tags()
        except Exception as exc:  # noqa: BLE001 - best-effort, tags just stay stale
            await store.set_meta("last_account_tag_error", str(exc))

    return {
        "synced": count,
        "opportunities_enriched": enriched,
        "tagged_accounts": tagged_accounts,
    }


# -------------------------------------------------------------------------- endpoints


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/summary")
async def summary(request: Request, stale_days: int = Query(DEFAULT_STALE_DAYS, ge=1, le=365)):
    scope = await resolve_viewer_scope(request.headers.get("x-forwarded-email"))
    tasks = filter_by_scope(await decorate(await store.all_tasks(), stale_days), scope)
    counts = buckets.summarize(tasks, buckets.now_jakarta(), stale_days)
    return {
        "counts": counts,
        "stale_days": stale_days,
        "last_sync_at": await store.get_meta("last_sync_at"),
        "last_sync_error": await store.get_meta("last_sync_error") or None,
        "crm_configured": crm.configured,
        "generated_at": buckets.now_jakarta().isoformat(),
    }


@app.get("/api/tasks")
async def list_tasks(
    request: Request,
    bucket: str | None = Query(
        None,
        pattern="^(never_updated|stale|overdue|no_due_date|bad_due_date|open|all)$",
    ),
    creator: int | None = None,
    owner: str | None = None,
    manager: str | None = None,
    alex_direction: bool = False,
    task_type: str = "Task",
    q: str | None = None,
    sort: str = Query("last_touched", pattern="^(last_touched|due_date|created_at|subject)$"),
    stale_days: int = Query(DEFAULT_STALE_DAYS, ge=1, le=365),
):
    scope = await resolve_viewer_scope(request.headers.get("x-forwarded-email"))
    rows = filter_by_scope(await decorate(await store.all_tasks(), stale_days), scope)

    if task_type and task_type != "all":
        rows = [r for r in rows if (r.get("task_type") or "") == task_type]
    if bucket and bucket not in ("all",):
        if bucket == "open":
            rows = [r for r in rows if r["flags"]["is_open"]]
        else:
            rows = [r for r in rows if r["flags"][bucket]]
    if creator is not None:
        rows = [r for r in rows if r.get("created_by") == creator]
    # Owner/manager match by display name — a sales manager filters by who a task
    # belongs to (Owner) or who they manage (Manager), same as the validated report.
    if owner:
        rows = [r for r in rows if r.get("owner_name") == owner]
    if manager:
        rows = [r for r in rows if r.get("manager") == manager]
    if alex_direction:
        rows = [r for r in rows if "alex direction" in (r.get("subject") or "").lower()]
    if q:
        needle = q.lower()
        rows = [
            r
            for r in rows
            if needle in (r.get("subject") or "").lower()
            or needle in (r.get("description") or "").lower()
            or needle in (r.get("opportunity_name") or "").lower()
            or needle in (r.get("account_name") or "").lower()
        ]

    if sort == "due_date":
        # Undated tasks sort last rather than pretending to be due at epoch.
        rows.sort(key=lambda r: (r["flags"]["due_at"] is None, r["flags"]["due_at"] or ""))
    elif sort == "subject":
        rows.sort(key=lambda r: (r.get("subject") or "").lower())
    elif sort == "created_at":
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    else:
        rows.sort(key=lambda r: r["flags"]["days_since_touch"] or 0, reverse=True)

    return {"items": rows, "total": len(rows), "stale_days": stale_days}


@app.get("/api/creators")
async def creators(request: Request, stale_days: int = Query(DEFAULT_STALE_DAYS, ge=1, le=365)):
    """Per-person rollup — who is sitting on the most unattended work."""
    scope = await resolve_viewer_scope(request.headers.get("x-forwarded-email"))
    rows = filter_by_scope(await decorate(await store.all_tasks(), stale_days), scope)
    by_creator: dict[int, dict] = {}
    for row in rows:
        if (row.get("task_type") or "") != "Task":
            continue
        key = row.get("created_by")
        entry = by_creator.setdefault(
            key,
            {
                "created_by": key,
                "name": row["created_by_name"],
                "total": 0,
                "open": 0,
                "never_updated": 0,
                "stale": 0,
                "overdue": 0,
                "no_due_date": 0,
                "bad_due_date": 0,
            },
        )
        entry["total"] += 1
        if row["flags"]["is_open"]:
            entry["open"] += 1
        for flag in ("never_updated", "stale", "overdue", "no_due_date", "bad_due_date"):
            if row["flags"][flag]:
                entry[flag] += 1

    items = sorted(
        by_creator.values(),
        key=lambda e: (e["overdue"] + e["never_updated"], e["open"]),
        reverse=True,
    )
    return {"items": items, "total": len(items)}


@app.post("/api/manager-roster")
async def upload_manager_roster(request: Request, file: UploadFile = File(...)):
    """Manual roster refresh. There's no live source for this sheet — the org's Google
    Workspace policy blocks external/link sharing entirely — so the roster owner exports
    it to CSV and uploads it here by hand, roughly monthly. Column names are validated
    explicitly so a sheet restructure (e.g. "ASM" renamed) fails loudly here rather than
    silently blanking the Manager column app-wide. Restricted to ROSTER_ADMIN_EMAILS (see
    is_roster_admin) since the roster also drives who can see whose tasks.
    """
    if not is_roster_admin(request.headers.get("x-forwarded-email")):
        raise HTTPException(status_code=403, detail="You are not authorized to modify the manager roster.")

    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400, detail=f"File is not valid UTF-8 text: {exc}"
        ) from exc

    try:
        rows = manager_roster_module.parse_manager_roster_csv(text)
    except manager_roster_module.ManagerRosterFormatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not rows:
        raise HTTPException(
            status_code=400, detail="CSV was read but has no rows with Name filled in."
        )

    await store.replace_manager_roster(rows)
    uploaded_at = buckets.now_jakarta().isoformat()
    await store.set_meta("manager_roster_uploaded_at", uploaded_at)
    # Anyone with a Name but no Email can't be matched to an SSO login, so the per-viewer
    # visibility rule can't restrict them — surfaced here (not just on GET status) so a
    # fresh upload immediately shows what still needs fixing in the sheet.
    return {
        "names": len(rows),
        "uploaded_at": uploaded_at,
        "names_without_email": await store.manager_roster_names_without_email(),
    }


@app.get("/api/manager-roster")
async def manager_roster_status():
    """Roster size + upload recency — lets a monthly re-upload confirm nothing broke,
    and flags when the cached roster is old enough that it's probably gone stale."""
    uploaded_at = await store.get_meta("manager_roster_uploaded_at")
    uploaded_dt = buckets.parse_crm_datetime(uploaded_at) if uploaded_at else None
    age_days = (buckets.now_jakarta() - uploaded_dt).days if uploaded_dt else None
    return {
        "names": len(await store.manager_roster()),
        "uploaded_at": uploaded_at,
        "age_days": age_days,
        "names_without_email": await store.manager_roster_names_without_email(),
        "stale": age_days is not None and age_days > 45,
    }


@app.post("/api/sync")
async def sync():
    try:
        return await run_sync()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        await store.set_meta("last_sync_error", str(exc))
        raise HTTPException(status_code=502, detail=f"CRM sync failed: {exc}") from exc


@app.get("/api/verify")
async def verify():
    """Gate: prove the REST API sees the same rows the MCP client saw during design.

    If these don't line up, the API key's CRM user has different visibility and every
    bucket number below it is suspect — fix this before trusting the dashboard.
    """
    if not crm.configured:
        raise HTTPException(status_code=503, detail="CRM not configured")
    # "task_type_note" dropped 2026-08-14: the validated report only ever surfaces
    # task_type="Task" (see crm_client module docstring), so Note counts aren't tracked.
    expected = {"indonesia_total": 233707, "task_type_task": 550}
    try:
        actual = await crm.expected_counts()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"CRM probe failed: {exc}") from exc

    checks = [
        {
            "name": name,
            "expected_at_design_time": expected[name],
            "actual": actual.get(name),
            "matches": actual.get(name) == expected[name],
        }
        for name in expected
    ]
    return {
        "checks": checks,
        "all_match": all(c["matches"] for c in checks),
        "cached_rows": await store.task_count(),
        "note": (
            "Counts are live and will drift as the team creates tasks. A small delta is "
            "normal; a large one (or 0) means the API key's user sees a different scope."
        ),
    }


class DeleteMarkPayload(BaseModel):
    marked: bool


@app.post("/api/tasks/{task_id}/delete-mark")
async def set_delete_mark(task_id: int, payload: DeleteMarkPayload, request: Request):
    """Shared "flag for CRM deletion" mark — stored server-side (see store.delete_marks)
    so every viewer sees the same state, unlike the static artifact report's per-browser
    localStorage version. `marked_by` comes from the SSO-injected X-Forwarded-Email header
    when the app is deployed behind Substrait's Google SSO; absent in local dev.
    """
    if payload.marked:
        await store.set_delete_mark(task_id, request.headers.get("x-forwarded-email"))
    else:
        await store.clear_delete_mark(task_id)
    return {"task_id": task_id, "marked": payload.marked}


@app.get("/api/me")
async def me(request: Request):
    """Viewer identity + resulting visibility scope, for the UI banner and for debugging
    why someone sees fewer tasks than expected. See resolve_viewer_scope's docstring."""
    email = request.headers.get("x-forwarded-email")
    scope = await resolve_viewer_scope(email)
    return {
        "mode": scope["mode"],
        "viewer_name": scope["viewer_name"],
        "team_size": len(scope["owner_names"]) if scope["owner_names"] else None,
        "is_roster_admin": is_roster_admin(email),
    }


@app.get("/api/whoami")
async def whoami():
    if not crm.configured:
        raise HTTPException(status_code=503, detail="CRM not configured")
    try:
        return await crm.whoami()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/export.csv")
async def export_csv(
    request: Request,
    bucket: str | None = None,
    stale_days: int = Query(DEFAULT_STALE_DAYS, ge=1, le=365),
    task_type: str = "Task",
):
    import csv
    import io

    scope = await resolve_viewer_scope(request.headers.get("x-forwarded-email"))
    rows = filter_by_scope(await decorate(await store.all_tasks(), stale_days), scope)
    if task_type and task_type != "all":
        rows = [r for r in rows if (r.get("task_type") or "") == task_type]
    if bucket and bucket != "all":
        rows = [r for r in rows if r["flags"].get(bucket)] if bucket != "open" else [
            r for r in rows if r["flags"]["is_open"]
        ]

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        ["id", "subject", "created_by", "owner", "manager", "status", "due_date",
         "days_overdue", "days_since_touch", "never_updated", "stale", "overdue",
         "no_due_date", "bad_due_date", "must_win", "hypercare", "strategic",
         "account", "opportunity", "crm_link"]
    )
    for r in rows:
        f = r["flags"]
        tags = r.get("tags") or []
        writer.writerow(
            [r["id"], r.get("subject"), r["created_by_name"], r.get("owner_name") or "",
             r.get("manager") or "", r.get("status"),
             f["due_at"] or "", f["days_overdue"] if f["days_overdue"] is not None else "",
             f["days_since_touch"] if f["days_since_touch"] is not None else "",
             f["never_updated"], f["stale"], f["overdue"], f["no_due_date"], f["bad_due_date"],
             "MUST WIN" in tags, "HYPERCARE" in tags, "STRATEGIC" in tags,
             r.get("account_name") or "", r.get("opportunity_name") or "",
             r.get("crm_link") or ""]
        )
    return Response(
        content=out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=indonesia-tasks.csv"},
    )
