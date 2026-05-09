"""
Windows autostart toggle. Writes/removes
HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\Teleproxy.

Only meaningful in the frozen (.exe) build — running from a python
interpreter wouldn't survive a reboot anyway, so we silently no-op.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_REG_VALUE_NAME = "Teleproxy"


def _exe_path() -> Optional[Path]:
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve()


def is_supported() -> bool:
    return sys.platform == "win32" and _exe_path() is not None


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH) as k:
            try:
                value, _ = winreg.QueryValueEx(k, _REG_VALUE_NAME)
            except FileNotFoundError:
                return False
            return bool(value)
    except OSError:
        return False


def set_enabled(enabled: bool) -> bool:
    """Returns True on success."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _REG_PATH, 0, winreg.KEY_SET_VALUE,
        ) as k:
            if enabled:
                exe = _exe_path()
                if exe is None:
                    return False
                cmd = f'"{exe}" --minimized'
                winreg.SetValueEx(k, _REG_VALUE_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, _REG_VALUE_NAME)
                except FileNotFoundError:
                    pass
            return True
    except OSError:
        return False
