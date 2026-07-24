#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Portable non-blocking single-instance file lock."""
from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO, Optional


class SingleInstanceError(RuntimeError):
    pass


class SingleInstanceLock:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle: Optional[BinaryIO] = open(self.path, "a+b")
        try:
            self._acquire()
            self.handle.seek(0)
            self.handle.truncate()
            self.handle.write(f"pid={os.getpid()}\n".encode("ascii"))
            self.handle.flush()
        except Exception:
            self.close(unlock=False)
            raise

    def _acquire(self) -> None:
        assert self.handle is not None
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt

            if self.path.stat().st_size == 0:
                self.handle.write(b"0")
                self.handle.flush()
                self.handle.seek(0)
            try:
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise SingleInstanceError(f"Уже запущен другой экземпляр: {self.path}") from exc
        else:
            import fcntl

            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise SingleInstanceError(f"Уже запущен другой экземпляр: {self.path}") from exc

    def close(self, unlock: bool = True) -> None:
        if self.handle is None:
            return
        try:
            if unlock:
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self.handle.close()
        finally:
            self.handle = None

    def __enter__(self) -> "SingleInstanceLock":
        return self

    def __exit__(self, *_args) -> None:
        self.close()
