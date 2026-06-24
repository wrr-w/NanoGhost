import os
import signal
import time
from typing import Optional


def pid_exists(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x0002
        INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == INVALID_HANDLE_VALUE:
            return False

        try:
            pe = PROCESSENTRY32()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if not kernel32.Process32First(snapshot, ctypes.byref(pe)):
                return False
            while True:
                if pe.th32ProcessID == int(pid):
                    return True
                if not kernel32.Process32Next(snapshot, ctypes.byref(pe)):
                    return False
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:
        return False


def terminate_pid(pid: int) -> bool:
    if not pid_exists(pid):
        return True
    if os.name != "nt":
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            return False
        for _ in range(30):
            if not pid_exists(pid):
                return True
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            return False
        return True
    try:
        import ctypes
        PROCESS_TERMINATE = 0x0001
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, 0, int(pid))
        if not h:
            return False
        ctypes.windll.kernel32.TerminateProcess(h, 1)
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return False
