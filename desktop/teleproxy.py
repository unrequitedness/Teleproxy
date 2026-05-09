"""
Teleproxy — by Nysiusa.

Local MTProto bridge proxy with a modern tabbed glassmorphism UI,
system tray, autostart and start-minimized support.

Performance note: window-level DWM Mica/Acrylic backdrop is intentionally
disabled. It causes severe drag lag on Tk-on-Windows because every pixel of
window movement forces the desktop compositor to re-blur the area underneath.
Glass aesthetic is painted *inside* the UI instead — soft violet backdrop,
translucent-feeling cards with stroke + top highlight.
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
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:
    Image = None
    ImageDraw = None
    ImageFilter = None

try:
    import pystray
except ImportError:
    pystray = None

from proxy import __version__
from utils import autostart
from utils.tray_common import (
    DEFAULT_CONFIG, LOG_FILE,
    acquire_lock, bootstrap, load_config,
    release_lock, save_config, start_proxy, stop_proxy,
    tg_proxy_url,
)

log = logging.getLogger("tg-ws-tray")


# ---------------------------------------------------------------------------
# Glass palette
# ---------------------------------------------------------------------------
PALETTE = {
    # Base layers
    "bg":           "#0B0820",   # solid window bg (no transparency, no blur)
    "card":         "#1A1635",   # raised glass card body
    "card_hi":      "#241D49",   # active / hover card
    "card_lo":      "#13102B",   # input/recessed
    "highlight":    "#2F2557",   # top-edge highlight stripe
    "stroke":       "#322763",   # subtle border
    "stroke_focus": "#7E5BD9",   # focused / accent border
    # Brand
    "accent":       "#BD93F9",
    "accent_hi":    "#DDC4FF",
    "accent_lo":    "#7E5BD9",
    "accent_dim":   "#4D3A85",
    # Status colors
    "ok":           "#7CE3A8",
    "ok_dim":       "#3F8C66",
    "warn":         "#FFCB6B",
    "err":          "#FF7E9C",
    "err_dim":      "#9C3A56",
    # Text
    "text":         "#F2EEFF",
    "text_dim":     "#B5ADD4",
    "text_mute":    "#7C7599",
}

APP_TITLE = "Teleproxy"
AUTHOR = "Nysiusa"
ICON_PRIMARY = str(Path(__file__).parent / "teleproxy_icon.ico")
ICON_FALLBACK = str(Path(__file__).parent / "icon.ico")


def _icon_path() -> str:
    if Path(ICON_PRIMARY).exists():
        return ICON_PRIMARY
    return ICON_FALLBACK


# ---------------------------------------------------------------------------
# Single-instance mutex (Win32)
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
# Backdrop — Pillow-rendered soft gradient with blurry violet highlights.
# Renders ONCE at startup, never on resize/drag, so it adds zero cost
# when the window is moved.
# ---------------------------------------------------------------------------

def render_backdrop(w: int, h: int) -> Optional["Image.Image"]:
    if Image is None:
        return None
    base = Image.new("RGB", (w, h), tuple(int(PALETTE["bg"][i:i+2], 16) for i in (1, 3, 5)))
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    # Big violet top-left blob
    d.ellipse(
        [-w * 0.10, -h * 0.30, w * 0.55, h * 0.45],
        fill=(126, 91, 217, 170),
    )
    # Magenta-ish mid blob
    d.ellipse(
        [w * 0.30, h * 0.30, w * 0.85, h * 0.85],
        fill=(189, 147, 249, 110),
    )
    # Deep blue bottom-right blob
    d.ellipse(
        [w * 0.55, h * 0.55, w * 1.20, h * 1.30],
        fill=(40, 30, 130, 200),
    )
    # Tiny accent
    d.ellipse(
        [w * 0.05, h * 0.55, w * 0.30, h * 0.85],
        fill=(98, 67, 195, 140),
    )
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=140))
    base = base.convert("RGBA")
    base = Image.alpha_composite(base, overlay).convert("RGB")
    return base


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
# Tray
# ---------------------------------------------------------------------------
class TrayController:
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
            pystray.MenuItem("Запущен", _toggle,
                             checked=lambda _: bool(self.get_running())),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", _quit),
        )
        self._icon = pystray.Icon("Teleproxy", img, "Teleproxy", menu)
        self._thread = threading.Thread(target=self._icon.run, daemon=True, name="tray")
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
# Glass card helper.
#
# A "glass" card is a CTkFrame in card body color with a 1px top-edge
# highlight strip. On a violet gradient backdrop this reads as frosted glass
# without any per-frame compositing cost.
# ---------------------------------------------------------------------------
def glass_card(parent, *, padx: int = 0, pady: int = 0,
               body=None, stroke=None) -> "ctk.CTkFrame":
    body = body or PALETTE["card"]
    stroke = stroke or PALETTE["stroke"]
    frame = ctk.CTkFrame(
        parent,
        fg_color=body,
        border_color=stroke,
        border_width=1,
        corner_radius=16,
    )
    # 1-pixel top highlight stripe (purely decorative).
    hi = ctk.CTkFrame(
        frame, fg_color=PALETTE["highlight"],
        corner_radius=0, height=1, border_width=0,
    )
    hi.place(relx=0.04, rely=0.0, relwidth=0.92, y=2)
    return frame


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

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.root = ctk.CTk()
        self.root.title(APP_TITLE)
        self.root.geometry("980x680")
        self.root.minsize(900, 620)
        self.root.configure(fg_color=PALETTE["bg"])
        try:
            self.root.iconbitmap(_icon_path())
        except Exception:
            pass

        # Pre-rendered violet gradient backdrop (rendered once, no DWM blur).
        self._backdrop_ctk: Optional["ctk.CTkImage"] = None
        self._backdrop_label: Optional["ctk.CTkLabel"] = None
        self._setup_backdrop(1280, 900)

        self.tray = TrayController(
            on_show=self._tray_show,
            on_toggle=self._tray_toggle,
            on_exit=self._real_exit,
            get_running=lambda: self.running,
        )

        self._build_ui()
        self._poll_log()

        # Start proxy automatically when not invoked --minimized.
        self.root.after(150, self._start)

        # Tray
        self.tray.start()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Escape>", lambda _e: self._minimize_to_tray())

        if start_minimized:
            self.root.after(20, self._minimize_to_tray)

    # ---- backdrop --------------------------------------------------------
    def _setup_backdrop(self, w: int, h: int) -> None:
        if Image is None:
            return
        try:
            img = render_backdrop(w, h)
            if img is None:
                return
            self._backdrop_ctk = ctk.CTkImage(light_image=img, dark_image=img,
                                              size=(w, h))
            self._backdrop_label = ctk.CTkLabel(
                self.root, text="", image=self._backdrop_ctk,
                fg_color=PALETTE["bg"],
            )
            self._backdrop_label.place(relx=0.5, rely=0.5, anchor="center")
            self._backdrop_label.lower()
        except Exception as exc:
            log.debug("backdrop render failed: %s", exc)

    # ---- top-level layout ------------------------------------------------
    def _build_ui(self) -> None:
        # Header — simple, no compositing.
        header = ctk.CTkFrame(self.root, fg_color="transparent", height=64)
        header.pack(fill="x", side="top", padx=20, pady=(14, 0))
        header.pack_propagate(False)

        ctk.CTkLabel(
            header, text="⚡",
            font=("Segoe UI Emoji", 24),
            text_color=PALETTE["accent_hi"],
        ).pack(side="left", padx=(0, 4))
        ctk.CTkLabel(
            header, text="Teleproxy",
            font=("Segoe UI Semibold", 22),
            text_color=PALETTE["text"],
        ).pack(side="left")
        ctk.CTkLabel(
            header, text=f"v{__version__}",
            font=("Segoe UI", 11),
            text_color=PALETTE["text_mute"],
        ).pack(side="left", padx=(10, 0), pady=(8, 0))

        # Right: status pill + minimize button
        right = ctk.CTkFrame(header, fg_color="transparent")
        right.pack(side="right")

        self.status_pill = ctk.CTkLabel(
            right, text="●  остановлен",
            font=("Segoe UI Semibold", 11),
            text_color=PALETTE["err"],
            fg_color=PALETTE["card_lo"],
            corner_radius=14,
            height=28,
        )
        self.status_pill.pack(side="left", ipadx=14, ipady=2)

        ctk.CTkButton(
            right, text="—", width=34, height=28,
            fg_color=PALETTE["card_lo"], hover_color=PALETTE["card"],
            text_color=PALETTE["text_dim"],
            font=("Segoe UI", 14),
            corner_radius=12,
            command=self._minimize_to_tray,
        ).pack(side="left", padx=(8, 0))

        # Tabs (segmented button)
        tab_wrap = ctk.CTkFrame(self.root, fg_color="transparent")
        tab_wrap.pack(fill="x", padx=20, pady=(12, 0))

        self.tabs = ctk.CTkSegmentedButton(
            tab_wrap,
            values=["Главная", "Настройки", "Журнал"],
            fg_color=PALETTE["card_lo"],
            selected_color=PALETTE["accent"],
            selected_hover_color=PALETTE["accent_hi"],
            unselected_color=PALETTE["card_lo"],
            unselected_hover_color=PALETTE["card"],
            text_color="#1A1630",
            font=("Segoe UI Semibold", 12),
            corner_radius=14,
            command=self._on_tab_changed,
            height=38,
        )
        self.tabs.pack(fill="x", pady=4)
        self.tabs.set("Главная")

        # Content stack
        self._stack = ctk.CTkFrame(self.root, fg_color="transparent")
        self._stack.pack(fill="both", expand=True, padx=20, pady=(12, 0))

        self._tab_home = self._build_home_tab()
        self._tab_settings = self._build_settings_tab()
        self._tab_log = self._build_log_tab()
        self._on_tab_changed("Главная")

        # Footer
        footer = ctk.CTkFrame(self.root, fg_color="transparent", height=28)
        footer.pack(fill="x", side="bottom", padx=20, pady=(6, 10))
        ctk.CTkLabel(
            footer,
            text=f"by {AUTHOR}",
            font=("Segoe UI", 10),
            text_color=PALETTE["text_mute"],
        ).pack(side="left")
        ctk.CTkLabel(
            footer,
            text="Esc / крестик — свернуть в трей",
            font=("Segoe UI", 10),
            text_color=PALETTE["text_mute"],
        ).pack(side="right")

    # ---- HOME tab --------------------------------------------------------
    def _build_home_tab(self) -> "ctk.CTkFrame":
        page = ctk.CTkFrame(self._stack, fg_color="transparent")
        page.grid_rowconfigure(1, weight=1)
        page.grid_columnconfigure(0, weight=1)

        # Hero card with status orb + power button.
        hero = glass_card(page)
        hero.grid(row=0, column=0, sticky="ew", padx=0, pady=(0, 12))

        inner = ctk.CTkFrame(hero, fg_color="transparent")
        inner.pack(fill="x", padx=24, pady=22)

        # Status orb (color label that changes)
        self.orb = ctk.CTkLabel(
            inner, text="◉",
            font=("Segoe UI Symbol", 56),
            text_color=PALETTE["err_dim"],
            fg_color="transparent",
            width=72, height=72,
        )
        self.orb.pack(side="left", padx=(0, 18))

        text_col = ctk.CTkFrame(inner, fg_color="transparent")
        text_col.pack(side="left", fill="x", expand=True)

        self.hero_title = ctk.CTkLabel(
            text_col, text="Прокси остановлен",
            font=("Segoe UI Semibold", 22),
            text_color=PALETTE["text"], anchor="w",
        )
        self.hero_title.pack(anchor="w")

        self.hero_subtitle = ctk.CTkLabel(
            text_col,
            text="Нажми кнопку справа, чтобы запустить.",
            font=("Segoe UI", 11),
            text_color=PALETTE["text_dim"], anchor="w",
        )
        self.hero_subtitle.pack(anchor="w", pady=(4, 0))

        # Round power button
        self.power_btn = ctk.CTkButton(
            inner, text="⏻", width=80, height=80,
            fg_color=PALETTE["accent"],
            hover_color=PALETTE["accent_hi"],
            text_color="#1A1630",
            font=("Segoe UI Symbol", 30),
            corner_radius=80,  # circle
            command=self._toggle_power,
        )
        self.power_btn.pack(side="right")

        # Action tiles
        actions = ctk.CTkFrame(page, fg_color="transparent")
        actions.grid(row=1, column=0, sticky="nsew")
        for i in range(3):
            actions.grid_columnconfigure(i, weight=1, uniform="tile")
        actions.grid_rowconfigure(0, weight=0)
        actions.grid_rowconfigure(1, weight=1)

        self._action_tile(
            actions, row=0, col=0,
            icon="📲", title="Открыть в Telegram",
            sub="Применить настройки и пробросить ссылку в клиент",
            primary=True,
            command=self._apply_and_add,
        )
        self._action_tile(
            actions, row=0, col=1,
            icon="🔗", title="Скопировать ссылку",
            sub="tg://proxy?... в буфер обмена",
            command=self._copy_link,
        )
        self._action_tile(
            actions, row=0, col=2,
            icon="📂", title="Открыть лог",
            sub=str(LOG_FILE),
            command=self._open_log_file,
        )

        # Link banner
        link_card = glass_card(actions, body=PALETTE["card_lo"])
        link_card.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ctk.CTkLabel(
            link_card, text="ССЫЛКА ДЛЯ TELEGRAM",
            font=("Segoe UI Semibold", 9),
            text_color=PALETTE["accent"], anchor="w",
        ).pack(fill="x", padx=14, pady=(10, 2))
        self.lbl_link = ctk.CTkLabel(
            link_card, text=self._link(),
            font=("Cascadia Mono", 11),
            text_color=PALETTE["accent_hi"], anchor="w", justify="left",
        )
        self.lbl_link.pack(fill="x", padx=14, pady=(0, 12))

        return page

    def _action_tile(self, parent, *, row: int, col: int,
                     icon: str, title: str, sub: str,
                     command, primary: bool = False) -> None:
        body = PALETTE["card_hi"] if primary else PALETTE["card"]
        tile = glass_card(parent, body=body,
                          stroke=PALETTE["stroke_focus"] if primary else PALETTE["stroke"])
        tile.grid(row=row, column=col, sticky="nsew",
                  padx=(0 if col == 0 else 6, 0 if col == 2 else 6))

        # Whole-tile click: bind through inner content
        inner = ctk.CTkFrame(tile, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=14, pady=14)

        ctk.CTkLabel(
            inner, text=icon,
            font=("Segoe UI Emoji", 22),
            text_color=PALETTE["accent_hi"] if primary else PALETTE["accent"],
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            inner, text=title,
            font=("Segoe UI Semibold", 13),
            text_color=PALETTE["text"], anchor="w", justify="left",
        ).pack(anchor="w", pady=(6, 0))
        ctk.CTkLabel(
            inner, text=sub,
            font=("Segoe UI", 10),
            text_color=PALETTE["text_dim"], anchor="w", justify="left",
            wraplength=220,
        ).pack(anchor="w", pady=(2, 0))

        for w in (tile, inner) + tuple(inner.winfo_children()):
            w.bind("<Button-1>", lambda _e, c=command: c())

    # ---- SETTINGS tab ----------------------------------------------------
    def _build_settings_tab(self) -> "ctk.CTkFrame":
        page = ctk.CTkFrame(self._stack, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_columnconfigure(1, weight=1)

        # --- Connection card -----------------------------------------------
        conn = glass_card(page)
        conn.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 0))
        self._section_title(conn, "ПОДКЛЮЧЕНИЕ")

        # host/port row
        row = ctk.CTkFrame(conn, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(0, 4))
        row.grid_columnconfigure(0, weight=3)
        row.grid_columnconfigure(1, weight=1)
        self._micro(row, "Адрес").grid(row=0, column=0, sticky="w")
        self._micro(row, "Порт").grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.e_host = self._entry(row, self.cfg.get("host", DEFAULT_CONFIG["host"]))
        self.e_host.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.e_port = self._entry(row, str(self.cfg.get("port", DEFAULT_CONFIG["port"])))
        self.e_port.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(2, 0))
        self.e_host.bind("<KeyRelease>", lambda *_: self._on_cfg_dirty())
        self.e_port.bind("<KeyRelease>", lambda *_: self._on_cfg_dirty())

        # secret
        self._micro(conn, "Секрет (32 hex)").pack(fill="x", padx=18, pady=(10, 2))
        sec_row = ctk.CTkFrame(conn, fg_color="transparent")
        sec_row.pack(fill="x", padx=18, pady=(0, 4))
        sec_row.grid_columnconfigure(0, weight=1)
        self.e_secret = self._entry(sec_row, self.cfg.get("secret", DEFAULT_CONFIG["secret"]))
        self.e_secret.grid(row=0, column=0, sticky="ew")
        self.e_secret.bind("<KeyRelease>", lambda *_: self._on_cfg_dirty())
        ctk.CTkButton(
            sec_row, text="Новый", width=80, height=32,
            fg_color=PALETTE["accent_dim"], hover_color=PALETTE["accent_lo"],
            text_color=PALETTE["text"],
            font=("Segoe UI Semibold", 11),
            corner_radius=10,
            command=self._regen_secret,
        ).grid(row=0, column=1, padx=(8, 0))

        # network toggles
        self._section_title(conn, "СЕТЬ", pady=(20, 4))
        self.var_cf = tk.BooleanVar(value=self.cfg.get("cfproxy", True))
        self.var_cf_prio = tk.BooleanVar(value=self.cfg.get("cfproxy_priority", True))
        self._switch(conn, "WebSocket через Cloudflare",
                     "Маскирует трафик под HTTPS — обходит блокировку прямых DC IP",
                     self.var_cf, command=self._on_cfg_dirty,
                     ).pack(fill="x", padx=18, pady=(2, 0))
        self._switch(conn, "CF раньше прямого TCP",
                     "Сначала пробовать Cloudflare, потом TCP",
                     self.var_cf_prio, command=self._on_cfg_dirty,
                     ).pack(fill="x", padx=18, pady=(8, 14))

        # Apply button at bottom
        ctk.CTkButton(
            conn, text="Применить и перезапустить", height=38,
            fg_color=PALETTE["accent"], hover_color=PALETTE["accent_hi"],
            text_color="#1A1630",
            font=("Segoe UI Semibold", 12),
            corner_radius=12,
            command=self._start,
        ).pack(fill="x", padx=18, pady=(0, 16))

        # --- Startup card --------------------------------------------------
        startup = glass_card(page)
        startup.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self._section_title(startup, "ЗАПУСК С WINDOWS")

        ctk.CTkLabel(
            startup,
            text=("Если включить — Teleproxy будет автоматически стартовать при\n"
                  "входе в Windows и сразу уходить в системный трей. Само окно\n"
                  "выскакивать не будет, прокси сразу начнёт работать в фоне."),
            font=("Segoe UI", 11),
            text_color=PALETTE["text_dim"],
            justify="left", anchor="w",
        ).pack(fill="x", padx=18, pady=(0, 12))

        self.var_autostart = tk.BooleanVar(value=autostart.is_enabled())
        self.var_start_minimized = tk.BooleanVar(
            value=bool(self.cfg.get("start_minimized", False)),
        )

        autostart_sw = self._switch(
            startup, "Автозапуск с Windows",
            "Запись в HKCU\\…\\Run с флагом --minimized",
            self.var_autostart, command=self._on_autostart_toggle,
        )
        autostart_sw.pack(fill="x", padx=18, pady=(2, 4))
        if not autostart.is_supported():
            for child in autostart_sw.winfo_children():
                try:
                    child.configure(state="disabled")
                except Exception:
                    pass
            ctk.CTkLabel(
                startup,
                text="⚠  Автозапуск работает только из собранного .exe",
                font=("Segoe UI", 10),
                text_color=PALETTE["warn"], anchor="w",
            ).pack(fill="x", padx=18, pady=(0, 4))

        self._switch(
            startup, "Стартовать сразу в трее",
            "Окно не появляется при запуске, прокси сразу в фоне",
            self.var_start_minimized, command=self._on_start_minimized_toggle,
        ).pack(fill="x", padx=18, pady=(2, 14))

        # Big primary action — minimize to tray now
        ctk.CTkButton(
            startup, text="Свернуть в трей сейчас", height=40,
            fg_color=PALETTE["accent"], hover_color=PALETTE["accent_hi"],
            text_color="#1A1630",
            font=("Segoe UI Semibold", 12),
            corner_radius=12,
            command=self._minimize_to_tray,
        ).pack(fill="x", padx=18, pady=(0, 8))

        ctk.CTkButton(
            startup, text="Полностью выйти", height=34,
            fg_color=PALETTE["card_lo"], hover_color=PALETTE["card"],
            text_color=PALETTE["text_dim"],
            font=("Segoe UI", 11),
            corner_radius=10,
            command=self._real_exit,
        ).pack(fill="x", padx=18, pady=(0, 16))

        return page

    # ---- LOG tab ---------------------------------------------------------
    def _build_log_tab(self) -> "ctk.CTkFrame":
        page = ctk.CTkFrame(self._stack, fg_color="transparent")
        page.grid_rowconfigure(0, weight=1)
        page.grid_columnconfigure(0, weight=1)

        card = glass_card(page)
        card.grid(row=0, column=0, sticky="nsew")

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(14, 6))
        ctk.CTkLabel(
            head, text="ЖУРНАЛ",
            font=("Segoe UI Semibold", 10),
            text_color=PALETTE["accent"],
        ).pack(side="left")
        ctk.CTkButton(
            head, text="Очистить", width=92, height=28,
            fg_color=PALETTE["card_lo"], hover_color=PALETTE["card"],
            text_color=PALETTE["text_dim"],
            font=("Segoe UI", 10),
            corner_radius=8,
            command=self._clear_log,
        ).pack(side="right")

        self.txt = ctk.CTkTextbox(
            card,
            fg_color=PALETTE["card_lo"],
            text_color=PALETTE["text"],
            border_color=PALETTE["stroke"],
            border_width=1,
            corner_radius=10,
            font=("Cascadia Mono", 10),
            wrap="none",
        )
        self.txt.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        self.txt.configure(state="disabled")

        return page

    # ---- shared widget helpers ------------------------------------------
    def _section_title(self, parent, text: str, pady=(14, 4)):
        return ctk.CTkLabel(
            parent, text=text,
            font=("Segoe UI Semibold", 10),
            text_color=PALETTE["accent"], anchor="w",
        ).pack(fill="x", padx=18, pady=pady)

    def _micro(self, parent, text: str):
        return ctk.CTkLabel(
            parent, text=text,
            font=("Segoe UI", 9),
            text_color=PALETTE["text_mute"], anchor="w",
        )

    def _entry(self, parent, value: str):
        e = ctk.CTkEntry(
            parent,
            fg_color=PALETTE["card_lo"],
            text_color=PALETTE["text"],
            border_color=PALETTE["stroke"],
            border_width=1,
            corner_radius=10,
            font=("Cascadia Mono", 11),
            height=34,
        )
        e.insert(0, value)
        return e

    def _switch(self, parent, label: str, sub: str, var, *, command=None) -> "ctk.CTkFrame":
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        text_col = ctk.CTkFrame(wrap, fg_color="transparent")
        text_col.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            text_col, text=label,
            font=("Segoe UI Semibold", 12),
            text_color=PALETTE["text"], anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            text_col, text=sub,
            font=("Segoe UI", 10),
            text_color=PALETTE["text_mute"], anchor="w", justify="left",
            wraplength=300,
        ).pack(anchor="w", pady=(2, 0))

        sw = ctk.CTkSwitch(
            wrap, text="", variable=var,
            progress_color=PALETTE["accent"],
            button_color="#FFFFFF",
            button_hover_color="#FFFFFF",
            fg_color=PALETTE["card_lo"],
            command=command,
            switch_width=44, switch_height=22,
        )
        sw.pack(side="right", padx=(8, 0))
        return wrap

    # ---- tabs ------------------------------------------------------------
    def _on_tab_changed(self, value: str) -> None:
        for t in (self._tab_home, self._tab_settings, self._tab_log):
            try:
                t.pack_forget()
            except Exception:
                pass
        target = {
            "Главная":   self._tab_home,
            "Настройки": self._tab_settings,
            "Журнал":    self._tab_log,
        }.get(value, self._tab_home)
        target.pack(fill="both", expand=True)

    # ---- proxy actions ---------------------------------------------------
    def _toggle_power(self) -> None:
        if self.running:
            self._stop()
        else:
            self._start()

    def _regen_secret(self) -> None:
        self.e_secret.delete(0, "end")
        self.e_secret.insert(0, secrets.token_hex(16))
        self._on_cfg_dirty()

    def _on_cfg_dirty(self) -> None:
        # Subtitle hint when secret changed while running.
        if self.running and self._applied_cfg:
            cur = self._snapshot_cfg()
            if cur.get("secret") != self._applied_cfg.get("secret"):
                self._set_subtitle(
                    "⚠  Секрет изменён. Жми «Применить и перезапустить»."
                )

    def _on_autostart_toggle(self) -> None:
        ok = autostart.set_enabled(bool(self.var_autostart.get()))
        if not ok:
            self.var_autostart.set(autostart.is_enabled())

    def _on_start_minimized_toggle(self) -> None:
        self.cfg["start_minimized"] = bool(self.var_start_minimized.get())
        try:
            save_config(self.cfg)
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

    def _config_dirty(self) -> bool:
        if not self._applied_cfg:
            return False
        cur = self._snapshot_cfg()
        keys = ("host", "port", "secret", "cfproxy", "cfproxy_priority")
        return any(cur.get(k) != self._applied_cfg.get(k) for k in keys)

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
        self._set_status(True)

    def _stop(self) -> None:
        stop_proxy()
        self._applied_cfg = {}
        self._set_status(False)

    def _apply_and_add(self) -> None:
        self._start()
        self.root.after(280, self._open_in_telegram)

    def _set_status(self, running: bool, err: Optional[str] = None) -> None:
        self.running = running
        if err:
            self.status_pill.configure(
                text=f"●  ошибка: {err}", text_color=PALETTE["err"],
            )
            self.orb.configure(text_color=PALETTE["err"])
            self.hero_title.configure(text="Ошибка запуска")
            self._set_subtitle(err)
            self.power_btn.configure(
                fg_color=PALETTE["accent"],
                hover_color=PALETTE["accent_hi"],
            )
        elif running:
            ep = f"{self.cfg.get('host')}:{self.cfg.get('port')}"
            self.status_pill.configure(text=f"●  {ep}",
                                       text_color=PALETTE["ok"])
            self.orb.configure(text_color=PALETTE["ok"])
            self.hero_title.configure(text="Прокси работает")
            self._set_subtitle(
                f"Слушаем {ep}. Жми тайл «Открыть в Telegram», чтобы пробросить ссылку.",
            )
            # Power button -> "stop" tone
            self.power_btn.configure(
                fg_color=PALETTE["err_dim"],
                hover_color=PALETTE["err"],
            )
        else:
            self.status_pill.configure(text="●  остановлен",
                                       text_color=PALETTE["err"])
            self.orb.configure(text_color=PALETTE["err_dim"])
            self.hero_title.configure(text="Прокси остановлен")
            self._set_subtitle("Нажми кнопку справа, чтобы запустить.")
            self.power_btn.configure(
                fg_color=PALETTE["accent"],
                hover_color=PALETTE["accent_hi"],
            )
        try:
            self.tray.refresh()
        except Exception:
            pass

    def _set_subtitle(self, text: str) -> None:
        try:
            self.hero_subtitle.configure(text=text)
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

    # ---- log polling ----------------------------------------------------
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
        self.root.after(140, self._poll_log)

    # ---- tray / lifecycle ----------------------------------------------
    def _minimize_to_tray(self) -> None:
        try:
            self.root.withdraw()
        except Exception:
            pass

    def _on_close(self) -> None:
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
        try:
            ctypes.windll.user32.MessageBoxW(
                None, "customtkinter не установлен. Переустановите приложение.",
                APP_TITLE, 0x10,
            )
        except Exception:
            pass
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
