"""Windows frame integration and portable window/link policy for the desktop."""

import ctypes
from ctypes import wintypes
from pathlib import Path
import re
import subprocess
import sys
import tkinter as tk
from urllib.parse import unquote, urlsplit
import webbrowser

from .desktop_design import CAPTION_GLYPHS


class _MouseTracking(ctypes.Structure):
    """Describe non-client pointer departure without querying Tk from native callbacks."""

    _fields_ = [("size", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("handle", wintypes.HWND), ("hover_time", wintypes.DWORD)]


class WindowFrame:
    """Retain native Windows resizing, snapping, and keyboard window conventions."""

    def __init__(self, root: tk.Tk, titlebar: tk.Widget, maximize: tk.Widget) -> None:
        """Install Windows hooks on map; keep other platforms' native frame intact."""
        self.root = root
        self.titlebar = titlebar
        self.maximize_button = maximize
        self.caption_widgets = {root, titlebar, *titlebar.winfo_children()}
        self.caption_buttons = [widget for widget in titlebar.winfo_children() if widget.winfo_class() == "TButton"]
        self.handle = 0
        self.previous = 0
        self.child_procedures: dict[int, tuple[int, object]] = {}
        self.retired_callbacks: list[object] = []
        self.callback = None
        self.caption_bounds = (0, 0, 0, 0)
        self.caption_controls: list[tuple[tuple[int, int, int, int], int]] = []
        self.resize_edge = 8
        self.requested_geometry: tuple[int, int, int, int] | None = None
        self.maximize_pressed = False
        self.class_styles: list[tuple[int, int]] = []
        self.maximize_hovered = False
        self._maximize_hover_applied = False
        self._maximize_icon_applied = False
        root.bind("<Configure>", self._cache_layout, add=True)
        root.bind("<Map>", self._mapped, add=True)
        root.bind("<Destroy>", self._destroyed, add=True)

    def _mapped(self, event: tk.Event) -> None:
        """Subclass the mapped wrapper once without removing its native window style."""
        if event.widget is not self.root or sys.platform != "win32" or self.previous:
            return
        requested_width, requested_height = self.root.winfo_width(), self.root.winfo_height()
        user = ctypes.WinDLL("user32", use_last_error=True)
        self.user = user
        result_type = ctypes.c_ssize_t
        self.proc_type = ctypes.WINFUNCTYPE(result_type, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user.GetParent.argtypes = [wintypes.HWND]
        user.GetParent.restype = wintypes.HWND
        self.handle = user.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        self.set_proc = user.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user.SetWindowLongW
        self.set_proc.argtypes = [wintypes.HWND, ctypes.c_int, result_type]
        self.set_proc.restype = result_type
        user.CallWindowProcW.argtypes = [result_type, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user.CallWindowProcW.restype = result_type
        user.GetClassLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user.GetClassLongW.restype = wintypes.DWORD
        user.SetClassLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
        user.SetClassLongW.restype = wintypes.DWORD
        user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user.IsZoomed.argtypes = [wintypes.HWND]
        user.IsZoomed.restype = wintypes.BOOL
        user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user.SetCapture.argtypes = [wintypes.HWND]
        user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user.TrackMouseEvent.argtypes = [ctypes.POINTER(_MouseTracking)]
        self.callback = self.proc_type(self._window_proc)
        self.previous = self.set_proc(self.handle, -4, ctypes.cast(self.callback, ctypes.c_void_p).value)
        if not self.previous:
            self.callback = None
            return
        # Tk invalidates every child on any resize through CS_HREDRAW/CS_VREDRAW.
        # Its widget layout already invalidates changed regions; preserve the rest.
        for handle in (self.handle, self.root.winfo_id()):
            style = user.GetClassLongW(handle, -26)
            if style & 0x0003:
                user.SetClassLongW(handle, -26, style & ~0x0003)
                self.class_styles.append((handle, style))
        self._cache_layout()
        self._hook_child(self.root, descendants=False)
        self._hook_child(self.titlebar)
        if self.requested_geometry:
            self._apply_geometry()
        else:
            # The old native caption/borders must not inflate the new client-rendered size.
            user.SetWindowPos(self.handle, None, 0, 0, requested_width, requested_height, 0x0026)
        try:
            dwm = ctypes.WinDLL("dwmapi")
            dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            value = ctypes.c_int(1)
            dwm.DwmSetWindowAttribute(self.handle, 20, ctypes.byref(value), ctypes.sizeof(value))
        except OSError:
            pass

    def restore_geometry(self, saved: dict, bounds: tuple[int, int, int, int]) -> None:
        """Restore exact native client dimensions without accumulating Tk's old frame extents."""
        geometry = restored_geometry(saved, bounds)
        match = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", geometry)
        width, height, x, y = map(int, match.groups())
        self.requested_geometry = (width, height, x, y)
        if sys.platform == "win32" and self.previous:
            self._apply_geometry()
        else:
            self.root.geometry(geometry)

    def _apply_geometry(self) -> None:
        """Apply saved size/position directly to the Windows frame after custom chrome is ready."""
        if self.requested_geometry:
            width, height, x, y = self.requested_geometry
            self.user.SetWindowPos(self.handle, None, x, y, width, height, 0x0034)

    def _window_proc(self, handle: int, message: int, wparam: int, lparam: int) -> int:
        """Handle only frame sizing and hit testing; preserve default OS processing."""
        if message == 0x00A0:  # WM_NCMOUSEMOVE: HTMAXBUTTON bypasses ttk pointer bindings.
            self.maximize_hovered = wparam == 9
            tracking = _MouseTracking(ctypes.sizeof(_MouseTracking), 0x12, handle, 0)
            self.user.TrackMouseEvent(ctypes.byref(tracking))
        elif message in {0x02A2, 0x0200}:  # Non-client leave or pointer returning to client content.
            self.maximize_hovered = False
        if message == 0x00A1 and wparam == 9:  # Custom HTMAXBUTTON needs an explicit click action.
            self.maximize_pressed = True
            self.user.SetCapture(handle)
            return 0
        if message in {0x0202, 0x00A2} and self.maximize_pressed:
            self.maximize_pressed = False
            self.user.ReleaseCapture()
            point = wintypes.POINT()
            self.user.GetCursorPos(ctypes.byref(point))
            packed = (point.y & 0xffff) << 16 | (point.x & 0xffff)
            if self._window_proc(handle, 0x0084, 0, packed) == 9:
                command = 0xF120 if self.user.IsZoomed(handle) else 0xF030
                self.user.PostMessageW(handle, 0x0112, command, 0)
            return 0
        if message == 0x0215:  # WM_CAPTURECHANGED cancels a displaced caption press.
            self.maximize_pressed = False
        if message == 0x0083 and wparam:  # WM_NCCALCSIZE: titlebar is client-rendered.
            if self.user.IsZoomed(handle):
                rect = ctypes.cast(lparam, ctypes.POINTER(wintypes.RECT)).contents
                # Maximized resize borders can extend into an adjacent monitor.
                bounds = display_bounds((rect.left + rect.right) // 2,
                                        (rect.top + rect.bottom) // 2)
                rect.left, rect.top, rect.right, rect.bottom = bounds
            return 0
        if message == 0x0084:  # WM_NCHITTEST: native edges and caption trigger OS snap.
            x = ctypes.c_short(lparam & 0xffff).value
            y = ctypes.c_short((lparam >> 16) & 0xffff).value
            rect = wintypes.RECT()
            self.user.GetWindowRect(handle, ctypes.byref(rect))
            edge = self.resize_edge
            if not self.user.IsZoomed(handle):
                left, right = x < rect.left + edge, x >= rect.right - edge
                top, bottom = y < rect.top + edge, y >= rect.bottom - edge
                if top and left:
                    return 13
                if top and right:
                    return 14
                if bottom and left:
                    return 16
                if bottom and right:
                    return 17
                if left or right or top or bottom:
                    return 10 if left else 11 if right else 12 if top else 15
            relative_x, relative_y = x - rect.left, y - rect.top
            left, top, right, bottom = self.caption_bounds
            if left <= relative_x < right and top <= relative_y < bottom:
                for (left, top, right, bottom), hit in self.caption_controls:
                    if left <= relative_x < right and top <= relative_y < bottom:
                        return hit
                return 2
            return 1
        return self.user.CallWindowProcW(self.previous, handle, message, wparam, lparam)

    def _cache_layout(self, event: tk.Event | None = None) -> None:
        """Cache Tk geometry on its event thread so native callbacks never reenter Tcl."""
        if not self.handle:
            return
        if event is not None and event.widget not in self.caption_widgets:
            return
        rect = wintypes.RECT()
        self.user.GetWindowRect(self.handle, ctypes.byref(rect))
        def geometry(widget: tk.Widget) -> tuple[int, int, int, int]:
            """Read a widget's complete local rectangle with one Tcl call."""
            match = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", widget.winfo_geometry())
            return tuple(map(int, match.groups()))
        width, height, _, _ = geometry(self.titlebar)
        left = self.titlebar.winfo_rootx() - rect.left
        top = self.titlebar.winfo_rooty() - rect.top
        self.caption_bounds = (left, top, left + width, top + height)
        self.caption_controls = []
        for child in self.caption_buttons:
            width, height, x, y = geometry(child)
            self.caption_controls.append(((left + x, top + y, left + x + width, top + y + height),
                                          9 if child is self.maximize_button else 1))
        self.resize_edge = max(6, round(float(self.root.tk.call("tk", "scaling")) * 5))

    def _hook_child(self, widget: tk.Widget, *, descendants: bool = True) -> None:
        """Forward caption hits through the root and titlebar without hooking chat widgets."""
        handle = widget.winfo_id()
        if handle != self.handle and handle not in self.child_procedures:
            callback = self.proc_type(self._child_window_proc)
            previous = self.set_proc(handle, -4, ctypes.cast(callback, ctypes.c_void_p).value)
            if previous:
                self.child_procedures[handle] = (previous, callback)
                widget.bind("<Destroy>", lambda event, widget=widget, handle=handle: self._unhook_child(handle) if event.widget is widget else None, add=True)
        if descendants:
            for child in widget.winfo_children():
                if not isinstance(child, tk.Toplevel):
                    self._hook_child(child)

    def _unhook_child(self, handle: int) -> None:
        """Restore a child before destruction, retaining its callback until native dispatch returns."""
        saved = self.child_procedures.pop(handle, None)
        if saved:
            self.set_proc(handle, -4, saved[0])
            self.retired_callbacks.append(saved[1])

    def _child_window_proc(self, handle: int, message: int, wparam: int, lparam: int) -> int:
        """Let the native parent handle caption, maximize, and resize hits through Tk children."""
        previous = self.child_procedures[handle][0]
        if message == 0x0084:
            hit = self._window_proc(self.handle, message, wparam, lparam)
            if hit != 1:
                return -1  # HTTRANSPARENT walks the same-thread native child chain.
        return self.user.CallWindowProcW(previous, handle, message, wparam, lparam)

    def maximize(self) -> None:
        """Toggle the normal native maximize/restore action."""
        self.root.state("normal" if self.root.state() == "zoomed" else "zoomed")

    def sync_hover(self) -> None:
        """Apply cached native hover through ttk only from the UI event loop."""
        if self.previous and self.maximize_hovered != self._maximize_hover_applied:
            self._maximize_hover_applied = self.maximize_hovered
            self.maximize_button.state(["active"] if self.maximize_hovered else ["!active"])

    def sync_window_state(self) -> None:
        """Show restore boxes when maximized, updating the icon only on state changes."""
        maximized = self.root.state() == "zoomed"
        if maximized != self._maximize_icon_applied:
            self._maximize_icon_applied = maximized
            self.maximize_button.configure(text=CAPTION_GLYPHS["restore" if maximized else "maximize"])

    def _destroyed(self, event: tk.Event) -> None:
        """Restore Tk's original procedure before releasing the callback reference."""
        if event.widget is self.root and self.previous:
            for handle, style in self.class_styles:
                self.user.SetClassLongW(handle, -26, style)
            self.class_styles.clear()
            for handle in list(self.child_procedures):
                self._unhook_child(handle)
            self.set_proc(self.handle, -4, self.previous)
            self.previous = 0
            # Keep the callback alive until the interpreter and native dispatch return.


def dark_dialog_frame(window: tk.Toplevel) -> None:
    """Use Windows dark caption and rounded dialog corners without changing shared layout."""
    if sys.platform != "win32":
        return
    user = ctypes.WinDLL("user32")
    user.GetParent.argtypes = [wintypes.HWND]
    user.GetParent.restype = wintypes.HWND
    handle = user.GetParent(window.winfo_id()) or window.winfo_id()
    try:
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        for attribute, setting in ((20, 1), (33, 2)):
            value = ctypes.c_int(setting)
            dwm.DwmSetWindowAttribute(handle, attribute, ctypes.byref(value), ctypes.sizeof(value))
    except OSError:
        pass


def system_reduced_motion() -> bool:
    """Respect Windows client-area animation settings; default to minimal motion elsewhere."""
    if sys.platform == "win32":
        enabled = wintypes.BOOL()
        if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0):
            return not bool(enabled.value)
    return True


def display_bounds(x: int, y: int, fallback: tuple[int, int] = (1920, 1080)) -> tuple[int, int, int, int]:
    """Find the nearest Windows monitor's usable work area, or a portable screen bound."""
    if sys.platform == "win32":
        class MonitorInfo(ctypes.Structure):
            """Describe the Win32 monitor and usable work rectangles."""
            _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT), ("work", wintypes.RECT), ("flags", wintypes.DWORD)]
        user = ctypes.WinDLL("user32")
        user.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user.MonitorFromPoint.restype = wintypes.HANDLE
        user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        monitor = user.MonitorFromPoint(wintypes.POINT(x, y), 2)
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        if user.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return info.work.left, info.work.top, info.work.right, info.work.bottom
    return 0, 0, fallback[0], fallback[1]


def restored_geometry(saved: dict, bounds: tuple[int, int, int, int]) -> str:
    """Clamp restored geometry to a usable display, including removed monitors."""
    left, top, right, bottom = bounds
    width = min(max(900, int(saved.get("width", 1120))), right - left)
    height = min(max(600, int(saved.get("height", 740))), bottom - top)
    x = min(max(left, int(saved.get("x", left + 80))), right - width)
    y = min(max(top, int(saved.get("y", top + 60))), bottom - height)
    return f"{width}x{height}{x:+d}{y:+d}"


def open_link(target: str, *, reveal_file: bool = False) -> None:
    """Open credential-free web links, or explicitly reveal a local file without executing it."""
    if any(ord(character) < 32 for character in target):
        raise ValueError("Invalid link target.")
    parsed = urlsplit(target)
    if not reveal_file:
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Only ordinary HTTP(S) links can be opened.")
        try:
            parsed.port
        except ValueError:
            raise ValueError("Invalid link address.") from None
        webbrowser.open(target)
        return
    if parsed.scheme == "file":
        if parsed.netloc not in {"", "localhost"}:
            raise ValueError("Only local file locations can be revealed.")
        raw = unquote(parsed.path)
        if sys.platform == "win32" and raw.startswith("/"):
            raw = raw[1:]
        path = Path(raw)
    else:
        path = Path(target)
    if str(path).startswith("\\\\") or not path.is_absolute() or not path.exists():
        raise ValueError("The local file no longer exists or its path is not absolute.")
    if sys.platform == "win32":
        subprocess.Popen(["explorer.exe", "/select," + str(path.resolve())])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path.resolve())])
    else:
        subprocess.Popen(["xdg-open", str(path.resolve().parent)])
