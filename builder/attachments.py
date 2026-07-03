"""Workflow attachments: SOP ingestion + runtime API agents.

Called from ``WorkflowViewSet`` after a workflow is created (or updated)
with ``sop_urls`` / ``runtime_agents``.  Two side effects:

1. **SOP URLs** → create an ``IngestionJob`` per URL, FK'd to the workflow,
   and dispatch the existing Celery task ``run_ingestion_pipeline``.
2. **Runtime agents** → register each as an endpoint in the
   ``api_agent_endpoints`` table via ``ApiAgentPipeline.register()``.
   The returned ``endpoint_id`` is stored back on
   ``Workflow.metadata['runtime_agents'][i]['endpoint_id']``.

Both operations are best-effort: a failed ingest dispatch is recorded on
the workflow's metadata under ``attachment_errors`` rather than failing
the whole create call — the user still gets their workflow.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

from django.conf import settings as djsettings

from sop_ingestion.models import IngestionJob, TriggerSource
from sop_ingestion.tasks import run_ingestion_pipeline

from .models import Workflow

log = logging.getLogger(__name__)

_ENV_PATH = Path(djsettings.BASE_DIR) / ".env"


# ── SOPs ────────────────────────────────────────────────────────────────────

def dispatch_sop_ingestions(workflow: Workflow, urls: Iterable[str]) -> list[dict]:
    """Create + queue an IngestionJob for every URL.

    Returns a list of ``{job_id, seed_url, status}`` dicts (also useful
    for write-back into ``Workflow.metadata``).
    """
    results: list[dict] = []
    for url in urls:
        url = (url or "").strip()
        if not url:
            continue
        try:
            job = IngestionJob.objects.create(
                workflow=workflow,
                seed_url=url,
                trigger_source=TriggerSource.WORKFLOW,
            )
            try:
                task = run_ingestion_pipeline.delay(str(job.job_id))
                job.celery_task_id = task.id
                job.save(update_fields=["celery_task_id"])
            except Exception as celery_exc:
                # Job row exists; just mark dispatch failure.  User can retry.
                log.warning("Celery dispatch failed for job %s: %s",
                            job.job_id, celery_exc)
                job.mark_failed(f"Celery dispatch failed: {celery_exc}")
            results.append({
                "job_id":   str(job.job_id),
                "seed_url": url,
                "status":   job.status,
            })
        except Exception as exc:
            log.exception("Could not create IngestionJob for %s", url)
            results.append({
                "job_id":   None,
                "seed_url": url,
                "status":   f"ERROR: {exc}",
            })
    return results


# ── Runtime agents (uhc-api-agent) ──────────────────────────────────────────

def _make_auth_payload(agent: dict) -> dict:
    """Translate the modal's auth fields into uhc_api_agent.AuthSpec kwargs."""
    auth_type  = (agent.get("auth_type") or "none").lower()
    token      = (agent.get("auth_token") or "").strip()
    if auth_type == "bearer":
        return {"type": "bearer", "token": token}
    if auth_type == "api_key":
        return {"type": "api_key", "api_key": token,
                "header_name": agent.get("header_name") or "Authorization"}
    if auth_type == "basic":
        user, _, pw = token.partition(":")
        return {"type": "basic", "username": user, "password": pw}
    return {"type": "none"}


def _mirror_to_tool_registry(agent: dict) -> None:
    """Upsert one ``Tool`` (kind='api_agent') for the registered endpoint.

    Best-effort: if ``agent_tools`` is not installed yet (e.g. during very
    early migrations) we silently skip. The runtime agent stays usable via
    its endpoint_id either way.
    """
    try:
        from agent_tools.models import Tool
    except Exception:
        return
    name = (agent.get("name") or "").strip()
    endpoint_id = (agent.get("endpoint_id") or "").strip()
    slug_source = name or endpoint_id or agent.get("url") or ""
    if not slug_source:
        return
    # Slug-safe identifier — keep it stable so re-registers don't double.
    slug = "agent_" + (
        endpoint_id
        or "".join(c if c.isalnum() else "_" for c in slug_source.lower())
    )
    slug = slug[:128]
    defaults = {
        "display_name": name or slug,
        "description": agent.get("description", "") or "",
        "kind": "api_agent",
        "invoke_url": agent.get("url", "") or "",
        "args_schema": {},
        "metadata": {
            "method": (agent.get("method") or "GET").upper(),
            "auth_type": agent.get("auth_type") or "none",
        },
        "endpoint_id": endpoint_id,
        "is_active": True,
    }
    try:
        Tool.objects.update_or_create(name=slug, defaults=defaults)
    except Exception as exc:
        log.warning("agent_tools: failed to mirror runtime agent %s: %s", slug, exc)


def register_runtime_agents(workflow: Workflow,
                            agents: list[dict]) -> list[dict]:
    """Save each agent as an endpoint in api_agent_endpoints.

    Returns the agents list with an ``endpoint_id`` filled in for each,
    ready to be stored on ``Workflow.metadata``.  The plaintext
    ``auth_token`` is **stripped** from the returned dicts so it never
    lands back in workflow metadata.
    """
    if not agents:
        return []

    try:
        # Imported lazily — package may not be installed in every env.
        from uhc_api_agent import ApiAgentPipeline
    except Exception as exc:
        log.warning("uhc_api_agent unavailable, agents stored without "
                    "endpoint_id: %s", exc)
        out = []
        for a in agents:
            clean = {k: v for k, v in a.items() if k != "auth_token"}
            clean["endpoint_id"] = ""
            out.append(clean)
            _mirror_to_tool_registry(clean)
        return out

    try:
        pipeline = ApiAgentPipeline(env_path=_ENV_PATH if _ENV_PATH.exists() else None)
    except Exception as exc:
        log.exception("ApiAgentPipeline init failed; storing agents only: %s", exc)
        out = []
        for a in agents:
            clean = {k: v for k, v in a.items() if k != "auth_token"}
            clean["endpoint_id"] = ""
            out.append(clean)
            _mirror_to_tool_registry(clean)
        return out

    out: list[dict] = []
    for agent in agents:
        clean = {k: v for k, v in agent.items() if k != "auth_token"}
        try:
            endpoint_id = pipeline.register(
                url=agent["url"],
                method=(agent.get("method") or "GET").upper(),
                auth=_make_auth_payload(agent),
                name=agent.get("name") or "",
            )
            clean["endpoint_id"] = endpoint_id
        except Exception as exc:
            log.exception("Could not register agent %r: %s",
                          agent.get("name"), exc)
            clean["endpoint_id"] = ""
            clean["register_error"] = str(exc)
        out.append(clean)
        _mirror_to_tool_registry(clean)
    return out


# ── Top-level orchestrator ──────────────────────────────────────────────────

def attach_to_workflow(workflow: Workflow,
                       *,
                       sop_urls: list[str] | None = None,
                       runtime_agents: list[dict] | None = None) -> dict:
    """Run both side effects and persist results back to workflow.metadata.

    Returns a summary dict for inclusion in the response.
    """
    sop_results = dispatch_sop_ingestions(workflow, sop_urls or [])
    agent_results = register_runtime_agents(workflow, runtime_agents or [])

    meta: dict[str, Any] = workflow.metadata or {}
    if sop_results:
        meta.setdefault("sop_ingestion_jobs", []).extend(sop_results)
    if agent_results:
        meta["runtime_agents"] = agent_results
    workflow.metadata = meta
    workflow.save(update_fields=["metadata", "updated_at"])

    return {"sops": sop_results, "agents": agent_results}
