"""
Medicare Provider Opt-Out Status Checker - Langchain Tool

Professional Langchain tool for checking Medicare provider opt-out status.
Returns opt-out dates and current status in clean JSON format.

Data Source: CMS Provider Opt-Out Affidavits Dataset
API: https://data.cms.gov/tools/provider-opt-out-affidavits-look-up-tool

Author: Healthcare Integration Team
Version: 1.0.0
"""

import os
import json
import requests
from typing import Optional
from dotenv import load_dotenv  # type: ignore
from langchain_core.tools import StructuredTool

from tools.schemas.schema_optout import ProviderOptOutInput, ProviderRecord, APIResponse

# Load environment variables from .env file
load_dotenv()


class MedicareOptOutChecker:
    """
    Professional API client for Medicare provider opt-out verification.

    Handles CMS Provider Opt-Out Affidavits API interactions with
    comprehensive validation and error handling.
    """

    def __init__(self):
        # Load configuration from environment variables (.env file)
        self.base_url = os.getenv('CMS_API_BASE_URL')
        self.dataset_id = os.getenv('CMS_DATASET_ID')
        self.timeout = int(os.getenv('CMS_API_TIMEOUT', '30'))

        # Validate required environment variables
        if not self.base_url or not self.dataset_id:
            raise ValueError("Missing required environment variables. Please check your .env file.")

        self.api_url = f"{self.base_url}/{self.dataset_id}/data"

    def search_provider(
        self,
        npi: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        state: Optional[str] = None,
        specialty: Optional[str] = None
    ) -> APIResponse:
        """
        Search for Medicare provider opt-out status.

        Args:
            npi: 10-digit National Provider Identifier
            first_name: Provider's first name
            last_name: Provider's last name
            state: 2-letter state code
            specialty: Provider specialty

        Returns:
            APIResponse: Structured response with results or error
        """
        try:
            # Build and execute API request
            params = self._build_api_params(npi, first_name, last_name, state, specialty)
            response = requests.get(self.api_url, params=params, timeout=self.timeout)
            response.raise_for_status()

            # Parse and format results
            data = response.json()
            if not isinstance(data, list) or not data:
                return APIResponse(
                    success=True,
                    message="No opt-out record found. This provider has not opted out of Medicare or has an expired opt-out."
                )

            records = [ProviderRecord.from_api_response(record) for record in data]
            return APIResponse(
                success=True,
                records=records,
                total_count=len(records),
                message=f"Found {len(records)} record(s)"
            )

        except requests.exceptions.Timeout:
            return APIResponse(success=False, error="API request timed out. Please try again.")
        except requests.exceptions.ConnectionError:
            return APIResponse(success=False, error="Failed to connect to CMS API. Check your internet connection.")
        except requests.exceptions.HTTPError as e:
            return APIResponse(success=False, error=f"API request failed: {str(e)}")
        except Exception as e:
            return APIResponse(success=False, error=f"Unexpected error: {str(e)}")

    def _build_api_params(
        self,
        npi: Optional[str],
        first_name: Optional[str],
        last_name: Optional[str],
        state: Optional[str],
        specialty: Optional[str]
    ) -> dict:
        """Build API request parameters based on search criteria."""
        params = {}

        if npi:
            params['filter[NPI]'] = npi
        else:
            filter_idx = 1
            for field, value in [
                ('First Name', first_name),
                ('Last Name', last_name),
                ('State Code', state),
                ('Specialty', specialty)
            ]:
                if value:
                    operator = '=' if field == 'State Code' else 'CONTAINS'
                    params[f'filter[filter-{filter_idx}][condition][path]'] = field
                    params[f'filter[filter-{filter_idx}][condition][operator]'] = operator
                    params[f'filter[filter-{filter_idx}][condition][value]'] = value
                    filter_idx += 1

        return params


def check_medicare_optout_status(
    npi: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    state: Optional[str] = None,
    specialty: Optional[str] = None
) -> str:
    """
    Check Medicare provider opt-out status.

    Args:
        npi: 10-digit National Provider Identifier
        first_name: Provider's first name
        last_name: Provider's last name
        state: 2-letter state code
        specialty: Provider specialty

    Returns:
        JSON string with provider records or error message
    """
    checker = MedicareOptOutChecker()
    result = checker.search_provider(npi, first_name, last_name, state, specialty)
    return json.dumps(result.to_json_output(), indent=2)


# Create Langchain tool
medicare_optout_tool = StructuredTool.from_function(
    func=check_medicare_optout_status,
    name="medicare_optout_checker",
    description=(
        "Check if a healthcare provider has opted out of Medicare. "
        "Search by NPI (10-digit number) or provider name (first and last name required). "
        "Returns provider information, opt-out effective dates, end dates, renewal information, "
        "and current opt-out status (Yes/No). "
        "Can filter results by state (2-letter code) or specialty for better accuracy. "
        "If no record exists, indicates the provider is participating in Medicare."
    ),
    args_schema=ProviderOptOutInput,
    return_direct=False,
    handle_tool_error=True,
    handle_validation_error=True
)
