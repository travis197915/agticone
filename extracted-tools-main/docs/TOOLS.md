# Tool Catalog — `extracted-tools-main/src/tools`

This document describes **every tool** exposed by the `src/tools/` package:
what it does, the **request schema** (LangChain `args_schema`) the agent must
supply, the **response shape** it returns, and the most important runtime
behavior (auth, caching, error handling).

> Tool wiring: each tool is a `langchain_core.tools.StructuredTool` (or the
> `@tool` decorator). Agents see the tool **name + description + args_schema**.
> The "response" shapes below are the Python dicts (JSON-serializable) that the
> tool actually returns to the agent — they typically follow a Pydantic schema
> in `src/tools/schemas/`.

---

## Quick index

| #   | Tool name (agent-visible)                   | Source file                                        | Purpose (one line)                                        |
| --- | ------------------------------------------- | -------------------------------------------------- | --------------------------------------------------------- |
| 1   | `doc360_read_claim_by_fln_dcc`              | `claim_tool.py`                                    | Read DOC360 claim document by FLN/DCC                     |
| 2   | `facets_get_summary`                        | `facets_tool.py`                                   | Facets claim summary header (group/member/provider/dates) |
| 3   | `facets_get_cob`                            | `facets_tool.py`                                   | Facets Coordination of Benefits data                      |
| 4   | `facets_get_line_details`                   | `facets_tool.py`                                   | All service-line details (iterates seq 1..N)              |
| 5   | `facets_get_member_eligibility`             | `facets_tool.py`                                   | Member eligibility (resolves MEME_CK from summary)        |
| 6   | `facets_get_provider_details`               | `facets_tool.py`                                   | Provider search via stored proc + DOC360 NPI filter       |
| 7   | `facets_get_duplicate_claim`                | `facets_tool.py`                                   | Duplicate-claim search filtered by line DOS               |
| 8   | `facet_extension_portal_provider`           | `facet_extension_portal_tool.py`                   | BH provider complete list by PRPR ID                      |
| 9   | `facet_extension_portal_programme`          | `facet_extension_portal_tool.py`                   | Programme details by Program Detailed ID                  |
| 10  | `facet_ext_portal_group_model`              | `facet_extension_portal_tool.py`                   | Network fee-schedule group model (e.g. `1A`)              |
| 11  | `check_medicare_coverage`                   | `cbd_tool.py`                                      | CBD CPT coverage check (LOB-aware)                        |
| 12  | `check_diagnosis_coverage`                  | `diagnosis_tool.py` (& duplicate in `cbd_tool.py`) | ICD diagnosis coverage check                              |
| 13  | `linx_claim_search`                         | `linx_tool.py`                                     | LINX BH claim search by subscriber                        |
| 14  | `medicare_optout_checker`                   | `opt_out_tool.py`                                  | CMS Medicare provider opt-out lookup                      |
| 15  | `check_cross_prevalence_billing`            | `cross_prevalence_billing_tool.py`                 | Cross-Billing Prevailing Code List lookup                 |
| 16  | `save_sop_step`                             | `sop_step_persistence_tool.py`                     | Persist one SOP-step execution row                        |
| 17  | `llm_parse_claim_with_ontology`             | `llm_claim_parser.py`                              | LLM-first HCFA extraction (ontology + JSON schema)        |
| 18  | `claim_parse_flat_template_with_confidence` | `electronic_claim_parser.py`                       | Regex-first HCFA parse (optional LLM merge)               |

Supporting clients (not LangChain-wrapped here): `cbd_api_client.py`,
`diagnosis_api_client.py`, `npi_api_client.py`, `lob_determination.py`,
`cbd_config.py`, `claim_templates.py`, `upload_era_file.py`.

---

## 1. `doc360_read_claim_by_fln_dcc`

- **File**: `claim_tool.py` (decorator `@tool("doc360_read_claim_by_fln_dcc")`)
- **Schema (input)**: `ClaimReadByFlnInput` (local) — `schema_claim_tool.py` for output.
- **Purpose**: Authenticate with DOC360 (OAuth2 client-credentials) and read
  the claim "print image" by FLN/DCC. Tries DOC360 classifiers
  `u_keyed_claim` → `u_edi_claim` → `u_clm_corsp_lwso_doc` in order; returns
  the first envelope with content.

### Request

```json
{
  "fln_dcc": "string  // FLN/DCC identifier (10–16 digit numeric)"
}
```

### Response (`ClaimReadOutput` / `ClaimContentEnvelope`)

```json
{
  "lookupId": "string | null", // usually FLN/DCC
  "status": "success | error",
  "httpStatus": 200,
  "doc360TypeName": "u_keyed_claim | u_edi_claim | u_clm_corsp_lwso_doc | null",
  "content": "string | object | array | null", // raw DOC360 body (typically the print image)
  "metadata": {
    "responseTimeMs": 123,
    "contentType": "application/json",
    "httpHeaders": { "...": "..." }
  },
  "error": {
    // present when status='error'
    "code": "string | null",
    "message": "string",
    "triedTypeNames": ["u_keyed_claim", "u_edi_claim", "u_clm_corsp_lwso_doc"]
  }
}
```

### Behavior

- OAuth token cached in-process (`Doc360Client._token`, refresh 60 s before expiry).
- 3-retry loop on transient errors; on 401, drops token + retries once.
- Successful results are **`ToolCache`-cached** (key = `{fln_dcc}`).
- Side effect: writes `data/doc360_envelope-<id>.json` and
  `data/doc360_content-<id>.txt` for traceability.
- Required env: `DOC360_TOKEN_URL`, `DOC360_API_BASE`, `DOC360_CLIENT_ID`,
  `DOC360_CLIENT_SECRET`, `DOC360_SCOPE`, `DOC360_READ_DOCUMENT_CONTENT`,
  `DOC360_APP_ID`, `DOC360_USER_ID`, `UPSTREAM_ENV`.

---

## 2. `facets_get_summary`

- **File**: `facets_tool.py`
- **Schema**: `ClaimNumberInput` (`schema_facets_tool.py`)
- **Purpose**: Fetch comprehensive claim summary from Facets (group, group id,
  subscriber id, member name, provider name/NPI/TIN, dates, diagnoses, line
  summaries).
- **Upstream call**: `GET {FACETS_BASE_URL}/Claims/{claim_number}/Inquiry/Summary`
  with `Authorization: Bearer <facets_token>`.

### Request

```json
{ "claim_number": "25XG44660400" }
```

### Response

```json
{
  "status_code": 200,
  "status_message": "string | null",
  "body": {
    /* upstream Facets JSON (Data.ClaimSummary.REC_CIV8 …) */
  },
  "claim_number": "25XG44660400",
  "endpoint": "summary",
  "timestamp": 1716470000
}
```

Error shape:

```json
{
  "error": "Token error | <upstream message>",
  "message": "same",
  "status_code": 4xx/5xx | null,
  "status_message": "string | null",
  "claim_number": "string",
  "endpoint": "summary",
  "timestamp": 1716470000,
  "token_status": { "status": "failed", "...": "..." } // present only on auth failures
}
```

### Behavior

- Facets OAuth token cached in-process (`_FACETS_TOKEN_TTL_SECONDS = 50 min`).
- Successful summaries cached via `ToolCache` keyed by `claim_number`.
- Required env: `FACETS_BASE_URL`, `FACETS_USERNAME`, `FACETS_PASSWORD`,
  `FACETS_REGION`, `FACETS_IDENTITY`, `FACETS_SIGNON_METHOD`.

---

## 3. `facets_get_cob`

- **File**: `facets_tool.py`
- **Schema**: `ClaimNumberInput`
- **Purpose**: Fetch Coordination of Benefits data for the claim.
- **Upstream call**: `GET {FACETS_BASE_URL}/Claims/{claim_number}/Inquiry/COB`.

### Request

```json
{ "claim_number": "25XG44660400" }
```

### Response

Same envelope as `facets_get_summary` with `endpoint: "cob"`. The `body`
contains upstream COB payload (may be empty / 404 when there is no other
insurance — this is expected).

### Behavior

- Cached per claim, same auth/token as summary.
- 404 is surfaced as an error envelope (no body); upstream describes "no COB"
  conditions.

---

## 4. `facets_get_line_details`

- **File**: `facets_tool.py`
- **Schema**: `ClaimNumberInput`
- **Purpose**: Fetch **all** service-line details by iterating
  `Lines/1/Details`, `Lines/2/Details`, … until 404. Returns aggregated
  per-line bodies (dates, CPT, units, charges, paid amounts).
- **Upstream call**: `GET {FACETS_BASE_URL}/Claims/{claim_number}/Inquiry/Lines/{seq}/Details`.

### Request

```json
{ "claim_number": "25XG44660400" }
```

### Response

```json
{
  "claim_number": "25XG44660400",
  "endpoint": "line_details",
  "timestamp": 1716470000,
  "first_missing_line_seq": 6, // 0/None when MAX_LINE_SEQ exhausted
  "total_lines": 5,
  "items": [
    {
      "line_seq": 1,
      "status_code": 200,
      "body": {
        /* upstream line-detail JSON */
      }
    }
    /* … one entry per fetched line */
  ]
}
```

### Behavior

- Loop bound: `MAX_LINE_SEQ` (env, default `100`).
- Cached per claim.

---

## 5. `facets_get_member_eligibility`

- **File**: `facets_tool.py`
- **Schema**: `ClaimNumberInput`
- **Purpose**: Resolve member eligibility for the claim. Internally calls
  `facets_get_summary` first to extract `MEME_CK`, then queries the eligibility
  endpoint.
- **Upstream call**: `GET {FACETS_BASE_URL}/Members/Coverage/MemberKey/{MEME_CK}/Eligibility`.

### Request

```json
{ "claim_number": "25XG44660400" }
```

### Response

Same envelope shape as the other Facets tools, with
`endpoint: "member_eligibility"`. `body` contains plan effective/term dates and
eligibility indicators for the relevant period.

If `MEME_CK` can't be resolved from the summary:

```json
{
  "error": "Could not extract MEME_CK",
  "claim_number": "string",
  "endpoint": "member_eligibility"
}
```

---

## 6. `facets_get_provider_details`

- **File**: `facets_tool.py`
- **Schema**: `ProviderDetailsInput` (`schema_facets_tool.py`)
- **Purpose**: Look up provider rows via the stored procedure
  `CMCSP_PRV1_SRCH_PRPR_NAME_REMT`, then **filter** rows by `PRPR_NPI`
  matching ANY of the DOC360-parsed NPI fields
  (`24 CONTINUED RENDERING NPI`, `33 NPI`, `11 NPI`, or per-line
  `rendering_npi`/`svc_npi`).
- **Upstream call**: `POST {FACETS_BASE_URL}/data/procedure/execute` with
  procedure parameters `PRPR_ENTITY`, `MCTN_ID`, plus `%` wildcards for name,
  city, state, zip.

### Request

```json
{
  "claim_number_for_reference": "25XG44660400", // REQUIRED — used to auto-resolve missing inputs
  "provider_entity_type": "P | G | I | F | null", // PRPR_ENTITY; auto-resolved from summary if null
  "tax_id": "string | null" // From DOC360 `25 FEDERAL TAX ID#`; auto-resolved if null
}
```

`ProviderDetailsInput` accepts these aliases too: `PRPR_ENTITY`, `prpr_entity`,
`provider_tin`, `tin`, `federal_tax_id`, `MCTN_ID`.

### Response

```json
{
  "status_code": 200,
  "status_message": "string | null",
  "body": {
    "Data": {
      "ResultSets": [
        {
          "Rows": [
            /* filtered provider rows */
          ],
          "RowCount": 3
        }
      ],
      "TotalRowCount": 3
    }
  },
  "provider_entity_type": "P",
  "tax_id": "123456789",
  "claim_number_for_reference": "25XG44660400",
  "doc360_rendering_npis": ["1234567890", "..."],
  "filter_applied": true,
  "rows_before_filter": 12,
  "rows_after_filter": 3,
  "filter_skipped_reason": "",
  "endpoint": "provider_details",
  "timestamp": 1716470000
}
```

### Behavior

- Auto-fetches DOC360 parse output via the (missing) orchestrator
  `claim_micro_image_id_to_fln_dcc_doc360_parse` to populate `tax_id` and
  NPI list when not provided. (See "Missing pieces" in `src/README.md`.)
- Validates `provider_entity_type ∈ {P, G, I, F}`.
- Cache key includes `provider_entity_type`, `tax_id`,
  `claim_number_for_reference`, `doc360_rendering_npis`, and a
  `filter_version` token.

---

## 7. `facets_get_duplicate_claim`

- **File**: `facets_tool.py`
- **Schema**: `ClaimNumberInput`
- **Purpose**: Find potential duplicates by searching with
  `(SubscriberID, GroupID, ClaimType)` extracted from the summary, then
  filter candidates **per line item** using service date ranges:
  `CDML_FROM_DT ≥ CLCL_LOW_SVC_DT` AND `CDML_TO_DT ≤ CLCL_HIGH_SVC_DT`.
- **Upstream call**: `GET {FACETS_BASE_URL}/Search/Claims/Inquiry?SubscriberID=…&GroupID=…&ClaimType=…`.

### Request

```json
{ "claim_number": "25XG44660400" }
```

### Response

```json
{
  "status_code": 200,
  "status_message": "string | null",
  "claim_number": "25XG44660400",
  "endpoint": "duplicate_claim",
  "timestamp": 1716470000,
  "search_params": {
    "SubscriberID": "string",
    "GroupID": "string",
    "ClaimType": "string"
  },
  "total_claims_found_before_filtering": 12,
  "total_line_items": 3,
  "line_items": [
    {
      "CDML_SEQ_NO": 1,
      "CDML_FROM_DT": "2025-01-24T00:00:00",
      "CDML_TO_DT": "2025-01-24T00:00:00",
      "filtered_claims_count": 1,
      "filtered_claims": [
        /* matching candidate rows */
      ]
    }
  ]
}
```

If summary lacks `SBSB_ID` / `GRGR_ID` / `CLCL_CL_SUB_TYPE`, returns an
`error` envelope listing what could / could not be extracted.

---

## 8. `facet_extension_portal_provider`

- **File**: `facet_extension_portal_tool.py`
- **Schema**: `ProviderInput` (`schema_facet_extension_portal.py`)
- **Purpose**: Retrieve the full BH provider list (all program detailed IDs)
  for a given Provider ID (PRPR ID).
- **Upstream call**: `GET {FACET_EXTENSION_PORTAL_BASE_URL}/getCompleteList/{provider_id}`.

### Request

```json
{ "provider_id": "FAC000022500" }
```

### Response

Success:

```json
{
  "success": true,
  "provider_id": "FAC000022500",
  "status_code": 200,
  "data": {
    /* upstream JSON */
  }
}
```

Failure:

```json
{
  "success": false,
  "provider_id": "FAC000022500",
  "error": "<requests.exceptions message>",
  "message": "Failed to retrieve data for provider ID: FAC000022500"
}
```

### Behavior

- Successful responses cached per `provider_id`.
- SSL verify disabled by default (`verify_ssl=False`).

---

## 9. `facet_extension_portal_programme`

- **File**: `facet_extension_portal_tool.py`
- **Schema**: `ProgrammeInput`
- **Purpose**: Get a single programme's details for the supplied Program
  Detailed ID (EDS_PRPR_PRGM_DET_ID).
- **Upstream call**: `GET {FACET_EXTENSION_PORTAL_BASE_URL}/getPrgm/{program_detailed_id}`.

### Request

```json
{ "program_detailed_id": "276728" }
```

### Response

```json
{
  "success": true,
  "program_detailed_id": "276728",
  "status_code": 200,
  "data": {
    /* upstream JSON */
  }
}
```

Error envelope mirrors tool 8.

---

## 10. `facet_ext_portal_group_model`

- **File**: `facet_extension_portal_tool.py`
- **Schema**: `GroupModelInput`
- **Purpose**: From a claim number, extract `PRPR_ID` via Facets summary, then
  fetch the **group model** string (e.g. `"1A"`) from the staging network
  fee-schedule API.
- **Upstream call**: `GET https://network-fee-sch-api-stg.hcck8s-ctc-np1.optum.com/common/checkModel/{PRPR_ID}`
  (hardcoded staging host).

### Request

```json
{ "claim_number": "25XG44660400" }
```

### Response

```json
{
  "success": true,
  "status_code": 200,
  "meta_data": "Group model lookup for provider FAC000022500",
  "prpr_id": "FAC000022500",
  "group_model": "1A",
  "claim_number": "25XG44660400",
  "endpoint": "group_model",
  "timestamp": 1716470000
}
```

On failure (timeout / connection / non-200), the same envelope is returned
with `success=false`, `group_model=null`, and `error` / `message` fields.

---

## 11. `check_medicare_coverage`

- **File**: `cbd_tool.py`
- **Schema**: `CBDCoverageInput` (`schema_cbd_tool.py`)
- **Purpose**: Check CPT coverage in the CBD (Covered Benefit Document) API.
  Dynamically resolves Line of Business (LOB) from `claim_id`, similarity-
  matches group/plan names against the per-LOB customer catalog, then queries
  coverage. Returns per-CPT coverage detail.
- **Upstream calls**:
  - OAuth2 token from `CBD_TOKEN_URL` (client credentials).
  - `POST {CBD_API_URL}` with `CBDConfig.build_payload(...)` (msid, lobs,
    markets, products, custNames, plans, advancedFilters).

### Request

```json
{
  "cpt_codes": ["99213", "99214", "G0548"], // required, non-empty, upper-cased server-side
  "group_name": "Standard Medicare", // optional, default 'Standard Medicare'
  "plan_name": "Standard Medicare", // optional, default 'Standard Medicare'
  "claim_id": "25XG44660400" // optional; triggers determine_lob()
}
```

### Response (`CBDCoverageOutput`)

```json
{
  "success": true,
  "group_name": "Standard Medicare",
  "plan_name": "Standard Medicare",
  "total_codes_queried": 3,
  "codes_found": 2,
  "coverage_details": [
    {
      "cpt_code": "99213",
      "covered": "Yes",
      "authorization": "No",
      "desc_name": "OFFICE OUTPT EST 20-29 MIN",
      "service_type": "OUTPT",
      "asam_level": null,
      "diagnosis": null,
      "effective_date": "2025-01-01",
      "term_date": null,
      "lob": "UHC M&R",
      "market": "ALL"
    }
  ],
  "not_found_codes": ["G0548"],
  "errors": []
}
```

### Behavior

- LOB resolution: when `claim_id` is provided, calls
  `tools.lob_determination.determine_lob(claim_id)` to choose product
  (`Medicare | Medicaid | Commercial`). Then product-specific groups/plans are
  used as similarity candidates. LOB defaults if no match:
  - Medicare → `Standard Medicare / Standard Medicare`
  - Commercial → `Standard Commercial / Standard Commercial`
  - Medicaid → `RI Medicaid / RI Medicaid`
- Cosine + word-similarity matching (`MIN_SIMILARITY_PERCENT = 80`), with
  TF-IDF fallback.
- Filesystem cache: `data/tool_cache/check_medicare_coverage/{sha256(input)}.json`.
- On config or network error, returns `success=false` with `errors[]` populated.

---

## 12. `check_diagnosis_coverage`

- **Files**: `diagnosis_tool.py` (primary) and a duplicate exported from
  `cbd_tool.py` (prefer `diagnosis_tool.py`).
- **Schema**: `DiagnosisInput` / `DiagnosisOutput` (`schema_diagnosis_tool.py`).
- **Purpose**: Look up coverage info for an ICD-10 diagnosis code via the
  Covered Diagnosis API.
- **Upstream call**: `POST {DIAGNOSIS_API_URL}` with body
  `{"globalFilter": "", "filters": [{"id": "code", "value":[{"condition":"equals","filterValue": "<code>"}]}]}`
  using the same OAuth2 token as CBD.

### Request

```json
{ "diagnosis_code": "E11.9" } // ICD-10
```

### Response

```json
{
  "success": true,
  "diagnosis_code": "E11.9",
  "result": {
    "diagnosis_code": "E11.9",
    "code_type": "Type1, Type2", // joined type1/type2/type3 (skipping 'N/A')
    "covered": "Yes | No", // 'Yes' iff API field 'coverageRecommend' contains 'Cover Services'
    "description": "string | null"
  },
  "error": null
}
```

When the API returns nothing or the code isn't in the coverage DB, the tool
still returns `success=true` with `covered: "No"` and
`description: "Diagnosis code not found in coverage"`. On config/exception:
`success=false` with `error` populated.

### Behavior

- Filesystem cache: `data/tool_cache/check_diagnosis_coverage/{sha256(code)}.json`.

---

## 13. `linx_claim_search`

- **File**: `linx_tool.py`
- **Schema**: `LinxClaimSearchInput` / `LinxClaimSearchOutput` (`schema_linx_tool.py`).
- **Purpose**: Search BH claims in LINX by subscriber ID (optionally narrowed
  by name, DOB, date range, UNET policy, external account IDs).
- **Upstream call**: `POST {LINX_API_URL}` with bearer token and a
  `bhRequestHeader` JSON string (`applicationId=obhagenticai`, `dataSource=prod`).

### Request

```json
{
  "subscriber_id": "string", // required (alias subscriberId)
  "first_name": "string | null",
  "last_name": "string | null",
  "dob": "MM/DD/YYYY | null",
  "start_date": "MM/DD/YYYY | null", // alias startDate
  "end_date": "MM/DD/YYYY | null", // alias endDate
  "unet_policy_nbr": "string | null", // alias unetPolicyNbr
  "claim_max_limit": 0, // 0 = no limit; alias claimMaxLimit
  "external_account_id_list": [
    // alias externalAccountIdList
    {
      "facetsAltId": "string",
      "riosAcctId": "string",
      "unetPolicyNbr": "string",
      "cosmosGrpId": "string"
    }
  ]
}
```

### Response (`LinxClaimSearchOutput`)

```json
{
  "success": true,
  "data": {
    /* upstream LINX response (lists are wrapped under {"results": [...]} ) */
  },
  "error": null,
  "cache_hit": false
}
```

Failure:

```json
{
  "success": false,
  "data": null,
  "error": "LINX API error: HTTP 403",
  "cache_hit": false
}
```

### Behavior

- OAuth2 token cached in-process per module.
- Result file cache (24 h TTL) in `data/tool_cache/linx_claim_search/{hash}.json`.
- Required env: `LINX_AUTH_URL`, `LINX_CLIENT_ID`, `LINX_CLIENT_SECRET`,
  `LINX_API_URL` (also read from `config.settings`).

---

## 14. `medicare_optout_checker`

- **File**: `opt_out_tool.py`
- **Schema**: `ProviderOptOutInput` / `APIResponse` / `ProviderRecord`
  (`schema_optout.py`).
- **Purpose**: Look up Medicare provider opt-out records in the CMS Provider
  Opt-Out Affidavits dataset by NPI or by name (with optional state/specialty).
- **Upstream call**: `GET {CMS_API_BASE_URL}/{CMS_DATASET_ID}/data?filter[NPI]=…`
  or `filter[filter-N][condition][...]` parameters for name searches.

### Request

```json
{
  "npi": "string | null", // 10 digits; validated
  "first_name": "string | null",
  "last_name": "string | null", // last_name OR npi must be present
  "state": "CA | NY | ... | null", // 2-letter
  "specialty": "string | null"
}
```

### Response (returned as a **JSON string** from `check_medicare_optout_status`)

When records found, an array of records:

```json
[
  {
    "provider_name": "JANE DOE",
    "npi": "1234567890",
    "optout_effective_date": "01/01/2024",
    "optout_end_date": "12/31/2025",
    "optout_status": "Yes | No (Expired) | Unknown",
    "renewal_info": "08/01/2024"
  }
]
```

When no record exists:

```json
{ "message": "No opt-out record found", "records": [] }
```

On error:

```json
{ "error": "API request timed out. Please try again." }
```

### Behavior

- `optout_status` is computed from `optout_end_date` vs. `datetime.now()`.
- Validates NPI / state / name format up-front.
- Built with `handle_tool_error=True` and `handle_validation_error=True` so
  validation failures surface as a tool-error message rather than raising.

---

## 15. `check_cross_prevalence_billing`

- **File**: `cross_prevalence_billing_tool.py`
- **Schema**: `CrossPrevalenceBillingInput` / `CrossPrevalenceBillingResult`
  (defined inline in the same file).
- **Purpose**: Look up cross-prevalence billing restrictions for a CPT pair
  (with optional embedded modifiers). On first call, bootstraps a SQL table
  from the seed Excel and also stores its "Revision History" sheet.
- **Backend**: SQL Server via `pymssql`; uses `agents.db_reporting.ensure_tables`
  for DDL.

### Request

```json
{
  "sql_dsn": "server:port;database;user;password", // required
  "cpt_code_a": "99213", // or compact numeric e.g. '0010459' → CPT 00104 + mod 59
  "cpt_code_b": "99214",
  "excel_path": "/abs/path/to/seed.xlsx" // optional; used only on bootstrap
}
```

### Response (`CrossPrevalenceBillingResult`)

```json
{
  "ok": true,
  "found": true,
  "cpt_pay":  "99213 | null",
  "cpt_deny": "99214 | null",
  "modifier": "Allowed with 25 | Not allowed | ... | null",
  "message":  "human-readable policy text or status note",
  "matches":  [
    { "cpt_pay": "99213", "cpt_deny": "99214", "modifier": "Allowed with 25" }
  ],
  "required_modifiers": ["59"],            // derived from compact numeric inputs
  "modifiers_satisfied": true | false | null,
  "missing_required_modifiers": [],
  "bootstrapped": false                     // true on the first call that loaded the Excel
}
```

### Behavior

- Compact numeric input: if `cpt_code_*` has > 5 digits, first 5 = CPT, last 2
  = required modifier (e.g., `0010459` → CPT `00104` + modifier `59`).
- On first call: parses the seed Excel (scans for `CPT Pay / CPT Deny /
Modifier` headers), bulk inserts into `cross_prevalence_billing_codes`, and
  stores the second sheet into `cross_prevalence_billing_rev_history`.
- Subsequent calls hit SQL only.
- Returns `ok=false` with `message` if SQL connection or lookup fails.

---

## 16. `save_sop_step`

- **File**: `sop_step_persistence_tool.py`
- **Schema**: `SaveSopStepInput` (Pydantic v2).
- **Purpose**: **Idempotent upsert** of one SOP-step row into SQL Server table
  `sop_step_executions`, including tool arrays as JSON columns
  (`NVARCHAR(MAX)`).
- **Idempotency key**: `(execution_id, claim_id, agent_name, sop_step_number)`.

### Request

```json
{
  "sql_dsn": "server:port;database;user;password", // required
  "execution_id": "string", // required
  "claim_id": "string", // required
  "agent_name": "string", // required

  "sop_name": "string | null",
  "sop_step_number": 1,
  "sop_step_name": "string | null",
  "sop_rule_id": "string | null",
  "sop_step_description": "string | null",
  "sop_action": "string | null",
  "step_exec_status": "string | null",
  "status": "string | null",
  "result_summary": "string | null",
  "rationale": "string | null",
  "evidence_refs": ["doc:abc", "facets:summary"],

  "timestamp": "2025-05-23T14:00:00Z", // ISO-8601 (Z OK)
  "started_at": "2025-05-23T13:59:00Z",
  "ended_at": "2025-05-23T14:00:00Z",
  "transaction_time_sec": 60.0,

  "tools_used": ["facets_get_summary", "..."],
  "tools_succeeded": ["facets_get_summary"],
  "tools_failed": [],
  "tools_skipped": [],
  "tool_error_details": { "tool_name": "error message" }
}
```

### Response

```json
{ "ok": true,  "action": "inserted | updated", "row_id": 1234 }
// or
{ "ok": false, "skipped": true,  "reason": "no_sql_connection" }
// or
{ "ok": false, "skipped": false, "reason": "<sql exception>" }
```

### Behavior

- Timestamps normalized to **naive UTC** for `DATETIME2`.
- All array/dict fields encoded with `json.dumps(...)` and explicitly CAST to
  `NVARCHAR(MAX)` to avoid 4000-char truncation.
- Performs `SELECT TOP 1 id ... WHERE <idempotency key>` then either UPDATE or
  INSERT; returns `SCOPE_IDENTITY()` after insert.

---

## 17. `llm_parse_claim_with_ontology`

- **File**: `llm_claim_parser.py`
- **Schema (input)**: `LlmClaimParseInput` (defined inline).
- **Purpose**: LLM-first structured extraction of an HCFA-1500 print image
  (DOC360 content) into a canonical, schema-validated payload (fields,
  diagnoses, line items, totals, other insurance, HCP pricing). Uses an
  ontology (YAML or dict) for label-resilient mapping and a JSON Schema to
  constrain the LLM output.
- **LLM**: Azure OpenAI (`core.model`); model resolved from
  `AGENT_MODEL_MAP` / `LLM_CLAIM_PARSER_AGENT_NAME`, falling back to
  `CHAT_DEPLOYMENT`. JSON mode enabled when the registry model kind is
  `azure_openai` / `openai_compat`.

### Request

```json
{
  "claim_data": {
    "content": "<DOC360 print image text>"
    /* … or any envelope; the tool normalizes via content_to_text */
  },
  "template_name": "emc_medical", // default
  "ontology": { "...": "..." }, // optional dict; else loads YAML
  "ontology_path": "/abs/path/claim_ontology.yaml" // optional
}
```

### Response (matches `PAYLOAD_SCHEMA` in the file)

```json
{
  "template_name": "emc_medical",
  "claim_source": "physician",
  "fields": {
    "<canonical key>": {
      "value": "string | number | null | array",
      "confidence": 0.95,
      "provenance": { "evidence": "<=200 chars" }
    }
  },
  "diagnoses": [{ "pointer": 1, "code": "F332", "provenance": { "evidence": "..." } }],
  "line_items": [
    {
      "from_date": "030325",
      "to_date": "030325",
      "place_of_service": "11",
      "type_of_service": "",
      "cpt_hcpcs": "99215",
      "modifiers": ["25"],
      "diag_pointer": "1230",
      "diag_pointers": [1, 2, 3],
      "charge_amount": 445.0,
      "units": 1.0,
      "anesthesia_time": "0000",
      "emg_ind": "N",
      "line_item_control_no": "683211375",
      "other_ins_allowed": null,
      "negotiated_rate_ind": null,
      "deductible_amount": null,
      "paid_amount": null,
      "epsdt_ind": "N",
      "family_planning_ind": "N",
      "remarks": "OFFICE OUTPATIENT ...",
      "remark_ref_cd": null,
      "rendering_npi": "1234567890",
      "svc_npi": "1234567890",
      "provenance": { "evidence": "..." }
    }
  ],
  "totals": {
    "total_charge": 445.0,
    "total_patient_paid": 0.0,
    "total_other_insurance": { "paid": null, "allowed": null }
  },
  "other_insurance": { "plan_name": "...", "claim_number": "...", "total_oi_paid": 0.0, "remark_codes": [] },
  "hcp_pricing": { "icn": "...", "price_method": "...", "repriced_allowed_amt": 0.0, "reject_code": "" },
  "unmapped_fields": { "<form key>": { "value": "...", "provenance": { "evidence": "..." } } },
  "confidence_scores": { "overall": 0.85, "field_level": { "<key>": 0.95 } },
  "_error": null, // present only on LLM failure
  "_schema_error": null // present only if jsonschema validation failed
}
```

### Behavior

- ICD-10, NPI, money post-validation (drops invalid ICDs, blanks bad NPIs,
  coerces amounts to floats).
- `ToolCache` keyed by `sha256(text) + template_name`; only success results
  (no `_error`) are cached.
- Falls back to a stable empty payload on LLM failure (with `_error`).

---

## 18. `claim_parse_flat_template_with_confidence`

- **File**: `electronic_claim_parser.py` (`@tool("claim_parse_flat_template_with_confidence")`)
- **Schema (input)**: `ClaimParseFlatTemplateInput`.
- **Purpose**: Deterministic regex-first HCFA print-image parser. Optionally
  merges with an LLM result (deterministic values win). Designed to suppress
  hallucinations while preserving a stable structured output.

### Request

```json
{
  "claim_data": {
    "content": "<DOC360 print image text>"
  },
  "template_name": "emc_medical" // default
}
```

### Response

Shape mirrors `llm_parse_claim_with_ontology`'s payload (regex fills `fields`,
`diagnoses`, `line_items`, `totals`, and `confidence_scores`). On parser
exception:

```json
{
  "status": "error",
  "error": { "code": "PARSE_FAILED", "message": "..." },
  "template_name": "emc_medical"
}
```

### Notable extractions

- Header inline: `HIC#`, `RTE`, `ATTCH` (sliced between labels to avoid bleed).
- `6 PAT RELATION TO INSURED` (e.g., `01/SELF`).
- `7C PAYOR ID` + `SPC` (e.g., `87726 F/COMMERCIAL`).
- `7E INSURANCE ADDRESS` (right-column only).
- `9B` / `9C` plan name, code, filing IND, description.
- Box 21 diagnoses (multi-style: `1 F332`, `1|A F411`, letter-only `E E118`).
- Box 24 line items (letter or numeric diag pointers; integer or decimal
  units; merges `24M` control numbers / SVC NPI, `24H` EPSDT/FP, `24S` remarks).
- Box 25–30 totals (with optional PD/ALW); Box 34 pay-to NPI and clearinghouse
  claim id.

---

## Cross-cutting concerns

### Auth

| Tool                                                  | Mechanism                                                                     | Token TTL                                       |
| ----------------------------------------------------- | ----------------------------------------------------------------------------- | ----------------------------------------------- |
| `doc360_*`                                            | OAuth2 client-credentials + `Doc360-*` headers                                | refresh 60 s pre-expiry                         |
| `facets_*`                                            | `POST /security/tokens` with username/password + Region/Identity/SignonMethod | 50 min in-process cache                         |
| `check_medicare_coverage`, `check_diagnosis_coverage` | OAuth2 client-credentials at `CBD_TOKEN_URL`                                  | per call (no in-process token cache)            |
| `linx_claim_search`                                   | OAuth2 client-credentials at `LINX_AUTH_URL`                                  | TTL from `expires_in` (refresh 60 s pre-expiry) |
| `medicare_optout_checker`                             | None (open CMS dataset)                                                       | n/a                                             |
| `facet_extension_portal_*`                            | None (internal API)                                                           | n/a                                             |
| `save_sop_step`, `check_cross_prevalence_billing`     | SQL DSN                                                                       | n/a                                             |

### Caching

| Layer                                           | Tools                                                                                                                    |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| In-memory `ToolCache`                           | DOC360, all Facets, CBD coverage (`cbd_coverage` namespace), LOB determination, Facet extension portal, LLM claim parser |
| Filesystem `data/tool_cache/{tool}/{hash}.json` | `check_diagnosis_coverage` (no TTL), `check_medicare_coverage` (no TTL), `linx_claim_search` (24 h TTL)                  |
| Disk artifacts                                  | `data/doc360_envelope-*.json`, `data/doc360_content-*.txt`                                                               |

### Error envelopes

Most HTTP-backed tools follow a "success envelope + error envelope" pattern
returned **as a dict** (not raised) so the agent can read `status_code` /
`error` / `message` and decide. The two SQL-backed tools (`save_sop_step`,
`check_cross_prevalence_billing`) return `ok: bool` plus a `reason` /
`message`.

### Missing dependencies (referenced but **not** in this extract)

- `tools.claim_micro_image_id_to_fln_dcc_doc360_parse` — used by
  `facets_get_provider_details` to auto-resolve DOC360 NPIs/TIN.
- `core.model` — Azure OpenAI client + model registry for
  `llm_parse_claim_with_ontology`.
- `agents.db_reporting.ensure_tables` — used by
  `check_cross_prevalence_billing`.
- `config.settings` — used by `linx_claim_search`.
- `npi_tool.py` (only the client + schema remain here; not wrapped as a
  StructuredTool in this extract).

See `src/README.md` "What is not in this repo (but required at runtime)" for
the full list and the system overview.
