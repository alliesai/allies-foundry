# CLD-005 Foundry gateway and Cloud activity projection plan

## Feature Overview

- Problem: Foundry FND-007 already owns executions, attempts, leases, ordered runtime events, and terminal/session truth, but its public internal API only exposes runtime-authenticated operations and profile provisioning. Cloud needs a versioned server-to-server command, reconciliation receipt, and safe event publication boundary.
- Target users: Cloud's managed-conversation boundary and operators validating runtime work; runtime workers remain the only callers of lease/event mutation APIs.
- Source docs/specs: Accepted Nabu `projects/allies/engineering/specs/cld-005-foundry-gateway-and-activity-projection.md`; Cloud architecture contract; Foundry `backend/runtime/models.py`, `services/executions.py`, `services/events.py`, `services/attempts.py`, `api/register.py`, and FND-007 tests.
- Success outcome: Authenticated Cloud commands create/replay exactly one execution by workspace/idempotency identity; Cloud can reconcile without side effects; normalized events are published with stable composite identity, bounded safe payloads, and no runtime authority leakage.

## User Stories

1. As Cloud, I want a strict v1 execution command and receipt, so a lost HTTP response can be retried or reconciled without a second Hermes stream.
2. As Cloud, I want ordered, authenticated lifecycle events, so Cloud can project truthful product activity while Foundry remains runtime truth owner.
3. As an operator, I want contract fixtures and sanitized delivery evidence, so schema drift, stale generations, and unsafe payloads fail before deployment.

## Scope

### In Scope

- Strict v1 command, receipt, reconciliation, and event-envelope DTOs with canonical JSON/SHA-256 fingerprints and unknown-version rejection. A future v2 rollout must define its own compatibility window and migration.
- Cloud-service-authenticated execution-intent and reconciliation endpoints.
- Execution correlation to immutable Cloud binding/intent references without trusting caller-supplied runtime Workspace/profile/attempt authority.
- Event publication/outbox tied to durable FND-007 event append and terminal/session evidence, with bounded delivery retry to Cloud. The wire vocabulary uses `execution.accepted`; any internal FND-007 `execution.dispatched` event is translated at the publisher and never emitted as a v1 wire kind.
- Contract fixtures, API/service tests, migrations, and sanitized staging compatibility evidence.

### Out of Scope

- Reworking FND-007 execution, lease, Machine, Hermes, provider, or session internals.
- Cloud conversation/message/activity models, Interface APIs, replay/stop/retry/repair UX, or direct Interface access.
- Returning Hermes profile keys, lease tokens, runtime addresses, provider payloads, or raw exceptions to Cloud/public surfaces.

### Dependencies and Assumptions

- Cloud Phase 0 is a hard cross-repository prerequisite: Cloud must complete its early-stage UUID identity reset for all non-waitlist tables and reconcile the CLD-004 base before Foundry implementation is enabled. Foundry remains UUID-native; its execution, attempt, lease, profile, and event UUIDs stay private and are not substituted for Cloud product IDs. Waitlist rows/data remain preserved in Cloud and their existing public-facing contract is compatibility-tested.
- `Execution` uniqueness `(workspace,idempotency_key)`, payload digest validation, and `ExecutionEvent` attempt-local event/sequence uniqueness remain authoritative.
- `RuntimeProfile` mapping is deterministic from Cloud binding (`uuid5` namespace already used by profile provisioning); Foundry resolves its own profile and Workspace records.
- FND-007 terminal completion has already performed the effective session update; publication of `execution.completed` is gated on that durable evidence.
- HTTPS bearer `ALLIES_CLOUD_SERVICE_TOKEN` is rotatable deployment configuration; runtime bearer and lease credentials stay on runtime-only endpoints.
- Event delivery uses a 60-second outbox lease, at most 8 deliveries, capped exponential backoff `min(300s, 2^(attempt-1))` plus bounded 0–25% jitter. A Cloud `409 sequence_gap` is held/retryable, never terminal.

## Contract and Shape Definitions

### Function and Service Shapes

| Location | Symbol | Signature | Inputs and validation | Return value | Side effects / errors |
| --- | --- | --- | --- | --- | --- |
| `backend/runtime/contracts/execution.py` | `parse_execution_command` | `parse_execution_command(raw: Mapping) -> ExecutionCommand` | Strict v1, `extra=forbid`, workspace scope, Cloud correlation, bounded NFC text, fingerprint; unknown versions rejected | Typed command | Raises `RuntimeValidationError`/`RuntimeIdempotencyConflictError`; never infers private authority |
| `backend/runtime/services/executions.py` | `create_cloud_execution` | `create_cloud_execution(command: ExecutionCommand) -> ExecutionReceipt` | Authenticated service context, immutable `cloud_binding_id`, profile/Workspace lookup, exact idempotency key/fingerprint | `accepted` or exact `duplicate` receipt; conflict/invalid/privacy-safe unavailable | Transaction calls existing `create_execution`; persists correlation and outbox linkage; unique race converges |
| `backend/runtime/services/executions.py` | `reconcile_cloud_execution` | `reconcile_cloud_execution(workspace_id, idempotency_key, fingerprint) -> ReconciliationReceipt` | Read-only, bounded key/fingerprint, service auth | Original receipt, `not_found`, or `conflict` | No create/claim/lease side effect |
| `backend/runtime/services/publication.py` | `publish_execution_event` | `publish_execution_event(event: ExecutionEvent) -> DeliveryResult` | Build allowlisted envelope from event/attempt/execution, current generation and Cloud correlation; translate internal `execution.dispatched` to wire `execution.accepted`; canonical fingerprint | `sent`, `duplicate`, `held_sequence_gap`, or retryable result | HTTP POST to Cloud with 5-second timeout/body bound; stores attempt/fence and safe error code only |
| `backend/runtime/services/publication.py` | `enqueue_event_publication` | `enqueue_event_publication(event_id: UUID) -> None` | Called in same transaction as event append/terminal receipt | None | Upserts immutable outbox row; commit-triggered task only |

### API and Transport Contracts

| Consumer | Method and path / event | Authentication and authorization | Request schema | Success response schema | Error responses / retry semantics |
| --- | --- | --- | --- | --- | --- |
| Cloud gateway | `POST /api/v1/internal/executions` | `_authenticate_cloud_service` bearer; Foundry derives Workspace/profile from `cloud_binding_id` and service records | `ExecutionCommand` | `ExecutionReceipt` (`accepted`/`duplicate`) with Cloud IDs and opaque internal receipt digest; private refs never serialized | `401` invalid credential; `404` privacy-safe binding denial; `409` fingerprint/profile conflict; `422` invalid/oversize; `503` unavailable; exact retry only |
| Cloud gateway | `GET /api/v1/internal/executions/reconcile` | Same bearer; no write capability | `idempotency_key`, `fingerprint` query | `ReconciliationReceipt` (`accepted`, `not_found`, `conflict`) | `401/404/409/422/503`; no side effect |
| Foundry publisher | `POST /api/v1/internal/foundry/events` (Cloud-owned receiver) | Cloud validates Foundry bearer; Foundry sends only authenticated envelopes | `FoundryEventEnvelope` with wire `execution.accepted` (internal `execution.dispatched` translated) | `202 {event_id,status}` | Exact envelope retry on timeout/5xx/408/429 and `409 sequence_gap`; `401/404/422` and irreconcilable fingerprint conflict are terminal; never regenerate event ID/fingerprint |

Representative command:

```json
{
  "schema_version":"v1","kind":"execution.command","producer":"cloud","service_identity":"cloud-service",
  "command_id":"550e8400-e29b-41d4-a716-446655440000","idempotency_key":"650e8400-e29b-41d4-a716-446655440000",
  "scope":{"kind":"workspace","cloud_workspace_id":"750e8400-e29b-41d4-a716-446655440000"},
  "conversation_turn_ordinal":12,
  "cloud":{"ally_id":"850e8400-e29b-41d4-a716-446655440000","conversation_id":"950e8400-e29b-41d4-a716-446655440000","message_id":"a50e8400-e29b-41d4-a716-446655440000","intent_id":"b50e8400-e29b-41d4-a716-446655440000","cloud_binding_id":"c50e8400-e29b-41d4-a716-446655440000"},
  "source_kind":"conversation_message","payload":{"kind":"execution_input","text":"normalized user text"},
  "issued_at":"2026-08-25T12:00:00Z","deadline_at":"2026-08-25T12:00:05Z",
  "fingerprint":"canonical-json-sha256:v1:<64 lowercase hex>"
}
```

Representative event:

```json
{
  "schema_version":"v1","kind":"execution.event","producer":"foundry","service_identity":"foundry-service",
  "event_id":"d50e8400-e29b-41d4-a716-446655440000","event_dedupe_key":"execution:attempt:generation:event",
  "scope":{"kind":"workspace","cloud_workspace_id":"750e8400-e29b-41d4-a716-446655440000"},
  "cloud":{"ally_id":"850e8400-e29b-41d4-a716-446655440000","conversation_id":"950e8400-e29b-41d4-a716-446655440000","intent_id":"b50e8400-e29b-41d4-a716-446655440000","cloud_binding_id":"c50e8400-e29b-41d4-a716-446655440000"},
  "conversation_turn_ordinal":12,
  "foundry":{"execution_id":"e50e8400-e29b-41d4-a716-446655440000","attempt_id":"f50e8400-e29b-41d4-a716-446655440000","generation":3,"attempt_sequence":7},
  "event_type":"message.delta","payload":{"kind":"assistant_delta","text":"safe bounded fragment"},
  "issued_at":"2026-08-25T12:00:01Z","fingerprint":"canonical-json-sha256:v1:<64 lowercase hex>"
}
```

The stable fingerprint projection uses NFC strings, sorted keys, compact JSON, ASCII escaping, `allow_nan=false`, UTF-8 SHA-256, and the `canonical-json-sha256:v1:` label. Timestamps, receipt times, retry counters, and transport headers are excluded. The immutable serialized envelope is the sole fingerprint source; persisted outbox state stores that fingerprint and never derives an optional delivery fingerprint. Reject unknown versions/kinds/fields. A future v2 rollout must publish a new fixture, compatibility window, and migration plan before accepting another version.

### Schema and Data Shapes

| Schema / model | Location | Fields and types | Required / nullable / defaults | Validation and invariants | Compatibility / migration notes |
| --- | --- | --- | --- | --- | --- |
| `Execution` correlation | `backend/runtime/models.py` | UUID `cloud_binding_id`, `cloud_workspace_id`, `cloud_ally_id`, `cloud_conversation_id`, `cloud_message_id`, `cloud_intent_id`; immutable `conversation_turn_ordinal`; `command_fingerprint` | Correlation required for CLD-005 commands; legacy/internal executions nullable | Ordinal is accepted from Cloud only once, included in the canonical fingerprint, and echoed unchanged in every event; all Cloud IDs are UUIDs, bounded, and non-secret; workspace/idempotency uniqueness remains | Additive migration after the Cloud UUID/CLD-004 gate; existing FND-007 executions remain valid with null correlation |
| `ExecutionEvent` publication identity | `backend/runtime/models.py` | Existing event/attempt/sequence/payload digest; immutable Cloud `conversation_turn_ordinal`; generation/attempt identity; envelope fingerprint | No optional delivery fingerprint field; the serialized immutable envelope is the sole fingerprint source | Composite identity remains `(attempt,event_id)` and `(attempt,sequence)`; Cloud ordering key is `(conversation_turn_ordinal,generation,attempt_id,attempt_sequence,event_id)` and stale generations are rejected/held; payload is allowlisted and <=16 KiB | No event rewrite; avoid a second persisted fingerprint authority |
| `CloudEventOutbox` | `backend/runtime/models.py` or focused publication app | event, immutable canonical UTF-8 envelope bytes/blob, byte length, SHA-256/fingerprint, delivery state, attempt count, next attempt, lease, safe error code, timestamps | New rows only for Cloud-correlated events; state `pending` default | Persist exact bounded bytes at append time; transmit the stored bytes directly (no DTO reserialization, newline normalization, or timestamp rebuild); every retry/restart must match captured request bytes, length, and hash; unique event/fingerprint; bounded attempts/backoff; no token/raw provider body; terminal delivery does not alter runtime truth | New migration; delete/retention follows existing event policy |
| `ExecutionCommand`, `ExecutionReceipt`, `FoundryEventEnvelope` | `backend/runtime/contracts/*.py` | Strict typed DTOs matching JSON above | `extra=forbid`; private Foundry refs are internal-only fields | Scope/correlation binding, size, digest, event registry, attempt sequence, generation checks | Strict v1 fixture consumed by Cloud and Foundry; future v2 compatibility is a separate rollout artifact |

### Frontend Interaction Shapes (if applicable)

Not applicable. Foundry never serves Interface state; Cloud owns customer-facing DTOs.

## Phases

### Phase 0 - Gate on Cloud UUID reset and CLD-004 reconciliation

- Goal: Prevent Foundry from publishing a contract against stale Cloud identity shapes. Cloud completes its non-waitlist UUID PK/FK reset and reconciles CLD-004 (`db3f590` versus the current Cloud base) before this repository enables CLD-005 traffic.
- Work items: Review Cloud's UUID-native DTO/route/fixture contract; update Foundry shared fixtures and type annotations so every `cloud_*` correlation value is a UUID; preserve Foundry-private execution/attempt/lease/profile/event UUIDs as internal-only fields. Verify Cloud waitlist preservation evidence and current waitlist contract compatibility, but make no Foundry migration or model change for waitlist data. If the CLD-004 contract is absent or still uses prefixed IDs, stop at the gate and do not add a compatibility parser.
- Validation/rollback: Require Cloud's Phase 0 checks (fresh schema, UUID FK constraints, waitlist before/after tests, `makemigrations --check`) plus Foundry fixture parity and strict-v1 parser tests. A failed gate rolls back only the pending Foundry fixture/route deployment; runtime execution truth remains unchanged and no Cloud event delivery is enabled.

### Phase 1 - Freeze the joint v1 contract

- Goal: Publish one executable schema and fingerprint implementation for Cloud and Foundry.
- Work items: Add contract modules, canonicalization helpers, allowlisted event registry (`execution.accepted`, `execution.awaiting_action`, `message.delta`, `activity.started`, `activity.completed`, `execution.completed`, `execution.stopped`, `execution.failed`), explicit internal `execution.dispatched`→wire `execution.accepted` translation, exact shared v1 JSON fixtures, and parity tests in both repositories. Unknown versions are rejected; v2 compatibility is deferred to a separate rollout.
- Impacted files/systems: `backend/runtime/contracts/**`, `docs/contracts/foundry-execution-gateway-v1.json`, runtime contract tests, Cloud fixture test.
- Exit criteria: Both repos produce identical fingerprints; unknown fields/kinds/versions, unsafe payloads, NaN/Infinity, and size violations fail closed.

### Phase 2 - Add Cloud command and reconciliation endpoints

- Goal: Accept exactly one Cloud execution intent and expose read-only lost-response recovery.
- Work items: Extend schemas/register routes; authenticate Cloud service token separately from runtime token; resolve binding→Workspace/profile; call existing `create_execution` in a transaction; persist correlation and receipt digest; map duplicate/conflict/invalid/privacy-safe unavailable; add API/service/concurrency tests and migration.
- Impacted files/systems: `backend/runtime/api/schemas.py`, `backend/runtime/api/register.py`, `backend/runtime/services/executions.py`, `backend/runtime/models.py`, migrations, tests.
- Exit criteria: Same workspace/key/fingerprint returns one execution; different profile/payload/binding conflicts without mutation; reconciliation never creates work; response serializers contain no lease/session/provider/runtime authority.

### Phase 3 - Publish normalized events to Cloud

- Goal: Make durable FND-007 events deliverable to Cloud without changing runtime truth.
- Work items: Add outbox row in the same transaction as `append_event` and terminal/session completion; build and persist bounded canonical UTF-8 envelope bytes (including immutable timestamps), byte length, and SHA-256/fingerprint once; translate internal dispatch evidence to wire `execution.accepted`; echo immutable Cloud `conversation_turn_ordinal` unchanged in every event; post stored bytes directly with 5-second timeout/body bound and rotatable token, without DTO reserialization, newline normalization, or new timestamps; use 60-second leases, at most 8 deliveries, `min(300s,2^(attempt-1)) + 0–25% jitter`; classify 2xx duplicate as success, `409 sequence_gap` as held/retryable, `401/404/422` or irreconcilable fingerprint conflict as terminal, and timeout/408/429/5xx/network as retryable; keep raw provider payload out of logs while retaining only the bounded contract envelope required for exact retry.
- Impacted files/systems: `backend/runtime/services/events.py`, `attempts.py`, `publication.py`, Celery task registration/settings, models/migration, API and contract tests.
- Exit criteria: Event publication preserves event ID/fingerprint/turn ordinal on retry; out-of-order N+1 delivery remains held until N is accepted or an operator/CLD-006 repair resolves it; fresh attempts with sequence 1 remain distinct by generation/attempt identity and stale generations cannot advance projection; concurrent outbox workers cannot double-send a terminal event; event order and composite identity match FND-007; terminal publication is gated by effective session update; duplicate Cloud response does not create another outbox event; exhausted delivery writes `delivery_exhausted` with operator-reconcilable identity. No automated replay/reconciliation protocol is added.

### Phase 4 - Staging compatibility and rollout

- Goal: Prove Cloud/Foundry can deploy independently and recover safely.
- Work items: Deploy Foundry endpoint and contract fixture first; configure staging Cloud URL/token only in secret store; run sanitized command→claim→event→terminal flow; verify strict v1 unknown-version rejection; publish delivery metrics without IDs/payloads; keep one global command/publication flag disabled by default and document global rollback. Future v2 compatibility is a separate rollout.
- Impacted files/systems: CI/staging docs and secret inventory; no provider/IaC changes in this plan.
- Exit criteria: Cross-repo fixture tests and staging evidence pass; disabling Cloud publication leaves Foundry truth intact and outbox retryable; rotating token does not expose or persist old secret.

## Acceptance Criteria

1. Cloud's Phase 0 evidence proves non-waitlist Cloud identifiers are UUID-native and waitlist rows/contract are preserved before Foundry traffic is enabled.
2. Valid Cloud command creates one workspace-scoped execution intent; exact duplicate replays receipt; conflicting key/fingerprint/profile/binding is rejected without mutation.
3. Reconciliation returns existing receipt, not-found, or conflict and never creates an execution or lease.
4. Foundry rejects unknown version/kind/field, malformed fingerprint, oversized/unsafe text, invalid UUID scope, cross-workspace binding, stale generation, and caller-supplied private authority.
5. Events carry stable composite identity `(execution, attempt, generation, event_id)` and attempt-local sequence plus immutable Cloud `conversation_turn_ordinal`; publication retries exact envelopes and never regenerates work.
6. `execution.completed` is published only after FND-007 durable terminal event and effective session update; failed/stopped/awaiting-action events remain truthful and bounded.
7. No Cloud/public response, log, metric, trace, fixture, or outbox record contains lease tokens, profile/session keys, runtime addresses, provider bodies, raw tool payloads, credentials, or unsafe exceptions.
8. Contract and API tests run without a live Fly Machine; sanitized staging evidence proves deployed compatibility.
9. Internal `execution.dispatched` is translated to one wire `execution.accepted`; both repositories consume the same fixture and reject contradictory event kinds.
10. Out-of-order events return held/retryable `sequence_gap` rather than terminal loss; generation/attempt identity disambiguates fresh attempt-local sequence values, Cloud command dispatch uses a separate maximum of 5 attempts, Foundry event delivery uses a maximum of 8 deliveries, and exhausted state/operator recovery tests are explicit without automated replay.

## Backend Considerations (if applicable)

### Query Optimization Plan

- Hotspots/endpoints: command create/replay, reconciliation lookup, event append/outbox enqueue, outbox claim.
- Query-shape choices: `select_related("workspace","profile")`; lock execution by workspace/idempotency; use existing unique indexes and add correlation index only where lookup evidence requires; claim outbox with `select_for_update(skip_locked=True)` and bounded batch 20.
- Expected query-count change: constant-query command and event append; no per-event relation loop; publication task batches due rows.
- Measurement/monitoring plan: query-count tests, PostgreSQL concurrency test, sanitized latency/error counters, inspect migration indexes before staging.

### N+1 Prevention

- Relation access map: command resolves one Workspace/profile; envelope builder loads one execution→attempt→event; outbox worker uses `select_related` and does not query per payload field.
- Prefetch/select plan: `select_related` on execution/profile/workspace/attempt; no unbounded prefetch.
- N+1 regression guardrails: query-count tests for command, reconciliation, and publication; reviewer check that event batching does not call ORM inside a network loop.

### Detailed Unit Test Cases

- Happy path: accepted command, duplicate receipt, reconciliation accepted/not-found, each allowlisted event, terminal/session gate, outbox sent.
- Validation and bad input: unknown versions/kinds/fields, bad digest, oversized/unsafe payload, invalid timestamp/deadline, invalid sequence/generation, caller private ID mismatch.
- Auth/RBAC boundaries: missing/invalid Cloud bearer, runtime token cannot access Cloud command, foreign binding/workspace privacy-safe denial, stale machine generation.
- Idempotency/retry behavior: concurrent same key, exact retry after 503/timeout, conflicting retry, duplicate outbox delivery, Cloud duplicate response, 60-second lease expiry, eight-delivery exhaustion, capped backoff/jitter bounds, operator receipt, retention, and exact-identity recovery; process restart after lease claim must resend byte-identical persisted envelope bytes (same captured request bytes, byte length, and SHA-256), and DTO reserialization/timestamp rebuild or fingerprint mismatch must fail the test.
- Failure-path behavior: malformed Cloud response, Cloud 401/404/409/422/5xx, timeout, `409 sequence_gap`, N+1-before-N delivery, fresh attempts sharing sequence 1, stale-generation event, concurrent outbox workers, partial DB/outbox failure, terminal event publication failure, terminal/session gate, and token rotation.

## Test Plan

- Unit tests: `runtime/tests/test_cld005_contract.py`, UUID fixture/parity tests, execution/API tests, publication/outbox tests, existing FND-007 event/terminal suites.
- Integration/API tests: Django client with fake Cloud receiver and service bearer; shared `docs/contracts/foundry-execution-gateway-v1.json` parity; no live Fly Machine.
- Regression checks: `runtime/tests/test_fnd007_execution.py`, `runtime/tests/test_services.py`, `runtime/tests/test_api.py`, migrations, existing profile provisioning and runtime auth tests.
- Manual verification checklist: inspect OpenAPI route registration; send one command and exact duplicate; force timeout and reconcile; append safe event and duplicate; append conflicting sequence; send N+1 before N and verify held/retryable response; verify terminal gate and sanitized logs; rotate Cloud token and retry; verify global flag rollback.
- Commands: `make sync`; `make check`; `make validate`; `make test APP=runtime/tests/test_cld005_contract.py runtime/tests/test_uuid_fixture_parity.py runtime/tests/test_fnd007_execution.py runtime/tests/test_services.py runtime/tests/test_api.py`; `make runtime-test`; `make lint`; `make format` (inspect diff). Cloud-side gate commands remain required: `make check`, fresh `make migrate`, `makemigrations --check`, and waitlist contract/preservation tests.
- CI basis: `.github/workflows/ci.yml` runs locked backend/runtime validation, migrations, production configuration checks, and Codecov; `scripts/validate.py` runs backend/runtime tests and coverage XML; staging HTTPS workflow remains a health redirect check only.

## Risks and Mitigations

- Risk: Cloud/Foundry schema drift. Mitigation: checked-in strict v1 fixture, unknown-version rejection, Foundry-first deployment, CI parity; future v2 requires a separate compatibility artifact. Rollback/fallback: disable the one global command/publication flag and retain strict v1 parser.
- Risk: Cloud UUID reset or CLD-004 base drift invalidates correlation. Mitigation: Phase 0 gate, UUID-only shared fixtures, explicit commit ancestry/contract evidence, and waitlist preservation checks. Rollback/fallback: keep Foundry routes disabled and revert only fixture/route deployment; do not add a compatibility parser or alter Foundry-private UUIDs.
- Risk: Duplicate runtime work after ambiguous response. Mitigation: existing unique `(workspace,idempotency_key)`, persisted digest, exact retry/reconciliation, no side-effect lookup. Rollback/fallback: preserve execution/outbox and reconcile; never generate a new key.
- Risk: Event replay/order or stale-generation corruption. Mitigation: existing attempt event/sequence constraints plus composite envelope identity, immutable turn ordinal, generation/attempt identity, and stale-generation rejection. Translate internal `execution.dispatched` to wire `execution.accepted`; treat Cloud `409 sequence_gap` as held/retryable and leave gap recovery to operator/CLD-006 repair. Concurrent workers use leases and unique event identity. Rollback/fallback: stop delivery, retain Foundry event truth, retry sequence gaps up to the eight-delivery event budget, then write `delivery_exhausted` with operator reconciliation identity; no automated replay.
- Risk: Process restart or timestamp reconstruction changes a retried event body. Mitigation: persist the bounded canonical serialized envelope and fingerprint in `CloudEventOutbox` at append time; retries read those bytes rather than rebuilding the envelope. Restart/timeout and body/fingerprint mismatch tests are release gates. Rollback/fallback: quarantine the outbox row and retain runtime truth if persisted bytes fail validation; never emit a newly timestamped retry.
- Risk: Private runtime data leakage. Mitigation: explicit allowlist/strict serializers, bounded payload, safe error codes, redaction tests, no raw outbox body/logging. Rollback/fallback: reject unsafe event and quarantine delivery; do not silently redact unknown fields.
- Risk: Credential or cross-repo rollout failure. Mitigation: rotatable secret settings, timeout/body bounds, fake receiver, staging evidence, deployment order Foundry then Cloud. Rollback/fallback: revoke/rotate token, disable publication, keep runtime execution truth unchanged.
- Retry policy: Cloud command dispatch is a separate 60-second lease with maximum 5 attempts; Foundry publication is a 60-second lease with maximum 8 deliveries. Both use backoff `min(300s, 2^(attempt-1))` with 0–25% jitter. Retryable event outcomes: timeout, network error, 408, 409 `sequence_gap`, 429, and 5xx. Terminal: invalid credential 401, privacy-safe binding denial 404, schema/size 422, and irreconcilable fingerprint conflict. Event-gap exhaustion occurs only after the eighth delivery and stores event identity, turn ordinal, immutable envelope fingerprint, sequence, safe error code, attempt count, and timestamps; operator/CLD-006 repair reuses the same envelope identity and recovery/retention tests prove no duplicate runtime work.
- Open decision before implementation: confirm final route aliases and whether outbox belongs in `runtime` or a new integration app; whichever is chosen must preserve the contract and transaction boundary above.
