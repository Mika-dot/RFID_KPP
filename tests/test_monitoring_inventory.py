import importlib.util
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

SPEC = importlib.util.spec_from_file_location("monitoring_inventory", Path(__file__).parents[1] / "deploy/ha/inspect_monitoring.py")
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


class MonitoringInventorySecurityTests(unittest.TestCase):
    def test_credential_cannot_be_sent_to_remote_host(self):
        with self.assertRaisesRegex(ValueError, "CredentialDestinationRefused"):
            inventory.fetch("http://example.invalid:3000/api", token="private-token")

    def test_redirects_do_not_forward_authorization(self):
        self.assertIsNone(inventory.NoRedirects().redirect_request(None, None, 302, "", {}, "http://elsewhere.invalid"))

    def test_environment_is_parsed_without_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "env"
            path.write_text("GRAFANA_TOKEN='$(private-command)'\nZABBIX_UID=existing-uid\nOTHER_PASSWORD=do-not-read\n")
            self.assertEqual(inventory.env_values(path), {"GRAFANA_TOKEN": "$(private-command)", "ZABBIX_UID": "existing-uid"})

    def test_output_omits_url_credentials_and_error_bodies(self):
        self.assertEqual(inventory.endpoint("http://user:private@localhost:19150/summary?token=private"), "http://localhost:19150/summary")
        self.assertEqual(inventory.safe_error(HTTPError("private-url", 403, "private-body", None, None)), "HTTP_403")
