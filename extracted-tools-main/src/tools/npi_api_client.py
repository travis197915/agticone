"""
CMS NPI Registry API Client
Query the CMS NPI Registry by NPI number or name with caching and rate limiting.
"""

import os
import re
from typing import Dict, List, Optional, Any

import requests
from ratelimit import limits, sleep_and_retry
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class NPIRegistryClient:
    """
    Client for querying the CMS NPI Registry API (version 2.1).

    Features:
    - Query by NPI number or provider name
    - Rate limiting to comply with CMS requirements
    - Normalized JSON output with structured data
    (All caching is handled at the tool layer via ToolCache.)
    """

    # Defaults resolved at class-load time; may be overridden by Key Vault
    # secrets injected later. __init__ re-reads os.environ so KV values win.
    BASE_URL: Optional[str] = os.getenv("NPI_API_BASE_URL")
    API_VERSION: Optional[str] = os.getenv("NPI_API_VERSION")
    CALLS_PER_HOUR: int = int(os.getenv("NPI_RATE_LIMIT_CALLS", "900"))
    RATE_LIMIT_PERIOD: int = int(os.getenv("NPI_RATE_LIMIT_PERIOD", "60"))

    def __init__(
        self,
        rate_limit_calls: Optional[int] = None,
        rate_limit_period: Optional[int] = None,
    ):
        """
        Initialize the NPI Registry client.

        Args:
            rate_limit_calls: Maximum API calls allowed per period (default from env).
            rate_limit_period: Time period for rate limiting in seconds (default from env).
        """
        # Re-read at init time so Key Vault secrets (loaded after class def) are picked up.
        self.BASE_URL = os.getenv("NPI_API_BASE_URL") or type(self).BASE_URL
        self.API_VERSION = os.getenv("NPI_API_VERSION") or type(self).API_VERSION
        if not self.BASE_URL:
            raise ValueError("NPI_API_BASE_URL is not configured")
        if not self.API_VERSION:
            raise ValueError("NPI_API_VERSION is not configured")

        self.rate_limit_calls = rate_limit_calls or int(
            os.getenv("NPI_RATE_LIMIT_CALLS", "900")
        )
        self.rate_limit_period = rate_limit_period or int(
            os.getenv("NPI_RATE_LIMIT_PERIOD", "60")
        )
        if self.rate_limit_calls <= 0 or self.rate_limit_period <= 0:
            raise ValueError("Rate limit calls and period must be positive integers")

        self.session = requests.Session()
        user_agent = os.getenv("NPI_USER_AGENT", "thynkr-bhagenticai/npi-client")
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "application/json",
        })

    @staticmethod
    def validate_npi(npi: str) -> bool:
        """
        Validate NPI number format (10 digits).

        Args:
            npi: NPI number to validate

        Returns:
            True if valid, False otherwise
        """
        if not npi or not isinstance(npi, str):
            return False
        # Remove any whitespace
        npi = npi.strip()
        # Check if it's exactly 10 digits
        return bool(re.match(r'^\d{10}$', npi))

    @sleep_and_retry
    @limits(calls=CALLS_PER_HOUR, period=RATE_LIMIT_PERIOD)
    def _make_request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Make rate-limited API request to CMS NPI Registry.

        Args:
            params: Query parameters for the API request

        Returns:
            API response as dictionary

        Raises:
            requests.RequestException: If the API request fails
        """
        params['version'] = self.API_VERSION

        try:
            response = self.session.get(self.BASE_URL, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            raise Exception(f"API request failed: {str(e)}")

    def _format_address(self, address: Dict[str, Any]) -> Dict[str, str]:
        """Format address data from API response."""
        return {
            "address_line_1": address.get("address_1", ""),
            "address_line_2": address.get("address_2", ""),
            "city": address.get("city", ""),
            "state": address.get("state", ""),
            "postal_code": address.get("postal_code", ""),
            "country_code": address.get("country_code", ""),
            "country_name": address.get("country_name", ""),
            "telephone_number": address.get("telephone_number", ""),
            "fax_number": address.get("fax_number", "")
        }

    def _format_taxonomy(self, taxonomy: Dict[str, Any]) -> Dict[str, Any]:
        """Format taxonomy data from API response."""
        return {
            "code": taxonomy.get("code", ""),
            "description": taxonomy.get("desc", ""),
            "state": taxonomy.get("state", ""),
            "license": taxonomy.get("license", ""),
            "primary": taxonomy.get("primary", False)
        }

    def _normalize_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize API result into structured format.

        Args:
            result: Raw result from API response

        Returns:
            Normalized result dictionary
        """
        basic = result.get("basic", {})

        # Extract provider information
        provider_info = {
            "npi": result.get("number", ""),
            "enumeration_type": result.get("enumeration_type", ""),
            "status": basic.get("status", "")
        }

        # Individual provider (Type 1)
        if result.get("enumeration_type") == "NPI-1":
            provider_info.update({
                "first_name": basic.get("first_name", ""),
                "last_name": basic.get("last_name", ""),
                "credential": basic.get("credential", ""),
                "organization_name": None
            })
        # Organizational provider (Type 2)
        else:
            provider_info.update({
                "first_name": None,
                "last_name": None,
                "credential": None,
                "organization_name": basic.get("organization_name", "")
            })

        # Extract addresses
        addresses = result.get("addresses", [])
        practice_address = None
        mailing_address = None

        for addr in addresses:
            formatted = self._format_address(addr)
            if addr.get("address_purpose") == "LOCATION":
                practice_address = formatted
            elif addr.get("address_purpose") == "MAILING":
                mailing_address = formatted

        # Extract taxonomies
        taxonomies = result.get("taxonomies", [])
        primary_taxonomy = None
        other_taxonomies = []

        for tax in taxonomies:
            formatted = self._format_taxonomy(tax)
            if tax.get("primary"):
                primary_taxonomy = formatted
            else:
                other_taxonomies.append(formatted)

        # Extract other identifiers
        identifiers = []
        for identifier in result.get("identifiers", []):
            identifiers.append({
                "identifier": identifier.get("identifier", ""),
                "type": identifier.get("desc", ""),
                "state": identifier.get("state", "")
            })

        return {
            "provider": provider_info,
            "practice_address": practice_address,
            "mailing_address": mailing_address,
            "primary_taxonomy": primary_taxonomy,
            "other_taxonomies": other_taxonomies,
            "identifiers": identifiers
        }

    def query_by_npi(self, npi: str) -> Dict[str, Any]:
        """
        Query NPI Registry by NPI number.

        Args:
            npi: 10-digit NPI number

        Returns:
            Normalized provider information dictionary

        Raises:
            ValueError: If NPI format is invalid
            Exception: If API request fails or NPI not found
        """
        # Validate NPI format
        npi = npi.strip()
        if not self.validate_npi(npi):
            raise ValueError(
                f"Invalid NPI format: '{npi}'. NPI must be a 10-digit number."
            )

        # Make API request
        params = {"number": npi}
        response = self._make_request(params)

        # Check if results found
        result_count = response.get("result_count", 0)
        results = response.get("results", [])
        if result_count == 0 or not results:
            return {
                "npi": npi,
                "found": False,
                "message": f"NPI {npi} not found in CMS registry",
            }

        # Normalize result
        normalized = self._normalize_result(results[0])

        return normalized

    def query_by_name(
        self,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        organization_name: Optional[str] = None,
        city: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Query NPI Registry by provider name.

        Args:
            first_name: Provider first name (for individuals)
            last_name: Provider last name (for individuals)
            organization_name: Organization name (for organizations)
            city: City filter
            state: State filter (2-letter abbreviation)
            limit: Maximum number of results to return (1-200, default: 10)

        Returns:
            List of normalized provider information dictionaries

        Raises:
            ValueError: If no search criteria provided
            Exception: If API request fails
        """
        # Validate search criteria
        if not any([first_name, last_name, organization_name]):
            raise ValueError(
                "At least one of first_name, last_name, or organization_name must be provided"
            )

        # Build API parameters
        params = {"limit": min(max(limit, 1), 200)}

        if first_name:
            params["first_name"] = first_name
        if last_name:
            params["last_name"] = last_name
        if organization_name:
            params["organization_name"] = organization_name
        if city:
            params["city"] = city
        if state:
            params["state"] = state.upper()

        # Make API request
        response = self._make_request(params)

        # Check if results found
        result_count = response.get("result_count", 0)
        if result_count == 0:
            return []

        # Normalize all results
        results = response.get("results", [])
        normalized_results = [self._normalize_result(result) for result in results]

        return normalized_results
