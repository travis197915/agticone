"""
CBD API Client Module

This module handles API communication with the CBD (Covered Benefit Document) service.
"""

import os
import requests
import urllib3
from typing import List, Dict, Any, Optional
from tools.cbd_config import CBDConfig
from thynkr_bhagenticai.logging_utils import get_logger
from thynkr_bhagenticai.tool_cache import ToolCache

# Disable SSL warnings for corporate certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_logger = get_logger(__name__)
_cache = ToolCache()


class CBDAPIClient:
    """Client for interacting with the CBD API."""

    def __init__(self, api_url: Optional[str] = None, msid: Optional[str] = None):
        """
        Initialize CBD API Client.
        Args:
            api_url: API endpoint URL (defaults to CBD_API_URL env var or config)
            msid: MSID value (defaults to CBD_MSID env var or 'bhagai_stg')
        """
        self.api_url = api_url or os.getenv("CBD_API_URL", CBDConfig.API_URL)
        self.msid = msid or os.getenv("CBD_MSID", "bhagai_stg")  # Dynamic, from invocation or env
        self.timeout = CBDConfig.TIMEOUT
        self.verify_ssl = CBDConfig.VERIFY_SSL

    def fetch_token(self) -> str:
        """
        Fetch OAuth2 bearer token using client credentials grant.
        Reads CBD_TOKEN_URL, CBD_CLIENT_ID, CBD_CLIENT_SECRET from environment.
        Returns:
            Bearer token as string
        Raises:
            Exception if token cannot be fetched
        """
        token_url = os.getenv("CBD_TOKEN_URL")
        client_id = os.getenv("CBD_CLIENT_ID")
        client_secret = os.getenv("CBD_CLIENT_SECRET")
        if not token_url or not client_id or not client_secret:
            raise ValueError("OAuth2 credentials (CBD_TOKEN_URL, CBD_CLIENT_ID, CBD_CLIENT_SECRET) are required.")
        data = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        response = requests.post(token_url, data=data, verify=False)
        if response.status_code != 200:
            raise Exception(f"Failed to fetch token: {response.status_code} {response.text}")
        token_data = response.json()
        return token_data.get("access_token")

    def fetch_coverage_data(
        self,
        group_name: str = "Standard Medicare",
        plan_name: str = "Standard Medicare",
        cpt_codes: Optional[List[str]] = None,
        products: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Fetch coverage data from CBD API, with logging and caching.
        Args:
            group_name: Customer/group name
            plan_name: Plan name
            cpt_codes: Optional list of CPT codes to filter by
            products: Products list to pass to the API payload (e.g. ["Medicaid"], ["Commercial"]).
                      Defaults to CBDConfig.PRODUCTS when None.
        Returns:
            API response as dictionary
        Raises:
            Exception if token or API request fails
        """
        cache_key = {
            "group_name": group_name,
            "plan_name": plan_name,
            "products": tuple(sorted(products)) if products else tuple(CBDConfig.PRODUCTS),
            "cpt_codes": tuple(sorted([str(c).upper() for c in cpt_codes])) if cpt_codes else []
        }
        cached = _cache.get("cbd_coverage", cache_key)
        if cached.hit:
            _logger.info("CBD coverage cache hit")
            return cached.value

        _logger.info("CBD coverage API call")
        bearer_token = self.fetch_token()
        headers = {
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
            "User-Agent": "CBD-LangChain-Tool/1.0"
        }
        payload = CBDConfig.build_payload(
            msid=self.msid,
            group_name=group_name,
            plan_name=plan_name,
            products=products,
        )
        try:
            response = requests.post(
                self.api_url,
                json=payload,
                headers=headers,
                timeout=self.timeout,
                verify=self.verify_ssl
            )
            response.raise_for_status()
            data = response.json()
            _logger.info("CBD coverage API success")
            _cache.set("cbd_coverage", cache_key, data)
            return data
        except Exception as e:
            _logger.error(
                "CBD coverage API error: %s %s",
                type(e).__name__,
                e,
                extra={"error_type": type(e).__name__, "cache_key": cache_key},
                exc_info=True,
            )
            raise

    def filter_by_cpt_codes(self, data: Dict[str, Any], cpt_codes: List[str]) -> List[Dict[str, Any]]:
        """
        Filter response data by CPT codes (client-side filtering).

        Args:
            data: API response data
            cpt_codes: List of CPT codes to filter by

        Returns:
            Filtered list of coverage records
        """
        if not data or "data" not in data:
            return []

        filtered_results = []
        cpt_codes_upper = [code.upper().strip() for code in cpt_codes]

        for item in (data.get("data") or []):
            desc_code = item.get("descCode", "").upper().strip()
            if desc_code in cpt_codes_upper:
                filtered_results.append(item)

        return filtered_results
