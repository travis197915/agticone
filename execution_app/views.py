"""REST views for the rule execution agent.

POST /api/execute/workflows/<workflow_id>/run-batch/   multipart upload
GET  /api/execute/batches/<batch_id>/                  prior batch result
GET  /api/execute/runs/<run_id>/                       single-claim audit trail
"""
from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import BatchExecutionRun, RuleExecutionRun
from .serializers import (BatchExecutionRunSerializer,
                           RuleExecutionRunSerializer)

logger = logging.getLogger(__name__)


class RunBatchView(APIView):
    """POST /api/execute/workflows/<workflow_id>/run-batch/

    Multipart form: ``file`` (.xlsx, required), ``claim_id_column`` (optional),
    ``sheet_name`` (optional). Runs every claim in the Excel through the rule
    engine and returns a JSON batch summary. Synchronous for v1.
    """
    parser_classes = [MultiPartParser]
    permission_classes = [AllowAny]

    def post(self, request: Request, workflow_id: str) -> Response:
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"detail": "file (multipart) is required"},
                            status=status.HTTP_400_BAD_REQUEST)
        if not upload.name.lower().endswith(".xlsx"):
            return Response({"detail": "only .xlsx is supported"},
                            status=status.HTTP_400_BAD_REQUEST)

        from uhc_execution_engine import BatchRunner
        runner = BatchRunner()
        try:
            result = runner.run_xlsx(
                workflow_id=workflow_id,
                xlsx_bytes=upload.read(),
                filename=upload.name,
                claim_id_column=request.data.get("claim_id_column") or None,
                sheet_name=request.data.get("sheet_name") or None,
            )
        except Exception as exc:
            logger.exception("run-batch crashed")
            return Response(
                {"detail": f"engine crashed: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        http_status = status.HTTP_200_OK
        if result.get("status") == "FAILED":
            http_status = status.HTTP_400_BAD_REQUEST
        return Response(result, status=http_status)


class BatchDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, _request: Request, batch_id: str) -> Response:
        try:
            batch = BatchExecutionRun.objects.prefetch_related("runs").get(id=batch_id)
        except BatchExecutionRun.DoesNotExist:
            return Response({"detail": "not found"},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(BatchExecutionRunSerializer(batch).data)


class RunDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, _request: Request, run_id: str) -> Response:
        try:
            run = (RuleExecutionRun.objects
                   .prefetch_related("evaluations", "tool_invocations")
                   .get(id=run_id))
        except RuleExecutionRun.DoesNotExist:
            return Response({"detail": "not found"},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(RuleExecutionRunSerializer(run).data)
