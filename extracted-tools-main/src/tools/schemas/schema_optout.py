"""
Pydantic Data Models for Medicare Opt-Out Checker Langchain Tool

Handles data validation, type conversion, and business logic for provider opt-out records.

Author: Healthcare Integration Team
Version: 1.0.0
"""

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, List
from datetime import datetime


class ProviderOptOutInput(BaseModel):
    """Input schema for Medicare opt-out checker tool"""

    npi: Optional[str] = Field(
        default=None,
        description="10-digit National Provider Identifier (NPI) number"
    )
    first_name: Optional[str] = Field(
        default=None,
        description="Provider's first name (required if not using NPI)"
    )
    last_name: Optional[str] = Field(
        default=None,
        description="Provider's last name (required if not using NPI)"
    )
    state: Optional[str] = Field(
        default=None,
        description="2-letter state code for filtering results (e.g., CA, NY, TX)"
    )
    specialty: Optional[str] = Field(
        default=None,
        description="Provider specialty for filtering results (e.g., Cardiology, Internal Medicine)"
    )

    @field_validator('npi')
    @classmethod
    def validate_npi(cls, v):
        """Validate NPI is 10 digits"""
        if v:
            v = str(v).strip()
            if not v:
                return None
            if not v.isdigit():
                raise ValueError('NPI must contain only digits')
            if len(v) != 10:
                raise ValueError(f'NPI must be exactly 10 digits')
        return v

    @field_validator('state')
    @classmethod
    def validate_state(cls, v):
        """Validate state code is 2 letters"""
        if v:
            v = v.strip().upper()
            if not v:
                return None
            if not v.isalpha():
                raise ValueError('State code must contain only letters')
            if len(v) != 2:
                raise ValueError('State code must be exactly 2 letters')
        return v

    @field_validator('first_name', 'last_name', 'specialty')
    @classmethod
    def normalize_text(cls, v):
        """Convert text to uppercase and validate"""
        if v:
            v = v.strip()
            if not v:
                return None
            if not all(c.isalpha() or c.isspace() or c in "'-." for c in v):
                raise ValueError('Name can only contain letters, spaces, hyphens, apostrophes, and periods')
            return v.upper() if v else v

    @model_validator(mode='after')
    def check_required_fields(self):
        """Validate that at least NPI or last name is provided"""
        if not self.npi and not self.last_name:
            raise ValueError('Either NPI or last name must be provided to search for providers')
        return self


class ProviderRecord(BaseModel):
    """Provider opt-out record with essential fields only"""

    provider_name: str = Field(description="Full name of the provider")
    npi: str = Field(description="10-digit National Provider Identifier")
    optout_effective_date: str = Field(description="Date when opt-out period begins")
    optout_end_date: str = Field(description="Date when opt-out period ends")
    optout_status: str = Field(description="Current opt-out status: Yes, No (Expired), or Unknown")
    renewal_info: str = Field(description="Last updated/renewal date")

    @classmethod
    def from_api_response(cls, record: dict) -> 'ProviderRecord':
        """
        Create ProviderRecord from CMS API response.

        Args:
            record: Raw API response dictionary

        Returns:
            ProviderRecord: Formatted provider record
        """
        first_name = record.get('First Name', 'N/A')
        last_name = record.get('Last Name', 'N/A')
        npi = str(record.get('NPI', 'N/A'))
        optout_effective_date = record.get('Optout Effective Date', 'N/A')
        optout_end_date = record.get('Optout End Date', 'N/A')
        renewal_info = record.get('Last updated', 'N/A')

        # Determine current opt-out status
        optout_status = cls._check_optout_status(optout_end_date)

        return cls(
            provider_name=f"{first_name} {last_name}",
            npi=npi,
            optout_effective_date=optout_effective_date,
            optout_end_date=optout_end_date,
            optout_status=optout_status,
            renewal_info=renewal_info
        )

    @staticmethod
    def _check_optout_status(optout_end_date: str) -> str:
        """
        Determine current opt-out status based on end date.

        Args:
            optout_end_date: End date in MM/DD/YYYY format

        Returns:
            str: "Yes" if opted out, "No (Expired)" if expired, "Unknown" if invalid
        """
        try:
            if not optout_end_date or optout_end_date == 'N/A':
                return "Unknown"

            end_date = datetime.strptime(optout_end_date, '%m/%d/%Y')
            return "Yes" if datetime.now() <= end_date else "No (Expired)"
        except (ValueError, TypeError):
            return "Unknown"

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        return {
            'provider_name': self.provider_name,
            'npi': self.npi,
            'optout_effective_date': self.optout_effective_date,
            'optout_end_date': self.optout_end_date,
            'optout_status': self.optout_status,
            'renewal_info': self.renewal_info
        }


class APIResponse(BaseModel):
    """API response wrapper with metadata"""

    success: bool = Field(description="Whether the API call was successful")
    records: List[ProviderRecord] = Field(default_factory=list, description="List of provider records")
    total_count: int = Field(default=0, description="Number of records found")
    message: Optional[str] = Field(default=None, description="Success or informational message")
    error: Optional[str] = Field(default=None, description="Error message if request failed")

    def to_json_output(self) -> dict:
        """Convert to clean JSON output format."""
        if not self.success:
            return {"error": self.error}

        if self.total_count == 0:
            return {"message": "No opt-out record found", "records": []}

        return [record.to_dict() for record in self.records]
