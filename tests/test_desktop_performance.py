"""Guard Windows resize/scroll responsiveness using inert production GUI fixtures."""

import sys
import time
import tkinter as tk
from tkinter import font
import unittest

from desktop_fixtures import build_fixture


@unittest.skipUnless(sys.platform == 'win32', 'Windows repaint batching is platform-specific')
class DesktopPerformanceTests(unittest.TestCase):
    """Exercise mapped widgets without a backend, model, or persistent user state."""

    def test_whole_interface_zoom_preserves_draft_and_turn_widgets(self) -> None:
        """Scale controls, fonts, and spacing without restarting conversation presentation."""
        root = tk.Tk()
        root.withdraw()
        try:
            desktop = build_fixture(root, 'working')
            root.deiconify()
            root.update()
            desktop.message_input.insert('1.0', 'Keep this unsent draft')
            original = list(desktop._transcript_widgets)
            owner = desktop.active_conversation_id
            base_arrow = desktop.model_selector.arrow.width()
            for percent in (125, 150, 75, 90, 110, 100, 125, 100):
                desktop._zoom_requested = percent
                desktop._apply_zoom()
                root.update()
                self.assertEqual(desktop._transcript_widgets, original)
                self.assertEqual(desktop.active_conversation_id, owner)
                self.assertEqual(desktop.message_input.get('1.0', 'end-1c'), 'Keep this unsent draft')
                self.assertEqual(font.nametofont('TkTextFont', root=root).cget('size'), round(11 * percent / 100))
                self.assertAlmostEqual(desktop.model_selector.arrow.width(), base_arrow * percent / 100, delta=1)
                self.assertIn('FluxZoom', desktop.message_input.bindtags())
                self.assertIn('FluxZoom', original[0].bindtags())
                self.assertEqual(int(desktop.message_input.cget('padx')), round(8 * percent / 100))
                desktop.model_selector._toggle()
                root.update_idletasks()
                desktop.model_selector.menu.close()
            desktop._zoom_requested = 150
            desktop._request_zoom(1)
            self.assertEqual(desktop._zoom_requested, 150)
            desktop._request_zoom(0)
            self.assertEqual(desktop._zoom_requested, 100)
            self.assertEqual(desktop._presentation['zoom_percent'], 100)
            button = desktop.model_selector
            positions = [x for x in range(button.winfo_width()) if button.identify(x, button.winfo_height() // 2) == button.indicator_element]
            self.assertTrue(positions)
            self.assertGreater(min(positions), button.winfo_width() * 0.7)
            desktop.sidebar_hint.show()
            hint = desktop.sidebar_hint.label
            desktop.toggle_sidebar()
            self.assertEqual(hint.cget('text'), desktop.sidebar_hint.text())
            desktop.toggle_sidebar()
            self.assertEqual(hint.cget('text'), desktop.sidebar_hint.text())
            self.assertIn('Side Panel', hint.cget('text'))
            desktop.transcript.yview_moveto(0)
            position = desktop.transcript.yview()
            desktop.transcript.event_generate('<MouseWheel>', delta=120, state=0x4, when='now')
            self.assertEqual(desktop._zoom_requested, 110)
            self.assertEqual(desktop.transcript.yview(), position)
            desktop._zoom_requested = 125
            desktop._apply_zoom()
            desktop._settings_loaded((desktop.client.settings(), desktop.client.list_grants()))
            root.update()
            windows = [child for child in root.winfo_children() if isinstance(child, tk.Toplevel)]
            self.assertEqual(len(windows), 1)
            self.assertIn('FluxZoom', windows[0].bindtags())
            windows[0].destroy()
        finally:
            if 'desktop' in locals():
                desktop._closing = True
            for token in root.tk.call('after', 'info'):
                root.after_cancel(token)
            root.destroy()

    def test_resize_and_scroll_preserve_visible_responsiveness(self) -> None:
        """Keep a substantial history responsive and leave native painting enabled."""
        root = tk.Tk()
        root.withdraw()
        try:
            desktop = build_fixture(root, 'long')
            view = desktop.views['session']
            for index in range(24):
                turn = f'fixture-{index}'
                view.pending_message = 'Explain this implementation. ' * 10
                for sequence, kind, extra in [(1, 'turn_started', {}),
                                               (2, 'assistant_text', {'text': '## Response\n' + 'A readable implementation explanation. ' * 60 + '\n```python\n' + 'print("fixture")\n' * 20 + '```\n'}),
                                               (3, 'completion', {'outcome': 'success'})]:
                    view.accept({'conversation_id': 'session', 'turn_id': turn,
                                 'sequence': sequence, 'type': kind, **extra})
            desktop._render_conversation()
            root.geometry('1120x740+80+50')
            root.deiconify()
            root.update()
            timings: dict[str, list[float]] = {"resize": [], "scroll": [], "combined": []}
            failures = []

            def step(index: int = 0) -> None:
                """Measure real geometry, scrolling, text reflow, and final paint together."""
                try:
                    started = time.perf_counter()
                    phase = ('resize', 'scroll', 'combined')[index // 32]
                    if phase != 'scroll':
                        root.geometry(f'{1000 + index % 20 * 8}x740')
                    if phase != 'resize':
                        desktop.transcript.yview_moveto((index % 20) / 20)
                    root.update_idletasks()
                    desktop._layout_changed()
                    root.update_idletasks()
                    timings[phase].append((time.perf_counter() - started) * 1000)
                    self.assertTrue(root.winfo_viewable())
                    if index < 95:
                        root.after(16, lambda: step(index + 1))
                    else:
                        root.quit()
                except Exception as error:
                    failures.append(error)
                    root.quit()

            root.after(300, step)
            root.mainloop()
            if failures:
                raise failures[0]
            for phase, values in timings.items():
                self.assertEqual(len(values), 32)
                measured = values[3:]
                percentile = sorted(measured)[int(len(measured) * 0.95)]
                print(f'\n{phase}: mean={sum(measured)/len(measured):.1f} ms, p95={percentile:.1f} ms, max={max(measured):.1f} ms')
                # Guard against the reproduced 700-ms stalls, allowing machine timing variation.
                budget = 100
                self.assertLess(percentile, budget, f'{phase} exceeds its fixture responsiveness budget')
            desktop.window_frame.maximize()
            root.update()
            self.assertEqual(root.state(), 'zoomed')
            desktop.window_frame.maximize()
            root.update()
            self.assertEqual(root.state(), 'normal')
            self.assertNotIn(desktop.transcript.winfo_id(), desktop.window_frame.child_procedures)
            self.assertLess(desktop.model_selector.winfo_reqheight(), 50 * desktop.theme.scale)
            desktop.model_selector._toggle()
            root.update_idletasks()
            self.assertIsNotNone(desktop.model_selector.menu)
            desktop.model_selector.menu.close()
            self.assertEqual(desktop.theme.style.lookup(desktop.sidebar_rail['style'], 'background'),
                             desktop.theme.style.lookup('Titlebar.TFrame', 'background'))
        finally:
            if 'desktop' in locals():
                desktop._closing = True
            for token in root.tk.call('after', 'info'):
                root.after_cancel(token)
            root.destroy()
