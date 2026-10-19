# Signed-approval adapter contract (retry-recovery-draft-1)

Status: implemented draft for Fabio/Arjun review. This is a fixture-local
integration contract, not an adopted MCP wire format and not a PIC API. It adds
no fields to PIC or MCP. The provider, recovery store and caller authorization
in this fixture remain synthetic.

## Responsibilities

The adapter verifies a signed PIC proposal and the refund workflow profile, and
compares the verified action with the action the harness expects. It **never
dispatches**: it receives no tool, sink or callback. The recovery harness owns
stable operation mapping, duplicate suppression, synthetic provider calls,
current caller authorization and receipt disclosure.

The pinned example's `integration.verify_and_dispatch` verifies and then calls
its tool sink, so it is not used. `pic_adapter/verify.py` reuses that example's
profile helpers in the same order as its steps 1 to 6 and replaces recording
and dispatch (steps 7 to 9) with the expected-action comparison.

## Pins

| Component | Pin |
| --- | --- |
| Refund example (vendored, unmodified) | `eb7cd4672addc02e1ea80a7a83fadf97d9b0a93e`, see `pic_adapter/vendor/SOURCE.md` |
| PIC SDK | `pic-standard[crypto]` at `330fdd817ef81d43461ea1787939e0d1cc87d589`, installed with the example's `constraints.txt` |
| MCP SDK | `mcp==2.2.0` (unchanged) |

The installed SDK commit is read from pip's `direct_url.json` and compared with
the pin. A pin mismatch prevents worker startup; the client reports this as
`verifier_unavailable`. A direct verifier call detecting a mismatch returns
`sdk_pin_mismatch`. PIC's `[mcp]` extra is not installed (it requires
`mcp<2`). Setup steps are in `README.md` (PIC approval setup).

## Process architecture

* `pic_adapter/worker.py` runs as a separate long-lived subprocess. Only that
  process puts the vendored example on `sys.path` and enables the test clock.
  Before reporting ready it verifies the vendor manifest, checks that the four
  keyring fixture files are present, verifies the SDK pin and confirms a clean
  clock. It answers one JSON line per request, in order, echoing a
  `request_id`.
* `pic_adapter/client.py` is the async (anyio) client used by the harness. It
  serialises requests, applies one timeout to sending and receiving, validates
  every reply against this contract, and on timeout, crash, mismatched or
  malformed reply terminates the worker and starts a fresh one on the next
  call. Cancellation discards the worker and propagates. Worker failures
  become `decision: "error"` with `reason_code: "verifier_unavailable"`.
* The harness process never imports the vendored modules by their top-level
  names and never patches the clock.

## Request

| Field | Meaning / trust boundary |
| --- | --- |
| `proposal` | Complete signed PIC proposal. Untrusted until verified. |
| `expected_action` | Exactly `tool`, `impact`, `args`, built by the harness from the actual tool-call arguments. |
| `verification_context.keyring_fixture` | One of `trusted`, `revoked`, `empty`, `expiring`, resolved to files under `pic_adapter/keyrings/`. Never a caller-supplied path. |
| `verification_context.profile` | Must be `refund-approval-v1`. |
| `verification_context.clock` | Optional test clock, see below. |

Unknown keys in the top-level request, `expected_action`, or
`verification_context` are rejected as `malformed_input`. Proposal validation
is delegated to PIC and the refund profile. Authenticated tenant and principal
come from the harness session fixture, not from the signed merchant claim; PIC
verification does not establish them in this example.

## Response

```json
{
  "adapter_contract": "retry-recovery-draft-1",
  "decision": "allow",
  "checks": {
    "signature": "pass",
    "proposal_binding": "pass",
    "signer_trust": "pass",
    "freshness": "pass",
    "workflow_profile": "pass",
    "expected_action": "pass"
  },
  "verified_claims": {"decision_id": "decision-001", "approval_ref": "approval-001"},
  "verified_action": {
    "tool": "refund",
    "impact": "money",
    "args": {"payment_id": "pay_A", "amount_minor": 4000, "currency": "EUR"}
  },
  "reason_code": null,
  "trace": [],
  "evidence": {
    "example_commit": "eb7cd4672addc02e1ea80a7a83fadf97d9b0a93e",
    "pic_sdk_commit": "330fdd817ef81d43461ea1787939e0d1cc87d589",
    "installed_pic_sdk_commit": "330fdd817ef81d43461ea1787939e0d1cc87d589"
  }
}
```

* `decision`: `allow`, `deny` or `error`. `allow` means this adapter's approval,
  profile and expected-action checks passed. It does not authorize a new effect
  or a historical-result disclosure; the harness decides both.
* `verified_claims` and `verified_action` are populated only on `allow`;
  otherwise `null`.
* `reason_code` is `null` on `allow` and one of the codes below otherwise.
* `trace` contains fixed explanatory text, PIC error codes and, for unexpected
  adapter exceptions, the exception type name. Raw exception, SDK and profile
  messages are not returned (see Diagnostics).
* `evidence`: worker-produced responses carry the verified installed pin.
  Client-generated errors have `installed_pic_sdk_commit: null`, because no
  verified worker result was obtained.
* `checks.expected_action` is an **extension** of the originally proposed
  response, added during implementation and open for review.

### Check statuses

| Status | Meaning |
| --- | --- |
| `pass` | Established by the pipeline result or an independent observation. |
| `fail` | Established as failed. |
| `not_evaluated` | Not performed: skipped after an earlier rejection, or not applicable because its input was absent. |
| `unavailable` | The pipeline did not expose that stage's result. |

* `signature`, `proposal_binding`: `pass` only when the pipeline succeeds and
  the canonical evidence entry is among the verified signature evidence.
  `proposal_binding` is the signed-proposal binding, distinct from
  `expected_action`.
* `freshness`: `pass` only when that entry verified **and** the profile-required
  `expires_at` was present. The SDK enforces expiry only when present.
* `signer_trust`: the configured keyring status of the canonical evidence
  entry's `key_id` (not its descriptive `signer` field), observed independently
  with `TrustedKeyRing.key_status`. It does **not** establish that the proposal
  was signed by that key.
* On pipeline failure, signature, binding and freshness are `unavailable`
  unless a pinned, tested diagnostic identifies the failed stage, and the
  ordering of SDK checks is never used to infer that earlier stages passed.
* Authorization depends only on the pipeline result, the profile checks and the
  expected-action comparison, never on diagnostic text.

### Reason codes (fixture-local, not PIC error codes)

| Code | Decision | Meaning |
| --- | --- | --- |
| `approval_expired` | deny | Attestation expiry check failed at the verification instant. |
| `signature_or_binding_rejected` | deny | Signature or signed-proposal binding check failed. |
| `signer_revoked` | deny | Canonical evidence `key_id` is revoked in the configured keyring. |
| `signer_untrusted` | deny | Canonical evidence `key_id` is missing or expired in the configured keyring. |
| `evidence_rejected_unclassified` | deny | Evidence failed; stage not identified (including an ambiguous evidence id). |
| `profile_rejected` | deny | Outside the refund-approval workflow profile. |
| `action_mismatch` | deny | Verified action differs from `expected_action`. |
| `proposal_rejected` | deny | PIC rejected the proposal outside the classified evidence stages, e.g. `PIC_VERIFIER_FAILED`. *Added in implementation.* |
| `verification_incomplete` | deny | Defensive: all gates passed but not every check was established. *Added in implementation.* |
| `malformed_input` | error | Request envelope is not well formed (also client-side, for unserialisable or oversize requests). |
| `sdk_pin_mismatch` | error | Direct verifier call: installed SDK commit differs from the pin. |
| `clock_rejected` | error | Test clock invalid or not permitted in this process. *Added in implementation.* |
| `verifier_error` | error | `PIC_INTERNAL_ERROR` or an unexpected adapter exception. |
| `verifier_unavailable` | error | Produced by the client: no worker result (startup refused, timeout, crash, protocol failure). All checks `unavailable`. *Added in implementation.* |

## Diagnostics

SDK messages are matched internally against pinned patterns from
`pic_standard/evidence.py` at the SDK pin, only for the canonical evidence
entry and only when its `id` is unique in the proposal. They select a reason
code and mark a failed stage; each pattern is exercised by a test. They are
never returned: caller-controlled values (arguments, claim text, key ids,
profile names, clock strings) cannot be echoed back through `trace`.

## Clock and keyring controls at the SDK pin

* **Keyring:** supported. `PipelineOptions.key_resolver` is used through the
  example's `make_pipeline_options(keyring_path=...)`, with the allowlisted
  fixtures above.
* **Clock:** the pinned SDK has no clock parameter. It reads
  `datetime.now(timezone.utc)` in `pic_standard.evidence` (attestation expiry,
  rejected when `expires_at < now`) and `pic_standard.keyring` (key expiry,
  rejected when `now >= expires_at`).
  `pic_adapter/clock.py` replaces only those modules' `datetime` name for one
  verification, inside the worker, and restores it afterwards. Signature,
  binding and freshness code run unchanged with their own boundaries. This is
  **test instrumentation, not a PIC clock API**.
* **Clock input:** a whole-second RFC 3339 timestamp with uppercase `T` and
  either `Z` or an explicit numeric offset, for example
  `2099-01-01T00:00:01Z`. Fractional seconds are deliberately unsupported by
  this fixture. A supplied clock outside the worker, a naive or malformed
  clock, or a nested patch is rejected.

## Harness integration (this fixture's recovery engine)

The harness verifies a presented approval, places the adapter response in its
own `Context`, and calls the synchronous engine with no intervening `await`.
`Engine(require_approval=True)` then applies these fixture rules:

* **New effect:** requires an `allow` response whose verified refund action
  equals the call's arguments, plus current execution permission.
* **Reservation before dispatch:** the operation record and a scoped mapping
  `(tenant, principal, tool, decision_id) -> operation_id` are written before
  the provider is called, and survive a post-commit error or unknown outcome.
* **Retained recovery:** fresh approval is not required; authenticated
  operation identity, matching arguments and current disclosure permission
  suffice. A presented, currently verified decision is checked for consistency
  with the retained one; a different verified decision is a `conflict`.
  Recovery currently ignores approval freshness and signer status.
* **Disclosure first:** in approval mode, denied disclosure returns a generic
  `denied` before conflicts or unknown outcomes are distinguished.
* **Same verified decision, fresh operation id:** no new effect. *Provisional:*
  the engine returns `conflict`; returning the original outcome is open.
* **Cross-context:** another tenant or principal cannot recover the operation
  or reuse its decision, and no receipt is returned. These are fixture rules,
  **not existence hiding**: behaviour may differ depending on whether an
  identifier exists elsewhere. The cross-context decision-id check is
  *proposed for review*; scoped identifiers are not automatically globally
  unique.

This association is a rule of this signed-refund fixture. It does not
prescribe MCP operation identity, require PIC approval fields in an SEP, or
constrain how other profiles authorize operations.

## Separate harness decisions

| Decision | Owner | Effect of negative result |
| --- | --- | --- |
| Authenticated caller/tenant and original-operation binding | Harness | No cross-context receipt access |
| Duplicate recognition and semantic-request match | Harness/store | Conflict or unknown; no accidental new dispatch |
| Current signed-approval verification/profile | Adapter | Deny execution under that approval |
| Current caller authorization for new execution | Harness policy fixture | Deny new effect |
| Current permission to disclose historical result | Harness policy fixture | No receipt disclosure; no receipt in errors |
| Actual provider outcome | Synthetic effect oracle | Not inferred from authorization or empty lookup |

## Acceptance checks

| Planned check | Where exercised |
| --- | --- |
| Valid proposal verifies without invoking any effect sink | `test_pic_adapter.py` (dispatch spy inside a real worker start) |
| Modified tool, impact, arguments, claim, intent, provenance or signature fails the relevant check | `test_pic_adapter.py` |
| Missing expiry rejected by the profile | `test_pic_adapter.py` |
| Expiry rejected through real pinned freshness, at a controlled instant | `test_pic_adapter.py`; MCP scenarios A01, A02 |
| Revoked signer key differs from withdrawn caller permission | `test_pic_adapter.py`; MCP scenarios A04 and new effect under revoked signer |
| Reissued approval recovers the original operation; a different decision conflicts | MCP scenarios A07 and its negative control |
| Unavailable verifier, malformed input or exceptions cannot dispatch | `test_pic_adapter.py` (client and in-worker tests) |
| Upstream example self-checks still pass | `pic_adapter/run_vendor_baseline.py` |

## Open for review

1. A03: whether retained recovery after signer revocation should keep the
   current behaviour (disclose under independent permission) or refuse.
2. Same verified decision under a fresh operation id: `conflict` (current) or
   the original operation's outcome.
3. The cross-context decision-id check, and whether denied recovery may
   reveal any existence or status information.
4. The added reason codes and the `checks.expected_action` extension.

## Not claimed

No exactly-once guarantee for a remote payment, no durability, restart or
concurrency safety, no pruning or retention behaviour (T01 to T03 not
executed), no adopted MCP semantics or merged conformance test, and no change
to PIC.
