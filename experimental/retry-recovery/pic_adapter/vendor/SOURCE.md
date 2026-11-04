# Vendored source: PIC signed refund approval example

`refund_approval_eb7cd467/` is an unmodified copy of the signed refund
approval example from the PIC Standard repository. It is vendored so the
approval adapter in this fixture can reuse the example's workflow-profile
checks at a fixed source revision without fetching the example source at
test time.

| Field | Value |
| --- | --- |
| Upstream repository | https://github.com/pic-standard/pic-standard |
| Upstream path | `examples/refund-approval/` |
| Upstream commit | `eb7cd4672addc02e1ea80a7a83fadf97d9b0a93e` (2026-10-12) |
| PIC SDK pin declared by the example | `330fdd817ef81d43461ea1787939e0d1cc87d589` |
| Licence | Apache License 2.0, copied unchanged as `refund_approval_eb7cd467/LICENSE` |
| Copyright | Copyright 2026 Fabio Marcello Salvadori, as stated in the upstream LICENSE |
| Modifications | None. The 16 example files and the upstream repository-root LICENSE are copied byte-for-byte |

## Integrity

`MANIFEST.json` records the sha256 of every vendored file. The vendor check
recomputes them and exits nonzero on any difference. `.gitattributes` marks
this directory `-text` so Git never normalises line endings.

Do not edit files under `refund_approval_eb7cd467/`. To move to another
upstream revision, re-vendor into a new directory named for that commit and
regenerate the manifest.

## Running the upstream checks

`test_cases.py` writes `actual-results.json`, and `self_check.py` temporarily
rewrites the expectations fixture and installed package metadata. Never run
them in place. Use `pic_adapter/run_vendor_baseline.py`, which copies this
directory to a disposable location and keeps generated output outside the
vendored tree.

## Key material

`keys/` contains the RFC 8032 section 7.1 test vector 1 Ed25519 key pair,
published upstream as test-only material under key_id `merchant-test-v1`.
It carries no real authority. See `refund_approval_eb7cd467/keys/README.md`.

## Scope

This copy is used only as test material for the retry-recovery fixture. It
does not change PIC, does not define MCP behaviour, and its presence here is
not an endorsement of any MCP wire format.
