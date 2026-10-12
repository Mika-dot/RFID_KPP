import ctypes
import os
import unittest
import queue
import threading
import json
import socket
import subprocess
import sys
from unittest.mock import Mock, patch

from guardian.wine_proxy import Function, WineLibrary, wine_path


class WineBridgeTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "Wine process-group cleanup is Linux-only")
    def test_real_rejected_tcp_retires_process_before_retry_then_connects(self):
        worker = '''import json,socket,sys
held=[]
print(json.dumps({"ready":True,"bits":32}),flush=True)
for line in sys.stdin:
    req=json.loads(line)
    sock=socket.socket();sock.settimeout(2);held.append(sock)
    try:
        sock.connect((req["ip"],req["port"]));rc=0
    except OSError:rc=1
    print(json.dumps({"id":req["id"],"rc":rc}),flush=True)
'''
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))  # Bound but not listening: genuine TCP refusal.
        address = {"ip": "127.0.0.1", "port": listener.getsockname()[1]}
        popen = subprocess.Popen
        def spawn(args, **kwargs):
            return popen([sys.executable, "-u", "-c", worker], **kwargs)
        with patch.dict(os.environ, {"RFID_WINE_PYTHON": "/test/python.exe",
                "RFID_WINE_PREFIX": "/test/prefix", "PERIMETER_HA_STATE_DIR": ""}), \
                patch("guardian.wine_proxy.subprocess.Popen", side_effect=spawn):
            lib = WineLibrary("/test/UHFAPI.dll")
            self.addCleanup(lib.close)
            retired = []
            for _ in range(3):
                self.assertEqual(1, lib.TCPConnect(b"127.0.0.1", address["port"]))
                retired.append(lib.process.pid)
                self.assertIsNotNone(lib.process.poll())
            self.assertEqual(3, len(set(retired)))
            listener.listen(1)
            self.assertEqual(0, lib.TCPConnect(b"127.0.0.1", address["port"]))
            self.assertIsNone(lib.process.poll())
            self.assertNotIn(lib.process.pid, retired)

    def bridge(self, rc):
        lib = WineLibrary.__new__(WineLibrary)
        lib.lock, lib.seq = threading.Lock(), 0
        lib.timeout, lib._needs_restart = 1, False
        lib.process, lib.queue = Mock(), queue.Queue()
        lib.queue.put({"id": 1, "rc": rc})
        lib.close, lib._spawn = Mock(), Mock()
        return lib

    def test_refused_connect_retires_bridge_and_lazy_retry_preserves_error(self):
        lib = self.bridge(1)
        self.assertEqual(1, lib.call("TCPConnect", {"ip": "reader", "port": 8888})["rc"])
        lib.close.assert_called_once()
        lib._spawn.assert_not_called()
        self.assertTrue(lib._needs_restart)
        # Final cleanup must neither invoke unsafe vendor disconnect nor spawn.
        writes = lib.process.stdin.write.call_count
        self.assertIsNone(lib.call("TCPDisconnect", {})["rc"])
        self.assertEqual(0, lib.call("UHFStopGet", {})["rc"])
        self.assertEqual(writes, lib.process.stdin.write.call_count)
        lib._spawn.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "no connected session"):
            lib.call("UHFInventory", {})
        lib.queue.put({"id": 2, "rc": 0})
        self.assertEqual(0, lib.call("TCPConnect", {})["rc"])
        lib._spawn.assert_called_once()
        self.assertFalse(lib._needs_restart)

    def test_successful_connect_keeps_live_bridge(self):
        lib = self.bridge(0)
        self.assertEqual(0, lib.call("TCPConnect", {})["rc"])
        lib.close.assert_not_called()
        lib._spawn.assert_not_called()

    def test_retired_reader_cannot_pollute_current_response_queue(self):
        lib = self.bridge(0)
        old_queue = queue.Queue()
        old_process = Mock(); old_process.stdout = iter([])
        lib._read(old_process, old_queue)
        self.assertIsNone(old_queue.get_nowait())
        self.assertEqual({"id": 1, "rc": 0}, lib.queue.get_nowait())

    def test_preserves_vendor_buffer_and_return_code(self):
        lib = Mock()
        lib.call.return_value = {"length":3, "data":[1,2,255], "rc":0}
        length, buf = ctypes.c_int(0), (ctypes.c_ubyte*512)()
        rc = Function(lib,"UHF_GetReceived_EX")(ctypes.byref(length), ctypes.byref(buf))
        self.assertEqual(0,rc)
        self.assertEqual(3,length.value)
        self.assertEqual([1,2,255],list(buf[:3]))

    def test_oversize_vendor_buffer_rejected(self):
        lib = Mock()
        lib.call.return_value = {"length":513,"data":[0]*513,"rc":0}
        with self.assertRaises(RuntimeError):
            Function(lib,"UHF_GetReceived_EX")(ctypes.byref(ctypes.c_int()), ctypes.byref((ctypes.c_ubyte*512)()))

    def test_wrong_length_rejected(self):
        lib = Mock()
        lib.call.return_value = {"length":2,"data":[0],"rc":0}
        with self.assertRaises(RuntimeError):
            Function(lib,"UHF_GetReceived_EX")(ctypes.byref(ctypes.c_int()), ctypes.byref((ctypes.c_ubyte*512)()))

    def test_tcp_connect_does_not_change_address(self):
        lib = Mock()
        lib.call.return_value = {"rc":1}
        self.assertEqual(1,Function(lib,"TCPConnect")(b"172.31.128.170",8888))
        lib.call.assert_called_once_with("TCPConnect", {"ip":"172.31.128.170","port":8888})

    @unittest.skipIf(os.name == "nt", "Z: path conversion belongs to the Linux Wine host")
    def test_windows_z_drive_mapping(self):
        self.assertEqual("Z:\\opt\\perimeter\\UHFAPI.dll",wine_path("/opt/perimeter/UHFAPI.dll"))

    def test_tcp_connect_accepts_ctypes_port(self):
        lib = Mock()
        lib.call.return_value = {"rc":1}
        Function(lib,"TCPConnect")(ctypes.c_char_p(b"reader"), ctypes.c_ushort(8888))
        lib.call.assert_called_once_with("TCPConnect", {"ip":"reader", "port":8888})
