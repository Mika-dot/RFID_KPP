"""Run ONLY in 32-bit Windows Python under Wine. No SQL or third-party wheels."""
import ctypes
import json
import sys


def main():
    if ctypes.sizeof(ctypes.c_void_p) != 4:
        raise RuntimeError("UHFAPI.dll requires 32-bit Windows Python")
    lib = ctypes.CDLL(sys.argv[1])
    lib.TCPConnect.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    lib.TCPConnect.restype = ctypes.c_int
    lib.TCPDisconnect.argtypes = []
    lib.TCPDisconnect.restype = None
    for name in ("UHFInventory", "UHFStopGet"):
        getattr(lib, name).argtypes = []
        getattr(lib, name).restype = ctypes.c_int
    lib.UHF_GetReceived_EX.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ubyte*512)]
    lib.UHF_GetReceived_EX.restype = ctypes.c_int
    print(json.dumps({"ready": True, "bits": 32}), flush=True)
    for line in sys.stdin:
        req = json.loads(line)
        name = req["op"]
        if name == "TCPConnect":
            result = {"rc": lib.TCPConnect(req["ip"].encode("ascii"), int(req["port"]))}
        elif name in ("UHFInventory", "UHFStopGet", "TCPDisconnect"):
            result = {"rc": getattr(lib, name)()}
        elif name == "UHF_GetReceived_EX":
            length = ctypes.c_int(0)
            buf = (ctypes.c_ubyte*512)()
            rc = lib.UHF_GetReceived_EX(ctypes.byref(length), ctypes.byref(buf))
            if not 0 <= length.value <= 512:
                raise RuntimeError("Invalid vendor buffer length")
            result = {"rc": rc, "length": length.value, "data": list(buf[:length.value])}
        else:
            raise ValueError("Unsupported vendor operation")
        result["id"] = req["id"]
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
