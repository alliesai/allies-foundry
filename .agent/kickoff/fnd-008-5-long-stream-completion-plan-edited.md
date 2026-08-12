# FND-008.5: let long Hermes streams finish

## Feature overview

- Problem: the Hermes SSE parser rejects the 513th raw event because
  `MAX_EVENTS` is 512. In the captured Sandy run, the 513th frame was the
  valid terminal completion after 510 assistant deltas, so Allies returned
  `malformed_response` after streaming most of the answer.
- Target users: people who ask an Ally to complete a long response or a
  tool-heavy responsibility, and the operators who need the streamed answer
  to finish truthfully.
- Source docs/specs: `runtime/allies_runtime/hermes.py`,
  `runtime/tests/test_hermes.py`, `runtime/tests/test_edges.py`, the captured
  Sandy/Greg evidence under `.agent/`, and the Nabu notes for Conversation and
  Streaming and the Foundry Continuity Layer.
- Success outcome: a long live Hermes stream reaches its terminal event and
  becomes a completed Allies execution while the existing byte, text, timeout,
  identity, tool-event, and malformed-stream safeguards remain active.

## User stories

1. As a person talking to an Ally, I want a long answer to finish after many
   streamed chunks, so I can read the complete response instead of receiving a
   false malformed-response failure.
2. As a person whose Ally uses tools, I want tool activity and the surrounding
   answer chunks to stay ordered, so the visible timeline remains truthful.
3. As an operator, I want long streams to remain bounded by bytes, event-frame
   size, parsed text size, and transport deadlines, so removing the 512-frame
   accident does not create an unbounded parser.
4. As an operator, I want a live staging smoke test to show the terminal event
   and retained streamed text, so the fix is not accepted from unit tests alone.

## Scope

### In scope

- Keep the existing `MAX_EVENTS` bound for marker traversal, Hermes inline
  transcript rows, and SessionDB history rows. It remains independent from the
  incremental stream path.
- Remove the 512 raw-event rejection from the incremental SSE path. Keep the
  existing total stream-byte limit, per-frame byte limit, bounded line reads,
  per-delta/final-text validation, and configured transport deadlines as the
  live stream bounds.
- Enforce the configured `stream_timeout` as an overall wall-clock deadline
  for the incremental response after it opens. The existing socket read
  timeout remains the idle-read bound. This prevents a peer that emits
  keepalives forever from holding a worker slot forever, while allowing the
  bounded timeout to be raised for an approved long task.
- Keep the legacy whole-result `_sse_events` helper bounded with an explicit
  `MAX_BUFFERED_EVENTS = 65_536` guard. This guard protects the compatibility
  API's retained list and is not applied to the incremental long-running
  worker path.
- Preserve the raw event ordinal only for deterministic tool activity IDs; it
  must not reject a valid stream.
- Add regression tests for a completion after more than 512 frames, long
  assistant output, interleaved tool lifecycle events, buffered-parser bounds,
  and existing malformed/truncated behavior.
- Publish an immutable runtime image and run a real staging long-stream smoke
  using the existing secure proof operations, retaining sanitized evidence and
  authoritatively cleaning resources.

### Out of scope

- Changing the Hermes image, profile/session IDs, session-history lookup, or
  Machine replacement protocol.
- Coalescing or dropping user-visible assistant deltas. The current Allies
  event contract keeps ordered `message.delta` events, and the byte bound is
  the demonstrated safety boundary for this fix.
- Adding Cloud persistence, a new event vocabulary, a UI replay protocol, or a
  background job/checkpoint system.
- Raising runtime timeouts or changing lease/fencing policy unless the live
  smoke exposes a separate, reproducible timeout defect.
- Changing the default stream timeout. The fix enforces the existing validated
  setting; operators may use the existing bounded setting for a longer task.

### Dependencies and assumptions

- The merged FND-008 backend contract is deployed to the staging origin used
  by the proof.
- The pinned Hermes image remains the inspected image digest recorded in the
  continuity notes.
- The actual worker path uses `stream_profile_incremental`; the buffered
  `stream_profile` path remains for the FND-004 proof coordinator and other
  compatibility callers.
- The live smoke uses approved temporary credentials and leaves no proof
  resources or secrets behind.

## Contract and shape definitions

### Function and service shapes

| Location | Symbol | Signature | Inputs and validation | Return value | Side effects / errors |
| --- | --- | --- | --- | --- | --- |
| `runtime/allies_runtime/hermes.py` | `_IncrementalHTTPStream.__anext__` | `async def __anext__(self) -> HermesEvent` | Reads bounded UTF-8 SSE lines and validates event identity, state, payload limits, and the configured overall stream deadline | One normalized `HermesEvent` or `StopAsyncIteration` | Maintains raw byte and ordinal counters; raises sanitized `HermesMalformedResponse`, `HermesTimeout`, or `HermesDisconnected` |
| `runtime/allies_runtime/hermes.py` | `_IncrementalHTTPStream._finish_event` | `def _finish_event(self) -> HermesEvent \| None` | One complete JSON SSE event within the per-event and total stream byte limits | Normalized event or no-op for lifecycle noise | Does not reject solely because the raw stream has passed 512 events |
| `runtime/allies_runtime/hermes.py` | `_sse_events` | `def _sse_events(lines: Iterable[bytes]) -> list[tuple[str, Mapping[str, Any]]]` | Compatibility parser retains parsed raw events and uses the separate buffered-event guard | Bounded parsed event list | Raises a buffered-stream limit error before retained memory grows beyond the compatibility bound |

### API and transport contracts

No public HTTP endpoint or Allies event name changes. Hermes input remains:

```text
POST /p/{profile}/api/sessions/{session_id}/chat/stream
Accept: application/json, text/event-stream
X-Hermes-Session-Key: <opaque stable key>
```

The runtime still emits the existing normalized vocabulary:

| Hermes input | Allies output |
| --- | --- |
| `assistant.delta` | `message.delta` with bounded `{ "text": "..." }` |
| `tool.started` | `activity.started` with a safe activity ID and `kind` |
| `tool.completed` | `activity.completed` with a safe activity ID and status |
| valid `run.completed` followed by `done` | `execution.completed` |

The terminal event remains strict. A missing, malformed, out-of-order, or
identity-changing terminal stream still fails with the existing sanitized
receipt; a stream is not successful merely because it emitted text.

### Schema and data shapes

No database migration or durable schema change is planned. Foundry continues
to receive one ordered safe event for each normalized assistant/activity event,
with sequence numbers allocated by the worker. The existing `MAX_EVENTS` bound
continues to protect history, transcript, and marker collections, while the
incremental stream and the retained whole-result parser use independent
guards.

### Frontend interaction shapes

Not applicable. Cloud and Interface behavior remains unchanged; they continue
to consume replayable Foundry events and render product language above the
runtime event vocabulary.

## Phases

### Phase 1: reproduce and implement the parser-bound fix

- Goal: make the terminal event reachable after 512 raw frames without
  weakening the parser's resource or lifetime limits.
- Work items:
  - Add a deterministic SSE fixture with 510 assistant deltas followed by
    `assistant.completed`, `run.completed`, and `done`.
  - Keep the shared collection limit, add the explicit buffered-list limit, and
    remove only the incremental raw event-count rejection.
  - Add the explicit `MAX_BUFFERED_EVENTS` compatibility-list guard to
    `_sse_events`.
  - Pass the validated overall stream deadline into the incremental adapter and
    fail a keepalive-only stream with the existing `HermesTimeout` code.
  - Preserve the raw ordinal for activity ID derivation and assert tool-event
    ordering.
- Impacted files/systems: `runtime/allies_runtime/hermes.py`,
  `runtime/tests/test_hermes.py`, `runtime/tests/test_edges.py`.
- Exit criteria: focused runtime tests pass, including the new >512-frame
  terminal regression and malformed/byte-bound regressions.

### Phase 2: validate, publish, and prove live behavior

- Goal: show the fix in the real staging runtime and prepare a reviewable PR.
- Work items:
  - Run runtime and repository validation in their own uv environments.
  - Build and publish the runtime image by immutable digest using the existing
    operator path.
  - Run a real staging long-stream smoke with an actual consumer-style prompt,
    record ordered stream evidence, verify a terminal completion after more than
    512 raw frames, and preserve the full streamed text in sanitized evidence.
    Use Sandy's fitness-coach profile and ask for a detailed 12-week program
    with weekly schedules, exercise explanations, substitutions, and recovery
    guidance. If the first response is shorter than 513 frames, do not call it
    a pass; rerun with a larger requested program.
  - Verify cleanup of Fly Machines, Volumes, apps, staged secrets, temporary
    credentials, and staging proof records.
  - Update the PR body and the relevant Nabu continuity/streaming notes with
    the accepted long-stream bound and live proof result.
- Impacted files/systems: runtime image, Fly staging proof resources, PR and
  Nabu records. No product dependency additions.
- Exit criteria: full tests, Ruff, diff checks, immutable image evidence, and
  a clean live smoke with terminal completion all pass.

## Acceptance criteria

1. A synthetic incremental stream with at least 513 raw JSON SSE events and a
   valid terminal sequence is accepted, and the consumer receives
   `execution.completed`.
2. An incremental stream that emits keepalives without a terminal event fails
   at the configured overall `stream_timeout`, even when each read remains
   within the idle socket timeout.
3. The ordered output preserves all assistant text chunks and tool start/end
   events that were accepted before the terminal event; no tool argument,
   result, URL, header, exception, credential, or private progress is exposed.
4. Total stream bytes, individual event bytes, line allocation, text/message
   size, transport timeout, invalid UTF-8, invalid JSON, unknown event names,
   identity changes, and incomplete terminal streams still fail closed.
5. The compatibility whole-result parser has an explicit retained-list guard
   of `MAX_BUFFERED_EVENTS = 65_536`, and that guard is independent from the
   existing `MAX_EVENTS` history, transcript, and marker bounds.
6. Backend and runtime tests plus Ruff and diff checks pass in the correct
   package environments.
7. A real staging smoke with Sandy's detailed fitness program shows more than
   512 raw frames, at least one valid terminal event, preserved streamed text,
   and no `malformed_response` caused by the old raw-event cap. Cleanup evidence
   is authoritative and complete.
8. The PR is ready for review. It is not merged without explicit owner
   approval.

## Backend considerations

### Query optimization plan

Not applicable. No backend query or endpoint changes are planned.

### N+1 prevention

Not applicable. The runtime continues to append events through the existing
Foundry client contract.

### Detailed unit test cases

- 510 deltas plus lifecycle and terminal frames complete successfully.
- More than the old 512 frames with interleaved tool start/progress/end keep
  activity IDs ordered and safe.
- A keepalive-only incremental stream reaches the configured overall deadline
  and raises `HermesTimeout`.
- Existing malformed JSON, unknown event, changed identity, missing terminal,
  byte limit, and oversized line tests remain failures.
- History row and inline transcript collection limits remain independent.
- The compatibility buffered parser rejects only its own retained-list bound.

## Test plan

- Focused runtime tests: `cd runtime; uv run --locked pytest tests/test_hermes.py tests/test_edges.py tests/test_fnd007_worker.py`
- Runtime lint: `cd runtime; uv run --locked ruff check .`
- Repository validation: `uv run --locked --project backend python scripts/validate.py`
- Full runtime coverage suite: `cd runtime; uv run --locked pytest --cov=allies_runtime --cov-report=xml:coverage.xml`
- Diff check: `git diff --check`
- Manual/live checklist: observe the long stream, verify terminal event and
  retained text, inspect sanitized evidence, and verify authoritative cleanup.

## Risks and mitigations

- Risk: removing the incremental raw-event cap could permit a peer to hold a
  worker slot forever. Mitigation: the parser enforces total bytes, event bytes,
  bounded reads, per-value text limits, idle socket timeouts, and the existing
  validated overall stream deadline; the compatibility retained-list path
  keeps its own explicit guard.
- Risk: changing the stream guard could accidentally weaken history or
  transcript validation. Mitigation: retain `MAX_EVENTS` for those collections,
  add `MAX_BUFFERED_EVENTS` only to the retained whole-result parser, and test
  the two paths independently.
- Risk: many normalized deltas can create many Foundry writes. Mitigation: keep
  the existing 4 MiB stream bound and event contract in this focused fix; track
  checkpoint/coalescing or event compaction as a separate product decision if
  live measurements show storage pressure.
- Risk: a live proof can leave infrastructure or credentials behind.
  Mitigation: use the bounded proof ledger, immutable image references, staged
  secret cleanup, and authoritative post-cleanup verification.
- Rollback/fallback: revert the runtime parser commit and use the last immutable
  runtime image digest. Do not change Hermes data or the durable Foundry schema.
