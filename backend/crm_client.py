"""Read-only adapter over the in-house SalesCRM REST API.

Follows the same contract proven by the EKYC project's crm_client.py:
  - Base path: <CRM_API_BASE_URL>/api/v1
  - Auth header: `X-API-Key: crm_...`  (NOT `Authorization: Bearer ...`)
  - GET /objects/{object_type}/records      -> {items, total, page, page_size, has_next}
    Equality filters are plain query params. There is NO range/comparison operator, which
    is why all date logic (overdue, stale) is computed app-side in buckets.py.
  - GET /objects/{object_type}/records/{id} -> {id, object_type, data: {...fields...}}
    Note the record sits under `data` on single-record GETs, unlike the list endpoint.

This module has no write methods at all — the monitor only ever reads.

Scoping decisions baked in here (verified against the CRM on 2026-08-14):
  - Indonesia == `record_type_id: 100` on Task/Opportunity, `record_type_id: 10` on
    Account. The label lives in `record_type_name` ("Indonesia") but that field CANNOT be
    used as a filter — filtering on it returns 0 rows. Always filter by the numeric id.
  - Native tasks are the ones with `task_type = "Task"` (550 rows). The validated report
    never surfaces `task_type = "Note"` (15 rows, a separate free-text log), so this
    client only fetches "Task" — the other ~233k Indonesia Task rows are a Salesforce
    import carrying `sf_id`, and are excluded regardless.
  - Tag derivation (MUST WIN / HYPERCARE / STRATEGIC — see main.py's enrich_account_tags):
    MUST WIN comes straight off each Opportunity's `lead_source_detail` field. HYPERCARE
    and STRATEGIC live on Account.customer_success_manager, but only on the PARENT
    account — a child account's own CSM field holds a raw Salesforce user id instead, so
    an opportunity is tagged when its own account is tagged, or when its account's parent
    is. Confirmed pattern this session: exactly 5 root Indonesia accounts (SwipeRx, ENB,
    Laku6, Paskomnas, Hermed) carry `customer_success_manager` containing
    "Hypercare"/"Strategic" text; everything else inherits via `parent_account_id`.
"""
from __future__ import annotations

import os

import httpx

CRM_API_BASE_URL = os.getenv("CRM_API_BASE_URL", "").rstrip("/")
CRM_API_KEY = os.getenv("CRM_API_KEY", "")
CRM_OPPORTUNITY_URL_TEMPLATE = os.getenv("CRM_OPPORTUNITY_URL_TEMPLATE", "")

# Indonesia record type — Task/Opportunity and Account use different numeric ids.
INDONESIA_RECORD_TYPE_ID = 100
INDONESIA_ACCOUNT_RECORD_TYPE_ID = 10
# The only task_type this app surfaces (see module docstring — "Note" is out of scope).
NATIVE_TASK_TYPE = "Task"


def opportunity_deep_link(opportunity_id) -> str | None:
    if not CRM_OPPORTUNITY_URL_TEMPLATE or opportunity_id in (None, ""):
        return None
    return CRM_OPPORTUNITY_URL_TEMPLATE.format(id=opportunity_id)


class CrmClient:
    def __init__(self) -> None:
        self._configured = bool(CRM_API_BASE_URL and CRM_API_KEY)

    @property
    def configured(self) -> bool:
        return self._configured

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"{CRM_API_BASE_URL}/api/v1",
            headers={"X-API-Key": CRM_API_KEY},
            timeout=30.0,
        )

    async def whoami(self) -> dict | None:
        """Which CRM user the API key acts as. Used to explain permission mismatches."""
        if not self._configured:
            return None
        async with self._client() as client:
            resp = await client.get("/auth/me")
            resp.raise_for_status()
            return resp.json()

    async def count_records(self, object_type: str, filters: dict) -> int:
        """Cheap `total` probe — page_size=1, we only read the count."""
        async with self._client() as client:
            params = {"page": 1, "page_size": 1, **filters}
            resp = await client.get(f"/objects/{object_type}/records", params=params)
            resp.raise_for_status()
            return int(resp.json().get("total", 0))

    async def list_all_records(
        self,
        object_type: str,
        filters: dict | None = None,
        search: str | None = None,
        page_size: int = 100,
        max_pages: int = 200,
    ) -> list[dict]:
        """Page through every matching record, following `has_next`.

        `max_pages` is a guard rail: the native task set is ~6 pages, so if we ever find
        ourselves 200 pages deep something is wrong with the filter and we should stop
        rather than hammer the CRM. `search` is a free-text param — see `search_accounts`
        for the one place this app actually relies on it, and the verification note there
        about confirming the live REST API honors it the same way the MCP tool did.
        """
        items: list[dict] = []
        page = 1
        async with self._client() as client:
            while page <= max_pages:
                params = {"page": page, "page_size": min(page_size, 100), **(filters or {})}
                if search:
                    params["search"] = search
                resp = await client.get(f"/objects/{object_type}/records", params=params)
                resp.raise_for_status()
                payload = resp.json()
                batch = payload.get("items") or []
                items.extend(batch)
                if not payload.get("has_next") or not batch:
                    break
                page += 1
        return items

    async def fetch_indonesia_tasks(self) -> list[dict]:
        """Every CRM-native Indonesia task (task_type "Task" — see module docstring).

        Rows carrying `sf_id` are dropped defensively: they should be impossible here
        (legacy Salesforce-import rows have no task_type) but the filter is cheap.
        """
        rows = await self.list_all_records(
            "Task",
            {"record_type_id": INDONESIA_RECORD_TYPE_ID, "task_type": NATIVE_TASK_TYPE},
        )
        return [row for row in rows if not row.get("sf_id")]

    async def get_opportunity(self, opportunity_id) -> dict | None:
        """Single Opportunity. Remember: the record is nested under `data` here."""
        async with self._client() as client:
            resp = await client.get(f"/objects/Opportunity/records/{opportunity_id}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            payload = resp.json()
            return payload.get("data") or payload.get("record") or payload

    async def get_account(self, account_id) -> dict | None:
        """Single Account. Same `data`-nesting convention as `get_opportunity`."""
        async with self._client() as client:
            resp = await client.get(f"/objects/Account/records/{account_id}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            payload = resp.json()
            return payload.get("data") or payload.get("record") or payload

    async def search_accounts(self, query: str) -> list[dict]:
        """Indonesia accounts whose text (incl. `customer_success_manager`) matches `query`.

        This is how the Strategic/Hypercare-tagged root accounts were originally found by
        hand this session (via the MCP client's equivalent `search` param). Verified live
        on 2026-08-20: `GET /objects/Account/records?record_type_id=10&search=Hypercare`
        returns exactly the 5 root Indonesia accounts (SwipeRx, ENB, Laku6, Paskomnas,
        Hermed), matching the MCP-tool result — the REST endpoint honors `search` the same
        way, so this is safe to rely on as-is.
        """
        return await self.list_all_records(
            "Account",
            {"record_type_id": INDONESIA_ACCOUNT_RECORD_TYPE_ID},
            search=query,
        )

    async def get_children_accounts(self, parent_account_id) -> list[dict]:
        """Direct children of a parent Account (`parent_account_id` equality filter)."""
        return await self.list_all_records(
            "Account", {"parent_account_id": parent_account_id}
        )

    async def expected_counts(self) -> dict:
        """Totals used by the /api/verify gate to prove the REST view matches the MCP view."""
        return {
            "indonesia_total": await self.count_records(
                "Task", {"record_type_id": INDONESIA_RECORD_TYPE_ID}
            ),
            "task_type_task": await self.count_records(
                "Task",
                {"record_type_id": INDONESIA_RECORD_TYPE_ID, "task_type": NATIVE_TASK_TYPE},
            ),
        }
