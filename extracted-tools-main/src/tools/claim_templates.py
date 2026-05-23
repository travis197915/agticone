"""Claim field templates.

These templates define canonical *flat* key lists for LLM extraction.
The LLM is instructed to return exactly these keys, using `null` when a
value is missing.

This keeps a stable payload shape even when the underlying claim text
leaves boxes empty.
"""

from __future__ import annotations

from typing import Dict, List


# Canonical flat keys for the "EMC CLAIM FORM - MEDICAL" ASCII layout.
#
# Notes:
# - Keys are intentionally human-readable and stable.
# - This is a "flat" template; repeated/service-line fields should be handled
#   by the structured parser (`line_items`) when you need per-line repetition.
# - When using the flat-template tool, missing keys MUST be returned as null.
EMC_CLAIM_MEDICAL_FLAT_KEYS: List[str] = [
    # Header-ish identifiers
    "FLN/DCC",
    "ID#",
    "SRC IND",
    "VENDOR ID",
    "FORM",
    "DATE",

    # Box 1
    "1 INSUREDS ID NUMBER",
    "HIC#",
    "RTE",
    "ATTCH",
    "KEYER",
    "DT",

    # Box 2-4
    "2 PATIENTS NAME (LFM)",
    "3 PAT DOB",
    "3 PAT SEX",
    "4 INSUREDS NAME (LFM)",
    "4 INS DOB",
    "4 INS SEX",

    # Box 5-8
    "5 PATIENTS ADDRESS, CITY, STATE, ZIP CODE",
    "5 PHONE#",
    "6 PAT RELATION TO INSURED",
    "7 INSUREDS ADDRESS, CITY, STATE, ZIP CODE",
    "7 PHONE#",
    "8 PATIENT STAT MARITAL",
    "8 EMPLOYMENT",
    "8 STUDENT",
    "8 DECEASED",

    # Box 7A-7D
    "7A EMPLOYERS NAME OR SCHOOL NAME, ADDRESS, EMPLOYEE ID",
    "7A EMPLOYMENT STATUS CODE",
    "7A ID#",
    "7B INSUREDS POLICY, GROUP OR FECA# AND NAME",
    "7B SPEC PRG IND",
    "7C INSURANCE PLAN OR PROGRAM NAME",
    "7C PAYOR ID",
    "7C SPC",
    "7D IS THERE ANOTHER HEALTH BENEFIT PLAN?",

    # Box 9-9D
    "9 OTHER INSUREDS NAME (LFM)",
    "9 DOB",
    "9 SEX",
    "9 INSURED ID#",
    "9 PATIENT RELATIONSHIP TO INSURED",
    "9 PHONE#",
    "9A OTHER INSUREDS ADDRESS, CITY, STATE, ZIP CODE",
    "9A PHONE#",
    "9B OTHER INSUREDS POLICY OR GROUP# AND NAME",
    "9B SPEC PRG IND",
    "9C INSURANCE PLAN OR PROGRAM NAME, PAYOR ID AND SPC",
    "9D EMPLOYERS NAME OR SCHOOL NAME, ADDRESS, EMPLOYEE ID",
    "9D EMPLOYMENT STATUS CODE",
    "9D ID#",

    # Box 10-11
    "10A EMPLOYMENT?",
    "10B AUTO/OTHER ACCIDENT?",
    "10 PLACE/ST",
    "10 DATE",
    "10 HOUR",
    "11 BILLING PROVIDER NAME",
    "11 PARTICIPATING PHYS",
    "11 PHONE#",
    "11 TIN#",
    "11 EIN/SSN",
    "11 UPIN#",
    "11 NPI",
    "11 MDCR#",
    "11 MDCD#",

    # Box 12-13
    "12 PATIENTS OR AUTHORIZED PERSONS SIGNATURE",
    "12A RESERVED",
    "13 INSUREDS OR AUTHORIZED PERSONS SIGNATURE",
    "13 BENEFITS ASSIGNED TO PROVIDER/PAY PROVIDER",

    # Box 14-16
    "14 DATE OF CURRENT ILLNESS/FIRST SYMPTOM/INJURY/ACCIDENT/PREGNANCY-LMP",
    "14 SAME SYMPTOM IND",
    "14 FIRST SYMPTOM IND",
    "15 SAME/SIMILAR ILLNESS DATE",
    "16 TOTAL DISABILITY FROM",
    "16 TOTAL DISABILITY TO",
    "16 PARTIAL DISABILITY FROM",
    "16 PARTIAL DISABILITY TO",
    "16 DATE RETURNED TO WORK",

    # Box 17-20
    "17 PHYSICIAN (REF/ORD/SUP)",
    "17 NPI",
    "17A ID# [REF PHY]",
    "18 HOSPITALIZATION FROM",
    "18 HOSPITALIZATION TO",
    "19 REF PHY UPIN",
    "19 REF PHY TAX ID & TYPE",
    "19 ADDL INFO",
    "20 OUTSIDE LAB?",
    "20 $CHARGE",

    # Box 21-23
    "21 ICD VERSION",
    "21 1A",
    "21 2B",
    "21 3C",
    "21 4D",
    "21 E",
    "21 F",
    "21 G",
    "21 H",
    "21 I",
    "21 J",
    "21 K",
    "21 L",
    "21A REMARKS",
    "22 MEDICAID RESUBMISSION CODE",
    "22 ORIGINAL REF. NO.",
    "23 PRIOR AUTHORIZATION NUMBER",

    # Box 24 (flat summary of headings; per-line values belong in structured line_items)
    "24 DATES OF SERVICE",
    "24 PLC/SV",
    "24 TYPE/SV",
    "24 CPT/HCPCS",
    "24 MOD",
    "24 DIAG CODE",
    "24 $CHARGES",
    "24 DAY/UNITS",
    "24 ANESTHESIA TIME",
    "24 EMG IND",
    "24 OTHER INS ALLOWED",
    "24 NEGOTIATED RATE REDUC IND",
    "24 DEDUCTIBLE AMOUNT",
    "24M LINE INFO",
    "24M SERVICE LINE #",
    "24M CLIA #",

    # Box 25-30A
    "25 FEDERAL TAX ID#",
    "25 SSN/EIN",
    "26 PATIENTS ACCOUNT#",
    "27 PROV ACCPTS ASGNMT",
    "27A MEDICARE VOUCHER",
    "28 TOT CHARGE",
    "29 TOT PAT PD",
    "30 TOTAL OTHER INSURANCE",
    "30 PD",
    "30 ALW",
    "30A PAT RESP",
    "30A COPAY",
    "30A COINS",
    "30A DEDUCT",
    "30A WRITEOFF",

    # Box 31-34
    "31 SIGNATURE OF SERVICING PHYSICIAN/SUPPLIER (FL)",
    "31 DEGREES OR CREDENTIALS",
    "31 TAXONOMY",
    "31 CORPORATE PROVIDER TYPE",
    "31 ST LICENSE#",
    "32 NAME AND ADDRESS OF FACILITY WHERE SERVICES WERE RENDERED",
    "32 NPI",
    "33 SERVICING PHYSICIAN/SUPPLIER NAME, ADDRESS, PHONE",
    "33 NPI",
    "34 PAY-TO PROVIDER NPI",

    # S1-S3
    "S1 OTHER INSURED2 INFORMATION NAME (LFM)",
    "S1 DOB",
    "S1 SEX",
    "S1 ID#",
    "S1 PATIENT RELATION TO INSURED",
    "S1 ADDRESS1",
    "S1 ADDRESS2",
    "S1 CITY, STATE, ZIP CODE",
    "S1 PHONE",
    "S1 EMPLOYMENT STATUS CODE",
    "S1 EMPLOYERS/SCHOOL NAME",
    "S1 EMPLOYEE ID",
    "S2 OTHER PAYOR2 INFORMATION INSURED2 POLICY/GROUP#",
    "S2 POLICY/GROUP NAME",
    "S2 SPEC PRG IND",
    "S2 INSURANCE PLAN/PROGRAM NAME",
    "S2 PAYOR2 ID AND SPC",
    "S3 CLAIM TAX AMOUNT",
    "S3 PROVIDER DISCOUNT AMOUNT",

    # Pricing information
    "PRICING INFORMATION TPO ID",
    "PRICING INFORMATION TPO REFERENCE#",
    "PRICING INFORMATION REJ MESSAGE IND",
    "PRICING INFORMATION AUTHORIZATION NUMBER",
    "PRICING INFORMATION PRICE MTHOD",
    "PRICING INFORMATION ALLOWED AMOUNT",
    "PRICING INFORMATION CHANGE IND",
    "PRICING INFORMATION PROCEDURE CODE",
    "PRICING INFORMATION APPRV UNITS",
    "PRICING INFORMATION RESERVED",
    "PRICING INFORMATION FREE FORM REMARKS",
]

TEMPLATES: Dict[str, List[str]] = {
    "emc_medical": EMC_CLAIM_MEDICAL_FLAT_KEYS,
}


def get_template_keys(template_name: str) -> List[str]:
    name = (template_name or "").strip().lower()
    if not name:
        name = "emc_medical"
    if name not in TEMPLATES:
        raise KeyError(f"Unknown template_name={template_name!r}. Known: {sorted(TEMPLATES.keys())}")
    return list(TEMPLATES[name])
