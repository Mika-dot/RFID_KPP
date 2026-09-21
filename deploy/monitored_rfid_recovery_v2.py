#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RFID continuous recovery hardening layer.

This layer deliberately changes only two things on top of the proven recovery
adapter:

1. A successful UHF_GetReceived_EX return refreshes the real SDK transport
   dependency, so a healthy long-lived reader connection cannot become falsely
   stale just because TCPConnect happened more than 45 seconds ago.
2. Semantic recovery is bounded-rate and non-thrashing.  The first recoveries
   are connection/session resets, one controlled Python/DLL recycle is allowed
   at a configured escalation attempt, and later retries are session-only with
   exponential backoff capped at a long interval.

The physical reader command sequence is otherwise left unchanged.
"""
from __future__ import annotations

import os

import deploy.monitored_rfid_recovery as base


class _BusinessFlowWatchdog(base._BusinessFlowWatchdog):
    """Continuous recovery without repeated process-recycle storms."""

    def __init__(self, reporter, connection_string: str) -> None:
        super().__init__(reporter, connection_string)
        self.recovery_backoff_max_sec = max(
            self.recovery_backoff_sec,
            float(os.getenv("RFID_BUSINESS_RECOVERY_BACKOFF_MAX_SEC", "1800")),
        )
        self.process_recycle_attempt = max(
            0,
            int(os.getenv("RFID_BUSINESS_PROCESS_RECYCLE_ATTEMPT", "3")),
        )

    def _recovery_delay(self) -> float:
        attempts = int(self.state.restart_attempts)
        if attempts < int(self.max_restarts):
            return self.self_heal_sec

        # After the fast recovery phase, increase spacing 5m -> 10m -> 20m ->
        # 30m (default cap).  Recovery continues forever, but cannot keep
        # interrupting a busy checkpoint every few minutes.
        exponent = max(0, attempts - int(self.max_restarts))
        exponent = min(exponent, 8)
        delay = self.recovery_backoff_sec * (2 ** exponent)
        return min(self.recovery_backoff_max_sec, delay)

    def _request_recovery(self, detail: str) -> None:
        next_attempt = int(self.state.restart_attempts) + 1
        mode = (
            "process"
            if self.process_recycle_attempt > 0
            and next_attempt == self.process_recycle_attempt
            else "session"
        )

        if not base._RECOVERY.request(
            attempt=next_attempt,
            reason=detail,
            mode=mode,
        ):
            return

        attempt = self.state.register_restart()
        self.meta.record(attempt, mode)
        self.reporter.set_metric("self_heal_restarts", attempt)
        self.reporter.set_metric("recovery_request_pending", True)
        self.reporter.set_metric("recovery_last_mode", mode)
        self.reporter.set_metric("recovery_last_attempt", attempt)
        self.reporter.capture_exception(
            RuntimeError(
                f"RfidBusinessFlowControlledRecovery:{mode}:{attempt}:{detail}"
            )
        )
        base.flush_sentry(2.0)
        base.log.error(
            "RFID semantic failure: requested %s recovery attempt=%s detail=%s",
            mode,
            attempt,
            detail,
        )


class _LibraryProxy(base._LibraryProxy):
    """Refresh transport liveness from the actual SDK receive path."""

    def UHF_GetReceived_EX(self, *args):
        rc = super().UHF_GetReceived_EX(*args)
        # Reaching here means the native receive call returned normally and no
        # controlled recovery exception was raised.  This is stronger evidence
        # of the active SDK session than opening a competing second TCP socket.
        self._reporter.touch_dependency("rfid_tcp")
        return rc


def main() -> int:
    # base.main() resolves these globals when wiring the legacy adapter.  Replace
    # only policy/transport-health classes; keep the proven reader/spool path.
    base._BusinessFlowWatchdog = _BusinessFlowWatchdog
    base._LibraryProxy = _LibraryProxy
    return int(base.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
