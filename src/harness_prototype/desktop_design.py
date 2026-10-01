"""Portable FLUX design tokens, rounded ttk components, and Markdown parsing."""

from collections.abc import Callable
from dataclasses import dataclass
import re
import sys
import tkinter as tk
from tkinter import font, ttk
from typing import Any

from PIL import Image, ImageDraw, ImageTk


COLORS = {
    "canvas": "#202427", "titlebar": "#303b43", "sidebar": "#262b2f", "surface": "#2d3338",
    "raised": "#353e44", "hover": "#414e55", "selected": "#344e54",
    "text": "#edf1f3", "muted": "#b4c0c6", "accent": "#91d4d5",
    "success": "#a2d4b1", "warning": "#e5c18d", "failure": "#f4b1b5",
    "border": "#65777f", "disabled": "#b4c0c6",
}
SPACING = (4, 8, 12, 16, 24, 32)
RADII = {"control": 8, "container": 14, "bubble": 16}
ELEVATION = {"base": "canvas", "panel": "surface", "dialog": "raised"}
MOTION = {"hover_ms": 0, "indicator_ms": 800, "sidebar_ms": 180, "frame_ms": 16}
ICONS = {"sidebar": "☰", "new": "+", "pin": "◆", "working": "›", "close": "×"}
TEXT_SIZES = {"small": 10, "default": 11, "large": 13}
FONT_FAMILY = "Segoe UI"
CODE_FAMILY = "Consolas"
CAPTION_GLYPHS = ({"minimize": "\ue921", "maximize": "\ue922", "restore": "\ue923", "close": "\ue8bb"}
                  if sys.platform == "win32" else
                  {"minimize": "−", "maximize": "□", "restore": "❐", "close": "×"})
MARK_PATHS = (((16, 2, 30, 10, 24, 14, 16, 9, 8, 14, 2, 10), COLORS["accent"]),
              ((2, 16, 8, 12, 8, 21, 16, 26, 16, 32, 2, 24), COLORS["accent"]),
              ((30, 16, 24, 12, 24, 21, 16, 26, 16, 32, 30, 24), COLORS["success"]))


class FluxTheme:
    """Keep one reusable charcoal theme and retained nine-slice assets per window."""

    def __init__(self, root: tk.Tk, name: str = "flux") -> None:
        """Install scalable rounded components without adding a frontend runtime."""
        self.root = root
        self.zoom = getattr(root, "_flux_zoom", 100) / 100
        self.images: list[ImageTk.PhotoImage] = []
        self.style = ttk.Style(root)
        if name not in self.style.theme_names():
            self.style.theme_create(name, parent="clam")
        self.style.theme_use(name)
        self.scale = max(0.5, float(root.tk.call("tk", "scaling")) / (96 / 72) * self.zoom)
        self.style.configure(".", background=COLORS["canvas"], foreground=COLORS["text"],
                             font=(FONT_FAMILY, 11), borderwidth=0)
        self.style.configure("TFrame", background=COLORS["canvas"])
        # Preserve the draggable divider without the inherited theme's dotted grip.
        sash_size = max(4, round(6 * self.scale))
        sash = ImageTk.PhotoImage(Image.new("RGB", (sash_size, sash_size), COLORS["canvas"]), master=root)
        self.images.append(sash)
        if "Flux.sash" not in self.style.element_names():
            self.style.element_create("Flux.sash", "image", sash, sticky="nsew")
        self.style.layout("Horizontal.Sash", [("Flux.sash", {"sticky": "nsew"})])

        self.style.configure("Sidebar.TFrame", background=COLORS["sidebar"])
        self.style.configure("Sidebar.TLabel", background=COLORS["sidebar"], foreground=COLORS["muted"])
        self.style.configure("TLabel", background=COLORS["canvas"])
        self.style.configure("Muted.TLabel", foreground=COLORS["muted"])
        self.style.configure("Title.TLabel", font=(FONT_FAMILY, 15, "bold"))
        self.style.configure("Titlebar.TFrame", background=COLORS["titlebar"])
        self.style.configure("Titlebar.Title.TLabel", background=COLORS["titlebar"])
        self.style.configure("Titlebar.Status.TLabel", background=COLORS["titlebar"])
        self.style.configure("Status.TLabel", foreground=COLORS["muted"], padding=(12, 6))
        self.style.configure("Surface.TFrame", background=COLORS["surface"])
        self.style.configure("Inset.TFrame", background=COLORS["surface"])
        self.style.configure("Surface.TLabel", background=COLORS["surface"], foreground=COLORS["muted"])
        self.style.configure("Surface.Title.TLabel", background=COLORS["surface"], foreground=COLORS["text"],
                             font=(FONT_FAMILY, 15, "bold"))
        self._rounded("Flux.button", COLORS["surface"], RADII["control"], interactive=True)
        self.style.layout("TButton", [("Flux.button", {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
        for prefix, corner in (("Inset", COLORS["surface"]), ("Sidebar", COLORS["sidebar"]), ("Titlebar", COLORS["titlebar"])):
            element = f"Flux.{prefix.lower()}button"
            self._rounded(element, COLORS["raised"] if prefix in {"Inset", "Titlebar"} else COLORS["surface"], RADII["control"], interactive=True, corner=corner)
            self.style.layout(prefix + ".TButton", [(element, {"sticky": "nsew", "children": [
                ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
        self.style.configure("Sidebar.TButton", foreground=COLORS["text"])
        self.style.configure("Inset.TButton", foreground=COLORS["text"])
        self.style.configure("Dropdown.Inset.TButton", padding=(10, 6), anchor="w")
        self._rounded("Flux.dropdownselected", COLORS["selected"], RADII["control"], interactive=True, corner=COLORS["surface"])
        self.style.layout("Selected.Dropdown.Inset.TButton", [("Flux.dropdownselected", {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
        self.style.configure("Selected.Dropdown.Inset.TButton", foreground=COLORS["accent"])
        for role, color in (("Success", COLORS["success"]), ("Neutral", COLORS["text"]), ("Danger", COLORS["failure"])):
            self.style.configure(role + ".Inset.TButton", foreground=color)
            selected = "Selected." + role + ".Inset.TButton"
            self.style.layout(selected, self.style.layout("Selected.Dropdown.Inset.TButton"))
            self.style.configure(selected, foreground=color)

        # Canvas menu rows need button corners painted in the row's own color.
        for prefix, corner in (("Skill", COLORS["raised"]), ("Selected.Skill", COLORS["selected"])):
            element = "Flux." + prefix.lower() + "button"
            self._rounded(element, COLORS["surface"], RADII["control"], interactive=True, corner=corner)
            layout = [(element, {"sticky": "nsew", "children": [
                ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})]
            for role, color in (("Success", COLORS["success"]), ("Danger", COLORS["failure"])):
                name = prefix + "." + role + ".TButton"
                self.style.layout(name, layout)
                self.style.configure(name, foreground=color, background=corner)

        for prefix, hover, pressed in (("Caption", COLORS["hover"], COLORS["selected"]),
                                        ("Close.Caption", "#c42b1c", "#a52317")):
            assets = []
            for state, color in (("", COLORS["titlebar"]), ("pressed", pressed), ("active", hover)):
                asset = ImageTk.PhotoImage(Image.new("RGB", (round(46 * self.scale), round(32 * self.scale)), color), master=root)
                self.images.append(asset)
                assets.append((state, asset))
            element = "Flux." + prefix.lower()
            if element not in self.style.element_names():
                self.style.element_create(element, "image", assets[0][1],
                                          *((state, asset) for state, asset in assets[1:]), sticky="nsew")
            self.style.layout(prefix + ".TButton", [(element, {"sticky": "nsew", "children": [
                ("Button.label", {"sticky": "nsew"})]})])
            self.style.configure(prefix + ".TButton", padding=0, anchor="center", foreground=COLORS["text"],
                                 font=("Segoe MDL2 Assets" if sys.platform == "win32" else FONT_FAMILY,
                                       -round(10 * self.scale)))
        self.style.map("Close.Caption.TButton", foreground=[("active", "#ffffff"), ("pressed", "#ffffff")])
        self.style.configure("TButton", padding=(4, 2), foreground=COLORS["text"], anchor="center")
        self.style.map("TButton", foreground=[("disabled", COLORS["disabled"])])
        self.style.configure("Accent.TButton", foreground=COLORS["accent"])
        self.style.configure("Success.TButton", foreground=COLORS["success"])
        self.style.configure("Danger.TButton", foreground=COLORS["failure"])
        self._rounded("Flux.send", COLORS["accent"], RADII["control"], interactive=True, corner=COLORS["surface"], emphasized=True)
        self.style.layout("Send.TButton", [("Flux.send", {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
        self.style.configure("Send.TButton", foreground=COLORS["canvas"], padding=(6, 3), font=(FONT_FAMILY, round(21 * self.zoom), "bold"))
        self.style.map("Send.TButton", foreground=[("disabled", COLORS["disabled"])])
        for prefix, fill in (("Session", COLORS["surface"]), ("SelectedSession", COLORS["selected"])):
            element = f"Flux.{prefix.lower()}"
            self._rounded(element, fill, RADII["control"], corner=COLORS["sidebar"])
            self.style.layout(prefix + ".TFrame", [(element, {"sticky": "nsew"})])
            self.style.configure(prefix + ".TLabel", background=fill, foreground=COLORS["text"])
            self.style.configure(prefix + "Detail.TLabel", background=fill, foreground=COLORS["muted"])
            self._rounded(element + "action", fill, RADII["control"], interactive=True, corner=fill)
            self.style.layout(prefix + "Action.TButton", [(element + "action", {"sticky": "nsew", "children": [
                ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
            self.style.configure(prefix + "Action.TButton", padding=(2, 2), foreground=COLORS["muted"])
            self.style.configure("Pinned." + prefix + "Action.TButton", foreground=COLORS["accent"])
        self.style.configure("Focus.Session.TLabel", background=COLORS["hover"], foreground=COLORS["text"], borderwidth=1, relief="solid")
        self._rounded("Flux.surface", COLORS["surface"], RADII["container"])
        self.style.layout("Surface.TFrame", [("Flux.surface", {"sticky": "nsew"})])
        self._rounded("Flux.entry", COLORS["surface"], RADII["control"], interactive=True)
        self.style.layout("TEntry", [("Flux.entry", {"sticky": "nsew", "children": [
            ("Entry.padding", {"sticky": "nsew", "children": [("Entry.textarea", {"sticky": "nsew"})]})]})])
        self.style.configure("TEntry", padding=8, fieldbackground=COLORS["surface"], insertcolor=COLORS["text"])
        self._rounded("Flux.search", COLORS["surface"], 6, interactive=True, corner=COLORS["sidebar"], compact=True)
        self.style.layout("Search.TEntry", [("Flux.search", {"sticky": "nsew", "children": [
            ("Entry.padding", {"sticky": "nsew", "children": [("Entry.textarea", {"sticky": "nsew"})]})]})])
        self.style.configure("Search.TEntry", padding=(7, 2), fieldbackground=COLORS["surface"])
        self.style.configure("SearchPlaceholder.TLabel", background=COLORS["surface"], foreground=COLORS["muted"])
        self.style.configure("TScrollbar", background=COLORS["raised"], troughcolor=COLORS["canvas"],
                             arrowcolor=COLORS["muted"], bordercolor=COLORS["canvas"], width=round(12 * self.scale))
        self._rounded("Flux.Scrollbar.trough", COLORS["sidebar"], 3, compact=True)
        self._rounded("Flux.Scrollbar.thumb", COLORS["border"], 3, interactive=True, emphasized=True, compact=True)
        self.style.layout("Chat.Vertical.TScrollbar", [("Flux.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Flux.Scrollbar.thumb", {"expand": 1, "sticky": "nsew"})]})])
        self.style.configure("Chat.Vertical.TScrollbar", width=round(12 * self.scale))
        self.style.configure("TCheckbutton", background=COLORS["canvas"], foreground=COLORS["text"])
        self.style.map("TCheckbutton", background=[("active", COLORS["surface"])])
        for widget in ("Text", "Listbox", "Menu"):
            for key, value in {"background": COLORS["canvas"], "foreground": COLORS["text"],
                               "selectBackground": COLORS["selected"], "selectForeground": COLORS["text"],
                               "highlightBackground": COLORS["canvas"], "highlightColor": COLORS["accent"],
                               "borderWidth": 0, "font": "TkDefaultFont"}.items():
                root.option_add(f"*{widget}.{key}", value)
        root.option_add("*Text.insertBackground", COLORS["text"])
        root.configure(background=COLORS["canvas"])
        self.set_text_size("default")
        for name in ("TButton", "Send.TButton", "TEntry", "Search.TEntry", "Dropdown.Inset.TButton",
                     "SessionAction.TButton", "SelectedSessionAction.TButton"):
            padding = self.style.configure(name).get("padding")
            if padding is not None:
                values = padding if isinstance(padding, (tuple, list)) else root.tk.splitlist(str(padding))
                self.style.configure(name, padding=tuple(round(float(str(value)) * self.zoom) for value in values))

    def _rounded(self, name: str, fill: str, radius: int, *, interactive: bool = False, corner: str | None = None, emphasized: bool = False, compact: bool = False) -> None:
        """Create antialiased nine-slice assets with visible hover and focus states."""
        border = max(3 if compact else 8, round(radius * self.scale))
        # Avoid thousands of tiny paint tiles while retaining compact natural sizes.
        size = border * 2 + 128
        states = [("", fill, fill)]
        if interactive:
            states += [("disabled", COLORS["canvas"], COLORS["canvas"]),
                       ("pressed", COLORS["success"] if emphasized else COLORS["selected"], COLORS["accent"]),
                       ("active focus", COLORS["success"] if emphasized else COLORS["hover"], COLORS["accent"]),
                       ("focus", fill, COLORS["text"] if emphasized else COLORS["accent"]),
                       ("active", COLORS["success"] if emphasized else COLORS["hover"], COLORS["border"])]
        assets = []
        for state, color, outline in states:
            image = Image.new("RGBA", (size * 3, size * 3), corner or COLORS["canvas"])
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((1, 1, size * 3 - 2, size * 3 - 2), radius=border * 3,
                                   fill=color, outline=outline, width=3)
            asset = ImageTk.PhotoImage(image.resize((size, size), Image.Resampling.LANCZOS), master=self.root)
            self.images.append(asset)
            assets.append((state, asset))
        # ttk uses first-match state order; disabled must win over focus and hover.
        if name not in self.style.element_names():
            self.style.element_create(name, "image", assets[0][1],
                                      *((state, asset) for state, asset in assets[1:]), border=border, width=border * 2 + 6, height=border * 2 + 6, sticky="nsew")

    def set_text_size(self, size: str) -> None:
        """Apply semantic typography to both themed and selectable text widgets."""
        zoom = getattr(self, "zoom", 1)
        points = round(TEXT_SIZES.get(size, 11) * zoom)
        font.nametofont("TkDefaultFont", root=self.root).configure(family=FONT_FAMILY, size=points)
        font.nametofont("TkTextFont", root=self.root).configure(family=FONT_FAMILY, size=points)
        font.nametofont("TkFixedFont", root=self.root).configure(family=CODE_FAMILY, size=points)
        self.style.configure(".", font=(FONT_FAMILY, points))
        self.style.configure("TButton", font=(FONT_FAMILY, points, "bold"))
        self.style.configure("Title.TLabel", font=(FONT_FAMILY, round((TEXT_SIZES.get(size, 11) + 4) * zoom), "bold"))
        self.style.configure("Surface.Title.TLabel", font=(FONT_FAMILY, round((TEXT_SIZES.get(size, 11) + 4) * zoom), "bold"))


def draw_mark(canvas: tk.Canvas, size: int = 32) -> None:
    """Render a supersampled FLUX mark and retain its image for crisp scaled edges."""
    supersampling = 4
    image = Image.new("RGBA", (size * supersampling, size * supersampling), canvas.cget("background"))
    draw = ImageDraw.Draw(image)
    ratio = size * supersampling / 32
    for points, color in MARK_PATHS:
        vertices = [(points[index] * ratio, points[index + 1] * ratio) for index in range(0, len(points), 2)]
        draw.polygon(vertices, fill=color)
    asset = ImageTk.PhotoImage(image.resize((size, size), Image.Resampling.LANCZOS), master=canvas)
    canvas.delete("all")
    canvas.create_image(0, 0, image=asset, anchor="nw")
    canvas._flux_mark_image = asset
    canvas._flux_mark_size = size


def set_application_icon(root: tk.Tk) -> None:
    """Replace Tk's default icon with the shared FLUX mark at taskbar and window sizes."""
    images = []
    for size in (16, 32, 48, 64, 256):
        image = Image.new("RGBA", (size * 4, size * 4))
        draw = ImageDraw.Draw(image)
        ratio = size * 4 / 36
        for points, color in MARK_PATHS:
            draw.polygon([((points[index] + 2) * ratio, (points[index + 1] + 2) * ratio)
                          for index in range(0, len(points), 2)], fill=color)
        images.append(ImageTk.PhotoImage(image.resize((size, size), Image.Resampling.LANCZOS), master=root))
    root.iconphoto(True, *images)
    root._flux_icons = images


class CommandPalette(ttk.Frame):
    """Show spacious rounded command rows while the composer retains keyboard focus."""

    def __init__(self, parent: tk.Widget, choose: Callable[[], object]) -> None:
        """Build a bounded inline menu using the application's shared named fonts."""
        super().__init__(parent, style="Surface.TFrame", padding=10)
        self.choices: list[tuple[str, str]] = []
        self.index = 0
        self.choose = choose
        self.title_font = font.Font(root=self, font="TkDefaultFont")
        ttk.Label(self, text="Commands", style="Surface.TLabel").pack(anchor="w", padx=8, pady=(0, 6))
        self.canvas = tk.Canvas(self, background=COLORS["surface"], highlightthickness=0, borderwidth=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        ttk.Label(self, text="↑ ↓ Browse    Enter Select    Esc Dismiss", style="Surface.TLabel").pack(anchor="w", padx=8, pady=(8, 0))
        self.canvas.bind("<Configure>", lambda _event: self.redraw())
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<ButtonRelease-1>", self._click)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.row_height = 60

    def set_choices(self, choices: list[tuple[str, str]]) -> None:
        """Replace filtered rows and reset the selection and scroll position."""
        self.choices = choices
        self.index = 0
        self.canvas.yview_moveto(0)
        self.redraw()

    def redraw(self) -> None:
        """Paint separate rounded rows with command titles and muted descriptions."""
        shared_font = font.nametofont("TkDefaultFont", root=self)
        self.title_font.configure(**shared_font.actual())
        self.title_font.configure(weight="bold")
        line = shared_font.metrics("linespace")
        self.row_height = line * 3 + 20
        width = max(120, self.canvas.winfo_width())
        self.canvas.delete("all")
        for index, (name, description) in enumerate(self.choices):
            top = index * self.row_height
            bottom = top + self.row_height - 5
            radius = 10
            self.canvas.create_polygon(4 + radius, top, width - 4 - radius, top,
                                       width - 4, top, width - 4, top + radius,
                                       width - 4, bottom - radius, width - 4, bottom,
                                       width - 4 - radius, bottom, 4 + radius, bottom,
                                       4, bottom, 4, bottom - radius, 4, top + radius, 4, top,
                                       smooth=True, splinesteps=24, outline="",
                                       fill=COLORS["selected"] if index == self.index else COLORS["raised"])
            self.canvas.create_text(16, top + 9, text=name, anchor="nw", font=self.title_font,
                                    fill=COLORS["accent"] if index == self.index else COLORS["text"])
            self.canvas.create_text(16, top + line + 13, text=description, anchor="nw",
                                    font="TkDefaultFont", fill=COLORS["muted"], width=max(80, width - 48))
        total = len(self.choices) * self.row_height
        self.canvas.configure(height=min(total, self.row_height * 4), scrollregion=(0, 0, width, total))

    def move(self, step: int) -> None:
        """Select and reveal a row without moving focus away from editable input."""
        if not self.choices:
            return
        self.index = (self.index + step) % len(self.choices)
        self.redraw()
        top = self.index * self.row_height
        visible = self.canvas.canvasy(0)
        height = self.canvas.winfo_height()
        if top < visible:
            self.canvas.yview_moveto(top / (len(self.choices) * self.row_height))
        elif top + self.row_height > visible + height:
            self.canvas.yview_moveto((top + self.row_height - height) / (len(self.choices) * self.row_height))

    def _hover(self, event: tk.Event) -> None:
        """Highlight the row under the pointer using the same keyboard selection."""
        index = int(self.canvas.canvasy(event.y) // self.row_height)
        if 0 <= index < len(self.choices) and index != self.index:
            self.index = index
            self.redraw()

    def _click(self, event: tk.Event) -> str:
        """Complete only a valid clicked command row."""
        index = int(self.canvas.canvasy(event.y) // self.row_height)
        if 0 <= index < len(self.choices):
            self.index = index
            self.choose()
        return "break"

    def _wheel(self, event: tk.Event) -> str:
        """Scroll command rows without scrolling the conversation underneath."""
        self.canvas.yview_scroll(-round(event.delta / 120 * 3), "units")
        return "break"


class DropdownMenu:
    """Present rounded menu choices inline, without a native popup or decision window."""

    def __init__(self, anchor: tk.Widget, choices: list[tuple[str, str]], choose: Callable[[str], None], selected: str | None = None) -> None:
        """Anchor bounded keyboard-accessible choices to their originating control."""
        self.anchor = anchor
        self.root = anchor.winfo_toplevel()
        self.choose = choose
        self.choices = choices
        self.closed = False
        self.index = next((index for index, (_, value) in enumerate(choices) if value == selected), 0)
        self.frame = ttk.Frame(self.root, style="Surface.TFrame", padding=8)
        self.canvas = tk.Canvas(self.frame, background=COLORS["surface"], highlightthickness=0)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.body = ttk.Frame(self.canvas, style="Inset.TFrame")
        self.item = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.buttons: list[ttk.Button] = []
        for index, (label, value) in enumerate(choices):
            button = ttk.Button(self.body, text=("✓  " if value == selected else "    ") + label,
                                style="Selected.Dropdown.Inset.TButton" if value == selected else "Dropdown.Inset.TButton", command=lambda index=index: self.confirm(index))
            button.pack(fill=tk.X, pady=3)
            button.bind("<Up>", lambda _event: self.move(-1))
            button.bind("<Down>", lambda _event: self.move(1))
            button.bind("<Return>", lambda _event: self.confirm(self.index))
            button.bind("<space>", lambda _event: self.confirm(self.index))
            button.bind("<Escape>", lambda _event: self.close())
            button.bind("<FocusIn>", lambda _event, index=index: self._focused(index))
            button.bind("<MouseWheel>", self._wheel)
            self.buttons.append(button)
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(self.item, width=event.width))
        self.body.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.focus_timer: str | None = None
        self.outside_binding = self.root.bind("<ButtonPress-1>", self._outside, add=True)
        self.key_binding = self.root.bind("<Escape>", lambda _event: self.close(), add=True)
        self.focus_binding = self.root.bind("<FocusOut>", self._focus_out, add=True)
        scale = max(1, float(self.root.tk.call("tk", "scaling")) / (96 / 72))
        width = min(max(anchor.winfo_width(), round(260 * scale)), max(120, self.root.winfo_width() - 16))
        self.canvas.configure(width=width - 16, height=1)
        self.frame.place(x=8, y=8, width=width)
        self.frame.update_idletasks()
        height = min(self.body.winfo_reqheight(), max(60, min(round(360 * scale), self.root.winfo_height() - 80)))
        self.canvas.configure(height=height)
        if self.body.winfo_reqheight() > height:
            scroll = ttk.Scrollbar(self.frame, orient=tk.VERTICAL, style="Chat.Vertical.TScrollbar", command=self.canvas.yview)
            scroll.pack(side=tk.RIGHT, fill=tk.Y)
            self.canvas.configure(yscrollcommand=scroll.set)
        x = anchor.winfo_rootx() - self.root.winfo_rootx()
        below = anchor.winfo_rooty() - self.root.winfo_rooty() + anchor.winfo_height() + 4
        total_height = height + 16
        y = below if below + total_height < self.root.winfo_height() - 8 else below - anchor.winfo_height() - total_height - 8
        self.frame.place(x=max(8, min(x, self.root.winfo_width() - width - 8)), y=max(8, y), width=width, height=total_height)
        self.frame.lift()
        if self.buttons:
            self.buttons[self.index].focus_set()

    def _focused(self, index: int) -> None:
        """Keep keyboard confirmation aligned with the actually focused option."""
        self.index = index

    def move(self, step: int) -> str:
        """Move between rounded choices and scroll the focused row into view."""
        if self.buttons:
            self.index = max(0, min(len(self.buttons) - 1, self.index + step))
            button = self.buttons[self.index]
            button.focus_set()
            top = button.winfo_y()
            visible = self.canvas.canvasy(0)
            if top < visible or top + button.winfo_height() > visible + self.canvas.winfo_height():
                self.canvas.yview_moveto(max(0, top - self.canvas.winfo_height() // 2) / max(1, self.body.winfo_height()))
        return "break"

    def confirm(self, index: int) -> str:
        """Dismiss before invoking the captured choice exactly once."""
        if not self.closed and 0 <= index < len(self.choices):
            value = self.choices[index][1]
            self.close()
            self.choose(value)
        return "break"

    def _outside(self, event: tk.Event) -> None:
        """Dismiss clicks outside the menu while letting the clicked control keep its action."""
        if self.closed:
            return
        for widget in (self.frame, self.anchor):
            if widget.winfo_rootx() <= event.x_root < widget.winfo_rootx() + widget.winfo_width() and widget.winfo_rooty() <= event.y_root < widget.winfo_rooty() + widget.winfo_height():
                return
        self.close(restore_focus=False)

    def _focus_out(self, _event: tk.Event) -> None:
        """Defer focus dismissal until keyboard traversal has reached its next control."""
        if not self.closed and self.focus_timer is None:
            self.focus_timer = self.root.after_idle(self._check_focus)

    def _check_focus(self) -> None:
        """Keep focus changes inside the menu, dismissing on navigation outside it."""
        self.focus_timer = None
        if self.closed:
            return
        focused = self.root.focus_get()
        if focused is self.anchor:
            return
        if focused is None or not str(focused).startswith(str(self.frame) + "."):
            self.close(restore_focus=False)

    def _wheel(self, event: tk.Event) -> str:
        """Scroll bounded option lists without moving the underlying conversation."""
        self.canvas.yview_scroll(-round(event.delta / 120 * 3), "units")
        return "break"

    def close(self, *, restore_focus: bool = True) -> str:
        """Release bindings and menu widgets without creating a permission or conversation event."""
        if self.closed:
            return "break"
        self.closed = True
        if self.focus_timer:
            self.root.after_cancel(self.focus_timer)
        self.root.unbind("<ButtonPress-1>", self.outside_binding)
        self.root.unbind("<Escape>", self.key_binding)
        self.root.unbind("<FocusOut>", self.focus_binding)
        self.frame.destroy()
        if restore_focus and self.anchor.winfo_exists():
            self.anchor.focus_set()
        return "break"


class DropdownButton(ttk.Button):
    """Use the same rounded button and hover states for selectors and ordinary controls."""

    def __init__(self, parent: tk.Widget, value: tk.StringVar, options: Callable[[], list[str]], changed: Callable[[str], None] | None = None, **kwargs: Any) -> None:
        """Bind a shared rounded selector to ordinary state and an inline option menu."""
        self.value = value
        self.options = options
        self.changed = changed
        self.menu: DropdownMenu | None = None
        self.label = tk.StringVar(master=parent)
        super().__init__(parent, textvariable=self.label, command=self._toggle, **kwargs)
        self.selector_style = str(self.cget("style") or "TButton")
        self.indicator_element = "Flux.Selector" + str(self).replace(".", "_")
        self.indicator_images: dict[str, ImageTk.PhotoImage] = {}
        self.refresh_icon()
        self.trace = value.trace_add("write", self._updated)
        self.bind("<Down>", lambda _event: self._toggle())
        self.bind("<Destroy>", self._destroyed, add=True)
        self._updated()

    def refresh_icon(self) -> None:
        """Draw a subdued rounded chevron that stays independent of the font."""
        scale = max(0.5, float(self.tk.call("tk", "scaling")) / (96 / 72) * getattr(self._root(), "_flux_zoom", 100) / 100)
        size = round(14 * scale)
        arrow = Image.new("RGBA", (size * 3, size * 3))
        drawing = ImageDraw.Draw(arrow)
        points = [(size * 0.65, size * 1.05), (size * 1.5, size * 1.9), (size * 2.35, size * 1.05)]
        stroke = max(3, round(size * 0.35))
        drawing.line(points, fill=COLORS["muted"], width=stroke, joint="curve")
        for x, y in points:
            drawing.ellipse((x - stroke / 2, y - stroke / 2, x + stroke / 2, y + stroke / 2), fill=COLORS["muted"])
        theme = ttk.Style(self)
        current = theme.theme_use()
        if current not in self.indicator_images:
            self.indicator_images[current] = ImageTk.PhotoImage(arrow.resize((size, size), Image.Resampling.LANCZOS), master=self)
        self.arrow = self.indicator_images[current]
        if self.indicator_element not in theme.element_names():
            theme.element_create(self.indicator_element, "image", self.arrow)
        layout = theme.layout(self.selector_style)
        def separate(elements: list) -> None:
            """Reserve the trailing edge for the chevron and let text use remaining space."""
            for name, options in elements:
                if name == "Button.padding":
                    options["children"] = [(self.indicator_element, {"side": "right", "sticky": "e"}),
                                           ("Button.label", {"sticky": "we"})]
                else:
                    separate(options.get("children", []))
        separate(layout)
        style = "Selector" + str(self).replace(".", "_") + "." + self.selector_style
        theme.layout(style, layout)
        theme.configure(style, anchor="w")
        self.configure(style=style, image="")

    def _updated(self, *_args: object) -> None:
        """Refresh the current value while retaining the dropdown affordance."""
        self.label.set(self.value.get() or "Select model")

    def _toggle(self) -> str:
        """Open or dismiss choices only while the originating selector is enabled."""
        if self.instate(["disabled"]):
            return "break"
        if self.menu and not self.menu.closed:
            self.menu.close()
        else:
            choices = [(value, value) for value in self.options()]
            if choices:
                self.menu = DropdownMenu(self, choices, self._selected, self.value.get())
        return "break"

    def _selected(self, value: str) -> None:
        """Update the value before delivering its existing application callback."""
        self.value.set(value)
        if self.changed:
            self.changed(value)

    def _destroyed(self, event: tk.Event) -> None:
        """Release the value trace and any menu with the owning selector."""
        if event.widget is self:
            if self.menu:
                self.menu.close(restore_focus=False)
            self.value.trace_remove("write", self.trace)


class SessionSearch(ttk.Frame):
    """Keep a compact in-field search hint separate from the actual query value."""

    def __init__(self, parent: tk.Widget, value: tk.StringVar) -> None:
        """Create a rounded entry with an empty-state magnifier and keyboard-friendly focus."""
        super().__init__(parent, style="Sidebar.TFrame")
        self.value = value
        self.entry = ttk.Entry(self, textvariable=value, style="Search.TEntry")
        self.entry.pack(fill=tk.X)
        self.placeholder = ttk.Label(self, text="Search sessions…", style="SearchPlaceholder.TLabel", takefocus=False, compound=tk.LEFT)
        self.refresh_icon()
        self.placeholder.bind("<Button-1>", self._focus_entry)
        self.entry.bind("<FocusIn>", self._update_placeholder)
        self.entry.bind("<FocusOut>", self._update_placeholder)
        self.trace = value.trace_add("write", self._update_placeholder)
        self.bind("<Destroy>", self._destroyed)
        self._update_placeholder()

    def refresh_icon(self) -> None:
        """Render a muted magnifier without emoji fallback or colored glyphs."""
        self._zoom_percent = getattr(self._root(), "_flux_zoom", 100)
        scale = max(0.5, float(self.tk.call("tk", "scaling")) / (96 / 72) * getattr(self._root(), "_flux_zoom", 100) / 100)
        size = round(18 * scale)
        image = Image.new("RGBA", (size * 3, size * 3))
        drawing = ImageDraw.Draw(image)
        drawing.ellipse((size * 0.4, size * 0.4, size * 1.9, size * 1.9), outline=COLORS["muted"], width=max(3, round(size * 0.3)))
        drawing.line((size * 1.65, size * 1.65, size * 2.55, size * 2.55), fill=COLORS["muted"], width=max(3, round(size * 0.3)))
        self.icon = ImageTk.PhotoImage(image.resize((size, size), Image.Resampling.LANCZOS), master=self)
        self.placeholder.configure(image=self.icon)

    def _focus_entry(self, _event: tk.Event) -> str:
        """Let clicking the visual hint focus the real search input."""
        self.entry.focus_set()
        return "break"

    def _update_placeholder(self, *_args: object) -> None:
        """Show the hint only for an empty, unfocused query without writing it into state."""
        if not self.value.get() and self.entry.focus_get() is not self.entry:
            self.placeholder.place(x=round(12 * getattr(self, "_zoom_percent", 100) / 100), rely=0.5, anchor="w")
        else:
            self.placeholder.place_forget()

    def _destroyed(self, event: tk.Event) -> None:
        """Remove the variable callback with its owning search field."""
        if event.widget is self:
            self.value.trace_remove("write", self.trace)


class SessionList(ttk.Frame):
    """Show separate conversation cards with owner-bound navigation and actions."""

    def __init__(self, parent: tk.Widget, select: Callable[[str], None], pin: Callable[[str], None], menu: Callable[[str], None]) -> None:
        """Create a scrollable sidebar whose keyboard and row actions share stable identities."""
        super().__init__(parent, style="Sidebar.TFrame")
        self.select = select
        self.pin = pin
        self.menu = menu
        self.active: str | None = None
        self.items: list[dict[str, Any]] = []
        self.rows: dict[str, tuple[ttk.Frame, ttk.Label, ttk.Label, ttk.Button, ttk.Button]] = {}
        self.canvas = tk.Canvas(self, width=240, background=COLORS["sidebar"], highlightthickness=0, takefocus=False)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.body = ttk.Frame(self.canvas, style="Sidebar.TFrame")
        self.item = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._resized)
        self.canvas.bind("<MouseWheel>", self._wheel)

    def replace(self, items: list[dict[str, Any]], active: str | None, pending: set[str]) -> None:
        """Refresh cards without changing the selected chat or losing sidebar scroll position."""
        fraction = self.canvas.yview()[0]
        for child in self.body.winfo_children():
            child.destroy()
        self.rows.clear()
        self.items = items
        self.active = active
        if not items:
            ttk.Label(self.body, text="No conversations here", style="Sidebar.TLabel", padding=(8, 16)).pack(anchor="w")
        for metadata in items:
            owner = metadata["conversation_id"]
            card = ttk.Frame(self.body, style="Session.TFrame", padding=(10, 10))
            card.pack(fill=tk.X, pady=(0, 8), padx=1)
            card.columnconfigure(0, weight=1)
            title = ttk.Label(card, text=metadata.get("title", "New conversation"), style="Session.TLabel", takefocus=True, anchor="w")
            title.grid(row=0, column=0, sticky="ew", padx=(0, 6))
            pin = ttk.Button(card, text="📌", width=2, style="SessionAction.TButton", command=lambda owner=owner: self.pin(owner))
            pin.grid(row=0, column=1, sticky="n")
            more = ttk.Button(card, text="⋯", width=2, style="SessionAction.TButton", command=lambda owner=owner: self.menu(owner))
            more.grid(row=0, column=2, sticky="n")
            status = " · ".join(label for label, enabled in (("Pinned", metadata.get("pinned")), ("Archived", metadata.get("archived")), ("Permission needed", owner in pending)) if enabled)
            detail = ttk.Label(card, text=metadata.get("snippet", "")[:130] or status or "Conversation", style="SessionDetail.TLabel", anchor="w")
            detail.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
            for widget in (card, title, detail):
                widget.bind("<Button-1>", lambda _event, owner=owner: self._choose(owner))
                widget.bind("<Button-3>", lambda _event, owner=owner: self.menu(owner))
            title.bind("<Return>", lambda _event, owner=owner: self._choose(owner))
            title.bind("<space>", lambda _event, owner=owner: self._choose(owner))
            title.bind("<Up>", lambda _event, owner=owner: self._move(owner, -1))
            title.bind("<Down>", lambda _event, owner=owner: self._move(owner, 1))
            title.bind("<FocusIn>", lambda _event, title=title: title.configure(style="Focus.Session.TLabel"))
            title.bind("<FocusOut>", lambda _event: self.set_active(self.active))
            ContextHint(title, lambda metadata=metadata: metadata.get("title", "Conversation") + "\n" + metadata.get("snippet", ""))
            ContextHint(pin, lambda metadata=metadata: ("Unpin" if metadata.get("pinned") else "Pin") + " conversation · " + metadata.get("title", ""))
            ContextHint(more, lambda metadata=metadata: "Rename, archive, or delete · " + metadata.get("title", ""))
            for widget in (card, title, detail, pin, more):
                widget.bind("<MouseWheel>", self._wheel)
            self.rows[owner] = (card, title, detail, pin, more)
        self.set_active(active)
        self._resize_rows(self.canvas.winfo_width())
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(fraction)

    def set_active(self, owner: str | None) -> None:
        """Keep selection styling independent of asynchronous navigation responses."""
        self.active = owner
        for identifier, (card, title, detail, pin, more) in self.rows.items():
            prefix = "SelectedSession" if identifier == owner else "Session"
            card.configure(style=prefix + ".TFrame")
            title.configure(style="Focus.Session.TLabel" if title.focus_get() is title else prefix + ".TLabel")
            detail.configure(style=prefix + "Detail.TLabel")
            pinned = next(item.get("pinned", False) for item in self.items if item["conversation_id"] == identifier)
            pin.configure(style=("Pinned." if pinned else "") + prefix + "Action.TButton")
            more.configure(style=prefix + "Action.TButton")

    def _choose(self, owner: str) -> str:
        """Navigate to exactly the clicked or keyboard-focused conversation."""
        self.rows[owner][1].focus_set()
        self.set_active(owner)
        self.select(owner)
        return "break"

    def _move(self, owner: str, step: int) -> str:
        """Move and select a card with Up/Down while keeping it in view."""
        owners = list(self.rows)
        index = max(0, min(len(owners) - 1, owners.index(owner) + step))
        target = owners[index]
        self.rows[target][1].focus_set()
        self._choose(target)
        card = self.rows[target][0]
        top = card.winfo_y()
        visible = self.canvas.canvasy(0)
        height = self.canvas.winfo_height()
        if top < visible or top + card.winfo_height() > visible + height:
            self.canvas.yview_moveto(max(0, top - height // 2) / max(1, self.body.winfo_height()))
        return "break"

    def _resized(self, event: tk.Event) -> None:
        """Wrap card titles and snippets to the actual resized sidebar width."""
        self.canvas.itemconfigure(self.item, width=event.width)
        self._resize_rows(event.width)

    def _resize_rows(self, width: int) -> None:
        """Keep distinct row surfaces and their action buttons within the sidebar."""
        for _, title, detail, pin, more in self.rows.values():
            title.configure(wraplength=max(65, width - 36 - pin.winfo_reqwidth() - more.winfo_reqwidth()))
            detail.configure(wraplength=max(100, width - 28))

    def _wheel(self, event: tk.Event) -> str:
        """Scroll session cards without sending wheel input to the chat."""
        self.canvas.yview_scroll(-round(event.delta / 120 * 3), "units")
        return "break"


@dataclass(frozen=True)
class MarkdownBlock:
    """Represent presentation text without evaluating HTML or embedded resources."""

    kind: str
    text: str
    language: str = ""


def markdown_blocks(text: str) -> list[MarkdownBlock]:
    """Parse streamed Markdown into bounded text blocks; unfinished fences are code."""
    blocks: list[MarkdownBlock] = []
    lines = text.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        line = lines[index]
        fence = re.match(r"^\s*(```+|~~~+)(.*)$", line.rstrip())
        if fence:
            marker, language = fence.groups()
            index += 1
            code = []
            while index < len(lines) and not lines[index].lstrip().startswith(marker):
                code.append(lines[index])
                index += 1
            blocks.append(MarkdownBlock("code", "".join(code), language.strip()[:40]))
        elif re.match(r"^#{1,6}\s", line):
            blocks.append(MarkdownBlock("heading", re.sub(r"^#+\s+", "", line)))
        elif re.match(r"^\s*([-*+] |\d+\. )", line):
            blocks.append(MarkdownBlock("list", re.sub(r"^\s*[-*+] ", "• ", line)))
        elif "|" in line and index + 1 < len(lines) and re.match(r"^[\s|:-]+$", lines[index + 1].strip()):
            table = [line]
            index += 2
            while index < len(lines) and "|" in lines[index]:
                table.append(lines[index])
                index += 1
            index -= 1
            rows = [[cell.strip() for cell in row.strip().strip("|").split("|")] for row in table]
            widths = [min(35, max(len(row[column]) if column < len(row) else 0 for row in rows)) for column in range(max(map(len, rows)))]
            rendered = "\n".join("  ".join(cell.ljust(widths[column]) for column, cell in enumerate(row)) for row in rows)
            blocks.append(MarkdownBlock("table", rendered + "\n"))
        else:
            blocks.append(MarkdownBlock("paragraph", line))
        index += 1
    return blocks


def contrast_ratio(first: str, second: str) -> float:
    """Calculate WCAG relative-luminance contrast for documented semantic pairs."""
    def luminance(color: str) -> float:
        """Linearize an sRGB color before computing relative luminance."""
        channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
        return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class DialogBody:
    """Provide a shared scrollable temporary view that remains usable at large scaling."""

    def __init__(self, window: tk.Toplevel) -> None:
        """Embed a rounded padded body with keyboard focus scrolling and bounded geometry."""
        outer = ttk.Frame(window)
        outer.pack(fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(outer, width=680, height=480, background=COLORS["canvas"], highlightthickness=0)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(outer, style="Chat.Vertical.TScrollbar", command=self.canvas.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.frame = ttk.Frame(self.canvas, padding=20)
        self.item = self.canvas.create_window((0, 0), window=self.frame, anchor="nw")
        self.frame.bind("<Configure>", self._content_changed)
        self.canvas.bind("<Configure>", self._resized)
        window.bind("<FocusIn>", self._focused, add=True)
        window.bind("<MouseWheel>", self._wheel, add=True)

    def _content_changed(self, _event: tk.Event) -> None:
        """Update scroll bounds after content wraps or appearance changes."""
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resized(self, event: tk.Event) -> None:
        """Keep body content within the visible width while allowing vertical scrolling."""
        self.canvas.itemconfigure(self.item, width=event.width)

    def _focused(self, event: tk.Event) -> None:
        """Bring focused dialog controls into view during keyboard navigation."""
        widget = event.widget
        if not str(widget).startswith(str(self.frame)):
            return
        top = widget.winfo_rooty() - self.frame.winfo_rooty()
        bottom = top + widget.winfo_height()
        visible = self.canvas.canvasy(0)
        height = self.canvas.winfo_height()
        total = max(1, self.frame.winfo_height())
        if top < visible or bottom > visible + height:
            self.canvas.yview_moveto(max(0, min(top, bottom - height)) / total)

    def _wheel(self, event: tk.Event) -> None:
        """Scroll the dialog body over background and controls with no own text scrolling."""
        if event.widget.winfo_class() not in {"Text", "Listbox"}:
            self.canvas.yview_scroll(-round(event.delta / 120), "units")


class ContextHint:
    """Show bounded control context on hover or keyboard focus inside the application."""

    def __init__(self, widget: tk.Widget, text: Callable[[], str]) -> None:
        """Attach an in-app hint without creating another modal or native window."""
        self.widget = widget
        self.text = text
        self.label: tk.Label | None = None
        widget.bind("<Enter>", self.show, add=True)
        widget.bind("<FocusIn>", self.show, add=True)
        widget.bind("<Leave>", self.hide, add=True)
        widget.bind("<FocusOut>", self.hide, add=True)
        widget.bind("<Destroy>", self.hide, add=True)

    def show(self, _event: object = None) -> None:
        """Display a readable short hint within the root's bounds."""
        self.hide()
        root = self.widget.winfo_toplevel()
        self.label = tk.Label(root, text=self.text()[:500], wraplength=420,
                              background=COLORS["raised"], foreground=COLORS["text"], padx=10, pady=8)
        self.label.update_idletasks()
        x = min(max(8, self.widget.winfo_rootx() - root.winfo_rootx()), max(8, root.winfo_width() - self.label.winfo_reqwidth() - 8))
        y = self.widget.winfo_rooty() - root.winfo_rooty() - self.label.winfo_reqheight() - 8
        self.label.place(x=x, y=max(8, y))
        self.label.lift()

    def refresh(self) -> None:
        """Update an already visible hint when the owning action changes state."""
        if self.label is not None:
            self.label.configure(text=self.text()[:500])

    def hide(self, _event: object = None) -> None:
        """Remove the transient hint without changing focus or conversation state."""
        if self.label is not None:
            self.label.destroy()
            self.label = None
