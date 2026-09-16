from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeploymentHotfixTests(unittest.TestCase):
    def test_legacy_entrypoint_is_replaced(self) -> None:
        launcher = ROOT / "RUN_RFID_KPP_FINAL.cmd"
        self.assertTrue(launcher.exists())
        text = launcher.read_text(encoding="ascii")
        self.assertIn("start_services_v3.py", text)
        self.assertNotIn('start "1 RFID Reader', text)
        self.assertNotIn("kpp_1_reliable_v2.4_full_rebuild.py", text)

    def test_production_config_has_no_placeholders(self) -> None:
        text = (ROOT / "deploy" / "config_v3.cmd").read_text(encoding="ascii")
        for placeholder in ("<SQL_SERVER>", "<SQL_PASSWORD>", "<CAMERA_USER>", "<RFID_READER_IP>"):
            self.assertNotIn(placeholder, text)
        for required in ("KPP_CONN_STR", "RFID_READER_IP", "RFID_RTSP_0", "RFID_RTSP_1", "SRC_SERVER"):
            self.assertIn(required, text)

    def test_cmd_files_are_ascii_crlf(self) -> None:
        files = [ROOT / "RUN_RFID_KPP_FINAL.cmd", *sorted((ROOT / "deploy").glob("*.cmd"))]
        self.assertGreaterEqual(len(files), 10)
        for path in files:
            data = path.read_bytes()
            self.assertTrue(all(byte < 128 for byte in data), path)
            self.assertIn(b"\r\n", data, path)
            self.assertNotIn(b"\n", data.replace(b"\r\n", b""), path)

    def test_manifest_uses_required_entrypoint(self) -> None:
        manifest = json.loads((ROOT / "DEPLOYMENT_MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["entry_point"], "RUN_RFID_KPP_FINAL.cmd")
        self.assertEqual(manifest["release"], "3.3.0-launchfix")

    def test_wrappers_are_argument_free(self) -> None:
        wrappers = sorted((ROOT / "deploy").glob("RUN_*_V3.cmd"))
        self.assertEqual(len(wrappers), 5)
        for wrapper in wrappers:
            text = wrapper.read_text(encoding="ascii")
            self.assertNotIn("%~1", text)
            self.assertNotIn("%~2", text)
            self.assertNotIn("%~3", text)
            self.assertIn("resolve_python.cmd", text)

    def test_python_launcher_builds_safe_command(self) -> None:
        path = ROOT / "deploy" / "start_services_v3.py"
        spec = importlib.util.spec_from_file_location("start_services_v3", path)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        command = module.build_command(r"C:\\Windows\\System32\\cmd.exe", Path(r"D:\\RFID KPP\\deploy\\RUN_WEB_V3.cmd"))
        self.assertEqual(command[:4], [r"C:\\Windows\\System32\\cmd.exe", "/D", "/K", "call"])
        self.assertEqual(command[4], r"D:\\RFID KPP\\deploy\\RUN_WEB_V3.cmd")

    def test_rtsp_driver_setting_is_disabled_by_default(self) -> None:
        config = (ROOT / "deploy" / "config_v3.cmd").read_text(encoding="ascii")
        source = (ROOT / "RTSP" / "RTSP_yolo_DB_v3.py").read_text(encoding="utf-8")
        self.assertIn("RFID_SET_CAPTURE_BUFFER=0", config)
        self.assertIn('RTSP_BACKEND = os.getenv("RFID_RTSP_BACKEND", "FFMPEG")', source)
        self.assertIn("if Config.SET_CAPTURE_BUFFER:", source)

    def test_warehouse_nullable_tag_contract_is_in_production_entrypoints(self) -> None:
        aggregator = (ROOT / "KPP" / "kpp_aggregator_v3_warehouse.py").read_text(encoding="utf-8")
        web = (ROOT / "web" / "kpp_reel_dashboard_v3_fixed.py").read_text(encoding="utf-8")
        self.assertNotIn("len(tag) < 24", aggregator)
        self.assertIn("not normalized_ids or not normalized_series", aggregator)
        self.assertIn("resolve_warehouse_identity", aggregator)
        self.assertIn("resolve_warehouse_identity", web)
        self.assertNotIn("ISNULL(e0.SourceTag,'')", web)


if __name__ == "__main__":
    unittest.main()
