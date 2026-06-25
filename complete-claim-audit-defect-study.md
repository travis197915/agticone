# Complete Claim Audit Pipeline — Defect Study (29 claims flagged DENY, auditors say CLEAN)

_Generated 2026-06-24 · DB `agentic_flow_v2db` · workflow **Complete Claim Audit Pipeline** (`complete-claim-audit-pipeline`)_

## Verdict

- **27/29 are false positives** caused by a data-level `decision_type` mislabel.
- **2/29 carry real denial codes** and need a human look: 25XJ02104300, 25XJ19692800.

## Two root causes (both are ingestion data bugs, not engine bugs)

A claim rolls up to `DEFECT` when **any rule matches AND `decision_type ∈ {DENY,STOP,REFER,PEND}`** (`trace_builder._eval_applies_defect`). It never checks whether a denial code was applied or whether the rule is terminal. Two rule families are mislabeled `DENY`:

1. **SOP 5 — *Provider Selection Guidelines* (steps 4/5/7 '3rd/4th/5th choice').** These are pure provider-record selection branches (`is_final=false`, `codes=[]`, action = 'check/select … record'). 1st/2nd choices are correctly `CONDITIONAL`; 3rd/4th/5th were ingested as `DENY`. Resolving to a Group/OON record is normal processing, so the match falsely flags the claim.
2. **SOP 8 — *Provider Opt-Out* (`RULE-003-001/002`).** Condition literally says *'audit is correct'*, action *'Mark audit as CORRECT'* — a CLEAN result — yet `decision_type=DENY`. An inversion.

**Secondary bug:** in several claims the LLM's own reasoning concludes the rule does **not** apply, yet the evaluation was stored as `matched=DENY` and counted as a defect (verdict ≠ reasoning).

## Per-claim study

### 25XI31158300 — `CLEAN`  ·  run `COMPLETED`  ·  codes: F3
- **step:5:4:3** — SOP5 Step4 '4th choice' (group record selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - ⚠️ verdict/reasoning **mismatch**: reasoning concludes the rule does NOT apply, but it was recorded as `DENY`.
  - why it fired: The rule's condition requires: (1) Individual is not billed in doc360 claim image form, (2) Provider Group record found in Facets claim summary per provider entity type field, and (3) 1st, 2nd, 3rd choices are not satisfied. From doc360 claim image, Box 11 shows billing provider 

### 25XI18819100 — `CLEAN`  ·  run `COMPLETED`  ·  codes: F3
- **step:5:5:2** — SOP5 Step5 '3rd choice' (group+individual+OON selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - why it fired: The rule's 3rd choice condition requires: (1) Group record from Provider entity type field, (2) Individual billed in DOC360, and (3) OON network indicator. From ClaimSummary: PRPR_ENTITY='G' (Provider Group) confirms Group record. From doc360 claim image field 27: 'A/ASSIGNED' in

### 25XI42719800 — `CLEAN`  ·  run `COMPLETED`  ·  codes: —
- **step:5:7:2** — SOP5 Step7 '3rd choice' (group+individual+OON selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - ⚠️ verdict/reasoning **mismatch**: reasoning concludes the rule does NOT apply, but it was recorded as `DENY`.
  - why it fired: The rule's 3rd choice condition requires: (1) Group record identified from Provider entity type field, (2) Individual is billed in DOC360 Claim image form, and (3) network indicator shows OON (Out of network). From FACETS claim summary: PRPR_ENTITY='G' and CIV8_PRPR_ENTITY_DESC='

### 25XI24819100 — `CLEAN`  ·  run `COMPLETED`  ·  codes: F3
- **step:5:4:3** — SOP5 Step4 '4th choice' (group record selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - why it fired: The rule requires checking if individual is NOT billed in DOC360 claim image form AND Provider Group record is found in Facets. From DOC360 form field 11 (Billing Provider), the billing provider is 'QUEST DIAGNOSTICS CLIFTON' with NPI 1932145778 and taxonomy code 291U00000X (indi

### 25XI38119700 — `CLEAN`  ·  run `COMPLETED`  ·  codes: —
- **step:5:5:2** — SOP5 Step5 '3rd choice' (group+individual+OON selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - why it fired: The rule's 3rd choice condition is satisfied: (1) Provider entity type field 'PRPR_ENTITY' is 'G' indicating a Group record, (2) DOC360 claim image shows field 27 as 'A/ASSIGNED' meaning Individual is billed (not Group), and (3) Network indicator 'CLCL_NTWK_IND' is 'O' indicating

### 25XI26764100 — `CLEAN`  ·  run `COMPLETED`  ·  codes: —
- **step:5:5:2** — SOP5 Step5 '3rd choice' (group+individual+OON selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - why it fired: The rule's 3rd choice condition requires: (1) Group record identified from Provider entity type field, (2) Individual is billed in DOC360 Claim image form, and (3) network indicator indicates OON. From the claim: PRPR_ENTITY='G' (Provider Group) confirms Group record; DOC360 form

### 25XI29270100 — `CLEAN`  ·  run `COMPLETED`  ·  codes: —
- **step:5:4:3** — SOP5 Step4 '4th choice' (group record selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - ⚠️ verdict/reasoning **mismatch**: reasoning concludes the rule does NOT apply, but it was recorded as `DENY`.
  - why it fired: The rule's 4th choice condition is satisfied: (1) Individual is not billed in DOC360 claim image form - Field 33 'SERVICING PHYSICIAN/SUPPLIER NAME' in the DOC360 claim form is blank/empty, indicating no individual provider billed; (2) Provider Group record found in Facets Claim 

### 25XI36357600 — `CLEAN`  ·  run `COMPLETED`  ·  codes: —
- **step:5:5:2** — SOP5 Step5 '3rd choice' (group+individual+OON selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - ⚠️ verdict/reasoning **mismatch**: reasoning concludes the rule does NOT apply, but it was recorded as `DENY`.
  - why it fired: The claim satisfies the 3rd choice condition: (1) Provider entity type field 'PRPR_ENTITY' = 'P' (Practitioner) indicates an Individual provider, NOT a Group record; however, the DOC360 claim image shows 'TAYEB ZEESHAN K' is billed as an individual practitioner in field 11 (Billi
- **step:5:5:4** — SOP5 Step5 '5th choice' (OON individual selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - why it fired: This is the 5th choice condition in Step 5, which applies when 1st-4th choices are not satisfied. The claim shows network indicator 'O - Out of Network' (CLCL_NTWK_IND='O'), indicating OON status. The provider PRPR_ID='000995905001' with PRPR_ENTITY='P' (Practitioner) is an indiv

### 25XI38549500 — `CLEAN`  ·  run `COMPLETED`  ·  codes: —
- **step:5:5:2** — SOP5 Step5 '3rd choice' (group+individual+OON selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - ⚠️ verdict/reasoning **mismatch**: reasoning concludes the rule does NOT apply, but it was recorded as `DENY`.
  - why it fired: The rule's 3rd choice condition requires: (1) Group record identified from Provider entity type field, (2) Individual is billed in DOC360 Claim image form, and (3) network indicator shows OON (Out of network). From the claim data: (1) PRPR_ENTITY='P' (Practitioner) indicates Indi

### 25XI59771900 — `CLEAN`  ·  run `COMPLETED`  ·  codes: F3
- **step:5:4:3** — SOP5 Step4 '4th choice' (group record selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - ⚠️ verdict/reasoning **mismatch**: reasoning concludes the rule does NOT apply, but it was recorded as `DENY`.
  - why it fired: The rule requires checking if individual is NOT billed in DOC360 claim image form but a Provider Group (entity type) record is found in Facets. From DOC360 claim form box 11 (BILLING PROVIDER), the entity is '1 LAB DIAGNOSTICS NJ LLC' with taxonomy code '291U00000X' (Laboratory),

### 25XI49251400 — `CLEAN`  ·  run `COMPLETED`  ·  codes: —
- **step:5:4:3** — SOP5 Step4 '4th choice' (group record selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - why it fired: The rule's 4th choice condition is satisfied: (1) From DOC360 claim image form field 11 'BILLING PROVIDER NAME', we see 'EMERGENCY MEDICAL ASSOC OF TAMPA BAY LLC' - a group/organization name, not an individual provider name, confirming individual is NOT billed. (2) From Facets Cl

### 25XI37940500 — `CLEAN`  ·  run `COMPLETED`  ·  codes: F3
- **step:5:4:3** — SOP5 Step4 '4th choice' (group record selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - why it fired: The rule condition requires: (1) Individual is NOT billed in doc360 claim image form, AND (2) Provider Group record found in Facets Claim Summary as per provider entity type field. From doc360 claim image (Box 11), the billing provider is 'CHANDLER RADIOLOGY ASSOCIATES LLC' with 

### 25XI86239000 — `CLEAN`  ·  run `TERMINATED_EARLY`  ·  codes: —
- **step:8:3:1** — SOP8 'not opted-out & not denied -> AUDIT IS CORRECT'
  - classification: **label_bug_inversion** — clean outcome 'AUDIT IS CORRECT' mislabeled decision_type=DENY
  - why it fired: The rule applies when provider is not opted-out AND claim is not denied (paid amount > $0, no opt-out error codes). Tool results are empty (no medicare_optout_tool records found), indicating provider is not opted-out. The claim shows CLCL_TOT_PAYABLE of 214.68 and CLSA_PAID_AMT o

### 25XI81969400 — `CLEAN`  ·  run `COMPLETED`  ·  codes: F3
- **step:5:4:3** — SOP5 Step4 '4th choice' (group record selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - ⚠️ verdict/reasoning **mismatch**: reasoning concludes the rule does NOT apply, but it was recorded as `DENY`.
  - why it fired: The rule's 4th choice condition is satisfied: (1) The DOC360 claim image form (section 11) shows 'MILLENNIUM HEALTH, LLC' as the BILLING PROVIDER NAME, which is a group/organization name, not an individual provider name - indicating individual is NOT billed; (2) The Facets Claim 

### 25XI53164600 — `CLEAN`  ·  run `COMPLETED`  ·  codes: —
- **step:5:4:3** — SOP5 Step4 '4th choice' (group record selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - ⚠️ verdict/reasoning **mismatch**: reasoning concludes the rule does NOT apply, but it was recorded as `DENY`.
  - why it fired: The rule's 4th choice condition is satisfied: (1) Individual is NOT billed in DOC360 claim image - the billing provider in section 11 shows 'THE PERMANENTE MEDICAL GROUP INC' with NPI 1639252604, which is a group entity, not an individual; (2) Provider Group record is found in Fa

### 25XJ01800200 — `CLEAN`  ·  run `COMPLETED`  ·  codes: —
- **step:5:4:3** — SOP5 Step4 '4th choice' (group record selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - ⚠️ verdict/reasoning **mismatch**: reasoning concludes the rule does NOT apply, but it was recorded as `DENY`.
  - why it fired: The rule's 4th choice condition requires: (1) Individual is NOT billed in DOC360 claim image form, AND (2) Provider Group record found in Facets claim summary as per provider entity type field. From DOC360, Box 11 shows 'BLAIR SCHELB' as the billing provider, which is an individu

### 25XI96897400 — `CLEAN`  ·  run `COMPLETED`  ·  codes: —
- **step:5:5:2** — SOP5 Step5 '3rd choice' (group+individual+OON selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - why it fired: The rule's 3rd choice condition requires: (1) Group record identified from Provider entity type field, (2) Individual is billed in DOC360 Claim image form, and (3) network indicator indicates OON (Out of network). From the claim data: (1) PRPR_ENTITY='G' and CIV8_PRPR_ENTITY_DESC

### 25XJ13000400 — `CLEAN`  ·  run `COMPLETED`  ·  codes: F3
- **step:5:5:2** — SOP5 Step5 '3rd choice' (group+individual+OON selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - ⚠️ verdict/reasoning **mismatch**: reasoning concludes the rule does NOT apply, but it was recorded as `DENY`.
  - why it fired: The rule's 3rd choice condition requires: (1) Group record identified from Provider entity type field, (2) Individual billed in DOC360 Claim image, and (3) Network indicator shows OON (Out of Network). From facet claim summary: PRPR_ENTITY='G' indicates Provider Group. From DOC36

### 25XJ02104300 — `DEFECT (human review)`  ·  run `COMPLETED`  ·  codes: W47, W46, F3
- **step:5:4:3** — SOP5 Step4 '4th choice' (group record selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - why it fired: This rule checks whether: (1) an individual provider is NOT billed in DOC360, and (2) a Provider Group record is found in Facets with entity type 'G'. From DOC360 claim image, box 11 shows billing provider 'QUEST DIAGNOSTICS OF PENN' with taxonomy code '291U00000X' (Clinical Labo
- **step:2:3:0** — SOP2 Cross-Billing review (W47/W46)
  - classification: **genuine** — carries real denial codes ['W47', 'W46']
  - why it fired: Evaluated the three exceptions specified in the rule: Exception 1 (inpatient POS) - Place of Service is '81' (INDEPENDENT LABORATORY), not an inpatient facility code, so FALSE. Exception 2 (add-on codes) - procedure codes are 84479, 84436, 80061, 86376, 80050, 3048F; none of thes

### 25XI98134900 — `CLEAN`  ·  run `COMPLETED`  ·  codes: —
- **step:5:4:3** — SOP5 Step4 '4th choice' (group record selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - why it fired: The rule's 4th choice condition requires: (1) Individual is not billed in DOC360 claim image form, and (2) Provider Group record is found in Facets Claim Summary as per provider entity type field. From the DOC360 claim form (field 11), the billing provider is 'RADIOLOGY ASSOCIATE

### 25XI83367000 — `CLEAN`  ·  run `COMPLETED`  ·  codes: —
- **step:5:7:2** — SOP5 Step7 '3rd choice' (group+individual+OON selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - why it fired: The rule requires three conditions for the 3rd choice to apply: (1) Group record identified from Provider entity type field - confirmed by PRPR_ENTITY='G' and CIV8_PRPR_ENTITY_DESC='Provider Group', (2) Individual is billed in DOC360 Claim image form - confirmed by Box 31 showing

### 25XJ30267800 — `CLEAN`  ·  run `COMPLETED`  ·  codes: —
- **step:5:5:2** — SOP5 Step5 '3rd choice' (group+individual+OON selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - why it fired: The rule's 3rd choice condition requires: (1) Group record identified from Provider entity type field, (2) Individual is billed in DOC360 Claim image form, and (3) network indicator indicates OON. All three conditions are satisfied: (1) PRPR_ENTITY='G' confirms Group record, (2) 

### 25XJ13339200 — `CLEAN`  ·  run `COMPLETED`  ·  codes: —
- **step:5:7:2** — SOP5 Step7 '3rd choice' (group+individual+OON selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - why it fired: The rule's 3rd choice condition requires: (1) Group record identified from Provider entity type field, (2) Individual is billed in DOC360 Claim image form, and (3) network indicator indicates OON (Out of network). From facets_get_summary, PRPR_ENTITY='G' and CIV8_PRPR_ENTITY_DESC

### 25XJ36465200 — `CLEAN`  ·  run `TERMINATED_EARLY`  ·  codes: —
- **step:5:5:2** — SOP5 Step5 '3rd choice' (group+individual+OON selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - why it fired: This is the 3rd choice subrule. Condition requires: (1) Group record identified from Provider entity type field, (2) Individual billed in claim form, (3) OON network indicator. From facet claim summary: PRPR_ENTITY='G' (Provider Group) confirms group record; CLCL_NTWK_IND='O' (Ou
- **step:8:3:0** — SOP8 'opted-out & denied -> AUDIT IS CORRECT'
  - classification: **label_bug_inversion** — clean outcome 'AUDIT IS CORRECT' mislabeled decision_type=DENY
  - why it fired: The claim shows the provider is opted-out (ATMM text 'Model 2I MPIN 007162164' indicates Medicare opt-out status), and the claim is denied with paid amount $0 (CLSA_PAID_AMT=0, CLCL_TOT_PAYABLE=0). The claim has override code 'FOE' with description 'Rendering Clinician not creden

### 25XJ19692800 — `DEFECT (human review)`  ·  run `TERMINATED_EARLY`  ·  codes: E51, F51, 003
- **step:2:8:3** — SOP2 Telehealth duplicate (E51/F51/003)
  - classification: **genuine** — carries real denial codes ['E51', 'F51', '003']
  - why it fired: The rule applies to duplicate telehealth claims. Both line items (seq 1 and 2) are billed with telehealth place of service (PSCD_ID='10' = 'TELEHEALTH PROVIDED IN PATIENT'S HOME') and telehealth modifiers (GT on line 1, 95 on line 2). The condition explicitly states 'If both clai

### 25XJ49060300 — `CLEAN`  ·  run `TERMINATED_EARLY`  ·  codes: —
- **step:8:3:1** — SOP8 'not opted-out & not denied -> AUDIT IS CORRECT'
  - classification: **label_bug_inversion** — clean outcome 'AUDIT IS CORRECT' mislabeled decision_type=DENY
  - why it fired: The rule requires two conditions: (1) Provider is not opted-out (no records from medicare_optout_tool), and (2) Claim is not denied (no opt-out error in EOB codes AND paid amount > $0). The tool results are empty, indicating no opt-out records exist for the provider. The claim sh

### 25XJ48700200 — `CLEAN`  ·  run `COMPLETED`  ·  codes: —
- **step:5:5:2** — SOP5 Step5 '3rd choice' (group+individual+OON selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - why it fired: The rule's 3rd choice condition requires: (1) Group record identified from Provider entity type field, (2) Individual is billed in DOC360 Claim image form, and (3) network indicator indicates OON. All three conditions are satisfied: (1) PRPR_ENTITY='G' (Provider Group) in claim s

### 25XJ40102200 — `CLEAN`  ·  run `COMPLETED`  ·  codes: —
- **step:5:5:2** — SOP5 Step5 '3rd choice' (group+individual+OON selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - why it fired: The rule evaluates the 3rd choice condition: Group record + Individual billed + OON. From FACETS claim summary, PRPR_ENTITY='G' confirms Group record (Provider Group). From DOC360 claim form field 33 'SERVICING PHYSICIAN/SUPPLIER NAME', the rendering provider is 'JOHANNES JILLIAN
- **step:5:5:4** — SOP5 Step5 '5th choice' (OON individual selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - why it fired: Rule condition evaluates 5th choice: when 1st-4th choices are not satisfied. From tool results, facet_ext_portal_group_model confirms group_model='2A'. Claim shows PRPR_ENTITY='G' (Provider Group) in REC_CIV8. However, CLCL_NTWK_IND='O' (Out of Network) indicates OON status. DOC3

### 25XJ30867500 — `CLEAN`  ·  run `TERMINATED_EARLY`  ·  codes: —
- **step:5:4:3** — SOP5 Step4 '4th choice' (group record selection)
  - classification: **label_bug_selection** — provider-record selection branch mislabeled decision_type=DENY (no codes, is_final=false)
  - why it fired: The individual is not billed in the doc360 claim image form and the Provider Group record was found in the Facets Claim Summary section as the provider entity type field is 'P' (Practitioner).
- **step:8:3:1** — SOP8 'not opted-out & not denied -> AUDIT IS CORRECT'
  - classification: **label_bug_inversion** — clean outcome 'AUDIT IS CORRECT' mislabeled decision_type=DENY
  - why it fired: The provider is not opted-out (no records in Medicare Opt-Out Tool) and the claim is not denied. The Claim EOB Explanation Code does not contain any opt-out errors and the paid amount is more than $0.

## Root cause (corrected after live tool-call verification)

My first pass called SOP 5's `3rd/4th/5th choice` rows a **label bug** (`DENY` that should be
`CONDITIONAL`). Reading the gold `yaml/ProviderSelectionVerification.yaml` shows that is **wrong**:
the 3rd/4th/5th choices explicitly say *"raise Provider Selection error and claim should be denied."*
`DENY` is the **intended** disposition for those branches. The defect is therefore **not** the label —
it is that the engine **selects the wrong choice** (3rd/4th/5th) when the correct choice is 1st/2nd
(clean). SOP 8 (`AUDIT IS CORRECT` → `DENY`) **is** a genuine inversion and still needs fixing.

### Live verification (claim 25XI18819100, POST to `claims-mock-mcp-server.toystack.dev`)
- `facet_ext_portal_group_model` → `group_model = 2A`
- `facets_get_summary` → `PRPR_ENTITY = G` (group), `PRPR_NAME = Essentia Health Duluth Clinic`,
  `PRPR_NPI = 1245278209` (the **group** NPI), `PRPR_ID = GRP000018405`, `CLCL_NTWK_IND = O`.

### Why the engine picks the wrong choice (two missing pieces of business context)
1. **INN/OON is read off `CLCL_NTWK_IND` at face value.** Gold `RULE-000` says INN/OON must be
   *derived* — multi-record **2‑point match → INN**, single-record **3‑point match → OON** against the
   FACETS provider-details section, *then* reconciled with the network indicator. The engine never runs
   that match; it takes `CLCL_NTWK_IND='O'` literally, concludes OON, and lands on the 3rd/4th/5th
   (deny) choice instead of 1st/2nd (clean).
2. **"Individual is billed" has no dedicated field.** `field_mapping.py` resolves with
   `_SYSTEM_ORDER = ("FACETS","DOC360",...)` so `Provider NPI`/`Provider Name` always resolve to the
   **FACETS group** values. The DOC360 box‑24 (Rendering NPI) / box‑33 (Servicing Physician Name)
   individual signal is **never surfaced as its own canonical field**, so the LLM guesses — in several
   traces it cited DOC360 **box 27 ("A/ASSIGNED")**, which is assignment-of-benefits, not a rendering
   provider. `_eval_common.py` *does* inject the mapped block (line 289), but the block only carries the
   group provider, reinforcing the wrong read.

## Fix

Context to add (so the engine selects the correct choice instead of relabeling intent):
1. **Add a distinct canonical field** to `yaml/sop_field_mapping.yaml`, e.g.
   `Rendering/Servicing Individual Provider` → `DOC360: [box 24 Rendering NPI, box 33 Servicing Physician Name]`
   (DOC360-only; do **not** let FACETS group values win). This gives the LLM a real
   "individual is billed" signal separate from the billing group.
2. **Encode the INN/OON determination procedure** from `RULE-000` into the provider-selection rule
   text / step narrative: "INN/OON is decided by the 2‑point (→INN) / 3‑point (→OON) provider match
   against the FACETS provider-details records, then reconciled with `CLCL_NTWK_IND`; do **not** read
   network status directly off `CLCL_NTWK_IND`." Ensure the provider-details (multi-record) section is
   fetched and passed in tool context so the match can be performed.
3. **SOP 8** `RULE-003-001/002` (`AUDIT IS CORRECT`): genuine inversion — `DENY` → `ALLOW/CONDITIONAL`.

Do **not** relabel SOP 5's 3rd/4th/5th choices — `DENY` is correct per the gold SOP. With (1)+(2)
the selection branches stop firing on clean group-INN claims (they correctly resolve to 1st/2nd
choice), and with (3) the opt-out "audit is correct" claims roll up CLEAN. The 2 code-bearing claims
(real E51/F51/003) remain genuine defects for human review.