#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RFID RAW DATA CONSOLE OUTPUT — NO LOGIC, JUST PRINT
"""
import ctypes
import time
import os
import sys

DLL_NAME = "UHFAPI.dll"
CONFIG_FILE = "ipConfig.txt"

def load_config(path=CONFIG_FILE):
    cfg = {"ip": None, "port": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if "=" in ln and not ln.startswith("#"):
                    k, v = ln.split("=", 1)
                    cfg[k.strip()] = v.strip()
    except: pass
    return cfg["ip"], cfg["port"]

def parse_buf(buf, length):
    """Парсит буфер ТОЧНО как C# uhfGetReceived()"""
    if length < 8: return None
    try:
        # 🔥 Конвертируем signed c_ubyte -> unsigned int
        d = [b & 0xFF for b in buf[:length]]
        uii_len = d[0]
        if uii_len < 3 or uii_len + 1 >= length: return None
        
        tid_len = d[uii_len + 1]
        tid_start = uii_len + 2
        if tid_start + tid_len > length: tid_len = length - tid_start
        
        rssi_idx = tid_start + tid_len
        if rssi_idx + 3 > length: return None  # +3: 2 bytes RSSI + 1 ant
        
        ant_idx = rssi_idx + 2
        
        # EPC: пропускаем 3 байта, берём (uii_len*2 - 4) hex-символов
        epc_start = 3
        epc_hex_len = uii_len * 2 - 4
        epc_end = epc_start + (epc_hex_len // 2)
        if epc_end > len(d): return None
        epc = "".join(f"{b:02X}" for b in d[epc_start:epc_end])
        
        # TID
        tid = "".join(f"{b:02X}" for b in d[tid_start:tid_start+tid_len]) if tid_len > 0 else ""
        
        # RSSI: big-endian uint16, формула (val - 65535) / 10.0
        rssi_raw = (d[rssi_idx] << 8) | d[rssi_idx + 1]
        rssi = (rssi_raw - 65535) / 10.0
        
        ant = d[ant_idx]
        return {"epc": epc, "tid": tid, "rssi": f"{rssi:.1f}", "ant": str(ant)}
    except: return None

def main():
    ip, port = load_config()
    if not ip or not port:
        print(f"[!] Нет IP:порт в {CONFIG_FILE}", file=sys.stderr)
        return 1
    port = int(port)
    
    dll_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DLL_NAME)
    if not os.path.exists(dll_path):
        print(f"[!] DLL не найдена: {dll_path}", file=sys.stderr)
        return 1
    
    try:
        lib = ctypes.CDLL(dll_path)
    except Exception as e:
        print(f"[!] Ошибка загрузки DLL: {e}", file=sys.stderr)
        print("    Python и DLL должны быть одной разрядности (32/64)!", file=sys.stderr)
        return 1
    
    # === Прототипы функций ===
    lib.TCPConnect.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    lib.TCPConnect.restype = ctypes.c_int
    lib.TCPDisconnect.argtypes = []
    lib.TCPDisconnect.restype = None
    lib.UHFInventory.argtypes = []
    lib.UHFInventory.restype = ctypes.c_int
    lib.UHFStopGet.argtypes = []
    lib.UHFStopGet.restype = ctypes.c_int
    lib.UHF_GetReceived_EX.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ubyte * 512)
    ]
    lib.UHF_GetReceived_EX.restype = ctypes.c_int
    
    # === Подключение ===
    print(f"[*] Подключение к {ip}:{port}...", file=sys.stderr)
    if lib.TCPConnect(ip.encode(), port) != 0:
        print("[!] Не удалось подключиться", file=sys.stderr)
        return 1
    print("[+] Подключено", file=sys.stderr)
    
    # === Заголовок таблицы ===
    print("\n" + "="*85)
    print(f"{'ВРЕМЯ':<10} {'АНТ':<4} {'RSSI':<8} {'EPC':<42} {'TID':<24}")
    print("="*85)
    
    # === Старт инвентаризации ===
    lib.UHFInventory()
    
    try:
        while True:
            uLen = ctypes.c_int(0)
            buf = (ctypes.c_ubyte * 512)()
            res = lib.UHF_GetReceived_EX(ctypes.byref(uLen), ctypes.byref(buf))
            
            if res == 0 and uLen.value > 0:
                tag = parse_buf(buf, uLen.value)
                if tag:
                    ts = time.strftime("%H:%M:%S")
                    print(f"{ts:<10} {tag['ant']:<4} {tag['rssi']:<8} {tag['epc']:<42} {tag['tid']:<24}")
                    sys.stdout.flush()
            time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        lib.UHFStopGet()
        lib.TCPDisconnect()
        print("\n[+] Отключено", file=sys.stderr)
    return 0

if __name__ == "__main__":
    sys.exit(main())