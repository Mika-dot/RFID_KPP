import copy
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError

from deploy.ha import grafana_visual as visual
from deploy.ha import install_monitoring as monitor
from deploy.ha.update_visual_wallboard import PLAYLIST_API, update


def sample_cluster():
    nodes = []
    for node in monitor.NODES:
        active = node == "physical"
        value = dict(node=node, label=monitor.LABELS[node], reachable=1, active=int(active),
                     healthy=int(active), prepared=1, faulted=0, epoch=42, http=200,
                     severity=0, role="ВЕДУЩИЙ" if active else "РЕЗЕРВ", detail="synthetic",
                     release="a" * 40, snapshot_fresh=True, bus_age=1,
                     bus={"rfid_pending": 0, "video_pending": 0, "fallback_records": 100,
                          "fallback_pending": 0},
                     mirror={"enabled": True, "retention_days": 90,
                             "streams": {"warehouse": {"age_sec": 1, "caught_up": True, "records": 100}}},
                     controller={"owner": "comparator", "valid": True, "at": time.time()},
                     correlation={"mode": "shadow"})
        value["services"] = {name: {"ok": active, "observed": active,
                                   "dependencies": {key: "ok" for key in ("rfid_reader", "camera_0", "camera_1", "source_database", "database")}}
                             for name in ("RfidReader", "Yolo", "RusGuardSync", "Aggregator", "WebDashboard")}
        nodes.append(value)
    data = monitor.summarize(nodes, (0, "Без предупреждений"))
    data["gateway"] = {"ready": True, "observed": True}
    return data


class FakeGrafana:
    def __init__(self, fail_queries=False):
        dashboard = {"uid": visual.OVERVIEW_UID, "version": 16,
                     "panels": [{"id": 1, "type": "text", "options": {"content": "Upper banner"},
                                 "gridPos": {"x": 0, "y": 0, "w": 24, "h": 3}},
                                {"id": 2, "type": "stat", "options": {"original": True},
                                 "gridPos": {"x": 0, "y": 3, "w": 24, "h": 12}}]}
        self.dashboards = {visual.OVERVIEW_UID: {"dashboard": dashboard, "meta": {"canSave": True, "folderUid": "original-folder"}}}
        self.playlist = None
        self.calls = []
        self.fail_queries = fail_queries

    def api(self, route, body=None, method=None):
        self.calls.append((route, body, method))
        if route == "/api/plugins":
            return [{"id": "volkovlabs-echarts-panel"}]
        if route == "/api/datasources/uid/wallboard-api":
            return {"type": visual.DS["type"], "jsonData": {"allowedHosts": ["http://127.0.0.1:19150"]}}
        if route == "/api/dashboards/db":
            dashboard = copy.deepcopy(body["dashboard"])
            old = self.dashboards.get(dashboard["uid"], {}).get("dashboard", {})
            if old.get("version", 0) != dashboard.get("version", 0):
                raise RuntimeError("version-conflict")
            dashboard["version"] = old.get("version", 0) + 1
            self.dashboards[dashboard["uid"]] = {"dashboard": dashboard, "meta": {"canSave": True, "folderUid": body.get("folderUid")}}
            return {"version": dashboard["version"], "status": "success"}
        if route.startswith("/api/dashboards/uid/"):
            uid = route.rsplit("/", 1)[1]
            if uid not in self.dashboards:
                raise HTTPError(route, 404, "missing", {}, io.BytesIO())
            if method == "DELETE":
                del self.dashboards[uid]
                return {}
            return copy.deepcopy(self.dashboards[uid])
        if route.startswith(PLAYLIST_API):
            if method == "DELETE":
                self.playlist = None
                return {}
            if body:
                value = copy.deepcopy(body)
                value["metadata"]["resourceVersion"] = str(int((self.playlist or {}).get("metadata", {}).get("resourceVersion", "0")) + 1)
                self.playlist = value
                return copy.deepcopy(value)
            if self.playlist is None:
                raise HTTPError(route, 404, "missing", {}, io.BytesIO())
            return copy.deepcopy(self.playlist)
        if route == "/api/ds/query":
            return {"results": {"A": {"error": "query-failure"} if self.fail_queries else {"frames": [{"schema": {"fields": []}}]}}}
        raise AssertionError(route)


class VisualTests(unittest.TestCase):
    def test_partial_mirror_is_warning_and_never_a_ready_local_copy(self):
        data = sample_cluster()
        rows = monitor.diagram_rows(data)
        self.assertEqual("warning", next(row["state"] for row in rows if row["id"] == "physical-queue"))
        self.assertTrue(all(row["cache_state"] == "ЗАПОЛНЕНИЕ" for row in monitor.queue_rows(data)))
        data["stale"] = True
        self.assertTrue(all(row["cache_state"] == "НЕТ ДАННЫХ" for row in monitor.queue_rows(data)))

    def test_repair_and_update_card_uses_observed_state_not_llm_readiness(self):
        data = sample_cluster()
        for value in data["nodes"].values():
            value["repair"] = {"verification_required": False}
            value["update"] = {"pending": False, "quarantined": 0}
        def repair():
            return next(row for row in monitor.diagram_rows(data) if row["id"] == "repair")
        self.assertEqual("ready", repair()["state"])
        data["nodes"]["perimetr"]["repair"]["verification_required"] = True
        self.assertEqual("warning", repair()["state"])
        data["stale"] = True
        self.assertEqual("unknown", repair()["state"])

    def test_all_fifteen_workers_and_three_nodes_are_visible(self):
        rows = monitor.diagram_rows(sample_cluster())
        self.assertEqual(3, sum(row["kind"] == "header" for row in rows))
        for node in monitor.NODES:
            for name in ("RfidReader", "Yolo", "RusGuardSync", "Aggregator", "WebDashboard"):
                self.assertTrue(any(row["id"] == node + "-" + name for row in rows))
        self.assertTrue(any(row["id"] == "gateway" for row in rows))
        self.assertTrue(any(row["id"] == "lease" for row in rows))

    def test_only_current_executor_has_an_active_business_route(self):
        rows = monitor.diagram_rows(sample_cluster())
        active_edges = [row for row in rows if row["kind"] == "edge" and row["state"] == "active"]
        self.assertTrue(active_edges)
        self.assertFalse(any("perimetr-" in row["source"] + row["target"] or "comparator-" in row["source"] + row["target"] for row in active_edges))

    def test_stale_or_split_brain_snapshot_has_no_green_business_edges(self):
        normal = sample_cluster()
        for value in (dict(normal, stale=True), normal):
            if not value.get("stale"):
                value["nodes"]["perimetr"]["active"] = 1
            rows = monitor.diagram_rows(value)
            self.assertFalse(any(row["kind"] == "edge" and row["state"] == "active" for row in rows))

    def test_warehouse_only_never_appears_as_a_reader_exit(self):
        data = sample_cluster()
        data["recent"] = {"stale": False, "events": [dict(FirstSeen="2026-10-08T12:00:00", RfidReadCount=0,
            SessionCloseReason="WAREHOUSE_ONLY", FinalDirection="OUT", SourceTag="synthetic")]}
        row = monitor.recent_rows(data)[0]
        self.assertEqual("СКЛАД", row["direction"])
        self.assertEqual("ТОЛЬКО СКЛАД", row["state"])

    def test_dashboard_has_no_text_banner_and_one_minute_rotation(self):
        dashboard = visual.visual_dashboard()
        self.assertEqual(9, len(dashboard["panels"]))
        self.assertFalse(any(panel["type"] == "text" for panel in dashboard["panels"]))
        self.assertEqual("1m", visual.playlist()["spec"]["interval"])
        self.assertEqual([visual.OVERVIEW_UID, visual.VISUAL_UID], [row["value"] for row in visual.playlist()["spec"]["items"]])
        self.assertTrue(all(target["parser"] == "backend" and target["url"].startswith(visual.BASE)
                            for panel in dashboard["panels"] for target in panel["targets"]))

    def test_live_plan_performs_no_writes(self):
        fake = FakeGrafana()
        before = copy.deepcopy(fake.dashboards)
        report = update(fake.api)
        self.assertFalse(report["applied"])
        self.assertEqual([1], report["removed_banner_ids"])
        self.assertEqual(before, fake.dashboards)
        self.assertFalse(any(body or method for _route, body, method in fake.calls))

    def test_apply_is_idempotent_preserves_other_projects_and_verifies_frames(self):
        fake = FakeGrafana()
        with tempfile.TemporaryDirectory() as folder:
            report = update(fake.api, True, backup_root=folder)
            second = update(fake.api, True, backup_root=folder)
        overview = fake.dashboards[visual.OVERVIEW_UID]["dashboard"]
        self.assertTrue(report["backend_queries_verified"])
        self.assertEqual([], second["removed_banner_ids"])
        self.assertEqual({"original": True}, next(row for row in overview["panels"] if row["id"] == 2)["options"])
        self.assertEqual(5, len(overview["panels"]))
        self.assertEqual("original-folder", fake.dashboards[visual.VISUAL_UID]["meta"]["folderUid"])

    def test_failed_backend_query_rolls_back_dashboards_playlist_and_collector(self):
        fake = FakeGrafana(fail_queries=True)
        original = copy.deepcopy(fake.dashboards[visual.OVERVIEW_UID]["dashboard"])
        rollback = []
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "BackendFrames"):
                update(fake.api, True, backup_root=folder, collector_update=lambda _folder: lambda: rollback.append(True))
            receipts = list(Path(folder).glob("*/rollback.json"))
            self.assertTrue(json.loads(receipts[0].read_text())["complete"])
        restored = fake.dashboards[visual.OVERVIEW_UID]["dashboard"]
        self.assertEqual(original["panels"], restored["panels"])
        self.assertNotIn(visual.VISUAL_UID, fake.dashboards)
        self.assertIsNone(fake.playlist)
        self.assertEqual([True], rollback)


if __name__ == "__main__":
    unittest.main()
