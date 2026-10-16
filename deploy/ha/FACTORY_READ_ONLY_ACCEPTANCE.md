# Factory read-only acceptance for a pinned HA release

This procedure checks **code deployment**, **live HA ownership**, **SQL progress**, and **Web visibility** separately. It is intentionally valid when a passive node is faulted or the release is still in probation. It never mutates leases, journals, queues, service state or SQL rows, never synthesizes a tag reading, and does not replace physical passage testing.

## Prerequisites

Run from the Physical Windows administrator PowerShell, with the exact currently installed native Python, the existing private `node.json` and `environment.local.json`. Download the current feature-branch commit to a *separate* clean worktree. Do not replace installed release files or reset a dirty stable checkout. Leave the three pinned `7fc779e406c250b69b5fa34c6cab0e0d1d3bd5a6` runtimes as they are. 

```powershell
$ErrorActionPreference = 'Stop'
$REL = '7fc779e406c250b69b5fa34c6cab0e0d1d3bd5a6'
$Node = Get-Content 'D:\PerimeterHA\node.json' -Raw -Encoding UTF8 | ConvertFrom-Json
$SOURCE = [string]$Node.update_source
git -C $SOURCE fetch --no-tags origin feature/adaptive-correlation-fallback-2026-10-08
if ($LASTEXITCODE -ne 0) { throw 'Fetch failed' }
$VERIFY = Join-Path $env:TEMP ('perimeter-acceptance-' + [guid]::NewGuid().ToString('N'))
git -C $SOURCE worktree add --detach $VERIFY FETCH_HEAD
if ($LASTEXITCODE -ne 0) { throw 'Checkout failed' }
$CHECKER = Join-Path $VERIFY 'deploy\ha\verify_factory_acceptance.py'
$COMMON = @('--nodes-config', 'D:\PerimeterHA\node.json',
            '--environment', 'D:\PerimeterHA\transfer-private\environment.local.json',
            '--release', $REL)
& $Node.python -B $CHECKER @COMMON --snapshot
```

`--snapshot`: JSON fields `deployment.passed`, `cluster.all_nodes_pinned`, `cluster.healthy_owner`, `sql.lease_valid`, `web.health_http_ok`, `web.events_http_ok`, `endpoints_ready`. Each node must have exact pinned release and matching epoch; exactly one healthy active owner and five ready workers on that owner. A faulted *passive* node is reported but is not a code-version deployment failure. Its recovery remains an outstanding resilience item. The checker reads the authoritative SQL lease and controller and calls Web `/api/health` and `/api/events`; 401, timeouts or malformed data cannot be counted as success.

## Evidence from a real reel passage

Capture a baseline **before** a normal physical passage, then run the check afterwards. Paths are local to the Physical node; no credential strings or tag identity are written to the baseline.

```powershell
$BASE = 'D:\PerimeterHA\rfid-factory-baseline-' + (Get-Date -Format yyyyMMdd-HHmmss) + '.json'
& $Node.python -B $CHECKER @COMMON --baseline $BASE
# A real tagged reel must pass the reader. Wait for normal SQL and Aggregator processing.
& $Node.python -B $CHECKER @COMMON --check $BASE
```

Expected evidence after real traffic: `business_delta.rfid_sql_aggregator_observed=true` when new SQL RFID rows appear and Aggregator cursor catches up; `business_delta.event_web_progress_observed=true` when a new SQL reel business event and an increased Web event count are observed, and Web `/api/event/<id>` returns the **same event ID and RFID raw-read ID** as SQL. `business_observed=true` requires both, and correlates a new RFID raw-read ID with the same displayed event; it does **not** independently prove the physical identity of the reel, direction correctness, camera capture, warehouse correctness or 1C reconciliation. These remain separate acceptance tests requiring a deliberately chosen reel with known identifiers and expected movement.

Exit code 0 means endpoints ready (and in `--check`, both progress criteria observed); 2 means readiness evidence unavailable; 3 means live business progress was not observed, **not** that the deployment has rolled back or that an idle checkpoint is faulty. Any failover or temporary outage can make a single sample inconclusive: retake `--snapshot` without mutating HA or resetting SQL. A physical event must never be invented merely to force a pass.

Production acceptance must additionally verify real tag -> SQL UUID -> session/reel identity -> movement direction -> Yolo video -> warehouse record -> Web entry, plus quantitative 1C one-to-one reconciliation. These cannot be certified solely from a generic GET health endpoint.
