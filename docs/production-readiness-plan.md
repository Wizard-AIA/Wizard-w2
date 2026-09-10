# Production-Readiness Plan

## Purpose and boundary

Wizard w2 is a local-first analytical agent, not a multi-tenant SaaS or a model
training platform. "Production grade" in this plan means that a supported
single-user or small-team deployment can make an analytical claim, execute
code, handle attached reference documents, survive ordinary failures, and be
operated with measurable evidence. It does **not** add instruction tuning, DPO,
tenant billing, cross-tenant data partitioning, distributed inference load
balancing, continuous batching, or webhooks.

The existing one-request-path, data-mode, CodeGuard, sandbox, permission, and
analytical-control-plane invariants remain authoritative. New behaviour must
flow through `AnalysisOrchestrator.run` and `CodeExecutor.execute`; no alternate
LLM or interpreter path may be introduced.

## Release rules

Every phase must meet these rules before it is enabled by default:

1. A behaviour test drives the public REST or WebSocket path where one exists;
   unit tests alone do not establish a production claim.
2. The test oracle is derived from executed output, captured evidence, or a
   checked fixture--never an LLM's self-reported success field.
3. Local-only, host-sandbox, Docker-sandbox, and cloud/hybrid modes are tested
   according to the feature's declared support. Unsupported environments fail
   closed or report a precise degraded capability.
4. A run's input identities, settings, model/provider, code, evidence,
   warnings, costs, and outcome are retained together. A later dataset or
   setting change must not rewrite historical evidence.
5. Add metrics, alert thresholds, and rollback evidence before declaring an
   operational feature released.

## Phase 0: Establish a trustworthy baseline

**Goal:** make future claims comparable and reproducible.

- Add a versioned `benchmarks/baselines/` manifest containing scenario ID,
  fixture hashes, expected evidence/grounding/uncertainty verdicts, permitted
  cost and latency envelopes, git revision, and hardware profile.
- Extend the existing content-based harness to write immutable JSON results;
  make comparison fail for changed fixture hashes, missing scenarios, or an
  unapproved baseline update.
- Replace `scripts/check_benchmark_drift.py` with a deterministic baseline
  comparator. Remove all random pass-rate and quality values.
- Keep live-provider evaluation separate from CI. It must capture provider,
  model, parameters, host profile, raw event transcript, and artifact hashes;
  it must never overwrite the offline baseline.

**Acceptance:** CI runs the offline corpus and comparison; a deliberately
weakened grounding, citation, or confidence fixture fails; live reports are
labeled as sampled observations rather than pass/fail release evidence.

## Phase 1: Source-aware retrieval and citations

**Goal:** every document-derived claim is inspectable at the source location.

- Replace `(document, text)` retrieval values with a typed `SourceSpan`:
  document ID, immutable upload hash, format, page range where available,
  character offsets, chunk ID, retrieval scores, and retrieval method.
- Preserve PDF page boundaries during extraction. Chunking may span paragraphs,
  but it must retain all contributing page and offset ranges. DOCX/HTML/text
  spans should retain their best available structural locator.
- Add a structured `Citation` to answer and evidence models. The answer prompt
  must require citations for claims based on attached documents; the renderer
  must display a source name and page/location link, not an unverifiable label.
- Add deterministic citation validation: cited span must belong to the session,
  upload hash must match the captured run, and the cited text must support the
  claim or be flagged. Never permit model-generated file paths or page numbers
  to become authoritative.
- Wire two first-stage retrieval lists (lexical and dense) into a single
  retrieval service with Reciprocal Rank Fusion. Apply metadata filters before
  ranking (session, document, source format, user-selected scope, and optional
  page range). Integrate the existing reranker only after first-stage retrieval;
  retain rank method/score in `SourceSpan`.
- Make dense, lexical, and reranking degradation observable. A missing encoder
  or reranker must select a named fallback, not silently claim hybrid search.

**Acceptance:** integration tests upload a multipage PDF and prove that a
document answer cites the correct page/span; cross-session documents cannot be
retrieved; lexical-only, dense+lexical, and reranker-unavailable paths have
deterministic results and emitted capability metadata.

## Phase 2: Untrusted-context and data-protection boundary

**Goal:** prevent uploaded or retrieved text from changing system authority or
leaking protected data.

- Introduce an `UntrustedContext` contract for history, skills, documents,
  connector metadata, and retrieved memories. It must carry source/type and be
  inserted into prompts under a fixed instruction boundary saying it is data,
  not instructions.
- Add deterministic prompt-injection detection for instruction-like directives,
  encoded payloads, tool/secret exfiltration requests, and role-boundary
  attempts. Detection produces a typed warning/quarantine decision; it must not
  rely solely on a model to judge hostile text.
- Add an allowlisted context adapter for each source. Skills remain executable-
  code-free, while documents/history are never promoted to system instructions.
- Add configurable PII/secret classifiers at ingest and before cloud-bound
  prompts. Store classifications and user policy separately from raw values.
  Schema-only remains the safe default for cloud use.
- Add output egress policy: redact detected secrets and configured PII classes
  from logs, telemetry, exports, and cloud-bound prompts; warn before returning
  an answer that contains protected raw data. Preserve legitimate local
  analytical results through explicit user policy rather than blanket regex
  deletion.
- Threat-test direct, nested, quoted, encoded, and retrieval-mediated attacks.

**Acceptance:** adversarial document/history/skill fixtures cannot alter tool
permissions, trigger network access, expose credentials, or suppress a policy
warning; no secret/PII sentinel occurs in logs, metrics, exports, or a redacted
cloud prompt; local-only analysis still handles approved local values correctly.

## Phase 3: Analytical trust, decision quality, and cost correctness

**Goal:** make answers and agent choices auditable at turn granularity.

- Change usage accounting from session-total readouts to immutable per-turn
  deltas, while retaining session and subagent rollups. Distinguish provider
  reported counts, estimates, and unknown prices.
- Make council/competing-route results explicit policy inputs. Define which
  findings merely warn, which force caveats, and which block an unqualified
  answer or require a human decision. A timeout must be represented as
  `not_reviewed`, never as agreement.
- Extend grounding beyond numbers to document citations and generated tables;
  retain its current non-destructive reporting behaviour.
- Expand golden scenarios for citations, injection resistance, PII policy,
  route disagreement, timeout/degradation, and cost roll-up. Add mutation
  tests around each safety decision.
- Require an immutable before/after evaluation record for changes to prompts,
  routing, retrieval, guardrails, or models. A regression needs an explicit
  approval plus a rollback plan.

**Acceptance:** every completed turn exposes a turn-specific cost record and
evidence graph; critical reviewer disagreement prevents an unqualified answer;
all safety-policy mutants are killed or triaged with a documented equivalent
mutation; CI rejects baseline regressions.

## Phase 4: Durable operation and recovery

**Goal:** recover safely from process, queue, session, and storage failures.

- Decide one durable job backend for supported production profiles. The current
  in-process queue may remain the default developer mode, but production jobs
  need durable payload/state, idempotency keys, lease/claim semantics, and
  replay-safe handlers.
- Persist idempotency state with request fingerprint, owner, expiry, terminal
  response, and conflict semantics. Do not reuse cached output for a different
  request body sharing a caller key.
- Keep exponential backoff with jitter, but classify retryable failures,
  respect provider `Retry-After`, and record every attempt. DLQ entries need
  sanitized payload provenance, a replay decision, and operator audit record.
- Wire scheduled SQLite backups into application lifecycle, encrypt or protect
  backup locations, define retention, and implement a tested restore command.
  Restore must verify integrity and preserve original run hashes.
- Define session loss behaviour: reconnect can resume event delivery only when
  the durable turn is resumable; otherwise it must return its immutable final
  result or a clear interrupted state. Never silently rerun generated code.

**Acceptance:** kill/restart tests prove idempotent replay, retry limits, DLQ
inspection/replay authorization, backup integrity, and restore into a clean
instance; duplicate HTTP submission and reconnect do not execute a side effect
twice.

## Phase 5: Observability and operational SLOs

**Goal:** make production health measurable rather than inferred.

- Add a request/turn middleware that records request duration, status, errors,
  turn duration, queue wait, execution duration, retry count, grounding ratio,
  citation validation, and degradation reason. Bound labels to avoid session,
  document, query, and tenant cardinality leaks.
- Complete OpenTelemetry setup with pinned optional dependencies, trace context
  propagation through REST/WebSocket, LLM, retrieval, queue, and sandbox
  boundaries, and a documented collector configuration.
- Publish Prometheus-compatible counters/histograms from real call sites.
  Compute p50/p95/p99 from valid histograms; do not expose an empty collector
  as operational evidence.
- Define SLOs for availability, 5xx/error rate, TTFT, turn latency, queue
  latency, sandbox denial/error rate, grounding/citation failures, and
  redaction/injection incidents. Add dashboards and alerts with runbooks.
- Redact sensitive fields before logs/traces/metrics leave the process.

**Acceptance:** an E2E turn produces one correlated trace and metrics for every
stage; synthetic 4xx/5xx, timeout, queue failure, and injection events alter
the expected counters; a dashboard query reproduces a known p95 and error rate.

## Phase 6: Execution safety and environment parity

**Goal:** support only execution environments whose confinement is proven.

- Make production startup refuse `inprocess` execution. Treat host sandbox
  capability gaps as degraded/unsupported according to explicit policy;
  Docker/OS sandbox claims must be based on child-reported enforcement.
- Run containment probes in a privileged, platform-specific CI job for each
  supported backend/OS. Cover filesystem escape, network egress, process
  escape, resource exhaustion, symlink/path tricks, reflection, and package
  installation policy.
- Build a release-image E2E matrix that uses the same locked dependencies,
  config, execution backend, and generated OpenAPI/client contract as the
  shipped images. Keep unit tests fast and isolated, but do not call them
  parity evidence.
- Define a compatibility matrix for supported OS, architecture, provider,
  execution backend, and optional capability. Surface it in `/health/ready`.

**Acceptance:** release-image E2E and containment jobs pass on every supported
platform; unsupported confinement causes a precise readiness failure; no
production image can start an unsandboxed generated-code runtime by accident.

## Phase 7: Real release controls

**Goal:** replace simulated delivery safety with deployer-backed controls.

- Delete or quarantine random canary and benchmark scripts. A release gate must
  call the actual deployment target, use the release image digest, and read
  real metrics and quality checks.
- Define supported deployment modes. For single-host local installs, use an
  atomic versioned release plus health check and documented rollback. For a
  managed multi-instance target, add progressive traffic shifting, canary
  scenario checks, alert thresholds, and automated rollback to the prior image
  digest.
- Introduce scoped, audited feature/capability flags for risky features such as
  hybrid retrieval, reranking, cloud use, output filtering, and durable jobs.
  Flags need defaults, owner, expiry, telemetry, and a tested kill switch.
- Require signed/versioned release artifacts, migration compatibility checks,
  backup verification before destructive migrations, and post-deploy smoke
  tests through the public API.
- Bring CLI release updates into this phase: stamp each binary with its build
  version; publish checksums; implement an explicit `wizard update --check`;
  and implement a platform-aware `wizard update --self` that verifies, stages,
  prepares, switches with platform-appropriate atomicity, and can roll back a
  release installation. The existing checkout updater remains a separate
  source-update path. Add signing when a release-signing key and verification
  policy are provisioned.

**Acceptance:** a test deployment proves an image-digest rollback after an SLO
or quality breach; disabled flags remove the live code path; release records
contain artifact, migration, environment, health, benchmark, and rollback data;
CLI tests reject an invalid manifest/checksum and prove that a failed self-update
leaves the active binary unchanged.

## Phase 8: Scale only within the supported local-first profile

**Goal:** improve predictable performance without inventing distributed-serving
requirements.

- Establish corpus-size thresholds for in-memory retrieval. Below the threshold
  retain simple session-scoped ranking; above it add a persistent local index
  with rebuild/version/backup behaviour and metadata-filter correctness tests.
- Measure and surface cache hit rate, embedding fallback, model residency,
  context length, eviction, and retrieval latency. Cache keys must include
  source/version/policy dimensions so redacted and unredacted data never share
  entries.
- Validate model quantization and residency choices against correctness,
  grounding, latency, and memory baselines. Keep quantization as a selected
  provider artifact, not an in-app weight transformation system.
- Do not add continuous batching or load balancing unless the supported product
  boundary changes to a multi-user distributed inference service; that change
  requires a separate architecture and tenancy plan.

**Acceptance:** threshold crossings preserve retrieval relevance and citation
accuracy; cache isolation tests pass; supported hardware profiles meet their
published latency/memory envelopes without hiding quality regressions.

## Execution order and release exit

1. Phase 0 first: no implementation claim is accepted without a reliable
   baseline.
2. Phases 1 and 2 next: source provenance and untrusted-data boundaries are
   prerequisites for expanding retrieval or cloud use.
3. Phase 3 follows: make quality, cost, and reviewer outcomes policy-visible.
4. Phases 4 through 6 establish durable operation, observability, and proven
   runtime safety; they may progress in parallel only after their shared event
   and run-record schema is agreed.
5. Phases 7 and 8 are release/scaling work, performed only after the preceding
   production gates pass.

The system is ready for a declared production profile only when every enabled
phase has passed its acceptance tests in release images, the offline baseline is
green, a live smoke evaluation is retained as evidence, backup/rollback has
been rehearsed, and the deployment reports no unsupported capability as safe.
