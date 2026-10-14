# Operator-pinned exact release — 2026-10-14

The prior factory workload candidate `3e86b67698d43b7342bdf160a6d462fa2e87966c` can roll back automatically on probation fault. `auto_update=false` is not a pin. Because this safeguard lives inside Guardian, the rollout target must be a **new qualified commit that contains the safeguard**; the older SHA cannot gain it by setting a value in release.json.

## Explicit, opt-in pin

Run the existing `release_update.py --prepare --release <NEW_EXACT_SHA>` first. For each passive Linux receiver, use `--apply --recover-reserve --hold-maintenance --pin-release --release <NEW_EXACT_SHA>`. After two receivers pass, use ordinary `--release-reserves --release <NEW_EXACT_SHA>`. Physical uses `--apply --handoff --pin-release --release <NEW_EXACT_SHA>` **only after** its existing two-reserve/lease/handoff qualification gates pass.

Installer stores `operator_pinned_release=<NEW_EXACT_SHA>` only after native stop and exact candidate verification. Its private journal retains old records/config for interrupted installation recovery. The pin is durable across Guardian restarts. If a future explicitly qualified rollout does not request `--pin-release`, its installer clears the old pin.

With an exact matching pin, a probation failure keeps the installed code identity instead of reverting to the previous SHA; it **does not** mark the runtime healthy, successful, prepared, or accepted. The ordinary SQL protocol-2 fencing, single-owner election, demotion, business stall checks, native supervisor and operator-maintenance rules remain active. A broken pinned node can remain faulted and out of service while the exact code stays on disk. An explicit /repair rollback_release is refused for a pinned SHA (the LLM repair catalogue also cannot override it). This is a release identity policy, **not** a health-check override, and does not promise availability when every node is faulty.

Do not manually modify `release.json`, SQL lease/fault records, locks, the journal, or pending queues. Do not run `--pin-release` against the old `3e86b676...` runtime; that code does not implement the pin. Monitor `/status.update.operator_pinned` along with current SHA, active/healthy/faulted, preflight, pending and five service statuses. Pinned pending remains pending until ordinary independent probation succeeds; no shortcut confirmation was added.

Production receipts, live validation, independent ub22, week trial, and business acceptance are still outstanding. `main` remains untouched until validated adoption.
