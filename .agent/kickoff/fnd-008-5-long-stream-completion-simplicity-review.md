# Simplicity Review

## Verdict

Simplification recommended. The revised plan protects the right outcomes and
has a narrow execution path, but it can avoid adding three separate collection
constants when the existing `MAX_EVENTS` already bounds the marker, transcript,
and history collections consistently.

## Findings

| Classification | Plan area | Evidence | Recommendation | Preserved outcome |
| --- | --- | --- | --- | --- |
| Simplify | Collection limits | `MAX_EVENTS` is used for bounded lists in `_content_contains_text`, `run.completed.messages`, and `profile_session_matches_markers`; the demonstrated defect is only the incremental raw-frame check and the retained `_sse_events` list. | Keep `MAX_EVENTS` as the shared bounded-collection limit for those three small lists. Add only `MAX_BUFFERED_EVENTS = 65_536` for the legacy whole-result parser, and remove the incremental use. Test the two paths independently. | History, transcript, and marker traversal remain bounded, while the long incremental stream can reach its terminal event. |
| Keep | Overall stream lifetime | The incremental adapter has per-read socket timeouts but no post-open wall-clock deadline. | Keep the existing validated `stream_timeout` as the overall incremental deadline and add the keepalive-only regression. | A peer cannot hold a worker slot forever after the raw-frame cap is removed. |
| Keep | Measured live gate | The old failure occurred just below the terminal frame, so an arbitrary successful prompt would not prove the fix. | Keep the Sandy detailed fitness-program prompt and require a measured raw-frame count above 512 plus terminal evidence. | Live acceptance exercises the exact boundary and preserves user-visible text. |
| Remove/Defer | Coalescing/checkpoint system | No current evidence shows storage pressure independent of this parser failure; Foundry's existing event contract forwards normalized deltas. | Keep compaction/checkpointing as a follow-up decision, not in this PR. | The focused fix does not change product event semantics or tool visibility. |

## Protected Complexity

- The overall lifetime deadline is necessary because the removed frame count was
  an accidental safety stop, not a valid replacement bound.
- The separate buffered-parser guard is necessary because that compatibility
  path retains all parsed events in memory, unlike the incremental worker path.
- The live staging smoke and authoritative cleanup remain required because the
  issue was first exposed in real usage and the runtime image is a deployed
  artifact.

## Plan Feedback For Revision

- Simplify the collection-limit change: retain `MAX_EVENTS` for the existing
  bounded collections and add only `MAX_BUFFERED_EVENTS` for `_sse_events`.
- Keep the overall stream deadline, exact Sandy prompt, measured `>512` gate,
  and live cleanup requirements.

## Residual Risk

The 4 MiB stream bound still permits many normalized Foundry writes. That risk
is explicit and should be measured separately rather than addressed by silently
changing the event vocabulary in this parser fix.

## Confidence

High - the simplification removes constants without removing any bound or
changing the demonstrated failure path.

## Independence note

The configured dedicated simplicity worker did not return after repeated bounded
waits and was interrupted. This review was completed in the orchestrator as the
explicit fallback, so it is evidence-backed but not an independent fresh
session.
