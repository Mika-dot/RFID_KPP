import json
import unittest
from unittest.mock import Mock

from deploy.ha.diagnose_sql_locks import collect, QUERIES


class SqlLockDiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"node_id": "comparator", "nodes": []}
        self.env = {"PERIMETER_HA_TOKEN": "token-private", "PERIMETER_HA_SQL": "db-private"}

    def test_bounded_select_only_parameterized_and_connection_closed(self):
        db = Mock(); cursor = Mock()
        cursor.description = [("session_id",)]
        cursor.fetchall.return_value = [(42,)]
        db.execute.return_value = cursor
        connect, request = Mock(return_value=db), Mock()
        result = collect(self.cfg, self.env, connect, request)
        self.assertTrue(result["read_only"])
        self.assertEqual(set(QUERIES), set(result["sql"]))
        connect.assert_called_once_with("db-private", timeout=3, autocommit=True, readonly=True)
        db.close.assert_called_once()
        request.assert_not_called()
        for call in db.execute.call_args_list:
            sql = call.args[0].strip().upper()
            self.assertTrue(sql.startswith("SELECT"))
            for forbidden in ("KILL ", "UPDATE ", "ALTER ", "INSERT ", "NOLOCK", "DBCC"):
                self.assertNotIn(forbidden, sql)
        self.assertEqual(("dbo.KPP_ReelEvents", "dbo.KPP_ReelEvents"),
                         db.execute.call_args_list[-1].args[1:])
        self.assertNotIn("db-private", json.dumps(result))

    def test_missing_dmv_permission_is_reported_without_grants_or_secrets(self):
        db = Mock(); cursor = Mock()
        cursor.description = [("database_name",)]
        cursor.fetchall.return_value = [("test",)]
        db.execute.side_effect = [cursor, RuntimeError("token=token-private db-private"), cursor, cursor]
        result = collect(self.cfg, self.env, Mock(return_value=db))
        self.assertIn("requests", result["sql_errors"])
        self.assertIn("sessions", result["sql"])
        raw = json.dumps(result)
        self.assertNotIn("token-private", raw)
        self.assertNotIn("db-private", raw)
        db.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
