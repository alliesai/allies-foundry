# Gmail Passthrough — Foundry Slice Plan

_Route: fast. HTML required: no. Branch `ft/gmail-passthrough` (base `origin/dev @ 78048de`).
Landing HELD: merges jointly with Cloud PR #55 as one unit, never alone (D-F1)._

## 1. Objective and current state

Cloud (PR #55, head `d8d4a54`) mints short-lived Gmail access tokens per execution
(`mint_execution_credential`, command-bound refs `gmail:{secret_id}:{command_id}:{nonce}`,
`resolve_credential_ref` with cross-command reject + expiry + generation check).
Nothing in Foundry can execute those credentials yet: `credential_refs` resolves
`.env`-only (`profile_store.py:1622-1630`, D-F2), there is no token-file path, no
tool-call gate, and the `google-workspace` skill's runtime deps in the Allies image
are unverified (`requirements.lock` carries only mnemosyne + PyYAML).

Intended outcome: Cloud-minted credentials become executable through a versioned
joint contract, per-execution `google_token.json` materialization, one
provider-neutral `authorize_tool_call` gate, and verified image deps — with the
skill unmodified, the kernel provider-blind, and core booting clean without the
integration.

## 2. Scope

### In scope

- Joint credential-resolution contract v1 (resolve-callback; §4) + Foundry
  resolve-client honoring it (R1).
- Generic per-execution file materialization on the profile seed path:
  opaque refs → `0600` files → scrub/re-materialize on rotation/revoke (R2).
  Gmail binding (filename, ACCESS-only shape) isolated to one small module,
  never core contracts (R5).
- One provider-neutral `authorize_tool_call` gate at the runtime tool-call
  boundary: execution allowlist + live generation + approval-payload match on
  sends; deny-by-default (R3).
- Skill-untouched proof: checksum guard + dead refresh write-back proof (R4).
- Image dep verification for `google-api-python-client` / `google-auth`
  (client libs the skill imports: `google.oauth2.credentials`,
  `google.auth.transport.requests`, `googleapiclient.discovery`), with the
  exact pinned-asset image change specified only if verification fails.
- BOUNDARY ticket tests 1/4/6 + watchpoint 3 evidence (R5); FND-005–008 entry
  prerequisites stated as evidenced-vs-blocking (R6).

### Out of scope

- Any Cloud-side work (OAuth, grants, mint, revoke, Interface): owned by
  Cloud PR #55. This slice consumes its `resolve_credential_ref` semantics.
- New Foundry event kinds, kernel OAuth/refresh logic, kernel tables (test 4).
- Calendar scopes, multi-account, background sync, per-workspace rollout flag.
- Hermes-overlay pre-exec patch: fallback only if adversarial review rejects
  the worker-boundary gate (§7 risk R-a).

## 3. Simplest approach per requirement

| Req | Approach (reuse first) |
| --- | --- |
| R1 joint contract | **Resolve-callback, not envelope field.** Cloud serves `POST` resolve; Foundry's existing injected `credential_resolver` seam (`composition.py:62-69`, `profile_store.py:1693-1704`) gains a versioned resolve-client. No `ExecutionCommand` change (it is `extra="forbid"`; avoiding the touch avoids cross-language fingerprint work and keeps secret bytes out of persisted command/outbox state per watchpoint 2). |
| R2 freshness | New **generic** seed entries (opaque `{ref, command_id, target_filename}`), resolved at materialization via the existing `_resolve_credential` sanitizing path, written `0600` by the existing atomic-write machinery (`_write_bytes_atomic`), fingerprinted into the seed (credential refs already feed `ProfileSeed.fingerprint`, `:519-543`), so a fresh ref forces rebuild, never the EXISTING-without-refresh path (`:1893-2000`). Cleanup/scrub reuses the existing cleanup + receipt path. |
| R3 gate | One new provider-neutral `authorize_tool_call(execution_allowlist, tool, args_hash, approval?, live_generation)` in the isolated module; consulted at worker admission and at the approval-decision point (`foundry.py` claim/approval flow, exact hook lines verify-only). Deny-by-default; send-class tools additionally require an approval whose bound `payload_sha256` exactly matches canonicalized call args. |
| R4 skill frozen | No changes under the catalogued skill tree. Tests: (a) checksum guard over the pinned skill source, (b) behavioral proof that materialized bytes without `refresh_token` make `google_api.py:189` (`if creds.expired and creds.refresh_token`) unable to take the refresh branch, so the `:191-196` write-back never fires; temp-`HERMES_HOME` E2E with an expired token asserting non-zero exit and no file rewrite. |
| R5 boundary | Core stays provider-blind: generic field names, opaque-string allowlist (same pattern as `memory_tool_allowlist` syntactic-only validation, `:264-275`), Google naming (`google_token.json`, `gmail …` strings) confined to the isolated module + Cloud. Grep tests enforce absence in core. Ticket tests 1/4/6 evidenced in §6. |
| R6 FND gates | No new lease/fencing/materialization semantics invented; slice rides evidenced machinery and names FND-008 + formal sign-off as blocking entry prerequisites (§6). |
| Image deps | Verify-first: probe the pinned base image for the three imports. If present, record evidence and change nothing. If absent, add hash-locked wheels to `requirements.lock` + offline install in `Dockerfile` following the exact mnemosyne wheelhouse pattern (`hermes-image-wheelhouse` target, `verify_wheelhouse.py`, `--no-index --no-deps`). |

## 4. Contracts (chosen mechanism: resolve-callback v1)

Producer owns schema (Cloud serves, Foundry implements the client; no shared
models/DBs). Authenticated (service identity, same pattern as existing
runtime calls), bounded (timeout/retry/idempotency per AL-05/AL-07; exact
numbers confirmed pre-rollout), privacy-safe (no tokens in logs; Foundry
`errors.py`/`evidence.py` sanitization patterns apply; resolution failures
surface as sanitized `ProfileStoreError("credential resolution failed")`).

### 4.1 Ref format (Cloud-owned, jointly pinned)

`gmail:{secret_id}:{command_id}:{nonce_hex8}` — opaque to Foundry except the
binding rule: the resolving call MUST present the same `command_id` the ref
was minted for; any other `command_id` is rejected (mirrors Cloud
`resolve_credential_ref`, `grants.py:181-204`).

### 4.2 Resolve request / response (representative JSON)

```json
{
  "schema_version": "v1",
  "kind": "credential.resolve",
  "ref": "gmail:3fa85f64-5717-4562-b3fc-2c963f66afa6:cmd_9f2a...:a1b2c3d4",
  "command_id": "cmd_9f2a..."
}
```

```json
{
  "schema_version": "v1",
  "kind": "credential.resolved",
  "expires_at": "2026-09-25T13:00:00Z",
  "grant_generation": 7,
  "tool_allowlist": ["gmail search", "gmail get"],
  "file": {
    "target_filename": "google_token.json",
    "access_bytes": "<ACCESS-only authorized_user JSON, never logged>"
  }
}
```

Error mapping (both sides implement): unknown/foreign-command ref →
`grant_denied` → Foundry `REPAIR_REQUIRED`; expired ref → `grant_denied
(expired)` → `REPAIR_REQUIRED`; provider/timeout race at mint →
`retryable`/`SERVICE_UNAVAILABLE`; revoked connection or generation
mismatch → `FENCED`/`REPAIR_REQUIRED (refresh_revoked)`. No new Foundry
error codes (`foundry.py:196-252` taxonomy reused).

### 4.3 ACCESS-only file shape (the only bytes Hermes ever sees)

```json
{
  "type": "authorized_user",
  "client_id": "<allies-owned client id>",
  "token": "<short-lived access token>",
  "expiry": "2026-09-25T13:00:00Z",
  "scopes": ["https://www.googleapis.com/auth/gmail.readonly",
             "https://www.googleapis.com/auth/gmail.send"]
}
```

No `refresh_token` key, no `client_secret`. `scopes` is informational for
the skill only; the kernel never parses it (watchpoint 3). Shape validated
in the isolated module before write: reject if `refresh_token` present,
if `token` absent, or if `expiry` unparseable → `REPAIR_REQUIRED`, nothing
written.

### 4.4 Gate contract (provider-neutral, in the isolated module)

```python
authorize_tool_call(execution_allowlist, tool, args_hash,
                    approval=None, live_generation=None) -> "allow" | ("deny", reason)
```

- `tool` not in `execution_allowlist` → `deny (grant_denied)`. Empty/missing
  allowlist → deny (deny-by-default; covers executions with no grant).
- `live_generation != execution generation` → `deny (fenced)`.
- Send-class tools (`…send`, `…reply` matched as opaque strings, never parsed
  scopes) additionally require `approval` whose bound `payload_sha256`
  exactly equals `args_hash` over canonical `{to, cc, subject, body,
  thread_id}`; mismatch/missing → `deny (approval_mismatch /
  approval_missing)`.
- Over-16-KiB normalized payloads are unapprovable (`payload_too_large`).

### 4.5 Fingerprint rule

This slice adds NO field to `ExecutionCommand` (extra=forbid untouched), so
no cross-language fingerprint migration is required. If joint review later
forces an envelope touch, both repos MUST implement identical
`canonical-json-sha256:v1` over the same stable projection with shared
fixture vectors, or the change is rejected (D-F3).

### 4.6 Merge / rollout order (D-F1)

Foundry slice PR (into `dev`, held) → Cloud PR #55 (held) → land together
as one unit after Phase 5 exit criteria + FND prerequisites evidence green.
Neither merges alone. No flag flip is owned by this slice; Cloud holds its
flag False until slice compatibility is evidenced (parent plan Phase 6).

## 5. Affected files / systems (uncertain paths are verify-only)

| Surface | Change | Req |
| --- | --- | --- |
| `runtime/allies_runtime/profile_store.py` | Generic file-materialization entries on/near `ProfileSeed` (opaque refs + target filenames, safety-validated as relative filenames); resolve via existing `_resolve_credential`; `0600` atomic write; fingerprint inclusion; cleanup scrub deletes materialized files with existing receipt machinery | R2, R5 |
| `runtime/allies_runtime/reconciliation.py` | Thread credential-file entries from claim payload into seed (existing `_mapping(payload, "credential_refs")` pattern at `:436`; exact key name verify-only) | R2 |
| New isolated module under `runtime/allies_runtime/` (exact filename verify-only — do NOT place in `profile_store.py`/`foundry.py` core) | Gmail binding (`google_token.json`, ACCESS-only shape check) + provider-neutral `authorize_tool_call` | R1, R3, R5 |
| `runtime/allies_runtime/foundry.py` worker claim/approval path (exact hook lines verify-only) | Consult the gate at admission + approval-decision; map deny reasons to existing terminal/`FENCED`/`REPAIR_REQUIRED` outcomes | R3 |
| `runtime/allies_runtime/composition.py` + settings (exact keys verify-only) | Wire resolve-client into existing `credential_resolver` injection; resolver absent → entries fail closed (`REPAIR_REQUIRED`), core boots clean | R1, R5 |
| `runtime/allies_runtime/config.py` (verify-only) | Only if a new env setting is needed for resolve endpoint URL/token; prefer existing settings patterns | R1 |
| `runtime/hermes-image/requirements.lock` + `Dockerfile` + smoke (`smoke_skills.sh` pattern) | ONLY if import probe fails: hash-locked google client wheels, offline install, build-time import smoke | Obj-4 |
| Cloud `backend/integrations/services/grants.py` + `google_oauth.py` (PR #55, read-only reference) | Consumed semantics: ref format, resolve, mint, revoke; no changes from this slice | R1, R2 |
| `hermes-agent/skills/productivity/google-workspace/*` | FROZEN — checksum guard only | R4 |
| No new kernel tables/migrations; no `ExecutionCommand` change | Explicit non-changes (tests 4, D-F3) | R1, R5 |

## 6. Phases (entry / exit gated)

### Step 1 — Joint contract pin + resolve-client (R1, R6-entry)

- Entry: Cloud PR #55 head confirmed (`d8d4a54` or recorded successor);
  FND-005/006/007 evidence acknowledged present in-worktree (lease/fence
  taxonomy `foundry.py:196-252`; seed/materialize/receipt/cleanup
  `profile_store.py`; ordered events + approval mirror `hermes.py`/`foundry.py`)
  with formal gate sign-off recorded as prerequisite; **FND-008 continuity
  proof confirmed by owner — BLOCKING if unverified (not evidenced in this
  worktree).**
- Work: freeze §4.1–4.2 + error mapping as the joint v1 surface with Cloud;
  implement the resolve-client on the existing injection seam; sanitized
  failures; redaction tests.
- Exit: contract fixtures shared with Cloud; resolve round-trip test
  (valid ref → bytes; foreign-command/expiry/generation-mismatch → typed
  deny); no `ExecutionCommand` diff on either side.

### Step 2 — Per-execution token-file materialization (R2, R5)

- Entry: Step 1 exit.
- Work: generic seed entries → `0600` files; fresh ref changes seed
  fingerprint → forced rebuild (never EXISTING-without-refresh);
  rotation/revoke/disconnect requeues re-materialization/scrub; stale files
  absent afterwards; Google naming only in the isolated module.
- Exit: fresh-command-bound-ref per execution; cross-command reuse rejected;
  same-command retry idempotent; post-cleanup file-absence test;
  `command_bytes`/outbox-equivalent holds refs, never bytes; grep test: no
  `google|gmail|scope` parsing in core.

### Step 3 — `authorize_tool_call` gate (R3)

- Entry: Step 2 exit.
- Work: isolated gate module; worker consults at admission + approval-decision;
  send-class requires exact approval-payload match; deny-by-default.
- Exit: matrix test (read/send/none × search/get/send/reply) green;
  read-only execution attempting send denied at every enforced point;
  post-approval field mutation denied (`approval_mismatch`); over-cap
  payload unapprovable; approval replay idempotent.

### Step 4 — Skill-frozen + dead-refresh proof (R4)

- Entry: Steps 2–3 exit.
- Work: checksum guard over pinned skill source; temp-`HERMES_HOME` E2E
  (real imports, expired token → non-zero exit, no write-back file);
  assertion that materialized bytes never contain `refresh_token`.
- Exit: guard + E2E green; skill tree diff empty.

### Step 5 — Image deps + boundary proof + joint readiness (Obj-4, R5, R1)

- Entry: Steps 1–4 exit.
- Work: import probe on pinned base; conditional pinned-asset change;
  BOUNDARY tests 1 (no-Cloud boot: resolver absent → clean boot +
  fail-closed entries), 4 (no kernel OAuth/billing/RBAC tables — migration
  diff empty), 6 (no vendor naming in core — grep + reviewer check);
  watchpoint 3 (no scope parsing in kernel) evidenced; joint landing
  checklist with Cloud #55 (D-F1).
- Exit: all acceptance green; PR held for joint landing; rollback = revert
  one PR (no migrations), image revert by tag if Step 5 changed it.

## 7. Material risks + rollback

- **R-a (gate bypass via non-approval tool path).** Hermes executes tools
  internally; Foundry observes via events + approval mirror (`terminal |
  execute_code | plugin_tool`). If adversarial review shows a send-class
  invocation reachable without passing the worker gate, fallback is a
  minimal source-pinned Hermes pre-exec hook (established `patches/`
  pattern) consulting the same `authorize_tool_call` policy — explicit
  open decision, not silent scope growth.
- **R-b (Cloud ref registry is process-local memory, `grants.py:147-165`).**
  Cloud restart drops live refs → resolves fail → executions go
  `REPAIR_REQUIRED`. Owned by Cloud; joint landing must accept or fix —
  Foundry mapping already fails closed, never open.
- **R-c (expiry mid-execution).** By design: no kernel refresh (test 4);
  skill exits non-zero → existing `retryable`/`repair_required` +
  `gmail_credential_expired` reason. No new event kind.
- **R-d (secret handling).** Resolve-client must never log bytes
  (AL-09); audit carries ref + expiry + allowlist-hash only (Cloud
  `grants.py:233-243` pattern mirrored Foundry-side).
- **Rollback:** pre-landing, revert the single Foundry PR (no migrations,
  core boots clean without the integration). Post-landing, joint revert
  with Cloud #55 per D-F1; in-flight executions fence via generation
  mismatch and refs expire within short TTL.

## 8. Validation basis (exact repo checks — run, do not claim)

From worktree `Makefile` + `.github/workflows/ci.yml` +
`.github/workflows/hermes-image.yml` + `AGENTS.md`:

- `make check` (backend check + migrations-check + runtime lock check)
- `make validate` (`scripts/validate.py`)
- `make lint` (ruff backend + runtime; `make format` if touched)
- `make test APP=<path>` (backend) and `make runtime-test`
  (`cd runtime && uv run --locked pytest --cov=allies_runtime`)
- Image (only if changed): `make hermes-image-wheelhouse`,
  `make hermes-image-build`, `make hermes-image-test`
  (`smoke_skills.sh` + approval/memory/reasoning smokes)
- New tests, each mapped: contract fixtures (R1), freshness/reuse/scrub
  (R2), gate matrix + mismatch (R3), checksum + dead-refresh E2E (R4),
  grep/boundary tests 1/4/6 + watchpoint 3 (R5), no-Cloud boot (R5).

## 9. Unresolved decisions

1. Exact new-module filename + worker hook lines (verify-only at
   implementation; must stay out of core contracts).
2. R-a disposition: worker-boundary gate sufficient, or Hermes pre-exec
   hook required (adversarial review input).
3. Resolve transport details: endpoint path, service-token reuse vs
   dedicated credential, timeout/retry values (AL-05 categories fixed;
   numbers confirmed pre-rollout with Cloud).
4. Resolver URL/token settings keys (verify-only; reuse existing patterns).
5. Claim-payload channel for allowlist/generation/approval binding
   (verify-only; reuse existing approval-mirror fields where possible).
6. FND-008 evidence location + formal FND-005–008 sign-off owner (blocking
   entry prerequisite).
