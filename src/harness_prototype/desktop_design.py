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
        self.style.configure("Send.TButton", foreground=COLORS["canvas"], padding=(4, 2), font=(FONT_FAMILY, round(14 * self.zoom), "bold"))
        self.style.map("Send.TButton", foreground=[("disabled", COLORS["disabled"])])
        self.style.layout("Stop.TButton", self.style.layout("Send.TButton"))
        self.style.configure("Stop.TButton", foreground=COLORS["canvas"], padding=(4, 2),
                             font=(FONT_FAMILY, round(14 * self.zoom)))
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
        self._rounded("Flux.chat_surface", COLORS["surface"], RADII["container"])
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
            background = (0, 0, 0, 0) if name == "Flux.chat_surface" else corner or COLORS["canvas"]
            image = Image.new("RGBA", (size * 3, size * 3), background)
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


class ChatSurface(tk.Canvas):
    """Paint a quiet palette gradient and an optional responsive welcome composition."""

    def __init__(
        self, parent: tk.Widget, start: Callable[[str], None] | None = None,
        content_height: Callable[[], int] | None = None,
        source: "ChatSurface | None" = None,
    ) -> None:
        """Keep decorative pixels separate from selectable conversation content."""
        super().__init__(parent, background=COLORS["canvas"], highlightthickness=0, borderwidth=0)
        self.start = start
        self.content_height = content_height
        self.source = source
        self.footer: tk.Canvas | None = None
        self.reading_area: tk.Text | None = None
        self.buttons: list[ttk.Button] = []
        self.heading_font = font.Font(root=self, font="TkDefaultFont")
        self.caption_font = font.Font(root=self, font="TkDefaultFont")
        if start:
            for label, prompt in (("Explore code", "Help me understand this codebase."),
                                  ("Find a bug", "Help me investigate a bug."),
                                  ("Plan a change", "Help me plan a change to this project.")):
                self.buttons.append(ttk.Button(self, text=label, style="Inset.TButton",
                                               command=lambda prompt=prompt: start(prompt)))
        self.redraw_timer: str | None = None
        self.redraw_owner = self._root()
        self.bind("<Configure>", self.queue_redraw)
        self.bind("<Destroy>", self._cancel_redraw, add=True)
        if source:
            source.bind("<<GradientChanged>>", self.queue_redraw, add=True)

    def queue_redraw(self, _event: object = None) -> None:
        """Combine backdrop geometry changes into one paint after native layout settles."""
        if not self.winfo_exists():
            return
        if self.redraw_timer is None:
            if self.source or not hasattr(self, "gradient_bitmap"):
                self.redraw_timer = self.redraw_owner.after_idle(self._run_redraw)
            else:
                self.redraw_timer = self.redraw_owner.after(MOTION["frame_ms"], self._run_redraw)

    def _run_redraw(self) -> None:
        """Clear the executing timer before painting to avoid canceling its Tcl callback."""
        self.redraw_timer = None
        self.redraw()

    def _cancel_redraw(self, event: tk.Event) -> None:
        """Release a queued backdrop paint with its owning widget."""
        if event.widget is self and self.redraw_timer is not None:
            self.redraw_owner.after_cancel(self.redraw_timer)
            self.redraw_timer = None

    def redraw(self) -> None:
        """Blend dark charcoal and teal, with centered typography that fits narrow panes."""
        if not self.winfo_exists():
            return
        if self.redraw_timer is not None:
            self.redraw_owner.after_cancel(self.redraw_timer)
            self.redraw_timer = None
        if self.start and not self.winfo_ismapped():
            return
        footer = self.footer
        extent = self.master if footer else self
        width, height = max(1, extent.winfo_width()), max(1, extent.winfo_height())
        reading_area = self.reading_area
        bounds = None
        if reading_area and reading_area.winfo_exists():
            top = max(1, reading_area.winfo_y())
            bounds = (top, min(height, top + reading_area.winfo_height()))
        signature = (width, height, bounds, id(getattr(self.source, "gradient_bitmap", None)),
                     self.winfo_rootx() if self.source else 0,
                     self.winfo_rooty() if self.source else 0)
        if not self.start and signature == getattr(self, "paint_signature", None):
            return
        self.paint_signature = signature
        base = tuple(int(COLORS["canvas"][index:index + 2], 16) for index in (1, 3, 5))
        teal = tuple(int(COLORS["selected"][index:index + 2], 16) for index in (1, 3, 5))
        charcoal = tuple(int(COLORS["titlebar"][index:index + 2], 16) for index in (1, 3, 5))
        if self.source and hasattr(self.source, "gradient_bitmap"):
            left = max(0, self.winfo_rootx() - self.source.winfo_rootx())
            top = max(0, self.winfo_rooty() - self.source.winfo_rooty())
            self.gradient_bitmap = self.source.gradient_bitmap.crop((left, top, left + width, top + height))
        else:
            # Ordered sub-channel dithering avoids large flat bands on dark 8-bit displays.
            thresholds = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))
            tile = Image.new("RGB", (4, height))
            pixels = []
            for y in range(height):
                fraction = 1 - y / max(1, height - 1)
                if bounds:
                    # Keep native display-row color steps imperceptible while retaining
                    # the teal wash through the entire conversation and composer.
                    fraction = 0.35 + fraction * 0.35
                glow, shade = fraction ** 1.6 * 0.45, fraction * 0.2
                channels = tuple(base[channel] * (1 - glow - shade)
                                 + teal[channel] * glow + charcoal[channel] * shade for channel in range(3))
                for x in range(4):
                    threshold = (thresholds[y % 4][x] + 0.5) / 16
                    pixels.append(tuple(int(value + threshold) for value in channels))
            tile.putdata(pixels)
            raw = tile.tobytes()
            rows = ((raw[y * 12:(y + 1) * 12] * ((width + 3) // 4))[:width * 3] for y in range(height))
            self.gradient_bitmap = Image.frombytes("RGB", (width, height), b"".join(rows))
        if bounds and not self.start and not self.source:
            top, bottom = bounds
            self._gradient_image = ImageTk.PhotoImage(self.gradient_bitmap.crop((0, 0, width, top)), master=self)
            self._gradient_bottom_image = ImageTk.PhotoImage(
                self.gradient_bitmap.crop((0, bottom, width, max(bottom + 1, height))), master=self)
            lower_canvas = footer or self
            offset = bottom - footer.winfo_y() if footer else bottom
            if lower_canvas.find_withtag("gradient_bottom"):
                lower_canvas.itemconfigure("gradient_bottom", image=self._gradient_bottom_image)
                lower_canvas.coords("gradient_bottom", 0, offset)
            else:
                lower_canvas.create_image(0, offset, image=self._gradient_bottom_image, anchor="nw", tags="gradient_bottom")
        else:
            self._gradient_image = ImageTk.PhotoImage(self.gradient_bitmap, master=self)
            self.delete("gradient_bottom")
        if not self.start and self.find_withtag("gradient"):
            self.itemconfigure("gradient", image=self._gradient_image)
        else:
            if self.start:
                self.delete("all")
            self.create_image(0, 0, image=self._gradient_image, anchor="nw", tags="gradient")
        self.event_generate("<<GradientChanged>>")
        if not self.start:
            return
        if self.content_height:
            height = min(height, max(1, self.content_height()))
        shared = font.nametofont("TkDefaultFont", root=self)
        self.heading_font.configure(**shared.actual())
        compact = height < shared.metrics("linespace") * 16
        self.heading_font.configure(size=round(shared.actual("size") * (1.6 if compact else 2)), weight="bold")
        self.caption_font.configure(**shared.actual())
        self.caption_font.configure(size=max(9, shared.actual("size") - 1))
        scale = max(0.75, shared.metrics("linespace") / 20)
        center = width / 2
        top = max(16, height * (0.12 if compact else 0.24) - 32 * scale)
        logo_size = round((32 if compact else 52) * scale)
        mark = Image.new("RGBA", (logo_size * 4, logo_size * 4))
        draw = ImageDraw.Draw(mark)
        ratio = logo_size * 4 / 32
        for points, color in MARK_PATHS:
            draw.polygon([(points[index] * ratio, points[index + 1] * ratio)
                          for index in range(0, len(points), 2)], fill=color)
        self._welcome_mark = ImageTk.PhotoImage(mark.resize((logo_size, logo_size), Image.Resampling.LANCZOS), master=self)
        self.create_image(center, top, image=self._welcome_mark, anchor="n", tags="welcome-mark")
        title = self.create_text(center, top + logo_size + 24 * scale, text="What are we doing today?",
                                 font=self.heading_font, fill=COLORS["text"], width=max(120, width - 64),
                                 justify="center", anchor="n", tags="welcome-title")
        bottom = self.bbox(title)[3]
        subtitle = self.create_text(center, bottom + 12 * scale,
                                    text="A question, a bug, or your next idea. Let's work on it.",
                                    font="TkDefaultFont", fill=COLORS["muted"], width=max(120, width - 64),
                                    justify="center", anchor="n", tags="welcome-subtitle")
        bottom = self.bbox(subtitle)[3] + 24 * scale
        gap = round(8 * scale)
        widths = [button.winfo_reqwidth() for button in self.buttons]
        horizontal = sum(widths) + gap * 2 < width - 48
        controls_height = max(button.winfo_reqheight() for button in self.buttons) if horizontal else sum(
            button.winfo_reqheight() + gap for button in self.buttons)
        if bottom + controls_height > height - 24:
            if bottom < height - 24:
                self.create_text(center, bottom, text="Start below, or type / for commands",
                                 font=self.caption_font, fill=COLORS["muted"], anchor="n", tags="welcome-hint")
            return
        if horizontal:
            left = (width - sum(widths) - gap * 2) / 2
            for button, button_width in zip(self.buttons, widths):
                self.create_window(left, bottom, window=button, anchor="nw")
                left += button_width + gap
            bottom += max(button.winfo_reqheight() for button in self.buttons)
        else:
            for button in self.buttons:
                self.create_window(center, bottom, window=button, anchor="n")
                bottom += button.winfo_reqheight() + gap
        if bottom + 32 * scale < height:
            self.create_text(center, bottom + 24 * scale, text="Start below, or type / for commands",
                             font=self.caption_font, fill=COLORS["muted"], anchor="n", tags="welcome-hint")


class ChatTranscript(tk.Text):
    """Match native text backgrounds without per-row overlay windows."""

    def __init__(self, parent: tk.Widget, backdrop: ChatSurface, **kwargs: Any) -> None:
        """Keep selection native and repaint row colors before scroll commands return."""
        super().__init__(parent, **kwargs)
        self.backdrop = backdrop
        backdrop.reading_area = self
        self.gradient_tags: set[str] = set()
        self.surface_styles: set[str] = set()
        self.painting = False
        self.paint_timer: str | None = None
        self.paint_signature: tuple | None = None
        self.revision = 0
        self.empty_surface = tk.Canvas(self, background=COLORS["canvas"], borderwidth=0,
                                       highlightthickness=0, takefocus=False)
        self.padding_surfaces = [tk.Canvas(self, background=COLORS["canvas"], borderwidth=0,
                                          highlightthickness=0, takefocus=False) for _ in range(2)]
        self.padding_signature: tuple | None = None
        for canvas in (self.empty_surface, *self.padding_surfaces):
            canvas._flux_gradient_layer = True
            canvas.bind("<MouseWheel>", self._wheel)
            canvas.bind("<Button-1>", lambda _event: self.focus_set())
        self.paint_owner = self._root()
        self.native_command = self._w + "_native"
        self.tk.call("rename", self._w, self.native_command)
        self.tk.createcommand(self._w, self._dispatch)
        self.bind("<Configure>", self._configured, add=True)
        self.bind("<Destroy>", self._destroy_paint, add=True)
        backdrop.bind("<<GradientChanged>>", self._queue_paint, add=True)

    def _wheel(self, event: tk.Event) -> str:
        """Route wheel input over empty background through the native scroll command."""
        units = -round(event.delta / 120 * 3) or (-1 if event.delta > 0 else 1)
        self.yview_scroll(units, "units")
        return "break"

    def _dispatch(self, *args: str) -> Any:
        """Recolor native scroll results before Tk displays the changed viewport."""
        result = self.tk.call(self.native_command, *args)
        if args and not self.painting:
            if args[0] in {"yview", "see"} and len(args) > 1:
                self.paint_rows()
            elif args[0] in {"insert", "delete"}:
                self.revision += 1
                self._queue_paint()
        return result

    def _configured(self, _event: object = None) -> None:
        """Coalesce native geometry and backdrop changes after layout."""
        self.backdrop.queue_redraw()
        self._queue_paint()

    def _queue_paint(self, _event: object = None) -> None:
        """Schedule one row-color update for a batch of content or geometry changes."""
        if self.winfo_exists() and self.paint_timer is None:
            self.paint_timer = self.paint_owner.after_idle(self._run_paint)

    def _run_paint(self) -> None:
        """Release the executing callback before updating native row backgrounds."""
        self.paint_timer = None
        self.paint_rows()

    def _destroy_paint(self, event: tk.Event) -> None:
        """Release pending painting and the scroll proxy when the native text closes."""
        if event.widget is self:
            if self.paint_timer is not None:
                self.paint_owner.after_cancel(self.paint_timer)
                self.paint_timer = None
            self.tk.deletecommand(self._w)

    def paint_rows(self, *, force: bool = False) -> None:
        """Tint only visible display rows using the shared background's local colors."""
        if self.painting or not self.winfo_exists() or not self.backdrop.winfo_exists() or not hasattr(self.backdrop, "gradient_bitmap"):
            return
        image = self.backdrop.gradient_bitmap
        left = max(0, self.winfo_rootx() - self.backdrop.winfo_rootx())
        top = max(0, self.winfo_rooty() - self.backdrop.winfo_rooty())
        if image.width < left + self.winfo_width() or image.height < top + self.winfo_height():
            self.backdrop.redraw()
            image = self.backdrop.gradient_bitmap
        signature = (id(image), self.revision, self.yview(), self.winfo_width(), self.winfo_height(),
                     self.tk.call("font", "metrics", self.cget("font"), "-linespace"))
        if not force and signature == self.paint_signature:
            return
        self.paint_signature = signature
        self.painting = True
        try:
            top = self.winfo_rooty() - self.backdrop.winfo_rooty()
            color = "#" + "".join(f"{channel:02x}" for channel in image.getpixel((0, max(0, min(image.height - 1, top)))))
            if self.cget("background") != color:
                self.configure(background=color)
            for tag in self.gradient_tags:
                self.tag_remove(tag, "1.0", tk.END)
            index = self.index("@0,0 display linestart")
            for _ in range(256):
                info = self.dlineinfo(index)
                if not info or info[1] >= self.winfo_height():
                    break
                following = self.index(index + " +1 display lines")
                if self.compare(following, "<=", index):
                    following = self.index(tk.END)
                y = min(image.height - 1, max(0, top + info[1] + info[3] // 2))
                color = "#" + "".join(f"{channel:02x}" for channel in image.getpixel((0, y)))
                tag = "gradient_" + color[1:]
                if tag not in self.gradient_tags:
                    self.tag_configure(tag, background=color)
                    self.tag_lower(tag)
                    self.gradient_tags.add(tag)
                self.tag_add(tag, index, following)
                if self.compare(following, ">=", "end-1c"):
                    break
                index = following
            last = self.dlineinfo("end-1c")
            width, height = self.winfo_width(), self.winfo_height()
            left = max(0, self.winfo_rootx() - self.backdrop.winfo_rootx())
            padding = int(self.cget("pady"))
            padding_signature = (id(image), width, height, top, left, padding)
            if padding_signature != self.padding_signature:
                self.padding_signature = padding_signature
                for canvas, offset in zip(self.padding_surfaces, (0, max(0, height - padding))):
                    crop = image.crop((left, top + offset, left + width, top + offset + padding))
                    canvas._gradient_asset = ImageTk.PhotoImage(crop, master=self)
                    if canvas.find_withtag("gradient"):
                        canvas.itemconfigure("gradient", image=canvas._gradient_asset)
                    else:
                        canvas.create_image(0, 0, image=canvas._gradient_asset, anchor="nw", tags="gradient")
                    canvas.place(x=0, y=offset, relwidth=1, height=padding, bordermode="outside")
            if last and last[1] + last[3] < height:
                # Tk includes a blank terminal display line after a trailing newline.
                # Cover that unused line too, rather than exposing the widget's top tint.
                terminal_blank = self.get("end-2c", "end-1c") == "\n"
                offset = max(0, last[1] if terminal_blank else last[1] + last[3])
                if getattr(self, "empty_bitmap", None) is not image:
                    self.empty_bitmap = image
                    self.empty_asset = ImageTk.PhotoImage(image, master=self)
                if self.empty_surface.find_withtag("gradient"):
                    self.empty_surface.itemconfigure("gradient", image=self.empty_asset)
                    self.empty_surface.coords("gradient", -left, -top - offset)
                else:
                    self.empty_surface.create_image(-left, -top - offset, image=self.empty_asset, anchor="nw", tags="gradient")
                self.empty_surface.place(x=0, y=offset, relwidth=1, height=height - offset, bordermode="outside")
            else:
                self.empty_surface.place_forget()
            for name in self.window_names():
                widget = self.nametowidget(name)
                if not widget.winfo_ismapped():
                    continue
                index = self.index(name)
                tags = [tag for tag in self.tag_names(index) if tag in self.gradient_tags]
                if tags:
                    color = self.tag_cget(tags[0], "background")
                    if hasattr(widget, "_flux_mark_size"):
                        if widget.cget("background") != color:
                            widget.configure(background=color)
                            draw_mark(widget, widget._flux_mark_size)
                    elif isinstance(widget, ttk.Frame) and str(widget.cget("style")).endswith("Surface.TFrame"):
                        style = ttk.Style(self)
                        style_name = "Gradient" + color[1:] + style.theme_use() + ".Surface.TFrame"
                        if style_name not in self.surface_styles:
                            style.configure(style_name, background=color)
                            style.layout(style_name, [("Flux.chat_surface", {"sticky": "nsew"})])
                            self.surface_styles.add(style_name)
                        if widget.cget("style") != style_name:
                            widget.configure(style=style_name)
        finally:
            self.painting = False


class GlassComposer(ttk.Frame):
    """Composite the rounded prompt surface over the workspace gradient."""

    def __init__(self, parent: tk.Widget, backdrop: ChatSurface, tint: Callable[[str], None]) -> None:
        """Retain native input widgets while painting their rounded blended surround."""
        self.backdrop = backdrop
        self.tint = tint
        super().__init__(parent, padding=12)
        self.asset = tk.PhotoImage(master=parent, width=1, height=1)
        self.element_name = "Flux.glass" + self._w
        self.style_name = "Glass" + self._w + ".TFrame"
        self.paint_theme: str | None = None
        self._install_style()
        self.bind("<Configure>", lambda _event: self.redraw())
        self.gradient_binding = backdrop.bind("<<GradientChanged>>", lambda _event: self.redraw(), add=True)
        self.bind("<Destroy>", self._release_gradient, add=True)

    def _install_style(self) -> None:
        """Retain the composited surface when interface zoom switches the ttk theme."""
        style = ttk.Style(self)
        current = style.theme_use()
        if current == self.paint_theme:
            return
        if self.element_name not in style.element_names():
            style.element_create(self.element_name, "image", self.asset, width=1, height=1, sticky="nsew")
        style.layout(self.style_name, [(self.element_name, {"sticky": "nsew", "children": [
            ("Frame.padding", {"sticky": "nsew"})]})])
        self.paint_theme = current
        self.configure(style=self.style_name)

    def _release_gradient(self, event: tk.Event) -> None:
        """Release the shared-backdrop listener when a message or composer surface closes."""
        if event.widget is self and self.backdrop.winfo_exists():
            self.backdrop.unbind("<<GradientChanged>>", self.gradient_binding)

    def redraw(self) -> None:
        """Blend the actual backdrop crop and match native controls to its middle tone."""
        if not self.winfo_exists() or not self.backdrop.winfo_exists() or not self.winfo_ismapped() or not hasattr(self.backdrop, "gradient_bitmap"):
            return
        self._install_style()
        width, height = max(1, self.winfo_width()), max(1, self.winfo_height())
        left = max(0, self.winfo_rootx() - self.backdrop.winfo_rootx())
        top = max(0, self.winfo_rooty() - self.backdrop.winfo_rooty())
        signature = (id(self.backdrop.gradient_bitmap), width, height, left, top, self.paint_theme)
        if signature == getattr(self, "paint_signature", None):
            return
        self.paint_signature = signature
        background = self.backdrop.gradient_bitmap.crop((left, top, left + width, top + height))
        self.blended_bitmap = Image.blend(background, Image.new("RGB", (width, height), COLORS["surface"]), 0.76)
        mask = Image.new("L", (width, height))
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=RADII["container"], fill=255)
        composed = Image.composite(self.blended_bitmap, background, mask)
        image = ImageTk.PhotoImage(composed, master=self)
        self.asset.configure(width=width, height=height)
        self.tk.call(str(self.asset), "copy", str(image))
        color = self.blended_bitmap.getpixel((width // 2, height // 3))
        self.tint("#" + "".join(f"{channel:02x}" for channel in color))


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
