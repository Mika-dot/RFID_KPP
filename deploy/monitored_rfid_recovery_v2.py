#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RFID continuous recovery transport-health correction.

The underlying recovery adapter owns the real SDK TCP lifecycle.  A successful
UHF_GetReceived_EX call is also proof that the established SDK session is alive,
so refresh rfid_tcp on every returned receive call.  This prevents a healthy
long-lived connection from becoming falsely stale 45 seconds after TCPConnect.
"""
from __future__ import annotations

import deploy.monitored_rfid_recovery as base


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
    # base.main() reads this module global when wiring the legacy adapter, so
    # replace only the transport-health proxy and keep all recovery/spool logic.
    base._LibraryProxy = _LibraryProxy
    return int(base.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
