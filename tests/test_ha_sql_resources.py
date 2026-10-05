import sys
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from guardian.sql import SqlStore


class SqlResourceTests(unittest.TestCase):
    def connect(self, connection):
        module = SimpleNamespace(connect=Mock(return_value=connection))
        return patch.dict(sys.modules, {"pyodbc": module})

    @patch.dict("os.environ", {"PERIMETER_HA_SQL": "test-only"})
    def test_session_setup_failure_closes_connection_without_entering_body(self):
        connection = Mock()
        connection.execute.side_effect = RuntimeError("session setup failed")
        with self.connect(connection):
            with self.assertRaisesRegex(RuntimeError, "session setup failed"):
                with SqlStore().connect():
                    self.fail("Failed session must not be used")
        connection.close.assert_called_once_with()

    @patch.dict("os.environ", {"PERIMETER_HA_SQL": "test-only"})
    def test_timeout_attribute_failure_also_closes_connection(self):
        class Connection:
            close = Mock()

            @property
            def timeout(self):
                return 0

            @timeout.setter
            def timeout(self, value):
                raise RuntimeError("timeout setup failed")

        connection = Connection()
        with self.connect(connection):
            with self.assertRaisesRegex(RuntimeError, "timeout setup failed"):
                with SqlStore().connect():
                    self.fail("Failed session must not be used")
        connection.close.assert_called_once_with()

    @patch.dict("os.environ", {"PERIMETER_HA_SQL": "test-only"})
    def test_failed_query_closes_connection_and_preserves_exception(self):
        connection = Mock()
        with self.connect(connection):
            with self.assertRaisesRegex(RuntimeError, "query failed"):
                with SqlStore().connect() as found:
                    self.assertIs(connection, found)
                    raise RuntimeError("query failed")
        connection.close.assert_called_once_with()

    @patch.dict("os.environ", {"PERIMETER_HA_SQL": "test-only"})
    def test_successful_read_still_closes_without_committing(self):
        connection = Mock()
        with self.connect(connection):
            with SqlStore().connect() as found:
                found.execute("SELECT 1")
        connection.close.assert_called_once_with()
        connection.commit.assert_not_called()

    @patch.dict("os.environ", {"PERIMETER_HA_SQL": "test-only"})
    def test_pooling_is_disabled_before_the_first_odbc_connection(self):
        module = SimpleNamespace(pooling=True)
        connection = Mock()

        def connect(*args, **kwargs):
            self.assertIs(module.pooling, False)
            return connection

        module.connect = connect
        with patch.dict(sys.modules, {"pyodbc": module}):
            with SqlStore().connect():
                pass
        connection.close.assert_called_once_with()

    @patch.dict("os.environ", {"PERIMETER_HA_SQL": "test-only"})
    def test_repeated_checks_do_not_retain_sockets_in_a_driver_pool(self):
        # Model ODBC's first-HENV latch with real sockets. Changing the flag
        # after connect() would leave all subsequent closes physically pooled.
        sockets = []
        retained = []
        module = SimpleNamespace(pooling=True, initialized_pooling=None)

        def connect(*args, **kwargs):
            if module.initialized_pooling is None:
                module.initialized_pooling = module.pooling
            physical = socket.socket()
            sockets.append(physical)
            connection = Mock()

            def close():
                if module.initialized_pooling:
                    retained.append(physical)
                else:
                    physical.close()

            connection.close.side_effect = close
            return connection

        module.connect = connect
        try:
            with patch.dict(sys.modules, {"pyodbc": module}):
                for index in range(200):
                    if index % 2:
                        with self.assertRaises(RuntimeError):
                            with SqlStore().connect():
                                raise RuntimeError("failed check")
                    else:
                        with SqlStore().connect() as connection:
                            connection.execute("SELECT 1")
            self.assertIs(module.initialized_pooling, False)
            self.assertFalse(retained)
            self.assertTrue(all(item.fileno() == -1 for item in sockets))
        finally:
            for item in sockets:
                item.close()
