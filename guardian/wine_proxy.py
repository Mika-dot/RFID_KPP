from __future__ import annotations

import atexit
import ctypes
import json
import os
import queue
import signal
import subprocess
import threading
from pathlib import Path

from guardian.config import atomic_json


def wine_path(path):
    return "Z:" + str(Path(path).resolve()).replace("/", "\\")


class Function:
    # ctypes-compatible attributes used by the existing production adapter.
    argtypes = None
    restype = None

    def __init__(self, library, name):
        self.library, self.name = library, name

    def __call__(self, *args):
        data = {}
        if self.name == "TCPConnect":
            ip = args[0].value if hasattr(args[0], "value") else args[0]
            port = args[1].value if hasattr(args[1], "value") else args[1]
            data = {"ip": ip.decode("ascii"), "port": int(port)}
        response = self.library.call(self.name, data)
        if self.name == "UHF_GetReceived_EX":
            length, buf = response["length"], response["data"]
            if len(buf) != length or not 0 <= length <= 512:
                raise RuntimeError("Invalid SDK bridge response")
            ctypes.cast(args[0], ctypes.POINTER(ctypes.c_int))[0] = length
            target = ctypes.cast(args[1], ctypes.POINTER(ctypes.c_ubyte))
            for i, value in enumerate(buf):
                target[i] = value
        return response["rc"]


class WineLibrary:
    def __init__(self, dll_path):
        self.lock = threading.Lock()
        self.seq = 0
        self.dll_path = dll_path
        self._needs_restart = False
        self.timeout = float(os.getenv("RFID_WINE_RPC_TIMEOUT_SEC", "25"))
        self._spawn()
        atexit.register(self.close)
        for name in ("TCPConnect", "TCPDisconnect", "UHFInventory", "UHFStopGet", "UHF_GetReceived_EX"):
            setattr(self, name, Function(self, name))

    def _spawn(self):
        self.queue = queue.Queue(maxsize=4)
        python = os.environ["RFID_WINE_PYTHON"]
        env = os.environ.copy()
        env["WINEDEBUG"] = "-all"
        env["WINEPREFIX"] = os.environ["RFID_WINE_PREFIX"]
        worker = Path(__file__).with_name("wine_worker.py")
        args = [os.getenv("RFID_WINE_BIN", "wine"), wine_path(python), "-u",
                wine_path(worker), wine_path(self.dll_path)]
        if not env.get("DISPLAY"):
            args = ["xvfb-run", "-a"] + args
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                        env=env, start_new_session=True)
        self.registry = None
        try:
            if env.get("PERIMETER_HA_STATE_DIR"):
                import psutil
                self.registry = (Path(env["PERIMETER_HA_STATE_DIR"]) / "wine-bridges"
                                 / (str(self.process.pid) + ".json"))
                atomic_json(self.registry, {"pid":self.process.pid,
                    "created":psutil.Process(self.process.pid).create_time(),
                    "parent_pid":os.getpid(), "parent_created":psutil.Process().create_time()})
        except BaseException:
            self.close()
            raise
        # Retired readers must never enqueue EOF into a newly spawned bridge.
        threading.Thread(target=self._read, args=(self.process, self.queue), daemon=True).start()
        try:
            ready = self.queue.get(timeout=60)
            if ready != {"ready": True, "bits": 32}:
                raise RuntimeError("Vendor SDK did not load in Wine")
        except BaseException:
            self.close()
            raise
    def _read(self, process, responses):
        try:
            for line in process.stdout:
                responses.put(json.loads(line), timeout=1)
        except Exception:
            pass
        try:
            responses.put(None, timeout=1)
        except queue.Full:
            pass

    def call(self, name, data):
        with self.lock:
            if self._needs_restart:
                if name in {"TCPDisconnect", "UHFStopGet"}:
                    return {"rc": None if name == "TCPDisconnect" else 0}
                if name != "TCPConnect":
                    raise RuntimeError("SDK bridge has no connected session")
                self._spawn()
                self._needs_restart = False
            self.seq += 1
            req = dict(data, op=name, id=self.seq)
            try:
                self.process.stdin.write(json.dumps(req) + "\n")
                self.process.stdin.flush()
                response = self.queue.get(timeout=self.timeout)
                if not response or response.get("id") != self.seq:
                    raise RuntimeError("SDK bridge disconnected or response mismatch")
                if name == "TCPConnect" and response.get("rc") != 0:
                    # The vendor retains a socket on failed connect and its
                    # disconnect deletes a critical section initialized only
                    # on success. Retire the isolated SDK process instead.
                    # SQL/spools remain in the parent; retry lazily starts one
                    # new bridge only after this process is confirmed stopped.
                    self.close()
                    self._needs_restart = True
                return response
            except BaseException:
                self.close()
                raise

    def close(self):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=5)
        for f in (self.process.stdin, self.process.stdout):
            if f:
                f.close()
        if self.registry is not None:
            self.registry.unlink(missing_ok=True)

    def __del__(self):
        if hasattr(self, "process"):
            try:
                self.close()
            except Exception:
                pass
