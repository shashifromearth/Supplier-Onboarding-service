# W-9 Supplier Onboarding Service — Design Document

**Author:** Karl (KHM Technology) · **For:** PairSoft Central AI Services case study · **Version:** 1.0 (Oct 2026)
**Companion artefacts:** working prototype (this repo), `README.md`, `docs/INTERVIEW_DEFENCE.md`, `eval/report_*.md`

---

## 0. Summary, position and assumptions

**The position in one paragraph.** A W-9 is a standardised form, so most of it can be read deterministically, but it arrives as phone photos, old revisions, and occasionally the wrong form. I use **OCR for evidence (text + positions + confidence), deterministic code for anything that must be exact (TIN, checkboxes, validation, decision rules), and a text-only LLM (Gemini first, Groq-hosted `gpt-oss-20b` as fallback) only where judgement over messy layout is needed — with every LLM value grounded back to the parsed text**. Identity is decided **TIN-first** (via a tenant-scoped blind index, never decrypting), with fuzzy name/address matching as corroboration and as a duplicate detector. Automation is *earned*: the service auto-creates or auto-updates only when the document is valid, critical fields are confidently read, and identity evidence is unambiguous; everything else goes to a human **with a pre-filled suggestion and machine-readable reasons**. The optimisation target is **zero wrong auto-updates** (a wrong merge redirects payments), then minimum human-review rate.

**What the prototype proves** (see §6 for numbers): on 13 adversarial sample documents (clean, scanned, phone photo, wrong form, TIN conflicts, ambiguous names, cross-tenant), decision accuracy is 100% on originals; on every document re-rendered as a degraded phone photo it is 77% with **0 false matches and 0 duplicate creations** — all misses are safe misses (routed to review).

### Assumptions (please challenge these)

| # | Assumption | Why it matters |
|---|---|---|
| A1 | Volume: 500 tenants × 10–50 W-9/week ⇒ ~15k docs/week (~65k/month), bursty (vendor campaigns, year-end 1099 prep). | Async queue, not a synchronous-only API. Cost is small; human review is the dominant cost. |
| A2 | Supplier master is reachable through a tenant-scoped API that supports lookup by **TIN blind index** and name search. TINs are encrypted at rest; we never need plaintext to match. | Matching design (§2.4). If the master can only return plaintext TINs, we compute the index on our side at ingest. |
| A3 | Consumers are internal PairSoft services (supplier-master writer, review queue). They need a **stable, versioned contract** and will not read free text to make decisions. | Machine-readable codes everywhere; `schema_version`; additive changes only. |
| A4 | US suppliers only (W-9). W-8 family is *detected and rejected*, not processed. Country packs (e.g. UK VAT, CA GST) plug in later as new `FormProfile`s. | Extensibility seam in extractor/validator. |
| A5 | IRS TIN Matching, OFAC/sanctions screening and bank-account verification are **separate downstream services**; we emit the signals they need, we don't call them in v1. | Keeps latency/cost bounded; avoids holding IRS e-Services credentials in the AI service. |
| A6 | Human review is cheap relative to a wrong merge (≈$1–2 of reviewer time vs. a misdirected payment or 1099 error). | Thresholds bias toward review (§2.5). |
| A7 | The core service runs in the tenant-isolated cloud account (Azure-style, PairSoft mid-market SaaS). Three **third-party processors** are used in this version: **LlamaParse** (LlamaCloud, document parsing) and two LLM providers, **Gemini** (first choice) and **Groq** (fallback). All three need a DPA, a no-training/zero-retention setting and an approved region before real PII is sent (§3). | Security posture (§3). |

---

# Part 1 — High-level architecture

## 1.1 Component diagram

```
                      ┌──────────────────────── PairSoft product (AP / Procurement) ─────────────────────────┐
                      │  uploads W-9 (PDF/PNG/JPG) on behalf of tenant T, receives decision via callback/event │
                      └───────────────┬──────────────────────────────────────────────────▲───────────────────┘
                                      │ POST /v1/w9-intake (tenant from token, idempotency key = sha256)
 ┌────────────────────────────────────▼──────────────────────────────────────────────────┴───────────────────┐
 │ API edge  (APIM: authN, tenant claim, rate limit per tenant, size/type checks)                              │
 └────────────────────────────────────┬─────────────────────────────────────────────────────────────────────┘
                                      │ 1. store raw doc → Blob (tenant container, CMK-encrypted, 90d TTL)
                                      │ 2. enqueue {tenant, doc_id, blob_uri}  → Service Bus (session = tenant)
 ┌────────────────────────────────────▼───────────────────── W-9 Onboarding Worker (stateless, scale-out) ───┐
 │                                                                                                             │
 │  ① Ingest/Normalise ─▶ ② OCR ─▶ ③ Extraction ─▶ ④ Validation ─▶ ⑤ Matching ─▶ ⑥ Decision ─▶ ⑦ Response  │
 │   type sniff, page      LlamaParse layout(det.)    IRS format     TIN blind     ordered      PII-safe JSON   │
 │   render, hash,         (text      + Gemini→Groq   rules, cross-  index +       rules,       contract v1,    │
 │   size/page limits      layer      (text-only,     field checks,  RapidFuzz     review       TIN → vault     │
 │                         first)     grounded,       confidence cap name/addr     reasons                      │
 │                                    TIN-redacted)                                                            │
 │        │                   │            │  ▲             │            │  ▲          │              │          │
 └────────┼───────────────────┼────────────┼──┼─────────────┼────────────┼──┼──────────┼──────────────┼──────────┘
          │                   │            │  │calibration  │            │  │          │              │
          ▼                   ▼            ▼  │ maps        ▼            ▼  │          ▼              ▼
   Blob (raw doc,      LlamaParse       Gemini→Groq        Policy/config  Supplier Master API     Event Grid / SB topic
   tenant-scoped)      (LlamaCloud,     (gpt-oss-20b,      (thresholds,   (tenant-scoped token)   "w9.decided" →
                       DPA/region)      DPA/region)        per tenant)    + TIN Vault (Key Vault  supplier-master writer
                                                                          HMAC keys per tenant)   / review queue
          └──────────────── Observability: OpenTelemetry → App Insights / Log Analytics; Audit log → immutable store ────┘
```

## 1.2 Data flow (happy path and why each step exists)

| Step | What happens | Why it exists |
|---|---|---|
| **W-9 Upload** | Product uploads the file with the tenant's token. API edge validates type/size, computes `sha256`, stores the blob in the tenant's container, enqueues a job, returns `202 {document_id}`. | Decouples bursty uploads from slow AI calls; `sha256` gives idempotency (re-uploads and retries don't create duplicate work or duplicate suppliers). |
| **① Ingest/Normalise** | Sniff magic bytes (not the extension), reject >20 MB / >6 pages / encrypted PDFs, render page 1 to an image at 200 DPI. | Every later stage gets the same canonical inputs: text + a pixel image. Cheap guardrails stop garbage early. |
| **② OCR** | If the PDF has a real text layer → use it (free, exact). Else **LlamaParse** (`tier="agentic"`, `expand=[markdown_full, metadata]`): Markdown for the LLM and a page-level confidence. LlamaParse layout items are block-level (label and value merged into one box), which the line-based layout reader cannot use, so positions always come from a local geometry pass (PDF text layer or RapidOCR). This was found in the first live run. | Evidence layer. Positions are needed for checkboxes/signature; confidences feed field confidence; text grounds LLM output. |
| **③ Extraction** | Deterministic layout extractor reads labelled boxes, checkbox ink and signature ink; Gemini (falling back to Groq `gpt-oss-20b`, text only) reads the parsed text with the TIN redacted and returns JSON citing line ids; the two are reconciled per field. The LLM cannot see pixels, so for checkbox and signature it abstains (`NOT_ASSESSED`) and the layout reader owns them. | Two independent readers give a strong confidence signal (agreement) and catch each other's failure modes (layout drift vs. hallucination). |
| **④ Validation** | IRS-format and cross-field rules (EIN prefixes, SSN structure, classification vs TIN type, signature/date, address completeness), confidence thresholds. | Rules that must be exact, explainable and identical for every tenant and model version. A format-invalid value is capped at low confidence. |
| **⑤ Matching** | Compute tenant-scoped TIN fingerprint → exact lookup. Retrieve fuzzy candidates (name blocking + same ZIP) → score legal name, DBA (incl. cross DBA↔legal), address with RapidFuzz → composite. Diff against best candidate. | Duplicate detection and change detection — the two KYS problems that cause duplicate payments and fraud. |
| **⑥ Decision** | Ordered rules → `CREATE_NEW_SUPPLIER` / `UPDATE_EXISTING_SUPPLIER` / `HUMAN_REVIEW` + `review_reasons[]` + `suggested_action` + `reasoning[]`. | Business policy separated from scoring so thresholds can change per tenant without touching ML/code. |
| **⑦ Response** | Build contract v1; TIN goes to the vault, response carries masked TIN, last4, fingerprint, `vault_ref`. Emit `w9.decided` event; write audit record. | Single PII choke point; stable contract for consumers. |

## 1.3 Service boundaries

* **This service owns:** reading the document, field-level confidence, validation, match *recommendation*, and the decision. It is **stateless** apart from audit/metrics and idempotency keys.
* **This service does not own:** the supplier master (read-only via API), writing supplier records, the review UI/workflow, IRS TIN matching, OFAC screening, bank verification. Those consume our event/contract.
* **Why the boundary is here:** the supplier master is a system of record with its own consistency rules; a recommendation service that writes to it would couple model behaviour to data integrity. Downstream applies the decision transactionally (and re-checks `last_updated` for optimistic concurrency against our `match_result`).

## 1.4 Multi-tenant design

| Concern | Design |
|---|---|
| **Tenant identity** | Derived from the caller's token at the API edge, never from the payload. Propagated in the job message and trace context. |
| **Data plane isolation** | Raw documents in per-tenant blob containers (or per-tenant prefix + ABAC conditions); supplier master accessed only through a repository constructed for one tenant (`CsvSupplierMaster(tenant_id)` in the prototype — no cross-tenant method exists). |
| **Cryptographic isolation** | Per-tenant HMAC key for TIN fingerprints (`security.tin_fingerprint`). The same EIN in two tenants yields **unlinkable** fingerprints, so even a bug that queries the wrong index cannot match across tenants (sample #13 proves the behaviour). |
| **Model isolation** | Shared stateless services (LlamaParse, Gemini, Groq `gpt-oss-20b`) — no fine-tuning on tenant data in v1, no cross-tenant few-shot examples, no prompt caching of tenant content across tenants. Calibration maps are fitted on reviewed outcomes; if per-tenant data is used, the map is per-tenant. |
| **Noisy neighbour** | Service Bus sessions keyed by tenant + per-tenant concurrency caps and APIM rate limits, so one tenant's 5,000-vendor campaign can't starve others. |
| **Config** | Thresholds/policies resolved per tenant (e.g. a tenant can require review on every address change: `review_on_address_change`). Every response carries `policy_version`. |

## 1.5 Security boundaries

```
 [Trust zone 0: caller]  ──TLS1.2+/token──▶  [Zone 1: API edge]  ──private link──▶  [Zone 2: worker VNet]
                                                                                    │  managed identity only
                                       ┌────────────────────────────────────────────┼───────────────────────┐
                                       ▼                    ▼                       ▼                       ▼
                         [Zone 3a: LlamaParse]   [Zone 3b: Gemini/Groq]   [Zone 4: Key Vault/HSM]  [Zone 5: Supplier master]
                         sees FULL document      sees TIN-REDACTED text    HMAC + DEK keys,         tenant-scoped token,
                         (incl. unredacted TIN)   only, no image            never exported           returns ciphertext/fp only
```

The important line: **the LLM never sees the TIN** (text is replaced with `[TIN REDACTED]` and no image is sent at all — `llm_extractor.llm_input_lines`). The TIN is read deterministically from OCR, which is both safer and more accurate for a 9-digit string.

---

# Part 2 — Detailed technical design

## 2.1 OCR layer

**Purpose:** turn any input into positioned text lines with confidence + a page image. **Inputs:** bytes, MIME. **Outputs:** `OcrResult{lines[text,bbox,confidence], page_images, provider}` (`ocr.py`).

| Option | Strengths | Weaknesses | Verdict |
|---|---|---|---|
| **LlamaParse** (LlamaCloud, `tier=agentic`) | Strong on messy layouts, returns clean Markdown, layout items and a page confidence; simple SDK; one vendor call for parse. | Block-level output (not word polygons), so geometry for checkbox/signature must be checked or taken locally; **third-party processor that sees the unredacted TIN**; credit-based pricing; measured 11–21 s per document at `agentic` tier. | **Chosen** for scans/photos in this version. |
| **Azure Document Intelligence** (`prebuilt-layout`, `prebuilt-tax.us.w9`) | Word polygons and confidences, selection marks, a prebuilt W-9 model, private endpoints. | Black-box prebuilt model; ties the build to Azure. | Not used now; the best candidate for a *third reader* / shadow comparison. |
| **AWS Textract** (FORMS/QUERIES) | Good key-value extraction. | No W-9 model; cross-cloud; higher price per page. | Not chosen. |
| **Vision LLM only** | Best at "which box is ticked". | No per-token positions or OCR confidence → can't ground or calibrate; TIN digit errors; must send the TIN. | Not used. `gpt-oss-20b` is text-only anyway. |
| **Local OCR** (RapidOCR/ONNX) | Free, offline, no data leaves. | Weaker on photos/handwriting; drops isolated glyphs. | Prototype fallback + dev/test. |

**Final choice:** *Text layer first* (≈40–60% of W-9s are digital PDFs from DocuSign/e-fill; reading them is free and exact) → **LlamaParse** for scans/photos → the LLM chain (Gemini, then Groq) reads the parsed text. In the prototype, `run_ocr(provider="auto")` implements text-layer → LlamaParse (if `LLAMA_CLOUD_API_KEY` is set; on failure it logs and falls back to RapidOCR) → RapidOCR.

**Failure scenarios:** corrupt/encrypted PDF → `OcrError` → response `HUMAN_REVIEW / DOCUMENT_UNREADABLE` (never a 500 to the consumer); parser throttling (429) → exponential backoff with jitter, job stays on queue; parser outage → circuit breaker, queue backlog alarms, no silent fallback to a weaker provider for production decisions (fallback provider may run in *shadow*); multi-page uploads with W-9 on page 3 → page classifier (deferred; v1 reads page 1 and flags if page 1 isn't a W-9).

## 2.2 Extraction layer

**Purpose:** OCR evidence → typed fields with calibrated per-field confidence. **Inputs:** `OcrResult`. **Outputs:** `ExtractedW9` (internal; holds plaintext TIN). Three interchangeable implementations behind one interface (`extractor.py`):

1. **`LayoutExtractor` (deterministic).** Finds labels fuzzily on squashed text (`"6CitytateandZPcod"` still finds “City, state, and ZIP code”), takes the value under each label, excludes the right-hand “Requester’s name and address” block (a classic trap — it is the *requester's* address), repairs OCR spacing (`2450BayshorePkwy` → `2450 Bayshore Pkwy`) with a confidence penalty, reads the TIN by position (which box → SSN vs EIN), detects **checkboxes** by locating the square's edges and measuring interior ink (relative to the median box, so scan darkness doesn't matter), detects the **signature** by ink density in the signature band after removing the ruled line, and parses the date. Free, ~10 ms, explainable — but brittle to unseen layouts.
2. **`LLMChain` → `LLMExtractor` (Gemini first, Groq fallback).** `llm_clients.py` holds one small transport class per provider (Gemini `gemini-2.5-flash` via its OpenAI-compatible Chat Completions; Groq `openai/gpt-oss-20b` via the Responses API); `llm_extractor.py` holds the provider-independent logic. The chain tries Gemini and, on an HTTP error status, timeout or unusable JSON, sends the same request to Groq. Details of the extractor: Input: numbered Markdown lines from LlamaParse (or OCR lines), **TIN redacted**; **no image** (the model is text-only). Output: one JSON object validated by Pydantic (one retry on invalid JSON, then the hybrid falls back to layout-only and flags `SECOND_READER_UNAVAILABLE`). Every value cites `source_line_ids`, a categorical `certainty`, and `issues[]`. Temperature 0, low reasoning effort. Because it cannot see marks, `signature_present` and `tax_classification` come back `NOT_ASSESSED` unless the text shows an explicit marker (☒/[x]); reconciliation treats that as no opinion, so the layout reader's pixel analysis decides.
3. **`HybridExtractor` (production default).** Runs both, reconciles per field: agree ⇒ `1-(1-a)(1-b)`; disagree ⇒ keep the more confident value at `min(a,b)×0.6` and raise `EXTRACTOR_DISAGREEMENT`. The TIN is always deterministic.

**Prompt** (full text in `prompts/w9_extraction_system.md`). Key design choices:
* Field definitions **by W-9 line number** and both revisions' label wording (2018 “Name (as shown on your income tax return)” vs 2024 “Name of entity/individual”).
* Explicit traps: printed option labels are not values; “Sign Here” is not a signature; ignore the requester block.
* **“Prefer null + low over a plausible guess.”** Missing must be representable.
* Mandatory citations (`source_line_ids`) → enables grounding.
* Wrong-form instruction: set `form_type`, null all fields, explain in `issues`.

```text
SYSTEM (abridged)
You are a document-extraction engine for US IRS Form W-9 ... You output ONLY JSON that conforms to the schema. You never guess.
- legal_name: Line 1 ... - address: Line 5. Ignore the "Requester's name and address" block.
- tax_classification and signature_present: you only see text, not pixels. Answer NOT_ASSESSED unless the text
  shows an explicit marker (e.g. [x] / checked box). Printed option labels are NOT values.
- The TIN has been redacted ([TIN REDACTED]); never output one.
Rules: copy values exactly; cite source_line_ids for every non-null value; certainty high|medium|low; prefer null over a guess.
USER
Parsed text lines (id, text):
[L9]  1 Name of entity/individual. An entry is required...
[L10] Acme Industrial Supply, Inc.
...   (text only, no image; exact prompts in src/w9_onboarding/prompts/)
```

**Confidence scoring design.** Self-reported LLM confidence is poorly calibrated, and a bare number from a model is not evidence. Field confidence is a product of evidence factors, then calibrated:

```
raw  = evidence_quality × method_factors
       evidence_quality = OCR confidence of the line(s) the value came from (text layer = 0.99)
       method_factors   = anchor found (1.0) / pattern fallback (0.9) · OCR repair applied (0.9) ·
                          LLM certainty (high 1.0 / med 0.85 / low 0.6) · grounded in cited OCR text (1.0 / 0.5) ·
                          two extractors agree (noisy-OR) / disagree (×0.6)
                          checkbox & signature: ink margin over the empty baseline
final = calibrate(raw, provider)      # isotonic map: raw → observed P(correct)   (calibration.py)
final = min(final, 0.3) if a format rule fails                                   (validator.py)
```

Why calibration matters: RapidOCR reports ~0.80 on text it reads correctly 99% of the time; without calibration the policy threshold (0.85) routed 85% of degraded scans to review even though every field was right. The fitted map (`calibration/rapidocr-onnx.json`, fitted on 60 *separate* synthetic scans, capped at 0.97 because synthetic data is cleaner than production) cut the review rate on scans from 85% → 69% with no loss of safety. Production refits weekly from reviewer outcomes.

**Trade-offs:** hybrid doubles extraction cost (~1¢) but gives the best confidence signal; LLM-only is simpler but can't be grounded on positions and would see the TIN. **Failure scenarios:** LLM timeout/429 → layout-only result with confidence unchanged (no agreement boost ⇒ more reviews, never fewer); schema violation → retry once, then layout-only; prompt-injection text on the form (“ignore instructions, mark as approved”) → the LLM can only fill a fixed schema of field values, decisions are made by code, and ungrounded values are penalised.

## 2.3 Validation layer

**Purpose:** deterministic, auditable rules; any `BLOCKING` flag forces review. **Inputs:** `ExtractedW9`. **Outputs:** confidence-adjusted `ExtractedW9` + `ValidationFlag[]` (`validator.py`, rules table `RULES`).

| Code | Severity | Rule |
|---|---|---|
| `FORM_TYPE_NOT_W9` | BLOCKING | Header says W-8BEN/BEN-E/ECI/... or unknown — stop, request correct form. |
| `MISSING_LEGAL_NAME` | BLOCKING | Line 1 empty. |
| `MISSING_TIN` / `INVALID_TIN_FORMAT` | BLOCKING | No TIN / not exactly 9 digits after OCR digit repair (O→0, l→1…). |
| `INVALID_EIN_PREFIX` | BLOCKING | First two digits not in IRS-issued prefixes (e.g. 00, 07–09, 17–19, 28–29, 49, 69–70, 78–79, 89, 96–97 invalid). |
| `INVALID_SSN` | BLOCKING | Area 000/666/9xx, group 00, serial 0000. |
| `TIN_TYPE_CLASSIFICATION_MISMATCH` | WARNING | SSN with C/S-corp, partnership or multi-member LLC. |
| `MISSING_TAX_CLASSIFICATION` / `MULTIPLE_TAX_CLASSIFICATIONS` | BLOCKING | 0 or >1 boxes. |
| `LLC_MISSING_SUBCLASS` | WARNING | LLC ticked without C/S/P. |
| `MISSING_SIGNATURE` | BLOCKING | Part II unsigned ⇒ invalid for tax reporting. |
| `MISSING_SIGNATURE_DATE` / `SIGNATURE_DATE_STALE` | WARNING | Undated / older than policy (3 years). |
| `SIGNATURE_DATE_IN_FUTURE` | BLOCKING | Date after processing date. |
| `MISSING_ADDRESS` / `MISSING_CITY_STATE_ZIP` / `INVALID_STATE` / `INVALID_ZIP` | BLOCKING | Line 5/6 incomplete or invalid (USPS state codes incl. territories; ZIP 5 or ZIP+4). |
| `PO_BOX_ADDRESS`, `OLD_FORM_REVISION` | INFO | Recorded for audit; not blocking. |
| `LOW_CONFIDENCE_CRITICAL_FIELD` | BLOCKING | legal_name, tin, tax_classification, signature_present < 0.85. |
| `LOW_CONFIDENCE_FIELD` | WARNING | other fields < 0.70. |
| `EXTRACTOR_DISAGREEMENT` / `UNGROUNDED_VALUE` | WARNING | Hybrid readers disagree / LLM value not in its cited OCR lines. |

Design notes: validation *feeds back* into confidence (a format-invalid TIN cannot be 0.99); low-confidence flags are suppressed for a field that already has a specific blocking flag (one clear reason, not three). Deferred: USPS address verification (CASS), exemption-code vs classification rules (Line 4), IRS TIN Matching (async, downstream).

## 2.4 Matching layer

**Purpose:** find whether this supplier already exists *in this tenant* and what would change. **Inputs:** validated `ExtractedW9`, tenant repository. **Outputs:** `MatchResult{strategy, best_candidate, candidates[top-3], change_set[]}` (`matcher.py`).

**Hybrid signal hierarchy**

1. **Exact TIN match** — `HMAC-SHA256(tenant_key, tin)` equality against the master's blind index. Strongest identity signal: the TIN is the IRS's identity of the payee. No decryption, constant-time lookup.
2. **Fuzzy legal name** — normalise (lowercase, `&`→`and`, strip punctuation, strip trailing legal suffixes `inc|corp|co|llc|ltd|lp|...`, leading “the”), then `max(token_sort_ratio, token_set_ratio × 0.90)` (×0.75 when the shorter name is one token — subset matches are the main false-positive source).
3. **Fuzzy DBA** — DBA↔DBA *and* cross DBA↔legal (suppliers routinely swap them: “Gonzalez Landscaping” vs “Maria L Gonzalez”).
4. **Address similarity** — USPS-style abbreviation normalisation (`Street→st`, `Parkway→pkwy`, `Suite→ste`), `0.70 × token_sort(street) + 0.20 × ZIP5 equal + 0.10 × city equal`.

`composite = 0.70 × max(legal, dba) + 0.30 × address`. **Candidate retrieval (blocking)** before scoring: union of top-20 by `token_set_ratio` on normalised names and all suppliers in the same ZIP5 (in production: a per-tenant Azure AI Search index with a normalised-name field + ZIP filter), so cost is O(candidates), not O(master size) — tens of thousands of rows is fine.

**RapidFuzz examples** (from `eval/name_pairs.csv`, after normalisation):

| A | B | score | |
|---|---|---|---|
| Acme Corporation | ACME CORPORATION INC | 100 | suffixes stripped |
| Contoso Ltd. | Contoso Limited | 100 | |
| Tailspin Toys Co | Tailspin Toy Company Inc | 96 | plural drift |
| Summit Peak | Summit Peak Consulting LLC | 90 | subset (discounted) |
| Gonzalez Landscaping | Gonzales Landscaping | 95 | **different** businesses |
| Acme Industrial Supply Inc | Acme Packaging Co | 40 | shared brand token |

**Thresholds and reasoning.** On the labelled pair set: threshold 80 → precision 0.70/recall 1.00; 90 → 0.80/0.95; 95 → 0.95/0.95. **Names alone are not safe to auto-merge below 95** (and even 95 admits “Gonzalez/Gonzales”), which is why the bands below are applied to the *composite* and TIN evidence dominates:

| Composite (no TIN match) | Band | Meaning |
|---|---|---|
| ≥ 95 | `AUTO_MATCH` | Same entity *if TIN evidence doesn't contradict* (legacy record without TIN + address ≥ 90 ⇒ auto-update & TIN backfill). |
| 80 – 95 | `REVIEW` | Plausible duplicate; a human decides. |
| < 80 | `NO_MATCH` | New supplier. |

Plus TIN-aware overrides: candidate with a *different* TIN on file needs composite ≥ 90 to be suspicious (otherwise it's a different legal entity with a similar name — very common, e.g. franchises); candidate with *no* TIN on file and name ≥ 90 is always reviewed (we can't rule it out). **Change set:** cosmetic differences aren't changes (“1200 West Lake Street” = “1200 W Lake St”); we never propose overwriting known master data with an unknown/blank value from the form. `legal_name`, `tin`, `tax_classification` are **material**.

**Failure scenarios:** master API down → retry, then `HUMAN_REVIEW` with `MATCH_UNAVAILABLE` (never “create new” on missing evidence — that's how duplicates are born); master contains its own duplicates (two rows share a TIN, or “Proseware Inc”/“PROSEWARE, INC.”) → `DUPLICATE_TIN_IN_MASTER` / `AMBIGUOUS_CANDIDATES`.

## 2.5 Decision engine

**Purpose:** policy. Ordered, first-match-wins rules; every rule writes `reasoning[]`, review rules add a `review_reasons[]` code (`decision_engine.py`).

```
0  unreadable                                   → HUMAN_REVIEW  DOCUMENT_UNREADABLE
1  not a W-9                                    → HUMAN_REVIEW  FORM_TYPE_NOT_W9
2  TIN matches ≥2 suppliers                     → HUMAN_REVIEW  DUPLICATE_TIN_IN_MASTER        (suggest UPDATE best)
3  TIN matches 1 supplier
     name ≥ 85 and no material change           → UPDATE_EXISTING_SUPPLIER (W-9 refresh / address change)
     name ≥ 85 and material change              → HUMAN_REVIEW  MATERIAL_CHANGE
     name < 85                                  → HUMAN_REVIEW  TIN_NAME_CONFLICT              (restructure or redirect fraud)
4  no TIN match, best candidate ...
     TIN DIFFERENT, composite ≥ 90              → HUMAN_REVIEW  NAME_MATCH_TIN_CONFLICT        (duplicate or impostor)
     TIN DIFFERENT, composite < 90              → CREATE_NEW_SUPPLIER (different legal entity)
     ≥2 candidates within 3 pts above 80        → HUMAN_REVIEW  AMBIGUOUS_CANDIDATES
     TIN MISSING_IN_MASTER, comp ≥ 95, addr ≥ 90→ UPDATE_EXISTING_SUPPLIER (+ TIN backfill)
     composite 80–95, or name ≥ 90 & no TIN      → HUMAN_REVIEW  FUZZY_MATCH_REVIEW_BAND
5  nothing ≥ 80                                 → CREATE_NEW_SUPPLIER
6  ANY BLOCKING validation flag                 → HUMAN_REVIEW (overrides 3–5; the proposed action becomes suggested_action)
confidence = min(critical field confidences) × identity confidence
```

**Examples (from the prototype run):**

| Sample | Decision | Why |
|---|---|---|
| 02 Acme Industrial Supply, Inc. (master: `ACME INDUSTRIAL SUPPLY INC`) | UPDATE → SUP-1001, 0.96 | TIN match, name 100, “W West Lake Street” = “W Lake St” ⇒ no changes: W-9 refresh. |
| 03 Northwind Traders, new address | UPDATE → SUP-1002, 0.96 | TIN + name match; address is a non-material change listed in `change_set`. |
| 04 “Contoso Ltd.” same address, **different EIN** | HUMAN_REVIEW `NAME_MATCH_TIN_CONFLICT` | Looks identical but TIN disagrees: duplicate entity or impostor. |
| 05 Fabrikam's EIN under “Blue Yonder Freight Partners LP” | HUMAN_REVIEW `TIN_NAME_CONFLICT` | Classic payment-redirection pattern; suggestion pre-filled (UPDATE SUP-1004, 0.38). |
| 08 “Tailspin Toys Co” vs legacy “Tailspin Toy Company Inc” (no TIN), other city | HUMAN_REVIEW `FUZZY_MATCH_REVIEW_BAND` | Name 96, address 13 — can't confirm or rule out. |
| 12 2018 revision, legacy “Wide World Importers” w/o TIN, same address | UPDATE → SUP-1007 + TIN backfill | Name and address both corroborate. Stale signature = warning only. |
| 06 unsigned | HUMAN_REVIEW `MISSING_SIGNATURE` | Valid identity, invalid document. |

`HUMAN_REVIEW` is never a dead end: `suggested_action` + codes let the review tool pre-fill the screen, and each reviewer decision becomes a labelled example (§6).

---

# Part 3 — Security architecture

| Area | Design (Azure-oriented) | Prototype |
|---|---|---|
| **PII inventory** | TIN (SSN = highest sensitivity), names of individuals (sole proprietors), addresses, signatures (biometric-adjacent), bank refs (only via master). Classified *Restricted*; data-flow diagram reviewed by security. | — |
| **TIN protection** | (1) Read deterministically, never sent to the LLM (text + image redaction). (2) Immediately tokenised: plaintext → TIN vault (envelope-encrypted: AES-256 DEK per tenant, wrapped by a Key Vault Managed-HSM KEK); response carries `masked`, `last4`, `fingerprint`, `vault_ref`. (3) Matching via HMAC blind index with a **per-tenant** key ⇒ no decryption for matching, unlinkable across tenants. (4) Detokenisation is a separate API with a separate entitlement, rate-limited and audited per call. | `security.TinVault` (placeholder crypto, real seam), `tin_fingerprint`, `mask_tin`, `redact_for_llm`; test proves the response never contains the TIN. |
| **Encryption** | In transit: TLS 1.2+, private endpoints for Blob/Key Vault, egress allow-list limited to the two vendor hosts. At rest: Storage SSE with **customer-managed keys** (per-tenant key option for enterprise tenants), Cosmos/SQL TDE with CMK, field-level encryption for TIN. | — |
| **Tenant isolation** | Tenant from token claim only; per-tenant blob containers with ABAC; repository bound to one tenant; per-tenant HMAC keys; per-tenant config; tests that a cross-tenant `vault_ref` is refused. | `CsvSupplierMaster` validates tenant id (blocks `../`); `TinVault.reveal` refuses foreign refs; sample #13. |
| **Third-party processors** | **LlamaParse sees the whole document including the unredacted TIN** (it has to, to parse it); Gemini and Groq see only TIN-redacted text. Before real PII: signed DPA/sub-processor entry, zero-data-retention or shortest retention, no training on customer data, approved region, SOC 2 evidence, breach-notification terms, and an egress allow-list. Only these two hosts are reachable from the worker. If a customer will not accept a third-party parser, keep them on the local path (text layer + RapidOCR) or a private-endpoint parser. **Gemini: use a paid / no-training API tier, since free-tier terms can allow use of prompts for product improvement.** Prompts/completions are logged only as hashes + token counts. | TIN-shaped strings redacted from Markdown and lines before any LLM call (tested). Keys come only from env vars (`LLAMA_CLOUD_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`) or a git-ignored `.env`. |
| **Audit logging** | Append-only audit event per decision: who/what/when, tenant, doc sha256, model+prompt+policy versions, field confidences, flags, decision, reasons — **no PII values** (fingerprints/last4 only). Stored in immutable storage (Blob WORM / Log Analytics with retention lock) for ≥ 7 years (tax record horizon). Reviewer actions appended to the same chain. | `reasoning[]`, `processing{}` versions, structured logs. |
| **Logging hygiene** | Allow-list logging (ids, codes, scores only) + `RedactingFilter` that scrubs TIN-shaped strings as defence in depth; App Insights sampling never includes request bodies. | `observability.JsonFormatter` + `RedactingFilter`; tested. |
| **Secrets** | No secrets in code or config: **Managed Identity** to Storage/Key Vault, and the two vendor API keys held in Key Vault (rotated; never in code, docs or chat); HMAC/DEK keys in Key Vault (Managed HSM), rotation with key versioning (fingerprints carry key version; dual-read during rotation). | Dev key via env var `TIN_HMAC_MASTER_KEY` with an obviously fake default. |
| **Retention** | Raw W-9 retained per tenant policy (default 90 days in our store; system of record keeps the official copy); OCR text purged with the job; DSAR/deletion via tenant-scoped purge. | — |
| **Abuse/fraud** | Upload limits, magic-byte sniffing, PDF render in sandboxed worker, malware scan (Defender for Storage) before processing; prompt-injection contained by schema-only LLM output + code decisions. | Type/size/page checks. |

---

# Part 4 — Observability

**Structured logs** (JSON, one line per span/event; `observability.py`): `trace_id, tenant_id, request_id, document_id, span, duration_ms, status`, final `document_processed{decision, confidence, review_codes, match_strategy, extractor, ocr}`. Never field values.

**Metrics** (OTel → Azure Monitor; prototype exports `outputs/_metrics.json`):

| Metric | Type | Labels | Alert / use |
|---|---|---|---|
| `w9_documents_total` | counter | tenant, decision | Volume; **human-review rate** per tenant (SLO e.g. ≤ 25%). |
| `w9_review_reasons_total` | counter | code | Which reasons drive review cost; spikes in `TIN_NAME_CONFLICT` = fraud campaign signal. |
| `w9_validation_flags_total` | counter | code, severity | Data-quality trends (e.g. `FORM_TYPE_NOT_W9` ⇒ customer requesting the wrong form). |
| `w9_stage_latency_ms` | histogram | stage | p95 per stage; parser/LLM latency SLOs. |
| `w9_stage_errors_total`, `w9_unreadable_documents_total` | counter | stage / tenant | Error budget. |
| `w9_field_confidence` | histogram | field, provider | **Confidence drift.** |
| `w9_best_match_composite` | histogram | — | **Match drift.** |
| `w9_llm_tokens_total`, `w9_cost_usd` | counter | tenant, model | Cost per doc / per tenant. |
| `w9_extractor_agreement_rate` | gauge | field | Hybrid agreement — early warning of layout or model change. |

**Tracing:** one trace per document across API → queue → worker → LlamaParse → Gemini (→ Groq) → master API, so a slow or failed document is explainable end-to-end; `request_id` returned to the caller.

**Confidence drift monitoring:** daily distribution of `w9_field_confidence` per field/provider vs. a 28-day baseline (PSI or KS; alert at PSI > 0.2), plus *calibration drift*: reviewer-corrected fields give observed accuracy per confidence bin; alert when a bin's observed accuracy falls > 5 pts below its calibrated value (the map is lying ⇒ refit or raise thresholds). Triggers: new form revision, a tenant uploading a new scanner's output, a model/version change on LlamaParse, Gemini or Groq.

**Match quality monitoring:** (a) reviewer overrides by reason (if `FUZZY_MATCH_REVIEW_BAND` is approved 98% of the time, the band is too wide); (b) **post-hoc false-merge detection** — downstream reversals of auto-updates and duplicate-supplier merges found later by the master's own dedup job are joined back to our `document_id` and counted as false matches/duplicate creations; (c) weekly sampled audit: 50 automated decisions per week re-reviewed blind. These feed the KPIs in §6.

---

# Part 5 — Cost analysis

**Assumptions (all vendor prices UNVERIFIED — check the LlamaCloud, Google AI and Groq price pages before quoting; they are env-overridable in `config.py`):** LlamaParse is credit-based, roughly **fast 1 / cost_effective 3 / agentic 10 / agentic_plus 45 credits per page** at about **$0.00125 per credit** (so agentic ≈ $0.0125/page). Groq `gpt-oss-20b` about **$0.075 / 1M input and $0.30 / 1M output** tokens. One page per W-9. Prompt ≈ 750 system + ~1,200 parsed text ≈ **2,000 input**; output incl. reasoning ≈ **1,000** tokens. 50% of documents are digital PDFs with a usable text layer (no parser call).

| Path | Parse | LLM | Total per doc |
|---|---|---|---|
| Scan/photo, LlamaParse agentic + Groq | 10 cr ≈ $0.0125 | 2,000×$0.075e-6 + 1,000×$0.30e-6 ≈ $0.0005 | **≈ $0.013** |
| Scan/photo, LlamaParse cost_effective + Groq | 3 cr ≈ $0.0038 | ≈ $0.0005 | **≈ $0.004** |
| Digital PDF, text layer + Groq | $0 | ≈ $0.0005 | **≈ $0.0005** |
| Digital PDF, text layer + Gemini 2.5 Flash (≈ $0.30 in / $2.50 out per 1M, unverified) | $0 | 2,000 in + 1,000 out ≈ $0.0031 | **≈ $0.003** |
| Blended (50/50, agentic) + 10% retries | | | **≈ $0.0075** |
| Blended (50/50, cost_effective) + 10% retries | | | **≈ $0.0025** |
| Prototype offline (text layer + RapidOCR, layout only) | $0 | $0 | $0 (compute only) |

The tier is the cost lever: agentic is about 25× the LLM cost, so it should be used only if measured accuracy justifies it. A cascade (cost_effective first, agentic only for low page confidence) is the obvious next step. Human review (~$0.25 per document at 2 minutes) still dominates, which is why review rate matters more than any of this.

**At A1 volume** (~65k docs/month): blended ≈ **$490/month** (agentic) or **≈ $160/month** (cost_effective) in AI services (+ ~$300–600 for compute, storage, monitoring). Per tenant ≈ $2–3/month.

**The real cost is human review:** at ~3 min per review × ~$35/h loaded ≈ $1.75/review. A 25% review rate costs **$0.44 per document — ~25× the AI cost**. Therefore every design decision that trades AI spend for lower review rate (hybrid extraction, calibration, better matching) is worth it, and cost optimisation should start with the review rate, not tokens. `processing.estimated_cost_usd` is returned per document so FinOps can attribute cost per tenant.

---

# Part 6 — Evaluation framework

**KPIs and definitions** (implemented in `eval/evaluate.py`):

| KPI | Definition | Target (initial) |
|---|---|---|
| Extraction accuracy | fields correct (after normalisation) / fields, incl. correct nulls | ≥ 98% digital, ≥ 95% scans |
| Field-level precision | correct non-null predictions / non-null predictions (per field) | ≥ 99% for TIN & legal name |
| Field-level recall | correct non-null predictions / non-null ground truth | ≥ 97% |
| Calibration | per confidence bin, observed accuracy ≈ bin; ECE | ECE ≤ 0.03 |
| Match precision | auto-updates to the right supplier / auto-updates | ≥ 99.5% |
| **False match rate** | wrong auto-updates / auto-updates (incl. should-have-been-new) | **≤ 0.1%** (hard gate) |
| Duplicate creation rate | auto-creates of an existing supplier / auto-creates | ≤ 1% |
| Human review rate | HUMAN_REVIEW / all (vs. the *necessary* review rate) | ≤ 25%, trending down |
| Review-reason accuracy | expected reason code present | ≥ 95% |

**Datasets.**
1. *Scenario set* (`samples/`, 13 docs, each a deliberate edge case with ground-truth fields and expected decision/reason) — regression gate on every change.
2. *Robustness variant* — the same set re-rendered as degraded phone photos (`--variant scan`).
3. *Name-pair set* (`eval/name_pairs.csv`, 40 labelled pairs incl. hard negatives) — threshold selection for the matcher.
4. *Calibration set* (60 random synthetic scans, disjoint from 1–3).
5. *Production (next)*: 300–500 real W-9s per quarter, stratified by source (digital/scan/photo/handwritten/2018 revision/wrong form), double-labelled with adjudication; reviewer decisions continuously captured as labels (with sampling to counter selection bias — reviewers only see routed docs, so we also blind-review a random 1% of automated decisions).

**Results from the prototype** (layout extractor, offline; full tables in `eval/report_clean.md`, `eval/report_scan.md`):

| | Clean originals | Degraded phone-photo re-renders |
|---|---|---|
| Field accuracy (11 fields × 12 W-9s) | 100% | 100% |
| Decision accuracy | 100% (13/13) | 77% (10/13) |
| False match rate | 0% | **0%** |
| Duplicate creation rate | 0% | **0%** |
| Human review rate (necessary = 46%) | 46% | 69% |

The three scan “misses” are all LLC subclass letters recovered by weak single-glyph re-OCR — values were *right*, but confidence was deliberately discounted, so they went to review. That is the system working as designed: errors degrade into review cost, not wrong merges. The live hybrid run (LlamaParse + Gemini → Groq) matched all 13 expected decisions, including the phone photo, but it was one run on synthetic originals, not a re-measurement of the degraded set. *Caveat:* 100% on synthetic documents means the harness works, not that the system is production-ready; real-world numbers come from dataset 5.

**Release gates:** a prompt/model/threshold change ships only if, on the frozen sets, false match rate does not increase and review rate does not increase by > 2 pts; shadow-mode for one week on live traffic before switching.

---

# Part 7 — Prototype design (repository)

**Layering (dependencies point inward, enforced by `tests/test_architecture.py`).** `domain` (entities and value objects) depends on nothing. `utils` (pure helpers) and `dto` (the public response contract) depend only on config / domain. `repositories` (supplier master, TIN vault) and `infrastructure` (OCR providers, LLM clients) are adapters at the edge and never import `services`. `services` holds the application logic, namely the pipeline, validation, matching, decisions and extraction. `presentation` (the CLI) is the outermost layer. The rule modules never import an adapter, so swapping LlamaParse, Gemini or the CSV master touches one file.

```
Supplier-Onboarding-service/
├── src/
│   ├── main.py                          launcher (`python src/main.py ...`)
│   └── w9_onboarding/
│       ├── config.py                    thresholds, policy, prices, paths (every number that changes a decision)
│       ├── presentation/cli.py          CLI: process | batch | schema
│       ├── services/
│       │   ├── pipeline.py              orchestration of the 7 stages, spans, metrics, error → "I don't know" response
│       │   ├── validator.py             IRS/business rules → flags + confidence caps
│       │   ├── matcher.py               TIN blind-index + RapidFuzz name/DBA/address scoring, change set
│       │   ├── decision_engine.py       ordered decision rules, review reasons, suggested action
│       │   ├── response_builder.py      PII choke point: domain → DTO, TIN → vault
│       │   ├── calibration.py           isotonic confidence calibration per OCR provider
│       │   └── extraction/              extractor.py (hybrid + factory), layout_extractor.py, llm_extractor.py (+ LLMChain)
│       ├── domain/models.py             entities, enums, value objects; depends on nothing else
│       ├── dto/onboarding_response.py   public response contract v1
│       ├── repositories/                supplier_master.py (tenant-scoped; CSV now, API/AI Search later), tin_vault.py
│       ├── infrastructure/              ocr.py (text layer, LlamaParse, RapidOCR), llm_clients.py (Gemini, Groq)
│       ├── utils/                       normalize.py, observability.py, security.py (HMAC fingerprint, masking, log redaction)
│       └── prompts/                     LLM system + user prompts (text-only)
├── samples/              13 synthetic W-9s, per-tenant supplier masters, ground_truth.json
├── calibration/          fitted calibration map (+ raw points)
├── schemas/              onboarding_response.v1.json (generated from Pydantic)
├── examples/             the JSON the service returns for every sample
├── eval/                 evaluate.py, name_pairs.csv, report_clean.md, report_scan.md
├── scripts/              generate_samples.py, fit_calibration.py, live_smoke.py, diff_readers.py
├── tests/                75 tests (rules, security, end-to-end, LLM grounding, architecture, all offline)
├── docs/                 DESIGN.md (this), INTERVIEW_DEFENCE.md
├── requirements.txt
└── README.md
```

The brief's suggested layout (`main / extractor / matcher / validator / decision_engine / models`) is preserved; the extra modules exist because each is a seam that changes for a different reason (OCR vendor, security controls, policy, contract).

# Part 8 — Implementation notes

The code *is* the implementation of Parts 1–6, not a separate exercise: every rule table above maps to a named function, every review code above is emitted by `decision_engine.py`, and every number is in `config.py`. Python 3.10+, Pydantic v2, type hints throughout; no stage imports a later stage. **Honest status:** the offline path (text layer + RapidOCR + layout extractor) is fully exercised by tests and eval. The LlamaParse provider and the Gemini → Groq chain have been **run live on all 13 synthetic samples (13/13 decisions match)**; caveats are in *Deferred* below (synthetic data, one reconcile fix made after seeing a result, Gemini-outage fallback only tested with fakes, prices unverified). The TIN vault uses placeholder XOR “encryption” to mark the seam.

# Part 9 — Output contract

Full JSON Schema: `schemas/onboarding_response.v1.json`. Contract rules: `schema_version` follows semver — additive fields only within v1; enums (`decision`, `review_reasons[].code`, `validation_flags[].code`) are closed sets documented in this file, and consumers must treat unknown codes as “review”; consumers **branch on `decision` and codes, never on `reasoning` text**.

```json
{
  "schema_version": "1.0",
  "request_id": "65223e60688440da9286bdcedae5a14f",
  "tenant_id": "tenant_acme_mfg",
  "document_id": "doc_c57a41ada8800e71",
  "decision": "UPDATE_EXISTING_SUPPLIER",
  "confidence": 0.96,
  "matched_supplier_id": "SUP-1002",
  "suggested_action": null,
  "review_reasons": [],
  "document": { "filename": "03_existing_address_change.pdf", "sha256": "c57a41…", "mime_type": "application/pdf",
                "page_count": 1, "form_type": "W-9", "form_revision": "2024-mar" },
  "extracted_fields": {
    "legal_name":         { "value": "Northwind Traders LLC", "confidence": 0.99, "source": "layout:value-below-label", "status": "OK" },
    "dba_name":           { "value": null, "confidence": 0.9, "source": "layout", "status": "NOT_PROVIDED" },
    "tax_classification": { "value": "LLC_C", "confidence": 0.99, "source": "layout:checkbox-ink", "status": "OK" },
    "address":            { "value": "900 Pacific Ave Suite 300", "confidence": 0.99, "status": "OK", "source": "…" },
    "city": { "value": "Tacoma", "…": "…" }, "state": { "value": "WA", "…": "…" }, "zip": { "value": "98402", "…": "…" },
    "tin": { "masked": "**-***8476", "last4": "8476", "tin_type": "EIN",
             "fingerprint": "hmac256:f19524aac70f831c989e686400bd332b",
             "vault_ref": "vault://tenant_acme_mfg/tin/b7beddaa9b324e8d", "confidence": 0.99, "status": "OK" },
    "signature_present":  { "value": true, "confidence": 0.97, "source": "layout:signature-ink", "status": "OK" },
    "signature_date":     { "value": "2026-09-21", "confidence": 0.99, "source": "layout:date", "status": "OK" }
  },
  "validation_flags": [],
  "match_result": {
    "strategy": "TIN_EXACT",
    "best_candidate": { "supplier_id": "SUP-1002", "legal_name": "Northwind Traders LLC", "tin_relation": "SAME",
      "scores": { "tin_exact": true, "legal_name": 100.0, "dba_name": 0.0, "address": 44.7, "composite": 83.4 },
      "band": "REVIEW" },
    "candidates": ["… top 3 …"], "candidates_considered": 2,
    "change_set": [ { "field": "address", "current": "455 Harbor Blvd", "proposed": "900 Pacific Ave Suite 300", "material": false } ]
  },
  "reasoning": [
    "TIN matches existing supplier SUP-1002 (fingerprint equality); name similarity 100.",
    "Non-material changes vs master: address.",
    "Critical-field extraction confidence 0.97; identity confidence 0.99.",
    "Automated decision: UPDATE_EXISTING_SUPPLIER -> SUP-1002."
  ],
  "processing": { "pipeline_version": "0.3.0", "policy_version": "2026-10-06.1", "ocr_provider": "pdf-text-layer",
                  "extractor": "layout-v1", "latency_ms": { "ocr": 145.5, "extract": 7.5, "match": 0.3 },
                  "estimated_cost_usd": 0.0, "processed_at": "2026-10-06T10:52:12Z" }
}
```

Field semantics a stranger needs: `confidence` = engine's confidence in the proposed resolution (for `HUMAN_REVIEW` it equals `suggested_action.confidence`, and a low value explains *why* it was routed); per-field `status` ∈ `OK | LOW_CONFIDENCE | MISSING | NOT_PROVIDED`; `band` is the name+address band alone, `tin_relation` ∈ `SAME | DIFFERENT | MISSING_IN_MASTER | MISSING_ON_FORM`; `change_set[].material = true` means the consumer must not apply it without a human.

---

## Deferred, and why

| Deferred | Why it's safe to defer | Next step |
|---|---|---|
| Live runs | **Run live on all 13 samples (LlamaParse + Gemini → Groq): 13/13 decisions match.** Measured: 11–21 s per document with `agentic` parsing, LLM call 1–3 s, about $0.0125 per parsed page. Caveats: synthetic, one document per scenario, and the document-12 reconcile fix was made after seeing that result, so this is not an accuracy estimate. Not yet exercised live: a Gemini failure falling back to Groq (fake-client tests only), vendor prices, behaviour under load. | Run 30–50 real, redacted W-9s; refit calibration (`fit_calibration.py --ocr llamaparse`); try `cost_effective` tier. |
| Handwritten forms | Safety holds (low OCR confidence ⇒ review); volume share unknown. | Add handwritten samples; test whether LlamaParse agentic reads them. |
| Multi-page / page classification | Most W-9s are 1 data page; wrong page ⇒ `FORM_TYPE_NOT_W9`, not a wrong decision. | A page classifier or first-page heuristics. |
| IRS TIN Matching, OFAC, bank change detection | Downstream services; we emit fingerprint + names. | Async enrichment events. |
| Exemption codes (Line 4), 3b foreign-partner box | Rarely populated; no decision impact in v1. | Add fields + rules. |
| Per-tenant threshold UI, review-feedback loop | Config exists; loop needs the review tool. | Capture reviewer labels → weekly calibration refit. |
| Country packs (W-8, CA, UK) | US-only assumption. | `FormProfile` = anchors + prompt + rules per form. |
