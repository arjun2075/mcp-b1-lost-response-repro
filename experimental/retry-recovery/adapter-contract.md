# Proposed signed-approval adapter contract

Status: proposal for Fabio/Arjun review, not an implemented API or MCP wire format.
This file is a fixture-local integration contract. It does not add fields to PIC or MCP.

## Responsibilities

The adapter verifies the signed proposal and the refund workflow profile, but **does not
dispatch a refund**. The recovery harness owns stable operation mapping, duplicate
suppression, synthetic provider calls, current caller authorization and receipt disclosure.

The pinned example's `integration.verify_and_dispatch` performs verification AND calls its
tool sink. Do not use it unmodified on a recovery lookup path. Fabio can expose a
non-dispatching verification entry point while retaining its profile checks, or propose
an alternative split here. Calling a recording sink is still dispatch for the harness;
a recovery lookup must not invoke it.

## Proposed request

Use JSON-compatible objects for a potential subprocess boundary. No shared Python dependency
environment is assumed. In-process integration can use the same contract.

| Input | Meaning / trust boundary |
| --- | --- |
| `proposal` | Complete signed PIC proposal. Untrusted until verified |
| `expected_action` | Harness-configured refund tool, impact and semantic arguments; compare against verified payload |
| `verification_context.keyring_fixture` | Fixture-owned allowlisted keyring/config reference, never a client-selected file path |
| `verification_context.clock` | Harness-owned test time; exact SDK injection mechanism to be agreed |
| `verification_context.profile` | Named fixture profile requiring canonical attestation, expiry and refund-claim shape |

Authenticated tenant/principal identity comes from the harness session fixture, not the
signed merchant claim. It is not something PIC verification establishes in this example.
Expected semantic request excludes JSON-RPC and harness attempt IDs.

Original example source:
`eb7cd4672addc02e1ea80a7a83fadf97d9b0a93e`.
PIC SDK source dependency:
`330fdd817ef81d43461ea1787939e0d1cc87d589`.
Keep `constraints.txt` from the example baseline. Record deviations explicitly.

## Proposed response

```json
{
  "adapter_contract": "retry-recovery-draft-1",
  "decision": "allow",
  "checks": {
    "signature": "pass",
    "proposal_binding": "pass",
    "signer_trust": "pass",
    "freshness": "pass",
    "workflow_profile": "pass"
  },
  "verified_claims": {
    "decision_id": "decision-fixture-1",
    "approval_ref": "approval-fixture-1"
  },
  "verified_action": {
    "tool": "refund",
    "impact": "money",
    "args": {
      "payment_id": "payment-fixture-1",
      "amount_minor": 100,
      "currency": "EUR"
    }
  },
  "reason_code": null,
  "evidence": {
    "example_commit": "eb7cd4672addc02e1ea80a7a83fadf97d9b0a93e",
    "pic_sdk_commit": "330fdd817ef81d43461ea1787939e0d1cc87d589"
  }
}
```

- `decision`: `allow`, `deny`, or `error`. Allow means this adapter's approval/profile checks
  passed, not that a new effect or historical-result disclosure is authorized.
- Each check: `pass`, `fail`, `not_evaluated`, or `unavailable`. Report only checks actually
  established by the SDK/wrapper. A short-circuit failure does not imply all other checks
  passed. Do not infer "signature valid" from "expired" unless independently established.
- `verified_claims` and `verified_action` are populated only on overall allow in this initial
  contract; otherwise null. This avoids presenting rejected/unverified content as authority.
  A later separately typed cryptographic observation can be discussed if needed.
- `reason_code`: fixture-local normalized code with original SDK diagnostic retained in
  sanitized trace. Candidate categories: signature/binding rejection, untrusted or revoked
  signer, expired approval, profile rejection, verifier unavailable. Agree actual mapping
  against pinned APIs; do not invent PIC error codes.
- Adapter errors/timeouts deny new dispatch and do not erase historical recognition.
- No private keys, complete keyrings or real receipts in output.

The toy fixture may retain the original verified decision-to-operation mapping in a durable
test store. On expiry/revocation it must recover by separately authenticated, previously
bound identity, not by trusting a `decision_id` extracted from the newly rejected proposal.

## Separate harness decisions

| Decision | Owner | Effect of negative result |
| --- | --- | --- |
| Authenticated caller/tenant and original-operation binding | Harness | No cross-context receipt access |
| Duplicate recognition and semantic-request match | Harness/store | Conflict or unknown; no accidental new dispatch |
| Current signed-approval verification/profile | Fabio adapter | Deny execution under that approval |
| Current caller authorization for new execution | Harness policy fixture | Deny new effect |
| Current permission to disclose historical result | Harness policy fixture | No receipt disclosure; no receipt in errors |
| Actual provider outcome | Synthetic effect oracle / optional lookup adapter | Do not infer from authorization or empty lookup |

## Minimum adapter acceptance checks (planned)

- Valid pinned refund proposal verifies without invoking any effect sink.
- Modified tool/impact/arguments or signed claim fails the relevant checks.
- Missing required expiry is rejected by the workflow profile.
- Expired attestation is rejected through real pinned freshness verification.
- Revoked signer key differs from withdrawing caller permission in the harness.
- Reissued signed approval can have a changed approval reference while the harness preserves
  the original operation mapping; changed semantic action is rejected as a conflicting retry.
- Unavailable verifier, malformed input or unexpected exceptions cannot dispatch.
- Verify both upstream example self-checks and integrated behavior before reporting results.

Open for review: supported keyring/time injection, minimal check granularity, deterministic
case fixtures, error mapping and whether separate environments are necessary. No combined
PIC/MCP install, implementation or test result is claimed by this document.
