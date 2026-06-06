# Facets tools — URLs

Six LangChain tools in `agent_tools/tools/facets_tool.py`. Each section lists:

1. **Invoke URL** — this Django backend (`POST`, JWT required)
2. **Upstream URL** — Facets API called by the tool (`FACETS_BASE_URL` from env)

Placeholders:

| Placeholder | Example |
|-------------|---------|
| `{BASE_URL}` | `http://localhost:8000` |
| `{FACETS_BASE_URL}` | `http://localhost:8000/api/mocks/facets` (local mock) or real Facets host |
| `{claim_number}` | `25XG44660400` |
| `{meme_ck}` | Member key from claim summary (tool resolves automatically) |
| `{seq}` | Line sequence `1`, `2`, … (tool iterates until 404) |

Shared auth (all upstream calls except token issuance):

```
Authorization: Bearer {facets_access_token}
```

Token is fetched once per ~50 min via:

```
POST {FACETS_BASE_URL}/security/tokens
```

---

## 1 — `facets_get_summary`

Claim summary header (group, subscriber, provider IDs, dates, etc.).

| Layer | Method | URL |
|-------|--------|-----|
| **Invoke** | `POST` | `{BASE_URL}/api/agent-tools/facets_get_summary/invoke` |
| **Upstream** | `GET` | `{FACETS_BASE_URL}/Claims/{claim_number}/Inquiry/Summary` |

**Invoke body**

```json
{ "args": { "claim_number": "{claim_number}" } }
```

---

## 2 — `facets_get_cob`

Coordination of Benefits data.

| Layer | Method | URL |
|-------|--------|-----|
| **Invoke** | `POST` | `{BASE_URL}/api/agent-tools/facets_get_cob/invoke` |
| **Upstream** | `GET` | `{FACETS_BASE_URL}/Claims/{claim_number}/Inquiry/COB` |

**Invoke body**

```json
{ "args": { "claim_number": "{claim_number}" } }
```

---

## 3 — `facets_get_line_details`

All service-line details. Upstream call is repeated for `seq = 1 … N` until the first 404.

| Layer | Method | URL |
|-------|--------|-----|
| **Invoke** | `POST` | `{BASE_URL}/api/agent-tools/facets_get_line_details/invoke` |
| **Upstream** | `GET` | `{FACETS_BASE_URL}/Claims/{claim_number}/Inquiry/Lines/{seq}/Details` |

**Invoke body**

```json
{ "args": { "claim_number": "{claim_number}" } }
```

---

## 4 — `facets_get_member_eligibility`

Member eligibility. Tool first calls **summary** to read `MEME_CK`, then eligibility.

| Layer | Method | URL |
|-------|--------|-----|
| **Invoke** | `POST` | `{BASE_URL}/api/agent-tools/facets_get_member_eligibility/invoke` |
| **Upstream (step 1)** | `GET` | `{FACETS_BASE_URL}/Claims/{claim_number}/Inquiry/Summary` |
| **Upstream (step 2)** | `GET` | `{FACETS_BASE_URL}/Members/Coverage/MemberKey/{meme_ck}/Eligibility` |

**Invoke body**

```json
{ "args": { "claim_number": "{claim_number}" } }
```

---

## 5 — `facets_get_provider_details`

Provider search via stored procedure `CMCSP_PRV1_SRCH_PRPR_NAME_REMT`. Entity/TIN auto-resolved from summary when omitted.

| Layer | Method | URL |
|-------|--------|-----|
| **Invoke** | `POST` | `{BASE_URL}/api/agent-tools/facets_get_provider_details/invoke` |
| **Upstream (optional)** | `GET` | `{FACETS_BASE_URL}/Claims/{claim_number}/Inquiry/Summary` |
| **Upstream** | `POST` | `{FACETS_BASE_URL}/data/procedure/execute` |

**Invoke body (minimal)**

```json
{ "args": { "claim_number_for_reference": "{claim_number}" } }
```

**Invoke body (explicit provider keys)**

```json
{
  "args": {
    "claim_number_for_reference": "{claim_number}",
    "provider_entity_type": "P",
    "tax_id": "123456789"
  }
}
```

---

## 6 — `facets_get_duplicate_claim`

Duplicate-claim search by subscriber/group/claim type from summary, filtered by line 1 DOS range.

| Layer | Method | URL |
|-------|--------|-----|
| **Invoke** | `POST` | `{BASE_URL}/api/agent-tools/facets_get_duplicate_claim/invoke` |
| **Upstream (step 1)** | `GET` | `{FACETS_BASE_URL}/Claims/{claim_number}/Inquiry/Summary` |
| **Upstream (step 2)** | `GET` | `{FACETS_BASE_URL}/Search/Claims/Inquiry?SubscriberID={SBSB_ID}&GroupID={GRGR_ID}&ClaimType={CLCL_CL_SUB_TYPE}` |
| **Upstream (step 3)** | `GET` | `{FACETS_BASE_URL}/Claims/{claim_number}/Inquiry/Lines/1/Details` |

**Invoke body**

```json
{ "args": { "claim_number": "{claim_number}" } }
```

---

## Quick reference (invoke URLs only)

```
POST {BASE_URL}/api/agent-tools/facets_get_summary/invoke
POST {BASE_URL}/api/agent-tools/facets_get_cob/invoke
POST {BASE_URL}/api/agent-tools/facets_get_line_details/invoke
POST {BASE_URL}/api/agent-tools/facets_get_member_eligibility/invoke
POST {BASE_URL}/api/agent-tools/facets_get_provider_details/invoke
POST {BASE_URL}/api/agent-tools/facets_get_duplicate_claim/invoke
```

## Local mock upstream paths

When `FACETS_BASE_URL=http://localhost:8000/api/mocks/facets`, mocks are served under `/api/mocks/` (see `agent_tools/mock/urls.py`):

```
POST /api/mocks/facets/security/tokens
GET  /api/mocks/facets/Claims/{claim_number}/Inquiry/Summary
GET  /api/mocks/facets/Claims/{claim_number}/Inquiry/COB
GET  /api/mocks/facets/Claims/{claim_number}/Inquiry/Lines/{seq}/Details
GET  /api/mocks/facets/Members/Coverage/MemberKey/{member_key}/Eligibility
POST /api/mocks/facets/data/procedure/execute
GET  /api/mocks/facets/Search/Claims/Inquiry
```
