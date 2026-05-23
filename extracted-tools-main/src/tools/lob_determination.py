"""
LOB (Line of Business) Determination Tool

Determines the Line of Business for a claim by:
1. Calling Facets Summary API to get GRGR_NAME + PDDS_DESC.
2. Quick-check on the combined text (GRGR_NAME + PDDS_DESC):
   - Contains "Medicare" -> LOB = "Medicare"
   - Contains "Medicaid" -> LOB = "Medicaid"
3. Fallback: call CBD fetchCustomerInfo API, similarity-match
   ``GRGR_NAME + " " + PDDS_DESC`` against each ``product``.
4. Classify the best CBD match:
   - Contains "Medicare" -> "Medicare"
   - Contains "Medicaid" -> "Medicaid"
   - Exchange / other / no match -> "Commercial"
5. Default to "Commercial".
"""

import os
from typing import Any, Dict, List, Optional, Tuple

import httpx

from thynkr_bhagenticai.logging_utils import get_logger
from thynkr_bhagenticai.tool_cache import ToolCache

from tools.facets_tool import get_claim_summary
from tools.cbd_tool import match_group_or_plan

logger = get_logger(__name__)
_CACHE = ToolCache()

# LOB constants
LOB_MEDICARE = "Medicare"
LOB_MEDICAID = "Medicaid"
LOB_COMMERCIAL = "Commercial"

# Keywords for LOB classification (checked in order; first match wins)
_LOB_KEYWORD_MAP = (
    ("medicare", LOB_MEDICARE),
    ("medicaid", LOB_MEDICAID),
)
_EXCHANGE_KEYWORDS = ("exchange",)


def _extract_facets_fields(summary: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract GRGR_NAME and PDDS_DESC from a Facets claim summary response.

    The response envelope is:
        body -> Data -> ClaimSummary -> REC_CIV8 -> {GRGR_NAME, PDDS_DESC}

    Returns:
        Tuple of (grgr_name, pdds_desc), either may be None.
    """
    body = summary.get("body")
    if not isinstance(body, dict):
        return None, None

    data = body.get("Data")
    if not isinstance(data, dict):
        return None, None

    claim_summary = data.get("ClaimSummary")
    if not isinstance(claim_summary, dict):
        return None, None

    rec = claim_summary.get("REC_CIV8")
    if not isinstance(rec, dict):
        return None, None

    grgr_name = rec.get("GRGR_NAME")
    pdds_desc = rec.get("PDDS_DESC")
    return (
        str(grgr_name).strip() if grgr_name else None,
        str(pdds_desc).strip() if pdds_desc else None,
    )


def _fetch_cbd_products() -> List[str]:
    """
    Fetch all unique ``product`` values from the CBD fetchCustomerInfo endpoint.

    Returns:
        Sorted list of unique product strings.
    """
    cached = _CACHE.get("lob_cbd_products", {})
    if cached.hit and isinstance(cached.value, list):
        logger.info("Cache hit for CBD product list")
        return cached.value

    api_url = os.getenv("CBD_API", "")
    if not api_url:
        logger.warning("CBD_API environment variable not set; skipping CBD lookup")
        return []

    try:
        with httpx.Client(timeout=30.0, verify=False) as client:
            response = client.post(
                api_url,
                json={},
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.error(f"CBD fetchCustomerInfo request failed: {exc}")
        return []

    if not isinstance(data, list):
        logger.warning("CBD response is not a list; cannot extract products")
        return []

    products: List[str] = sorted(
        {str(item.get("product", "")).strip() for item in data if item.get("product")}
    )
    _CACHE.set("lob_cbd_products", {}, products)
    logger.info(f"Fetched {len(products)} unique CBD products")
    return products


def _classify_product(product: str) -> str:
    """
    Classify a CBD product string into a LOB value.

    Order of checks:
      1. Contains "medicare" -> "Medicare"
      2. Contains "medicaid" -> "Medicaid"
      3. Contains "exchange" -> "Commercial"
      4. Anything else      -> "Commercial"

    Args:
        product: Product name from CBD.

    Returns:
        LOB string: "Medicare", "Medicaid", or "Commercial".
    """
    lowered = product.lower()
    for kw, lob in _LOB_KEYWORD_MAP:
        if kw in lowered:
            logger.debug(
                "CBD product classification: product=%r matched keyword=%r -> LOB=%s",
                product, kw, lob,
            )
            return lob
    for kw in _EXCHANGE_KEYWORDS:
        if kw in lowered:
            logger.debug(
                "CBD product classification: product=%r matched exchange keyword=%r -> LOB=%s",
                product, kw, LOB_COMMERCIAL,
            )
            return LOB_COMMERCIAL
    logger.debug(
        "CBD product classification: product=%r matched no keywords -> LOB=%s",
        product, LOB_COMMERCIAL,
    )
    return LOB_COMMERCIAL


def determine_lob(claim_id: str) -> str:
    """
    Determine the Line of Business for a single claim.

    Steps:
        1. Call Facets Summary to retrieve GRGR_NAME + PDDS_DESC.
        2. If combined text contains "Medicare" -> LOB = "Medicare".
           If combined text contains "Medicaid" -> LOB = "Medicaid".
        3. Otherwise, fetch CBD products and similarity-match
           ``GRGR_NAME + " " + PDDS_DESC`` against each ``product``.
        4. Classify the best CBD match.
        5. Default to "Commercial".

    Args:
        claim_id: Business claim identifier.

    Returns:
        LOB string: "Medicare", "Medicaid", or "Commercial".
    """
    # Check cache first
    cached = _CACHE.get("lob_determination", {"claim_id": claim_id})
    if cached.hit and isinstance(cached.value, str):
        logger.info(f"Cache hit for LOB determination: claim_id={claim_id}")
        return cached.value

    # --- Step 1: Facets Summary ---
    grgr_name: Optional[str] = None
    pdds_desc: Optional[str] = None

    try:
        summary = get_claim_summary(claim_id)
        if summary.get("error"):
            logger.warning(
                f"Facets summary error for {claim_id}: {summary.get('error')}; defaulting to Commercial"
            )
        else:
            grgr_name, pdds_desc = _extract_facets_fields(summary)
            logger.info(
                f"Facets fields for {claim_id}: GRGR_NAME={grgr_name!r}, PDDS_DESC={pdds_desc!r}"
            )
    except Exception as exc:
        logger.error(f"Facets summary call failed for {claim_id}: {exc}")

    # --- Step 2: Quick-check GRGR_NAME + PDDS_DESC for Medicare / Medicaid ---
    combined_text = " ".join(filter(None, [grgr_name, pdds_desc])).lower()
    logger.info(
        "LOB step 2 | claim_id=%s | GRGR_NAME=%r | PDDS_DESC=%r | combined_text=%r",
        claim_id, grgr_name, pdds_desc, combined_text,
    )
    if combined_text:
        for kw, lob in _LOB_KEYWORD_MAP:
            if kw in combined_text:
                logger.info(
                    "LOB step 2 MATCH | claim_id=%s | keyword=%r found in combined_text -> LOB=%s",
                    claim_id, kw, lob,
                )
                _CACHE.set("lob_determination", {"claim_id": claim_id}, lob)
                return lob
        logger.info(
            "LOB step 2 NO MATCH | claim_id=%s | no Medicare/Medicaid keyword in combined_text",
            claim_id,
        )

    # --- Step 3: CBD similarity fallback ---
    search_text = combined_text.strip()
    if not search_text:
        lob = LOB_COMMERCIAL
        logger.info(f"No Facets text available for {claim_id}; defaulting to {lob}")
        _CACHE.set("lob_determination", {"claim_id": claim_id}, lob)
        return lob

    cbd_products = _fetch_cbd_products()
    if not cbd_products:
        lob = LOB_COMMERCIAL
        logger.info(
            "LOB step 3 SKIP | claim_id=%s | no CBD products available -> LOB=%s",
            claim_id, lob,
        )
        _CACHE.set("lob_determination", {"claim_id": claim_id}, lob)
        return lob

    logger.info(
        "LOB step 3 | claim_id=%s | search_text=%r | cbd_product_count=%d | sample_products=%r",
        claim_id, search_text, len(cbd_products), cbd_products[:5],
    )

    matched_product, similarity, word_matches = match_group_or_plan(
        text=search_text,
        candidates=cbd_products,
        default="",
    )

    logger.info(
        "LOB step 3 RESULT | claim_id=%s | matched_product=%r | similarity=%.2f%% | word_matches=%r",
        claim_id, matched_product, similarity, word_matches,
    )

    if not matched_product or similarity == 0.0:
        lob = LOB_COMMERCIAL
        logger.info(
            "LOB step 4 NO MATCH | claim_id=%s | no CBD product match -> LOB=%s",
            claim_id, lob,
        )
    else:
        lob = _classify_product(matched_product)
        logger.info(
            "LOB step 4 CLASSIFIED | claim_id=%s | product=%r | similarity=%.2f%% -> LOB=%s",
            claim_id, matched_product, similarity, lob,
        )

    _CACHE.set("lob_determination", {"claim_id": claim_id}, lob)
    return lob
