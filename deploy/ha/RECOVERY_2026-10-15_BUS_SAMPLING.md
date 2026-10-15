# Recovery work on 15 October 2026

## Observed installation

Operator snapshots around 09:21–09:36 Moscow time establish:

- Physical: exact `7fc779e406c250b69b5fa34c6cab0e0d1d3bd5a6`, pinned,
  pending trial, repeatedly faulted/repaired.
- Comparator: same release and pin, pending trial, process running without
  native restarts. Counters include 116 failovers and 118 promotions since
  process startup, not independent experiments.
- Perimetr: **actually executing `1c7930912ad84e8f205cf16fbdd715c76c9eb74e`**
  from `/opt/perimeter/source`; healthy owner at epochs 6323 and 6335.
  Its durable record has `previous=null`, `pending=false`, but retains the
  newer release's qualification and operator pin and lists that release as
  rejected. That pin does not match the current SHA and cannot protect it.
  The state is consistent with rollback; the responsible launcher/process
  has not yet been identified from its exact rollback journal.
- Perimetr's local status lacks the new update/repair/lease fields because
  it executes the old code. Missing fields are not evidence of failed HTTP.
- Physical logs show actual delivery: 09:27:02–09:28:42 pending 70 -> 0,
  sent 74185 -> 74258; connection stays up. Other audit entries report
  `candidate_rfid_delivery_stalled`. These excerpts alone do not establish
  that the two observations concern the same time window or spool path.
- Comparator has `candidate_business_metrics_unavailable`; mirror reads
  repeatedly report SQL lock timeout 1222 for events. SQL controller errors
  only expose class names, insufficient to identify their precise cause.

Do not publish operator logs, credential bundles, actual tags or connection
strings. This summary contains aggregate counts and installation metadata.

## Reproduced software defects and changes

1. Queue sampling used to share a loop with mirror SQL and peer replication.
   This can delay samples. Publication after mirror I/O also assigned a new
   timestamp to counts read before that I/O.
2. Candidate probation assessed the same stale counts on every tick even when
   its freshness check failed. A stale cache could therefore manufacture a
   delivery-stall verdict while the worker actually delivered data.
3. Relative spool overrides were resolved against the Guardian cwd rather
   than the worker release root, allowing different files to be observed.
4. Passive candidates assessed business probation even though their workers
   were deliberately stopped, producing misleading candidate fault events.

Queue sampling now has its own supervised thread, read-only bounded SQLite
scans, paths resolved as worker paths, sample-start timestamps, and role/root
identity checks before publication. Missing/error samples are exposed as
`bus_sample_errors`, never zeros. Stale/incomplete evidence resets the stall
observation interval and cannot confirm a trial. Active candidates still fail
on a genuine fresh 120-second stall or missing metrics after the existing
240-second grace. Fencing, service readiness and operator pin semantics remain.

The regression workflow now also runs for pushes to the actual working branch.

## Preparing the replacement

`release_update.py --prepare --replace-pinned-trial` can qualify a replacement
when the existing pending trial is itself qualified and exactly operator-pinned.
It does not confirm/clear that trial, reset faults, stop services or modify SQL.
Unpinned and unqualified pending trials remain blocked. Existing receipt and
digest checks remain in force. A role/release change after preparation may
invalidate the receipt and requires preparation against the new actual state.

Preparation also reports `source_bootstrap_pin_guard_present` for boot.py in
the configured source checkout. This textual check is only a clue: it does
not prove which launcher the native service/task actually executes. Inspect
that binding before applying a pinned replacement. Updating a release alone
does not update a supervisor that still executes an older bootstrap file.

Next stage: prepare the same exact candidate on each node, inspect results,
then apply to passive nodes before handing off Perimetr. Do not overwrite
installed trees, delete queues, force SQL flags, or assume the old Perimetr
runtime has the current repair/update API. The existing installer's physical
and active-owner reserve gates must be respected or deliberately revised with
regression evidence before executing a mixed-version rolling update.

## Limits

Validation on the development Linux host: 701 discovered tests, 684 passed,
17 environment-dependent skips; no failures. The installed qualifier passed
6 adapter contracts, 8 HA model scenarios and 27 golden traces against a
separate checkout of pre-change `4b8b54b`, with zero business replay differences.
This includes 12 new regression tests (10 sampling/probation and 2 guarded
replacement-preparation checks). Windows, native systemd and real SQL Server
integration are not certified by this run.

This change repairs reproduced monitoring defects, not a certification that all
factory failures share that cause. SQL mirror contention, controller SQL errors,
the old supervisor binding, production spool paths and post-update stability
still require host evidence. Auto-update remains disabled until deployment is
accepted; no claim that the factory already executes this fix is made.
