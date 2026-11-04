# Retention and non-authoritative readback increment

This is a separate increment on PR #4 at
`f01ccb479399798e68a2c4d4ad1ad0ef69e6e1d3`. It does not merge that PR or modify
its verifier, worker, vendored example, original engine, or original runner.
All statuses and policies below are fixture-local proposals, not MCP requirements.

## Independent PR #4 verification

Executed the unmodified PR head on Python **3.12.14** (the contributor used
3.11.9), `mcp==2.2.0`, `mcp-types==2.2.0`, and installed PIC revision
`330fdd817ef81d43461ea1787939e0d1cc87d589`. The vendored example remains at
`eb7cd4672addc02e1ea80a7a83fadf97d9b0a93e`.

- 113 unit tests passed.
- 10 original MCP scenarios and 19 real-PIC approval scenarios passed.
- One verification worker, 37 verifications, no worker replacement or cleanup error.
- Vendored baseline: 22 cases, 2 integrity probes and 7 self-checks passed;
  the self-check used a separate environment, restored its metadata, and the
  vendor manifest remained intact.
- The unchanged root reproduction and positive control produced 2 and 1 effects,
  respectively, with response-drop faults and fresh request IDs.

Raw records are in `evidence/2026-10-29/`. This verifies the tested paths; it is
not a security audit. A03 recovery after signer revocation remains a candidate
policy grounded in retained authenticated binding and current disclosure
permission. Decision/operation binding rules and adapter reason codes remain
provisional. The adapter clock patches two pinned SDK modules in the worker;
it is test instrumentation, not an SDK clock API.

## Executed retention policy

`RetentionEngine` extends the synthetic engine without changing `recovery.py`.
The controlled retention clock starts at zero, corresponding to
`2026-10-29T00:00:00Z`. Real PIC verifications use the corresponding instant
through the existing worker clock. The original approval expires at day 7;
the reissued approval has a new approval reference, the same decision and
action, and expires at day 14. The expired control expires at day 1.
PIC's pinned expiry comparison allows the exact expiry instant; admission
expiry at day 7 is nevertheless independently enforced by the harness.

| Transition | Surviving admission evidence | Result for the original operation |
|---|---|---|
| Before 24h | Scoped record with receipt | Original receipt, if disclosure permitted |
| At/after 24h, before day 7 | Scoped tombstone, semantic args and decision mapping; no receipt | Unknown; no redispatch |
| At/after day 7 | Global admission-closed flag; expired operation records/mapping removed | Unknown/refused; no new admission |

The effect oracle is never pruned. A tombstone includes tenant, principal, tool
and operation scope. Caller timestamps do not determine admission time. A new
approval reference cannot refresh server-owned admission time or replace the
operation identity. A different verified decision on the same retained operation
conflicts under this candidate policy.

After recognition evidence expires, this deliberately conservative profile
refuses **all unrecognized identities**, including genuinely new operations.
This availability cost is explicit. The global refusal flag is retained; this
does not show reliable duplicate suppression after complete loss of all state.
A production service would need an independently trusted admission mechanism
or another documented restriction before safely reopening admission.

While the original tombstone survives, T04 demonstrates that a new operation
with a separately verified decision can still execute. A denied disclosure or
changed authenticated principal returns no receipt. The runner checks all
delivered JSON-RPC text, structured and error fields for receipt-marker leakage
in scenarios with no disclosed completion.

The 14 real-MCP/real-PIC retention cases exercise T01, T02, T03, the T04 control,
approval expiry, receipt/admission horizon boundaries, denied disclosure and
changed principal. Each attempt records verification checks, store state,
effect count, dispatch observation and client-visible outcome separately.
Nine additional policy unit tests cover the transition boundaries and retained
result behavior after admission closes.

Pruning, lookup and reservation are synchronous with no intervening `await` in
the engine. That only establishes sequential ordering in this single-process
fixture. It does not demonstrate thread, replica, crash or restart safety.

## Mapping terminalstate's external trace

Source: [terminalstate/double-take at 3bfd494](https://github.com/terminalstate/double-take/tree/3bfd494293e70cb5a4eb068e005d76739fd760aa).
The concrete source record is line 142 of
[`runs-l2/run.jsonl`](https://github.com/terminalstate/double-take/blob/3bfd494293e70cb5a4eb068e005d76739fd760aa/runs-l2/run.jsonl):
`paypal_refund`, `claude-sonnet-5`, `plain`, trial `2`.

The source record shows a refund followed by a timeout, a transaction search
by capture ID, and a repeated refund. The search returns `found: true` for
the capture (`T0006`); it does not return the separate refund transaction.
The absence of a refund in that response is not authoritative evidence that
the refund did not execute. This is a correlation/scope error, not a literal
`found: false` response.

Our adaptation is intentionally smaller: a synthetic refund commits, returns
an explicit execution error with unknown outcome, a synthetic capture-only
search returns successfully, then the scripted client retries with the same
logical-operation ID and fresh JSON-RPC IDs. It adapts the source's timeout
into the existing commit-then-error fault and adds a pruning variant. It does
not execute PayPal code, reproduce provider semantics or rerun the source model.

| New case | Recovery evidence | Effects | Final outcome |
|---|---|---|---|
| `negative_read_retained_unknown` | Unresolved reservation | 1 | Unknown |
| `negative_read_pruned_receipt` | Tombstone after 24h | 1 | Unknown |
| `negative_read_unsafe_eviction_control` | Reservation discarded after error | 2 | Second effect completed |

The search never updates the admission store or establishes non-execution.
The two safe profiles block redispatch through the retained operation binding;
the unsafe control exposes the duplicate. These cases do not infer intent from
equal arguments, and do not establish protection if a caller invents a new
operation identity while admission remains open.

The external project's five local adapter exactness tests also passed (79
recorded answers compared). This checks its adapters against its recordings;
it does not independently validate the providers or the reported 600 model
trials. Their 70 duplicate simulated effects remain contributor-reported.
No real funds, credentials, customer accounts or payment providers were used.

## Reproduce

Use the existing PIC approval setup in README.md, including its constraints
and the separate environment for the upstream self-check. From this directory,
where `$PY` is the absolute path to that MCP/PIC environment's Python:

```bash
"$PY" -m unittest -v test_recovery test_pic_adapter test_run_vendor_baseline test_run_cases test_retention
"$PY" run_retention_cases.py --output local-results/retention-results.json
```

Create `local-results/` first if absent. The new runner refuses optimized Python
because its scenario validations use assertions. It uses real MCP memory
streams and the existing 2s read / 1.5s verification timeouts. Unexpected
timeouts fail; only a recorded after-commit drop qualifies as the injected fault.

The original `run_cases.py` and its committed `results.json` are intentionally
unchanged. Their pending labels describe that runner, not this separate
increment. Retention evidence is `evidence/2026-10-29/retention-results.json`.
