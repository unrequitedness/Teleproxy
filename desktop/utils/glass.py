"""
Glassmorphism / blur backdrop helpers for Tk windows on Windows.

Applies Mica (Win 11 22H2+) → MicaAlt → Acrylic → BlurBehind in that order
of preference. Silent no-op on non-Windows / unsupported builds.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
from typing import Optional

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35

DWMSBT_AUTO = 0
DWMSBT_NONE = 1
DWMSBT_MAINWINDOW = 2          # Mica
DWMSBT_TRANSIENTWINDOW = 3     # Acrylic
DWMSBT_TABBEDWINDOW = 4        # MicaAlt


class _AccentPolicy(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_uint),
        ("AccentFlags", ctypes.c_uint),
        ("GradientColor", ctypes.c_uint),
        ("AnimationId", ctypes.c_uint),
    ]


class _WinCompAttrData(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.POINTER(_AccentPolicy)),
        ("SizeOfData", ctypes.c_size_t),
    ]


_ACCENT_DISABLED = 0
_ACCENT_ENABLE_BLURBEHIND = 3
_ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
_WCA_ACCENT_POLICY = 19


def _hwnd_for(tk_root) -> Optional[int]:
    try:
        # On Windows, tk's winfo_id is the child HWND; we need the toplevel one.
        child_hwnd = int(tk_root.winfo_id())
        try:
            tk_root.update_idletasks()
        except Exception:
            pass
        parent = ctypes.windll.user32.GetAncestor(child_hwnd, 2)  # GA_ROOT
        return int(parent or child_hwnd)
    except Exception:
        return None


def _set_dark_titlebar(hwnd: int, dark: bool = True) -> None:
    val = ctypes.c_int(1 if dark else 0)
    try:
        hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wt.HWND(hwnd), DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(val), ctypes.sizeof(val),
        )
        if hr != 0:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                wt.HWND(hwnd), DWMWA_USE_IMMERSIVE_DARK_MODE_OLD,
                ctypes.byref(val), ctypes.sizeof(val),
            )
    except Exception:
        pass


def _set_border_color(hwnd: int, argb: int) -> None:
    """argb: 0x00BBGGRR (high byte ignored)."""
    try:
        val = ctypes.c_uint(argb)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wt.HWND(hwnd), DWMWA_BORDER_COLOR,
            ctypes.byref(val), ctypes.sizeof(val),
        )
    except Exception:
        pass


def _try_mica(hwnd: int, alt: bool = False) -> bool:
    val = ctypes.c_int(DWMSBT_TABBEDWINDOW if alt else DWMSBT_MAINWINDOW)
    try:
        hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wt.HWND(hwnd), DWMWA_SYSTEMBACKDROP_TYPE,
            ctypes.byref(val), ctypes.sizeof(val),
        )
        return hr == 0
    except Exception:
        return False


def _try_accent(hwnd: int, accent_state: int, gradient_argb: int = 0) -> bool:
    try:
        user32 = ctypes.windll.user32
        try:
            user32.SetWindowCompositionAttribute.argtypes = [
                wt.HWND, ctypes.POINTER(_WinCompAttrData),
            ]
            user32.SetWindowCompositionAttribute.restype = ctypes.c_int
        except Exception:
            pass
        policy = _AccentPolicy(accent_state, 2, gradient_argb, 0)
        data = _WinCompAttrData(
            _WCA_ACCENT_POLICY, ctypes.pointer(policy), ctypes.sizeof(policy),
        )
        hr = user32.SetWindowCompositionAttribute(wt.HWND(hwnd), ctypes.byref(data))
        return hr != 0
    except Exception:
        return False


def apply_glass(tk_root, *, dark: bool = True, accent_argb: int = 0x60101020,
                border_argb: int = 0x00BD93F9) -> str:
    """Apply best-available blur backdrop to *tk_root*.

    Returns one of: "mica", "mica-alt", "acrylic", "blur", "none".
    """
    if sys.platform != "win32":
        return "none"
    hwnd = _hwnd_for(tk_root)
    if not hwnd:
        return "none"

    _set_dark_titlebar(hwnd, dark)
    if border_argb is not None:
        _set_border_color(hwnd, border_argb)

    if _try_mica(hwnd, alt=False):
        return "mica"
    if _try_mica(hwnd, alt=True):
        return "mica-alt"
    if _try_accent(hwnd, _ACCENT_ENABLE_ACRYLICBLURBEHIND, accent_argb):
        return "acrylic"
    if _try_accent(hwnd, _ACCENT_ENABLE_BLURBEHIND, 0):
        return "blur"
    return "none"
