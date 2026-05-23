"""
CBD API Configuration

This module contains default configuration values for the CBD API payload.
MSID is not hardcoded - it should be passed during invocation.
"""
import os
from dotenv import load_dotenv
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent
load_dotenv(_repo_root / ".env")

class CBDConfig:
    """Configuration for CBD API requests."""
    CBD_MSID = "bhagai_stg"
    # API Endpoint
    API_URL = os.getenv("CBD_CONFIG_API_URL", "")
    # LOBs (Lines of Business) - From config
    LOBS = ["Employer", "Payer", "UHC E&I", "UHC C&S", "UHC M&R"]

    # Markets
    MARKETS = ["All"]

    # Products
    PRODUCTS = ["Commercial"]

    # Default Customer/Group Name
    DEFAULT_CUSTOMER = "Standard Commercial"

    # Default Plan Name
    DEFAULT_PLAN = "Standard Commercial"

    # Advanced Filters Structure
    ADVANCED_FILTERS = {
        "globalFilter": "",
        "filters": [
            {
                "id": "",
                "value": [
                    {
                        "condition": "",
                        "filterValue": ""
                    }
                ]
            }
        ]
    }


    # Request Settings
    TIMEOUT = int(os.getenv("CBD_TIMEOUT", "30"))
    VERIFY_SSL = os.getenv("CBD_VERIFY_SSL", "false").lower() == "true"

    @classmethod
    def build_payload(cls, msid: str, group_name: str = None, plan_name: str = None, products: list = None):
        """
        Build the API payload with correct structure.

        Args:
            msid: MSID value (required, passed during invocation)
            group_name: Customer/group name (defaults to DEFAULT_CUSTOMER)
            plan_name: Plan name (defaults to DEFAULT_PLAN)
            products: List of products to query (defaults to cls.PRODUCTS = ["Commercial"])

        Returns:
            dict: Complete API payload
        """
        # Use defaults if not provided or empty
        if not group_name or not group_name.strip():
            group_name = cls.DEFAULT_CUSTOMER
        if not plan_name or not plan_name.strip():
            plan_name = cls.DEFAULT_PLAN

        return {
            "msid": msid, # Dynamic - passed during invocation, NOT hardcoded
            "lobs": cls.LOBS,
            "markets": cls.MARKETS,
            "products": products if products is not None else cls.PRODUCTS,
            "custNames": [group_name],
            "plans": [plan_name],
            "advancedFilters": cls.ADVANCED_FILTERS
        }
