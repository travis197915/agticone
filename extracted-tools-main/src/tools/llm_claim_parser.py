"""
LLM-first claim parser with ontology & JSON-schema control.

This tool extracts a structured claim payload from DOC360 "print image" text using:
- A canonical ontology (with alias field names) to be resilient to future label/layout drift.
- A strict JSON Schema to constrain the output shape.
- Light post-validation (ICD-10, NPI, amounts) to adjust confidence and coerce types.

It integrates with the env-configured model layer in `model.py`:
- reads secrets and model settings from .env.stg (or env vars)
- can select a named model via AGENT_MODEL_MAP using LLM_CLAIM_PARSER_AGENT_NAME
- falls back to the legacy Azure client using CHAT_DEPLOYMENT when no named registry is configured

Exports:
- LangChain Tool: llm_parse_claim_with_ontology
- Functions: llm_parse_claim(...), content_to_text(...)

Author: thynkr-bhagenticai
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_SRC_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (str(_SRC_ROOT), str(_REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from thynkr_bhagenticai.logging_utils import get_logger
from thynkr_bhagenticai.tool_cache import ToolCache
from tools.claim_templates import get_template_keys  # re-use your flat keys if desired

# Your Azure OpenAI client
import core.model as _model
from core.model import initialize_clients

logger = get_logger(__name__)

_CACHE = ToolCache()

# Optional dependencies
try:
    import yaml  # for ontology YAML load
    _HAS_YAML = True
except Exception:
    _HAS_YAML = False

try:
    import jsonschema
    from jsonschema import Draft7Validator
    _HAS_JSONSCHEMA = True
except Exception:
    _HAS_JSONSCHEMA = False

# -----------------------------------------------------------------------------
# Constants & Validators
# -----------------------------------------------------------------------------

EVIDENCE_MAX = 220

ICD10_RE = re.compile(r"^[A-Z]\d[0-9A-Z]{1,6}(?:\.[0-9A-Z]{1,4})?$")
NPI10_RE = re.compile(r"^\d{10}$")
PAYOR_ID_RE = re.compile(r"^\d{4,6}$")
MONEY_RE = re.compile(r"^-?\d+(?:\.\d{1,4})?$")  # allow 2-4 decimals; we cast to float

DEFAULT_TEMPLATE = "emc_medical"
LLM_CLAIM_PARSER_AGENT_ENV_KEY = "LLM_CLAIM_PARSER_AGENT_NAME"
JSON_COMPATIBLE_MODEL_KINDS = frozenset({"azure_openai", "openai_compat"})

# JSON Schema for the output payload (kept close to your existing shape)
PAYLOAD_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["template_name", "fields", "diagnoses", "line_items", "totals", "confidence_scores"],
    "properties": {
        "template_name": {"type": "string"},
        "fields": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "required": ["value", "confidence", "provenance"],
                "properties": {
                    "value": {},  # any JSON type
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "provenance": {
                        "type": "object",
                        "required": ["evidence"],
                        "properties": {
                            "evidence": {"type": "string"},
                        },
                    },
                },
            },
        },
        "diagnoses": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["pointer", "code"],
                "properties": {
                    "pointer": {"type": "integer"},
                    "code": {"type": "string"},
                    "provenance": {
                        "type": "object",
                        "properties": {"evidence": {"type": "string"}},
                    },
                },
            },
        },
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["from_date", "to_date", "place_of_service", "cpt_hcpcs", "charge_amount", "units"],
                "properties": {
                    "from_date": {"type": "string"},
                    "to_date": {"type": "string"},
                    "place_of_service": {"type": "string"},
                    "type_of_service": {"type": "string"},
                    "cpt_hcpcs": {"type": "string"},
                    "modifiers": {"type": "array", "items": {"type": "string"}},
                    "diag_pointer": {"type": "string"},
                    "diag_pointers": {"type": "array", "items": {"type": "integer"}},
                    "charge_amount": {"type": "number"},
                    "units": {"type": "number"},
                    "anesthesia_time": {"type": "string"},
                    "emg_ind": {"type": "string"},
                    "line_item_control_no": {"type": "string"},
                    "other_ins_allowed": {"type": ["number", "null"]},
                    "negotiated_rate_ind": {"type": "string"},
                    "deductible_amount": {"type": ["number", "null"]},
                    "paid_amount": {"type": ["number", "null"]},
                    "epsdt_ind": {"type": "string"},
                    "family_planning_ind": {"type": "string"},
                    "remarks": {"type": "string"},
                    "rendering_npi": {"type": "string"},
                    "svc_npi": {"type": "string"},
                    "provenance": {
                        "type": "object",
                        "properties": {"evidence": {"type": "string"}},
                    },
                },
            },
        },
        "totals": {
            "type": "object",
            "properties": {
                "total_charge": {"type": ["number", "null"]},
                "total_patient_paid": {"type": ["number", "null"]},
                "total_other_insurance": {
                    "type": "object",
                    "properties": {
                        "paid": {"type": ["number", "null"]},
                        "allowed": {"type": ["number", "null"]},
                    },
                },
            },
        },
        "unmapped_fields": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "required": ["value", "provenance"],
                "properties": {
                    "value": {},
                    "provenance": {
                        "type": "object",
                        "required": ["evidence"],
                        "properties": {"evidence": {"type": "string"}},
                    },
                },
            },
        },
        "confidence_scores": {
            "type": "object",
            "properties": {
                "overall": {"type": "number"},
                "field_level": {
                    "type": "object",
                    "additionalProperties": {"type": "number"},
                },
            },
        },
        "claim_source": {"type": "string"},
        "other_insurance": {
            "type": "object",
            "description": "S4 other insurance information at claim level",
            "properties": {
                "plan_name": {"type": "string"},
                "claim_number": {"type": "string"},
                "payer_resp_seq": {"type": "string"},
                "route_ind": {"type": "string"},
                "total_oi_paid": {"type": ["number", "null"]},
                "total_deductible": {"type": ["number", "null"]},
                "contractual_adj": {"type": ["number", "null"]},
                "interest_paid": {"type": ["number", "null"]},
                "total_coinsurance": {"type": ["number", "null"]},
                "remark_codes": {"type": "array", "items": {"type": "string"}},
                "adjustment_indicator": {"type": "string"},
                "adjustment_orig_payment": {"type": ["number", "null"]},
                "remittance_remark_codes": {"type": "array", "items": {"type": "string"}},
            },
        },
        "hcp_pricing": {
            "type": "object",
            "description": "HCP line pricing/repricing information",
            "properties": {
                "icn": {"type": "string"},
                "price_method": {"type": "string"},
                "repriced_allowed_amt": {"type": ["number", "null"]},
                "savings_amount": {"type": ["number", "null"]},
                "reference_id": {"type": "string"},
                "rate": {"type": ["number", "null"]},
                "approved_drg_amt": {"type": ["number", "null"]},
                "reject_code": {"type": "string"},
                "policy_compliance_code": {"type": "string"},
                "exception_code": {"type": "string"},
            },
        },
    },
}

# -----------------------------------------------------------------------------
# Ontology loading
# -----------------------------------------------------------------------------

def load_ontology(ontology: Optional[Dict[str, Any]] = None,
                  ontology_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Load ontology dict either from provided dict or YAML file (config/claim_ontology.yaml).
    """
    if ontology:
        return ontology

    if ontology_path and os.path.exists(ontology_path):
        if not _HAS_YAML:
            raise RuntimeError("PyYAML is required to load ontology YAML")
        with open(ontology_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    # default search path
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    default_path = os.path.join(repo_root, "config", "claim_ontology.yaml")
    if os.path.exists(default_path):
        if not _HAS_YAML:
            raise RuntimeError("PyYAML is required to load ontology YAML")
        with open(default_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    # Minimal fallback ontology (very small; recommend providing full YAML)
    return {
        "version": 1,
        "namespaces": {
            "claim": {
                "header": {
                    "hic": {"aliases": ["HIC#", "HIC"]},
                    "route": {"aliases": ["RTE", "ROUTE"]},
                    "attachments": {"aliases": ["ATTCH", "ATTACH", "ATTACHMENTS"]},
                    "keyer": {"aliases": ["KEYER"]},
                    "dt": {"aliases": ["DT"]},
                },
                "payer": {
                    "payor_id": {"aliases": ["7C PAYOR ID", "PAYOR ID", "PAYER ID"]},
                    "spc": {"aliases": ["SPC", "CI/COMM INS", "F/COMMERCIAL"]},
                    "address": {"aliases": ["7E INSURANCE ADDRESS", "INSURANCE ADDRESS"]},
                },
                "totals": {
                    "charge": {"aliases": ["28 TOT CHARGE", "TOTAL CHARGE"]},
                    "patient_paid": {"aliases": ["29 TOT PAT PD", "PATIENT PAID"]},
                    "other_ins_pd": {"aliases": ["30 PD", "PD"]},
                    "other_ins_alw": {"aliases": ["30 ALW", "ALW"]},
                },
                "provider": {
                    "pay_to_npi": {"aliases": ["34 PAY-TO PROVIDER NPI"]},
                    "clearinghouse_id": {"aliases": ["34 CLRNG HOUSE CLAIM ID"]},
                },
            }
        },
        "validators": {
            "provider.pay_to_npi": r"^\d{10}$",
            "payer.payor_id": r"^\d{4,6}$",
            "diagnosis.icd10_code": r"^[A-Z]\d[0-9A-Z]{1,6}(\.[0-9A-Z]{1,4})?$",
        },
    }


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def content_to_text(envelope_or_text: Any) -> str:
    """
    Normalize a DOC360 envelope (or raw string) to the print-image text.
    """
    if isinstance(envelope_or_text, dict):
        content = envelope_or_text.get("content")
        if isinstance(content, str):
            return content
        return json.dumps(content, default=str)
    if isinstance(envelope_or_text, str):
        return envelope_or_text
    return json.dumps(envelope_or_text, default=str)


def _limit(s: str, n: int = EVIDENCE_MAX) -> str:
    return (s or "")[:n]


def _load_llm_env() -> Dict[str, Any]:
    """Load the same env view used by core.model from .env.stg or process env."""
    return _model._load_env()


def _resolve_named_model_name(env: Dict[str, Any]) -> Optional[str]:
    """Resolve the parser's named model from env-backed MODEL_REGISTRY if configured."""
    registry = _model.refresh_model_registry(env)
    if not registry:
        return None

    parser_agent_name = _model._env_get(env, LLM_CLAIM_PARSER_AGENT_ENV_KEY) or "__default__"
    raw_map = _model._env_get(env, "AGENT_MODEL_MAP")
    mapping: Dict[str, Any] = {}
    if raw_map:
        try:
            parsed = json.loads(raw_map)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Invalid AGENT_MODEL_MAP JSON in env") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("AGENT_MODEL_MAP must be a JSON object keyed by agent name")
        mapping = parsed

    if parser_agent_name != "__default__" and parser_agent_name not in mapping:
        raise RuntimeError(
            f"{LLM_CLAIM_PARSER_AGENT_ENV_KEY}='{parser_agent_name}' is not present in AGENT_MODEL_MAP"
        )

    model_name = (
        mapping.get(parser_agent_name)
        or mapping.get("__default__")
        or _model._default_agent_model_name(env, registry)
    )
    if str(model_name) not in registry:
        raise RuntimeError(
            f"Resolved parser model '{model_name}' is not present in MODEL_REGISTRY. "
            f"Known models: {sorted(registry)}"
        )

    return str(model_name)


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.DOTALL)


def _extract_json(text: str) -> Dict[str, Any]:
    """Best-effort JSON extraction from free-text LLM output.

    Handles:
    1. Pure JSON string
    2. JSON wrapped in ```json ... ``` fences
    3. A top-level { ... } block embedded in surrounding prose
    """
    text = (text or "").strip()
    # 1) Try direct parse first (cheapest path)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 2) Try fenced code block
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 3) Find outermost { ... }
    start = text.find("{")
    if start != -1:
        depth, end = 0, -1
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end != -1:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    logger.error(
        "Failed to extract JSON from LLM response (len=%d). Preview: %.500s",
        len(text),
        text[:500],
    )
    return {"status": "error", "message": "LLM returned non-JSON content", "raw": text[:2000]}


def _resolve_deployment_name(env: Optional[Dict[str, Any]] = None) -> str:
    """
    Resolve the legacy Azure OpenAI deployment name from .env.stg.
    """
    resolved_env = env if env is not None else _load_llm_env()
    env_model = _model._env_get(resolved_env, "CHAT_DEPLOYMENT")
    if env_model:
        return env_model
    # Attempt to introspect from client (best-effort)
    try:
        cfg = getattr(_model.openaiclient, "_config", None)
        dep = getattr(cfg, "azure_deployment", None)
        if dep:
            return dep
    except Exception:
        pass
    raise RuntimeError("CHAT_DEPLOYMENT not set in .env.stg and cannot be inferred from client config")


def _ensure_client_ready() -> None:
    """
    Initialize Azure OpenAI client if not already initialized.
    """
    if _model.openaiclient is None:
        initialize_clients()
    if _model.openaiclient is None:
        raise RuntimeError("Azure OpenAI client not initialized")


def _coerce_money(x: Any) -> Optional[float]:
    if x is None:
        return None
    s = str(x).strip()
    if s.startswith("."):
        s = "0" + s
    if not MONEY_RE.match(s):
        return None
    try:
        return float(s)
    except Exception:
        return None


def _post_validate(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Light post-validation:
    - ICD-10 pattern for diagnoses (drop or lower confidence on invalids)
    - NPI must be 10 digits
    - Money coercions for totals and line-items
    """
    # ICD-10 filter
    dx = payload.get("diagnoses") or []
    valid_dx = []
    for d in dx:
        code = str(d.get("code") or "").strip()
        if ICD10_RE.match(code):
            valid_dx.append(d)
        else:
            # skip invalid ICD codes
            logger.debug("Dropping invalid ICD code")
    payload["diagnoses"] = valid_dx

    # NPI 10 check in fields
    flds = payload.get("fields") or {}
    npi_field_keys = [k for k in flds.keys() if "NPI" in k.upper()]
    for k in npi_field_keys:
        v = flds[k].get("value")
        if v is None:
            continue
        s = str(v).strip()
        if "PAY-TO" in k.upper():
            # pay-to provider NPI specifically should be 10 digits
            if not NPI10_RE.match(s):
                flds[k]["value"] = None
                flds[k]["confidence"] = min(0.5, flds[k].get("confidence", 0.95))
        # other NPIs (like 11 NPI / 33 NPI) also check if strictly numeric 10
        elif k.strip().upper().endswith("NPI"):
            if not NPI10_RE.match(s):
                flds[k]["value"] = None
                flds[k]["confidence"] = min(0.5, flds[k].get("confidence", 0.95))

    # totals coercion
    totals = payload.get("totals") or {}
    totals["total_charge"] = _coerce_money(totals.get("total_charge"))
    totals["total_patient_paid"] = _coerce_money(totals.get("total_patient_paid"))
    oi = totals.get("total_other_insurance") or {}
    oi["paid"] = _coerce_money(oi.get("paid"))
    oi["allowed"] = _coerce_money(oi.get("allowed"))
    totals["total_other_insurance"] = oi
    payload["totals"] = totals

    # line-items coercion
    items = payload.get("line_items") or []
    for it in items:
        it["charge_amount"] = _coerce_money(it.get("charge_amount"))
        it["other_ins_allowed"] = _coerce_money(it.get("other_ins_allowed"))
        it["deductible_amount"] = _coerce_money(it.get("deductible_amount"))
        it["paid_amount"] = _coerce_money(it.get("paid_amount"))
        units = it.get("units")
        try:
            it["units"] = float(units) if units is not None else None
        except Exception:
            it["units"] = None
    payload["line_items"] = items

    # other_insurance coercion
    oi_section = payload.get("other_insurance") or {}
    for money_key in ("total_oi_paid", "total_deductible", "contractual_adj",
                      "interest_paid", "total_coinsurance", "adjustment_orig_payment"):
        oi_section[money_key] = _coerce_money(oi_section.get(money_key))
    payload["other_insurance"] = oi_section

    # hcp_pricing coercion
    hcp = payload.get("hcp_pricing") or {}
    for money_key in ("repriced_allowed_amt", "savings_amount", "rate", "approved_drg_amt"):
        hcp[money_key] = _coerce_money(hcp.get(money_key))
    payload["hcp_pricing"] = hcp

    return payload


# -----------------------------------------------------------------------------
# Prompt & LLM Invocation
# -----------------------------------------------------------------------------

def _build_messages(text: str, ontology: Dict[str, Any], template_name: str) -> List[Dict[str, str]]:
    """
    Build system/user messages for LLM extraction with ontology + schema control
    """
    # Keep ontology compact in prompt (stringify YAML/JSON)
    try:
        ontology_str = yaml.safe_dump(ontology, sort_keys=False) if _HAS_YAML else json.dumps(ontology, indent=2)
    except Exception:
        ontology_str = json.dumps(ontology, indent=2)

    # Short instruction: conservative, evidence, confidence
    system = (
        "You are an expert medical-claim extractor. "
        "Given a DOC360 'print image' of an HCFA-1500 (physician) claim and a canonical ontology "
        "with aliases, extract ALL fields, diagnoses, line items, totals, other insurance "
        "(S4/S5), HCP pricing/repricing, 24S remarks, and every additional section.\n"
        "Rules:\n"
        "- Map field names to the ontology's canonical names; use aliases when labels differ.\n"
        "- Extract EVERY labeled field from boxes 1-34, S-blocks (S1-S5), pricing, ambulance (36), drug info, and all continued sections.\n"
        "- For line items (Box 24), extract ALL columns: dates, POS, CPT, modifiers, diag pointer, charges, units, anes time, EMG, OTHER INS ALLOWED (col I), NEGOTIATED RATE IND (col II), DEDUCTIBLE AMOUNT (col J), PAID AMOUNT (col L).\n"
        "- For Box 24M, extract line_item_control_no and SVC NPI per service line.\n"
        "- For Box 24H, extract EPSDT IND and FAMILY PLANNING IND per service line.\n"
        "- For Box 24S, extract REMARKS and REMARK REF CD per service line.\n"
        "- For S4 other insurance, populate the 'other_insurance' section with plan name, claim number, OI paid/deductible/contractual adj/interest/coinsurance amounts, remark codes, adjustment details, remittance advice codes.\n"
        "- For HCP LINE PRICING/REPRICING, populate the 'hcp_pricing' section with ICN, repriced amounts, reject/compliance/exception codes.\n"
        "- Box 11 contains TWO distinct addresses: the BILLING PROVIDER ADDRESS (the address lines directly inside Box 11) and the 11A PAY-TO PROVIDER ADDRESS (under the 11A label). Extract both separately. The billing address may have a ZIP+4 that differs from the pay-to ZIP.\n"
        "- Only output valid ICD-10 codes for diagnoses.\n"
        "- Include a short provenance evidence snippet for each extracted field (<= 200 chars).\n"
        "- Provide per-field confidence [0.0-1.0], conservative for ambiguous values.\n"
        "- Include 'unmapped_fields' for anything not covered by ontology or dedicated arrays.\n"
        "- Do NOT invent or hallucinate fields. Only extract fields that have labels present in the document. "
        "If no Box 6 address label exists, do not create one.\n"
        "- For numeric diagnosis pointer values like '1230', split into individual pointers [1, 2, 3] (drop zeros).\n"
        "- ATTCH, RTE, and HIC# may be blank -- output null if the value area is empty.\n"
        "- Return a single JSON object matching the given JSON Schema exactly.\n"
    )

    user = (
        f"Template: {template_name}\n\n"
        "Ontology (YAML or JSON):\n"
        f"{ontology_str}\n\n"
        "JSON Schema (for the entire output object):\n"
        f"{json.dumps(PAYLOAD_SCHEMA, indent=2)}\n\n"
        "DOC360 print image text:\n"
        f"{text}\n"
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _call_llm(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Call the env-configured LLM and parse a JSON object.
    Uses MODEL_REGISTRY when configured, else falls back to the shared Azure client.
    """
    try:
        env = _load_llm_env()
        named_model = _resolve_named_model_name(env)
    except Exception as exc:
        logger.error("LLM model resolution failed: %s", exc, exc_info=True)
        return {"status": "error", "message": f"Model resolution failed: {exc}"}

    try:
        if named_model:
            registry = _model.refresh_model_registry(env)
            spec = registry.get(named_model)
            use_json_mode = spec is not None and spec.kind in JSON_COMPATIBLE_MODEL_KINDS
            result = _model.invoke_model(
                named_model,
                messages=messages,
                json_mode=use_json_mode,
                max_tokens=16384,
                env=env,
            )
            content = result.get("content") or "{}"
        else:
            _ensure_client_ready()
            deployment_name = _resolve_deployment_name(env)
            resp = _model.openaiclient.chat.completions.create(
                model=deployment_name,
                messages=messages,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content if resp and resp.choices else "{}"
    except Exception as exc:
        logger.error("LLM API call failed: %s", exc, exc_info=True)
        return {"status": "error", "message": f"LLM call failed: {exc}"}

    return _extract_json(content)


def _validate_schema(payload: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """
    Validate payload against JSON Schema (best-effort).
    """
    if not _HAS_JSONSCHEMA:
        return True, None
    try:
        Draft7Validator(PAYLOAD_SCHEMA).validate(payload)
        return True, None
    except Exception as e:
        return False, str(e)


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------

def llm_parse_claim(text: str,
                    ontology: Optional[Dict[str, Any]] = None,
                    template_name: str = DEFAULT_TEMPLATE,
                    ontology_path: Optional[str] = None) -> Dict[str, Any]:
    """
    LLM-first extraction with ontology+schema control.

    Args:
        text: DOC360 print image text
        ontology: canonical ontology dict (optional; if None, tries YAML file)
        template_name: logical template (default 'emc_medical')
        ontology_path: path to claim_ontology.yaml (optional)

    Returns:
        dict: structured payload with fields/diagnoses/line_items/totals/unmapped_fields and confidences
    """
    # Check cache first (keyed by text hash + template_name).
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cache_params = {"text_hash": text_hash, "template_name": template_name}
    cache_entry = _CACHE.get("llm_parse_claim", cache_params)
    if cache_entry.hit and isinstance(cache_entry.value, dict):
        logger.info("llm_parse_claim cache hit")
        return cache_entry.value

    # Load ontology
    onto = load_ontology(ontology, ontology_path)

    # Build prompt
    msgs = _build_messages(text, onto, template_name)

    # Call model
    llm_out = _call_llm(msgs)
    if isinstance(llm_out, dict) and llm_out.get("status") == "error":
        return {
            "template_name": template_name,
            "fields": {},
            "diagnoses": [],
            "line_items": [],
            "totals": {},
            "unmapped_fields": {},
            "confidence_scores": {"overall": 0.0, "field_level": {}},
            "claim_source": "physician",
            "_error": llm_out,
        }

    # Minimum shape guard
    payload: Dict[str, Any] = {
        "template_name": template_name,
        "fields": llm_out.get("fields") or {},
        "diagnoses": llm_out.get("diagnoses") or [],
        "line_items": llm_out.get("line_items") or [],
        "totals": llm_out.get("totals") or {},
        "unmapped_fields": llm_out.get("unmapped_fields") or {},
        "confidence_scores": llm_out.get("confidence_scores") or {"overall": 0.0, "field_level": {}},
        "claim_source": llm_out.get("claim_source") or "physician",
        "other_insurance": llm_out.get("other_insurance") or {},
        "hcp_pricing": llm_out.get("hcp_pricing") or {},
    }

    # Post-validation and coercions
    payload = _post_validate(payload)

    # Schema validation (best-effort)
    ok, err = _validate_schema(payload)
    if not ok:
        logger.debug("Schema validation failed", extra={"error": err})
        payload["_schema_error"] = err

    # Cache successful results (no _error key).
    if payload.get("_error") is None:
        _CACHE.set("llm_parse_claim", cache_params, payload)

    return payload


# -----------------------------------------------------------------------------
# LangChain Tool wrapper
# -----------------------------------------------------------------------------

try:
    from langchain.tools import tool
    from pydantic import BaseModel, Field

    class LlmClaimParseInput(BaseModel):
        """
        Input schema for llm_parse_claim_with_ontology tool.
        """
        claim_data: Dict[str, Any] = Field(
            ...,
            description="DOC360 envelope or raw text under 'content'."
        )
        template_name: str = Field(
            "emc_medical",
            description="Template identifier (default: emc_medical)."
        )
        ontology: Optional[Dict[str, Any]] = Field(
            None,
            description="Ontology dict; if omitted, the tool will try to load config/claim_ontology.yaml."
        )
        ontology_path: Optional[str] = Field(
            None,
            description="Explicit path to ontology YAML (optional)."
        )

    @tool("llm_parse_claim_with_ontology", args_schema=LlmClaimParseInput)
    def llm_parse_claim_with_ontology(claim_data: Dict[str, Any],
                                      template_name: str = "emc_medical",
                                      ontology: Optional[Dict[str, Any]] = None,
                                      ontology_path: Optional[str] = None) -> Dict[str, Any]:
        """
        LLM-first extraction tool:
        1) Normalize claim_data into text,
        2) Load ontology (dict or YAML),
        3) Invoke Azure OpenAI to extract canonical fields+arrays per JSON Schema,
        4) Post-validate and return the structured payload.
        """
        try:
            text = content_to_text(claim_data)
            result = llm_parse_claim(text=text,
                                     ontology=ontology,
                                     template_name=template_name,
                                     ontology_path=ontology_path)
            return result
        except Exception as e:
            logger.exception("llm_parse_claim_with_ontology failed")
            return {
                "status": "error",
                "error": {"code": "LLM_PARSE_FAILED", "message": str(e)},
                "template_name": template_name,
            }

except Exception:
    # Fallback function if LangChain is not installed
    def llm_parse_claim_with_ontology(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        """
        Fallback function that behaves like the Tool but without decorator metadata.
        """
        claim_data = kwargs.get("claim_data") if not args else args[0]
        template_name = kwargs.get("template_name", DEFAULT_TEMPLATE)
        ontology = kwargs.get("ontology")
        ontology_path = kwargs.get("ontology_path")
        text = content_to_text(claim_data)
        return llm_parse_claim(text=text,
                               ontology=ontology,
                               template_name=template_name,
                               ontology_path=ontology_path)
