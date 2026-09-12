import os
import sys


def current_version() -> str:
    if getattr(sys, "frozen", False):
        try:
            path = os.path.join(sys._MEIPASS, "VERSION")
            if os.path.isfile(path):
                return open(path, encoding="utf-8").read().strip()
        except Exception:
            pass
        exe_dir = os.path.dirname(sys.executable)
        path = os.path.join(exe_dir, "VERSION")
        if os.path.isfile(path):
            return open(path, encoding="utf-8").read().strip()
    for base in [
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        os.getcwd(),
    ]:
        path = os.path.join(base, "VERSION")
        if os.path.isfile(path):
            return open(path, encoding="utf-8").read().strip()
    return "0.0.0"


def compare_versions(v1: str, v2: str) -> int:
    try:
        parts1 = [int(x) for x in v1.split(".")]
        parts2 = [int(x) for x in v2.split(".")]
        for a, b in zip(parts1, parts2):
            if a < b:
                return -1
            if a > b:
                return 1
        if len(parts1) < len(parts2):
            return -1
        if len(parts1) > len(parts2):
            return 1
        return 0
    except Exception:
        return 0 if v1 == v2 else (-1 if v1 < v2 else 1)
