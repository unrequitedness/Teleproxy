"""
Teleproxy — by smokinghazy.

Local MTProto bridge proxy with a customtkinter main-window UI.
"""
from __future__ import annotations

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

from proxy import __version__, get_link_host
from utils.tray_common import (
    APP_NAME, DEFAULT_CONFIG, IS_FROZEN, LOG_FILE,
    acquire_lock, bootstrap, ensure_dirs, load_config,
    release_lock, restart_proxy, save_config, start_proxy, stop_proxy,
    tg_proxy_url,
)

log = logging.getLogger("tg-ws-tray")


# ---------------------------------------------------------------------------
# Palette — violet/purple theme (different from upstream's cyan)
# ---------------------------------------------------------------------------
PALETTE = {
    "bg":           "#1A1630",
    "panel":        "#241C44",
    "field":        "#2F2657",
    "field_border": "#3F3475",
    "accent":       "#BD93F9",   # light violet
    "accent_hi":    "#D3B3FF",
    "accent_dim":   "#6A4FAD",
    "success":      "#8AE28A",
    "danger":       "#FF6B8A",
    "text":         "#E8E4F7",
    "text_dim":     "#9A93B8",
}

APP_TITLE = "Teleproxy"
ICON_PATH_PRIMARY = str(Path(__file__).parent / "smoke_icon.ico")
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
# Log tailer (reads proxy.log into the GUI widget)
# ---------------------------------------------------------------------------
class LogTailer:
    """Background thread that tails the proxy.log file and pushes new lines
    into a thread-safe queue consumed by the main UI loop."""

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
# Main window
# ---------------------------------------------------------------------------
class MainWindow:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.running = False
        # Snapshot of the config the proxy was last *actually started* with.
        # When the user changes e.g. the secret in the GUI, this stays at the
        # old value until they press Start again — we use the diff to surface
        # a warning ("secret changed but the Telegram client still has the
        # old one").
        self._applied_cfg: dict = {}
        self.log_q: "queue.Queue[str]" = queue.Queue()
        self.tailer = LogTailer(self.log_q)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.root = ctk.CTk()
        self.root.title(APP_TITLE)
        self.root.geometry("820x620")
        self.root.minsize(720, 520)
        self.root.configure(fg_color=PALETTE["bg"])
        try:
            self.root.iconbitmap(_icon_path())
        except Exception:
            pass

        self._build_ui()
        self._poll_log()

        # Autostart proxy on open
        self.root.after(200, self._start)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        header = ctk.CTkFrame(self.root, fg_color=PALETTE["panel"], corner_radius=0)
        header.pack(fill="x", side="top")

        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.pack(fill="x", padx=16, pady=(12, 2))

        ctk.CTkLabel(
            title_row,
            text="⚡ Teleproxy",
            font=("Segoe UI Semibold", 22),
            text_color=PALETTE["accent_hi"],
        ).pack(side="left")

        ctk.CTkLabel(
            title_row,
            text="  by smokinghazy",
            font=("Segoe UI", 11),
            text_color=PALETTE["text_dim"],
        ).pack(side="left", padx=(8, 0), pady=(7, 0))

        self.status_dot = ctk.CTkLabel(
            title_row,
            text="●  остановлен",
            font=("Segoe UI Semibold", 12),
            text_color=PALETTE["danger"],
        )
        self.status_dot.pack(side="right")

        # Warning banner — hidden by default, shown when settings in the form
        # no longer match the currently running proxy.
        self.banner = ctk.CTkLabel(
            header,
            text="",
            font=("Segoe UI Semibold", 11),
            text_color="#1A1630",
            fg_color="#FFD479",
            corner_radius=0,
            anchor="w",
            justify="left",
            height=26,
        )
        # Not packed — _update_banner() packs/forgets on demand.

        # ---- main container split: left = settings, right = log ----
        body = ctk.CTkFrame(self.root, fg_color=PALETTE["bg"], corner_radius=0)
        body.pack(fill="both", expand=True, padx=0, pady=0)
        body.grid_columnconfigure(0, weight=0, minsize=340)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body, fg_color=PALETTE["panel"], corner_radius=10)
        left.grid(row=0, column=0, sticky="nsw", padx=(12, 6), pady=12)

        right = ctk.CTkFrame(body, fg_color=PALETTE["panel"], corner_radius=10)
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 12), pady=12)

        self._build_left(left)
        self._build_right(right)

        # footer with credit
        footer = ctk.CTkFrame(self.root, fg_color=PALETTE["bg"], corner_radius=0, height=28)
        footer.pack(fill="x", side="bottom")
        ctk.CTkLabel(
            footer,
            text=f"by smokinghazy  ·  лог: {LOG_FILE}",
            font=("Segoe UI", 10),
            text_color=PALETTE["text_dim"],
        ).pack(padx=12, pady=4, side="left")

    def _build_left(self, parent: "ctk.CTkFrame") -> None:
        pad = 14

        # Section: Connection
        self._section_label(parent, "Настройки прокси").pack(fill="x", padx=pad, pady=(pad, 2))

        # host/port row
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=pad, pady=(4, 6))
        row.grid_columnconfigure(0, weight=3)
        row.grid_columnconfigure(1, weight=1)

        self._label(row, "Адрес").grid(row=0, column=0, sticky="w")
        self._label(row, "Порт").grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.e_host = self._entry(row, self.cfg.get("host", DEFAULT_CONFIG["host"]))
        self.e_host.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.e_port = self._entry(row, str(self.cfg.get("port", DEFAULT_CONFIG["port"])))
        self.e_port.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(2, 0))

        self.e_host.bind("<KeyRelease>", lambda *_: self._update_banner())
        self.e_port.bind("<KeyRelease>", lambda *_: self._update_banner())

        # secret
        self._label(parent, "Секрет (32 hex)").pack(fill="x", padx=pad, pady=(6, 2))
        sec_row = ctk.CTkFrame(parent, fg_color="transparent")
        sec_row.pack(fill="x", padx=pad, pady=(0, 4))
        sec_row.grid_columnconfigure(0, weight=1)
        self.e_secret = self._entry(sec_row, self.cfg.get("secret", DEFAULT_CONFIG["secret"]))
        self.e_secret.grid(row=0, column=0, sticky="ew")
        self.e_secret.bind("<KeyRelease>", lambda *_: self._update_banner())
        ctk.CTkButton(
            sec_row, text="Новый", width=70, height=30,
            fg_color=PALETTE["accent_dim"], hover_color=PALETTE["accent"],
            text_color=PALETTE["text"],
            command=self._regen_secret,
        ).grid(row=0, column=1, padx=(8, 0))

        # Section: Advanced
        self._section_label(parent, "Дополнительно").pack(fill="x", padx=pad, pady=(pad, 2))

        self.var_cf = tk.BooleanVar(value=self.cfg.get("cfproxy", True))
        self.var_cf_prio = tk.BooleanVar(value=self.cfg.get("cfproxy_priority", True))

        self._checkbox(parent, "CF-proxy фолбэк", self.var_cf).pack(fill="x", padx=pad, pady=(2, 0))
        self._checkbox(parent, "CF раньше TCP", self.var_cf_prio).pack(fill="x", padx=pad, pady=(2, 0))

        # buttons block
        self._section_label(parent, "Действия").pack(fill="x", padx=pad, pady=(pad, 2))

        btn_row_1 = ctk.CTkFrame(parent, fg_color="transparent")
        btn_row_1.pack(fill="x", padx=pad, pady=(4, 4))
        btn_row_1.grid_columnconfigure(0, weight=1)
        btn_row_1.grid_columnconfigure(1, weight=1)

        self.btn_start = ctk.CTkButton(
            btn_row_1, text="Запустить", height=40,
            fg_color=PALETTE["accent"], hover_color=PALETTE["accent_hi"],
            text_color="#1A1630",
            font=("Segoe UI Semibold", 13),
            command=self._start,
        )
        self.btn_start.grid(row=0, column=0, sticky="ew", padx=(0, 4))

        self.btn_stop = ctk.CTkButton(
            btn_row_1, text="Остановить", height=40,
            fg_color=PALETTE["field"], hover_color=PALETTE["field_border"],
            text_color=PALETTE["text"],
            font=("Segoe UI Semibold", 13),
            command=self._stop,
        )
        self.btn_stop.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        ctk.CTkButton(
            parent, text="Применить и добавить в Telegram", height=40,
            fg_color=PALETTE["accent"], hover_color=PALETTE["accent_hi"],
            text_color="#1A1630",
            font=("Segoe UI Semibold", 12),
            command=self._apply_and_add,
        ).pack(fill="x", padx=pad, pady=(6, 2))

        ctk.CTkButton(
            parent, text="Открыть в Telegram (текущие настройки)", height=32,
            fg_color=PALETTE["accent_dim"], hover_color=PALETTE["accent"],
            text_color=PALETTE["text"],
            font=("Segoe UI", 11),
            command=self._open_in_telegram,
        ).pack(fill="x", padx=pad, pady=(2, 2))

        ctk.CTkButton(
            parent, text="Скопировать ссылку", height=34,
            fg_color=PALETTE["field"], hover_color=PALETTE["field_border"],
            text_color=PALETTE["text"],
            font=("Segoe UI", 12),
            command=self._copy_link,
        ).pack(fill="x", padx=pad, pady=(2, 2))

        ctk.CTkButton(
            parent, text="Открыть файл логов", height=30,
            fg_color="transparent", hover_color=PALETTE["field"],
            text_color=PALETTE["text_dim"],
            font=("Segoe UI", 11),
            command=self._open_log_file,
        ).pack(fill="x", padx=pad, pady=(4, pad))

    def _build_right(self, parent: "ctk.CTkFrame") -> None:
        pad = 14
        head = ctk.CTkFrame(parent, fg_color="transparent")
        head.pack(fill="x", padx=pad, pady=(pad, 6))
        ctk.CTkLabel(
            head, text="Журнал прокси",
            font=("Segoe UI Semibold", 14),
            text_color=PALETTE["text"],
        ).pack(side="left")
        ctk.CTkButton(
            head, text="Очистить", width=90, height=28,
            fg_color=PALETTE["field"], hover_color=PALETTE["field_border"],
            text_color=PALETTE["text_dim"],
            font=("Segoe UI", 11),
            command=self._clear_log,
        ).pack(side="right")

        self.txt = ctk.CTkTextbox(
            parent,
            fg_color=PALETTE["field"],
            text_color=PALETTE["text"],
            border_color=PALETTE["field_border"],
            border_width=1,
            font=("Consolas", 11),
            wrap="none",
        )
        self.txt.pack(fill="both", expand=True, padx=pad, pady=(0, pad))
        self.txt.configure(state="disabled")

        # link row
        link_row = ctk.CTkFrame(parent, fg_color="transparent")
        link_row.pack(fill="x", padx=pad, pady=(0, pad))
        ctk.CTkLabel(
            link_row, text="tg://",
            font=("Segoe UI", 11),
            text_color=PALETTE["text_dim"],
        ).pack(side="left")
        self.lbl_link = ctk.CTkLabel(
            link_row, text=self._link(),
            font=("Consolas", 11),
            text_color=PALETTE["accent_hi"],
            anchor="w",
        )
        self.lbl_link.pack(side="left", fill="x", expand=True, padx=(6, 0))

    # ---- helpers ---------------------------------------------------------

    def _section_label(self, parent, text: str):
        return ctk.CTkLabel(
            parent, text=text,
            font=("Segoe UI Semibold", 12),
            text_color=PALETTE["accent_hi"],
            anchor="w",
        )

    def _label(self, parent, text: str):
        return ctk.CTkLabel(
            parent, text=text,
            font=("Segoe UI", 11),
            text_color=PALETTE["text_dim"],
            anchor="w",
        )

    def _entry(self, parent, value: str):
        e = ctk.CTkEntry(
            parent,
            fg_color=PALETTE["field"],
            text_color=PALETTE["text"],
            border_color=PALETTE["field_border"],
            border_width=1,
            font=("Consolas", 11),
            height=30,
        )
        e.insert(0, value)
        return e

    def _checkbox(self, parent, text: str, var):
        return ctk.CTkCheckBox(
            parent, text=text, variable=var,
            fg_color=PALETTE["accent"],
            hover_color=PALETTE["accent_hi"],
            border_color=PALETTE["field_border"],
            checkmark_color="#1A1630",
            text_color=PALETTE["text"],
            font=("Segoe UI", 11),
        )

    # ---- actions ---------------------------------------------------------

    def _regen_secret(self) -> None:
        self.e_secret.delete(0, "end")
        self.e_secret.insert(0, secrets.token_hex(16))
        self._update_banner()

    def _config_dirty(self) -> bool:
        """Return True when the form has unapplied changes that require the
        proxy to be restarted AND the Telegram client to be re-added."""
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
            self.banner.pack(fill="x", side="bottom")
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
        return cfg

    def _refresh_link(self) -> None:
        try:
            self.lbl_link.configure(text=self._link())
        except Exception:
            pass

    def _start(self) -> None:
        """Start or restart the proxy with whatever is currently in the form."""
        cfg = self._snapshot_cfg()
        self.cfg.update(cfg)
        try:
            save_config(self.cfg)
        except Exception as exc:
            log.warning("save_config failed: %s", exc)

        def _on_error(msg: str) -> None:
            self.root.after(0, lambda: self._set_status(False, msg))

        # Always restart when settings differ from the currently applied ones,
        # otherwise start_proxy() short-circuits with "Proxy already running"
        # and the new secret/port/etc. never take effect.
        if self.running and self._applied_cfg and self._config_dirty():
            log.info("Config changed — restarting proxy")
            stop_proxy()
            # tiny wait so the asyncio loop actually shuts down
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
        """Apply current settings (restart proxy if needed) and open the
        resulting tg:// link so Telegram updates its stored secret."""
        self._start()
        # give the proxy a moment to actually start listening
        self.root.after(300, self._open_in_telegram)

    def _set_status(self, running: bool, err: Optional[str] = None) -> None:
        self.running = running
        if err:
            self.status_dot.configure(text=f"●  ошибка: {err}", text_color=PALETTE["danger"])
            return
        if running:
            self.status_dot.configure(
                text=f"●  запущен на {self.cfg.get('host')}:{self.cfg.get('port')}",
                text_color=PALETTE["success"],
            )
        else:
            self.status_dot.configure(text="●  остановлен", text_color=PALETTE["danger"])

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

    def _on_close(self) -> None:
        try:
            stop_proxy()
        except Exception:
            pass
        try:
            self.tailer.stop()
        except Exception:
            pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def run_gui() -> None:
    cfg = load_config()
    bootstrap(cfg)
    if ctk is None:
        ctypes.windll.user32.MessageBoxW(
            None,
            "customtkinter не установлен. Переустановите приложение.",
            APP_TITLE, 0x10,
        )
        return
    win = MainWindow(cfg)
    win.run()


def main() -> None:
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
        run_gui()
    finally:
        release_lock()
        _release_win_mutex()


if __name__ == "__main__":
    main()
