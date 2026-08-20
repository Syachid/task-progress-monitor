-- Initial schema for the Task Progress Monitor, ported from the local SQLite
-- verification build (backend/store.py's old SCHEMA) to MySQL/OceanBase dialect.
-- The app never CREATE TABLEs itself once deployed — every schema change belongs in a
-- new V*.sql file here.

CREATE TABLE tasks (
    id                  BIGINT PRIMARY KEY,
    subject             TEXT,
    description         TEXT,
    status              VARCHAR(64),
    priority            VARCHAR(32),
    task_type           VARCHAR(32),
    due_date            VARCHAR(64),
    created_at          VARCHAR(64),
    updated_at          VARCHAR(64),
    version             INT,
    created_by          BIGINT,
    owner_id            BIGINT,
    owner_name          VARCHAR(255),
    assigned_to_id      BIGINT,
    related_object_type VARCHAR(64),
    related_record_id   BIGINT,
    raw                 LONGTEXT,
    KEY idx_tasks_created_by (created_by),
    KEY idx_tasks_status (status)
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE opportunities (
    id                  BIGINT PRIMARY KEY,
    name                VARCHAR(500),
    account_name        VARCHAR(500),
    stage               VARCHAR(128),
    owner_name          VARCHAR(255),
    account_id          BIGINT,
    lead_source_detail  TEXT,
    fetched_at          VARCHAR(64)
) DEFAULT CHARSET=utf8mb4;

-- Strategic/Hypercare tags found on Indonesia Accounts (see main.py's
-- enrich_account_tags). `tags` is a comma-joined string, e.g. "HYPERCARE,STRATEGIC" —
-- rebuilt wholesale each sync, same replace-not-upsert reasoning as `tasks`.
CREATE TABLE account_tags (
    account_id BIGINT PRIMARY KEY,
    tags       VARCHAR(255)
) DEFAULT CHARSET=utf8mb4;

-- Cache of the last manually-uploaded roster CSV (see manager_roster.py). `email`
-- powers resolve_viewer_scope's SSO-email -> roster-Name lookup; `sales_head` is the
-- second hierarchy level checked there (main.py).
CREATE TABLE manager_roster (
    name       VARCHAR(255) PRIMARY KEY,
    manager    VARCHAR(255),
    email      VARCHAR(255),
    sales_head VARCHAR(255)
) DEFAULT CHARSET=utf8mb4;

-- Shared "flag for CRM deletion" marks (the Delete Yes/No column) — server-persisted so
-- every viewer of the dashboard sees the same marks.
CREATE TABLE delete_marks (
    task_id   BIGINT PRIMARY KEY,
    marked_by VARCHAR(255),
    marked_at VARCHAR(64)
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE meta (
    `key`  VARCHAR(255) PRIMARY KEY,
    value  TEXT
) DEFAULT CHARSET=utf8mb4;
