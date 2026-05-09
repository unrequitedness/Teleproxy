"""
Teleproxy — by Nysiusa.

Local MTProto bridge proxy with a customtkinter glassmorphism UI,
system tray, autostart and start-minimized support.
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import queue
import secrets
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

try:
    import pyperclip
except ImportError:
    pyperclip = None

try:
    import customtkinter as ctk
    import tkinter as tk
except ImportError:
    ctk = None
    tk = None

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    import pystray
except ImportError:
    pystray = None

from proxy import __version__, get_link_host
from utils import autostart
from utils.glass import apply_glass
from utils.tray_common import (
    APP_NAME, DEFAULT_CONFIG, IS_FROZEN, LOG_FILE,
    acquire_lock, bootstrap, ensure_dirs, load_config,
    release_lock, restart_proxy, save_config, start_proxy, stop_proxy,
    tg_proxy_url,
)

log = logging.getLogger("tg-ws-tray")


# ---------------------------------------------------------------------------
# Palette — refined glass theme
# ---------------------------------------------------------------------------
PALETTE = {
    "void":         "#0E0B1F",  # deepest, behind glass
    "glass":        "#1A1530",  # opaque card body
    "glass_hi":     "#241C44",  # raised card body
    "glass_lo":     "#15112A",  # recessed surface (inputs)
    "stroke":       "#2E2553",  # subtle border
    "stroke_hi":    "#4B3A8E",  # focused border / tile separator
    "accent":       "#BD93F9",  # primary violet
    "accent_hi":    "#D9C2FF",  # bright hover
    "accent_lo":    "#7E5BD9",
    "accent_dim":   "#5A40A0",
    "success":      "#7CE3A8",
    "warning":      "#FFCB6B",
    "danger":       "#FF7E9C",
    "text":         "#EFEAFF",
    "text_dim":     "#B5ADD4",
    "text_mute":    "#7C7599",
}

APP_TITLE = "Teleproxy"
AUTHOR = "Nysiusa"
ICON_PATH_PRIMARY = str(Path(__file__).parent / "teleproxy_icon.ico")
ICON_PATH_FALLBACK = str(Path(__file__).parent / "icon.ico")


def _icon_path() -> str:
    if Path(ICON_PATH_PRIMARY).exists():
        return ICON_PATH_PRIMARY
    return ICON_PATH_FALLBACK


# ---------------------------------------------------------------------------
# Win32 mutex (single-instance)
# ---------------------------------------------------------------------------
_win_mutex_handle = None
_ERROR_ALREADY_EXISTS = 183


def _acquire_win_mutex() -> Optional[bool]:
    global _win_mutex_handle
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        handle = kernel32.CreateMutexW(None, True, "Local\\Teleproxy_SingleInstance")
        if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return False
        if not handle:
            return None
        _win_mutex_handle = handle
        return True
    except Exception:
        return None


def _release_win_mutex() -> None:
    global _win_mutex_handle
    if _win_mutex_handle:
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.ReleaseMutex(ctypes.c_void_p(_win_mutex_handle))
            kernel32.CloseHandle(ctypes.c_void_p(_win_mutex_handle))
        except Exception:
            pass
        _win_mutex_handle = None


# ---------------------------------------------------------------------------
# Log tailer
# ---------------------------------------------------------------------------
class LogTailer:
    def __init__(self, q: queue.Queue) -> None:
        self.q = q
        self._stop = threading.Event()
        self._th: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._th and self._th.is_alive():
            return
        self._stop.clear()
        self._th = threading.Thread(target=self._run, daemon=True, name="log-tail")
        self._th.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        path = LOG_FILE
        offset = 0
        while not self._stop.is_set():
            try:
                if path.exists():
                    size = path.stat().st_size
                    if size < offset:
                        offset = 0
                    if size > offset:
                        with open(path, "r", encoding="utf-8", errors="replace") as f:
                            f.seek(offset)
                            chunk = f.read()
                            offset = f.tell()
                        for line in chunk.splitlines():
                            self.q.put(line)
            except Exception:
                pass
            time.sleep(0.35)


# ---------------------------------------------------------------------------
# System tray
# ---------------------------------------------------------------------------
class TrayController:
    """Thin wrapper around pystray that runs in its own thread."""

    def __init__(self, on_show, on_toggle, on_exit, get_running) -> None:
        self.on_show = on_show
        self.on_toggle = on_toggle
        self.on_exit = on_exit
        self.get_running = get_running
        self._icon = None
        self._thread: Optional[threading.Thread] = None

    def is_available(self) -> bool:
        return pystray is not None and Image is not None

    def start(self) -> None:
        if self._icon is not None or not self.is_available():
            return
        try:
            img = Image.open(_icon_path())
        except Exception:
            img = Image.new("RGBA", (64, 64), (189, 147, 249, 255))

        def _show(icon, item):  # noqa: ARG001
            self.on_show()

        def _toggle(icon, item):  # noqa: ARG001
            self.on_toggle()

        def _quit(icon, item):  # noqa: ARG001
            try:
                icon.stop()
            except Exception:
                pass
            self.on_exit()

        menu = pystray.Menu(
            pystray.MenuItem("Открыть Teleproxy", _show, default=True),
            pystray.MenuItem(
                "Запущен",
                _toggle,
                checked=lambda _: bool(self.get_running()),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", _quit),
        )
        self._icon = pystray.Icon("Teleproxy", img, "Teleproxy", menu)
        self._thread = threading.Thread(
            target=self._icon.run, daemon=True, name="tray",
        )
        self._thread.start()

    def refresh(self) -> None:
        if self._icon is not None:
            try:
                self._icon.update_menu()
            except Exception:
                pass

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None


# ---------------------------------------------------------------------------
# Glass card helper — frame with subtle border + soft inner highlight stripe
# ---------------------------------------------------------------------------
def make_card(parent, *, body=PALETTE["glass_hi"], stroke=PALETTE["stroke"]):
    return ctk.CTkFrame(
        parent,
        fg_color=body,
        border_color=stroke,
        border_width=1,
        corner_radius=14,
    )


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow:
    def __init__(self, cfg: dict, *, start_minimized: bool = False) -> None:
        self.cfg = cfg
        self.running = False
        self._applied_cfg: dict = {}
        self.log_q: "queue.Queue[str]" = queue.Queue()
        self.tailer = LogTailer(self.log_q)
        self._exiting = False
        self._tray_notice_shown = False

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.root = ctk.CTk()
        self.root.title(APP_TITLE)
        self.root.geometry("900x640")
        self.root.minsize(820, 580)
        self.root.configure(fg_color=PALETTE["void"])
        try:
            self.root.iconbitmap(_icon_path())
        except Exception:
            pass

        # Window translucency for the glass feel.
        try:
            self.root.attributes("-alpha", 0.985)
        except Exception:
            pass

        # Tray needs to be ready before the window is hidden via close-to-tray.
        self.tray = TrayController(
            on_show=self._tray_show,
            on_toggle=self._tray_toggle,
            on_exit=self._real_exit,
            get_running=lambda: self.running,
        )

        self._build_ui()
        self._poll_log()

        # Apply blur after widgets exist so HWND is settled.
        self.root.after(50, lambda: apply_glass(self.root))

        # Start proxy automatically when launched normally.
        self.root.after(200, self._start)

        # Tray
        self.tray.start()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if start_minimized:
            self.root.after(20, self._minimize_to_tray)

    # ---- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        # Header bar
        header = ctk.CTkFrame(self.root, fg_color=PALETTE["glass"], corner_radius=0,
                              border_width=0, height=64)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.pack(fill="both", expand=True, padx=18, pady=10)

        # Logo + name
        ctk.CTkLabel(
            title_row, text="⚡",
            font=("Segoe UI Emoji", 22),
            text_color=PALETTE["accent_hi"],
        ).pack(side="left", padx=(0, 6))
        ctk.CTkLabel(
            title_row, text="Teleproxy",
            font=("Segoe UI Semibold", 22),
            text_color=PALETTE["text"],
        ).pack(side="left")
        ctk.CTkLabel(
            title_row, text=f"v{__version__}",
            font=("Segoe UI", 11),
            text_color=PALETTE["text_mute"],
        ).pack(side="left", padx=(10, 0), pady=(8, 0))

        # Status pill on the right.
        self.status_pill = ctk.CTkLabel(
            title_row,
            text="●  остановлен",
            font=("Segoe UI Semibold", 11),
            text_color=PALETTE["danger"],
            fg_color=PALETTE["glass_lo"],
            corner_radius=12,
            height=28,
        )
        self.status_pill.pack(side="right", ipadx=12, ipady=2)

        # Warning banner (hidden by default).
        self.banner = ctk.CTkLabel(
            self.root, text="",
            font=("Segoe UI Semibold", 11),
            text_color="#1A1630",
            fg_color=PALETTE["warning"],
            corner_radius=0,
            anchor="w", justify="left", height=28,
        )

        # Main body — two cards, glass style.
        body = ctk.CTkFrame(self.root, fg_color=PALETTE["void"], corner_radius=0)
        self._body = body
        body.pack(fill="both", expand=True, padx=14, pady=(10, 6))
        body.grid_columnconfigure(0, weight=0, minsize=380)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        left = make_card(body)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 7))
        right = make_card(body)
        right.grid(row=0, column=1, sticky="nsew", padx=(7, 0))

        self._build_left(left)
        self._build_right(right)

        # Footer
        footer = ctk.CTkFrame(self.root, fg_color=PALETTE["void"],
                              corner_radius=0, height=26)
        footer.pack(fill="x", side="bottom", padx=14, pady=(0, 8))
        ctk.CTkLabel(
            footer,
            text=f"by {AUTHOR}  ·  лог: {LOG_FILE}",
            font=("Segoe UI", 9),
            text_color=PALETTE["text_mute"],
        ).pack(side="left")
        ctk.CTkLabel(
            footer,
            text="Esc — свернуть в трей",
            font=("Segoe UI", 9),
            text_color=PALETTE["text_mute"],
        ).pack(side="right")

        # Esc -> minimize-to-tray.
        self.root.bind("<Escape>", lambda _e: self._minimize_to_tray())

    def _build_left(self, parent) -> None:
        pad = 16

        # Top label
        self._section_label(parent, "ПОДКЛЮЧЕНИЕ").pack(
            fill="x", padx=pad, pady=(pad, 6),
        )

        # host/port row
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=pad, pady=(0, 8))
        row.grid_columnconfigure(0, weight=3)
        row.grid_columnconfigure(1, weight=1)

        self._micro(row, "Адрес").grid(row=0, column=0, sticky="w")
        self._micro(row, "Порт").grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.e_host = self._entry(row, self.cfg.get("host", DEFAULT_CONFIG["host"]))
        self.e_host.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.e_port = self._entry(row, str(self.cfg.get("port", DEFAULT_CONFIG["port"])))
        self.e_port.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(2, 0))

        self.e_host.bind("<KeyRelease>", lambda *_: self._update_banner())
        self.e_port.bind("<KeyRelease>", lambda *_: self._update_banner())

        # secret
        self._micro(parent, "Секрет (32 hex)").pack(fill="x", padx=pad, pady=(8, 2))
        sec_row = ctk.CTkFrame(parent, fg_color="transparent")
        sec_row.pack(fill="x", padx=pad, pady=(0, 4))
        sec_row.grid_columnconfigure(0, weight=1)
        self.e_secret = self._entry(sec_row, self.cfg.get("secret", DEFAULT_CONFIG["secret"]))
        self.e_secret.grid(row=0, column=0, sticky="ew")
        self.e_secret.bind("<KeyRelease>", lambda *_: self._update_banner())
        ctk.CTkButton(
            sec_row, text="Новый", width=72, height=32,
            fg_color=PALETTE["accent_dim"], hover_color=PALETTE["accent_lo"],
            text_color=PALETTE["text"],
            font=("Segoe UI Semibold", 11),
            corner_radius=10,
            command=self._regen_secret,
        ).grid(row=0, column=1, padx=(8, 0))

        # advanced section
        self._section_label(parent, "ДОПОЛНИТЕЛЬНО").pack(
            fill="x", padx=pad, pady=(pad, 4),
        )

        self.var_cf = tk.BooleanVar(value=self.cfg.get("cfproxy", True))
        self.var_cf_prio = tk.BooleanVar(value=self.cfg.get("cfproxy_priority", True))

        self._checkbox(parent, "WebSocket через Cloudflare",
                       self.var_cf).pack(fill="x", padx=pad, pady=(2, 0))
        self._checkbox(parent, "CF раньше прямого TCP",
                       self.var_cf_prio).pack(fill="x", padx=pad, pady=(2, 4))

        # system section
        self._section_label(parent, "СИСТЕМА").pack(
            fill="x", padx=pad, pady=(pad, 4),
        )

        self.var_autostart = tk.BooleanVar(value=autostart.is_enabled())
        self.var_start_minimized = tk.BooleanVar(
            value=bool(self.cfg.get("start_minimized", False)),
        )

        autostart_chk = self._checkbox(
            parent, "Запускать с Windows", self.var_autostart,
            command=self._on_autostart_toggle,
        )
        autostart_chk.pack(fill="x", padx=pad, pady=(2, 0))
        if not autostart.is_supported():
            autostart_chk.configure(
                state="disabled",
                text="Запускать с Windows  (только для .exe)",
            )

        self._checkbox(
            parent, "Стартовать свёрнутым в трей",
            self.var_start_minimized,
            command=self._on_start_minimized_toggle,
        ).pack(fill="x", padx=pad, pady=(2, 8))

        # actions
        self._section_label(parent, "УПРАВЛЕНИЕ").pack(
            fill="x", padx=pad, pady=(pad, 6),
        )

        btn_row_1 = ctk.CTkFrame(parent, fg_color="transparent")
        btn_row_1.pack(fill="x", padx=pad, pady=(0, 6))
        btn_row_1.grid_columnconfigure(0, weight=1)
        btn_row_1.grid_columnconfigure(1, weight=1)

        self.btn_start = ctk.CTkButton(
            btn_row_1, text="Запустить", height=42,
            fg_color=PALETTE["accent"], hover_color=PALETTE["accent_hi"],
            text_color="#1A1630",
            font=("Segoe UI Semibold", 13),
            corner_radius=12,
            command=self._start,
        )
        self.btn_start.grid(row=0, column=0, sticky="ew", padx=(0, 4))

        self.btn_stop = ctk.CTkButton(
            btn_row_1, text="Остановить", height=42,
            fg_color=PALETTE["glass_lo"], hover_color=PALETTE["stroke"],
            text_color=PALETTE["text"],
            font=("Segoe UI Semibold", 13),
            corner_radius=12,
            command=self._stop,
        )
        self.btn_stop.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        ctk.CTkButton(
            parent, text="⚡  Применить и добавить в Telegram", height=44,
            fg_color=PALETTE["accent"], hover_color=PALETTE["accent_hi"],
            text_color="#1A1630",
            font=("Segoe UI Semibold", 12),
            corner_radius=12,
            command=self._apply_and_add,
        ).pack(fill="x", padx=pad, pady=(4, 4))

        ctk.CTkButton(
            parent, text="Открыть в Telegram", height=34,
            fg_color=PALETTE["accent_dim"], hover_color=PALETTE["accent_lo"],
            text_color=PALETTE["text"],
            font=("Segoe UI", 11),
            corner_radius=10,
            command=self._open_in_telegram,
        ).pack(fill="x", padx=pad, pady=(2, 2))

        ctk.CTkButton(
            parent, text="Скопировать ссылку", height=34,
            fg_color=PALETTE["glass_lo"], hover_color=PALETTE["stroke"],
            text_color=PALETTE["text"],
            font=("Segoe UI", 11),
            corner_radius=10,
            command=self._copy_link,
        ).pack(fill="x", padx=pad, pady=(2, 2))

        ctk.CTkButton(
            parent, text="Открыть файл логов", height=30,
            fg_color="transparent", hover_color=PALETTE["glass_lo"],
            text_color=PALETTE["text_dim"],
            font=("Segoe UI", 10),
            corner_radius=8,
            command=self._open_log_file,
        ).pack(fill="x", padx=pad, pady=(4, pad))

    def _build_right(self, parent) -> None:
        pad = 16
        head = ctk.CTkFrame(parent, fg_color="transparent")
        head.pack(fill="x", padx=pad, pady=(pad, 6))
        ctk.CTkLabel(
            head, text="ЖУРНАЛ",
            font=("Segoe UI Semibold", 11),
            text_color=PALETTE["accent"],
        ).pack(side="left")
        ctk.CTkButton(
            head, text="Очистить", width=92, height=28,
            fg_color=PALETTE["glass_lo"], hover_color=PALETTE["stroke"],
            text_color=PALETTE["text_dim"],
            font=("Segoe UI", 10),
            corner_radius=8,
            command=self._clear_log,
        ).pack(side="right")

        self.txt = ctk.CTkTextbox(
            parent,
            fg_color=PALETTE["glass_lo"],
            text_color=PALETTE["text"],
            border_color=PALETTE["stroke"],
            border_width=1,
            corner_radius=10,
            font=("Cascadia Mono", 10),
            wrap="none",
        )
        self.txt.pack(fill="both", expand=True, padx=pad, pady=(0, pad))
        self.txt.configure(state="disabled")

        # link row
        link_card = ctk.CTkFrame(
            parent,
            fg_color=PALETTE["glass_lo"],
            border_color=PALETTE["stroke"],
            border_width=1,
            corner_radius=10,
        )
        link_card.pack(fill="x", padx=pad, pady=(0, pad))

        link_inner = ctk.CTkFrame(link_card, fg_color="transparent")
        link_inner.pack(fill="x", padx=10, pady=8)

        ctk.CTkLabel(
            link_inner, text="Ссылка для Telegram",
            font=("Segoe UI", 9),
            text_color=PALETTE["text_mute"],
        ).pack(anchor="w")
        self.lbl_link = ctk.CTkLabel(
            link_inner, text=self._link(),
            font=("Cascadia Mono", 10),
            text_color=PALETTE["accent_hi"],
            anchor="w", justify="left",
        )
        self.lbl_link.pack(fill="x", anchor="w")

    # ---- helpers ---------------------------------------------------------

    def _section_label(self, parent, text: str):
        return ctk.CTkLabel(
            parent, text=text,
            font=("Segoe UI Semibold", 10),
            text_color=PALETTE["accent"],
            anchor="w",
        )

    def _micro(self, parent, text: str):
        return ctk.CTkLabel(
            parent, text=text,
            font=("Segoe UI", 9),
            text_color=PALETTE["text_mute"],
            anchor="w",
        )

    def _entry(self, parent, value: str):
        e = ctk.CTkEntry(
            parent,
            fg_color=PALETTE["glass_lo"],
            text_color=PALETTE["text"],
            border_color=PALETTE["stroke"],
            border_width=1,
            corner_radius=10,
            font=("Cascadia Mono", 11),
            height=34,
        )
        e.insert(0, value)
        return e

    def _checkbox(self, parent, text: str, var, *, command=None):
        return ctk.CTkCheckBox(
            parent, text=text, variable=var,
            fg_color=PALETTE["accent"],
            hover_color=PALETTE["accent_hi"],
            border_color=PALETTE["stroke_hi"],
            checkmark_color="#1A1630",
            text_color=PALETTE["text"],
            font=("Segoe UI", 11),
            command=command,
        )

    # ---- actions ---------------------------------------------------------

    def _regen_secret(self) -> None:
        self.e_secret.delete(0, "end")
        self.e_secret.insert(0, secrets.token_hex(16))
        self._update_banner()

    def _on_autostart_toggle(self) -> None:
        ok = autostart.set_enabled(bool(self.var_autostart.get()))
        if not ok:
            # Couldn't write registry — revert UI state.
            self.var_autostart.set(autostart.is_enabled())

    def _on_start_minimized_toggle(self) -> None:
        self.cfg["start_minimized"] = bool(self.var_start_minimized.get())
        try:
            save_config(self.cfg)
        except Exception:
            pass

    def _config_dirty(self) -> bool:
        if not self._applied_cfg:
            return False
        cur = self._snapshot_cfg()
        keys = ("host", "port", "secret", "cfproxy", "cfproxy_priority")
        return any(cur.get(k) != self._applied_cfg.get(k) for k in keys)

    def _update_banner(self) -> None:
        if not self._config_dirty():
            try:
                self.banner.pack_forget()
            except Exception:
                pass
            return
        cur = self._snapshot_cfg()
        if cur.get("secret") != self._applied_cfg.get("secret"):
            txt = ("  ⚠  Секрет изменён. Нажмите «Применить и добавить в Telegram», "
                   "чтобы прокси и клиент Telegram использовали один и тот же секрет.")
        else:
            txt = ("  ⚠  Настройки изменены. Нажмите «Применить и добавить в Telegram», "
                   "чтобы прокси перезапустился с новыми значениями.")
        try:
            self.banner.configure(text=txt)
            self.banner.pack(fill="x", side="top", before=self._body)
        except Exception:
            try:
                self.banner.pack(fill="x", side="top")
            except Exception:
                pass

    def _link(self) -> str:
        try:
            return tg_proxy_url(self._snapshot_cfg())
        except Exception:
            return ""

    def _snapshot_cfg(self) -> dict:
        cfg = dict(self.cfg)
        cfg["host"] = self.e_host.get().strip() or DEFAULT_CONFIG["host"]
        try:
            cfg["port"] = int(self.e_port.get().strip() or DEFAULT_CONFIG["port"])
        except ValueError:
            cfg["port"] = DEFAULT_CONFIG["port"]
        sec = self.e_secret.get().strip()
        if sec:
            cfg["secret"] = sec
        cfg["cfproxy"] = bool(self.var_cf.get())
        cfg["cfproxy_priority"] = bool(self.var_cf_prio.get())
        cfg["start_minimized"] = bool(self.var_start_minimized.get())
        return cfg

    def _refresh_link(self) -> None:
        try:
            self.lbl_link.configure(text=self._link())
        except Exception:
            pass

    def _start(self) -> None:
        cfg = self._snapshot_cfg()
        self.cfg.update(cfg)
        try:
            save_config(self.cfg)
        except Exception as exc:
            log.warning("save_config failed: %s", exc)

        def _on_error(msg: str) -> None:
            self.root.after(0, lambda: self._set_status(False, msg))

        if self.running and self._applied_cfg and self._config_dirty():
            log.info("Config changed — restarting proxy")
            stop_proxy()
            time.sleep(0.3)

        start_proxy(self.cfg, _on_error)
        self.tailer.start()
        self._applied_cfg = dict(self.cfg)
        self._refresh_link()
        self._update_banner()
        self._set_status(True)

    def _stop(self) -> None:
        stop_proxy()
        self._applied_cfg = {}
        self._set_status(False)
        self._update_banner()

    def _apply_and_add(self) -> None:
        self._start()
        self.root.after(300, self._open_in_telegram)

    def _set_status(self, running: bool, err: Optional[str] = None) -> None:
        self.running = running
        if err:
            self.status_pill.configure(
                text=f"●  ошибка: {err}",
                text_color=PALETTE["danger"],
            )
        elif running:
            self.status_pill.configure(
                text=f"●  {self.cfg.get('host')}:{self.cfg.get('port')}",
                text_color=PALETTE["success"],
            )
        else:
            self.status_pill.configure(
                text="●  остановлен",
                text_color=PALETTE["danger"],
            )
        try:
            self.tray.refresh()
        except Exception:
            pass

    def _open_in_telegram(self) -> None:
        self._refresh_link()
        url = self._link()
        if not url:
            return
        try:
            webbrowser.open(url)
        except Exception as exc:
            log.warning("Failed to open %s: %s", url, exc)
            self._copy_link()

    def _copy_link(self) -> None:
        self._refresh_link()
        url = self._link()
        if pyperclip is not None:
            try:
                pyperclip.copy(url)
                return
            except Exception:
                pass
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
            self.root.update()
        except Exception:
            pass

    def _open_log_file(self) -> None:
        try:
            if not LOG_FILE.exists():
                LOG_FILE.touch()
            if sys.platform == "win32":
                os.startfile(str(LOG_FILE))  # type: ignore[attr-defined]
            else:
                webbrowser.open(f"file://{LOG_FILE}")
        except Exception as exc:
            log.warning("open log failed: %s", exc)

    def _clear_log(self) -> None:
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    # ---- log pumping -----------------------------------------------------

    def _poll_log(self) -> None:
        if self._exiting:
            return
        drained = 0
        try:
            while drained < 200:
                line = self.log_q.get_nowait()
                self.txt.configure(state="normal")
                self.txt.insert("end", line + "\n")
                self.txt.configure(state="disabled")
                self.txt.see("end")
                drained += 1
        except queue.Empty:
            pass
        self.root.after(120, self._poll_log)

    # ---- tray / close handling ------------------------------------------

    def _minimize_to_tray(self) -> None:
        try:
            self.root.withdraw()
        except Exception:
            pass
        if self.tray.is_available() and not self._tray_notice_shown:
            self._tray_notice_shown = True
            log.info("Teleproxy свёрнут в трей. Кликни иконку чтобы открыть.")

    def _on_close(self) -> None:
        # Crucial UX point: closing the X must NOT kill the proxy when the
        # tray is available — instead hide to tray. Only the tray menu's
        # "Выход" actually exits.
        if self.tray.is_available():
            self._minimize_to_tray()
        else:
            self._real_exit()

    def _tray_show(self) -> None:
        try:
            self.root.after(0, self._restore_window)
        except Exception:
            pass

    def _restore_window(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def _tray_toggle(self) -> None:
        # Run on tk thread.
        def _do():
            if self.running:
                self._stop()
            else:
                self._start()
            self.tray.refresh()
        try:
            self.root.after(0, _do)
        except Exception:
            pass

    def _real_exit(self) -> None:
        if self._exiting:
            return
        self._exiting = True
        try:
            stop_proxy()
        except Exception:
            pass
        try:
            self.tailer.stop()
        except Exception:
            pass
        try:
            self.tray.stop()
        except Exception:
            pass
        try:
            self.root.after(0, self.root.destroy)
        except Exception:
            try:
                self.root.destroy()
            except Exception:
                pass

    def run(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def _parse_args(argv) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="Teleproxy", add_help=True)
    p.add_argument("--minimized", action="store_true",
                   help="Стартовать свёрнутым в трей.")
    return p.parse_known_args(argv)[0]


def run_gui(start_minimized: bool = False) -> None:
    cfg = load_config()
    bootstrap(cfg)
    if ctk is None:
        ctypes.windll.user32.MessageBoxW(
            None,
            "customtkinter не установлен. Переустановите приложение.",
            APP_TITLE, 0x10,
        )
        return
    if start_minimized is False and cfg.get("start_minimized"):
        start_minimized = True
    win = MainWindow(cfg, start_minimized=start_minimized)
    win.run()


def main() -> None:
    args = _parse_args(sys.argv[1:])
    mutex_result = _acquire_win_mutex()
    if mutex_result is False or (mutex_result is None and not acquire_lock()):
        try:
            ctypes.windll.user32.MessageBoxW(
                None, "Приложение уже запущено.", APP_TITLE, 0x40,
            )
        except Exception:
            pass
        return

    try:
        run_gui(start_minimized=args.minimized)
    finally:
        release_lock()
        _release_win_mutex()


if __name__ == "__main__":
    main()
