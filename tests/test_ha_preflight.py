import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from guardian.config import SERVICES
from guardian.probes import preflight


class PreflightDatabaseTests(unittest.TestCase):
    def report(self, different_dst):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for _, script in SERVICES.values():
                (root / script).parent.mkdir(parents=True, exist_ok=True)
                (root / script).touch()
            asset = root / "asset"
            asset.touch()
            env = {k:"value" for k in (
                "RFID_DB_CONNECTION", "KPP_CONN_STR", "KPP_WEB_DB_CONNECTION", "RFID_READER_IP",
                "SRC_SERVER", "SRC_DATABASE", "SRC_USERNAME", "SRC_PASSWORD",
                "DST_SERVER", "DST_DATABASE", "DST_USERNAME", "DST_PASSWORD",
                "RFID_RTSP_0", "RFID_RTSP_1", "KPP_WEB_AUTH_USER", "KPP_WEB_AUTH_PASSWORD")}
            env.update(RFID_MODEL_PATH=str(asset), RFID_DLL_PATH=str(asset), RFID_MASK_ENABLED="0")
            cfg = {"node_id":"physical", "root":folder, "state_dir":folder, "python":sys.executable,
                   "min_free_bytes":0, "env":env}
            store = MagicMock()
            conn = store.connect.return_value.__enter__.return_value
            conn.execute.return_value.fetchone.side_effect = [("sql", "output"), (9,)]
            db = Mock()
            db.execute.return_value.fetchone.side_effect = [
                ("sql", "output"), ("sql", "output"), ("sql", "output"),
                ("other-sql", "output") if different_dst else ("sql", "output")]
            pyodbc = Mock()
            pyodbc.pooling = True
            pyodbc.connect.return_value = db
            def connect(*args, **kwargs):
                self.assertIs(pyodbc.pooling, False)
                return db
            pyodbc.connect.side_effect = connect
            with patch.dict(sys.modules, {"pyodbc":pyodbc}), patch("guardian.probes.subprocess.run") as run:
                run.return_value.returncode = 0
                result = preflight(cfg, store, active=True)
            self.assertEqual(4, pyodbc.connect.call_count)
            self.assertIn("DATABASE={value}", pyodbc.connect.call_args.args[0])
            return result

    def test_wrong_rusguard_destination_is_not_prepared(self):
        result = self.report(True)
        self.assertFalse(result["ok"])
        self.assertFalse(result["checks"]["same_output_database"])

    def test_all_four_destinations_must_match_control_database(self):
        self.assertTrue(self.report(False)["ok"])
