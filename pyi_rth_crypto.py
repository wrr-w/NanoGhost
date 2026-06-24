# PyInstaller runtime hook: fix pycryptodome path resolution in frozen env
import os
import sys


def _patch_pycryptodome_filename():
    try:
        from Crypto.Util import _file_system as _cfs
    except ImportError:
        return

    _orig = _cfs.pycryptodome_filename

    def _fixed_pycryptodome_filename(dir_comps, filename):
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            return os.path.join(sys._MEIPASS, *dir_comps, filename)
        return _orig(dir_comps, filename)

    _cfs.pycryptodome_filename = _fixed_pycryptodome_filename


_patch_pycryptodome_filename()
