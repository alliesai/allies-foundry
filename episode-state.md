# Gmail passthrough — Foundry slice (joint contract + neutral gate)

## Identity
- Episode ID: gmail-passthrough-foundry-01
- Objective: Make Cloud-minted Gmail credentials executable: joint credential envelope/resolve contract, per-execution token-file materialization, provider-neutral `authorize_tool_call` gate, google-workspace runtime dependency verification.
- Work type: single-repo feature slice (Foundry runtime + image), joint contract with Cloud
- Route: fast
- Route reason: bounded single-repo change with a short plan, but security/data-bearing (secret bytes at rest, per-call enforcement) so adversarial review is required; simplicity review required because the neutral gate is new machinery.
- Repositories: allies-foundry
- Worktree/branch: allies-foundry/.worktrees/ft-gmail-passthrough @ ft/gmail-passthrough
- Intended base branch: dev (origin/dev @ 78048de)
- Delivery path: PR into dev; LANDING HELD — merge together with Cloud PR #55 as a unit, never alone (owner decision D-F1)
- Brief: this file + intake below
- Plan: allies-foundry/.worktrees/ft-gmail-passthrough/docs/plans/gmail-passthrough.md (pending)
- HTML required and reason: no — fast route, concise Markdown; no visual review requested.
- Command path/version provenance (closure commands):

## Current State
- Phase: plan-review
- Status: needs-input
- Last transition at: 2026-09-25
- Next action: owner answers 4 decisions below; then planner revision; then simplicity review; then approval gate
- Blocking condition: adversarial verdict Blocked (2 Blockers + 2 Majors); owner decisions unanswered (question tool aborted mid-session — see Missing Owner Decisions)

## Decisions
| ID | Decision | Source | Affected phases |
| --- | --- | --- | --- |
| D-F1 | Cloud PR #55 and Foundry PR land together as a unit; neither merges alone | User 2026-09-25 | implementation, closeout |
| D-F2 | credential_refs stays .env-only; Gmail delivery is a new token-file path, not an extension of env resolution | Code inspection 2026-09-25 (profile_store.py:1537-1541) | planning |
| D-F3 | ExecutionCommand is extra=forbid — envelope or resolve-callback must be a versioned joint contract, no unilateral Cloud change | Code inspection 2026-09-25 (contracts.py) + ADV-006/CR-004 | planning |
| D-F4 | google-workspace skill assumed catalog-present (bundled in Hermes image); runtime deps (client libs, gws) must be verified in Allies image | Code inspection 2026-09-25 (build_skills_catalog.py) + 2026-09-10 audit | planning |

## User Corrections
| At | Category | Correction | Artifacts updated |
| --- | --- | --- | --- |

## Evidence Index
| Evidence | Path or URL | Why it matters |
| --- | --- | --- |
| Boundary law | helpers/BOUNDARY.md | Kernel vs adapter placement; integration stays optional/isolated |
| credential_refs → .env only | allies-foundry/runtime/allies_runtime/profile_store.py:1537-1541,1605-1616 | Proves .env-only; token-file path is new |
| ExecutionCommand extra=forbid | allies-cloud/backend/allies/gateways/contracts.py (ContractModel) | Proves joint contract requirement |
| Hermes skill token path | hermes-agent/skills/productivity/google-workspace/scripts/google_api.py:42-43,181-200 | TOKEN_PATH + refresh/write-back drives access-only design |
| Skill catalog source | allies-foundry/runtime/hermes-image/build_skills_catalog.py | Catalog builds from pinned Hermes image |
| Cloud mint/grant/vault truth | Cloud PR #55 head d8d4a54 (backend/integrations/) | The exact Cloud side this slice plugs into |
| Parent plan + findings | allies-cloud/.worktrees/ft-gmail-managed-connection/docs/plans/gmail-managed-connection.md; episode ADV/CR/SIM findings | Requirements, watchpoints, deferred CR-004 |

## Review Mode
- Combined or separate:
- Risk and policy basis:

## Review Findings
| ID | Review | Severity | Disposition | Plan revision |
| --- | --- | --- | --- | --- |
| ADV-F001 | adversarial gpt-6-luna CLI 2026-09-25 | Blocker | Pending — mandatory pre-execution interception point (worker admission is not a gate; approval path lacks tool args for payload comparison) | planner revision |
| ADV-F002 | adversarial gpt-6-luna CLI 2026-09-25 | Blocker | Pending — per-execution token-file update/removal path independent of profile rebuild (seed fingerprint change yields CONFLICT, not rebuild) | planner revision |
| ADV-F003 | adversarial gpt-6-luna CLI 2026-09-25 | Major | Pending — versioned typed source/mapping for ref, command_id, allowlist, generation into the resolver; keep bytes out of persisted commands | planner revision |
| ADV-F004 | adversarial gpt-6-luna CLI 2026-09-25 | Major | Pending — Cloud ref-registry topology disposition (constrain+document vs shared store) + pinned Cloud head; revalidation on drift | owner decision + planner revision |
| ADV-F005 | adversarial gpt-6-luna CLI 2026-09-25 | Minor | Pending — cite done FND tickets, drop vague sign-off gate unless a distinct sign-off exists; add credential-file continuity test | owner decision + planner revision |

## Missing Owner Decisions (unanswered — question tool aborted before asking)
| # | Question | Recommendation |
| --- | --- | --- |
| Q1 | Who owns the joint credential-resolution contract: Foundry-defines/Cloud-serves, or Cloud-defines/Foundry-adapts? | Foundry defines, Cloud serves (Foundry owns the consumption constraint) |
| Q2 | Cloud process-local ref registry: constrain+document (+repair behavior) or build a shared/durable store first? | Constrain + document; shared store only if evidence demands |
| Q3 | Is any formal FND-005–008 sign-off required beyond the done tickets, and who owns it? | Tickets suffice + focused continuity test |
| Q4 | Pin joint validation to Cloud head d8d4a54, or float with revalidation? | Pin d8d4a54; any later Cloud commit re-triggers contract tests |

## Validation
| Command/check | Head SHA | Result | At |
| --- | --- | --- | --- |

## Delivery And Monitor
- Delivery mode: pull-request (held for joint landing with Cloud #55)
- PR URL:
- Head/base:
- Head SHA:
- Remote target SHA: 78048de (origin/dev at branch-off)
- Monitor status: not-applicable
- Monitor ID:
- Monitor terminal condition:
- Last verified checks/reviews:

## Closeout
- Deployment/promotion:
- Forest worktree: present
- Cleanup authorization source:
- Exact worktree owner at closeout:
- Live-use check/result (owner task and associated terminals/processes):
- Temporary retention owner:
- Retention revisit trigger: review/testing complete | merge | other
- Temporary artifacts:
- Durable records reconciled:
- Terminal evidence:

## Metrics
- Phase timestamps: intake 2026-09-25
- Planning worker runs: 0
- Adversarial review runs: 0
- Simplicity review runs: 0
- Implementation worker runs: 0
- Ponytail code-review runs: 0
- Correctness code-review runs: 0
- Context compactions observed: 0
- User correction count: 0
