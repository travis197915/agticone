"""Seed ClaimMemory from legacy audit exports (xlsx).

Imports the legacy system's per-step reasoning so the execution engine starts
warm for claims it has never processed: each claim's latest legacy execution
is mapped onto the ingested SOP structure and written as per-(claim, SOP)
ClaimMemory rows whose ``rule_memory`` entries carry the legacy rationale.

Granularity: the legacy export records ONE verdict per (claim, SOP, step);
the engine evaluates per decision ROW. The step verdict is therefore fanned
across every decision row of that step, keyed exactly as the rule loader
builds keys at runtime (``step:{sop.id}:{step_number}:{row_index}``). Because
that is coarser than the engine's own memory, every entry is marked
``seeded: true`` — the engine injects it as prompt context but never adopts
it via prior_wins; the first engine run rebuilds the row at full granularity.

SOP matching is by normalized title, plus explicit aliases for legacy names
that drifted from the ingested titles. Unmatched SOPs are reported, not
guessed. Rows already containing engine-written history are never touched.

Usage:
    PYTHONPATH=. python manage.py seed_claim_memory \
        [--dir reference_data] [--claim-id X] [--dry-run] [--overwrite]
"""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone as dt_timezone

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from execution_app.models import ClaimMemory

from uhc_execution_engine.memory import _NARRATIVE_CAP, _REASONING_CAP

STEP_EXEC_FILE = "SOP Step Executions.xlsx"
SUMMARY_FILE = "Summary Agent.xlsx"

# Legacy sop_name -> ingested AuditSop title, for names that drifted.
# Keys/values are normalized (casefold, collapsed whitespace).
SOP_ALIASES = {
    "timely filing sop": "obh facets timely filing (050526)",
}

# Legacy step status -> (matched, skipped, llm_status)
STATUS_MAP = {
    "met":            (True,  False, "Met"),
    "not met":        (False, False, "Not-Met"),
    "inconclusive":   (False, False, "Inconclusive"),
    "defect":         (True,  False, "Met"),
    "clean":          (False, False, "Not-Met"),
    "skip":           (False, True,  ""),
    "skipped":        (False, True,  ""),
    "not applicable": (False, True,  ""),
    "no group match": (False, False, "Inconclusive"),
}

# Legacy overall_status -> engine decision type (claim-level, injection-only)
DECISION_MAP = {
    "clean": "ALLOW", "met": "ALLOW",
    "defect": "DENY", "not met": "DENY",
    "inconclusive": "INCONCLUSIVE",
}


def _norm(s) -> str:
    return " ".join(str(s or "").casefold().split())


def _clean_text(s) -> str:
    # Common UTF-8-as-cp1252 mojibake in the legacy export.
    return str(s or "").replace("â†’", "→").strip()


def _iso(v) -> str:
    if isinstance(v, datetime):
        return v.isoformat()
    return datetime.now(dt_timezone.utc).isoformat()


def _read_sheet(path: str) -> list[dict]:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    headers = [str(h) if h is not None else "" for h in next(rows)]
    out = [dict(zip(headers, r)) for r in rows]
    wb.close()
    return out


class Command(BaseCommand):
    help = "Seed ClaimMemory rows from legacy audit xlsx exports."

    def add_arguments(self, parser):
        parser.add_argument("--dir", default="reference_data",
                            help="Directory containing the legacy xlsx exports.")
        parser.add_argument("--claim-id", default="",
                            help="Seed only this claim id.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would be written; change nothing.")
        parser.add_argument("--overwrite", action="store_true",
                            help="Replace existing seed-only rows. Rows with "
                                 "engine-written history are never touched.")

    # ── SOP structure from the ingested DB ──────────────────────────────────
    def _sop_index(self):
        from sop_ingestion.models import AuditSop, AuditStep

        by_title: dict[str, object] = {}
        steps_by_sop: dict[int, dict[int, list[int]]] = {}
        for sop in AuditSop.objects.all():
            by_title[_norm(sop.title)] = sop
            steps: dict[int, list[int]] = {}
            for step in (AuditStep.objects.filter(sop=sop)
                         .prefetch_related("decisions")):
                steps[step.step_number] = sorted(
                    d.row_index for d in step.decisions.all())
            steps_by_sop[sop.id] = steps
        return by_title, steps_by_sop

    def handle(self, *args, **opts):
        src = opts["dir"]
        for fname in (STEP_EXEC_FILE, SUMMARY_FILE):
            if not os.path.exists(os.path.join(src, fname)):
                raise CommandError(f"missing {fname} in {src!r}")

        by_title, steps_by_sop = self._sop_index()

        def resolve_sop(legacy_name: str):
            key = _norm(legacy_name)
            return by_title.get(SOP_ALIASES.get(key, key))

        # ── claim-level final output from the latest legacy summary ────────
        last_summary: dict[str, dict] = {}
        for row in _read_sheet(os.path.join(src, SUMMARY_FILE)):
            cid = str(row.get("claim_id") or "").strip()
            if not cid:
                continue
            cur = last_summary.get(cid)
            started = row.get("started_at")
            if cur is None or (isinstance(started, datetime)
                               and isinstance(cur.get("started_at"), datetime)
                               and started > cur["started_at"]):
                last_summary[cid] = row

        # ── latest legacy execution per (claim, sop) from step rows ────────
        # group: (claim, sop_name) -> execution_id -> rows; keep newest exec.
        grouped: dict[tuple[str, str], dict[str, list[dict]]] = \
            defaultdict(lambda: defaultdict(list))
        for row in _read_sheet(os.path.join(src, STEP_EXEC_FILE)):
            cid = str(row.get("claim_id") or "").strip()
            if not cid:
                continue
            if opts["claim_id"] and cid != opts["claim_id"]:
                continue
            grouped[(cid, str(row.get("sop_name") or "").strip())][
                str(row.get("execution_id") or "")].append(row)

        stats = defaultdict(int)
        unmatched_sops: set[str] = set()
        unmatched_steps: set[tuple[str, object]] = set()

        # rows to write: (claim_id, sop) -> rule_memory
        pending: dict[tuple[str, object], dict] = {}
        for (cid, sop_name), execs in grouped.items():
            sop = resolve_sop(sop_name)
            if sop is None:
                unmatched_sops.add(sop_name)
                continue
            latest_rows = max(
                execs.values(),
                key=lambda rows: max((r.get("started_at") for r in rows
                                      if isinstance(r.get("started_at"), datetime)),
                                     default=datetime.min))
            sop_steps = steps_by_sop.get(sop.id) or {}
            rule_memory = pending.setdefault((cid, sop), {})
            for row in latest_rows:
                step_no = row.get("sop_step_number")
                try:
                    step_no = int(step_no)
                except (TypeError, ValueError):
                    unmatched_steps.add((sop_name, step_no))
                    continue
                if step_no not in sop_steps:
                    # Legacy/ingested numbering drift: when the SOP has
                    # exactly one step, map onto it; otherwise skip.
                    if len(sop_steps) == 1:
                        step_no = next(iter(sop_steps))
                    else:
                        unmatched_steps.add((sop_name, step_no))
                        continue
                status_key = _norm(row.get("status"))
                matched, skipped, llm_status = STATUS_MAP.get(
                    status_key, (False, False, "Inconclusive"))
                reasoning = _clean_text(row.get("rationale"))[:_REASONING_CAP]
                at = _iso(row.get("ended_at") or row.get("started_at"))
                # Fan the step verdict across the step's decision rows,
                # keyed exactly as rule_loader builds keys at runtime.
                for row_index in sop_steps[step_no]:
                    rule_memory[f"step:{sop.id}:{step_no}:{row_index}"] = {
                        "matched": matched,
                        "skipped": skipped,
                        "confidence": 0.7,
                        "reasoning": reasoning,
                        "decision_type": "",
                        "llm_status": llm_status,
                        "navigation": None,
                        "run_id": "",
                        "at": at,
                        "seeded": True,
                        "source": "legacy_import",
                    }
                    stats["entries"] += 1

        # ── write ───────────────────────────────────────────────────────────
        for (cid, sop), rule_memory in sorted(
                pending.items(), key=lambda kv: (kv[0][0], kv[0][1].id)):
            if not rule_memory:
                continue
            summary = last_summary.get(cid) or {}
            decision = DECISION_MAP.get(_norm(summary.get("overall_status")), "")
            narrative = _clean_text(
                summary.get("rationale_summary"))[:_NARRATIVE_CAP]
            if opts["dry_run"]:
                stats["rows_written"] += 1
                continue
            with transaction.atomic():
                mem = (ClaimMemory.objects.select_for_update()
                       .filter(claim_id=cid, sop_id=str(sop.id))
                       .first())
                if mem is not None:
                    history = list(mem.run_history or [])
                    seed_only = all(
                        isinstance(h, dict) and h.get("source") == "legacy_seed"
                        for h in history)
                    if history and not seed_only:
                        stats["rows_kept_engine"] += 1
                        continue
                    if not opts["overwrite"]:
                        stats["rows_kept_existing"] += 1
                        continue
                else:
                    mem = ClaimMemory(claim_id=cid, sop_id=str(sop.id))
                mem.sop_title = sop.title or ""
                mem.runs_count = 0          # no engine run yet
                mem.last_run = None
                mem.last_decision_type = decision
                mem.last_narrative = narrative
                mem.claim_payload_hash = ""  # unknown -> memory active on first run
                mem.rule_memory = rule_memory
                mem.tool_memory = {}
                mem.run_history = [{
                    "run_id": "",
                    "source": "legacy_seed",
                    "status": "SEEDED",
                    "decision_type": decision,
                    "codes": [],
                    "finished_at": _iso(summary.get("ended_at")),
                }]
                mem.save()
            stats["rows_written"] += 1

        verb = "would write" if opts["dry_run"] else "wrote"
        self.stdout.write(self.style.SUCCESS(
            f"seed_claim_memory: {verb} {stats['rows_written']} row(s), "
            f"{stats['entries']} rule entries; "
            f"kept {stats['rows_kept_engine']} engine row(s), "
            f"{stats['rows_kept_existing']} existing seed row(s)"))
        for name in sorted(unmatched_sops):
            self.stdout.write(self.style.WARNING(
                f"  unmatched legacy SOP (skipped): {name!r}"))
        for sop_name, step_no in sorted(unmatched_steps,
                                        key=lambda t: (t[0], str(t[1]))):
            self.stdout.write(self.style.WARNING(
                f"  unmatched step (skipped): {sop_name!r} step {step_no}"))
