"""Physical Windows: prove Comparator's five production services, then fail back.

Uses an adjacent, hash-verified test-failover.py helper. Perimetr keeps its
controller lease while guarded maintenance excludes it as an executor.
Kill only the registered physical HA Aggregator. The controller elects the
Comparator; no SQL lease writes or synthetic business events are performed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import uuid
from pathlib import Path

RELEASE = "79d1caa3a0709ca96d6ee6d55c8ed73623ca8665"
BASE_HASH = "e51e84643fa1ba58d0c27bd462ae367b3b62f2da5f9b4b3643fedccf6d96cee7"
BASE = None


class Abort(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise Abort(message)


def load_base(directory):
    path = directory / "test-failover.py"
    require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == BASE_HASH,
            "Download the matching hash-verified test-failover.py helper")
    spec = importlib.util.spec_from_file_location("perimeter_comparator_base", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_test(base):
    class Test(base.Test):
        def __init__(self):
            super().__init__()
            self.reserve = "comparator"
            self.perimetr_held = False
            self.recovering = False

        def ready_snapshot(self, snapshot, owner, held):
            # Once the controller rejects the only unheld executor, keeping
            # both reserves held cannot produce a successful proof. Release
            # them through run()'s recovery immediately, not at the 300s timeout.
            if (owner == "comparator" and held and snapshot.get("consistent") is True
                    and snapshot["nodes"]["comparator"].get("faulted") is True):
                raise base.Halt("Comparator quarantined; releasing both executor holds")
            controller = snapshot["controller"]["owner"]
            if controller != "perimetr" and not (self.recovering and controller == "comparator"):
                return False
            holds = ("perimetr",) if self.perimetr_held else ()
            return base.ready(snapshot, owner, held, controller=controller, held_nodes=holds)

        def perimetr_maintenance(self, enabled):
            code, data = self.get_json(self.nodes["perimetr"]["url"].rstrip("/") + "/maintenance", self.token,
                                      timeout=15, body={"enabled": enabled, "release": RELEASE})
            require(code == 200 and data.get("operator_maintenance") is enabled and data.get("stopped") is True,
                    "Perimetr executor maintenance transition unconfirmed")

        def hold_perimetr(self):
            s = self.observe()
            require(self.ready_snapshot(s, "physical", False) and self.workers("perimetr") == {},
                    "Healthy physical and passive Perimetr required before executor hold")
            lease = self.store.lease()
            require(all(lease[k] == s["lease"][k] for k in ("owner", "epoch", "valid", "enabled")),
                    "Executor lease changed before Perimetr hold")
            # Persist intent before the HTTP request; lost acknowledgement is recoverable.
            self.perimetr_held = self.record["perimetr_held"] = True
            self.phase("PERIMETR_EXECUTOR_HOLD_INTENT")
            self.perimetr_maintenance(True)
            self.phase("PERIMETR_EXECUTOR_HELD_CONTROLLER_CONTINUES")

        def release_perimetr(self):
            if not self.perimetr_held:
                return
            deadline = base.time.monotonic() + 120
            while True:
                try:
                    lease = self.store.lease()
                    if lease["enabled"] is not True:
                        raise base.Halt("HA disabled externally; recovery did not change it")
                    s = self.status("perimetr")
                    if s.get("operator_maintenance") is False:
                        # A previous request may have succeeded before its acknowledgement was lost.
                        self.perimetr_held = self.record["perimetr_held"] = False
                        self.phase("PERIMETR_EXECUTOR_HOLD_ALREADY_RELEASED")
                        return
                    require(s.get("operator_maintenance") is True and s.get("active") is False
                            and not (lease["valid"] and lease["owner"] == "perimetr"),
                            "Cannot release hold on an active Perimetr executor")
                    self.perimetr_maintenance(False)
                    self.perimetr_held = self.record["perimetr_held"] = False
                    self.phase("PERIMETR_RELEASED_FOR_INDEPENDENT_RECOVERY")
                    return
                except base.Halt:
                    raise
                except Exception as exc:
                    print("PERIMETR_RELEASE_WAIT", type(exc).__name__, flush=True)
                require(base.time.monotonic() < deadline, "Perimetr hold release unconfirmed; use --recover")
                base.time.sleep(2)

        def recover(self):
            self.recovering = True
            # Attempt BOTH releases even when physical release encounters an error.
            failure = None
            try:
                self.release_physical()
            except BaseException as exc:
                failure = exc
            self.release_perimetr()
            if failure is not None:
                raise failure
            s = self.wait_ready("physical")
            self.phase("PHYSICAL_FULL_STACK_AND_BOTH_RESERVES_RESTORED")
            print("RESTORED_CONTROLLER", s["controller"]["owner"], flush=True)

        def run(self):
            self.precheck()
            target = self.capture_aggregator()
            folder = Path(self.cfg["state_dir"]) / "acceptance"
            self.private_directory(folder)
            self.journal = folder / ("comparator-stack-" + uuid.uuid4().hex + ".json")
            self.record = {"release": RELEASE, "kind": "comparator-stack", "changed": False,
                           "perimetr_held": False, "reserve_proven": False, "events": [],
                           "aggregator_pid": target[0].pid, "created": target[2], "initial_epoch": target[3]}
            self.phase("COMPARATOR_STACK_TEST_CAPTURED")
            print("COMPARATOR_STACK_TEST_JOURNAL", str(self.journal), flush=True)
            changed = False
            try:
                changed = self.record["changed"] = True
                self.phase("COMPARATOR_STACK_TEST_CHANGE_INTENT")
                self.hold_perimetr()
                self.phase("AGGREGATOR_FAILURE_REQUESTED")
                self.stop_aggregator(target)
                self.hold_demoted_physical()
                self.wait_ready("comparator", held=True, timeout=300)
                self.record["reserve_proven"] = True
                self.phase("COMPARATOR_FULL_STACK_PROVEN")
                self.recover()
                self.phase("COMPARATOR_STACK_AND_FAILBACK_PROTOCOL2_OK")
            except BaseException:
                if changed:
                    try:
                        self.recover()
                    except BaseException as exc:
                        print("COMPARATOR_STACK_RECOVERY_UNCONFIRMED", str(exc) if isinstance(exc, (Abort, base.Abort))
                              else type(exc).__name__, flush=True)
                        print("DO_NOT_START_LEGACY_OR_DISABLE_HA; USE_RECOVER_WITH_JOURNAL", flush=True)
                raise
    return Test


def main(argv=None):
    global BASE
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--comparator-test", action="store_true")
    modes.add_argument("--recover", type=Path, metavar="JOURNAL")
    args = parser.parse_args(argv)
    BASE = load_base(Path(__file__).resolve().parent)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    app = make_test(BASE)()
    from common.single_instance import SingleInstanceLock
    with SingleInstanceLock(str(Path(app.cfg["state_dir"]) / "operator-cutover.lock")):
        if args.recover:
            root = (Path(app.cfg["state_dir"]) / "acceptance").resolve()
            journal = args.recover.resolve()
            require(journal.is_relative_to(root) and journal.name.startswith("comparator-stack-") and journal.suffix == ".json",
                    "Unexpected Comparator stack recovery journal path")
            app.journal, app.record = journal, json.loads(journal.read_text(encoding="utf-8-sig"))
            require(app.record.get("release") == RELEASE and app.record.get("kind") == "comparator-stack"
                    and app.record.get("changed") is True and type(app.record.get("perimetr_held")) is bool
                    and isinstance(app.record.get("events"), list), "Interrupted Comparator stack test journal required")
            app.perimetr_held = app.record["perimetr_held"]
            app.recover()
        elif args.check:
            app.precheck()
        else:
            app.run()
    return 0


def cli():
    try:
        return main()
    except (Exception, KeyboardInterrupt) as exc:
        controlled = isinstance(exc, Abort) or BASE is not None and isinstance(exc, BASE.Abort)
        print("COMPARATOR_STACK_TEST_STOPPED", str(exc) if controlled else type(exc).__name__, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
