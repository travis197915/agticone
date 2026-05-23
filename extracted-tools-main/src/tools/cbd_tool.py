"""
LangChain Tool for CBD Coverage Query

This tool determines whether CPT/procedure codes are covered under a member's
benefit plan by querying the CBD API. It dynamically resolves the Line of Business
(LOB/product - Medicare, Medicaid, or Commercial) from the claim ID, selects the
appropriate group and plan via cosine similarity-match against the CBD customer plans catalogue,
and calls the CBD coverage endpoint. Results are returned as a structured coverage
summary indicating Clean/Defect/Inconclusive per code, along with the matched group,
plan, and product used for the lookup.
"""

from difflib import SequenceMatcher
from typing import List, Dict, Any, Tuple, Optional
import hashlib
import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.tools import StructuredTool
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import httpx

from .cbd_api_client import CBDAPIClient
from .schemas.schema_cbd_tool import CBDCoverageInput, CBDCoverageOutput, CPTCoverageResult
from thynkr_bhagenticai.logging_utils import get_logger
from tools.diagnosis_api_client import DiagnosisAPIClient
from tools.schemas.schema_diagnosis_tool import DiagnosisInput, DiagnosisOutput, DiagnosisResult

logger = get_logger(__name__)

# ================= SIMILARITY MATCHING CONFIG =================
MIN_SIMILARITY_PERCENT = 80    # Tune: 50 / 70 / 80
DEFAULT_GROUP = "Standard Medicare"
# ==============================================================

def _clean_text(text: str) -> str:
    """
    Clean text by removing special characters after $ or digits or HMO keyword.
    Returns uppercase cleaned text.
    """
    match = re.search(r"(\$|\d|\bHMO\b)", text, re.IGNORECASE)
    if match:
        text = text[:match.start()]
    return re.sub(r"[^A-Za-z ]", "", text).upper().strip()


def _similarity_percent(a: str, b: str) -> float:
    """Calculate similarity percentage between two strings using SequenceMatcher."""
    return SequenceMatcher(None, a, b).ratio() * 100


def _match_topic_by_words(
    text: str,
    topics: List[str],
    threshold: float,
) -> Tuple[Optional[str], float, List[Tuple[str, str, float]]]:
    """
    Match text to topics by comparing individual words.

    Args:
        text: Input text to match
        topics: List of topic strings to match against
        threshold: Minimum similarity percentage for a word match

    Returns:
        Tuple of (matched_topic, max_similarity, list_of_matches)
    """
    cleaned_text = _clean_text(text)
    if not cleaned_text:
        return None, 0, []

    text_words = cleaned_text.split()
    first_word = text_words[0] if text_words else ""

    topic_results = []

    for topic in topics:
        topic_cleaned = _clean_text(topic)
        topic_words = topic_cleaned.split()

        # Track unique matched text words to avoid double-counting
        matched_text_words: set[str] = set()
        matched_topic_words: set[str] = set()
        all_matches: List[Tuple[str, str, float]] = []
        cumulative_sim = 0.0
        max_similarity = 0.0

        for tw in text_words:
            # Skip if this text word already matched
            if tw in matched_text_words:
                continue

            for tpw in topic_words:
                # Skip if this topic word already matched
                if tpw in matched_topic_words:
                    continue

                sim = _similarity_percent(tw, tpw)
                if sim >= threshold:
                    matched_text_words.add(tw)
                    matched_topic_words.add(tpw)
                    all_matches.append((tw, tpw, sim))
                    cumulative_sim += sim
                    max_similarity = max(max_similarity, sim)
                    break  # Move to next text word

        # Calculate first-word match score (for tie-breaking)
        first_word_match_score = 0.0
        first_word_match_idx = 999  # High number = no match
        for idx, tpw in enumerate(topic_words):
            sim = _similarity_percent(first_word, tpw)
            if sim >= threshold and sim > first_word_match_score:
                first_word_match_score = sim
                first_word_match_idx = idx

        if all_matches:
            match_count = len(all_matches)
            topic_results.append({
                "topic": topic,
                "topic_words": topic_words,
                "match_count": match_count,
                "cumulative_sim": cumulative_sim,
                "max_similarity": max_similarity,
                "first_word_match_idx": first_word_match_idx,
                "first_word_match_score": first_word_match_score,
                "matches": all_matches
            })

    if not topic_results:
        return None, 0, []

    # STRICT PRIORITY RANKING:
    # 1) Highest match_count (MOST IMPORTANT - 2 words ALWAYS beats 1 word)
    # 2) Highest cumulative similarity
    # 3) Highest max similarity
    # 4) First-word rule: prefer topic where first_word matches earlier in topic
    topic_results.sort(
        key=lambda x: (
            -x["match_count"],
            -x["cumulative_sim"],
            -x["max_similarity"],
            x["first_word_match_idx"],
            -x["first_word_match_score"]
        )
    )

    best = topic_results[0]
    return best["topic"], best["max_similarity"], best["matches"]


def _tfidf_fallback(text: str, topics: List[str]) -> Tuple[str, float]:
    """
    Fallback matching using TF-IDF cosine similarity.

    Args:
        text: Input text to match
        topics: List of topic strings to match against

    Returns:
        Tuple of (matched_topic, similarity_percent)
    """
    cleaned_text = _clean_text(text)
    cleaned_topics = [_clean_text(t) for t in topics]

    # Handle empty strings
    if not cleaned_text or not any(cleaned_topics):
        return DEFAULT_GROUP, 0.0

    vectorizer = TfidfVectorizer()
    try:
        vectors = vectorizer.fit_transform([cleaned_text] + cleaned_topics)
        similarities = cosine_similarity(vectors[0], vectors[1:])[0]
        idx = int(similarities.argmax())
        return topics[idx], float(similarities[idx] * 100)
    except ValueError:
        # Handle case where vectorizer fails (e.g., empty vocabulary)
        return DEFAULT_GROUP, 0.0


def match_group_or_plan(
    text: str,
    candidates: List[str],
    default: str = DEFAULT_GROUP,
) -> Tuple[str, float, List[Tuple[str, str, float]]]:
    """
    Match text to a list of candidate group/plan names using similarity matching.

    Args:
        text: Input text to match (e.g., user-provided group name)
        candidates: List of valid group/plan names to match against
        default: Default value to return if no match found

    Returns:
        Tuple of (matched_name, similarity_percent, list_of_word_matches)
    """
    if not text or not candidates:
        return default, 0.0, []

    # First try word-by-word matching
    topic, similarity, matches = _match_topic_by_words(
        text, candidates, MIN_SIMILARITY_PERCENT
    )

    if topic and similarity >= MIN_SIMILARITY_PERCENT:
        return topic, round(similarity, 2), matches

    # Fallback to TF-IDF
    topic, similarity = _tfidf_fallback(text, candidates)
    if similarity >= MIN_SIMILARITY_PERCENT:
        return topic, round(similarity, 2), []

    return default, 0.0, []


###### This is the code to fetch group and plan name from YAML File
# # Load Medicare group and plan names from config file
# def _load_cbd_config() -> Dict[str, Any]:
#     """Load CBD tool configuration from YAML file."""
#     config_path = Path(__file__).parent.parent.parent / "config" / "config_cbd_tool.yaml"
#     try:
#         with config_path.open("r", encoding="utf-8") as f:
#             return yaml.safe_load(f) or {}
#     except Exception as e:
#         logger.warning(f"Failed to load CBD config from {config_path}: {e}")
#         return {"medicare_group_names": ["Standard Medicare"], "medicare_plan_names": {"Standard Medicare": ["Standard Medicare"]}}


# _cbd_config = _load_cbd_config()
# medicare_group_names = _cbd_config.get("medicare_group_names", ["Standard Medicare"])
# medicare_plan_names = _cbd_config.get("medicare_plan_names", {"Standard Medicare": ["Standard Medicare"]})


###### This is the code to fetch group and plan name from API Call
# Load .env from repo root
_repo_root = Path(__file__).resolve().parent.parent
load_dotenv(_repo_root / ".env")


def cbd_customer_info(url: str, payload: dict | None = None, headers: dict | None = None) -> dict:
    """
    Make a POST request to the given URL and return the JSON response.

    Args:
        url: The API endpoint URL
        payload: JSON payload to send (optional)
        headers: Request headers (optional)

    Returns:
        JSON response as dictionary
    """
    default_headers = {"Content-Type": "application/json"}
    if headers:
        default_headers.update(headers)

    with httpx.Client(timeout=30.0, verify=False) as client:
        response = client.post(
            url,
            json=payload or {},
            headers=default_headers,
        )
        response.raise_for_status()
        return response.json()


def get_customer_plans_mapping(data: list[dict], product: str = "Commercial") -> dict[str, list[str]]:
    """
    Filter data by the given product and return a mapping of customerName to unique plans.

    Args:
        data: List of customer info dictionaries
        product: Product name to filter by (e.g. "Commercial", "Medicare", "Medicaid")

    Returns:
        Dictionary with customerName as keys and list of unique plan names as values
    """
    customer_plans: dict[str, list[str]] = {}

    for item in data:
        if item.get("product") == product:
            customer_name = item.get("customerName")
            plan = item.get("plan")
            if customer_name:
                if customer_name not in customer_plans:
                    customer_plans[customer_name] = []
                if plan and plan not in customer_plans[customer_name]:
                    customer_plans[customer_name].append(plan)

    # Sort keys and values for consistent output
    return {k: sorted(v) for k, v in sorted(customer_plans.items())}


def cbd_api_info() -> Dict[str, Any]:
    """
    Fetch coverage data from CBD API and return configs for all products.

    Returns:
        Dictionary with:
            - all_product_configs: mapping of product -> {group_names, plan_names}
            - medicare_group_names / medicare_plan_names kept for backward compat
    """
    # Fetch API URL from environment variable CBD_API
    api_url = os.getenv("CBD_API", "")
    if not api_url:
        print("Error: CBD_API environment variable is not set in .env file")
        return {
            "medicare_group_names": ["Standard Medicare"],
            "medicare_plan_names": {"Standard Medicare": ["Standard Medicare"]},
            "all_product_configs": {},
        }

    try:
        result = cbd_customer_info(url=api_url)

        if isinstance(result, list):
            # Build per-product group/plan mappings
            all_products = sorted(
                {str(item.get("product", "")).strip() for item in result if item.get("product")}
            )
            all_product_configs: Dict[str, Dict[str, Any]] = {}
            for prod in all_products:
                plan_names_map = get_customer_plans_mapping(result, prod)
                all_product_configs[prod] = {
                    "group_names": sorted(list(plan_names_map.keys())),
                    "plan_names": plan_names_map,
                }

            # Backward compat: keep original keys using "Commercial" product config
            commercial_config = all_product_configs.get("Commercial", {})
            return {
                "medicare_group_names": commercial_config.get("group_names", ["Standard Medicare"]),
                "medicare_plan_names": commercial_config.get("plan_names", {"Standard Medicare": ["Standard Medicare"]}),
                "all_product_configs": all_product_configs,
            }
        else:
            print("\nExpected a list response to filter plans")
    except httpx.HTTPStatusError as e:
        print(f"HTTP Error: {e.response.status_code} - {e.response.text}")
    except Exception as e:
        print(f"Error: {e}")

    # Return default on error
    return {
        "medicare_group_names": ["Standard Medicare"],
        "medicare_plan_names": {"Standard Medicare": ["Standard Medicare"]},
        "all_product_configs": {},
    }


_cbd_config = cbd_api_info()
_all_product_configs: Dict[str, Dict[str, Any]] = _cbd_config.get("all_product_configs", {})
medicare_group_names = _cbd_config.get("medicare_group_names", ["Standard Medicare"])
medicare_plan_names = _cbd_config.get("medicare_plan_names", {"Standard Medicare": ["Standard Medicare"]})

# LOB-specific default group/plan names when no similarity match can be found.
# Standard Default is always Medicare -> Standard Medicare / Standard Medicare.
_LOB_DEFAULTS: Dict[str, Tuple[str, str]] = {
    "Medicare": ("Standard Medicare", "Standard Medicare"),
    "Commercial": ("Standard Commercial", "Standard Commercial"),
    "Medicaid": ("RI Medicaid", "RI Medicaid"),
}
_STANDARD_DEFAULT_GROUP = "Standard Medicare"
_STANDARD_DEFAULT_PLAN = "Standard Medicare"


def _get_groups_for_product(product: str) -> Tuple[List[str], Dict[str, List[str]]]:
    """Return (group_names, plan_names) for *product* from the loaded CBD config."""
    config = _all_product_configs.get(product, {})
    return config.get("group_names", []), config.get("plan_names", {})


print(f"Loaded Medicare groups: {medicare_group_names}")
print(f"Loaded Medicare plans: {medicare_plan_names}")

def check_medicare_coverage_func(
    cpt_codes: List[str],
    group_name: str = "Standard Medicare",
    plan_name: str = "Standard Medicare",
    claim_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Check coverage for CPT procedure codes.

    When ``claim_id`` is provided the tool calls ``determine_lob(claim_id)`` to
    select the correct product (LOB), then uses the product-specific group/plan
    candidates and LOB-based defaults. If ``claim_id`` is omitted the tool
    falls back to the standard Medicare defaults.

    Args:
        cpt_codes: List of CPT procedure codes to check (required)
        group_name: Customer or employer group name (default: "Standard Medicare")
        plan_name: Plan name (default: "Standard Medicare")
        claim_id: Optional claim identifier used to resolve product/LOB dynamically.

    Returns:
        Dictionary with coverage details for each CPT code
    """
    # --- Step 0: Determine product (LOB) for this claim ---
    # Standard Default is always Medicare -> Standard Medicare / Standard Medicare.
    product = "Medicare"
    if claim_id:
        try:
            # Deferred import to break circular dependency:
            # lob_determination imports match_group_or_plan from this module.
            from tools.lob_determination import determine_lob  # noqa: PLC0415 # circular dep
            product = determine_lob(claim_id)
            logger.info("LOB determined: claim_id=%s product=%s", claim_id, product)
        except Exception as exc:
            logger.warning(
                "LOB determination failed: claim_id=%s error=%s; using default product=%s",
                claim_id, exc, product,
            )

    # Resolve LOB-specific defaults and product-specific group/plan candidates.
    # - Matching pool uses the product-specific groups loaded from the CBD customer info API.
    # - The coverage API call uses products=[product] so it queries the right product's data.
    # - If no product-specific groups are loaded (API not available), fall back to Commercial.
    lob_default_group, lob_default_plan = _LOB_DEFAULTS.get(product, (_STANDARD_DEFAULT_GROUP, _STANDARD_DEFAULT_PLAN))
    product_group_names, product_plan_names = _get_groups_for_product(product)
    effective_group_names = product_group_names if product_group_names else medicare_group_names
    effective_plan_names = product_plan_names if product_plan_names else medicare_plan_names
    # Products list sent to the coverage API - matches the resolved LOB so the API
    # returns coverage data for the correct product (Medicare/Medicaid/Commercial).
    api_products = [product]

    logger.info(
        "Checking coverage: codes_count=%d product=%s lob_default_group=%s lob_default_plan=%s "
        "group_candidates_count=%d",
        len(cpt_codes), product, lob_default_group, lob_default_plan, len(effective_group_names),
    )

    # Set defaults: if group_name not provided, use LOB default
    orig_group_input = group_name if group_name else None  # Store original user input for plan matching
    group_name = group_name or lob_default_group
    logger.info(f"Checking coverage: codes_count={len(cpt_codes)} product={product}, {cpt_codes}")

    # Track if group was successfully matched (used to determine fallback behavior)
    group_was_matched = (group_name == lob_default_group)  # True if default, or will be set if matched

    # Match group_name using similarity matching against product-specific candidates
    if group_name != lob_default_group:
        try:
            match_input = orig_group_input or group_name

            # Use product-specific group candidates so matching stays within the correct LOB.
            # e.g., for Medicaid: match against Medicaid groups only, not Commercial groups.
            matching_candidates = effective_group_names

            # Use similarity matching to find the best group match
            matched_group, similarity, matches = match_group_or_plan(
                text=match_input,
                candidates=matching_candidates,
                default=lob_default_group,
            )

            if matched_group and matched_group != lob_default_group:
                logger.info(
                    f"Matched group_name='{matched_group}' via similarity matching "
                    f"(score: {similarity}%, matches: {len(matches)} words)"
                )
                group_name = matched_group
                group_was_matched = True
            else:
                logger.info(
                    "No close match for group_name in product=%s candidates; using LOB default: %s",
                    product, lob_default_group,
                )
                group_name = lob_default_group
                group_was_matched = False
                orig_group_input = None

        except Exception as e:
            logger.warning(f"Group matching error: {e}")
            group_name = lob_default_group
            orig_group_input = None

    # Similarity match plan_name for the matched group
    try:
        plans_for_group = effective_plan_names.get(group_name, [])
        if plans_for_group:
            if len(plans_for_group) == 1:
                plan_name = plans_for_group[0]
                logger.info("Single plan available for group")
            else:
                # Match plan name using the original user input (which may contain extra terms like 'hmo', 'ppo')
                plan_match_input = orig_group_input if orig_group_input else group_name
                matched_plan, plan_similarity, plan_matches = match_group_or_plan(
                    text=plan_match_input,
                    candidates=plans_for_group,
                    default=plans_for_group[0]  # Default to first plan if no match
                )

                if matched_plan and plan_similarity >= 60:  # Lower threshold for plan matches
                    logger.info(f"Similarity matched plan (score: {plan_similarity}%)")
                    plan_name = matched_plan
                else:
                    logger.info("No close plan match, using first available plan")
                    plan_name = plans_for_group[0]
        else:
            logger.info("No plans found for group; using LOB default plan: %s", lob_default_plan)
            plan_name = lob_default_plan
    except Exception as e:
        logger.warning(f"Similarity matching error for plan name: {e}")
        plan_name = lob_default_plan

    # NOW check cache AFTER group/plan matching (use normalized names for cache key)
    cache_dir = Path(__file__).parent.parent.parent / "data" / "tool_cache" / "check_medicare_coverage"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Deterministic cache key: hash of sorted input with NORMALIZED group/plan names
    cache_key = hashlib.sha256(json.dumps({
        "cpt_codes": sorted([c.upper() for c in cpt_codes]),
        "group_name": group_name,
        "plan_name": plan_name,
        "product": product,
    }, sort_keys=True).encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.json"

    # Try cache with normalized names
    if cache_file.exists():
        try:
            with cache_file.open("r", encoding="utf-8") as f:
                cached = json.load(f)
            logger.info("Cache hit for coverage query")
            return cached
        except Exception as e:
            logger.warning(f"Cache read error: {e}")

    try:
        client = CBDAPIClient()
        # Try with product-specific group/plan and products filter
        response_data = client.fetch_coverage_data(
            group_name=group_name,
            plan_name=plan_name,
            products=api_products,
        )
        filtered_data = []
        if response_data and isinstance(response_data, dict):
            filtered_data = client.filter_by_cpt_codes(response_data, cpt_codes)
            logger.info(f"[COVERAGE RESULT] product={product} group={group_name} plan={plan_name} cpt_codes={cpt_codes} filtered_count={len(filtered_data)}")

        # Fallback: if product-specific query returned no data, retry with the LOB default
        # group/plan (still using the same product).
        if (not filtered_data) and (not group_was_matched) and (group_name != lob_default_group):
            logger.info(
                "Group not matched in product=%s list; falling back to LOB default: %s / %s",
                product, lob_default_group, lob_default_plan,
            )
            response_data = client.fetch_coverage_data(
                group_name=lob_default_group,
                plan_name=lob_default_plan,
                products=api_products,
            )

            print(f"gziufhz90{response_data}")
            if response_data and isinstance(response_data, dict):
                filtered_data = client.filter_by_cpt_codes(response_data, cpt_codes)
            else:
                filtered_data = []
            group_name = lob_default_group
            plan_name = lob_default_plan

        coverage_details = []
        found_codes = set()
        for item in filtered_data:
            cpt_code = item.get("descCode", "").upper()
            found_codes.add(cpt_code)
            coverage_result = CPTCoverageResult(
                cpt_code=cpt_code,
                covered=item.get("covered", "Unknown"),
                authorization=item.get("authorization", "Unknown"),
                desc_name=item.get("descName"),
                service_type=item.get("serviceType"),
                asam_level=item.get("asamLevel"),
                diagnosis=item.get("diagnosis"),
                effective_date=item.get("effectiveDate"),
                term_date=item.get("termDate"),
                lob=item.get("lob"),
                market=item.get("market")
            )
            coverage_details.append(coverage_result)

        requested_codes_upper = [code.upper() for code in cpt_codes]
        not_found_codes = [code for code in requested_codes_upper if code not in found_codes]

        output = CBDCoverageOutput(
            success=True,
            group_name=group_name,
            plan_name=plan_name,
            total_codes_queried=len(cpt_codes),
            codes_found=len(found_codes),
            coverage_details=coverage_details,
            not_found_codes=not_found_codes,
            errors=[]
        )

        # Save to cache
        try:
            with cache_file.open("w", encoding="utf-8") as f:
                json.dump(output.model_dump(), f, ensure_ascii=False)
            logger.info(f"Cached coverage result: codes_found={len(found_codes)}")
        except Exception as e:
            logger.warning(f"Cache write error: {e}")

        return output.model_dump()

    except ValueError as e:
        # Configuration error (missing bearer token, etc.)
        logger.error(f"Configuration error: {e}")
        return CBDCoverageOutput(
            success=False,
            group_name=group_name,
            plan_name=plan_name,
            total_codes_queried=len(cpt_codes),
            codes_found=0,
            errors=[f"Configuration error: {str(e)}"]
        ).model_dump()

    except Exception as e:
        # Unexpected error
        logger.error(f"Unexpected error: {e}")
        return CBDCoverageOutput(
            success=False,
            group_name=group_name,
            plan_name=plan_name,
            total_codes_queried=len(cpt_codes),
            codes_found=0,
            errors=[f"Unexpected error: {str(e)}"]
        ).model_dump()


# Create StructuredTool
medicare_coverage_tool = StructuredTool.from_function(
    func=check_medicare_coverage_func,
    name="check_medicare_coverage",
    description="""Check coverage for CPT procedure codes.

This tool queries the CBD (Covered Benefit Document) API to determine if specific CPT codes are covered under a plan. It dynamically resolves the Line of Business (LOB/product) for the claim and selects the matching group/plan.

Input Parameters:
- cpt_codes (required): List of CPT procedure codes (e.g., ["99213", "99214"])
- claim_id: Claim identifier used to determine LOB (Medicare/Commercial/Medicaid) automatically
- group_name: Customer/group name (default resolved from LOB)
- plan_name: Plan name (default resolved from LOB)

LOB-based defaults when no group match is found:
- Medicare → Standard Medicare / Standard Medicare
- Commercial → Standard Commercial / Standard Commercial
- Medicaid → SRI Medicaid / RI Medicaid

Returns JSON with coverage details for each CPT code including:
- CPT code (descCode field from API)
- Covered status (Yes/No)
- Authorization requirements (Yes/No)
- Service description and type
- Effective and termination dates
- Diagnosis category and ASAM level""",
    args_schema=CBDCoverageInput,
    return_direct=False
)

"""
LangChain Tool for Covered Diagnosis Query

This module provides a structured LangChain tool for querying diagnosis coverage
information using the Covered Diagnosis API.
"""


def check_diagnosis_coverage_func(diagnosis_code: str) -> Dict[str, Any]:
    """
    Check coverage information for a diagnosis code.

    This function queries the Covered Diagnosis API to determine the type
    and coverage status for a specified diagnosis code.

    Args:
        diagnosis_code: Diagnosis code to check (e.g., ICD-10 code like 'E11.9')

    Returns:
        Dictionary with diagnosis coverage details
    """
    logger.info(f"Checking diagnosis coverage: code={diagnosis_code}")

    # Check cache first
    cache_dir = Path(__file__).parent.parent / "data" / "tool_cache" / "check_diagnosis_coverage"
    cache_dir.mkdir(parents=True, exist_ok=True)

    cache_key = hashlib.sha256(diagnosis_code.upper().encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.json"

    if cache_file.exists():
        try:
            with cache_file.open("r", encoding="utf-8") as f:
                cached = json.load(f)
            logger.info("Cache hit for diagnosis query")
            return cached
        except Exception as e:
            logger.warning(f"Cache read error: {e}")

    try:
        client = DiagnosisAPIClient()
        response_data = client.fetch_diagnosis_info(diagnosis_code)

        logger.info(f"API response for {diagnosis_code}: type={type(response_data)}, data={response_data}")

        if response_data:
            if isinstance(response_data, list) and response_data:
                data_item = None
                for item in response_data:
                    if item.get("code", "").upper() == diagnosis_code.upper():
                        data_item = item
                        break
                if not data_item:
                    # Not found in coverage DB is a valid business answer (not covered).
                    output = DiagnosisOutput(
                        success=True,
                        diagnosis_code=diagnosis_code,
                        result=DiagnosisResult(
                            diagnosis_code=diagnosis_code,
                            code_type=None,
                            covered="No",
                            description="Diagnosis code not found in coverage",
                        ),
                        error=None,
                    )
                    try:
                        with cache_file.open("w", encoding="utf-8") as f:
                            json.dump(output.model_dump(exclude_none=True), f, ensure_ascii=False, indent=2)
                    except Exception as e:
                        logger.warning(f"Cache write error: {e}")
                    return output.model_dump(exclude_none=True)
            elif isinstance(response_data, dict):
                data_item = response_data
            else:
                data_item = {}

            # Extract fields based on actual API response structure
            # type1, type2, type3 represent different categorizations
            code_types = []
            for type_field in ["type1", "type2", "type3"]:
                type_val = data_item.get(type_field)
                if type_val and type_val != "N/A":
                    code_types.append(type_val)
            code_type_str = ", ".join(code_types) if code_types else None

            # Coverage is determined from coverageRecommend field
            coverage_recommend = data_item.get("coverageRecommend", "")
            covered_str = None
            if coverage_recommend:
                if "Cover Services" in coverage_recommend:
                    covered_str = "Yes"
                else:
                    covered_str = "No"

            result = DiagnosisResult(
                diagnosis_code=diagnosis_code,
                code_type=code_type_str,
                covered=covered_str,
                description=data_item.get("diagDesc")
            )

            output = DiagnosisOutput(
                success=True,
                diagnosis_code=diagnosis_code,
                result=result,
                error=None
            )
        else:
            # Empty API response -> code not in covered-diagnosis DB (not covered).
            output = DiagnosisOutput(
                success=True,
                diagnosis_code=diagnosis_code,
                result=DiagnosisResult(
                    diagnosis_code=diagnosis_code,
                    code_type=None,
                    covered="No",
                    description="Diagnosis code not found in coverage",
                ),
                error=None,
            )

        # Save to cache
        try:
            with cache_file.open("w", encoding="utf-8") as f:
                json.dump(output.model_dump(exclude_none=True), f, ensure_ascii=False)
            logger.info(f"Cached diagnosis result for {diagnosis_code}")
        except Exception as e:
            logger.warning(f"Cache write error: {e}")

        return output.model_dump(exclude_none=True)

    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return DiagnosisOutput(
            success=False,
            diagnosis_code=diagnosis_code,
            result=None,
            error=f"Configuration error: {str(e)}"
        ).model_dump(exclude_none=True)

    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return DiagnosisOutput(
            success=False,
            diagnosis_code=diagnosis_code,
            result=None,
            error=f"Unexpected error: {str(e)}"
        ).model_dump(exclude_none=True)


# Create StructuredTool
diagnosis_coverage_tool = StructuredTool.from_function(
    func=check_diagnosis_coverage_func,
    name="check_diagnosis_coverage",
    description="""Check coverage information for a diagnosis code.

This tool queries the Covered Diagnosis API to determine the type and coverage status
of a diagnosis code (e.g., ICD-10 codes).

Input Parameters:
- diagnosis_code (required): Diagnosis code to check (e.g., 'E11.9', 'I10')

Returns JSON with:
- success: Whether the query was successful
- diagnosis_code: The code that was queried
- result: Object containing code_type, covered status, and description
- error: Error message if query failed""",
    args_schema=DiagnosisInput,
    return_direct=False
)


# Export
__all__ = [
    "medicare_coverage_tool",
    "check_medicare_coverage_func",
    "diagnosis_coverage_tool",
    "check_diagnosis_coverage_func",
]
