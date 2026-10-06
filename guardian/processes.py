from __future__ import annotations

import os
import signal
import subprocess
import logging
from logging.handlers import RotatingFileHandler
import threading
import time
from pathlib import Path

from guardian.config import SERVICES, atomic_json


class Processes:
    """Own only directly spawned services; never kill by an unverified bare PID."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.children = {}
        self.logs = {}
        self.epoch = None
        self.registry = Path(cfg["state_dir"]) / "children.json"

    def reap_orphans(self):
        import json
        import psutil
        self.reap_bridges()
        if not self.registry.exists():
            return
        rows = json.loads(self.registry.read_text(encoding="utf-8"))
        for row in rows:
            try:
                p = psutil.Process(row["pid"])
                if abs(p.create_time()-row["created"]) > .01:
                    continue
                env = p.environ()
                if env.get("PERIMETER_HA_NODE") != self.cfg["node_id"]:
                    continue
                if not any("run_service.py" in arg for arg in p.cmdline()):
                    continue
                targets = p.children(recursive=True) + [p]
                for item in reversed(targets):
                    try:
                        item.kill()
                    except psutil.NoSuchProcess:
                        pass
                psutil.wait_procs(targets, timeout=5)
            except psutil.NoSuchProcess:
                pass
        self.registry.unlink(missing_ok=True)

    def reap_bridges(self, orphaned_only=False):
        """Wine has a separate process group; it must also be fenced on demotion."""
        import json
        import psutil
        for path in (Path(self.cfg["state_dir"]) / "wine-bridges").glob("*.json"):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
                if orphaned_only:
                    if "parent_pid" not in row:
                        continue
                    try:
                        parent = psutil.Process(row["parent_pid"])
                        if (abs(parent.create_time()-row["parent_created"]) <= .01
                                and parent.status() != psutil.STATUS_ZOMBIE):
                            continue
                    except psutil.NoSuchProcess:
                        pass
                p = psutil.Process(row["pid"])
                if (abs(p.create_time()-row["created"]) > .01
                        or p.environ().get("PERIMETER_HA_NODE") != self.cfg["node_id"]
                        or not any("wine_worker.py" in arg for arg in p.cmdline())):
                    path.unlink(missing_ok=True)
                    continue
                targets = p.children(recursive=True) + [p]
                for item in reversed(targets):
                    try:
                        item.kill()
                    except psutil.NoSuchProcess:
                        pass
                psutil.wait_procs(targets, timeout=5)
            except psutil.NoSuchProcess:
                pass
            except FileNotFoundError:
                # The owning bridge can finish and remove its record concurrently.
                continue
            path.unlink(missing_ok=True)

    def start(self, epoch):
        if self.epoch == epoch and self.children:
            return
        self.stop()
        import psutil
        root = Path(self.cfg["root"])
        logdir = Path(self.cfg["state_dir"]) / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(self.cfg.get("env", {}))
        # Pipes under Windows SYSTEM otherwise use the locale (often cp1251),
        # which cannot encode the dashboard banner or emoji. Capture is UTF-8.
        env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        env.update(PERIMETER_HA_NODE=self.cfg["node_id"], PERIMETER_HA_EPOCH=str(epoch),
                   PERIMETER_HA_STATE_DIR=self.cfg["state_dir"],
                   PERIMETER_HEALTH_HOST="127.0.0.1", RFID_HEADLESS="1")
        registry = []
        try:
            for name, (_, script) in SERVICES.items():
                py = self.cfg.get("python32", self.cfg["python"]) if name == "RfidReader" else self.cfg["python"]
                options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
                p = subprocess.Popen([py, "-u", str(root / "deploy/run_service.py"),
                       "--service", "Perimeter." + name, "--script", str(root / script)],
                       cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, **options)
                self.children[name] = p
                thread = threading.Thread(target=self.capture, args=(p,logdir/(name+".log")), daemon=True)
                self.logs[name] = thread
                thread.start()
                registry.append({"pid": p.pid, "created": psutil.Process(p.pid).create_time()})
                atomic_json(self.registry, registry)
            self.epoch = epoch
        except BaseException:
            self.stop()
            raise

    def stop(self):
        for p in self.children.values():
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            else:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        until = time.monotonic() + 3
        for p in self.children.values():
            try:
                p.wait(timeout=max(.1, until-time.monotonic()))
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    p.kill()
                p.wait(timeout=5)
        self.reap_bridges()
        self.children.clear()
        self.epoch = None
        for thread in self.logs.values():
            thread.join(timeout=2)
        self.logs.clear()
        self.registry.unlink(missing_ok=True)

    def alive(self):
        return len(self.children) == 5 and all(p.poll() is None for p in self.children.values())

    def capture(self, process, path):
        handler=RotatingFileHandler(path,maxBytes=self.cfg.get("child_log_bytes",10*1024*1024),
                                   backupCount=3,encoding="utf-8")
        try:
            for raw in iter(lambda:process.stdout.readline(65536),b""):
                text=raw.decode("utf-8","replace").rstrip("\r\n")
                handler.emit(logging.LogRecord("perimeter-child",logging.INFO,"",0,text,(),None))
        finally:
            process.stdout.close()
            handler.close()
