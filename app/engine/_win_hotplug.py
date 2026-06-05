"""Optional Windows hotplug via WM_DEVICECHANGE (needs pywin32).

Best-effort: a message-only window on a daemon thread that pokes a callback when
devices arrive/leave, so the port list refreshes immediately instead of waiting
for the poll. If pywin32 is absent or anything fails, the caller falls back to
polling. (Imported only on win32, so it never affects Linux/macOS.)
"""
from __future__ import annotations

import threading
import time

WM_DEVICECHANGE = 0x0219


def start(loop, callback, stop) -> threading.Thread:
    import win32api  # noqa: F401
    import win32con  # noqa: F401
    import win32gui

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_DEVICECHANGE:
            loop.call_soon_threadsafe(callback)
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def run():
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = "ser2netDevWatch"
        wc.lpfnWndProc = wndproc
        atom = win32gui.RegisterClass(wc)
        hwnd = win32gui.CreateWindow(atom, "ser2net", 0, 0, 0, 0, 0, 0, 0, 0, 0)
        try:
            while not stop.is_set():
                win32gui.PumpWaitingMessages()
                time.sleep(0.2)
        finally:
            with __import__("contextlib").suppress(Exception):
                win32gui.DestroyWindow(hwnd)

    t = threading.Thread(target=run, daemon=True, name="win-hotplug")
    t.start()
    return t
