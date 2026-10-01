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
            data = {"ip": ip.decode("ascii"), "port": int(args[1])}
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
        self.queue = queue.Queue(maxsize=4)
        self.timeout = float(os.getenv("RFID_WINE_RPC_TIMEOUT_SEC", "25"))
        python = os.environ["RFID_WINE_PYTHON"]
        env = os.environ.copy()
        env["WINEDEBUG"] = "-all"
        env["WINEPREFIX"] = os.environ["RFID_WINE_PREFIX"]
        worker = Path(__file__).with_name("wine_worker.py")
        args = [os.getenv("RFID_WINE_BIN", "wine"), wine_path(python), "-u",
                wine_path(worker), wine_path(dll_path)]
        if not env.get("DISPLAY"):
            args = ["xvfb-run", "-a"] + args
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                        env=env, start_new_session=True)
        threading.Thread(target=self._read, daemon=True).start()
        atexit.register(self.close)
        try:
            ready = self.queue.get(timeout=60)
            if ready != {"ready": True, "bits": 32}:
                raise RuntimeError("Vendor SDK did not load in Wine")
        except BaseException:
            self.close()
            raise
        for name in ("TCPConnect", "TCPDisconnect", "UHFInventory", "UHFStopGet", "UHF_GetReceived_EX"):
            setattr(self, name, Function(self, name))

    def _read(self):
        try:
            for line in self.process.stdout:
                self.queue.put(json.loads(line), timeout=1)
        except Exception:
            pass
        try:
            self.queue.put(None, timeout=1)
        except queue.Full:
            pass

    def call(self, name, data):
        with self.lock:
            self.seq += 1
            req = dict(data, op=name, id=self.seq)
            try:
                self.process.stdin.write(json.dumps(req) + "\n")
                self.process.stdin.flush()
                response = self.queue.get(timeout=self.timeout)
                if not response or response.get("id") != self.seq:
                    raise RuntimeError("SDK bridge disconnected or response mismatch")
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

    def __del__(self):
        if hasattr(self, "process"):
            try:
                self.close()
            except Exception:
                pass
