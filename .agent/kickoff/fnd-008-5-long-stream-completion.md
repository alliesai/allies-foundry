# FND-008.5 Long-Stream Completion

## Status

- Type: bug / reliability improvement
- Implementation delegation: never
- Delegation source: kickoff fallback default; no task-specific worker selection
- Review worker: `codex` selector from `.agent/kickoff.yaml`; exact worker resolution pending review-skill handoff
- Planning mode: full
- Worktree manager: Forest
- Branch: `feat/fnd-008-5-long-stream-completion`
- Worktree path: `E:\Users\Oluwatimilehin\Documents\Programming\helpers\allies-foundry\.forest\worktrees\feat\fnd-008-5-long-stream-completion`
- Task workspace: `.agent/kickoff/fnd-008-5-long-stream-completion`
- Created: 2026-08-12
- Target date: 2026-08-12, subject to reviewed plan and live validation
- Current phase: implementation and focused validation

## Objective

Allow Allies to complete and preserve long Hermes gateway streams whose valid
terminal event arrives after more than 512 raw SSE events, while retaining
bounded resource use and the existing safety, tool-event, profile-isolation,
and continuity guarantees.

## Context

The merged FND-008 runtime was exercised with realistic Sandy/Greg sessions.
Sandy's long response streamed 510 ordered `message.delta` events but no
terminal completion was delivered to the caller. The runtime returned
`malformed_response` because `runtime/allies_runtime/hermes.py` counts every
raw SSE frame and rejects frame 513 before parsing it. The expected terminal
frame was therefore rejected after `run.started`, `message.started`, and 510
assistant delta frames. Greg's shorter stream completed.

This is an Allies Runtime Hermes SSE parser failure, not evidence of Fly volume
loss or Hermes session-history loss. The fix must support long-running tasks,
including tool lifecycle events and large numbers of progress/delta frames,
without weakening terminal validation or unboundedly buffering data.

## Requirements

- Reproduce the failure with a synthetic stream containing more than 512 raw
  frames and a valid terminal event.
- Preserve ordered semantic events and valid terminal completion after long
  assistant output and tool activity.
- Replace the universal raw-event cutoff with explicit, bounded protections
  appropriate to stream bytes, text/message size, idle/overall time, and any
  remaining parser safety limit.
- Keep terminal validation strict: malformed, truncated, or missing terminal
  streams remain failures.
- Keep tool lifecycle events observable and ordered; progress deltas may be
  coalesced or bounded only where the semantic contract permits.
- Do not change FND-008 Machine replacement, profile isolation, immutable-image,
  file-credential, generation-fencing, or cleanup guarantees.
- Add focused regression tests and run the complete backend/runtime validation
  required by the repository.
- Perform a real staging long-stream smoke test before claiming the issue is
  closed, with evidence retained under `.agent/` and no secrets committed.

## Acceptance Criteria

1. A stream with at least 513 raw SSE frames followed by a valid completion is
   accepted and yields the completion event.
2. Long assistant output and interleaved tool lifecycle/progress events preserve
   semantic ordering and bounded memory/byte/time limits.
3. Existing malformed/truncated/oversized/timeout safeguards still fail closed
   with stable sanitized error receipts.
4. The realistic Sandy/Greg-style long-stream behavior completes successfully
   in a live staging smoke test, and the captured evidence shows the terminal
   event plus preserved streamed text.
5. Backend/runtime tests, Ruff, diff checks, code review, CI, and the GitHub
   review loop pass; the PR is ready to merge. Merging remains subject to the
   user's explicit approval.

## Evidence And Sources

- Merged FND-008 PR: https://github.com/Timmyy3000/allies-foundry/pull/10
- Captured long-stream evidence: `.agent/fnd008-sandy-greg-r2-first-streams.json`
- Live conversation evidence: `.agent/fnd-008-live-conversation-r2.json`
- Pinned Hermes runtime behavior and FND-008 proof notes in the repository
  napkin and Allies continuity specifications.
- Suspect parser: `runtime/allies_runtime/hermes.py`, `MAX_EVENTS = 512` and
  `HermesIncrementalStream._finish_event()`.

## Decisions

- Treat this as FND-008.5, a focused follow-up branch off merged `origin/dev`.
- Use the full kickoff planning path because parser bounds, stream semantics,
  tool events, and live rollout/rollback are material reliability concerns.
- Keep implementation delegation `never`; implementation stays in this agent.
- Preserve the existing proof's direct transcript and terminal-event evidence;
  do not replace it with model recall or stable IDs.

## Risks

- Raising or removing a raw frame cap without byte/time bounds could allow
  memory or resource exhaustion.
- Coalescing the wrong events could hide tool progress or reorder terminal
  events.
- Runtime and deployed backend/image contracts may need coordinated rollout.
- Live staging validation consumes Fly/provider resources and must clean up
  authoritatively.

## Open Questions

- Which existing stream byte, text, timeout, and persistence bounds already
  protect this path, and which need adjustment?
- Should coalescing happen in the Hermes parser, the runtime event adapter, or
  the Foundry persistence boundary?
- What live prompt reliably produces a terminal stream beyond 512 frames with
  synthetic or approved staging credentials?

## Plan

Plan paths:

- Lavish review artifact: `.lavish/fnd-008-5-long-stream-completion-plan.html`
- Clean Markdown plan: `.agent/kickoff/fnd-008-5-long-stream-completion-plan-edited.md`
- Preserved draft: `.agent/kickoff/fnd-008-5-long-stream-completion-plan-draft.md`
- Draft-to-clean comparison notes: `.agent/kickoff/fnd-008-5-long-stream-completion-plan-edit-notes.md`

The plan recommends removing the raw-event rejection only from the
incremental parser, retaining `MAX_EVENTS` for existing bounded collections,
and adding a separate retained-list guard for the legacy whole-result parser. It now also
enforces the existing validated `stream_timeout` as an overall incremental
stream deadline, defines `MAX_BUFFERED_EVENTS = 65_536`, and makes the Sandy
long-prompt `>512` frame count and required Nabu update explicit delivery gates.
The adversarial and simplicity review outputs are retained at
`.agent/kickoff/fnd-008-5-long-stream-completion-adversarial-review.md` and
`.agent/kickoff/fnd-008-5-long-stream-completion-simplicity-review.md`.

## Execution Notes

- 2026-08-12: Confirmed GitHub `dev` now points at merged FND-008 commit
  `e79fa66`; created isolated Forest worktree from `origin/dev`.
- 2026-08-12: Initial reproduction evidence shows 510 `message.delta` events,
  then `malformed_response` when raw frame 513 is rejected before the valid
  terminal event.
- 2026-08-12: Created the full reviewed-plan draft, clean edited copy, and
  Lavish artifact. The browser audit initially flagged the skip link as
  unreachable; the page-owned shell override was corrected and the follow-up
  audit produced no fresh warning before the poll timeout.
- 2026-08-12: The configured adversarial worker did not return after repeated
  bounded waits, so it was interrupted. The orchestrator ran the explicit
  fallback review, recorded the independence limitation, and revised the plan
  with an overall stream deadline, exact buffered guard, reproducible live
  prompt, and mandatory Nabu update.
- 2026-08-12: The configured simplicity worker also did not return after
  repeated bounded waits, so it was interrupted. The orchestrator ran the
  explicit fallback review, simplified the plan to add only
  `MAX_BUFFERED_EVENTS` while retaining `MAX_EVENTS` for existing collections,
  and recorded the independence limitation.
- 2026-08-12: Owner approved the reviewed plan. Added failing-first regressions
  for a terminal after 512 raw frames, keepalive-only overall timeout, and the
  independent buffered-parser guard. Focused tests now pass; the full runtime
  suite passes 278 tests with five skips and the backend suite passes 201 tests
  with two skips. Runtime coverage is 90.08%; both runtime and backend Ruff
  checks and `git diff --check` are clean. Ruff format check still reports
  pre-existing formatting drift in `foundry.py` plus repository line-ending and
  touched-file formatting suggestions; no formatter rewrite has been applied.
- 2026-08-12: Implemented the approved parser fix. Incremental streams no
  longer reject valid terminal events solely because more than 512 raw frames
  preceded them; the existing byte, text, terminal, and overall stream-time
  bounds remain active. The whole-result parser now uses an independent
  `MAX_BUFFERED_EVENTS = 65_536` guard, and the configured stream timeout is
  enforced after the incremental response opens, including keepalive-only
  streams.
- 2026-08-12: Published immutable runtime image
  `ghcr.io/timmyy3000/allies-runtime@sha256:c5e84e5f4ca6164c7c69aebb1145f05a9a7d83834b3f1dbe246b8cabfa38a20e`.
  The real Sandy generation-one response completed through the new parser with
  762 persisted events and terminal `ok`; Greg completed with 74 events. The
  interactive replacement adapter stalled before creating active rows and was
  cleaned up without claiming a continuity result.
- 2026-08-12: The separate standard same-volume proof passed all live gates
  with the same image: generation-one startup, isolated turns, two overlapping
  streams plus queued work, volume-preserving Machine replacement, old-claim
  fencing, generation-two isolated session continuity, and complete cleanup.
  Sanitized evidence is retained in `.agent/fnd-008-5-standard-proof-debug1.json`.
- 2026-08-12: Pre-PR local review covered the incremental parser state machine,
  deadline/cancellation path, byte and retained-list bounds, session/tool
  validation, security isolation, and the new regression tests. No actionable
  P0-P2 findings were identified. The isolated reviewer could not start
  because the configured reviewer model is unsupported for this account; this
  limitation is recorded and the review was completed locally instead.
- 2026-08-12: Pushed the branch and opened PR #11 targeting `dev`:
  https://github.com/Timmyy3000/allies-foundry/pull/11. No merge was performed.
