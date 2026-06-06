# Tool calls — claim `25XH84977500`

```bash
export JWT_SECRET='your-shared-secret'
export BASE_URL='http://localhost:8000'
export TOKEN=$(python3 -c 'import os,jwt,time; print(jwt.encode(
  {"sub":"me","email":"you@example.com","role":"ADMIN",
   "iat":int(time.time()),"exp":int(time.time())+3600},
  os.environ["JWT_SECRET"], algorithm="HS256"))')
export CLAIM='25XH84977500'
```

## List tools

```bash
curl -s "$BASE_URL/api/agent-tools/" \
  -H "Authorization: Bearer $TOKEN"
```

## 1 — doc360_read_claim_by_fln_dcc

```bash
curl -s -X POST "$BASE_URL/api/agent-tools/doc360_read_claim_by_fln_dcc/invoke" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"args\": {\"fln_dcc\": \"$CLAIM\"}}"
```

## 2 — facets_get_summary

```bash
curl -s -X POST "$BASE_URL/api/agent-tools/facets_get_summary/invoke" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"args\": {\"claim_number\": \"$CLAIM\"}}"
```

## 3 — facets_get_cob

```bash
curl -s -X POST "$BASE_URL/api/agent-tools/facets_get_cob/invoke" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"args\": {\"claim_number\": \"$CLAIM\"}}"
```

## 4 — facets_get_line_details

```bash
curl -s -X POST "$BASE_URL/api/agent-tools/facets_get_line_details/invoke" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"args\": {\"claim_number\": \"$CLAIM\"}}"
```

## 5 — facets_get_member_eligibility

```bash
curl -s -X POST "$BASE_URL/api/agent-tools/facets_get_member_eligibility/invoke" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"args\": {\"claim_number\": \"$CLAIM\"}}"
```

## 6 — facets_get_provider_details

```bash
curl -s -X POST "$BASE_URL/api/agent-tools/facets_get_provider_details/invoke" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"args\": {\"claim_number_for_reference\": \"$CLAIM\"}}"
```

## 7 — facets_get_duplicate_claim

```bash
curl -s -X POST "$BASE_URL/api/agent-tools/facets_get_duplicate_claim/invoke" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"args\": {\"claim_number\": \"$CLAIM\"}}"
```

## 8 — facet_extension_portal_provider

```bash
export PROVIDER_ID='FAC000022500'

curl -s -X POST "$BASE_URL/api/agent-tools/facet_extension_portal_provider/invoke" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"args\": {\"provider_id\": \"$PROVIDER_ID\"}}"
```

## 9 — facet_extension_portal_programme

```bash
export PROGRAM_ID='276728'

curl -s -X POST "$BASE_URL/api/agent-tools/facet_extension_portal_programme/invoke" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"args\": {\"program_detailed_id\": \"$PROGRAM_ID\"}}"
```

## linx_claim_search

```bash
curl -s -X POST "$BASE_URL/api/agent-tools/linx_claim_search/invoke" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"args\": {\"subscriber_id\": \"$CLAIM\"}}"
```

## 10 — facet_ext_portal_group_model

```bash
curl -s -X POST "$BASE_URL/api/agent-tools/facet_ext_portal_group_model/invoke" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"args\": {\"claim_number\": \"$CLAIM\"}}"
```
