# REST API reference

Every endpoint exposed by this backend. For schemas in machine-readable form, import [openapi.yaml](openapi.yaml) into Postman or Swagger UI.

* **Base URL (dev):** `http://localhost:8000`
* **Auth:** `Authorization: Bearer <JWT>` (HS256, issued by the Node `claims-corebackend`). Some endpoints allow anonymous access — flagged inline.
* **Content type:** `application/json`.

---

## Table of contents

1. [Builder — catalog](#1-builder--catalog)
2. [Builder — server-driven chrome](#2-builder--server-driven-chrome)
3. [Builder — workflows](#3-builder--workflows)
4. [Builder — workbenches & shapes (inspector CRUD)](#4-builder--workbenches--shapes-inspector-crud)
5. [Ingestion — jobs](#5-ingestion--jobs)
6. [Ingestion — SOP data (graph + sections)](#6-ingestion--sop-data-graph--sections)
7. [Ingestion — narrative backfill](#7-ingestion--narrative-backfill)
8. [Ingestion — HTML viewer](#8-ingestion--html-viewer)
9. [Errors](#9-errors)

---

## 1. Builder — catalog

Read-only. Drives the SPA's palette without any frontend hardcoding.

### `GET /api/builder/catalog/categories/`

Palette grouped by category. Each category embeds its shapes.

**Sample response**

```json
[
  {
    "id": "9e2a...",
    "slug": "general",
    "label": "General",
    "description": "Standard flowchart shapes",
    "order": 0,
    "is_active": true,
    "shapes": [
      {
        "id": "ab12...",
        "slug": "rectangle",
        "label": "Process",
        "description": "Action / process step",
        "kind": "rectangle",
        "svg_path": "M0 0h100v60H0z",
        "viewbox": "0 0 100 60",
        "default_label": "Process",
        "default_width": 120,
        "default_height": 80,
        "default_style": {
          "fill": "#eff6ff",
          "stroke": "#3b82f6",
          "text": "#1e3a8a",
          "accent": "#3b82f6"
        },
        "ports": [
          { "id": "top",    "x": 0.5, "y": 0,   "side": "top",    "kind": "both" },
          { "id": "right",  "x": 1,   "y": 0.5, "side": "right",  "kind": "both" },
          { "id": "bottom", "x": 0.5, "y": 1,   "side": "bottom", "kind": "both" },
          { "id": "left",   "x": 0,   "y": 0.5, "side": "left",   "kind": "both" }
        ],
        "property_schema": [
          { "name": "label", "label": "Label", "type": "string" }
        ],
        "category_slug": "general",
        "order": 1,
        "is_active": true
      }
    ]
  }
]
```

### `GET /api/builder/catalog/shapes/`

Flat list of every palette item.

Optional query: `?category=<slug>`.

Response: an array of `ShapeDefinition` payloads identical to the `shapes` field above.

### `GET /api/builder/catalog/shapes/<slug>/`

One palette item by slug.

---

## 2. Builder — server-driven chrome

### `GET /api/builder/ui/navigation/`

Sidebar entries the current user can see (admin-only items are filtered out for `MEMBER` tokens).

**Sample response**

```json
[
  {
    "id": "...",
    "slug": "workflows",
    "label": "Workflows",
    "icon": "GitBranch",
    "href": "/workflows",
    "section": "Automation",
    "min_role": "MEMBER",
    "order": 10
  }
]
```

### `GET /api/builder/ui/dashboard/`

Dashboard tiles.

**Sample response**

```json
[
  {
    "id": "...",
    "slug": "total-workflows",
    "label": "Total workflows",
    "icon": "Workflow",
    "kind": "stat",
    "value": "—",
    "query": "/api/builder/workflows/?count_only=1",
    "color_class": "text-blue-600",
    "order": 10
  }
]
```

---

## 3. Builder — workflows

### `GET /api/builder/workflows/`

List workflows.

Optional query: `?is_active=true|false`.

**Sample response (truncated)**

```json
[
  {
    "id": "1c5b…",
    "name": "OBH Facets Timely Filing",
    "slug": "obh-facets-timely-filing",
    "description": "",
    "is_active": true,
    "metadata": { "runtime_agents": [], "sop_ingestion_jobs": [] },
    "owner_id": "u_42",
    "owner_email": "auditor@example.com",
    "created_at": "2026-05-01T12:34:56Z",
    "updated_at": "2026-05-21T09:11:02Z",
    "sops": [
      {
        "job_id": "8e1f…",
        "seed_url": "https://...",
        "status": "COMPLETED",
        "docs_processed": 3,
        "docs_failed": 0,
        "created_at": "...",
        "completed_at": "...",
        "audit_sop_id": 22
      }
    ],
    "attached_agents": []
  }
]
```

### `POST /api/builder/workflows/`

Create a workflow.  Optionally attach SOPs and runtime agents in the same call.

**Request**

```json
{
  "name": "OBH Facets Timely Filing",
  "description": "Audit OBH claims for timely filing",
  "is_active": true,
  "metadata": {},
  "sop_urls": [
    "http://localhost:9191/obh_facets_timely_filing.html"
  ],
  "runtime_agents": [
    {
      "name": "Eligibility check",
      "url": "https://api.example.com/eligibility",
      "method": "POST",
      "auth_type": "bearer",
      "auth_token": "sk-xyz-secret",
      "description": "Verify member coverage"
    }
  ]
}
```

**Response** — `201 Created`. Same shape as list-item above. Side effects:

* Each `sop_urls` entry creates an `IngestionJob` and dispatches `run_ingestion_pipeline.delay(job_id)`. The dispatched jobs are written to `Workflow.metadata.sop_ingestion_jobs`.
* Each `runtime_agents` entry is registered with `ApiAgentPipeline.register()`. The returned `endpoint_id` is stored on `Workflow.metadata.runtime_agents`. `auth_token` is **stripped** from the stored copy.

### `GET /api/builder/workflows/<id>/`

One workflow with `sops[]` and `attached_agents[]` materialised.

### `PATCH /api/builder/workflows/<id>/`

Partial update — `name`, `description`, `is_active`, `metadata`. Returns the updated workflow.

### `DELETE /api/builder/workflows/<id>/`

Cascading delete (work areas, workbenches, shapes, connections).

### `GET /api/builder/workflows/<id>/graph/`

Full nested canvas state.

**Response**

```json
{
  "id": "1c5b…",
  "name": "OBH Facets Timely Filing",
  "slug": "obh-facets-timely-filing",
  "description": "",
  "is_active": true,
  "metadata": {},
  "created_at": "...",
  "updated_at": "...",
  "work_areas": [
    {
      "id": "wa1…",
      "name": "Phase 1",
      "description": "",
      "order": 0,
      "color": "",
      "position_x": 0,
      "position_y": 0,
      "width": 1200,
      "height": 800,
      "metadata": {},
      "workbenches": [
        {
          "id": "wb1…",
          "name": "Eligibility",
          "node_key": "eligibility",
          "kind": "Eligibility",
          "config": {},
          "order": 0,
          "position_x": 100,
          "position_y": 100,
          "width": 800,
          "height": 600,
          "style": {},
          "shapes": [
            {
              "id": "sh1…",
              "definition_slug": "rectangle",
              "label": "Verify member",
              "description": "",
              "position_x": 200,
              "position_y": 200,
              "width": 120,
              "height": 80,
              "style": {},
              "properties": {
                "sop_rules": ["pre:22:3:0"],
                "tool_calls": ["agent:ep_a1b2c3"]
              },
              "order": 0
            }
          ]
        }
      ]
    }
  ],
  "connections": [
    {
      "id": "c1…",
      "source_shape": "sh1…",
      "target_shape": "sh2…",
      "source_port": "right",
      "target_port": "left",
      "label": "",
      "condition_label": "Yes",
      "waypoints": [],
      "style": {}
    }
  ],
  "sops": [ /* same as list view */ ],
  "attached_agents": []
}
```

### `PUT /api/builder/workflows/<id>/graph/`

Atomic bulk save of the entire canvas. Send the same shape returned by `GET …/graph/`.

The writer ([builder/services.py](../builder/services.py)) reconciles:

1. Workflow-level fields (`name`, `description`, `is_active`, `metadata`).
2. Work areas — match by `id`, fallback to `name`. Missing rows are deleted.
3. Workbenches — match by `id`, fallback to `node_key`.
4. Shapes — must include `definition_slug` for new rows. Match by `id`.
5. Connections — endpoints can be UUIDs *or* `client_id` references from the same payload (so freshly-created shapes get linked correctly).

`client_id` is a write-only hint:

```json
{
  "shapes": [{ "client_id": "tmp-1", "definition_slug": "diamond", "label": "Eligible?" }],
  "connections": [
    { "source_client_id": "tmp-1", "target_shape": "sh1…", "condition_label": "Yes" }
  ]
}
```

**Response** — full graph (same as GET).

**Errors** — `400` on:

* `connections: source and target must differ.`
* `connections: <id> does not belong to this workflow.`
* `connections: No shape with client_id='<id>' in this payload.`
* `shapes: New shapes must include 'definition_slug'.`
* `shapes: Unknown shape definition '<slug>'.`

### `POST /api/builder/workflows/<id>/attach/`

Attach **more** SOPs / agents to an existing workflow.

**Request**

```json
{
  "sop_urls": ["https://…"],
  "runtime_agents": [{ "name":"…","url":"…","method":"POST","auth_type":"bearer","auth_token":"…" }]
}
```

**Response** — `202 Accepted`:

```json
{
  "workflow": { /* updated workflow */ },
  "dispatched": {
    "sops":   [{ "job_id": "…", "seed_url": "…", "status": "QUEUED" }],
    "agents": [{ "name": "…", "url": "…", "endpoint_id": "ep_…" }]
  }
}
```

### `GET /api/builder/workflows/<id>/attachable/`

Enumerates every rule and tool a node on this workflow's canvas can attach to. Used by the SPA's "Pick rules & tools" dialog.

**Response**

```json
{
  "sops": [
    { "sop_id": 22, "title": "OBH Facets Timely Filing", "narrative": "This SOP governs…" }
  ],
  "sop_rules": [
    {
      "key": "pre:22:3:0",
      "sop_id": 22,
      "sop_title": "OBH Facets Timely Filing",
      "source": "precondition",
      "section_id": 3,
      "section_label": "Lines of Business",
      "section_category": "LOB",
      "section_narrative": "Auditor confirms the member's LOB before…",
      "condition": "Member is Commercial LOB",
      "action": "Apply 90-day timely filing",
      "decision_type": "CONDITIONAL",
      "is_exception": false,
      "codes": [],
      "is_blocking": false,
      "references": [],
      "goto_step": null
    },
    {
      "key": "step:22:4:1",
      "sop_id": 22,
      "sop_title": "OBH Facets Timely Filing",
      "source": "decision",
      "section_id": 4,
      "section_label": "Step 4: Was the claim submitted within 90 days?",
      "section_category": "DECISION",
      "section_narrative": "At this step the auditor…",
      "condition": "Days from DOS > 90 AND Group ≠ exception list",
      "action": "Deny with EOB E51",
      "decision_type": "DENY",
      "is_exception": false,
      "codes": ["E51", "003", "346"],
      "is_blocking": true,
      "references": [
        "step:22:5:0",
        "step:22:5:1"
      ],
      "goto_step": 5
    }
  ],
  "tool_calls": [
    {
      "key": "agent:ep_a1b2c3",
      "endpoint_id": "ep_a1b2c3",
      "name": "Eligibility check",
      "method": "POST",
      "url": "https://api.example.com/eligibility",
      "description": "Verify member coverage",
      "auth_type": "bearer"
    }
  ]
}
```

The `references[]` array enables **cascade selection**: ticking a rule auto-ticks the rules it transitively goto-jumps to.

### `POST /api/builder/workflows/<id>/duplicate/`

Clone a workflow, all of its work areas / workbenches / shapes / connections.

**Request** — optional `{ "name": "Custom name" }`.

**Response** — `201 Created`, the new workflow.

### `POST /api/builder/workflows/<id>/activate/`

Sets `is_active = true`.  Response: the workflow.

### `POST /api/builder/workflows/<id>/deactivate/`

Sets `is_active = false`.  Response: the workflow.

---

## 4. Builder — workbenches & shapes (inspector CRUD)

Flat CRUD used by the inspector form when individual rows are edited outside of the full graph save.

| Method            | Path                                  | Notes                                  |
|-------------------|---------------------------------------|----------------------------------------|
| `GET`             | `/api/builder/workbenches/`           | List workbenches.                      |
| `POST`            | `/api/builder/workbenches/`           | Create one.                            |
| `GET`/`PATCH`/`DELETE` | `/api/builder/workbenches/<id>/` | Detail.                                |
| `GET`             | `/api/builder/shapes/`                | List shapes.                           |
| `POST`            | `/api/builder/shapes/`                | Create one (requires `definition_slug`). |
| `GET`/`PATCH`/`DELETE` | `/api/builder/shapes/<id>/`     | Detail.                                |

Schemas match the nested versions in `GET /workflows/<id>/graph/`.

---

## 5. Ingestion — jobs

### `GET /api/ingest/health/`  *(no auth)*

```json
{
  "status": "ok",
  "checks": {
    "uhc_sop_ingestion": "ok",
    "celery": "ok"
  },
  "env_file_found": true,
  "jobs_total": 12,
  "jobs_running": 1
}
```

`celery` is `"no_workers"` if no worker is connected. `uhc_sop_ingestion` is `"MISSING — …"` if the editable package isn't installed.

### `GET /api/ingest/`

List jobs. Query params: `?status=COMPLETED|RUNNING|…`, `?page=1`, `?page_size=20` (default 20).

**Response**

```json
{
  "count": 47,
  "page": 1,
  "results": [
    {
      "job_id": "8e1f…",
      "seed_url": "https://...",
      "status": "COMPLETED",
      "docs_queued": 3,
      "docs_processed": 3,
      "docs_failed": 0,
      "max_depth": 4,
      "max_docs": 200,
      "llm_provider": "anthropic",
      "llm_model": "claude-sonnet-4-5-20250929",
      "celery_task_id": "…",
      "created_at": "...",
      "started_at": "...",
      "completed_at": "...",
      "duration_seconds": 73.2,
      "summary": {
        "total_docs_processed": 3,
        "total_rules": 47,
        "total_codes": 23
      },
      "errors": [],
      "documents": [
        {
          "url": "https://…",
          "doc_format": "HTML",
          "depth": 0,
          "status": "OK",
          "neo4j_sop_id": "obh_facets_timely_filing",
          "steps_count": 8,
          "rules_count": 47,
          "codes_count": 23,
          "links_found": 4,
          "created_at": "..."
        }
      ]
    }
  ]
}
```

### `POST /api/ingest/`

Start an async ingestion job.

**Request**

```json
{
  "seed_url": "https://example.com/sop.html",
  "max_depth": 4,
  "max_docs": 200,
  "llm_provider": "anthropic",
  "llm_model": "claude-sonnet-4-5-20250929"
}
```

All fields except `seed_url` are optional. Validation: `1 ≤ max_depth ≤ 8`, `1 ≤ max_docs ≤ 500`, `llm_provider ∈ {openai, anthropic}`.

**Response** — `202 Accepted`. Same shape as a single `results[]` item above with `status: "QUEUED"`. Use `GET /api/ingest/<job_id>/` to poll.

### `GET /api/ingest/<job_id>/`

One job, fully serialised (same shape as a list result). `404` if missing.

### `DELETE /api/ingest/<job_id>/`

Delete a job. `409` if status is `RUNNING`. Returns `204` on success.

### `POST /api/ingest/run-sync/`  *(DEBUG only, no auth)*

Runs the pipeline **in-process**, blocks for the full duration (60–180s typical). Useful for local debugging without a Celery worker. Same request body as `POST /api/ingest/`. Returns the completed job (or `500` with the error).

Disabled (`403 Forbidden`) when `DJANGO_DEBUG=false`.

---

## 6. Ingestion — SOP data (graph + sections)

### `GET /api/ingest/<job_id>/graph/`  *(no auth)*

Knowledge graph for the primary SOP in the job. Drives the SPA's React-Flow viewer.

**Response**

```json
{
  "sop_id": 22,
  "seed_url": "https://…",
  "status": "COMPLETED",
  "nodes": [
    {
      "id": "doc",
      "label": "OBH Facets Timely Filing",
      "type": "DOCUMENT",
      "details": { "platform": "Facets", "lob": ["Commercial"] },
      "ref_table": "auditsop",
      "ref_id": 22,
      "display_order": 0
    },
    {
      "id": "step_4",
      "label": "Was the claim submitted within 90 days?",
      "type": "STEP",
      "details": { "step_number": 4 },
      "ref_table": "auditstep",
      "ref_id": 88,
      "display_order": 10
    }
  ],
  "edges": [
    {
      "id": "e1",
      "source": "doc",
      "target": "step_1",
      "rel": "HAS_STEP",
      "label": "",
      "details": {}
    },
    {
      "id": "e2",
      "source": "step_4",
      "target": "dec_42",
      "rel": "HAS_DECISION",
      "label": "Days from DOS > 90",
      "details": { "row_index": 1 }
    }
  ]
}
```

Empty `nodes` / `edges` when the job is still queued or has no SOPs yet.

### `GET /api/ingest/<job_id>/sections/`  *(no auth)*

Structured tabular view of the SOP. Returns everything needed for the sections panel and the rule picker.

**Response (abbreviated)**

```json
{
  "sop_id": 22,
  "seed_url": "https://…",
  "status": "COMPLETED",
  "title": "OBH Facets Timely Filing",
  "purpose": "Audit OBH claims for timely filing",
  "summary": "This SOP governs…",
  "narrative": "When auditing OBH claims for timely filing, an examiner…",
  "platform": "Facets",
  "lob": ["Commercial"],
  "preconditions": [
    {
      "id": 33,
      "order": 0,
      "category": "LOB",
      "label": "Lines of Business",
      "content_text": "This SOP applies to Commercial members.",
      "is_blocking": true,
      "rules": [
        { "condition": "Member is Commercial LOB", "action": "Apply 90-day filing", "decision_type": "CONDITIONAL", "is_exception": false }
      ]
    }
  ],
  "steps": [
    {
      "step_number": 4,
      "question": "Was the claim submitted within 90 days?",
      "intro_text": "Calculate days from DOS to received date.",
      "narrative": "At this step the auditor…",
      "is_terminal": false,
      "terminal_action": "",
      "is_sub_procedure": false,
      "sub_procedure": "",
      "decisions": [
        {
          "row_index": 1,
          "condition_if": "Days from DOS > 90",
          "condition_and": "Group ≠ exception list",
          "action_text": "Deny with EOB E51",
          "action_summary": "Deny — timely filing",
          "decision_type": "DENY",
          "goto_step": null,
          "is_final": true,
          "eob_codes": ["E51"],
          "ex_codes": ["003"],
          "denial_codes": ["346"],
          "system_actions": [],
          "all_codes": ["E51", "003", "346"]
        }
      ]
    }
  ],
  "codes": [
    { "value": "E51", "type": "EOB", "description": "Timely filing denial", "context": "If days > 90, deny with E51", "source_step": 4 }
  ],
  "group_limits": [
    {
      "group_name": "OBH",
      "inn_days": 90,
      "oon_days": 180,
      "limit_days": null,
      "limit_months": null,
      "limit_years": null,
      "basis": "DOS",
      "network_type": "BOTH",
      "exceptions": [],
      "special_notes": []
    }
  ],
  "annotations": [
    { "type": "ALERT", "content_text": "For DOS on or after 01/01/2024…", "is_claim_impact": true, "step_number": 4 }
  ],
  "references": [
    { "ref_text": "POTF-2024 calculator", "ref_url": "https://…", "ref_type": "RESOLVED", "is_resolved": true, "step_number": null }
  ]
}
```

---

## 7. Ingestion — narrative backfill

### `POST /api/ingest/<job_id>/contextualize/`  *(no auth)*

Re-runs only the narrative agents on already-ingested SOPs. Useful for jobs ingested before the narrative stage existed or whose `narrative_context` is blank from a previous LLM failure. Does **not** re-fetch source HTML — it rebuilds state from Postgres.

Modes:

* **Async (default)** — `POST /api/ingest/<job_id>/contextualize/`

  ```json
  {
    "job_id": "8e1f…",
    "celery_task_id": "abcd…"
  }
  ```

  → `202 Accepted`.

* **Sync** — `POST /api/ingest/<job_id>/contextualize/?sync=true` (or body `{"sync": true}`).  Blocks ~60s.

  ```json
  {
    "job_id": "8e1f…",
    "results": [
      { "sop_id": 22, "sop_overview_updated": true, "steps_updated": 8 }
    ]
  }
  ```

---

## 8. Ingestion — HTML viewer

Server-rendered debug UI. Not part of the SPA — open in a browser tab.

| Method | Path                                                 | What it shows                                  |
|--------|------------------------------------------------------|------------------------------------------------|
| `GET`  | `/api/ingest/viewer/`                                | Job list with filter chips.                    |
| `GET`  | `/api/ingest/viewer/<job_id>/`                       | Job detail — stage logs, LLM logs, SOP list.   |
| `GET`  | `/api/ingest/viewer/<job_id>/doc/<doc_id>/`          | One SOP — pre-conditions, decision tree, codes, group limits, annotations, references, Cytoscape graph viewer. |

Always 200; routes return 404 if the IDs don't exist.

---

## 9. Errors

DRF default error shape applies.

* `400 Bad Request` — validation errors:

  ```json
  { "seed_url": ["Enter a valid URL."] }
  ```

* `401 Unauthorized` — `{"detail": "Authentication credentials were not provided."}` or `{"detail": "Token expired"}`.
* `403 Forbidden` — typically the sync-run endpoint when `DEBUG=false`.
* `404 Not Found` — `{"detail": "Not found."}`.
* `409 Conflict` — `{"detail": "Cannot delete a running job."}`.
* `500 Internal Server Error` — `{"error": "<message>"}`, with `error` being the exception string.
