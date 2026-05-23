"""
This module provides a structured LangChain tool for querying behavioral health claims
from the LINX API by subscriber ID, with OAuth2 token generation and response caching.
"""

import os
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from langchain_core.tools import StructuredTool

from config.settings import get_settings
from thynkr_bhagenticai.logging_utils import get_logger
from tools.schemas.schema_linx_tool import (
    ExternalAccountId,
    LinxClaimSearchInput,
    LinxClaimSearchOutput,
)

logger = get_logger(__name__)

# Token cache (in-memory, runtime-only)
_token_cache: Dict[str, Any] = {}


def _get_oauth_token() -> str:
    """
    Generate OAuth2 access token for LINX API.

    Caches token in memory until expiry (based on expires_in from token response).

    Returns:
        Access token string

    Raises:
        Exception: If token generation fails or credentials are missing
    """
    linx_auth_url = os.environ.get('LINX_AUTH_URL')
    linx_client_id = os.environ.get('LINX_CLIENT_ID')
    linx_client_secret = os.environ.get('LINX_CLIENT_SECRET')
    if not linx_auth_url:
        raise ValueError("LINX_AUTH_URL not configured in environment")
    if not linx_client_id:
        raise ValueError("LINX_CLIENT_ID not configured in environment")
    if not linx_client_secret:
        raise ValueError("LINX_CLIENT_SECRET not configured in environment")

    # Check in-memory cache
    cache_key = "linx_oauth_token"
    cached = _token_cache.get(cache_key)

    if cached:
        expires_at = cached.get("expires_at", 0)
        if time.time() < expires_at - 60:  # Refresh 60s before expiry
            logger.info("Using cached OAuth token")
            return cached["access_token"]

    # Generate new token
    logger.info("Generating new LINX OAuth token")

    try:
        with httpx.Client(timeout=30.0, verify=False) as client:
            response = client.post(
                linx_auth_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": linx_client_id,
                    "client_secret": linx_client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            token_data = response.json()
            access_token = token_data.get("access_token")
            expires_in = token_data.get("expires_in", 3600)
            if not access_token:
                raise ValueError("No access_token in OAuth response")
            _token_cache[cache_key] = {
                "access_token": access_token,
                "expires_at": time.time() + expires_in,
            }
            logger.info("OAuth token generated successfully")
            return access_token
    except httpx.HTTPStatusError as e:
        logger.error(f"OAuth token generation failed: HTTP {e.response.status_code}")
        raise Exception(f"Failed to generate OAuth token: {e}") from e
    except Exception as e:
        logger.error(f"OAuth token generation error: {e}")
        raise Exception(f"OAuth token generation failed: {e}") from e


def linx_claim_search_func(
    subscriber_id: str,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    dob: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    unet_policy_nbr: Optional[str] = None,
    claim_max_limit: Optional[int] = 0,
    external_account_id_list: Optional[list[ExternalAccountId]] = None,
) -> Dict[str, Any]:
    """
    Search for behavioral health claims in LINX API by subscriber ID.

    This function queries the LINX API with subscriber details and date range
    to retrieve claim information. Results are cached to improve performance.

    Args:
        subscriber_id: Subscriber ID to search for (required)
        first_name: First name of the subscriber (optional)
        last_name: Last name of the subscriber (optional)
        dob: Date of birth in MM/DD/YYYY format (optional)
        start_date: Claim search start date in MM/DD/YYYY format (optional)
        end_date: Claim search end date in MM/DD/YYYY format (optional)
        unet_policy_nbr: UNET policy number (optional)
        claim_max_limit: Maximum number of claims to retrieve (default: 0 = no limit)
        external_account_id_list: List of external account IDs (optional)

    Returns:
        Dictionary with claim search results or error details
    """
    settings = get_settings()
    # Try settings, fallback to os.environ
    linx_api_url = getattr(settings, 'linx_api_url', None) or os.environ.get('LINX_API_URL')
    if not linx_api_url:
        logger.error("LINX_API_URL not configured")
        linx_api_url = os.environ.get('LINX_API_URL')
        if not linx_api_url:
            logger.error("LINX_API_URL not configured in environment")
            return LinxClaimSearchOutput(
                success=False,
                error="LINX_API_URL not configured in environment",
            ).model_dump()
    cache_dir = Path(__file__).parent.parent.parent / "data" / "tool_cache" / "linx_claim_search"
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Create deterministic cache key
    cache_input = {
        "subscriber_id": subscriber_id,
        "first_name": first_name or "",
        "last_name": last_name or "",
        "dob": dob or "",
        "start_date": start_date or "",
        "end_date": end_date or "",
        "unet_policy_nbr": unet_policy_nbr or "",
        "claim_max_limit": claim_max_limit or 0,
        "external_account_id_list": [acc.model_dump() for acc in (external_account_id_list or [])],
    }
    cache_key = hashlib.sha256(json.dumps(cache_input, sort_keys=True).encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.json"
    # Check if cache exists and is valid (24 hour TTL)
    if cache_file.exists():
        cache_age = time.time() - cache_file.stat().st_mtime
        if cache_age < 86400:  # 24 hours
            logger.info("Returning cached LINX claim data")
            try:
                with cache_file.open("r", encoding="utf-8") as f:
                    cached_data = json.load(f)
                    cached_data["cache_hit"] = True
                    return cached_data
            except Exception as e:
                logger.warning(f"Cache read failed: {e}")
    # Make API call
    try:
        access_token = _get_oauth_token()

        # Use provided external_account_id_list or create default
        if external_account_id_list:
            account_list = [acc.model_dump() for acc in external_account_id_list]
        else:
            external_account = ExternalAccountId(
                facetsAltId="",
                riosAcctId="",
                unetPolicyNbr=unet_policy_nbr or "",
                cosmosGrpId="",
            )
            account_list = [external_account.model_dump()]

        request_body = {
            "externalAccountIdList": account_list,
            "subscriberId": subscriber_id,
        }
        # Add optional fields if provided
        if first_name:
            request_body["firstName"] = first_name
        if last_name:
            request_body["lastName"] = last_name
        if dob:
            request_body["dob"] = dob
        if start_date:
            request_body["startDate"] = start_date
        if end_date:
            request_body["endDate"] = end_date
        if claim_max_limit is not None:
            request_body["claimMaxLimit"] = claim_max_limit
        endpoint_url = linx_api_url
        logger.info(f"Calling LINX API: {endpoint_url}")
        with httpx.Client(timeout=60.0, verify=False) as client:
            response = client.post(
                endpoint_url,
                json=request_body,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "bhRequestHeader": json.dumps({
                        "applicationId": "obhagenticai",
                        "dataSource": "prod",
                        "cartItems": [],
                    }),
                },
            )
            response.raise_for_status()
            response_data = response.json()
        # Wrap list response in dict for schema compatibility
        if isinstance(response_data, list):
            response_data = {"results": response_data}
        output = LinxClaimSearchOutput(
            success=True,
            data=response_data,
            cache_hit=False,
        ).model_dump()
        try:
            with cache_file.open("w", encoding="utf-8") as f:
                json.dump(output, f, indent=2)
            logger.info("LINX claim data cached successfully")
        except Exception as e:
            logger.warning(f"Cache write failed: {e}")
        return output
    except httpx.HTTPStatusError as e:
        error_msg = f"LINX API error: HTTP {e.response.status_code}"
        logger.error(error_msg)
        return LinxClaimSearchOutput(
            success=False,
            error=error_msg,
        ).model_dump()
    except Exception as e:
        error_msg = f"LINX claim search failed: {str(e)}"
        logger.error(error_msg)
        return LinxClaimSearchOutput(
            success=False,
            error=error_msg,
        ).model_dump()


# Create LangChain tool
linx_claim_search_tool = StructuredTool.from_function(
    func=linx_claim_search_func,
    name="linx_claim_search",
    description=(
        "Search for behavioral health claims in LINX API by subscriber ID. "
        "Retrieves claim details for a subscriber within a specified date range. "
        "Requires subscriber ID, first name, last name, date of birth, and date range. "
        "Results are cached for 24 hours to improve performance."
    ),
    args_schema=LinxClaimSearchInput,
    return_direct=False,
)
