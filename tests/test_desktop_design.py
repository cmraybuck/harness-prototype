"""Verify design contrast, Markdown, platform geometry, and inert link policies."""

import ctypes
from ctypes import wintypes
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from harness_prototype.desktop import ConversationView, FluxDesktop
from harness_prototype.desktop_design import CAPTION_GLYPHS, COLORS, FONT_FAMILY, ContextHint, DropdownButton, DropdownMenu, FluxTheme, SessionSearch, contrast_ratio, draw_mark, markdown_blocks, set_application_icon
from harness_prototype.desktop_platform import WindowFrame, open_link, restored_geometry


class DesktopDesignTests(unittest.TestCase):
    """Exercise visual contracts without a model or personal filesystem fixtures."""

    @patch("harness_prototype.desktop_design.font.nametofont")
    @patch("harness_prototype.desktop_design.ttk.Style")
    @patch("harness_prototype.desktop_design.ImageTk.PhotoImage", side_effect=lambda image, **kwargs: image)
    def test_popup_button_assets_match_surface_in_every_state(
        self, _images: MagicMock, style_factory: MagicMock, _fonts: MagicMock,
    ) -> None:
        """Keep rounded corners surface colored and delete text red in both selections."""
        root = MagicMock()
        root._flux_zoom = 100
        root.tk.call.return_value = 96 / 72
        style = style_factory.return_value
        style.theme_names.return_value = ()
        style.element_names.return_value = ()
        FluxTheme(root)
        style.layout.assert_any_call("Horizontal.Sash", [("Flux.sash", {"sticky": "nsew"})])
        sash_call = next(call for call in style.element_create.call_args_list if call.args[0] == "Flux.sash")
        self.assertEqual(sash_call.args[2].getcolors(), [(36, (32, 36, 39))])
        for element in ("Flux.insetbutton", "Flux.dropdownselected"):
            call = next(call for call in style.element_create.call_args_list if call.args[0] == element)
            images = [call.args[2], *(state[1] for state in call.args[3:])]
            for image in images:
                self.assertEqual(image.getpixel((0, 0))[:3], (45, 51, 56))
        for prefix in ("", "Selected."):
            style.configure.assert_any_call(prefix + "Danger.Inset.TButton", foreground=COLORS["failure"])
            style.configure.assert_any_call(prefix + "Neutral.Inset.TButton", foreground=COLORS["text"])

    @patch("harness_prototype.desktop_design.ImageTk.PhotoImage", side_effect=lambda image, **kwargs: image)
    def test_mark_has_antialiased_edges_and_retains_its_image(self, _images: MagicMock) -> None:
        """Render intermediate edge colors at the requested logo size without stale items."""
        canvas = MagicMock()
        canvas.cget.return_value = COLORS["canvas"]
        draw_mark(canvas, 96)
        image = canvas._flux_mark_image
        self.assertEqual(image.size, (96, 96))
        self.assertGreater(len(image.getcolors(maxcolors=96 * 96)), 3)
        canvas.delete.assert_called_once_with("all")
        canvas.create_image.assert_called_once_with(0, 0, image=image, anchor="nw")
        self.assertEqual(canvas._flux_mark_size, 96)

    def test_startup_restores_sidebar_open_even_with_saved_collapse(self) -> None:
        """Keep startup navigation visible while restoring other saved presentation settings."""
        desktop = FluxDesktop.__new__(FluxDesktop)
        desktop._presentation = {"sidebar_collapsed": False}
        desktop._zoom_percent = 100
        desktop._apply_typography = MagicMock()
        desktop._apply_sidebar = MagicMock()
        desktop._presentation_loaded({"sidebar_collapsed": True, "sidebar_width": 300})
        self.assertFalse(desktop._presentation["sidebar_collapsed"])
        self.assertFalse(desktop._sidebar_auto_collapsed)
        self.assertEqual(desktop._presentation["sidebar_width"], 300)
        desktop._apply_sidebar.assert_called_once_with(animate=False)

    def test_application_icon_replaces_default_and_is_retained(self) -> None:
        """Supply multiple FLUX sizes and inherit the icon in later application windows."""
        root = MagicMock()
        with patch("harness_prototype.desktop_design.ImageTk.PhotoImage", side_effect=lambda image, **kwargs: image):
            set_application_icon(root)
        root.iconphoto.assert_called_once_with(True, *root._flux_icons)
        self.assertEqual([image.size for image in root._flux_icons], [(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])
        image = root._flux_icons[-1]
        self.assertEqual(image.getpixel((0, 0))[3], 0)
        self.assertEqual(image.getpixel((128, 40))[:3], (145, 212, 213))

    def test_visible_context_hint_refreshes_without_reopening(self) -> None:
        """Update a displayed action hint immediately when its underlying state changes."""
        hint = ContextHint.__new__(ContextHint)
        hint.label = MagicMock()
        hint.text = lambda: "Open Side Panel"
        hint.refresh()
        hint.label.configure.assert_called_once_with(text="Open Side Panel")
        hint.label.destroy.assert_not_called()

    def test_zoom_steps_coalesce_and_stop_at_limits(self) -> None:
        """Consume Ctrl-wheel and keyboard requests within bounded interface zoom levels."""
        desktop = FluxDesktop.__new__(FluxDesktop)
        desktop.root = MagicMock()
        desktop.root.after.return_value = "zoom-timer"
        desktop._zoom_percent = 100
        desktop._zoom_requested = 100
        desktop._zoom_timer = None
        self.assertEqual(desktop._request_zoom(1), "break")
        desktop._request_zoom(1)
        self.assertEqual(desktop._zoom_requested, 125)
        desktop.root.after.assert_called_once()
        for _ in range(10):
            desktop._request_zoom(1)
        self.assertEqual(desktop._zoom_requested, 150)
        for _ in range(10):
            desktop._request_zoom(-1)
        self.assertEqual(desktop._zoom_requested, 75)
        desktop._request_zoom(0)
        self.assertEqual(desktop._zoom_requested, 100)
        self.assertEqual(desktop._zoom_wheel(MagicMock(delta=120)), "break")
        self.assertEqual(desktop._zoom_requested, 110)

    def test_maximize_icon_tracks_window_state_without_repeated_redraws(self) -> None:
        """Reflect maximize and restore transitions, including external window actions."""
        frame = WindowFrame.__new__(WindowFrame)
        frame.root = MagicMock()
        frame.maximize_button = MagicMock()
        frame._maximize_icon_applied = False
        frame.root.state.return_value = "normal"
        frame.sync_window_state()
        frame.maximize_button.configure.assert_not_called()
        frame.root.state.return_value = "zoomed"
        frame.sync_window_state()
        frame.maximize_button.configure.assert_called_once_with(text=CAPTION_GLYPHS["restore"])
        frame.sync_window_state()
        self.assertEqual(frame.maximize_button.configure.call_count, 1)
        frame.root.state.return_value = "normal"
        frame.sync_window_state()
        frame.maximize_button.configure.assert_called_with(text=CAPTION_GLYPHS["maximize"])
        self.assertEqual(frame.maximize_button.configure.call_count, 2)

    def test_native_maximize_hover_is_applied_only_on_ui_thread(self) -> None:
        """Bridge non-client hover to shared ttk states without reentering Tcl in callbacks."""
        frame = WindowFrame.__new__(WindowFrame)
        frame.user = MagicMock()
        frame.root = MagicMock()
        frame.maximize_button = MagicMock()
        frame.previous = 1
        frame._maximize_hover_applied = False
        frame._window_proc(1, 0x00A0, 9, 0)
        frame.root.assert_not_called()
        frame.maximize_button.state.assert_not_called()
        frame.sync_hover()
        frame.maximize_button.state.assert_called_once_with(["active"])
        frame.sync_hover()
        self.assertEqual(frame.maximize_button.state.call_count, 1)
        frame._window_proc(1, 0x02A2, 0, 0)
        frame.sync_hover()
        frame.maximize_button.state.assert_called_with(["!active"])

    def test_dropdown_selection_dismisses_before_delivering_once(self) -> None:
        """Keep inline menu choices single-shot and dismiss before a captured action runs."""
        menu = DropdownMenu.__new__(DropdownMenu)
        menu.closed = False
        menu.choices = [("Rename", "rename"), ("Archive", "archive")]
        menu.choose = MagicMock(side_effect=lambda _value: self.assertTrue(menu.closed))
        menu.close = MagicMock(side_effect=lambda: setattr(menu, "closed", True))
        menu.confirm(1)
        menu.confirm(1)
        menu.choose.assert_called_once_with("archive")

    def test_dropdown_selector_keeps_disabled_state_and_value_callback(self) -> None:
        """Block opening disabled model choices and update ordinary state before its callback."""
        selector = DropdownButton.__new__(DropdownButton)
        selector.instate = MagicMock(return_value=True)
        selector.options = MagicMock()
        selector._toggle()
        selector.options.assert_not_called()
        selector.value = MagicMock()
        selector.changed = MagicMock()
        selector._selected("chosen-model")
        selector.value.set.assert_called_once_with("chosen-model")
        selector.changed.assert_called_once_with("chosen-model")

    def test_dropdown_focus_on_anchor_does_not_reopen_during_toggle(self) -> None:
        """Retain the menu until the anchor's click command can deliberately close it."""
        menu = DropdownMenu.__new__(DropdownMenu)
        menu.closed = False
        menu.root = MagicMock()
        menu.anchor = MagicMock()
        menu.root.focus_get.return_value = menu.anchor
        menu.close = MagicMock()
        menu._check_focus()
        menu.close.assert_not_called()

    def test_composer_placeholder_never_enters_a_draft(self) -> None:
        """Show a prompt hint separately from real input and remove it when typing starts."""
        desktop = FluxDesktop.__new__(FluxDesktop)
        desktop.message_input = MagicMock()
        desktop.message_input.get.return_value = ""
        desktop.message_input.focus_get.return_value = None
        desktop.composer_placeholder = MagicMock()
        desktop._update_composer_placeholder()
        desktop.composer_placeholder.place.assert_called_once_with(in_=desktop.message_input, x=10, y=8)
        desktop.message_input.insert.assert_not_called()
        desktop.message_input.get.return_value = "Real draft"
        desktop._update_composer_placeholder()
        desktop.composer_placeholder.place_forget.assert_called_once()

    @patch("harness_prototype.desktop.tk.Canvas")
    @patch("harness_prototype.desktop.draw_mark")
    def test_agent_response_uses_inline_mark_without_a_flux_heading(self, draw: MagicMock, canvas: MagicMock) -> None:
        """Identify the assistant with a small adjacent mark instead of a misleading heading."""
        desktop = FluxDesktop.__new__(FluxDesktop)
        desktop.transcript = MagicMock()
        desktop._embed = MagicMock()
        desktop._inline_markdown = MagicMock()
        desktop._markdown("Plain response", "turn:0", ConversationView())
        draw.assert_called_once_with(canvas.return_value, 22)
        desktop._embed.assert_called_once_with(canvas.return_value, "assistant", newline=False)
        self.assertFalse(any("FLUX" in str(call) for call in desktop.transcript.insert.call_args_list))

    @patch("harness_prototype.desktop_design.font.nametofont")
    def test_typography_roles_share_the_logo_typeface(self, named_font: MagicMock) -> None:
        """Keep body, controls, and brand typography in one family with semantic emphasis."""
        theme = FluxTheme.__new__(FluxTheme)
        theme.root = MagicMock()
        theme.style = MagicMock()
        theme.set_text_size("large")
        theme.style.configure.assert_any_call(".", font=(FONT_FAMILY, 13))
        theme.style.configure.assert_any_call("TButton", font=(FONT_FAMILY, 13, "bold"))
        theme.style.configure.assert_any_call("Title.TLabel", font=(FONT_FAMILY, 17, "bold"))

    def test_repeated_reflow_reuses_dimensions_without_tcl_queries(self) -> None:
        """Avoid crossing into Tk for every retained widget on unchanged resize frames."""
        desktop = FluxDesktop.__new__(FluxDesktop)
        widget = MagicMock()
        widget.cget.side_effect = lambda option: 32 if option == "width" else 3
        desktop._reflow_texts = {widget: ("bubble", "x" * 96, 12)}
        desktop._reflow_sizes = {}
        desktop._content_columns = MagicMock(return_value=32)
        desktop._reflow_embedded()
        widget.reset_mock()
        for _ in range(20):
            desktop._reflow_embedded()
        widget.cget.assert_not_called()
        widget.configure.assert_not_called()

    def test_offscreen_reflow_waits_until_text_enters_view(self) -> None:
        """Avoid resizing retained history until its owning embedded window is visible."""
        desktop = FluxDesktop.__new__(FluxDesktop)
        widget = MagicMock()
        widget.winfo_viewable.return_value = False
        desktop._reflow_texts = {widget: ("bubble", "x" * 96, 12)}
        desktop._reflow_sizes = {widget: (32, 3)}
        desktop._content_columns = MagicMock(return_value=24)
        desktop._reflow_embedded()
        widget.configure.assert_not_called()
        self.assertEqual(desktop._reflow_sizes[widget], (32, 3))
        widget.winfo_viewable.return_value = True
        desktop._reflow_embedded()
        widget.configure.assert_called_once_with(width=24, height=4)

    def test_sidebar_breakpoint_does_not_oscillate_during_resize(self) -> None:
        """Keep automatic collapse stable near its breakpoint while preserving user preferences."""
        desktop = FluxDesktop.__new__(FluxDesktop)
        desktop.root = MagicMock()
        desktop.theme = MagicMock(scale=1)
        desktop._presentation = {"sidebar_collapsed": False}
        desktop._set_sidebar_open = MagicMock()
        desktop.root.winfo_width.side_effect = [1000, 1050, 1030, 1120, 1160]
        for _ in range(5):
            desktop._apply_sidebar(animate=False)
        self.assertEqual([call.args[0] for call in desktop._set_sidebar_open.call_args_list],
                         [True, True, False, False, True])
        self.assertFalse(desktop._presentation["sidebar_collapsed"])

    def test_unrelated_widget_geometry_does_not_query_caption_children(self) -> None:
        """Ignore conversation child Configure events before querying native caption geometry."""
        frame = WindowFrame.__new__(WindowFrame)
        frame.handle = 123
        frame.root = MagicMock()
        frame.titlebar = MagicMock()
        frame.caption_widgets = {frame.root, frame.titlebar}
        event = MagicMock(widget=MagicMock())
        frame._cache_layout(event)
        frame.titlebar.winfo_children.assert_not_called()

    def test_caption_layout_reads_each_button_geometry_once(self) -> None:
        """Project local caption geometry correctly without repeated Tcl coordinate queries."""
        frame = WindowFrame.__new__(WindowFrame)
        frame.handle = 123
        frame.root = MagicMock()
        frame.root.tk.call.return_value = 4 / 3
        frame.user = MagicMock()
        frame.titlebar = MagicMock()
        frame.titlebar.winfo_rootx.return_value = 108
        frame.titlebar.winfo_rooty.return_value = 208
        frame.titlebar.winfo_geometry.return_value = "1120x48+0+0"
        frame.maximize_button = MagicMock()
        frame.maximize_button.winfo_geometry.return_value = "48x36+1000+6"
        frame.caption_buttons = [frame.maximize_button]
        def bounds(_handle: int, pointer: object) -> None:
            """Supply the outer native frame origin without accessing the desktop."""
            pointer._obj.left, pointer._obj.top = 100, 200
        frame.user.GetWindowRect.side_effect = bounds
        frame._cache_layout()
        self.assertEqual(frame.caption_bounds, (8, 8, 1128, 56))
        self.assertEqual(frame.caption_controls, [((1008, 14, 1056, 50), 9)])
        frame.maximize_button.winfo_geometry.assert_called_once()
        frame.maximize_button.winfo_rootx.assert_not_called()
        frame.titlebar.winfo_children.assert_not_called()

    def test_semantic_text_and_focus_contrast_meet_targets(self) -> None:
        """Keep all readable state colors above AA text and control contrast targets."""
        for text in ("text", "muted", "accent", "success", "warning", "failure", "disabled"):
            for surface in ("canvas", "titlebar", "sidebar", "surface", "raised", "hover", "selected"):
                with self.subTest(text=text, surface=surface):
                    self.assertGreaterEqual(contrast_ratio(COLORS[text], COLORS[surface]), 4.5)
        self.assertGreaterEqual(contrast_ratio(COLORS["accent"], COLORS["surface"]), 3)
        self.assertGreaterEqual(contrast_ratio(COLORS["border"], COLORS["sidebar"]), 3)

    def test_resize_reflows_live_text_without_destroying_selection(self) -> None:
        """Change bubble dimensions without clearing text, replacing widgets, or rebuilding history."""
        desktop = FluxDesktop.__new__(FluxDesktop)
        desktop._closing = False
        desktop._layout_timer = "frame"
        desktop.transcript = MagicMock()
        desktop.transcript.yview.return_value = (0.2, 0.7)
        bubble = MagicMock()
        bubble.cget.side_effect = lambda option: 64 if option == "width" else 1
        desktop._reflow_texts = {bubble: ("bubble", "x" * 96, 12)}
        desktop._reflow_sizes = {}
        desktop._content_columns = MagicMock(return_value=32)
        desktop._render_conversation = MagicMock()
        desktop._layout_changed()
        bubble.configure.assert_called_once_with(width=32, height=3)
        bubble.delete.assert_not_called()
        bubble.destroy.assert_not_called()
        desktop._render_conversation.assert_not_called()
        desktop.transcript.see.assert_not_called()

    def test_resize_events_share_one_scheduled_frame(self) -> None:
        """Reflow continuously at frame cadence instead of resetting a long resize debounce."""
        desktop = FluxDesktop.__new__(FluxDesktop)
        desktop._closing = False
        desktop._layout_timer = None
        desktop.root = MagicMock()
        desktop.root.after.return_value = "frame"
        for _ in range(20):
            desktop._queue_layout()
        desktop.root.after.assert_called_once_with(16, desktop._layout_changed)
        desktop.root.after_cancel.assert_not_called()

    def test_sidebar_animation_clips_content_and_reduced_motion_is_immediate(self) -> None:
        """Slide a stable sidebar to its exact target, and skip animation when motion is reduced."""
        desktop = FluxDesktop.__new__(FluxDesktop)
        desktop._presentation = {"sidebar_width": 260, "reduced_motion": False}
        desktop.theme = MagicMock(scale=1)
        desktop.root = MagicMock()
        desktop.root.winfo_ismapped.return_value = True
        desktop.navigation = MagicMock()
        desktop.navigation.winfo_width.return_value = 260
        desktop.navigation_content = MagicMock()
        desktop.panes = MagicMock()
        panes = []
        positions = [0]
        desktop.panes.panes.side_effect = lambda: panes
        desktop.panes.insert.side_effect = lambda *_args, **_kwargs: panes.append(str(desktop.navigation))
        desktop.panes.forget.side_effect = lambda *_args: panes.clear()
        def sash(_index: int, position: int | None = None) -> int:
            """Track animated sash geometry without creating native widgets."""
            if position is not None:
                positions.append(position)
            return positions[-1]
        desktop.panes.sashpos.side_effect = sash
        callbacks = []
        def after(_delay: int, callback: object) -> str:
            """Capture scheduled animation steps for deterministic advancement."""
            callbacks.append(callback)
            return "animation"
        desktop.root.after.side_effect = after
        desktop._sidebar_target = 0
        desktop._sidebar_timer = None
        desktop._sidebar_animating = False
        desktop._closing = False
        desktop._queue_layout = MagicMock()
        with patch("harness_prototype.desktop.time.monotonic", side_effect=[0, 0, .09, .18]):
            desktop._set_sidebar_open(True)
            callbacks.pop(0)()
            self.assertGreater(positions[-1], 0)
            self.assertLess(positions[-1], 260)
            callbacks.pop(0)()
        self.assertEqual(positions[-1], 260)
        self.assertFalse(desktop._sidebar_animating)
        desktop.navigation_content.place_configure.assert_called_with(width=260)
        with patch("harness_prototype.desktop.time.monotonic", side_effect=[1, 1, 1, 1, 1.2]):
            desktop._set_sidebar_open(False)
            desktop._set_sidebar_open(True)
            callbacks[-1]()
        desktop.root.after_cancel.assert_called_once_with("animation")
        desktop.panes.forget.assert_not_called()
        self.assertEqual(desktop._sidebar_target, 260)
        self.assertFalse(desktop._sidebar_animating)
        desktop._presentation["reduced_motion"] = True
        desktop.root.after.reset_mock()
        desktop._set_sidebar_open(False)
        desktop.panes.forget.assert_called_once_with(desktop.navigation)
        desktop.root.after.assert_not_called()

    def test_search_placeholder_never_becomes_a_query(self) -> None:
        """Keep the magnifier hint visual and remove it for focus or a real query."""
        search = SessionSearch.__new__(SessionSearch)
        search.value = MagicMock()
        search.value.get.return_value = ""
        search.entry = MagicMock()
        search.entry.focus_get.return_value = None
        search.placeholder = MagicMock()
        search._update_placeholder()
        search.placeholder.place.assert_called_once_with(x=12, rely=0.5, anchor="w")
        search.value.set.assert_not_called()
        search.entry.focus_get.return_value = search.entry
        search._update_placeholder()
        search.placeholder.place_forget.assert_called_once()
        search.placeholder.reset_mock()
        search.value.get.return_value = "existing query"
        search.entry.focus_get.return_value = None
        search._update_placeholder()
        search.placeholder.place_forget.assert_called_once()

    def test_markdown_tables_lists_and_unfinished_code(self) -> None:
        """Parse supported structural Markdown while leaving HTML and remote images inert."""
        blocks = markdown_blocks("# Heading\n- Item\n| Name | Value |\n| --- | --- |\n| first | second |\n```python\nprint('hello')\n")
        self.assertEqual([block.kind for block in blocks], ["heading", "list", "table", "code"])
        self.assertIn("first", blocks[2].text)
        self.assertEqual(blocks[3].language, "python")
        self.assertIn("hello", blocks[3].text)
        self.assertEqual(markdown_blocks("<script>inert</script>")[0].text, "<script>inert</script>")

    def test_native_frame_hit_tests_never_reenter_tcl(self) -> None:
        """Keep native callback hit testing entirely in cached geometry and Win32 calls."""
        frame = WindowFrame.__new__(WindowFrame)
        frame.root = MagicMock()
        frame.user = MagicMock()
        frame.user.IsZoomed.return_value = False
        frame.previous = 123
        frame.resize_edge = 8
        frame.caption_bounds = (0, 0, 1120, 48)
        frame.caption_controls = [((1000, 0, 1060, 48), 9)]
        def rectangle(_handle: int, pointer: object) -> None:
            """Supply native bounds without constructing a live window."""
            pointer._obj.left, pointer._obj.top = 100, 200
            pointer._obj.right, pointer._obj.bottom = 1220, 940
        frame.user.GetWindowRect.side_effect = rectangle
        for x, y, expected in ((400, 220, 2), (1120, 220, 9), (102, 500, 10), (102, 202, 13), (400, 500, 1)):
            with self.subTest(expected=expected):
                packed = (y & 0xffff) << 16 | (x & 0xffff)
                self.assertEqual(frame._window_proc(1, 0x84, 0, packed), expected)
        self.assertEqual(frame.root.mock_calls, [])

    def test_maximized_frame_uses_center_to_select_secondary_monitor(self) -> None:
        """Ignore maximized resize margins that extend into a neighboring monitor."""
        frame = WindowFrame.__new__(WindowFrame)
        frame.user = MagicMock()
        frame.user.IsZoomed.return_value = True
        rectangle = wintypes.RECT(1912, -8, 3848, 1088)

        def bounds(x: int, y: int) -> tuple[int, int, int, int]:
            """Distinguish the secondary work area from the adjacent primary display."""
            return (1920, 0, 3840, 1040) if x >= 1920 else (0, 0, 1920, 1040)

        with patch("harness_prototype.desktop_platform.display_bounds", side_effect=bounds):
            frame._window_proc(1, 0x0083, 1, ctypes.addressof(rectangle))
        self.assertEqual((rectangle.left, rectangle.top, rectangle.right, rectangle.bottom),
                         (1920, 0, 3840, 1040))

    def test_native_maximize_click_uses_release_and_cancels_outside_target(self) -> None:
        """Keep custom caption clicks native, release-bound, and independent of Tcl callbacks."""
        frame = WindowFrame.__new__(WindowFrame)
        frame.root = MagicMock()
        frame.user = MagicMock()
        frame.user.IsZoomed.return_value = False
        frame.maximize_pressed = False
        frame.previous = 123
        frame.resize_edge = 8
        frame.caption_bounds = (0, 0, 1120, 48)
        frame.caption_controls = [((1000, 0, 1060, 48), 9)]
        def rectangle(_handle: int, pointer: object) -> None:
            """Supply a mapped native frame rectangle for the release hit test."""
            pointer._obj.left, pointer._obj.top = 100, 200
            pointer._obj.right, pointer._obj.bottom = 1220, 940
        def cursor(pointer: object) -> None:
            """Place the release within the cached maximize target."""
            pointer._obj.x, pointer._obj.y = 1120, 220
        frame.user.GetWindowRect.side_effect = rectangle
        frame.user.GetCursorPos.side_effect = cursor
        frame._window_proc(1, 0xA1, 9, 0)
        frame.user.PostMessageW.assert_not_called()
        frame._window_proc(1, 0x202, 0, 0)
        frame.user.PostMessageW.assert_called_once_with(1, 0x112, 0xF030, 0)
        frame.user.PostMessageW.reset_mock()
        frame._window_proc(1, 0xA1, 9, 0)
        frame.user.GetCursorPos.side_effect = lambda pointer: setattr(pointer._obj, "x", 400)
        frame._window_proc(1, 0x202, 0, 0)
        frame.user.PostMessageW.assert_not_called()
        frame._window_proc(1, 0xA1, 9, 0)
        frame._window_proc(1, 0x215, 0, 0)
        self.assertFalse(frame.maximize_pressed)
        self.assertEqual(frame.root.mock_calls, [])

    @patch("harness_prototype.desktop_platform.sys.platform", "win32")
    def test_native_restore_does_not_add_old_caption_extents(self) -> None:
        """Apply saved client geometry directly so restarts do not grow the window."""
        frame = WindowFrame.__new__(WindowFrame)
        frame.root = MagicMock()
        frame.user = MagicMock()
        frame.handle = 123
        frame.previous = 456
        frame.restore_geometry({"width": 1120, "height": 740, "x": 80, "y": 50}, (0, 0, 1600, 900))
        frame.user.SetWindowPos.assert_called_once_with(123, None, 80, 50, 1120, 740, 0x0034)
        frame.root.geometry.assert_not_called()

    def test_restored_windows_remain_within_usable_bounds(self) -> None:
        """Recover removed-monitor positions without hiding the titlebar or controls."""
        self.assertEqual(restored_geometry({"width": 1400, "height": 900, "x": 9000, "y": -500}, (0, 0, 1280, 800)), "1280x800+0+0")
        self.assertEqual(restored_geometry({"width": 900, "height": 600, "x": -1400, "y": 20}, (-1920, 0, 0, 1040)), "900x600-1400+20")

    @patch("harness_prototype.desktop_platform.webbrowser.open")
    def test_links_reject_execution_credentials_and_control_characters(self, browser: object) -> None:
        """Permit ordinary web navigation while blocking execution and credential-bearing links."""
        for target in ("javascript:alert(1)", "file:///C:/secret.exe", "https://user:secret@example.com", "https://example.com\n", "https://example.com:bad"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                open_link(target)
        browser.assert_not_called()
        open_link("https://example.com/docs")
        browser.assert_called_once_with("https://example.com/docs")

    @patch("harness_prototype.desktop_platform.subprocess.Popen")
    @patch("harness_prototype.desktop_platform.sys.platform", "win32")
    def test_file_locations_reveal_existing_files_without_opening_them(self, launch: object) -> None:
        """Use Explorer selection only after absolute local-file validation."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample file.txt"
            path.write_text("Fixture", encoding="utf-8")
            open_link(str(path), reveal_file=True)
            launch.assert_called_once_with(["explorer.exe", "/select," + str(path.resolve())])
            with self.assertRaises(ValueError):
                open_link(str(path.parent / "missing.txt"), reveal_file=True)
