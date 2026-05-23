# Transcribed from screenshots:
# - diagnosis_tool.py folder s2_00570.png–s2_00599.png (file open during transition)
# - electronic_claim_parser.py folder s2_00600.png–s2_00711.png
# Frames s2_00712+ show file explorer / facet_extension_portal_tool.py (excluded).

"""
Electronic claim (HCFA-1500 / EMC) parser with deterministic and LLM-assisted extraction.

This module provides:
- ElectronicClaimParser: a parser that first applies deterministic (regex-based)
  extraction tailored to DOC360 "print image" payloads and then merges with an
  LLM-driven "flat template with confidence" result. Deterministic values take
  precedence to avoid hallucinations and to ensure stable structured output.
- claim_parse_flat_template_with_confidence: a LangChain Tool wrapper for agent
  integrations (e.g., LangGraph ToolNode), returning a JSON-serializable dict
  aligned with your existing pipeline contracts.

Deterministic extraction covers:
- Header inline fields: HIC#, RTE, ATTCH (sliced between labels; no bleed)
- 6 PAT RELATION (e.g., "01/SELF")
- 7C PAYOR ID and SPC (e.g., "87726  F/COMMERCIAL" or "87726  CI/COMM INS")
- 7E Insurance Address (right column only; avoids mixing with 9C on the left)
- 9B Other insured policy/group; 9C Plan name/code, CLM FILING IND, DESCRIPTION
- Box 21 Diagnoses -> `diagnoses[]` {pointer:int, code:str}, strict ICD patterns,
  supports both "1  F332" and "1|A F411" styles
- Box 24 Line items -> `line_items[]` (supports letter pointers like "ABCD" and
  numeric pointers like "1230", and integer/decimal units), derives numeric
  `diag_pointers` from letters A-L => 1-12; merges 24M control numbers if present
- Totals (Boxes 25-30) -> patients_account_number, provider_accepts_assignment,
  totals.total_charge, totals.total_patient_paid, totals.total_other_insurance.paid/allowed (optional)
- 34 Pay-to provider NPI (10 digits only) and Clearinghouse Claim ID (if present)

Public surface:
- ElectronicClaimParser.parse_claim_text_flat_template_with_confidence_json(raw_text, template_name="emc_medical") -> dict
- @tool("claim_parse_flat_template_with_confidence")
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple, Optional

from thynkr_bhagenticai.logging_utils import get_logger

# Optional: only imported if you use the Tool wrapper with LangGraph.
try:
    from langchain.tools import tool
    _HAS_LANGCHAIN = True
except Exception:
    _HAS_LANGCHAIN = False

logger = get_logger(__name__)

# -----------------------------------------------------------------------------
# Utility helpers
# -----------------------------------------------------------------------------

_MAX_EVIDENCE_LEN = 220

# ICD-10-like pattern: Letter + Digit + Alnum{1..6}, optional dot + Alnum{1..4}
# Examples: F411, F4320, Z719, S72.001A
ICD_CODE_RE = r"[A-Z]\d[0-9A-Z]{1,6}(?:\.[0-9A-Z]{1,4})?"


def _evidence(snippet: str) -> str:
    """Return a small, safe evidence slice for traceability."""
    return (snippet or "").strip()[:_MAX_EVIDENCE_LEN]


def _search_first(pattern: str, text: str, flags: int = re.M) -> Optional[re.Match]:
    """Safe regex search returning the first match or None."""
    try:
        return re.search(pattern, text, flags)
    except re.error:
        logger.debug("Bad regex pattern", extra={"pattern": pattern})
        return None


def _findall(pattern: str, text: str, flags: int = re.M) -> List[Tuple]:
    """Safe regex findall returning results list (possibly empty)."""
    try:
        return re.findall(pattern, text, flags)
    except re.error:
        logger.debug("Bad regex pattern", extra={"pattern": pattern})
        return []


def _maybe_json_load(obj: Any) -> Any:
    """Parse JSON string to Python if possible, otherwise return as-is."""
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                return json.loads(s)
            except Exception:
                return obj
    return obj


# -----------------------------------------------------------------------------
# Parser
# -----------------------------------------------------------------------------


class ElectronicClaimParser:
    """
    Electronic claim parser for DOC360 "print image" content.

    Two-pass strategy:
    1) Deterministic extraction tailored to the stable layout of HCFA-1500 print images.
    2) LLM-driven "flat template with confidence" extraction (optional) for supplemental fields.
       Deterministic values overwrite LLM values to improve reliability.
    """

    def __init__(self, tool: Any = None, debug: bool = False) -> None:
        self.tool = tool
        self.debug = debug

    # -------------------------- Public API --------------------------

    def parse_claim_text_flat_template_with_confidence_json(
        self,
        raw_text: str,
        template_name: str = "emc_medical",
    ) -> Dict[str, Any]:
        """
        Parse DOC360 text and return a JSON-compatible payload matching your contract.

        Args:
            raw_text: The raw "print image" text from DOC360 (string).
            template_name: Template identifier. Currently supports "emc_medical".

        Returns:
            dict with:
                - template_name, claim_source,
                - fields (dict of {value, confidence, provenance, evidence}),
                - diagnoses (list),
                - line_items (list),
                - totals (dict),
                - confidence_scores (overall + per-field).
        """
        if not isinstance(raw_text, str):
            raw_text = str(raw_text or "")

        if self.debug:
            logger.debug("Parsing claim text", extra={"template_name": template_name})

        # 1) Deterministic parse (reliable, schema-first)
        deterministic = (
            self._deterministic_emc_medical_extract(raw_text)
            if template_name == "emc_medical"
            else {"fields": {}, "diagnoses": [], "line_items": [], "totals": {}}
        )

        # 2) LLM parse (if available), then merge
        llm_out = self._llm_parse_flat_template(raw_text, template_name=template_name)

        merged: Dict[str, Any] = dict(llm_out) if isinstance(llm_out, dict) else {}
        merged["template_name"] = template_name

        # Merge fields: deterministic values take precedence
        llm_fields = dict(merged.get("fields") or {})
        for key, value in (deterministic.get("fields") or {}).items():
            llm_fields[key] = value
        merged["fields"] = llm_fields

        # Merge diagnoses / line_items / totals
        merged["diagnoses"] = deterministic.get("diagnoses") or merged.get("diagnoses") or []
        merged["line_items"] = deterministic.get("line_items") or merged.get("line_items") or []
        merged["totals"] = deterministic.get("totals") or merged.get("totals") or {}

        # Confidence aggregation
        try:
            merged["confidence_scores"] = self._recompute_confidences(merged)
        except Exception:
            logger.debug("Confidence recompute failed", exc_info=True)

        # Best effort claim source
        merged.setdefault("claim_source", "physician")

        return merged

    # ----------------------- DOC360 envelope helper (optional) -----------------------

    @staticmethod
    def content_to_text(envelope_or_text: Any) -> str:
        """
        Convert a DOC360 envelope (or raw content) into a plain string for parsing.
        """
        if isinstance(envelope_or_text, dict):
            content = envelope_or_text.get("content")
            if isinstance(content, str):
                return content
            return json.dumps(content, default=str)
        if isinstance(envelope_or_text, str):
            return envelope_or_text
        return json.dumps(envelope_or_text, default=str)

    # ----------------------- Deterministic extraction -----------------------

    def _deterministic_emc_medical_extract(self, raw_text: str) -> Dict[str, Any]:
        """
        Run all deterministic passes and assemble a structured payload.

        Returns:
            Dict with keys: fields (dict), diagnoses (list), line_items (list), totals (dict)
        """
        result: Dict[str, Any] = {
            "fields": {},
            "diagnoses": [],
            "line_items": [],
            "totals": {},
        }

        # Header inline fields: HIC#, RTE, ATTCH
        header_fields = self._extract_header_inline_fields(raw_text)
        if header_fields:
            result["fields"].update(header_fields)

        # 6 PAT RELATION (e.g., "01/SELF")
        pat_rel = self._extract_6_patient_relation(raw_text)
        if pat_rel is not None:
            result["fields"]["6 PAT RELATION TO INSURED"] = {
                "value": pat_rel,
                "confidence": 0.95,
                "provenance": ["payload"],
                "evidence": pat_rel,
            }

        # 7C PAYOR ID + SPC
        block_7c = self._extract_7c_payor_and_spc(raw_text)
        if block_7c:
            result["fields"].update(block_7c)

        # 7E Insurance address (earlier layout; right column-only)
        addr_7e = self._extract_7e_insurance_address(raw_text)
        if addr_7e:
            result["fields"]["7E INSURANCE ADDRESS"] = {
                "value": addr_7e,
                "confidence": 0.90,
                "provenance": ["payload"],
                "evidence": _evidence(addr_7e),
            }

        # 9B/9C block
        nine_block = self._extract_9b_9c_block(raw_text)
        if nine_block:
            result["fields"].update(nine_block)

        # Diagnoses (Box 21)
        diagnoses = self._extract_diagnoses(raw_text)
        if diagnoses:
            result["diagnoses"] = diagnoses

        # Line items (Box 24 + 24M control numbers)
        line_items = self._extract_box24_lines(raw_text)
        if line_items:
            result["line_items"] = line_items

        # Totals (Boxes 25-30): totals + account + assignment
        totals = self._extract_totals_block(raw_text)
        if totals:
            if "patients_account_number" in totals:
                result["fields"]["26 PATIENTS ACCOUNT#"] = {
                    "value": totals["patients_account_number"],
                    "confidence": 0.95,
                    "provenance": ["payload"],
                    "evidence": "",
                }
            if "provider_accepts_assignment" in totals:
                result["fields"]["27 PROV ACCPTS ASGNMT"] = {
                    "value": totals["provider_accepts_assignment"],
                    "confidence": 0.95,
                    "provenance": ["payload"],
                    "evidence": "",
                }
            if totals.get("totals"):
                result["totals"] = totals["totals"]

        # 34: Pay-to NPI and Clearinghouse claim id
        blk34 = self._extract_34_block(raw_text)
        if blk34:
            result["fields"].update(blk34)

        return result

    def _extract_header_inline_fields(self, raw_text: str) -> Dict[str, Any]:
        """
        Extract inline header fields that often appear empty but should be present:
        - HIC# (Medicare)
        - RTE (Route)
        - ATTCH (Attachments)

        Robust against cross-label bleeding by slicing between known labels.
        """
        fields: Dict[str, Any] = {}
        match = _search_first(
            r"\|1\s+INSUREDS ID NUMBER.*?ID#:\s+([^\|]+?)\|", raw_text, flags=re.M | re.S
        )
        if not match:
            return fields

        row = match.group(0)

        labels_in_order = ["HIC#", "RTE:", "ATTCH:", "KEYER:", "DT:"]
        positions: Dict[str, int] = {}
        for label in labels_in_order:
            m = _search_first(rf"{re.escape(label)}", row)
            if m:
                positions[label] = m.start()

        def _between(label: str, next_labels: List[str]) -> str:
            start_m = _search_first(rf"{re.escape(label)}\s*", row)
            if not start_m:
                return ""
            start = start_m.end()
            end = len(row)
            for nl in next_labels:
                pos = positions.get(nl)
                if pos is not None and pos >= start:
                    end = min(end, pos)
            value = row[start:end]
            value = " ".join(value.split())
            if re.match(r"^[A-Z#]+:$", value) or value == "":
                return ""
            tok = value.split()[0]
            tok = re.sub(r"[^A-Z0-9\-]", "", tok)
            return tok

        hic = _between("HIC#", ["RTE:", "ATTCH:", "KEYER:", "DT:"])
        rte = _between("RTE:", ["ATTCH:", "KEYER:", "DT:"])
        attch = _between("ATTCH:", ["KEYER:", "DT:"])

        for key, val in (("HIC#", hic), ("RTE", rte), ("ATTCH", attch)):
            fields[key] = {
                "value": val,
                "confidence": 0.99,
                "provenance": ["payload"],
                "evidence": _evidence(row),
            }

        return fields

    def _extract_6_patient_relation(self, raw_text: str) -> Optional[str]:
        """
        Extract "6 PAT RELATION TO INSURED" entry (e.g., "01/SELF") if present.
        """
        block = _search_first(
            r"\|5.*?\|6\s+PAT\s+RELATION.*?\|7\s+INSURED\s+ADDRESS",
            raw_text,
            flags=re.M | re.S,
        )
        if not block:
            return None
        segment = block.group(0)
        m = _search_first(r"\b\d{2}/[A-Z]+\b", segment)
        return m.group(0) if m else None

    def _extract_7c_payor_and_spc(self, raw_text: str) -> Dict[str, Any]:
        """
        Extract 7C PAYOR ID and SPC (e.g., "87726  F/COMMERCIAL" or "87726  CI/COMM INS").
        """
        out: Dict[str, Any] = {}
        m7c = _search_first(
            r"\|7C\s+INSURANCE PLAN OR PROGRAM NAME, PAYOR ID AND SPC\s*\|(?P<body>.*?)(?=\|+\-\+|\|\|\s*7D\s+IS THERE)",
            raw_text,
            flags=re.M | re.S,
        )
        if not m7c:
            return out
        body = m7c.group("body")

        payor_id = None
        spc = None
        for ln in body.splitlines():
            s = " ".join(ln.strip(" |").split())
            if not s:
                continue
            # Allow 1-3 letters before '/', then the rest (covers F/COMMERCIAL and CI/COMM INS)
            m = _search_first(r"(?P<pay>\d{4,6})\s+(?P<spc>[A-Z]{1,3}/[A-Z][A-Z/ ]+)", s)
            if m:
                payor_id = m.group("pay")
                spc = m.group("spc").strip()
                break

        if payor_id:
            out["7C PAYOR ID"] = {
                "value": payor_id,
                "confidence": 0.95,
                "provenance": ["payload"],
                "evidence": _evidence(body),
            }
        if spc:
            out["7C SPC"] = {
                "value": spc,
                "confidence": 0.95,
                "provenance": ["payload"],
                "evidence": _evidence(body),
            }
        return out

    def _extract_7e_insurance_address(self, raw_text: str) -> Optional[str]:
        """
        Extract the 7E Insurance Address block as a single normalized string from the RIGHT column only.
        This avoids concatenating 9C (left) text with 7E (right) lines that share the same row.
        """
        m = _search_first(
            r"\|7E\s+INSURANCE ADDRESS\s*\|(?P<body>.*?)(?=\|+\-\+|\|\|\s*9D\s+EMPLOYERS\|\|\s*S\d+\s+)",
            raw_text, flags=re.M | re.S
        )
        if not m:
            return None

        body = m.group("body")
        right_col_segments: List[str] = []
        for ln in body.splitlines():
            # capture right column cell only: |<left>|<right>|
            mr = _search_first(r"^\s*\|.*?\|\s*(?P<right>[^|]+?)\s*\|", ln, flags=re.M)
            if mr:
                cell = " ".join(mr.group("right").split())
                if cell:
                    right_col_segments.append(cell)

        address = " ".join(right_col_segments).strip()
        return address or None

    def _extract_9b_9c_block(self, raw_text: str) -> Dict[str, Any]:
        """
        Extract fields under 9B and 9C:
        - 9B: left-column-only cell content, with filters to avoid 7C noise.
        - 9C: bounded Plan name/code, Filing IND, Description.
        """
        out: Dict[str, Any] = {}

        # 9B bounded block
        m9b = _search_first(
            r"\|9B\s+OTHER INSUREDS POLICY OR GROUP# AND NAME\s*\|(?P<body>.*?)(?=\|\s*9C\s+INSURANCE|\+\-+\+)",
            raw_text, flags=re.M | re.S
        )
        if m9b:
            body = m9b.group("body")
            candidates: List[str] = []
            for ln in body.splitlines():
                # left column cell only: |<left>|<right>|
                m1 = _search_first(r"^\s*\|\s*(?P<left>[^|]+?)\s*\|", ln)
                if not m1:
                    continue
                left = " ".join(m1.group("left").split())
                if not left:
                    continue
                # Negative filters (skip 7C-ish noise or boilerplate)
                if "PAYOR SEQ" in left or re.search(r"\b\d{5}\b.*COMM", left):
                    continue
                if left.upper() in {"SPEC PRG IND", "NONE", "000"}:
                    continue
                candidates.append(left)

            if candidates:
                chosen = candidates[0]
                out["9B OTHER INSUREDS POLICY OR GROUP# AND NAME"] = {
                    "value": chosen,
                    "confidence": 0.85,
                    "provenance": ["payload"],
                    "evidence": _evidence(body),
                }

        # 9C bounded block
        m9c = _search_first(
            r"\|9C\s+INSURANCE PLAN OR PROGRAM NAME.*?\|(?P<body>.*?)(?=\|+\-\+|\s*7E\s+INSURANCE|\|\s*9D\s+EMPLOYERS)",
            raw_text, flags=re.M | re.S
        )
        if m9c:
            body = m9c.group("body")
            plan_name: Optional[str] = None
            plan_code: Optional[str] = None
            filing_ind: Optional[str] = None
            description: Optional[str] = None

            for ln in body.splitlines():
                s = " ".join(ln.strip(" |").split())
                if not s:
                    continue
                if s.startswith("CLM FILING IND:"):
                    filing_ind = s.split(":", 1)[-1].strip()
                elif s.startswith("DESCRIPTION:"):
                    description = s.split(":", 1)[-1].strip()
                elif plan_name is None:
                    plan_name = s
                else:
                    norm = s.replace(" ", "")
                    if plan_code is None and re.fullmatch(r"[A-Z0-9]{2,10}", norm):
                        plan_code = s.strip()

            if plan_name:
                out["9C PLAN NAME"] = {
                    "value": plan_name,
                    "confidence": 0.90,
                    "provenance": ["payload"],
                    "evidence": _evidence(body),
                }
            if plan_code:
                out["9C PLAN CODE"] = {
                    "value": plan_code,
                    "confidence": 0.85,
                    "provenance": ["payload"],
                    "evidence": _evidence(body),
                }
            if filing_ind:
                out["9C CLM FILING IND"] = {
                    "value": filing_ind,
                    "confidence": 0.90,
                    "provenance": ["payload"],
                    "evidence": _evidence(body),
                }
            if description:
                out["9C DESCRIPTION"] = {
                    "value": description,
                    "confidence": 0.90,
                    "provenance": ["payload"],
                    "evidence": _evidence(body),
                }

        return out

    def _extract_diagnoses(self, raw_text: str) -> List[Dict[str, Any]]:
        """
        Extract diagnoses from Box 21 as pointer+code pairs.
        Accept only ICD-like codes (e.g., F411, F4320, Z719, S72.001A).
        Supports both "1 F332" and "1|A F411" line styles.
        """
        m = _search_first(
            r"\|21\s+DIAGNOSIS.*?\|\s*(?P<row>.*?)\|\|\s*23 PRIOR AUTHORIZATION NUMBER",
            raw_text, flags=re.M | re.S
        )
        if not m:
            return []

        row = m.group("row")

        pairs: List[Tuple[str, str]] = []
        # Style A: "1 F332"
        pairs += _findall(rf"(\d+)\s+({ICD_CODE_RE})\b", row)
        # Style B: "1|A F411"
        pairs += _findall(rf"(\d+)\s*\|\s*[A-L]?\s*({ICD_CODE_RE})\b", row)
        # Style C: letter-only pointer without leading digit, e.g. "E E118"
        # Converts letter A-L to numeric pointer 1-12.
        _letter_map = {chr(ord("A") + i): str(i + 1) for i in range(12)}
        for letter, code in _findall(rf"\b([A-L])\s+({ICD_CODE_RE})\b", row):
            pairs.append((_letter_map[letter], code))

        # Deduplicate by (pointer, code) preserving order
        seen = set()
        results: List[Dict[str, Any]] = []
        for pointer_str, code in pairs:
            key = (pointer_str, code)
            if key in seen:
                continue
            seen.add(key)
            try:
                pointer = int(pointer_str)
            except ValueError:
                continue
            results.append({
                "pointer": pointer,
                "code": code,
                "provenance": ["payload"],
                "evidence": _evidence(row),
            })
        return results

    def _extract_box24_lines(self, raw_text: str) -> List[Dict[str, Any]]:
        """
        Extract line items from Box 24 grid rows. Merged with 24M control numbers by order.

        Supports:
        - Letter diagnosis pointers (A-L), possibly multiple (e.g., "ABCD")
        - Numeric diagnosis pointers (e.g., "1230")
        - Integer or decimal units (e.g., "0001" or "0001.0")

        Example row (older layout):
        | 030325  030325 | 11 |    |99215             |1230 |      445.00 |00001.0|0000| N | ...
        """
        line_re = re.compile(
            r"""
            ^\s*\|\s*(?P<from>\d{6})\s+(?P<to>\d{6})\s*\|     # from/to dates
            \s*(?P<pos>\d{2})\s*\|\s*                         # place of service
            (?P<typesv>[A-Z0-9]{0,2})?\s*\|\s*                # type of service (optional/empty)
            (?P<cpt>\d{5})\s*                                 # CPT/HCPCS
            (?P<mods>(?:\s+[A-Z0-9]{2,4}){0,4})\s*\|          # up to 4 modifiers
            \s*(?P<diagptr>[A-L]{1,4}|\d{1,4})\s*\|           # diagnosis pointer(s): letters or digits
            \s*(?P<charge>\d+\.\d{2})\s*\|                    # $CHARGES
            \s*(?P<units>\d+(?:\.\d+)?)\s*\|                  # UNITS: int or decimal
            \s*(?P<anes>\d{4})\s*\|                           # ANESTHESIA TIME
            \s*(?P<emg>[NY])\s*\|                             # EMG IND
            (?:                                                # Extended columns (optional)
                \s*(?P<oi_allowed>[^|]*)\|                    # OTHER INS ALLOWED (col I)
                \s*(?P<neg_rate>[^|]*)\|                      # NEGOTIATED RATE IND (col II)
                \s*(?P<deductible>[^|]*)\|                    # DEDUCTIBLE AMOUNT (col J)
                \s*(?P<paid>[^|]*)\|                          # PAID AMOUNT (col L)
            )?
            """,
            re.X | re.M
        )

        def _letters_to_pointers(s: str) -> List[int]:
            s = (s or "").strip().upper()
            out: List[int] = []
            for ch in s:
                if "A" <= ch <= "L":
                    out.append((ord(ch) - ord("A")) + 1)
            return out

        items: List[Dict[str, Any]] = []
        for match in line_re.finditer(raw_text):
            mods_raw = match.group("mods") or ""
            modifiers = [z for z in re.findall(r"[A-Z0-9]{2,4}", mods_raw) if z]

            diag_pointer_raw = match.group("diagptr")
            if re.fullmatch(r"[A-L]{1,4}", diag_pointer_raw or ""):
                diag_pointers = _letters_to_pointers(diag_pointer_raw)
            elif diag_pointer_raw and diag_pointer_raw.isdigit():
                # Each digit is a separate diagnosis pointer (e.g., "1230" -> [1, 2, 3])
                diag_pointers = [int(d) for d in diag_pointer_raw if d != "0"]
            else:
                diag_pointers = []

            # Parse extended columns (I, II, J, L) if captured
            oi_allowed_raw = (match.group("oi_allowed") or "").strip()
            neg_rate_raw = (match.group("neg_rate") or "").strip()
            deductible_raw = (match.group("deductible") or "").strip()
            paid_raw = (match.group("paid") or "").strip()

            def _parse_amount(s: str) -> Optional[float]:
                if not s:
                    return None
                if s.startswith("."):
                    s = "0" + s
                try:
                    return float(s)
                except ValueError:
                    return None

            try:
                items.append({
                    "from_date": match.group("from"),
                    "to_date": match.group("to"),
                    "place_of_service": match.group("pos"),
                    "type_of_service": (match.group("typesv") or ""),
                    "cpt_hcpcs": match.group("cpt"),
                    "modifiers": modifiers,
                    "diag_pointer": diag_pointer_raw,        # original pointer (e.g., "ABCD" or "1230")
                    "diag_pointers": diag_pointers,          # derived numeric pointers
                    "charge_amount": float(match.group("charge")),
                    "units": float(match.group("units")),
                    "anesthesia_time": match.group("anes"),
                    "emg_ind": match.group("emg"),
                    "other_ins_allowed": _parse_amount(oi_allowed_raw),
                    "negotiated_rate_ind": neg_rate_raw or None,
                    "deductible_amount": _parse_amount(deductible_raw),
                    "paid_amount": _parse_amount(paid_raw),
                    "provenance": ["payload"],
                    "evidence": _evidence(match.group(0)),
                })
            except Exception:
                logger.debug("Failed to parse Box 24 line", exc_info=True)
                continue

        # Merge control numbers from 24M block by order seen (if any)
        items = self._merge_24m_control_numbers(raw_text, items)
        # Merge EPSDT / Family Planning indicators from 24H block
        items = self._merge_24h_epsdt(raw_text, items)
        # Merge line-level remarks from 24S block
        items = self._merge_24s_remarks(raw_text, items)
        return items

    def _merge_24m_control_numbers(self, raw_text: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Attach line item control numbers from the 24M continuation block, preserving order.

        Expected 24M row shape:
        | 001 | 683211375 | ... |
        If not present, this is a no-op.
        """
        control_pairs = _findall(r"^\s*\|\s*(\d{3})\s*\|\s*(\d+)\s*\|", raw_text, flags=re.M)
        for idx, pair in enumerate(control_pairs):
            if idx < len(items):
                items[idx]["line_item_control_no"] = pair[1]
        return items

    def _merge_24h_epsdt(self, raw_text: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Attach EPSDT IND and FAMILY PLANNING IND from the 24H continuation block.

        Expected 24H row shape:
        | 001 |             |             |
        """
        # Find the 24H section and extract per-line values
        epsdt_re = re.compile(
            r"^\s*\|\s*(\d{3})\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|",
            re.M,
        )
        # Only match rows that appear after the 24H header
        header_match = _search_first(r"24H\s+EPSDT\s+IND", raw_text)
        if not header_match:
            return items
        epsdt_section = raw_text[header_match.start():]
        for match in epsdt_re.finditer(epsdt_section):
            try:
                line_no = int(match.group(1)) - 1  # 0-based index
            except ValueError:
                continue
            if 0 <= line_no < len(items):
                epsdt_val = match.group(2).strip() or None
                fp_val = match.group(3).strip() or None
                items[line_no]["epsdt_ind"] = epsdt_val
                items[line_no]["family_planning_ind"] = fp_val
        return items

    def _merge_24s_remarks(self, raw_text: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Attach line-level remarks from the 24S REMARKS CONT block.

        Expected 24S row shape:
        | 01 |OFFICE OUTPATIENT ESTABLISHED HIGH MDM 40 MIN  REMARK REF CD:  |
        """
        header_match = _search_first(r"24S\s+REMARKS\s+CONT", raw_text)
        if not header_match:
            return items
        section_text = raw_text[header_match.start():]
        # Find the closing table divider after data rows to limit scope
        divider_after_data = re.search(
            r"^\s*\+-{10,}\+\s*$",
            section_text,
            re.M,
        )
        if divider_after_data:
            # Find the LAST divider in the initial chunk (skip header dividers)
            all_dividers = list(re.finditer(
                r"^\s*\+-{10,}",
                section_text,
                re.M,
            ))
            # The closing divider is the one that comes after data rows
            # Data rows match | NN |text|
            last_data_pos = 0
            for dm in re.finditer(r"^\s*\|\s*\d{2}\s*\|", section_text, re.M):
                last_data_pos = dm.end()
            # Find first divider after last data row
            for dv in all_dividers:
                if dv.start() > last_data_pos:
                    section_text = section_text[:dv.start()]
                    break

        remarks_re = re.compile(
            r"^\s*\|\s*(\d{2})\s*\|\s*(.+?)\|\s*$",
            re.M,
        )
        for match in remarks_re.finditer(section_text):
            try:
                line_no = int(match.group(1)) - 1  # 0-based index
            except ValueError:
                continue
            if 0 <= line_no < len(items):
                raw_remark = match.group(2).strip()
                # Skip if it looks like a column header (no alphabetic words)
                if not re.search(r"[A-Z]{3}", raw_remark):
                    continue
                # Split off REMARK REF CD if present
                remark_text = raw_remark
                remark_ref_cd = None
                ref_cd_match = re.search(r"REMARK\s+REF\s+CD:\s*(.*)", raw_remark)
                if ref_cd_match:
                    remark_ref_cd = ref_cd_match.group(1).strip() or None
                    remark_text = raw_remark[:ref_cd_match.start()].strip()
                items[line_no]["remarks"] = remark_text or None
                items[line_no]["remark_ref_cd"] = remark_ref_cd
        return items

    def _extract_totals_block(self, raw_text: str) -> Dict[str, Any]:
        """
        Extract totals and account-level fields (Boxes 25-30).

        Handles:
        1) Row with PD/ALW amounts (earlier sample)
        2) Row without PD/ALW (some layouts)
        """
        out: Dict[str, Any] = {"totals": {}}

        m1 = _search_first(
            r"""
            \|
            \s*(?P<tin>\d{9})\s+(?:EIN|SSN)\s*
            \| \s*(?P<acct>[A-Z0-9]+)\s*
            \| \s*(?P<assign>[A-Z/ ]+?)\s*
            \| \s*(?P<totchg>\d+\.\d{2})\s*
            \| \s*(?P<totpp>\.?\d+\.\d{2})\s*
            \| [^|]*?PD\s*(?P<pd>\.?\d+\.\d{2})\s*ALW\s*(?P<alw>\.?\d+\.\d{2})\s*\|
            """,
            raw_text, flags=re.M | re.S | re.X
        )

        m2 = _search_first(
            r"""
            \|
            \s*(?P<tin>\d{9})\s+(?:EIN|SSN)\s*
            \| \s*(?P<acct>[A-Z0-9]+)\s*
            \| \s*(?P<assign>[A-Z/ ]+?)\s*
            \| \s*(?P<totchg>\d+\.\d{2})\s*
            \| \s*(?P<totpp>\.?\d+\.\d{2})\s*\|
            """,
            raw_text, flags=re.M | re.S | re.X
        )

        match = m1 or m2
        if not match:
            return out

        def _num(s: str) -> float:
            s = s.strip()
            return float(("0" + s) if s.startswith(".") else s)

        out["patients_account_number"] = match.group("acct")
        out["provider_accepts_assignment"] = " ".join(match.group("assign").split())
        out["totals"] = {
            "total_charge": float(match.group("totchg")),
            "total_patient_paid": _num(match.group("totpp")),
            "total_other_insurance": {
                "paid": _num(match.group("pd")) if m1 and match.re == m1.re else None,
                "allowed": _num(match.group("alw")) if m1 and match.re == m1.re else None,
            },
        }
        return out

    def _extract_34_block(self, raw_text: str) -> Dict[str, Any]:
        """
        Extract Pay-to Provider NPI (10 digits only) and Clearinghouse Claim ID (Box 34 line).
        Ensures we never capture 'CLRNG' into NPI when NPI is blank.
        """
        out: Dict[str, Any] = {}
        m = _search_first(
            r"\|34\s+PAY-TO PROVIDER NPI:\s*(?P<payto>\d{10})?\s*(?:CLRNG HOUSE CLAIM ID:\s*(?P<clid>[A-Z0-9]+))?",
            raw_text,
            flags=re.M,
        )
        if not m:
            return out

        pay_to_npi = (m.group("payto") or "").strip()
        clearinghouse_id = (m.group("clid") or "").strip() if "clid" in m.groupdict() else ""

        ev = _evidence(m.group(0))
        if pay_to_npi:
            out["34 PAY-TO PROVIDER NPI"] = {
                "value": pay_to_npi,
                "confidence": 0.90,
                "provenance": ["payload"],
                "evidence": ev,
            }
        if clearinghouse_id:
            out["34 CLRNG HOUSE CLAIM ID"] = {
                "value": clearinghouse_id,
                "confidence": 0.90,
                "provenance": ["payload"],
                "evidence": ev,
            }
        return out

    # ----------------------- LLM parsing (fallback) -----------------------

    def _llm_parse_flat_template(self, raw_text: str, template_name: str) -> Dict[str, Any]:
        """
        Hook to call an LLM-backed parser that returns the "flat template with confidence" shape.
        If no LLM is integrated (self.tool is None), returns a minimal skeleton.
        """
        # If you have a custom LLM tool wired here, call it and return its dict.
        # Example (pseudo):
        # if self.tool and hasattr(self.tool, "invoke"):
        #     return self.tool.invoke({"text": raw_text, "template": template_name})
        # elif callable(self.tool):
        #     return self.tool(text=raw_text, template=template_name)

        # Minimal skeleton to keep structure stable when LLM is unavailable:
        return {
            "template_name": template_name,
            "fields": {},
            "missing_fields": {},
            "extra_fields": {},
            "confidence_scores": {"overall": 0.0, "field_level": {}},
        }

    # ----------------------- Confidence scoring -----------------------

    def _recompute_confidences(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Recompute overall and field-level confidence scores using available values
        (deterministic fields use static confidences; LLM fields carry their own).
        """
        field_level: Dict[str, float] = {}
        values: List[float] = []

        # Existing fields
        for key, meta in (payload.get("fields") or {}).items():
            try:
                conf = float(meta.get("confidence", 0.0))
            except Exception:
                conf = 0.0
            field_level[key] = conf
            values.append(conf)

        # Diagnoses: treat presence as high-confidence deterministic
        for idx, _ in enumerate(payload.get("diagnoses") or []):
            conf = 0.95
            field_level[f"diagnosis_{idx+1}"] = conf
            values.append(conf)

        # Line items: treat presence as high-confidence deterministic
        for idx, _ in enumerate(payload.get("line_items") or []):
            conf = 0.95
            field_level[f"line_item_{idx+1}"] = conf
            values.append(conf)

        # Totals: presence -> high confidence
        if payload.get("totals"):
            conf = 0.95
            field_level["totals"] = conf
            values.append(conf)

        overall = (sum(values) / len(values)) if values else 0.0
        return {"overall": round(overall, 3), "field_level": field_level}


# -----------------------------------------------------------------------------
# LangChain Tool wrapper
# -----------------------------------------------------------------------------

if _HAS_LANGCHAIN:
    from pydantic import BaseModel, Field

    class ClaimParseFlatTemplateInput(BaseModel):
        """Input schema for claim_parse_flat_template_with_confidence tool."""
        claim_data: Dict[str, Any] = Field(
            ...,
            description="DOC360 envelope or raw content. If envelope, must contain `content` key."
        )
        template_name: str = Field(
            "emc_medical",
            description="Template type (default: emc_medical)."
        )

    @tool("claim_parse_flat_template_with_confidence", args_schema=ClaimParseFlatTemplateInput)
    def claim_parse_flat_template_with_confidence(
        claim_data: Dict[str, Any],
        template_name: str = "emc_medical",
    ) -> Dict[str, Any]:
        """
        Parse the given DOC360 envelope/content into a flat template with confidence.

        Steps:
          1) Normalize `claim_data` into a string (DOC360 print image),
          2) Run ElectronicClaimParser with deterministic + LLM merge,
          3) Return a JSON-serializable dict payload.
        """
        parser = ElectronicClaimParser(debug=False)
        raw_text = ElectronicClaimParser.content_to_text(claim_data)
        try:
            result = parser.parse_claim_text_flat_template_with_confidence_json(
                raw_text=raw_text,
                template_name=template_name,
            )
            return result
        except Exception:
            logger.exception("Claim parse tool failed", extra={"template_name": template_name})
            return {
                "status": "error",
                "error": {
                    "code": "PARSE_FAILED",
                    "message": "Parsing failed in claim_parse_flat_template_with_confidence",
                },
                "template_name": template_name,
            }
else:
    # Fallback export for environments without LangChain
    def claim_parse_flat_template_with_confidence(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        """
        Fallback function when LangChain is not installed. This behaves like the Tool
        but without decorator metadata. It parses the claim and returns the same shape.
        """
        parser = ElectronicClaimParser(debug=False)
        claim_data = kwargs.get("claim_data") if not args else args[0]
        template_name = kwargs.get("template_name", "emc_medical")
        raw_text = ElectronicClaimParser.content_to_text(claim_data)
        return parser.parse_claim_text_flat_template_with_confidence_json(
            raw_text=raw_text,
            template_name=template_name,
        )
