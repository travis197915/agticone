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
9. [Execution — batch claim adjudication](#9-execution--batch-claim-adjudication)
10. [Errors](#10-errors)

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

## 9. Execution — batch claim adjudication

The execution engine takes an Excel of claim ids + a workflow id and
produces a per-claim adjudication. Full architecture in
[EXECUTION_ENGINE.md](EXECUTION_ENGINE.md).

### `POST /api/execute/workflows/<workflow_id>/run-batch/`

Multipart upload. For each row in the Excel, the engine fetches the claim
via `linx_claim_search`, walks the workflow's Shapes in canvas order, and
runs one LLM call per attached rule. The claim halts immediately on any
matched rule whose `decision_type` is `DENY` or `STOP`. Synchronous; the
response contains the full per-claim audit trail.

**Form fields**

| Field             | Required | Default      | Notes                                                                        |
|-------------------|----------|--------------|------------------------------------------------------------------------------|
| `file`            | yes      | —            | `.xlsx` upload.                                                              |
| `claim_id_column` | no       | `claim_id`   | Case-insensitive. Also accepts `subscriber_id`, `claimid`, etc.              |
| `sheet_name`      | no       | first sheet  | Optional sheet selector.                                                     |

**Sample request**

```bash
curl -F file=@claims.xlsx -F claim_id_column=subscriber_id \
     -X POST http://localhost:8000/api/execute/workflows/<workflow_id>/run-batch/
```

**Sample response (truncated)**

```json
{
  "batch_id": "uuid",
  "status": "COMPLETED",
  "total_claims": 50,
  "completed": 48,
  "failed": 2,
  "results": [
    {
      "claim_id": "CLM-12345",
      "run_id": "uuid",
      "status": "TERMINATED_EARLY",
      "final_decision_type": "DENY",
      "applied_codes": ["E51", "346"],
      "narrative": "Halted at shape Timely filing by rule step:42:3:2 ...",
      "terminated_at_shape_id": "9b1a…",
      "evaluations": [
        {"rule_key": "pre:42:5:0", "shape_id": "8f2c…",
         "shape_label": "Eligibility", "matched": true,
         "decision_type": "ALLOW",
         "reasoning": "Member eligible on DOS per Linx."},
        {"rule_key": "step:42:3:2", "shape_id": "9b1a…",
         "shape_label": "Timely filing", "matched": true,
         "decision_type": "DENY",
         "reasoning": "DOS − received_date = 195d > 180d INN limit",
         "codes": ["E51", "346"]}
      ],
      "tool_invocations": [
        {"tool": "linx_claim_search", "phase": "FETCH", "ok": true, "ms": 142}
      ]
    },
    {
      "claim_id": "CLM-67890",
      "run_id": "uuid",
      "status": "FETCH_FAILED",
      "error_message": "linx_claim_search returned no records"
    }
  ]
}
```

**Per-claim statuses**

| Status                      | Meaning                                                                  |
|-----------------------------|--------------------------------------------------------------------------|
| `COMPLETED`                 | Every Shape evaluated; verdict reached via the aggregator.               |
| `TERMINATED_EARLY`          | A DENY/STOP rule on some Shape halted the claim mid-workflow.            |
| `FETCH_FAILED`              | `linx_claim_search` didn't return a usable claim for that id.            |
| `FAILED`                    | Engine crashed on this claim (see `error_message`).                      |

**Batch statuses**

| Status      | Meaning                                                              |
|-------------|----------------------------------------------------------------------|
| `COMPLETED` | All claims reached a verdict (`COMPLETED` or `TERMINATED_EARLY`).    |
| `PARTIAL`   | At least one claim succeeded and at least one failed.                |
| `FAILED`    | Workbook itself couldn't be parsed, or every claim failed.           |

**Failure modes**

* `400 Bad Request` — missing/non-`.xlsx` file, claim-id column not found, workflow has no `NodeRuleBinding` rows.
* `404 Not Found` — unknown workflow id.

### `POST /api/execute/workflows/<workflow_id>/run-batch-async/`

Async sibling of the sync endpoint above. Same multipart form. Stashes
the upload, reserves a `BatchExecutionRun` row in `RUNNING`, dispatches
the `execution_app.run_batch_async` Celery task, and returns **202
Accepted** immediately so the SPA can subscribe to the SSE stream
without waiting for the batch to finish.

Use this when the SPA wants live per-Shape / per-rule progress.
For Postman, CI, or shell scripts that just want the final answer in
one response, prefer the synchronous endpoint above.

**Requires a running Celery worker** (`celery -A sop_backend worker`).

**Form fields**: same as the sync endpoint (`file`, optional
`claim_id_column`, optional `sheet_name`).

**Sample request**

```bash
curl -F file=@claims.xlsx -F claim_id_column=subscriber_id \
     -X POST http://localhost:8000/api/execute/workflows/<workflow_id>/run-batch-async/
```

**Sample response**

```json
{
  "batch_id":   "uuid",
  "status":     "RUNNING",
  "stream_url": "/api/execute/batches/<batch_id>/events/"
}
```

The SPA opens `stream_url` with `new EventSource(...)` — see the SSE
endpoint below.

**Failure modes**

* `400 Bad Request` — missing/non-`.xlsx` file.
* `500 Internal Server Error` — the server could not stash the upload to
  disk (the temp dir is the place to look — `MEDIA_ROOT/execution_uploads/`).

### `GET /api/execute/batches/<batch_id>/`

Fetch a saved batch with per-claim run summaries (so the SPA can re-render
a prior run without re-uploading the Excel).

```json
{
  "id": "uuid",
  "workflow": "uuid",
  "source_filename": "claims.xlsx",
  "claim_id_column": "subscriber_id",
  "total_claims": 50,
  "completed": 48,
  "failed": 2,
  "status": "PARTIAL",
  "runs": [
    {"id": "uuid", "claim_id": "CLM-12345", "status": "COMPLETED",
     "final_decision_type": "DENY", "applied_codes": ["E51", "346"],
     "error_message": ""},
    ...
  ]
}
```

### `GET /api/execute/batches/<batch_id>/events/`

Server-Sent Events stream of live batch progress. Pairs with `POST
/run-batch-async/` above. Subscribes to the Redis pub/sub channel
`batch:<batch_id>` and pipes per-Shape / per-rule / per-claim events to
the SPA as the Celery task produces them. On connect, replays
already-finished claims from the DB so a mid-batch reconnect picks up
cleanly.

**Response headers**

```
Content-Type:      text/event-stream
Cache-Control:     no-store
X-Accel-Buffering: no
Connection:        keep-alive
```

**Event grammar** — each chunk is one SSE event followed by a blank line.
The `event:` line names the kind; the `data:` line is the JSON payload.

```
event: batch_start
data: {"batch_id":"...","workflow_id":"...","total_claims":50,
       "claim_id_column":"subscriber_id","source_filename":"...",
       "status":"RUNNING"}

event: shape_start
data: {"batch_id":"...","run_id":"...","claim_id":"CLM-1",
       "shape_id":"8f2c…","shape_label":"Eligibility",
       "rules_total":1,"ts":1716941472.10}

event: rule_evaluated
data: {"batch_id":"...","run_id":"...","claim_id":"CLM-1",
       "shape_id":"8f2c…","shape_label":"Eligibility",
       "rule_key":"pre:42:5:0","rule_source":"PRECONDITION",
       "matched":true,"decision_type":"ALLOW","confidence":0.92,
       "reasoning":"Member eligible on DOS per Linx.",
       "codes":[],
       "llm_provider":"anthropic","llm_model":"claude-sonnet-4-5",
       "llm_ms":1840,"llm_attempts":1,
       "ts":1716941473.95}

event: claim
data: {"claim_id":"CLM-1","run_id":"...","status":"COMPLETED",
       "final_decision_type":"ALLOW","applied_codes":[],
       "narrative":"...","error_message":""}

event: summary
data: {"id":"...","status":"COMPLETED","total_claims":50,
       "completed":50,"failed":0,"duration_ms":312000}

: keepalive    ← SSE comment, every 15s when idle (defeats proxy timeouts)
```

**Event kinds**

| `event:` | Payload | When |
|---|---|---|
| `batch_start` | Metadata about the batch (workflow id, claim count, source filename, status). | Once, immediately on connect. Synthesised from the `BatchExecutionRun` row. |
| `shape_start` | Canvas-node anchor (`shape_id`, `shape_label`, `rules_total`). | Once per Shape, just before its rules begin evaluating. |
| `rule_evaluated` | One rule's verdict — `matched`, `decision_type`, `confidence`, `reasoning`, `codes`, plus rolled-up LLM telemetry (`llm_provider`, `llm_model`, `llm_ms`, `llm_attempts`). | Once per rule, immediately after the LLM verdict lands. Retries / cross-provider fallback collapse into one event — `llm_attempts` indicates how many physical attempts were made. |
| `claim` | Full per-claim verdict (`status`, `final_decision_type`, `applied_codes`, `narrative`). | Once per claim, after `RuleExecutionRun` is committed to the DB. |
| `summary` | Final batch tally (`status`, `completed`, `failed`, `duration_ms`). | Once, at the end. Terminal — bridge closes after this. |
| `error` | Task-level crash (`batch_id`, `message`). | Terminal — bridge closes after this. |

**Sample SPA wiring**

```js
const es = new EventSource(stream_url);
es.addEventListener("batch_start",    e => initUI(JSON.parse(e.data)));
es.addEventListener("shape_start",    e => openShapeGroup(JSON.parse(e.data)));
es.addEventListener("rule_evaluated", e => appendRule(JSON.parse(e.data)));
es.addEventListener("claim",          e => finalizeClaim(JSON.parse(e.data)));
es.addEventListener("summary",        e => { closeUI(JSON.parse(e.data)); es.close(); });
es.addEventListener("error",          e => { showError(JSON.parse(e.data)); });
```

**Ordering guarantee**: within one claim's run, events fire in strict
order — `shape_start`(A) → `rule_evaluated`(A.r1) → `rule_evaluated`(A.r2)
→ `shape_start`(B) → … → `claim`. The `claim` event is published only
after `RuleExecutionRun` is committed, so a subscriber can immediately
call `GET /runs/<run_id>/` or `GET /runs/<run_id>/nodes/` on the
`run_id` it sees.

**Catch-up on reconnect**: already-finished claims are replayed as
`claim` events on connect (deduped by `run_id` against subsequent live
events). `shape_start` / `rule_evaluated` events are **not** replayed —
they're live-only. A reconnect mid-batch sees only verdicts for the
claims it missed, not their step-by-step detail. Full per-Shape history
is available via `GET /runs/<run_id>/nodes/` after the fact.

**Terminal-batch shortcut**: if the batch is already `COMPLETED` /
`PARTIAL` / `FAILED` when the SPA connects, the bridge replays all
claims, emits a synthetic `summary`, and closes immediately.

**Failure modes**

* `404 Not Found` — unknown `batch_id`. Delivered as a single
  `event: error` SSE payload (SSE responses can't easily 404
  mid-stream).
* `event: error` mid-stream — Celery task crashed, Redis unavailable,
  or the bridge itself faulted. Treat as terminal.
* Client disconnect — handled gracefully; the Celery task keeps running
  and the SPA can reconnect.

**Operational note**: the SSE endpoint pins one gunicorn sync worker
per active stream. For production with > 2 concurrent streams, run the
SSE endpoint on a `gthread`-worker gunicorn process (`gunicorn -k
gthread --threads 64`) or split it onto its own process.

### `GET /api/execute/runs/<run_id>/`

One claim's full audit trail — every `RuleEvaluation` + every
`ToolInvocationRecord` inlined.

```json
{
  "id": "uuid",
  "batch": "uuid",
  "workflow": "uuid",
  "claim_id": "CLM-12345",
  "claim_payload": { "...": "..." },
  "raw_fetch":     { "...": "..." },
  "status": "TERMINATED_EARLY",
  "final_decision_type": "DENY",
  "applied_codes": ["E51", "346"],
  "narrative": "...",
  "evaluations": [
    {"order_index": 0, "rule_key": "pre:42:5:0", "rule_source": "PRECONDITION",
     "condition": "...", "action": "...", "matched": true, "confidence": 0.92,
     "reasoning": "...", "decision_type": "ALLOW", "codes": [],
     "tool_results_used": [], "llm_provider": "anthropic", "llm_ms": 1840}
  ],
  "tool_invocations": [
    {"id": 1, "tool_name": "linx_claim_search", "phase": "FETCH",
     "args": {"subscriber_id": "CLM-12345"}, "ok": true,
     "result": {"...": "..."}, "error": "", "duration_ms": 142,
     "called_at": "2026-05-27T04:46:12Z"}
  ]
}
```

Cost telemetry is on every LLM attempt: query `sop_ingestion_llmcalllog`
WHERE `execution_run_id = '<run_id>'` for the per-attempt token /
duration breakdown (the response above carries only the final retained
provider + duration per evaluation).

### `GET /api/execute/runs/<run_id>/nodes/`

Per-canvas-node rollup for one claim's run. Walks the already-persisted
`RuleEvaluation` + `ToolInvocationRecord` rows for the run and groups
them by the Shape that owned each binding — one entry per node the engine
visited, in the order it visited them. No new tables; this is purely a
derived view.

Useful for the SPA's "how did this claim flow through this workflow?"
debug panel, where you want to see node A's verdict, then node B's,
without re-fetching the whole audit blob.

```json
{
  "run_id": "uuid",
  "workflow_id": "uuid",
  "claim_id": "CLM-12345",
  "status": "TERMINATED_EARLY",
  "final_decision_type": "DENY",
  "applied_codes": ["E51", "346"],
  "narrative": "Halted at shape Timely filing by rule step:42:3:2 ...",
  "nodes": [
    {
      "shape_id": "8f2c…",
      "shape_label": "Eligibility",
      "rules_evaluated": 1,
      "rules_matched": 1,
      "matched_decision_types": ["ALLOW"],
      "terminated_here": false,
      "evaluations": [
        {"order_index": 0, "rule_key": "pre:42:5:0", "rule_source": "PRECONDITION",
         "condition": "...", "action": "...", "matched": true, "confidence": 0.92,
         "reasoning": "Member eligible on DOS per Linx.",
         "decision_type": "ALLOW", "codes": [],
         "llm_provider": "anthropic", "llm_ms": 1840}
      ],
      "tool_invocations": []
    },
    {
      "shape_id": "9b1a…",
      "shape_label": "Timely filing",
      "rules_evaluated": 1,
      "rules_matched": 1,
      "matched_decision_types": ["DENY"],
      "terminated_here": true,
      "evaluations": [
        {"order_index": 1, "rule_key": "step:42:3:2", "rule_source": "DECISION",
         "condition": "DOS > 180d", "action": "Deny ...", "matched": true,
         "confidence": 0.95, "reasoning": "...",
         "decision_type": "DENY", "codes": ["E51", "346"],
         "llm_provider": "anthropic", "llm_ms": 2100}
      ],
      "tool_invocations": [
        {"tool_name": "check_diagnosis", "phase": "EVALUATE",
         "ok": true, "duration_ms": 88, "error": "",
         "called_at": "2026-05-27T04:46:13Z"}
      ]
    }
  ],
  "outer_tool_invocations": [
    {"tool_name": "linx_claim_search", "phase": "FETCH",
     "ok": true, "duration_ms": 142, "error": "",
     "called_at": "2026-05-27T04:46:12Z"}
  ]
}
```

**Field notes**

- `nodes[]` is ordered by `RuleEvaluation.order_index` (the order the engine
  walked the canvas).
- `terminated_here` is `true` on the first node whose matched-rule list
  contains a `DENY` or `STOP` (only meaningful when `status =
  TERMINATED_EARLY`).
- `outer_tool_invocations` collects tool calls that don't belong to any
  Shape — the outer-layer `FETCH` (claim fetch via `linx_claim_search`)
  and optional `PARSE`. Inner-pipeline `EVALUATE` tool calls bound to a
  specific Shape live under `nodes[].tool_invocations` instead.
- If a `NodeRuleBinding` has been deleted since the run, the corresponding
  evaluations are grouped under a synthetic `shape_id` of the form
  `orphaned:<rule_key>` with an empty `shape_label`. This preserves the
  per-rule history without dropping it.

---

## 10. Errors

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
