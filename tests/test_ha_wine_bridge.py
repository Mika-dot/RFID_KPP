import ctypes
import os
import unittest
from unittest.mock import Mock

from guardian.wine_proxy import Function, wine_path


class WineBridgeTests(unittest.TestCase):
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
