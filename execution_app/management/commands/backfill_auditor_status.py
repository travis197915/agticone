"""Backfill ``RuleExecutionRun.auditor_status`` from an ERA Excel upload.

Self-contained: parses ClaimID + Audit_Sts with openpyxl directly, so it
works on environments that do not yet have the upload-path Audit_Sts changes.
Only requires the existing ``auditor_status`` column (migration 0008).

Does not re-run the engine — metadata only.

Usage:
    PYTHONPATH=. python manage.py backfill_auditor_status /path/to/ERA.xlsx
    PYTHONPATH=. python manage.py backfill_auditor_status ./199_ERA.xlsx --dry-run
    PYTHONPATH=. python manage.py backfill_auditor_status ./199_ERA.xlsx --batch-id <uuid>
    PYTHONPATH=. python manage.py backfill_auditor_status ./199_ERA.xlsx --force
    PYTHONPATH=. python manage.py backfill_auditor_status ./199_ERA.xlsx --latest-per-claim
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from execution_app.models import RuleExecutionRun

_CLAIM_ID_ALIASES = (
    "claim_id", "claimid", "subscriber_id", "subscriberid",
    "claim id", "subscriber id",
)
_AUDIT_STS_ALIASES = ("audit_sts", "audit sts", "auditor_status", "auditor status")


def _norm_header(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def _find_column(norm_headers: list[str], aliases: tuple[str, ...]) -> int:
    for alias in aliases:
        key = _norm_header(alias)
        if key in norm_headers:
            return norm_headers.index(key)
    return -1


def _parse_audit_sts_map(xlsx_bytes: bytes) -> tuple[dict[str, str], str, int, int]:
    """Return ``(claim_id → Audit_Sts, claim_col_name, total_rows, missing_sts)``.

    Independent of ``uhc_execution_engine.xlsx_parser`` so this command can run
    before the upload-path Audit_Sts changes are deployed.
    """
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise CommandError("openpyxl is required: pip install openpyxl") from exc

    try:
        wb = load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    except Exception as exc:
        raise CommandError(f"failed to open workbook: {exc}") from exc

    ws = wb[wb.sheetnames[0]]
    rows = ws.iter_rows(values_only=True)
    header_row = next(rows, None)
    if not header_row:
        raise CommandError("workbook has no rows")

    headers = [(str(h).strip() if h is not None else "") for h in header_row]
    norm = [_norm_header(h) for h in headers]

    claim_idx = -1
    for alias in _CLAIM_ID_ALIASES:
        claim_idx = _find_column(norm, (alias,))
        if claim_idx >= 0:
            break
    if claim_idx < 0:
        raise CommandError(
            f"could not find a claim-id column "
            f"(looked for {list(_CLAIM_ID_ALIASES)}); headers: {headers}"
        )

    sts_idx = _find_column(norm, _AUDIT_STS_ALIASES)
    if sts_idx < 0:
        raise CommandError(
            f"could not find Audit_Sts column "
            f"(looked for {list(_AUDIT_STS_ALIASES)}); headers: {headers}"
        )

    by_claim: dict[str, str] = {}
    total = 0
    missing_sts = 0
    for row in rows:
        if claim_idx >= len(row):
            continue
        claim_cell = row[claim_idx]
        if claim_cell is None:
            continue
        claim_id = str(claim_cell).strip()
        if not claim_id:
            continue
        total += 1
        sts_cell = row[sts_idx] if sts_idx < len(row) else None
        status = str(sts_cell).strip().upper() if sts_cell is not None else ""
        if not status:
            missing_sts += 1
            continue
        # Last row wins if the sheet has duplicate claim ids.
        by_claim[claim_id] = status

    return by_claim, headers[claim_idx], total, missing_sts


class Command(BaseCommand):
    help = (
        "Update existing RuleExecutionRun.auditor_status from an ERA Excel "
        "Audit_Sts column (matched by claim_id). Self-contained — does not "
        "require the new upload-path parser changes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "xlsx_path",
            type=str,
            help="Path to the .xlsx workbook (must include ClaimID + Audit_Sts).",
        )
        parser.add_argument(
            "--batch-id",
            default="",
            help="Only update runs belonging to this batch UUID.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Overwrite auditor_status even when already set.",
        )
        parser.add_argument(
            "--latest-per-claim",
            action="store_true",
            help="Only update the most recent run for each claim_id.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse + report matches; do not write.",
        )

    def handle(self, *args, **opts):
        path = Path(opts["xlsx_path"]).expanduser().resolve()
        if not path.is_file():
            raise CommandError(f"file not found: {path}")
        if path.suffix.lower() not in {".xlsx", ".xlsm"}:
            raise CommandError(f"expected an .xlsx file, got: {path.suffix!r}")

        by_claim, claim_col, total_rows, missing_sts = _parse_audit_sts_map(
            path.read_bytes()
        )

        if not by_claim:
            raise CommandError(
                f"no rows with both claim id ({claim_col!r}) and Audit_Sts "
                f"in {path.name}"
            )

        self.stdout.write(
            f"Parsed {total_rows} row(s) from {path.name} "
            f"→ {len(by_claim)} claim(s) with Audit_Sts"
            + (f" ({missing_sts} missing Audit_Sts)" if missing_sts else "")
        )

        qs_all = RuleExecutionRun.objects.filter(claim_id__in=by_claim.keys())
        if opts["batch_id"]:
            qs_all = qs_all.filter(batch_id=opts["batch_id"])

        # True "not in DB" — compute before the empty-status filter.
        claims_in_db = set(qs_all.values_list("claim_id", flat=True))
        unmatched = sorted(set(by_claim) - claims_in_db)

        qs = qs_all
        if not opts["force"]:
            qs = qs.filter(Q(auditor_status="") | Q(auditor_status__isnull=True))

        qs = qs.order_by("-started_at")
        runs = list(qs.only("id", "claim_id", "auditor_status", "started_at"))

        if opts["latest_per_claim"]:
            seen: set[str] = set()
            filtered: list[RuleExecutionRun] = []
            for run in runs:
                if run.claim_id in seen:
                    continue
                seen.add(run.claim_id)
                filtered.append(run)
            runs = filtered

        already_set = len(claims_in_db) - len({r.claim_id for r in runs})
        if not opts["force"] and already_set > 0:
            self.stdout.write(
                f"Skipping {already_set} claim(s) that already have "
                f"auditor_status (use --force to overwrite)."
            )

        if not runs:
            self.stdout.write(self.style.WARNING(
                "No matching runs to update "
                "(try --force, drop --batch-id, or check claim ids)."
            ))
            if unmatched:
                self.stdout.write(
                    f"{len(unmatched)} excel claim(s) with no DB run."
                )
            return

        updated = 0
        unchanged = 0
        for run in runs:
            new_status = by_claim[run.claim_id]
            if run.auditor_status == new_status:
                unchanged += 1
                continue
            if opts["dry_run"]:
                self.stdout.write(
                    f"  would set claim={run.claim_id} run={run.id} "
                    f"{run.auditor_status or '(empty)'} → {new_status}"
                )
                updated += 1
                continue
            run.auditor_status = new_status
            run.save(update_fields=["auditor_status"])
            updated += 1

        verb = "Would update" if opts["dry_run"] else "Updated"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {updated} run(s); {unchanged} already matched; "
            f"{len(unmatched)} excel claim(s) with no DB run"
        ))
        if unmatched and opts.get("verbosity", 1) >= 2:
            preview = ", ".join(unmatched[:20])
            more = f" (+{len(unmatched) - 20} more)" if len(unmatched) > 20 else ""
            self.stdout.write(f"  unmatched: {preview}{more}")
