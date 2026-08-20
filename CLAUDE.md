# Task Progress Monitor

Internal dashboard for Ninja Van Indonesia's Sales org: tracks SalesCRM Tasks that are
never-updated, stale, overdue, or missing a due date, with Alex Direction / Hypercare-
Strategic / Must Win views grouped by Opportunity. Read-only against CRM end to end.

**Roles:** identity comes from Google SSO (`X-Forwarded-Email`, see `main.py`'s
`resolve_viewer_scope`) mapped through the manually-uploaded manager roster CSV — Sales
sees only their own tasks, a manager (found via the roster's `ASM`/`Sales Head` columns)
sees their team's tasks plus their own, anyone unmatched (Legal/Admin, or SSO off) sees
everything. Roster upload (`POST /api/manager-roster`) is restricted to
`ROSTER_ADMIN_EMAILS`.

**CRM integration:** SalesCRM's REST API (`X-API-Key` auth, `/api/v1/objects/...`), same
contract as documented in `backend/crm_client.py`'s docstring — Indonesia scoping,
task/account record-type ids, the three `due_date` shapes, etc. Read-only; no write-back.

<!-- BEGIN substrait-app contract (v3) — managed by the substrait plugin (link/deploy); edits inside this block are overwritten on update. Delete the whole block to opt out. -->
## Substrait deployment

**Linked app:** not linked yet — run /substrait:link

This project deploys to the **Substrait platform** (linked via the gitignored
`.substrait/config.json`). Deploy with **`/substrait:deploy`** (packages source-only,
uploads, `--watch` follows the build to the live preview); re-link with
`/substrait:link`. The `substrait-app` skill has the full contract; the essentials:

**Hard requirements (platform-enforced):**
- Backend in any language. Its Dockerfile — `cicd/Dockerfile.backend` (repo-root build
  context), `cicd/Dockerfile`, or `backend/Dockerfile` (backend/ context) — must
  `EXPOSE 8000`, serve `GET /health` (200) and the API under **`/api`**.
- Frontend optional, any framework: built site served on **port 80** via
  `cicd/Dockerfile.frontend` (or `frontend/Dockerfile`). One ingress host routes
  `/api` → backend, everything else → frontend (no `frontend/` → everything → backend,
  so serve `/` yourself). The frontend calls the API via **relative `/api` paths** —
  never an absolute URL, never `VITE_API_URL`.
- Database is **always OceanBase (MySQL wire)** — MySQL driver only, never Postgres.
  The platform injects `DATABASE_URL` and `JWT_SECRET`. **All DDL lives in
  Flyway files** `backend/resources/db/migration/V*.sql` (MySQL dialect) — the app
  never `CREATE TABLE`s.
- Backing services (redis / kafka / qdrant): declare them in a **`substrait.yaml`** at
  the repo root (`services: {redis: {}, kafka: {persistent: true}, qdrant: {}}`) — the
  platform provisions them and injects `REDIS_URL` / `KAFKA_BROKERS` / `QDRANT_URL`
  only for what's declared. Ephemeral unless `persistent: true`.
- Custom env vars/secrets: declare in `backend/.env.example` (`NAME=value`, trailing
  `# secret` marks a secret) — the portal pre-creates them for the owner to fill in.
  Build-time frontend vars go in a committed `frontend/.env.production` (public,
  non-secret values only).
- Never create `k8s/` (platform-owned, discarded). Uploads are source-only, ≤ 16 MB
  (no `node_modules/`, `.venv/`, `dist/`, build output).

**Platform capabilities to build on:**
- **User identity (Google SSO):** when the app owner enables SSO (portal Access tab),
  every gated backend request carries unspoofable `X-Forwarded-Email` /
  `X-Forwarded-User` headers — identity with no OAuth flow in the app. Absent in local
  dev, on declared public paths, and when SSO is off (then they're client-spoofable —
  never treat as access control). The browser can't see them: expose e.g. `/api/me`.
  SSO-exempt paths (MCP servers, webhooks) are declared on the Access tab and must be
  authenticated by the app itself (e.g. Bearer token from an env secret).
- **API endpoint inventory:** the portal's API tab lists the backend's endpoints,
  auto-harvested from the app's OpenAPI spec after each deploy (FastAPI serves
  `/openapi.json` by default). Spec-less stacks: `/substrait:deploy` generates
  `.substrait/endpoints.json` instead.

**Local dev:** use a MySQL-wire DB so drivers/migrations run unchanged — never SQLite.
Scaffolded projects: `docker compose up -d db && docker compose run --rm migrate`, then
the backend on `:8000` (reading `DATABASE_URL`) and `npm run dev` in `frontend/`.
<!-- END substrait-app contract -->
