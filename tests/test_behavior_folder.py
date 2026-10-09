"""Installer compatibility without accessing any live Grafana instance."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from deploy.ha import finish_behavior_monitoring as installer


class BehaviorFolderTests(unittest.TestCase):
    def test_save_preserves_legacy_numeric_or_modern_uid_folder(self):
        for metadata, key, value in (({"folderId": 7}, "folderId", 7),
                                     ({"folderUid": "factory", "folderId": 7}, "folderUid", "factory")):
            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as folder:
                saved = []
                def request(url, **kwargs):
                    if url.endswith("/summary"):
                        return 200, [{"status": "collecting_baseline", "stale": False}]
                    if "body" in kwargs:
                        saved.append(kwargs["body"])
                        return 200, {}
                    return 200, {"dashboard": {"uid": "existing", "panels": []},
                                 "meta": {"canSave": True, **metadata}}
                monitor = SimpleNamespace(read_env=lambda: ("test", {}), http_json=request)
                original_path = Path
                def local_path(value):
                    return original_path(folder) if str(value).startswith("/var/lib/") else original_path(value)
                with patch.dict(sys.modules, {"install_monitoring": monitor}), \
                     patch.object(installer.sys, "platform", "linux"), \
                     patch.object(installer.os, "geteuid", return_value=0, create=True), \
                     patch.object(installer, "Path", side_effect=local_path):
                    installer.apply()
                self.assertEqual(value, saved[0][key])
                self.assertNotIn("folderId" if key == "folderUid" else "folderUid", saved[0])
