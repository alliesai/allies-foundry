# Adversarial Review

## Verdict

Needs revision. The plan addresses the observed 512-frame failure, but it
needed an explicit lifetime bound after removing that frame count and more
concrete live and compatibility acceptance details. Those revisions are now
incorporated into the clean plan.

## Findings

| Severity | Area | Finding | Evidence | Recommendation |
| --- | --- | --- | --- | --- |
| Blocker | Resource lifetime | Removing the incremental raw-event cap without an explicit overall stream deadline could let a peer emit keepalives forever and hold a runtime worker slot indefinitely. | `stream_profile_incremental` only applies `stream_timeout` while opening the response; `_IncrementalHTTPStream.__anext__` applies the socket read timeout but has no wall-clock stream deadline. | Enforce the existing validated `stream_timeout` as an overall deadline after the response opens, retain the idle read timeout, and add a keepalive-only timeout regression. **Accepted and added to the plan.** |
| Major | Live acceptance | A generic long prompt did not guarantee more than 512 frames, so a live run could appear healthy without exercising the old failure boundary. | The captured Sandy run stopped at 510 deltas, immediately below the rejected terminal frame. | Specify Sandy's detailed 12-week fitness-program prompt, require an observed raw-frame count above 512, and rerun with a larger requested program if the first response is shorter. **Accepted and added to the plan.** |
| Major | Compatibility memory bound | The planned separate buffered-parser guard did not define a value or a memory rationale. | `_sse_events` retains a list of parsed events and currently shares `MAX_EVENTS`. | Use an explicit `MAX_BUFFERED_EVENTS = 65_536` guard, test it independently, and keep it off the incremental path. **Accepted and added to the plan.** |
| Minor | Durable knowledge | The plan made Nabu updates conditional even though the stream contract is changing materially. | Workspace instructions require revision-aware Nabu updates for material architecture or contract changes. | Make the Nabu continuity/streaming note update a required Phase 2 exit item. **Accepted and added to the plan.** |
| Question | Storage pressure | One Foundry event per normalized delta can still produce many database writes under the 4 MiB stream bound. | `FoundryWorker` forwards every `message.delta` and activity event. | Keep the current bounded event contract for this focused fix, record the concern as a follow-up decision, and measure the live evidence rather than silently coalescing user-visible text. **Risk accepted and retained.** |

## Missing Questions

- What live prompt reliably crosses 512 frames? The revised plan answers this with
  a detailed Sandy fitness-program prompt and a measured frame-count gate.
- What lifetime bound replaces the accidental frame-count termination? The
  revised plan uses the existing validated overall `stream_timeout`.

## Plan Feedback For Revision

- Add the overall incremental stream deadline and keepalive-only regression.
- State the exact compatibility list bound and test it separately.
- Make the live prompt and `>512` observation an explicit acceptance gate.
- Make the Nabu update a required delivery step.

## Confidence

High - the findings are tied directly to the parser implementation, the
captured boundary failure, and the repository's accepted stream contract.

## Independence note

The configured dedicated review worker did not return after repeated bounded
waits and was interrupted. This review was completed in the orchestrator as
the explicit fallback, so it is evidence-backed but not an independent fresh
session.
