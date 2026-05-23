"""
LangChain Tool for Covered Diagnosis Query

This module provides a structured LangChain tool for querying diagnosis coverage
information using the Covered Diagnosis API.
"""

from typing import Dict, Any
import hashlib
import json
from pathlib import Path
from langchain_core.tools import StructuredTool

from tools.diagnosis_api_client import DiagnosisAPIClient
from tools.schemas.schema_diagnosis_tool import DiagnosisInput, DiagnosisOutput, DiagnosisResult
from thynkr_bhagenticai.logging_utils import get_logger

logger = get_logger(__name__)

def check_diagnosis_coverage_func(diagnosis_code: str) -> Dict[str, Any]:
    """
    Check coverage information for a diagnosis code.

    This function queries the Covered Diagnosis API to determine the type
    and coverage status for a specified diagnosis code.

    Args:
        diagnosis_code: Diagnosis code to check (e.g., ICD-10 code like 'E11.9')

    Returns:
        Dictionary with diagnosis coverage details
    """
    logger.info(f"Checking diagnosis coverage: code={diagnosis_code}")

    # Check cache first
    cache_dir = Path(__file__).parent.parent / "data" / "tool_cache" / "check_diagnosis_coverage"
    cache_dir.mkdir(parents=True, exist_ok=True)

    cache_key = hashlib.sha256(diagnosis_code.upper().encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.json"

    if cache_file.exists():
        try:
            with cache_file.open("r", encoding="utf-8") as f:
                cached = json.load(f)
            logger.info("Cache hit for diagnosis query")
            return cached
        except Exception as e:
            logger.warning(f"Cache read error: {e}")

    try:
        client = DiagnosisAPIClient()
        response_data = client.fetch_diagnosis_info(diagnosis_code)

        logger.info(f"API response for {diagnosis_code}: type={type(response_data)}, data={response_data}")

        if response_data:
            if isinstance(response_data, list) and response_data:
                data_item = None
                for item in response_data:
                    if item.get("code", "").upper() == diagnosis_code.upper():
                        data_item = item
                        break
                if not data_item:
                    # Not found in coverage DB is a valid business answer (not covered).
                    output = DiagnosisOutput(
                        success=True,
                        diagnosis_code=diagnosis_code,
                        result=DiagnosisResult(
                            diagnosis_code=diagnosis_code,
                            code_type=None,
                            covered="No",
                            description="Diagnosis code not found in coverage",
                        ),
                        error=None,
                    )
                    try:
                        with cache_file.open("w", encoding="utf-8") as f:
                            json.dump(output.model_dump(exclude_none=True), f, ensure_ascii=False, indent=2)
                    except Exception as e:
                        logger.warning(f"Cache write error: {e}")
                    return output.model_dump(exclude_none=True)
            elif isinstance(response_data, dict):
                data_item = response_data
            else:
                data_item = {}

            # Extract fields based on actual API response structure
            # type1, type2, type3 represent different categorizations
            code_types = []
            for type_field in ["type1", "type2", "type3"]:
                type_val = data_item.get(type_field)
                if type_val and type_val != "N/A":
                    code_types.append(type_val)
            code_type_str = ", ".join(code_types) if code_types else None

            # Coverage is determined from coverageRecommend field
            coverage_recommend = data_item.get("coverageRecommend", "")
            covered_str = None
            if coverage_recommend:
                if "Cover Services" in coverage_recommend:
                    covered_str = "Yes"
                else:
                    covered_str = "No"

                result = DiagnosisResult(
                    diagnosis_code=diagnosis_code,
                    code_type=code_type_str,
                    covered=covered_str,
                    description=data_item.get("diagDesc")
                )

                output = DiagnosisOutput(
                    success=True,
                    diagnosis_code=diagnosis_code,
                    result=result,
                    error=None
                )
            else:
                # Empty API response -> code not in covered-diagnosis DB (not covered).
                output = DiagnosisOutput(
                    success=True,
                    diagnosis_code=diagnosis_code,
                    result=DiagnosisResult(
                        diagnosis_code=diagnosis_code,
                        code_type=None,
                        covered="No",
                        description="Diagnosis code not found in coverage",
                    ),
                    error=None,
                )

            # Save to cache
            try:
                with cache_file.open("w", encoding="utf-8") as f:
                    json.dump(output.model_dump(exclude_none=True), f, ensure_ascii=False)
                logger.info(f"Cached diagnosis result for {diagnosis_code}")
            except Exception as e:
                logger.warning(f"Cache write error: {e}")

            return output.model_dump(exclude_none=True)

    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return DiagnosisOutput(
            success=False,
            diagnosis_code=diagnosis_code,
            result=None,
            error=f"Configuration error: {str(e)}"
        ).model_dump(exclude_none=True)

    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return DiagnosisOutput(
            success=False,
            diagnosis_code=diagnosis_code,
            result=None,
            error=f"Unexpected error: {str(e)}"
        ).model_dump(exclude_none=True)


# Create StructuredTool
diagnosis_coverage_tool = StructuredTool.from_function(
    func=check_diagnosis_coverage_func,
    name="check_diagnosis_coverage",
    description="""Check coverage information for a diagnosis code.

This tool queries the Covered Diagnosis API to determine the type and coverage status
of a diagnosis code (e.g., ICD-10 codes).

Input Parameters:
- diagnosis_code (required): Diagnosis code to check (e.g., 'E11.9', 'I10')

Returns JSON with:
- success: Whether the query was successful
- diagnosis_code: The code that was queried
- result: Object containing code_type, covered status, and description
- error: Error message if query failed""",
    args_schema=DiagnosisInput,
    return_direct=False
)


# Export
__all__ = [
    "diagnosis_coverage_tool",
    "check_diagnosis_coverage_func",
]
