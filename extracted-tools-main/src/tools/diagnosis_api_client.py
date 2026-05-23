"""
API Client for Covered Diagnosis Service

This module provides a client for querying the Covered Diagnosis API.
"""
import os
from typing import Dict, Any, Optional
import httpx
import requests
from dotenv import load_dotenv
from pathlib import Path
from thynkr_bhagenticai.logging_utils import get_logger

logger = get_logger(__name__)

_repo_root = Path(__file__).resolve().parent.parent
load_dotenv(_repo_root / ".env")


class DiagnosisAPIClient:
    """Client for Covered Diagnosis API."""

    BASE_URL = os.getenv("DIAGNOSIS_API_URL", "")
    def __init__(self):
        """Initialize the diagnosis API client."""
        self.timeout = 30.0
        self.verify_ssl = False

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

    def fetch_diagnosis_info(self, diagnosis_code: str) -> Optional[Dict[str, Any]]:
        """
        Fetch covered diagnosis information for a diagnosis code.

        Args:
            diagnosis_code: The diagnosis code to query (e.g., ICD-10 code)

        Returns:
            Dictionary with diagnosis information, or None if not found
        """
        bearer_token = self.fetch_token()

        headers = {
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
        }

        payload = {
            "globalFilter": "",
            "filters": [
                {
                    "id": "code",
                    "value": [
                        {
                            "condition": "equals",
                            "filterValue": diagnosis_code
                        }
                    ]
                }
            ]
        }

        try:
            with httpx.Client(timeout=self.timeout, verify=self.verify_ssl) as client:
                response = client.post(
                    self.BASE_URL,
                    json=payload,
                    headers=headers
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error fetching diagnosis info: {e.response.status_code} - {e.response.text}")
            return None
        except Exception as e:
            logger.error(f"Error fetching diagnosis info: {e}")
            return None
