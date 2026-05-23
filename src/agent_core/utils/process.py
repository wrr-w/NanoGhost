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
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
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
