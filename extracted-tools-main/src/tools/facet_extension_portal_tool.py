import os
import requests
import time
from datetime import datetime
from typing import Dict, Any, Optional
from pathlib import Path
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import ToolNode
from dotenv import load_dotenv
from thynkr_bhagenticai.logging_utils import get_logger
from thynkr_bhagenticai.tool_cache import ToolCache

# Import schemas
from tools.schemas.schema_facet_extension_portal import ProviderInput, ProgrammeInput, GroupModelInput

# Import facets tool for get_claim_summary
from tools.facets_tool import get_claim_summary, _find_first_key


# Load environment variables
load_dotenv()

# Repo logger and shared cache
logger = get_logger(__name__)
_CACHE = ToolCache()

# API Configuration from environment
BASE_URL = os.getenv("FACET_EXTENSION_PORTAL_BASE_URL")

def fetch_data_facet_extension_portal_provider(provider_id: str, verify_ssl: bool = False) -> Dict:
    """
    Fetch complete list information from Facet Extension Portal API based on Provider ID.

    This function makes a GET request to the Facet API endpoint with a Provider ID
    and returns the complete list data for that provider.

    Args:
        provider_id: The Provider ID (PRPR ID) to query (e.g., FAC000022500)
        verify_ssl: Whether to verify SSL certificates (default: False for internal APIs)

    Returns:
        dict: A dictionary containing:
            - success (bool): Whether the request was successful
            - provider_id (str): The provider ID that was queried
            - data (dict): The API response data (if successful)
            - status_code (int): HTTP status code (if successful)
            - error (str): Error message (if failed)
            - message (str): Detailed error message (if failed)
    """
    # Normalize and check cache
    provider_id_norm = str(provider_id).strip()
    cached = _CACHE.get("facet_extension_portal_provider", {"provider_id": provider_id_norm})
    if cached.hit and isinstance(cached.value, dict):
        logger.info("Cache hit", extra={"endpoint": "facet_extension_portal_provider"})
        return cached.value

    try:
        # Construct the full URL with the provider ID as path parameter
        url = f"{BASE_URL}/getCompleteList/{provider_id_norm}"

        # Set up headers
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

        # Suppress SSL warnings if not verifying
        if not verify_ssl:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        # Make the GET request
        logger.info("Calling Facet Extension Portal provider", extra={"endpoint": "facet_extension_portal_provider"})
        response = requests.get(url, headers=headers, timeout=30, verify=verify_ssl)

        # Raise an exception for bad status codes
        response.raise_for_status()

        # Get the JSON response
        result = {
            "success": True,
            "provider_id": provider_id_norm,
            "data": response.json(),
            "status_code": response.status_code
        }

        # Field mapping transformation removed
        logger.info(
            "Facet Extension Portal provider response",
            extra={"endpoint": "facet_extension_portal_provider", "status_code": result.get("status_code")},
        )
        # Cache successful responses only
        _CACHE.set("facet_extension_portal_provider", {"provider_id": provider_id_norm}, result)
        return result

    except requests.exceptions.RequestException as e:
        result = {
            "success": False,
            "provider_id": provider_id_norm,
            "error": str(e),
            "message": f"Failed to retrieve data for provider ID: {provider_id_norm}"
        }

        # Field mapping transformation removed for error responses
        logger.warning(
            "Facet Extension Portal provider error",
            extra={"endpoint": "facet_extension_portal_provider", "error": result.get("error")},
        )
        # Do not cache errors
        return result


def fetch_data_facet_extension_portal_programme(program_detailed_id: str, verify_ssl: bool = False) -> Dict:
    """
    Fetch programme details from Facet Extension Portal API based on Program Details ID.

    This function makes a GET request to the Facet API endpoint with a Program Details ID
    and returns the programme data.

    Args:
        program_detailed_id: The Program Details ID to query (e.g., 276728)
        verify_ssl: Whether to verify SSL certificates (default: False for internal APIs)

    Returns:
        dict: A dictionary containing:
            - success (bool): Whether the request was successful
            - program_detailed_id (str): The program details ID that was queried
            - data (dict): The API response data (if successful)
            - status_code (int): HTTP status code (if successful)
            - error (str): Error message (if failed)
            - message (str): Detailed error message (if failed)
    """
    # Normalize and check cache
    program_id_norm = str(program_detailed_id).strip()
    cached = _CACHE.get("facet_extension_portal_programme", {"program_detailed_id": program_id_norm})
    if cached.hit and isinstance(cached.value, dict):
        logger.info("Cache hit", extra={"endpoint": "facet_extension_portal_programme", "program_detailed_id": program_id_norm})
        return cached.value

    try:
        # Construct the full URL with the program details ID as path parameter
        url = f"{BASE_URL}/getPrgm/{program_id_norm}"

        # Set up headers
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

        # Suppress SSL warnings if not verifying
        if not verify_ssl:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        # Make the GET request
        logger.info("Calling Facet Extension Portal programme", extra={"program_detailed_id": program_id_norm, "endpoint":
"facet_extension_portal_programme"})
        response = requests.get(url, headers=headers, timeout=30, verify=verify_ssl)

        # Raise an exception for bad status codes
        response.raise_for_status()

        # Get the JSON response
        result = {
            "success": True,
            "program_detailed_id": program_id_norm,
            "data": response.json(),
            "status_code": response.status_code
        }

        # Field mapping transformation removed
        logger.info(
            "Facet Extension Portal programme response",
            extra={"program_detailed_id": program_id_norm, "endpoint": "facet_extension_portal_programme", "status_code": result.get("status_code")},
        )
        # Cache successful responses only
        _CACHE.set("facet_extension_portal_programme", {"program_detailed_id": program_id_norm}, result)
        return result

    except requests.exceptions.RequestException as e:
        result = {
            "success": False,
            "program_detailed_id": program_id_norm,
            "error": str(e),
            "message": f"Failed to retrieve data for program details ID: {program_id_norm}"
        }

        # Field mapping transformation removed for error responses
        logger.warning(
            "Facet Extension Portal programme error",
            extra={"program_detailed_id": program_id_norm, "endpoint": "facet_extension_portal_programme", "error": result.get("error")},
        )
        # Do not cache errors
        return result


def get_group_model(claim_number: str) -> Dict[str, Any]:
    """
    Tool: Fetch group model information from network fee schedule API using PRPR_ID from claim summary.
          This function will first hit claim summary to extract PRPR_ID (provider ID),
          then use that to fetch the group model from the network fee schedule API.

    Args:
        claim_number: Claim ID to query (e.g., "25XG44660400")

    Returns:
        dict: Response containing:
            - success (bool): Whether the request was successful
            - status_code (int): HTTP status code
            - prpr_id (str): The provider ID extracted from claim summary
            - group_model (str): The group model value (e.g., "1A")
            - meta_data (str): Metadata about the request
            - error (str): Error message if failed
            - message (str): Detailed error message if failed
            - claim_number (str): The claim number queried
            - endpoint (str): The endpoint name
            - timestamp (int): Unix timestamp of request
    """
    # Normalize claim number
    claim_number = str(claim_number).strip()

    # Check cache
    cached = _CACHE.get("facet_ext_portal_group_model", {"claim_number": claim_number})
    if cached.hit and isinstance(cached.value, dict):
        logger.info("Cache hit", extra={"endpoint": "group_model"})
        return cached.value

    try:
        # Step 1: Get PRPR_ID from claim summary
        logger.info(
            "Fetching claim summary for group model",
            extra={"endpoint": "group_model"}
        )
        summary = get_claim_summary(claim_number)

        # Extract PRPR_ID from summary
        prpr_id = _find_first_key(summary, "PRPR_ID")
        if prpr_id is None:
            logger.error(
                "Could not extract PRPR_ID from claim summary",
                extra={"endpoint": "group_model"}
            )
            payload = {
                "success": False,
                "error": "Could not extract PRPR_ID from claim summary",
                "message": "Failed to find PRPR_ID in the claim summary response",
                "claim_number": claim_number,
                "prpr_id": None,
                "endpoint": "group_model",
                "timestamp": int(time.time()),
            }
            return payload

        prpr_id = str(prpr_id).strip()

        # Step 2: Fetch group model from network fee schedule API
        group_model_url = f"https://network-fee-sch-api-stg.hcck8s-ctc-np1.optum.com/common/checkModel/{prpr_id}"

        logger.info(
            "Calling network fee schedule API for group model",
            extra={"endpoint": "group_model"}
        )

        response = requests.get(group_model_url, timeout=30, verify=False)

        # Suppress SSL warnings
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        # Extract the plain text response (group model value like "1A")
        group_model_value = response.text.strip() if response.text else ""

        # Build response
        result = {
            "success": response.status_code == 200,
            "status_code": response.status_code,
            "meta_data": f"Group model lookup for provider {prpr_id}",
            "prpr_id": prpr_id,
            "group_model": group_model_value if response.status_code == 200 else None,
            "claim_number": claim_number,
            "endpoint": "group_model",
            "timestamp": int(time.time()),
        }

        if response.status_code == 200:
            logger.info(
                "Group model retrieved successfully",
                extra={
                    "endpoint": "group_model"
                }
            )
            # Cache successful responses only
            _CACHE.set("facet_ext_portal_group_model", {"claim_number": claim_number}, result)
        else:
            logger.warning(
                "Failed to retrieve group model",
                extra={
                    "status_code": response.status_code,
                    "endpoint": "group_model"
                }
            )
            result["error"] = f"API returned status {response.status_code}"
            result["message"] = "Failed to retrieve group model"

        return result

    except requests.exceptions.Timeout:
        logger.error(
            "Group model request timeout",
            extra={"claim_number": claim_number, "endpoint": "group_model"}
        )
        payload = {
            "success": False,
            "status_code": None,
            "error": "Request timeout",
            "message": "Network fee schedule API request timed out",
            "claim_number": claim_number,
            "prpr_id": None,
            "endpoint": "group_model",
            "timestamp": int(time.time()),
        }
        return payload

    except requests.exceptions.ConnectionError as e:
        logger.error(
            "Group model connection error",
            extra={"claim_number": claim_number, "error": str(e), "endpoint": "group_model"}
        )
        payload = {
            "success": False,
            "status_code": None,
            "error": str(e),
            "message": "Failed to connect to network fee schedule API",
            "claim_number": claim_number,
            "prpr_id": None,
            "endpoint": "group_model",
            "timestamp": int(time.time()),
        }
        return payload

    except Exception as e:
        logger.exception(
            "Unexpected error fetching group model",
            extra={"claim_number": claim_number, "error": str(e), "endpoint": "group_model"}
        )
        payload = {
            "success": False,
            "status_code": None,
            "error": str(e),
            "message": f"Unexpected error: {str(e)}",
            "claim_number": claim_number,
            "prpr_id": None,
            "endpoint": "group_model",
            "timestamp": int(time.time()),
        }
        return payload


# Tool 1: Provider-based data retrieval
facet_extension_portal_provider_tool = StructuredTool.from_function(
    func=fetch_data_facet_extension_portal_provider,
    name="facet_extension_portal_provider",
    description=(
        "Retrieves complete list information for a given Provider ID (PRPR ID) for all program_detailed_id(EDS_PRPR_PRGM_DET_ID)"
        "from the Facet Extension Portal system. Use this tool when you need to get comprehensive "
        "details for a particular provider id for all program_detailed_id corresponding to that particular provider ID."
    ),
    args_schema=ProviderInput,
    return_direct=False
)

# Tool 2: Programme-based data retrieval
facet_extension_portal_programme_tool = StructuredTool.from_function(
    func=fetch_data_facet_extension_portal_programme,
    name="facet_extension_portal_programme",
    description=(
        "Retrieves particular programme details for a given Program Detailed ID (EDS_PRPR_PRGM_DET_ID) "
        "from the Facet Extension Portal system. Use this tool when you need to get "
        "particular programme information using the Program Detailed ID."
    ),
    args_schema=ProgrammeInput,
    return_direct=False
)

# Tool 3: Group Model-based data retrieval
facet_ext_portal_group_model_tool = StructuredTool.from_function(
    func=get_group_model,
    name="facet_ext_portal_group_model",
    description=(
        "Retrieves group model information from the API using a claim number. "
        "This tool first extracts the provider ID (PRPR_ID) from the get_claim_summary function of facet,"
        "then fetches the group model value (e.g., '1A') from API based on the provider ID (PRPR_ID). "
        "Use this tool when you need to determine the group model for a specific claim's provider."
    ),
    args_schema=GroupModelInput,
    return_direct=False
)

# Create ToolNode with all tools
facet_extension_portal_tool_node = ToolNode([
    facet_extension_portal_provider_tool,
    facet_extension_portal_programme_tool,
    facet_ext_portal_group_model_tool
])
