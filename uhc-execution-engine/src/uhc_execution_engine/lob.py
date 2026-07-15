"""Line-of-Business identification for a single claim.

The SOW requires "Identification of Line of Business per claim" across the six
supported LOBs (Medicare / Commercial / Medicaid, each INN or OON). This module
derives the LOB from the claim payload that has *already* been fetched into
context, so it costs no extra API round-trip.

Product axis (Medicare / Medicaid / Commercial)
    Mirrors the primary path of the standalone
    ``extracted-tools-main/src/tools/lob_determination.determine_lob``: read the
    Facets ``GRGR_NAME`` + ``PDDS_DESC`` and keyword-match (medicare wins over
    medicaid when both appear, matching that tool's ordered keyword map). When
    neither keyword is present we default to Commercial.

Network axis (INN / OON)
    Best-effort read of the claim network indicator (``CLCL_NTWK_IND`` and
    common aliases). Per the provider-selection SOP, true INN/OON is *derived*
    from a provider match, so this face-value read is a label only and is
    ``UNKNOWN`` when no indicator is present.
"""
from __future__ import annotations

from typing import Any

LOB_MEDICARE = "Medicare"
LOB_MEDICAID = "Medicaid"
LOB_COMMERCIAL = "Commercial"

NETWORK_INN = "INN"
NETWORK_OON = "OON"
NETWORK_UNKNOWN = "UNKNOWN"

# Tools that only make sense for Medicare claims (per the SOP flowcharts, which
# gate them behind "Is the plan Medicare? → Yes"). Used as a built-in default
# LOB scope so a Medicare-only tool is never invoked for a non-Medicare claim
# even before anyone sets a per-binding ``_lob_scope``. An auditor flagged
# ``check_medicare_coverage`` firing on a Commercial claim — this is the guard.
MEDICARE_ONLY_TOOLS = {
    "check_medicare_coverage",
    "medicare_optout_checker",
    "medicare_opt_out_checker",
    "provider_optout_lookup",
}


def default_tool_lob_scope(tool_name: str) -> list[str]:
    """Built-in LOB scope for a tool when no per-binding scope is configured."""
    return [LOB_MEDICARE] if (tool_name or "") in MEDICARE_ONLY_TOOLS else []


def tool_in_lob_scope(lob_scope, product: str, label: str = "") -> bool:
    """False when a tool is scoped to LOBs that exclude this claim.

    Empty scope → always in scope. Unknown claim LOB (no product) → in scope
    (never skip on missing data). Mirrors ``_rule_in_lob_scope`` in the shape
    executor so tool-level and SOP-level gating behave identically.
    """
    scope = lob_scope or []
    if not scope:
        return True
    p = (product or "").strip()
    if not p:
        return True
    lab = (label or "").strip()
    return p in scope or (bool(lab) and lab in scope)

# Checked in order; first hit wins (medicare before medicaid, like the tool).
_PRODUCT_KEYWORDS = ((("medicare",), LOB_MEDICARE), (("medicaid",), LOB_MEDICAID))

# Claim keys (case-insensitive) that carry the group / product description text.
_PRODUCT_TEXT_KEYS = ("GRGR_NAME", "PDDS_DESC", "group_name", "product",
                      "product_description", "plan_name", "line_of_business",
                      "lob")
# Claim keys that carry the in/out-of-network indicator.
_NETWORK_KEYS = ("CLCL_NTWK_IND", "network_indicator", "ntwk_ind",
                 "network_status", "network")


def _iter_values(obj: Any, keys_lower: set[str]):
    """Yield every value whose (case-insensitive) key is in ``keys_lower``.

    Walks nested dicts / lists so deeply-nested Facets envelopes
    (``body.Data.ClaimSummary.REC_CIV8.GRGR_NAME``) are found.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in keys_lower and v not in (None, "", {}, []):
                yield v
            else:
                yield from _iter_values(v, keys_lower)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_values(v, keys_lower)


def _collect_text(*sources: Any, keys: tuple[str, ...]) -> str:
    keys_lower = {k.lower() for k in keys}
    parts: list[str] = []
    for src in sources:
        if not src:
            continue
        for v in _iter_values(src, keys_lower):
            if isinstance(v, str):
                parts.append(v)
    return " ".join(parts)


def _classify_network(value: str) -> str:
    v = value.strip().upper()
    if not v:
        return NETWORK_UNKNOWN
    if v in {"I", "IN", "INN", "P", "PAR", "PARTICIPATING"} or "IN NETWORK" in v or "IN-NETWORK" in v:
        return NETWORK_INN
    if v in {"O", "OUT", "OON", "NP", "NONPAR", "NON-PAR", "NONPARTICIPATING"} or "OUT OF NETWORK" in v or "OUT-OF-NETWORK" in v:
        return NETWORK_OON
    return NETWORK_UNKNOWN


def determine_claim_lob(claim: dict[str, Any] | None,
                        raw_fetch: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the LOB descriptor for a claim.

    Shape::

        {"product": "Medicare"|"Medicaid"|"Commercial",
         "network": "INN"|"OON"|"UNKNOWN",
         "label":   "Medicare INN",          # product (+ network when known)
         "source":  "GRGR_NAME/PDDS_DESC"}
    """
    text = _collect_text(claim or {}, raw_fetch or {}, keys=_PRODUCT_TEXT_KEYS).lower()
    product = LOB_COMMERCIAL
    for kws, lob in _PRODUCT_KEYWORDS:
        if any(kw in text for kw in kws):
            product = lob
            break

    net_text = _collect_text(claim or {}, raw_fetch or {}, keys=_NETWORK_KEYS)
    network = NETWORK_UNKNOWN
    for token in net_text.split():
        network = _classify_network(token)
        if network != NETWORK_UNKNOWN:
            break
    if network == NETWORK_UNKNOWN and net_text:
        network = _classify_network(net_text)

    label = product if network == NETWORK_UNKNOWN else f"{product} {network}"
    return {
        "product": product,
        "network": network,
        "label": label,
        "source": "GRGR_NAME/PDDS_DESC" if text else "default",
    }
