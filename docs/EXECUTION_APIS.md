# Execution APIs

Execution-specific REST and streaming endpoints exposed by this backend.

- **Base path:** `/api/execute/`
- **Auth:** currently `AllowAny` on execution views
- **Primary use case:** run workflow-driven claim adjudication over uploaded Excel files

---

## 1) Run Batch (Synchronous)

### `POST /api/execute/workflows/<workflow_id>/run-batch/`

Runs the uploaded workbook immediately and returns the final batch result in one response.

**Request**
- Content type: `multipart/form-data`
- Fields:
  - `file` (**required**) — `.xlsx` file
  - `claim_id_column` (optional) — defaults to `claim_id`
  - `sheet_name` (optional) — defaults to first sheet

**Responses**
- `200 OK` — completed/partial batch payload
- `400 Bad Request` — missing/invalid file or engine-level validation failure
- `500 Internal Server Error` — execution engine crash

---

## 2) Run Batch (Asynchronous)

### `POST /api/execute/workflows/<workflow_id>/run-batch-async/`

Accepts the same multipart upload as sync mode, creates a running batch row, dispatches Celery work, and returns immediately.

**Request**
- Content type: `multipart/form-data`
- Fields:
  - `file` (**required**) — `.xlsx` file
  - `claim_id_column` (optional) — defaults to `claim_id`
  - `sheet_name` (optional)

**Response (`202 Accepted`)**

```json
{
  "batch_id": "uuid",
  "status": "RUNNING",
  "stream_url": "/api/execute/batches/<batch_id>/events/"
}
```

**Errors**
- `400 Bad Request` — missing/invalid file
- `500 Internal Server Error` — upload could not be stashed

---

## 3) Get Batch Detail

### `GET /api/execute/batches/<batch_id>/`

Returns a persisted batch with run summaries.

**Responses**
- `200 OK` — batch payload with `runs[]`
- `404 Not Found` — unknown `batch_id`

---

## 4) Stream Batch Events (SSE)

### `GET /api/execute/batches/<batch_id>/events/`

Server-Sent Events stream for live async batch progress.

**Response headers**
- `Content-Type: text/event-stream`
- `Cache-Control: no-store`
- `X-Accel-Buffering: no`

`Connection: keep-alive` is managed by the HTTP server (gunicorn / nginx)
on HTTP/1.1 and is **not** set by the app — it's a hop-by-hop header
(RFC 7230 §6.1), so emitting it from a WSGI app crashes wsgiref / runserver.

**Event kinds**
- `batch_start`
- `shape_start`
- `rule_evaluated`
- `claim`
- `summary` (terminal)
- `error` (terminal)
- Keepalive comments (`: keepalive`)

**Notes**
- Uses Redis pub/sub channel `batch:<batch_id>`
- Replays already-finished claims on reconnect

---

## 5) Get Run Detail

### `GET /api/execute/runs/<run_id>/`

Returns one claim run with full audit trail (evaluations + tool invocations).

**Responses**
- `200 OK`
- `404 Not Found`

---

## 6) Get Run Node Rollup

### `GET /api/execute/runs/<run_id>/nodes/`

Returns a node-wise grouped view of one claim run.

Includes:
- per-node evaluations
- per-node tool invocations
- `outer_tool_invocations` (for non-shape-bound phases like `FETCH`/`PARSE`)

**Responses**
- `200 OK`
- `404 Not Found`

---

## 7) Get Claim Processing Snapshot

### `GET /api/claims/<claim_id>/processing/`

Aggregated claim-detail payload for the SPA in a single request. This endpoint
is served by `execution_app` but mounted outside `/api/execute/` so it matches
the SPA contract (`/api/claims/:claimId/processing/`).

**Query params**
- `run_id` (optional) — UUID; direct run lookup; overrides claim lookup
- `batch_id` (optional) — UUID; narrows claim lookup to one batch

**Response (`200 OK`)**
- Top-level fields:
  - `claimId`, `runId`, `batchId`, `workflowId`
  - `claimStatus` (`MET` | `NOT_MET` | `INCONCLUSIVE` | `DEFECT`)
  - `processingTimeMin`, `startedAt`, `finishedAt`
  - `agents[]` (per-canvas-node status, processSummary, steps)
  - `outerToolInvocations[]` (FETCH/PARSE and non-shape calls)
  - `reviewStatus`, `feedback` (currently both `null`)

**Error envelope**

```json
{
  "error": "Human-readable message",
  "details": {},
  "source": "django"
}
```

**Errors**
- `400` malformed `run_id`/`batch_id` UUID
- `404` no run found for claim
- `500` unexpected server failure

---

## URL Summary

- `POST /api/execute/workflows/<workflow_id>/run-batch/`
- `POST /api/execute/workflows/<workflow_id>/run-batch-async/`
- `GET /api/execute/batches/<batch_id>/`
- `GET /api/execute/batches/<batch_id>/events/`
- `GET /api/execute/runs/<run_id>/`
- `GET /api/execute/runs/<run_id>/nodes/`
- `GET /api/claims/<claim_id>/processing/`
