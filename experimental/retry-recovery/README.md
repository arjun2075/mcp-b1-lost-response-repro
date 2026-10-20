# Retry recovery: collaboration plan and scenario matrix

Status: draft for collaborator review; updated 2026-10-20.
The matrix contains both executed and proposed cases; coverage is identified below. These
expectations are not adopted MCP semantics.
A real-MCP recovery harness is implemented and run (see below). A non-dispatching PIC
signed-approval adapter is implemented in `pic_adapter/` and exercised with real pinned PIC
verification. A03 remains unexecuted as an MCP scenario; pruning, durability and concurrency
remain unexecuted.

## Purpose and ownership

Build deterministic test material for the retry-identity work discussed in
[modelcontextprotocol/modelcontextprotocol#3394](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394).
Arjun owns the failure-case matrix, lost-response/recovery harness and effect-count oracle.
Fabio contributes approval verification and the signed-approval adapter (`pic_adapter/`,
see [adapter-contract.md](adapter-contract.md)).
ndreno proposed taking the SEP specification and rationale. Sponsor, wire format and final
conformance expectations require their own review; this fixture does not predetermine them.
Other contributors' vectors must retain source attribution and their stated limitations.

The original root scripts and requirements remain unchanged. They use a real MCP SDK
client/server over in-memory streams and a synthetic in-process effect ledger, not a real
payment provider. The expanded fixture must describe its own persistence and transaction
boundaries explicitly; it must not turn a local ledger result into an exactly-once
guarantee for a remote payment.

## Fixed baselines

| Component | Pin | Use |
| --- | --- | --- |
| Original lost-response repository | `6373793aed3c98c281d6fa695afe9fb6fa541586` | Preserve both root scripts and root requirements |
| MCP Python SDK in original baseline | `mcp==2.2.0` | Preserve negotiated-version logging; no SDK upgrade in this work |
| Fabio's refund example | [`eb7cd4672addc02e1ea80a7a83fadf97d9b0a93e`](https://github.com/pic-standard/pic-standard/tree/eb7cd4672addc02e1ea80a7a83fadf97d9b0a93e/examples/refund-approval) | Vendored unmodified under `pic_adapter/vendor/`; example source, fixtures and workflow checks |
| PIC SDK dependency declared by that example | `330fdd817ef81d43461ea1787939e0d1cc87d589` | `pic-standard[crypto]` source pin; package version alone is insufficient |
| PIC transitive constraints | `constraints.txt` at the example pin (vendored) | Applied to every installation step of the approval setup |

Record Python version, both SDK source/version identities, dependency resolution and
negotiated MCP protocol version in results. The combined install has been tested:
`mcp==2.2.0` and the crypto-only PIC pin resolve together when the refund constraints are
applied to both installation steps (Python 3.11.9). The combined environment retains the
pinned refund dependencies and adds MCP and its dependencies. Its resolved package list is
recorded in `results.json`. PIC's `[mcp]` extra is not installed because it requires
`mcp<2`. Approval verification still runs in a separate worker process, to isolate the
vendored module names and the test clock, not for dependency reasons.

## Shared definitions and independent oracles

- **Operation identity:** a fixture-owned stable identity scoped to authenticated tenant,
  principal and tool, with a bound semantic request. Approval references, attempt IDs,
  JSON-RPC IDs and mutable authorization epochs are distinct from it.
- **Effect oracle:** an append-only synthetic provider ledger independent of the
  deduplication store. Pruning the dedup store must not prune the oracle.
- **Recovery knowledge:** recorded completion, reserved/unknown, authoritative non-execution,
  or insufficient evidence. Actual provider completion may differ from what recovery knows.
- **Disclosure oracle:** inspect actual client-visible content, structured content and errors
  for a unique receipt marker. A denied result must not leak through an error or metadata.
- **New-execution permission:** current caller permission plus applicable approval/profile
  verification. It is distinct from current permission to disclose a historical receipt.
- **Recovery window:** fixture policy with explicit admission evidence. A caller-supplied
  creation time is not authenticated merely by being present.

Default recovery profile proposed for review: recognize an existing operation without
dispatch; authorize receipt disclosure independently; require current execution permission
for any new dispatch; never infer non-execution from a pruned-record lookup miss.
Recognition must not itself disclose operation existence across tenants.

The execution coverage table below identifies the implemented subset; other rows remain
planned. Except the two baseline rows, expectations describe candidate application-layer
controls to test, not MCP conformance. In-process policy tests are distinguished from SDK cases.

## Initial deterministic matrix

| ID | Setup / fault / transition | Expected effect count | Client-visible expectation / evidence |
| --- | --- | --- | --- |
| B01 | Original unguarded baseline: commit, drop response, fresh-ID application retry | 2 | Preserve original demonstration; observe timeout and second completion |
| B02 | Original positive control: same stable logical-operation key | 1 | Preserve original application-layer dedup demonstration |
| R01 | Commit and store receipt, drop response; same operation, valid approval and allowed disclosure | 1 | Return original receipt, no redispatch |
| R02 | Reserve operation, commit synthetic provider effect, interrupt before receipt storage; no provider lookup | 1 | Recovery remains unknown, no redispatch; oracle knows more than recovery |
| R03 | Same interrupted boundary as R02, provider supports authoritative operation lookup | 1 | Reconcile original receipt; log that lookup, not key alone, closed the gap |
| R04 | Deliberately unsafe effect-then-record control loses record; retry admitted | 2 | Demonstrate remote/local transaction gap; expected counterexample, not safe behavior |
| R05 | Provider commits, then returns an error before recovery receipt storage; reservation retained | 1 | Retry stays unknown without dispatch; contrasts with unsafe success-only cache eviction in R04 |
| A01 | Approval expires after first commit; receipt retained; disclosure independently allowed | 1 | Return historical receipt without treating expired approval as new-effect permission |
| A02 | Same expiry, but current receipt disclosure denied | 1 | Denial without receipt marker, no dispatch |
| A03 | Signer key revoked after first commit; retained receipt; recovery identity independently authenticated | 1 | Reject that approval for new execution; disclose historical receipt only under explicit current recovery policy |
| A04 | Caller execution authority withdrawn after first commit | 1 | No new effect; vary disclosure permission independently and assert both outcomes |
| A05 | Same key/arguments presented by another tenant or principal | 1 for original; no new effect in rejection case | No original receipt/existence leak; reject fixture context change; separately defined genuinely new operations are not this case |
| A06 | Same scoped operation ID with changed payment, amount, currency or tool | 1 | Conflict/rejection, no cached receipt substitution and no dispatch; include signed-payload tampering control |
| A07 | New valid signed approval/ref, same authenticated decision-to-operation mapping | 1 | Recover same operation; new approval is not new operation identity |
| T01 | Receipt/dedup record pruned at 24h, approval still valid at day 2 of 7 days | 1 under safe profile | Reject recovery or report unknown according to chosen policy; lookup miss alone never authorizes dispatch |
| T02 | Same pruning plus approval reissuance | 1 under safe profile | Reissuance must not reset original operation admission window; retain independent mapping/tombstone or reject unverifiable recovery |
| T03 | Caller replaces claimed creation timestamp to appear fresh | 1 under safe profile | Reject changed/untrusted admission evidence; timestamp alone cannot restore eligibility |
| T04 | New, explicitly admitted operation after old window; distinguish from retry | 2 total only when independently authorized as new | Positive control: do not collapse legitimately distinct operations solely because arguments match |
| C01 | Concurrent requests with same scoped ID and semantic request | 1 under safe profile | Atomic reservation; other request gets retained result or in-progress/unknown; no unlocked check-then-act |
| X01 | Duplicate transport delivery with same operation identity | 1 under safe profile | Application effect suppression; log transport fault separately from model/application retry |
| X02 | Read-back returns empty while original request is in flight or provider visibility is delayed | 1 after controlled completion | Remain unknown until reliable evidence; missing list entry is not proof of non-execution |
| X03 | Transport success with empty payload | Assert actual oracle independently | Do not report confirmed success/non-execution solely from transport status; inspect operation outcome evidence |

For A03, denying all recovery under a deployment's policy is also a legitimate candidate.
Do not force PIC approval freshness or key trust to authorize disclosure. Agree the chosen
fixture policy and expected output before coding that row.

For T01-T03, demonstrate the unsafe lookup-miss behavior separately if useful, but never
describe the safe profile as achievable after deleting all recognition/admission evidence
while still allowing arbitrary fresh admission. Retained tombstones/mappings, server-bound
admission evidence or an explicit refusal policy are design options, not decided wire fields.

## Harness requirements

1. Commit exactly one synthetic effect before injecting the lost response; assert the
   first result is actually dropped and the client observes the intended SDK timeout.
2. Record both JSON-RPC IDs and stable operation identity. Keep semantic request arguments
   equal across retries. The original scripts vary `attempt_id`; any new semantic digest
   must explicitly exclude harness-only attempt metadata rather than claiming byte-identical
   arguments in the original scripts.
3. Inject transitions with barriers and a controllable clock. Do not wait 24 hours or use
   sleeps to create a race. The pinned SDK has no clock parameter; the approval cases use a
   worker-scoped test clock described in [adapter-contract.md](adapter-contract.md).
   Signature and freshness verification are never stubbed and reported as PIC verification.
4. Keep effect ledger, dedup state, approval verification, caller authorization and disclosure
   policy separately observable. No real payments, production credentials or personal data.
5. Missing/invalid approval bindings or adapter errors cannot reach a new-effect dispatch.
   Recovery can use a separately authenticated original operation mapping; an unverified
   claim inside a rejected approval is never sufficient for receipt access.
6. Concurrency and restart claims need their own exercised storage/locking path. Until then
   label in-process tests as in-process, not durable or crash-safe.
7. Archive per-attempt traces: transition, admission decision, approval result, lookup state,
   dispatch count, effect delta, disclosure decision and client-visible classification.
   Log receipt digests/test markers rather than private key material.
8. New test assertions must exit nonzero on failure. Baseline script stdout/exit alone is
   not sufficient: its unexpected-count branch currently only prints a message.

## Adapter and review

See [adapter-contract.md](adapter-contract.md) for the implemented contract
(`retry-recovery-draft-1`), its clock and keyring controls at the SDK pin, and the items
open for review. The adapter lives in `pic_adapter/`; the refund example it reuses is
vendored unmodified under `pic_adapter/vendor/` (see `SOURCE.md`). Contributions come
through forks; repository write access is not required.

## Executed first subset

Complete the [PIC approval setup](#pic-approval-setup) below before running the combined
runner, which now also runs the approval cases. Then, from this directory:

```bash
.venv/bin/python run_cases.py --output results.json
.venv/bin/python -m unittest -v test_recovery
```

The runner checks mcp==2.2.0. It uses actual ClientSession/MCPServer memory streams,
fresh JSON-RPC IDs and a deterministic after-commit response drop with a 0.25s SDK
read timeout. This shorter timeout differs from the unchanged original 5s baseline.
There is one explicit scripted application retry per scenario, no automatic SDK retry.
The tool is a synthetic refund, not a real payment or scheduling integration.

| Runner case | Matrix coverage | Observed effects / retry result |
| --- | --- | --- |
| unguarded_lost_response | B01 shape; ryjen A | 2 / completed |
| stable_identity_retry | R01; ryjen B under this in-process enforcement | 1 / original receipt |
| intentional_repeat | T04 identity distinction; ryjen D | 2 / completed (new operation ID) |
| commit_then_error | R05; R02 reserved/unknown boundary | 1 / unknown |
| unsafe_success_only_cache | R04 | 2 / completed (expected unsafe control) |
| result_disclosure_denied | Disclosure half of A02; no approval in this case | 1 / denied, no receipt marker in delivered wire response |
| execution_revoked_recovery_allowed | A04 with disclosure allowed | 1 / original receipt; no new execution |
| different_tenant_rejected | A05 | 1 / denied, no original receipt marker |
| same_identity_changed_args | Semantic mismatch half of A06; no signature verification | 1 / conflict |
| fresh_key_each_attempt | ryjen E | 2 / completed; models fresh per-attempt IDs, not a production server claim |

`results.json` records source hashes, versions, actual provider counts, client observations,
scripted client reports, JSON-RPC IDs and injected faults. No LLM participates and no PIC
checks are mocked as if verified. The existing `environment.txt` and `b1_*.log` files
document Arjun's earlier baseline runs; the `pip freeze` embedded in the regenerated
`results.json` describes the combined run reported here.

The stdlib policy tests in `test_recovery.py` cover an unresolved reservation, receipt-copy
isolation, revoked execution versus allowed recovery, principal isolation, invalid amounts,
post-commit error recovery, delayed/in-flight negative read-back (ryjen C / X02), and empty
success (X03). The read-back test advances a controlled model state; it is not a concurrent
provider or a real network/eventual-consistency experiment. Provider facts remain separate
from recovery knowledge: an effect can exist while the client correctly reports unknown.

Majny's scheduling fixture is proposed separately in
[PR #3](https://github.com/arjun2075/mcp-b1-lost-response-repro/pull/3) and is not included
in the results reported here; R01/T04/R05 are corresponding synthetic-provider patterns for
comparison. The original root scripts were rerun separately with their unchanged
requirements and checked for exactly one fault and counts 2 / 1; captured stdout is in the
two `b1_*.log` files.

## PIC approval setup

Use Python 3.11 and apply the vendored refund constraints to every installation step.
Paths below are POSIX; on Windows use `.venv\Scripts\python.exe`.

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r ../../requirements.txt -c pic_adapter/vendor/refund_approval_eb7cd467/constraints.txt
.venv/bin/python -m pip install "pic-standard[crypto] @ git+https://github.com/pic-standard/pic-standard.git@330fdd817ef81d43461ea1787939e0d1cc87d589" -c pic_adapter/vendor/refund_approval_eb7cd467/constraints.txt
.venv/bin/python -m pip check
```

The vendored example's own `self_check.py` temporarily rewrites installed package metadata,
so its baseline uses a second, dedicated environment containing only the PIC pin:

```bash
python3.11 -m venv .venv-selfcheck
.venv-selfcheck/bin/python -m pip install "pic-standard[crypto] @ git+https://github.com/pic-standard/pic-standard.git@330fdd817ef81d43461ea1787939e0d1cc87d589" -c pic_adapter/vendor/refund_approval_eb7cd467/constraints.txt
```

Then, from this directory:

```bash
.venv/bin/python pic_adapter/run_vendor_baseline.py --python .venv/bin/python --selfcheck-python .venv-selfcheck/bin/python
.venv/bin/python -m unittest -v test_recovery test_pic_adapter test_run_vendor_baseline test_run_cases
.venv/bin/python run_cases.py --output results.json
```

`.venv/`, `.venv-*/` and `local-results/` are git-ignored. The baseline runner executes the
upstream checks in a disposable copy and never writes into the vendored tree.

## Executed approval subset

The approval cases use the same MCP memory streams and response-drop fault, with real pinned
PIC verification in the `pic_adapter` worker and a documented 2.0s SDK read timeout sized for
verification. The provider, recovery store and caller authorization remain synthetic. Each
case asserts, for both attempts, the provider effect count, delivered status, client-reported
outcome and the adapter's decision; every delivered receipt is compared with the independent
provider record, and cases with no completed attempt must deliver no receipt marker.

| Runner case | Matrix coverage | Effects per attempt / final result |
| --- | --- | --- |
| approval_stable_identity_retry | R01 with real approval | 1, 1 / original receipt |
| approval_recovery_without_fresh_approval | R01; retry presents no approval | 1, 1 / original receipt; one adapter call |
| approval_expired_recovery_allowed | A01 | 1, 1 / original receipt; adapter `approval_expired` |
| approval_expired_disclosure_denied | A02 | 1, 1 / denied, no receipt marker |
| approval_execution_withdrawn_disclosure_allowed | A04 | 1, 1 / original receipt |
| approval_execution_withdrawn_disclosure_denied | A04 | 1, 1 / denied, no receipt marker |
| approval_other_tenant | A05 | 1, 1 / denied, no receipt |
| approval_other_principal | A05 | 1, 1 / denied, no receipt |
| approval_changed_args_conflict | A06 | 1, 1 / conflict |
| approval_tampered_new_operation | A06 tampering control | 1, 1 / denied; adapter `signature_or_binding_rejected` |
| approval_reissued_recovery | A07 | 1, 1 / original receipt |
| approval_retained_decision_mismatch | A07 negative control | 1, 1 / conflict; original decision mapping retained |
| approval_replay_fresh_operation | Replay control (provisional: conflict) | 1, 1 / conflict |
| approval_second_decision_new_operation | T04-style positive control | 1, 2 / both completed with their own receipts |
| approval_expired_new_effect_denied | New effect under expired approval | 0, 0 / denied |
| approval_revoked_new_effect_denied | New effect under revoked signer | 0, 0 / denied; adapter `signer_revoked` |
| approval_commit_then_error | R05 with real approval | 1, 1 / execution error then unknown |

A05 here demonstrates that the attempted context change is rejected without returning the
original receipt. It does not establish existence hiding; indistinguishable responses for
present and absent identifiers are not tested.

Supporting tests: `test_pic_adapter.py` verifies the adapter through a real worker (valid,
tampered, expired, revoked, untrusted, reissued, malformed and real `PIC_VERIFIER_FAILED`
inputs, plus client timeout, crash, cancellation and replacement paths).
`test_recovery.py` adds approval-mode engine policy tests driven by synthetic adapter
responses; they establish engine policy, not PIC verification. `test_run_vendor_baseline.py`
covers the vendored-baseline runner.

`test_run_cases.py` checks that the runner refuses to run with assertions disabled
(`python -O` or `PYTHONOPTIMIZE`), since its scenario validation uses `assert`.

`results.json` additionally records per-verification request and response diagnostic digests
(distinct from PIC's signed canonical digests), adapter decisions and their role, per-call
latency, worker health, the adapter source, keyring and vendor-manifest hashes, a `pip freeze`
of the run environment and the list of pending cases. Interpreter paths are display metadata
relative to the runner directory; the Python version, package freeze and verified SDK commit
identify the environment.

Approval integration boundary: verification is real; the provider, recovery store and caller
authorization are synthetic fixture components. Fresh approval is not required for retained
recovery; a presented, currently verified decision is checked for consistency. Retained
recovery currently ignores approval freshness and signer status; this candidate A03 policy
has synthetic engine tests, and A03 remains unexecuted as an MCP scenario pending review.
This does not claim exactly-once remote effects, cross-process durability, restart safety,
an adopted wire format, or merged MCP conformance tests.

Suggested implementation sequence:
1. Agree matrix scope and adapter contract here. (Contract implemented as draft-1; open
   items listed in adapter-contract.md.)
2. Add the recovery harness and separate synthetic effect/dedup stores. (Done.)
3. Integrate actual pinned PIC verification; run independent PIC baseline first. (Done.)
4. Add expiry, the two revocation cases, disclosure and reissuance tests. (Done except the
   A03 MCP scenario, pending its policy.)
5. Add pruning/unknown-outcome cases, then concurrency and contributed fault definitions.
6. Publish actual results and limitations; map candidate vectors to a reviewed SEP draft.

Do not cite contributor registry/model statistics as fixture validation. The Oct 15
registry-sampling correction supersedes the earlier figures; raw methods/results require
review before reuse.

## Review questions

- Does the verifier expose a supported deterministic clock and keyring override at the pin?
  Keyring: yes. Clock: no SDK parameter; a worker-scoped test clock is proposed in
  adapter-contract.md as test instrumentation.
- Which outputs can Fabio independently report without inferring caller authorization?
  Proposed in adapter-contract.md (check statuses and the precise meaning of `signer_trust`).
- Should denied recovery reveal any existence/status information in the chosen fixture policy?
- What immutable admission evidence survives pruning, and how is reissuance bound to it?
- Which first subset can we complete before considering an upstream conformance proposal?
- A03 recovery policy, the outcome for the same verified decision under a fresh operation ID,
  and the cross-context decision-ID check (see adapter-contract.md, Open for review).

Sources:
[ndreno work split](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394#issuecomment-6150734000),
[process routing](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394#issuecomment-6151318313),
[ryjen authorization vectors](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394#issuecomment-6107356108),
[Fabio expiry vector](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394#issuecomment-6122699649),
[retention vector](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394#issuecomment-6130163272),
[Arjun retention clarification](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394#issuecomment-6127493861),
[contributor sampling correction](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394#issuecomment-6192645077),
[Majny's offered cases](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394#issuecomment-6250429570),
[ryjen's proposed A-E matrix](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394#issuecomment-6254309502).
