# uhc-execution-engine

Rule execution engine for the UHC claims-audit backend. Excel of claim ids in,
per-claim adjudication (DENY/ALLOW/PEND/...) out.

## Pipeline

**Outer (batch) layer** — `BatchRunner`
```
upload .xlsx → parse_workbook → for each claim_id:
                                    fetch_claim (linx_claim_search)
                                    parse_claim (llm_parse_claim_with_ontology, optional)
                                    run inner pipeline ↓
                              → aggregate batch summary
```

**Inner (per-claim) layer** — LangGraph, 7 nodes
```
validate_input → load_bindings → run_tools →
evaluate_preconditions → evaluate_decisions →
aggregate_decision → persist_and_respond
```

Each rule is evaluated against the claim by a single LLM call (Anthropic primary,
OpenAI fallback) using the dual-provider/retry pattern lifted from
`uhc-sop-ingestion`. All calls log to the existing `LLMCallLog` table.

## Install

```
pip install -e ./uhc-execution-engine
```

The package shares the repo's `.env` and Django/Postgres setup; it must be
imported from within the Django process (it reads `agent_tools.registry` and
the SOP / binding ORM models directly).

## Python API

```python
from uhc_execution_engine import RuleEnginePipeline, BatchRunner

# Single claim:
out = RuleEnginePipeline().run(workflow_id="<uuid>", claim={"subscriber_id": "..."})

# Batch from Excel bytes:
out = BatchRunner().run_xlsx(
    workflow_id="<uuid>",
    xlsx_bytes=open("claims.xlsx", "rb").read(),
    filename="claims.xlsx",
    claim_id_column="subscriber_id",
)
```

## REST surface (exposed by `execution_app` Django app)

- `POST /api/execute/workflows/<id>/run-batch/` — multipart upload of `.xlsx`
- `GET  /api/execute/batches/<batch_id>/` — fetch a prior batch result
- `GET  /api/execute/runs/<run_id>/` — fetch one claim's audit trail
