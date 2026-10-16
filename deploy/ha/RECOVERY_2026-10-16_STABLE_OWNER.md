# Stable-owner recovery, 16 October 2026

## Scope and observed state

This supersedes the deployment target in the 15 October instructions. There is
no new host installation output after those snapshots. Do not infer deployment
from a pushed commit or a passing software model. Do not rerun old installation
commands for `2115fce` after selecting this target.

The worktree is isolated from an unrelated local recording archive modification.
No production queue, credentials, installed configuration or SQL flag was edited
in this development session.

## Changes

- `auto_failback` is a validated boolean, default **false**. The exact-release
  installer explicitly writes false into the candidate config. A recovered
  higher-priority standby does not displace a healthy valid owner. Priority
  still selects replacements on owner failure/expiry; startup grace, fault
  exclusion, SQL epochs, handoff and fencing retain their checks. An explicit
  `auto_failback=true` opts into the former behavior after the stability delay.
- Authenticated `/status` exposes the effective local setting. Final acceptance
  requires false on all three exact-release nodes; an older elected controller
  can retain its older behavior until its own rollout is completed.
- The actual ECharts renderer draws visible muted directed routes and arrowheads
  for every structural edge, even without a healthy owner. Only observed healthy
  business routes are green. Missing/stale data remain unknown; real node faults
  remain red. Standby workers are allowed to remain stopped.
- The observer carries maintenance state to the topology. Historical rejected
  release counts remain visible as history without keeping the repair card in
  warning forever. Pending repair/trial still warns. The gateway location is
  corrected to ub22, `172.31.0.97:5051`. Mirror errors remain visible and are not
  declared repaired by a dashboard styling change.
- Final verification requires continuous successful polling for at least
  **120 seconds**, one unchanged owner/epoch shared by all nodes, completed owner
  probation, all five healthy owner workers, exact pinned/prepared/not-faulted
  nodes, fresh numeric queues and independent gateway readiness. A failed sample
  or identity change resets the interval. Divergent epochs cannot be accepted.
- Repeating the held phase for an already observed, pinned, held target with
  successful preflight does not reinstall it. Repeating finish on the installed
  target runs verification instead of reinstalling.

## Installation sequence

Use one full published SHA throughout, in separate clean recovery-tools worktrees.
Keep `/opt/perimeter/source` and `D:/PerimeterHA/source` untouched. The bootstrap
refresh is journaled and restored on installation failure as described in
`RECOVERY_2026-10-15_BUS_SAMPLING.md`.

1. Comparator: `recover_bus_rollout.py --release SHA --phase hold`.
2. Physical (administrator PowerShell): same command with `--phase hold`.
3. Comparator: same command with `--phase release`; wait for `PAIR_READY`.
4. Perimetr: same command with `--phase finish`; wait for `CORE_HA_ACCEPTED`.
5. ub22: from a clean checkout of the same SHA, run
   `sudo python3 -B deploy/ha/update_visual_wallboard.py --apply --release SHA`.
   This separately updates the observer and the two Grafana pages, validates
   backend queries, and uses its existing private backup/rollback path. It does
   not restart HA workers. A runtime rollout alone cannot update the wallboard.

`HELD_NODE_INSTALLED` means staged, **not** full deployment accepted. If a held
stage finds its local node active, it stops before mutation; send that output.
Never turn this into a manual stop/reset of the current owner. If any phase
fails, do not proceed to the next phase or substitute an older target.

The final `CORE_HA_ACCEPTED` output distinguishes live core readiness from mirror
copy completeness, physical tag/camera acceptance and long-term stability. SQL
mirror lock timeouts and controller SQL errors seen in earlier host logs require
post-install host evidence if they remain. Auto-update stays disabled during
this recovery; publishing the target does not enable an unattended rollout from
main. Planned handoffs during deployment are expected. Zero failovers under
real failures is neither a goal nor a guarantee.

## Development evidence

- Full regression suite: 725 discovered tests, 708 passed, 17 environment skips
  on this Linux development host, no failures.
- Installed qualifier: 132 Python files parsed; 6 adapter contracts; 9 controller
  model scenarios; 27 golden business traces; zero output differences against
  the previous published release. Models do not emulate SQL Server locks,
  firmware, physical power, native Windows Task Scheduler or live camera/RFID.
- New checks cover sticky ownership for both reserve owners, preserved failure
  handling, rejected divergent epochs, reset of the 120-second interval on a bad
  sample, repeated held-stage idempotence, historical quarantine and maintenance
  display, and execution of the shipped JS renderer in normal/failure/SQL-loss/
  stale scenarios. Synthetic normal/stale previews were rendered and inspected.
- Native Windows and Ubuntu CI must pass on the actual published SHA before
  issuing it as the install target. Host acceptance remains a separate step.
