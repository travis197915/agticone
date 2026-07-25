"""CPT/HCPCS → CBD benefit-category crosswalk and coverage resolver.

WHY THIS EXISTS
---------------
The Covered Benefit Document (CBD) API does **not** return coverage keyed by the
claim's CPT/HCPCS codes. It returns a *benefit-category grid* — rows keyed by
``descCode`` / ``descName`` with a per-plan ``covered`` Yes/No flag. ``cptCode``
on those rows is empty.

Two grid taxonomies appear in the wild:

1. **Fine (procedure)** — ``descCode`` 100–107, names like
   ``PSYCHOTHERAPY W/PATIENT 60 MINUTES`` (see ``out/claims_full.json``).
2. **Coarse (facility / revenue)** — names like ``Psychiatric/pvt``,
   ``Psychiatric clinic`` (what the live/mock MCP ``cbd_coverage`` tool returns
   today, often inside ``{"raw": "<json-string>"}`` and truncated at 32k).

The MCP tool result is frequently shaped as ``{"raw": "<json array string>"}``
(and that string is truncated mid-object). ``_grid_rows`` unwraps and salvages
complete row objects so resolution still works.

Correct determination = map each CPT to its benefit signal, then read coverage:

    CPT --(crosswalk)--> category --(plan grid)--> Covered / Not covered
    CPT in EXPLICIT_NOT_COVERED (e.g. G2211) --> Not covered
    CPT is E/M office visit --> Covered when clinic/visit benefits are covered
    otherwise --> Not in CBD (review)
"""
from __future__ import annotations

import json
import re

# Canonical fine-grained benefit-category names (descName).
CAT_PSYCH_DIAG = "Psychiatric Diagnostic Evaluation"
CAT_PSYTX_30 = "Psychotherapy W/Patient 30 Minutes"
CAT_PSYTX_45 = "Psychotherapy W/Patient 45 Minutes"
CAT_PSYTX_60 = "Psychotherapy W/Patient 60 Minutes"
CAT_NEURO_1ST = "Neuropsychological Testing - First Hour"
CAT_NEURO_ADDL = "Neuropsychological Testing - Each Add'L Hour"
CAT_PSYTEST_1ST = "Psychological Test Admin - First 30 Min"
CAT_PSYTEST_ADDL = "Psychological Test Admin - Each Add'L 30 Min"
CAT_ABA_PROTOCOL = "Adaptive Behavior Treatment With Protocol Mod"
CAT_CASE_MGMT = "Case Management, Each 15 Minutes"

# CPT / HCPCS → fine benefit category. Keys are bare codes (no modifiers).
CPT_TO_CATEGORY: dict[str, str] = {
    "90791": CAT_PSYCH_DIAG, "90792": CAT_PSYCH_DIAG,
    "90832": CAT_PSYTX_30, "90833": CAT_PSYTX_30,
    "90834": CAT_PSYTX_45, "90836": CAT_PSYTX_45,
    "90837": CAT_PSYTX_60, "90838": CAT_PSYTX_60,
    "90846": CAT_PSYTX_45, "90847": CAT_PSYTX_45,
    "90853": CAT_PSYTX_60, "90863": CAT_PSYTX_30,
    "90785": CAT_PSYCH_DIAG,
    "96127": CAT_PSYTEST_1ST,
    "96132": CAT_NEURO_1ST, "96133": CAT_NEURO_ADDL,
    "96136": CAT_PSYTEST_1ST, "96137": CAT_PSYTEST_ADDL,
    "97155": CAT_ABA_PROTOCOL,
    "T1016": CAT_CASE_MGMT,
    # Common BH HCPCS billed on these claims (coarse grid → Psychiatric*).
    "H0031": CAT_PSYCH_DIAG, "H0032": CAT_PSYCH_DIAG, "H0038": CAT_PSYTX_30,
    "H2011": CAT_PSYTX_30, "H2016": CAT_PSYTX_60, "S9480": CAT_PSYTX_60,
    "99492": CAT_PSYTX_60, "99494": CAT_PSYTX_30,
}

# Auditor-confirmed: present in CBD but NOT covered (never flip to Covered).
EXPLICIT_NOT_COVERED: dict[str, str] = {
    "G2211": "E/M visit-complexity add-on — Not Covered per CBD",
}

# Quality / HEDIS reporting HCPCS that are not CBD covered benefits (UAT).
EXPLICIT_NOT_IN_CBD: dict[str, str] = {
    "G8431": "Quality measure / screening HCPCS — not a Covered Benefit Document benefit",
}

# E/M office / outpatient visit family — auditors treat these as Covered when
# the plan's clinic/visit benefits are covered (Wendy 7/17: 99214 is covered).
EM_OFFICE_CODES: set[str] = {
    "99201", "99202", "99203", "99204", "99205",
    "99211", "99212", "99213", "99214", "99215",
}

# Backward-compat alias used by older callers / docs.
NON_BH_CODES: dict[str, str] = {
    **EXPLICIT_NOT_COVERED,
    **EXPLICIT_NOT_IN_CBD,
    **{c: "E/M office visit" for c in EM_OFFICE_CODES},
}

# CPT Category II (performance measurement) — e.g. 1036F, 1159F, 3008F, 3078F.
# These are quality-reporting codes, not CBD covered benefits (UAT bug).
_CATEGORY_II_RE = re.compile(r"^\d{4}F$")

COVERED = "Covered"
NOT_COVERED = "Not covered"
NOT_IN_CBD = "Not in CBD"

# Fine-grid category name fragments (normalized) that signal procedure taxonomy.
_FINE_CAT_FRAGMENTS = (
    "psychotherapywpatient",
    "psychiatricdiagnosticevaluation",
    "neuropsychologicaltesting",
    "psychologicaltestadmin",
    "adaptivebehavior",
)


def _norm(s: str | None) -> str:
    """Normalise a category name for comparison (case/space/punct-insensitive)."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def normalize_cpt(code: str | None) -> str:
    """Strip modifiers/whitespace and upper-case a CPT/HCPCS code.

    FACETS appends 2-char modifiers (e.g. ``9921425`` = 99214+25,
    ``90834GT`` = 90834 + telehealth GT).
    """
    c = re.sub(r"[^A-Za-z0-9]", "", (code or "")).upper()
    if not c:
        return ""
    if c[0].isalpha():
        return c[:5]
    m = re.match(r"(\d{5})", c)
    return m.group(1) if m else c


def cpt_category(code: str | None) -> str | None:
    """Benefit category (canonical descName) for a CPT/HCPCS, or ``None``."""
    return CPT_TO_CATEGORY.get(normalize_cpt(code))


def is_category_ii(code: str | None) -> bool:
    """True for CPT Category II performance-measurement codes (``####F``)."""
    return bool(_CATEGORY_II_RE.match(normalize_cpt(code)))


def _salvage_json_objects(raw: str) -> list[dict]:
    """Extract complete ``{...}`` objects from a (possibly truncated) JSON array."""
    rows: list[dict] = []
    depth = 0
    start: int | None = None
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    obj = json.loads(raw[start : i + 1])
                except Exception:
                    obj = None
                if isinstance(obj, dict):
                    rows.append(obj)
                start = None
    return rows


def _as_row_list(value: object) -> list[dict]:
    if isinstance(value, list):
        return [r for r in value if isinstance(r, dict)]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return _salvage_json_objects(value)
        if isinstance(parsed, list):
            return [r for r in parsed if isinstance(r, dict)]
        if isinstance(parsed, dict):
            return _grid_rows(parsed)
        return []
    return []


def _grid_rows(cbd_response: object) -> list[dict]:
    """Best-effort extraction of benefit-category grid rows.

    Handles:
      * bare list
      * ``{data:[...]}`` / ``{value:{data:[...]}}``
      * MCP envelope ``{raw: "<json>"|list}`` (incl. truncated raw)
      * outer MCP wrapper ``{response: {body|raw|data}}``
    """
    if isinstance(cbd_response, list):
        return [r for r in cbd_response if isinstance(r, dict)]
    if not isinstance(cbd_response, dict):
        return []

    # Unwrap common MCP / gateway envelopes.
    if "response" in cbd_response and not cbd_response.get("data") and not cbd_response.get("raw"):
        inner = cbd_response.get("response")
        if isinstance(inner, dict) and "body" in inner:
            return _grid_rows(inner.get("body"))
        return _grid_rows(inner)

    if isinstance(cbd_response.get("data"), list):
        return [r for r in cbd_response["data"] if isinstance(r, dict)]

    if "raw" in cbd_response:
        rows = _as_row_list(cbd_response.get("raw"))
        if rows:
            return rows

    val = cbd_response.get("value")
    if isinstance(val, dict) and isinstance(val.get("data"), list):
        return [r for r in val["data"] if isinstance(r, dict)]
    if isinstance(val, (list, str)):
        rows = _as_row_list(val)
        if rows:
            return rows

    return []


def _row_covered(row: dict) -> bool | None:
    """Read a grid row's coverage flag. ``None`` when absent/unrecognised."""
    v = row.get("covered")
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in {"yes", "y", "true", "1", "covered"}:
        return True
    if s in {"no", "n", "false", "0", "not covered", "notcovered"}:
        return False
    return None  # e.g. "Medical" — not a Yes/No signal


def _has_fine_taxonomy(rows: list[dict]) -> bool:
    for row in rows:
        n = _norm(row.get("descName"))
        if any(frag in n for frag in _FINE_CAT_FRAGMENTS):
            return True
        dc = str(row.get("descCode") or "").strip()
        if dc in {"100", "101", "102", "103", "104", "105", "106", "107"}:
            return True
    return False


def _psych_facility_covered(rows: list[dict]) -> bool | None:
    """Aggregate covered signal from coarse Psychiatric* / Psychiatric clinic rows."""
    flags: list[bool] = []
    for row in rows:
        n = _norm(row.get("descName"))
        if not n:
            continue
        if n.startswith("psychiatric") or "psychiatricclinic" in n:
            cov = _row_covered(row)
            if cov is not None:
                flags.append(cov)
    if not flags:
        return None
    # Any Yes ⇒ BH benefits are covered on this plan.
    return any(flags)


def _clinic_visit_covered(rows: list[dict]) -> bool | None:
    """Covered signal for E/M office visits from Clinic / Visit-charge rows."""
    flags: list[bool] = []
    for row in rows:
        n = _norm(row.get("descName"))
        if not n:
            continue
        if n in {"clinic", "visitchargegeneral"} or n.startswith("clinicvisit"):
            cov = _row_covered(row)
            if cov is not None:
                flags.append(cov)
        # Psychiatric clinic also covers office-style BH E/M on these plans.
        if "psychiatricclinic" in n:
            cov = _row_covered(row)
            if cov is not None:
                flags.append(cov)
    if not flags:
        return None
    return any(flags)


def build_category_index(cbd_response: object) -> dict[str, dict]:
    """Map normalised category descName → a representative grid row.

    When the same ``descName`` appears with mixed ``covered`` flags (different
    ``serviceType`` / lob rows), prefer a ``covered=Yes`` row so a single Yes
    on the plan counts as covered for that benefit category.
    """
    idx: dict[str, dict] = {}
    for row in _grid_rows(cbd_response):
        key = _norm(row.get("descName"))
        if not key:
            continue
        prev = idx.get(key)
        if prev is None:
            idx[key] = row
            continue
        # Upgrade a No/unknown row when we later see Yes for the same category.
        if _row_covered(prev) is not True and _row_covered(row) is True:
            idx[key] = row
    return idx


def resolve_coverage(cbd_response: object, cpt_codes: list[str]) -> dict:
    """Deterministically resolve per-CPT coverage from a raw CBD grid response.

    Returns::

        {
          "coverage_details": [...],
          "not_found_codes": [...],
          "codes_found": <int>,
          "determinations": {CPT: "Covered"|"Not covered"|"Not in CBD"},
        }
    """
    # If the payload already carries per-CPT coverage_details, honour them
    # (local mock fixture / newer API shape) — but still force G2211.
    if isinstance(cbd_response, dict):
        pre = cbd_response.get("coverage_details")
        if isinstance(pre, list) and pre and any(
            isinstance(d, dict) and d.get("cpt_code") for d in pre
        ):
            determ: dict[str, str] = {}
            details: list[dict] = []
            not_found: list[str] = []
            found: set[str] = set()
            by_code = {
                normalize_cpt(d.get("cpt_code")): d
                for d in pre
                if isinstance(d, dict) and d.get("cpt_code")
            }
            for raw in cpt_codes:
                base = normalize_cpt(raw)
                if base in EXPLICIT_NOT_COVERED:
                    determ[raw] = NOT_COVERED
                    details.append({
                        "cpt_code": raw, "covered": "No",
                        "disposition": NOT_COVERED,
                        "desc_name": None, "service_type": None,
                        "note": EXPLICIT_NOT_COVERED[base],
                    })
                    continue
                if base in EXPLICIT_NOT_IN_CBD or is_category_ii(base):
                    note = EXPLICIT_NOT_IN_CBD.get(
                        base, "CPT Category II quality-measure code — not a CBD benefit")
                    determ[raw] = NOT_IN_CBD
                    not_found.append(raw)
                    details.append({
                        "cpt_code": raw, "covered": "Unknown",
                        "disposition": NOT_IN_CBD,
                        "desc_name": None, "service_type": None,
                        "note": note,
                    })
                    continue
                d = by_code.get(base)
                if not d:
                    determ[raw] = NOT_IN_CBD
                    not_found.append(raw)
                    details.append({
                        "cpt_code": raw, "covered": "Unknown",
                        "disposition": NOT_IN_CBD,
                        "desc_name": None, "service_type": None,
                    })
                    continue
                cov = str(d.get("covered") or "").strip().lower()
                is_cov = cov in {"yes", "y", "true", "1", "covered"}
                determ[raw] = COVERED if is_cov else NOT_COVERED
                found.add(base)
                details.append({
                    "cpt_code": raw,
                    "covered": "Yes" if is_cov else "No",
                    "disposition": determ[raw],
                    "authorization": str(d.get("authorization") or "Unknown"),
                    "desc_name": d.get("desc_name") or d.get("descName"),
                    "service_type": d.get("service_type") or d.get("serviceType"),
                })
            return {
                "coverage_details": details,
                "not_found_codes": not_found,
                "codes_found": len(found),
                "determinations": determ,
            }

    rows = _grid_rows(cbd_response)
    idx = build_category_index(cbd_response)
    fine = _has_fine_taxonomy(rows)
    psych_cov = _psych_facility_covered(rows)
    clinic_cov = _clinic_visit_covered(rows)

    details = []
    not_found: list[str] = []
    determ = {}

    for raw in cpt_codes:
        base = normalize_cpt(raw)

        # 1) Auditor-confirmed not-covered codes (G2211).
        if base in EXPLICIT_NOT_COVERED:
            determ[raw] = NOT_COVERED
            details.append({
                "cpt_code": raw, "covered": "No",
                "disposition": NOT_COVERED,
                "desc_name": None, "service_type": None,
                "note": EXPLICIT_NOT_COVERED[base],
            })
            continue

        # 1b) Category II / quality-measure codes — never CBD "Covered" (UAT).
        if base in EXPLICIT_NOT_IN_CBD or is_category_ii(base):
            note = EXPLICIT_NOT_IN_CBD.get(
                base, "CPT Category II quality-measure code — not a CBD benefit")
            determ[raw] = NOT_IN_CBD
            not_found.append(raw)
            details.append({
                "cpt_code": raw, "covered": "Unknown",
                "disposition": NOT_IN_CBD,
                "desc_name": None, "service_type": None,
                "note": note,
            })
            continue

        cat = CPT_TO_CATEGORY.get(base)

        # 2) Fine taxonomy: map CPT → procedure category → covered flag.
        if cat and fine:
            row = idx.get(_norm(cat))
            if row is not None:
                cov = _row_covered(row)
                is_cov = True if cov is None else cov
                determ[raw] = COVERED if is_cov else NOT_COVERED
                details.append({
                    "cpt_code": raw,
                    "covered": "Yes" if is_cov else "No",
                    "disposition": determ[raw],
                    "authorization": str(row.get("authorization") or "Unknown"),
                    "desc_name": row.get("descName") or cat,
                    "service_type": row.get("serviceType"),
                })
                continue
            # Fine grid present but this category missing — fall through to coarse.

        # 3) Coarse taxonomy: BH CPTs follow Psychiatric* covered flag.
        if cat and psych_cov is not None:
            determ[raw] = COVERED if psych_cov else NOT_COVERED
            details.append({
                "cpt_code": raw,
                "covered": "Yes" if psych_cov else "No",
                "disposition": determ[raw],
                "authorization": "Unknown",
                "desc_name": "Psychiatric clinic" if psych_cov else cat,
                "service_type": None,
                "note": "resolved via coarse Psychiatric* facility categories",
            })
            continue

        # 4) E/M office visits — Covered when clinic/visit (or psych clinic) is Yes.
        if base in EM_OFFICE_CODES:
            flag = clinic_cov if clinic_cov is not None else psych_cov
            if flag is not None:
                determ[raw] = COVERED if flag else NOT_COVERED
                details.append({
                    "cpt_code": raw,
                    "covered": "Yes" if flag else "No",
                    "disposition": determ[raw],
                    "authorization": "Unknown",
                    "desc_name": "Clinic / Visit charge",
                    "service_type": None,
                    "note": "E/M office visit resolved via clinic/visit benefits",
                })
                continue

        # 5) Mapped BH category but no usable grid signal.
        if cat:
            determ[raw] = NOT_IN_CBD
            not_found.append(raw)
            details.append({
                "cpt_code": raw, "covered": "Unknown",
                "disposition": NOT_IN_CBD,
                "desc_name": cat, "service_type": None,
                "note": f"benefit category '{cat}' not present in this plan's CBD grid",
            })
            continue

        # 6) Unmapped — out of scope for review.
        determ[raw] = NOT_IN_CBD
        not_found.append(raw)
        details.append({
            "cpt_code": raw, "covered": "Unknown",
            "disposition": NOT_IN_CBD,
            "desc_name": None, "service_type": None,
            "note": "no behavioral-health benefit category maps to this code",
        })

    return {
        "coverage_details": details,
        "not_found_codes": not_found,
        "codes_found": len([d for d in details if d.get("disposition") in (COVERED, NOT_COVERED)]),
        "determinations": determ,
    }
