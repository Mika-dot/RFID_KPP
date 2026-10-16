"""Automatic main rollout must never strand a live owner with one new replica.

All tests are offline; no SQL, RFID readers, clocks or native services are touched.
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import Mock, patch

from guardian.controller import Controller


OLD = "a" * 40
NEW = "b" * 40


def status(name, release, epoch=400, target=None, ready=True):
    active = name == "physical"
    return {
        "node": name, "release_sha": release, "main_sha": target,
        "active": active, "healthy": active, "epoch": epoch,
        "prepared": ready, "faulted": False, "operator_maintenance": False,
        "replication_enabled": True, "fallback_enabled": True,
        "preflight": {"ok": True}, "preflight_sample_age": 2,
        "update": {"qualification": "passed"},
    }


def fixture(auto_update=True):
    cfg = {
        "auto_update": auto_update,
        "node_id": "comparator",
        "nodes": [
            {"id": "physical", "priority": 1, "url": "http://physical"},
            {"id": "perimetr", "priority": 2, "url": "http://perimetr"},
            {"id": "comparator", "priority": 3, "url": "http://comparator"},
        ],
    }
    store = Mock()
    store.lease.return_value = {
        "enabled": True, "valid": True, "owner": "physical", "epoch": 400,
        "age": 200,
    }
    store.node_state.return_value = {"faulted": False}
    store.claim_controller.return_value = True
    controller = Controller(cfg, store, Mock(), threading.Event())
    controller.policy.stable = {
        "perimetr": time.monotonic() - 240,
        "comparator": time.monotonic() - 240,
    }
    statuses = {
        "physical": status("physical", OLD, target=NEW),
        "perimetr": status("perimetr", NEW),
        "comparator": status("comparator", NEW),
    }
    controller.poll = lambda node: (node["id"], statuses[node["id"]])
    return controller, store, statuses


class AutomaticRollingUpdateTests(unittest.TestCase):
    def run_tick(self, controller):
        with patch.dict("os.environ", {"PERIMETER_HA_TOKEN": "test"}):
            with patch("guardian.controller.get_json", return_value=(200, {})):
                controller.tick()

    def test_switches_only_when_both_exact_receivers_are_prepared(self):
        ctrl, store, _ = fixture()
        self.run_tick(ctrl)
        self.assertEqual([None, "perimetr"],
                         [call.args[0] for call in store.grant.call_args_list])
        ctrl.telemetry.event.assert_any_call(
            "rolling_update_admitted", previous="physical", target=NEW,
            receivers=["perimetr", "comparator"],
        )
        store.fault.assert_not_called()

    def test_one_ready_receiver_does_not_switch(self):
        ctrl, store, rows = fixture()
        rows["comparator"]["release_sha"] = OLD
        self.run_tick(ctrl)
        store.grant.assert_called_once_with("physical")
        store.fault.assert_not_called()

    def test_maintenance_pending_fault_and_no_replica_are_fail_closed(self):
        for key, value in (
            ("operator_maintenance", True),
            ("faulted", True),
            ("prepared", False),
            ("replication_enabled", False),
            ("fallback_enabled", False),
            ("epoch", 399),
            ("preflight_sample_age", 91),
        ):
            with self.subTest(key=key):
                ctrl, store, rows = fixture()
                rows["comparator"][key] = value
                self.run_tick(ctrl)
                store.grant.assert_called_once_with("physical")

    def test_unqualified_receiver_and_bad_preflight_do_not_switch(self):
        for kind in ("qualification", "preflight"):
            with self.subTest(kind=kind):
                ctrl, store, rows = fixture()
                if kind == "qualification":
                    rows["perimetr"]["update"]["qualification"] = "failed"
                else:
                    rows["perimetr"]["preflight"]["ok"] = False
                self.run_tick(ctrl)
                store.grant.assert_called_once_with("physical")

    def test_auto_update_off_never_switches_for_code_only(self):
        ctrl, store, _ = fixture(auto_update=False)
        self.run_tick(ctrl)
        store.grant.assert_called_once_with("physical")

    def test_unhealthy_old_owner_follows_normal_failover_policy(self):
        ctrl, store, rows = fixture(auto_update=False)
        rows["physical"]["healthy"] = False
        rows["comparator"]["release_sha"] = OLD
        ctrl.policy.proven.add("physical")
        self.run_tick(ctrl)
        self.assertEqual([None, "perimetr"],
                         [call.args[0] for call in store.grant.call_args_list])
        store.fault.assert_called_once_with("physical")


if __name__ == "__main__":
    unittest.main()
