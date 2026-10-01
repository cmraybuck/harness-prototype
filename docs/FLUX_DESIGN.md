# FLUX desktop design system

This implementation follows the approved GUI handoff and issue #54, including its
expanded session-management scope. Only charcoal mode is included.

## Toolkit decision

Retain Tk/ttk, the native launcher, Python service, and SQLite persistence. Existing
Pillow supplies antialiased nine-slice rounded component images; no dependency is
added. ttk preserves keyboard navigation, selection, disabled states, and native
desktop behavior without another runtime. Selectable Text widgets render Markdown;
HTML and external image resources remain inert. Directory selection and some
dialog framing remain a maintainability tradeoff. Selectors use rounded in-app menus.

## Tokens and components

`desktop_design.py` owns portable semantic colors, typography, spacing, corner
radii, elevation levels, motion, icons, and shared rounded components.

| Token | Value / use |
| --- | --- |
| Canvas / sidebar | `#202427` / `#262b2f` |
| Titlebar | `#303b43`, distinct blue-charcoal chrome |
| Surface / raised | `#2d3338` / `#353e44` |
| Hover / selected | `#414e55` / `#344e54` |
| Text / muted | `#edf1f3` / `#b4c0c6` |
| Accent | `#91d4d5`, cyan focus, links, send, identity |
| Success / warning / failure | `#a2d4b1` / `#e5c18d` / `#f4b1b5` |
| Spacing | 4, 8, 12, 16, 24, 32 logical units |
| Radius | 8 control, 14 container, 16 bubble |
| Typography | Segoe UI with system fallback; Consolas/TkFixedFont for code |
| Text sizes | Small 10 pt, Default 11 pt, Large 13 pt |
| Elevation | Canvas → surface → raised; restrained borders |
| Motion | Immediate hover/focus changes; 180 ms sidebar ease-out, 16 ms frame scheduling |

The geometric current mark uses three interlocking cyan and green facets around
an open center, without a letterform. Its portable source is
`src/harness_prototype/assets/flux-mark.svg`; Canvas uses the same paths.

| State | Treatment and behavior |
| --- | --- |
| Default | Soft surface, readable text, comfortable padding |
| Hover | Slightly lighter surface and restrained edge |
| Focus | Visible cyan outline; keyboard confirmation |
| Selected / pressed | Muted teal surface; readable text |
| Disabled | Quiet surface, legible muted text; invocation blocked |
| Loading | Subdued Working text; a stop square replaces the cyan send arrow |
| Success | Restrained green token; uncertain outcomes never labelled successful |
| Warning | Muted warm token with actionable explanation |
| Failure | Muted rose token; affected-turn guidance and safe retry |

Ordinary text targets 4.5:1, large text 3:1, and meaningful controls/focus 3:1.
`contrast_ratio` uses WCAG sRGB relative luminance. Automated coverage checks
semantic text colors against all documented surfaces. Selectable text remains
available with keyboard focus and copying. Hidden payloads never enter labels.

## Platform boundary

`desktop_platform.py` subclasses the Windows native window procedure for client
frame calculation and non-client hit testing, retaining the thick frame and default
OS processing. Caption hits preserve dragging, double-click maximize, system menu,
and snapping; edge hits preserve resizing. The maximize hit target preserves snap
layouts. Pointer-width declarations and retained callbacks support native dispatch;
the custom maximize target queues a native maximize/restore system command on an
inside-target button release. Releasing outside it or losing capture cancels the press.
Destruction restores the original procedure. Native child hit tests pass caption and
edge targets through to the top-level frame. Callbacks use cached geometry and never
reenter Tcl. Child procedures restore before destruction, and callbacks remain alive
until native dispatch returns. Assets follow Tk scaling.

`/v1/presentation` persists normal geometry/maximized state, sidebar width/collapse,
text size, and reduced motion. Windows client-area-animation settings supply the
initial motion preference; other platforms default to minimal motion. Restore uses
the nearest available monitor work area. Shared design components contain no Windows
calls. Other platforms retain native chrome; macOS visual QA remains future work.

Validated HTTP(S) links use the default browser. An explicit file-location action
reveals an existing absolute local path using Explorer selection. Missing paths,
credential-bearing links, control characters, and executable schemes are rejected.
The action does not open or execute the file.

## Session and safety contracts

The sidebar presents distinct rounded conversation cards with wrapped titles,
selection/focus states, pin indicators, and a per-card three-dot menu. Row actions
capture the row's identity rather than using whichever chat happens to be open.
Up/Down navigates cards, Enter/Space opens a focused card, and Tab reaches pin/menu.
Each selected chat has an arrowless, rounded vertical scrollbar with a subdued rail,
contrasting thumb, and hover/focus states. Empty/full-fit chats show its disabled
state. Dragging, rail paging, wheel input, and keyboard scrolling use the same
conversation viewport and its saved per-session position.
The titlebar uses a distinct blue-charcoal surface, including label backgrounds
and the rounded corners of its controls. Search uses a compact rounded field with
an inline magnifier and placeholder; the placeholder is never part of a query.
The model selector shares Workspace/Skills' rounded button surface, padding, and
hover/focus states. Its inline dropdown has rounded options and a teal selected row.
Session actions use the same inline menu; rename entry and delete confirmation
appear as temporary panels inside the chat area. Permission and other decision
views use the same in-app host, preserving the conversation and composer beneath
it. Escape and outside clicks dismiss the menu.
Body, headings, and controls share Segoe UI; weight and size establish hierarchy,
with monospace reserved for code. Assistant replies carry a small geometric mark
instead of a repeated brand heading. The composer shows an empty-state placeholder
without adding it to saved drafts. A persistent left rail holds the sidebar toggle
even when the navigation panel is closed.
The rail and exposed Windows resize edges share the titlebar color. Dropdown
indicators are muted rounded chevron assets at the trailing edge, independent of
font glyph availability. Search uses a drawn magnifier instead of an emoji.
The send arrow is enlarged within tighter padding. The Side Panel hint refreshes
in place after toggling. Native non-client maximize hover is cached by Windows
callbacks and applied as the shared ttk active state on the UI thread.

Ctrl+wheel, Ctrl+plus/equals, and Ctrl+minus scale the entire interface across
75%, 90%, 100%, 110%, 125%, and 150%; Ctrl+0 resets to 100%. Zoom uses cached
owned themes and original widget spacing, so repeated changes do not accumulate
rounding or grow assets without bound. Fonts, icons, corners, and controls scale
together; existing conversation widgets and drafts remain intact. A leading
shortcut bind tag prevents Ctrl-wheel from also scrolling. Newly opened controls
and decision views inherit the routing and scale. `zoom_percent` is validated
and persisted through the presentation API; invalid types and levels are rejected.
The send arrow uses
a cyan surface and a descriptive hover/focus hint, changing to a stop square while busy.

Rendering retains the unchanged prefix of a conversation, rebuilding only its
changed tail. UI callback batches apply ordered events before one repaint. Embedded
bubbles route wheel input to the chat; expanded text scrolls internally while it can,
then passes boundary scrolling to the chat. Completed selectable widgets remain alive
during streaming. Changed tails release their widgets and link tags before replacement.
Viewport width is excluded from content signatures: resize events adjust existing
embedded text widths and wrapped heights in place at a coalesced 16 ms cadence.
Cached dimensions avoid repeated Tcl reads when widths are unchanged. Caption
geometry ignores unrelated widget events and reads each button's geometry once.
These reductions address application layout work, not development hosting.
Fixture timing and its limits are recorded in the visual review document.
Rounded assets use larger paint tiles with explicit compact requested dimensions.
Windows disables Tk's redundant full-client redraw class flags, restoring them on
destruction. Only the root and titlebar need native hit forwarding; an exposed
scaled edge preserves resizing without hooking every content widget.
Offscreen text defers width changes until it enters view. Automatic sidebar changes
during window resizing apply immediately; explicit toggle actions still animate.
Automatic collapse at 1040 logical pixels reopens at 1160, avoiding repeated
sidebar changes and chat rewrapping near the threshold. Explicit reopening resets
that automatic collapsed state.
Sidebar transitions slide a clipped fixed-width content panel for 180 ms with
cubic ease-out; reduced-motion settings skip the transition. Repeated toggles cancel
the pending frame and reverse from the current sash position. User resizing still
updates the remembered sidebar width.

Schema six adds pin/archive flags, manual-title provenance, message activity,
separate drafts, and searchable turn display text. Migration preserves existing
history. The first prompt supplies a deterministic 70-character title. Manual rename
disables automatic replacement. Preference changes do not reorder message activity.
Search includes archives, excludes drafts/tool payloads, limits queries to 200
characters, and returns short snippets in pages of at most 100 sessions. Activity
changes between pages may require refreshing navigation.

Archive and deletion check idleness under the turn-reservation lock. Archived
sessions cannot start turns. Deletion cascades through that session's history and
removes its draft and selection provenance. Other sessions, workspace files, logs,
workspace defaults, and independent grants remain intact. Drafts are separate from
submitted history, bounded at 16,000 characters, and saved with serialized debounced
writes. Failed exit persistence keeps the window available for recovery.

Working retains the established safe projection and user-chosen expansion. Code
Copy and Show more use permitted assistant text, capped at 12,000 characters per
displayed block; expansion cannot reveal hidden fields. The composer grows to eight
lines, permits drafts during work, and never queues messages. Model/workspace
changes show context-reset consequences before calling the existing backend.

Permissions are serialized without extending original backend deadlines. Dialogs
show owning session, operation, risk, workspace, scope, and advertised lifetimes.
Allow once starts selected; Up/Down moves and Space/Enter confirms. Escape and
dismissal deny. Receipts, completion, cancellation, disconnect, and expiry remove
stale views. Other sessions get in-app notices and badges without navigation changes. Permission
Choices and confirmation stay visible outside the scrollable review body, including at high scaling.
Closing active work offers Keep working or Stop work and exit; the latter denies
permissions and requests cooperative cancellation.

## Repeatable visual review

`tests/desktop_fixtures.py` uses production components with inert fixture data.
It starts no service/model, reads no personal database, and grants no real access.
Manual GUI checks require explicit authorization. After approval:

```powershell
uv run python tests/desktop_fixtures.py --state empty --size 1120x740 --scale 1
uv run python tests/desktop_fixtures.py --state permission --size 1280x800 --scale 1.25
```

Review `empty`, `active`, `working`, `permission`, `settings`, `skills`, `error`,
`long`, and `sessions`. Include 900 × 600, 1120 × 740, 1366 × 768, and maximized
layouts. Check Windows 100%, 125%, 150%, and 200% scaling; `--scale` exercises Tk
scaling but does not replace changing OS display scaling.

Record size/scaling, screenshot, focus order, clipping, readability, and findings.
Check native dragging, edges/corners, maximize, snap layouts, minimize/restore,
keyboard system menu, sidebar resizing, selectable bubbles, composer growth,
scroll-up preservation, Jump to latest, code Copy/Show more, keyboard links,
Working expansion, permission dismissal, settings/grants, Skills, archive labels,
search snippets, and failure guidance. Real desktop checks are required for OS
chrome and file-picker behavior.

Automated checks cover mocked API operations, session persistence/search/lifecycle,
preference validation, contrast, parsing, link policy, draft navigation, Working
expansion, stream recovery, and permission identities. Record visual and live-model
verification separately. Fixtures alone do not satisfy reviewed-visual acceptance.

Implementation references: [ttk image elements](https://www.tcl-lang.org/man/tcl9.0/TkCmd/ttk_image.html)
and [Microsoft custom window frames](https://learn.microsoft.com/en-us/windows/win32/dwm/customframe).

The implementation's screenshots, checks actually performed, and remaining
verification limits are recorded in [FLUX_VISUAL_REVIEW.md](FLUX_VISUAL_REVIEW.md).
