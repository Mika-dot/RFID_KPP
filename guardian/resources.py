"""Observe the agent's own handles before it loses SQL, HTTP and audit access."""
from __future__ import annotations

import errno
import os
import sys

import psutil


EXHAUSTED_ERRNOS = frozenset((errno.EMFILE, errno.ENFILE))


class ResourceGuard:
    def __init__(self):
        self.process = None if sys.platform == "linux" and os.name != "nt" else psutil.Process()

    def sample(self):
        result = {"open_fds": None, "fd_limit": None, "open_handles": None,
                  "restart_required": False}
        try:
            if os.name == "nt":
                result["open_handles"] = self.process.num_handles()
            else:
                import resource
                limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
                if limit != resource.RLIM_INFINITY:
                    result["fd_limit"] = int(limit)
                result["open_fds"] = (len(os.listdir("/proc/self/fd"))
                                      if sys.platform == "linux" else self.process.num_fds())
                # Leave room for shutdown, SQL closes and one final audit record.
                if result["fd_limit"] is not None:
                    result["restart_required"] = result["open_fds"] >= max(1, int(limit * .85))
        except OSError as exc:
            result["error_errno"] = exc.errno
            result["restart_required"] = exc.errno in EXHAUSTED_ERRNOS
        return result
