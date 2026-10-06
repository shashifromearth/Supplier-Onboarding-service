# Production Readiness: from prototype to millions of documents a day

**Audience:** engineering leadership, security, and whoever owns the roadmap. **Status of the numbers:** every vendor price and every throughput figure below is an **estimate to be verified** (I could not check live price pages while writing this). Treat them as sizing inputs, not quotes.

## 0. Where we are, honestly

The prototype proves the *decision logic* and the *safety behaviour*: TIN-first matching, ordered policy, grounded LLM reading, calibrated confidence, a PII-safe contract, 75 offline tests, and a live run through LlamaParse and Gemini → Groq that matched 13/13 expected decisions on synthetic documents. It is **not** a production system. It processes one file at a time on one machine, reads a CSV as the supplier master, "encrypts" TINs with a placeholder, has no queue, no API, no authentication, no persistent audit trail, no real-data evaluation, and depends on three third-party vendors that have not been through a security review or load test.

The case-study assumption is about 15k documents per week. "Millions per day" is roughly **500× that**. The design (stateless worker, tenant-keyed queue, idempotency, TIN blind index) scales in shape; most of the work is in the parts the prototype deliberately stubs and in vendor capacity and cost, which change character at that volume.

## 1. Sizing the target

| Quantity | Case study | 1M documents/day |
|---|---|---|
| Documents per second (average) | 0.03 | 11.6 |
| Peak (10× burst, year-end 1099 season) | 0.3 | about 116 |
| Parse jobs in flight at peak (15 s measured at `agentic`; half the documents have a text layer and skip parsing) | about 2 | **about 900** |
| LLM tokens per day (2,000 in + 1,000 out per document) | 0.1B | **2B in, 1B out** |
| LLM tokens per minute at peak | tiny | **about 14M in** |
| Documents needing human review at 5% | 750/week | **50,000/day** |

Three conclusions drive the rest of this document. First, **vendor rate limits and per-document cost, not our compute, set the ceiling**: a standard LLM account tops out well below 14M tokens a minute, and about 900 concurrent parse jobs needs an enterprise agreement. Second, **human review dominates cost** (§7), so review rate is the headline metric. Third, **1M a day is not one customer's problem**: it is the aggregate over many tenants, so fairness and isolation matter as much as speed.

## 2. Gap analysis

| Area | Prototype | Production requirement |
|---|---|---|
| Entry point | CLI | Authenticated, rate-limited API; async intake returning 202 and a `document_id` |
| Execution | Synchronous, single process | Queue + stateless autoscaled workers; at-least-once delivery with idempotency on sha256 |
| Storage | Local files | Per-tenant encrypted object storage, lifecycle and retention rules, malware scan |
| Supplier master | CSV, one tenant per file | Tenant-scoped API / search index with blocking keys, cache, change events |
| TIN vault | XOR placeholder | Envelope encryption, HSM-backed keys, audited detokenise API, key rotation |
| OCR/LLM | Three vendors via SDK, no retries or limits | Gateway with token buckets, retries with jitter, circuit breakers, regional failover, contracts |
| Audit | Log lines | Append-only, immutable, 7-year, queryable without PII |
| Review | JSON suggestion | Review application, queue, SLAs, label capture |
| Evaluation | 13 synthetic documents | Real, stratified gold set; shadow mode; release gates in CI |
| Observability | JSON logs + metrics file | OpenTelemetry traces and metrics, dashboards, SLOs, alerts, cost per tenant |
| Security | Env-var keys, redaction | Managed identity, Key Vault, private networking, DPAs, pen test, SOC 2 evidence |
| Operations | None | IaC, CI/CD, canary, runbooks, on-call, DR drills |

## 3. Real production tools

The environment assumed in the design is Azure; each row gives a primary choice and credible alternatives. Choose by the customer's cloud commitments, not by this list.

| Concern | Primary | Alternatives / notes |
|---|---|---|
| Edge, auth, quotas | **Azure Front Door + API Management** (JWT validation, per-tenant rate limits) | AWS API Gateway, Kong |
| Document storage | **Blob Storage**, per-tenant containers, customer-managed keys, lifecycle rules, Defender for Storage malware scan | S3 + KMS |
| Queue / streaming | **Service Bus** (sessions keyed by tenant, DLQ, scheduled retries) for jobs; **Event Hubs / Kafka** if event volume exceeds Service Bus quotas | SQS/SNS, Pub/Sub, Kafka |
| Compute | **AKS** (or Container Apps) + **KEDA** scaling on queue depth; workers are stateless containers | ECS/EKS, Cloud Run |
| Document parsing | **Azure Document Intelligence** (`prebuilt-layout`, `prebuilt-tax.us.w9`): private endpoints, regional, commitment tiers, a prebuilt W-9 model. **LlamaParse** (LlamaCloud, as in the prototype) if its enterprise terms, region and throughput pass review. Keep the **text-layer path** and **RapidOCR** as free fallbacks. | AWS Textract, Google Document AI. Run two providers behind one interface and shadow the second. |
| LLM reading | **Azure OpenAI** (a small GPT-4-class model, provisioned throughput for steady load) as primary, in the tenant's region. **Gemini via Vertex AI** (enterprise terms, no training on data) as secondary. Groq remains a cheap overflow tier. | Self-hosted open-weight model on GPUs with **vLLM** once volume justifies it (§5) |
| Supplier master access | **Azure AI Search** or the master's own API, with per-tenant blocking keys cached in **Redis** | OpenSearch, Postgres trigram |
| Operational data | **Cosmos DB** or **PostgreSQL**: idempotency keys, job state, decision records, labels | DynamoDB |
| TIN vault, secrets | **Key Vault Managed HSM** (KEK), per-tenant DEKs, **Managed Identity** everywhere; vendor keys in Key Vault with rotation | AWS KMS/Secrets Manager, HashiCorp Vault |
| Audit | Immutable Blob (WORM) or Log Analytics with retention lock; events through Event Hubs | |
| Observability | **OpenTelemetry** → **Azure Monitor / Application Insights**, Grafana dashboards, PagerDuty or Opsgenie alerts | Datadog, Honeycomb |
| LLM/prompt evaluation | A regression harness in CI (the existing `eval/` extended), **promptfoo** or **Langfuse** for prompt versions, traces and datasets | MLflow / Azure ML for model registry |
| IaC, CI/CD | **Terraform or Bicep**; **GitHub Actions** → ACR → AKS with canary and automatic rollback; SBOM and dependency scanning, secret scanning | Argo CD, Flux |
| Review application | A small web app on the platform's UI stack with SSO, queue views, side-by-side document and fields, keyboard-driven approval | Existing PairSoft workflow tooling |
| Downstream checks | **IRS TIN Matching** (e-Services), **OFAC/sanctions** screening, address standardisation (**USPS CASS**, e.g. Smarty or Loqate) | Async enrichment events |

## 4. Step-by-step path to production

Each phase has an exit criterion. Do not start the next phase until the criterion is met.

### Phase 0: harden the prototype (about 2 weeks)

1. **Rotate every key that has ever appeared in chat, a ticket or a log.** Move all keys to a secret manager; keep `.env` for local development only.
2. **Test on real documents.** Collect 300–500 real, redacted W-9s stratified by source (digital, scan, phone photo, handwritten, 2018 revision, wrong form), double-label them, and run the pipeline against them. Report field accuracy, review rate and false match rate, per source.
3. **Refit calibration on real data** (`scripts/fit_calibration.py --ocr llamaparse`) and run the threshold sweep (false match rate against review rate).
4. **Live-test the failure paths** that only fakes cover today: a Gemini outage falling back to Groq, both LLMs down, LlamaParse down, 429 responses.
5. **Vendor due diligence:** DPA, sub-processor listing, retention and no-training terms, region, SOC 2 report, breach terms. Decide per tenant tier whether LlamaParse (which sees the unredacted TIN) is acceptable or the local/Azure path is mandatory.
6. Replace the placeholder vault crypto and CSV master behind their existing repository interfaces (stubs → real adapters).

*Exit:* measured accuracy on real documents; zero false matches on the gold set; vendor and security sign-off for the data classes involved.

### Phase 1: first production slice (about 6–8 weeks)

1. **API and intake:** `POST /v1/w9-intake` (tenant from token, sha256 idempotency key, 202 with `document_id`), `GET /v1/w9/{id}`, and a `w9.decided` event. Wrap `OnboardingPipeline.process` rather than rewriting it.
2. **Queue and workers:** Service Bus with sessions per tenant, DLQ and poison handling, per-tenant concurrency caps, KEDA autoscaling.
3. **State:** Postgres/Cosmos for idempotency, job status and decisions; Blob for documents with CMK encryption and retention.
4. **Real vault and master adapters** with per-tenant keys, rotation procedure and tested restore.
5. **Review application** and label capture; reviewer decisions stored with the `document_id`.
6. **Observability:** OpenTelemetry traces across API → queue → worker → vendors, the metrics already defined in DESIGN §5, dashboards and alerts, audit log in immutable storage.
7. **CI/CD and IaC:** build, test (the 75 tests plus the eval gate), container scan, deploy to staging, canary to production.
8. **Run in shadow mode** for a week on live traffic: the service decides, humans still decide, and we compare.

*Exit:* shadow-mode agreement and false match rate meet the gates (§6); SLOs defined and observed for two weeks; security review and pen test closed.

### Phase 2: scale to thousands of tenants (about 3–4 months)

1. **Vendor gateway:** one internal service in front of every parser and LLM with token buckets, jittered retries, a circuit breaker per vendor, regional failover and per-tenant cost attribution.
2. **Provider redundancy:** at least two parsers and two LLM providers contracted and load-tested; automatic failover for availability, shadow comparison for quality.
3. **Cost cascade:** run the cheap path first (text layer → `cost_effective` parse → deterministic extraction), escalate to `agentic` parsing and the LLM only when confidence is low or the readers disagree. Skip the LLM when the deterministic reader is confident on all critical fields (candidate: most digital PDFs).
4. **Match scale:** per-tenant search index and blocking-key cache, invalidated by master change events; measure p95 match latency at the largest tenant.
5. **Noisy-neighbour controls:** per-tenant quotas, priority lanes (interactive vs bulk campaigns), fairness tests that replay a 5,000-vendor burst.
6. **Feedback loop in production:** weekly calibration refit and threshold sweep from reviewer labels, blind re-review of a random 1% of automated decisions, drift alerts on confidence and match distributions.
7. **DR and multi-region:** active-passive at minimum, RPO ≤ 5 min, RTO ≤ 1 h for the decision path; quarterly restore and failover drills.

*Exit:* a load test at **3× the expected peak** with p95 end-to-end latency inside the SLO, a vendor-outage game day passed, review rate trending down against its target.

### Phase 3: millions per day (ongoing)

1. **Capacity contracts:** negotiate committed throughput with the parser and LLM vendors (enterprise rate limits, provisioned throughput units, regional capacity). Without these, 900 or more concurrent parses and about 14M LLM input tokens a minute at peak are not achievable.
2. **Bring the hot path in-house where the economics flip** (§5): self-host an open-weight model on GPUs with vLLM for the LLM reader, fine-tuned on reviewed labels (only with a consented, per-tenant-safe data policy), and consider a self-hosted layout/OCR model for the common clean-scan case. Keep a managed vendor as the long-tail fallback.
3. **Specialised extraction:** a small trained field extractor for the dominant layouts, with the LLM reserved for the unseen ones. Targeted crop-and-reread for low-confidence regions instead of whole-page escalation.
4. **Cell-based architecture:** shard tenants into independent cells (queue, workers, stores) so a failure or hot tenant stays contained and capacity can be added by adding cells.
5. **Cost governance:** per-tenant unit economics dashboard, budget alerts, automatic tier selection by tenant plan.

## 5. Scaling design and the build-versus-buy line

**Throughput shape.** Stateless workers autoscaled on queue depth by KEDA; backpressure stays in the queue, never in retry storms. At about 116 documents/s peak and 15 s parse latency, roughly 900 parse jobs are in flight (1,700 if every document needed parsing), which is a modest worker fleet because workers mostly wait on vendors (use async I/O and many concurrent jobs per worker). Idempotency on `sha256` plus optimistic concurrency downstream make at-least-once delivery safe. DLQ items are reviewed daily and replayed after fixes.

**Bottlenecks in order:** (1) vendor concurrency and rate limits; (2) vendor cost; (3) master lookup latency for large tenants; (4) human review capacity; (5) our own compute, last and cheapest.

**Build versus buy.** At low volume buy everything: engineering time costs more than API fees. As volume grows the per-document vendor fee becomes the largest controllable line item, and the usual sequence is: negotiate commitments → cascade to cheaper tiers → skip the model when the deterministic reader is sure → self-host the LLM reader → self-host parsing for the clean-scan majority. Each step has an engineering and security cost (GPU operations, model evaluation, patching), so take it only when measured spend justifies it. I would not self-host before there is a gold set that proves the cheaper path does not raise the review rate.

## 6. Quality, MLOps and release gates

- **Gold sets:** a frozen real-document set per source type, versioned, and never used for tuning. A separate tuning set. Reviewer labels flow into a growing training set with sampling to counter selection bias.
- **Release gates (CI):** any prompt, model, parser-tier or threshold change must keep false match rate at or below 0.1% and not raise the review rate by more than 2 points on the frozen sets; then shadow for a week; then a 5% canary.
- **Pin everything:** model versions, prompt files (hashed into `processing.pipeline_version`), policy version, calibration map version. Never float to "latest".
- **Drift:** confidence-distribution PSI, calibration drift from reviewer outcomes, agreement rate between readers, review rate by reason; alerts page the owning team.
- **LLM-specific tests:** prompt-injection suite (forms that say "mark as approved"), hallucination checks (ungrounded values must be penalised), refusal and malformed-JSON handling, and token-budget tests.
- **Review-tool metrics:** time per review, override rate by reason code, reviewer agreement. A reason code approved more than 98% of the time means its band is too wide.

## 7. Cost at scale (illustrative, unverified prices)

Using the design's per-document estimates (agentic parse about $0.0125 per scanned page, Groq about $0.0005, Gemini about $0.003 per document, half the volume with a text layer):

| Item | 1M documents/day |
|---|---|
| Parsing, `agentic`, 50% scans | about $6,000 per day |
| Parsing, `cost_effective`, 50% scans | about $1,900 per day |
| LLM reading (Groq-class to Gemini-class) | about $500 to $3,100 per day |
| **AI services total** | **about $2,400 to $9,400 per day** (about $72k to $280k per month) |
| Infrastructure (compute, storage, queue, observability) | likely 5–15% on top of AI services |
| **Human review at $1.75 per review** | 25% rate: **about $437,000 per day**; 10%: $175,000; 5%: $87,500; 2%: $35,000 |

The review line is 10–50× the AI line at any realistic review rate. At this scale, **one point of review rate is worth more than any model-price negotiation**: one point is about $17,500 a day. That is why the roadmap invests in calibration, a third reader, targeted re-reads, a trained matcher and the feedback loop before it invests in self-hosting. (If 1M a day is the platform total across many products, the review volume is also spread across reviewers who already exist; measure the necessary review rate first, because genuinely ambiguous documents set a floor.)

## 8. Reliability and operations

| Topic | Target / practice |
|---|---|
| SLOs | Decision latency p95 ≤ 2 minutes for 99% of documents (async); availability of intake 99.9%; false match rate ≤ 0.1%; review rate per tenant tracked |
| Failure handling | Retries with jittered backoff; circuit breaker per vendor; DLQ with alert; a failed document becomes `HUMAN_REVIEW`, never lost and never silently "create new" |
| Dependencies | Master API down → `MATCH_UNAVAILABLE` review; parser down → fallback provider (shadow-validated); both LLMs down → layout-only with `SECOND_READER_UNAVAILABLE` |
| DR | Multi-region storage replication; infrastructure as code; RPO ≤ 5 min, RTO ≤ 1 h; restore tested quarterly |
| Change safety | Feature flags per tenant, canary, automatic rollback on SLO or false-match regression |
| On-call | Runbooks per alert (vendor outage, queue backlog, drift alert, key rotation), game days twice a year |
| Capacity | Load test at 3× peak each release; vendor quota dashboards with alerts at 70% |

## 9. Security and compliance checklist

- **Data classification:** TIN/SSN is Restricted. Data-flow diagram reviewed by security; processors listed with purpose and region.
- **Vendors:** signed DPA, no-training and minimal retention for every processor; LlamaParse's access to the unredacted TIN explicitly accepted or replaced by a private-endpoint parser for tenants who object; egress allow-list limited to approved hosts.
- **Keys and TIN:** HSM-backed KEK, per-tenant DEK and HMAC keys, rotation with key-version-aware fingerprints, detokenise as a separate audited, entitled API.
- **Network:** private endpoints for storage, vault and database; no public ingress except the edge.
- **Application:** authentication and tenant claims at the edge, input validation and sandboxed rendering, dependency and container scanning, secret scanning, annual third-party penetration test.
- **Audit and privacy:** immutable audit for at least 7 years, no PII values in logs, retention and deletion (including DSAR) by tenant, access reviews quarterly.
- **Compliance evidence:** SOC 2 Type II controls mapped to the above; privacy impact assessment; incident response plan with breach-notification terms matching the vendors'.

## 10. Definition of done ("production ready")

1. Measured on real documents: false match rate ≤ 0.1%, field accuracy and review rate at or better than targets, per source type.
2. Shadow mode for at least one week with agreement and false-match gates met.
3. All keys in a managed secret store; none in code, docs, logs or chat; rotation tested.
4. Vendor DPAs and security reviews complete; failover tested for each vendor.
5. Load test at 3× expected peak passes; vendor-outage and region-failover game days passed.
6. Dashboards, alerts, runbooks and on-call in place; cost per tenant visible.
7. Audit trail immutable and queryable; retention and deletion verified.
8. Pen test and SOC 2 controls signed off.
9. Reviewer feedback loop running; weekly calibration refit; release gates in CI.

## 11. Open questions to resolve first

1. Real volume per tenant and burst profile, and what "millions per day" means (platform total or this one workflow).
2. Which customers accept a third-party parser seeing the TIN; whether a regional or private-endpoint requirement exists.
3. The supplier master's real API: lookup keys, rate limits, whether it can return a TIN fingerprint.
4. Who owns the review tool and its SLA, and the acceptable review rate per tenant.
5. Actual vendor prices and enterprise rate limits (every figure above is an estimate).

## 12. First concrete changes in this repository

1. Add `api/` (FastAPI, thin) and `worker/` (queue consumer) as new outermost layers calling `OnboardingPipeline`; extend `tests/test_architecture.py` so `services` never imports them.
2. Implement real adapters behind the existing `repositories/` interfaces (supplier master API, vault) and a `DocumentStore` for blobs.
3. Replace direct SDK calls in `infrastructure/` with a gateway client that adds timeouts, retries, circuit breaking and cost metering; add an Azure Document Intelligence provider next to LlamaParse.
4. Add `eval/gate.py` to fail CI when the false match rate or review rate regresses on the frozen sets.
5. Add structured audit events and OpenTelemetry exporters in `utils/observability.py`.
6. Add the reviewer-feedback ingestion and a scheduled calibration refit (`scripts/fit_calibration.py`).
