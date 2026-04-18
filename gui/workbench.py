"""
workbench.py — pdf-converter Workbench: the primary GUI for the full pipeline.

Launch
------
    python gui/workbench.py [PDF_FILE]
    python gui/workbench.py          # opens file chooser on launch

Layout
------
  [📂 Open PDF]  my_document.pdf  →  data/my_document/
  ┌──────────────────┬──────────────────────────┬───────────────────────────┐
  │ Pipeline steps   │  Page 3 / 42  [◀]  [▶]  │  [OCR (this page)]        │
  │                  │                          │  [Markdown]  [💾 Save]    │
  │ ● 1. Render   ▶  │  continuous page scroll  │                           │
  │ ● 2. Analyze  ▶  │  with bounding-box       │  raw / editable Markdown  │
  │   Edit boxes →   │  overlays; Ctrl+scroll   │  with syntax highlighting │
  │   …              │  or pinch to zoom        │                           │
  │ ○ 7. Export   ▶  │                          │                           │
  ├──────────────────┴──────────────────────────┴───────────────────────────┤
  │  Log:  $ python cli.py render …   ✓ Done (exit 0)                       │
  └─────────────────────────────────────────────────────────────────────────┘

Step buttons
------------
- CLI steps: status dot (● green = output exists, ● grey = pending,
  ▶ orange = running), label, optional engine selector, ▶ Run button.
  Clicking Run when output already exists asks whether to overwrite (--force).
- GUI steps: a single "Label →" button that opens the tool as a separate
  process (bbox_editor, ocr_editor, alt_text_editor, table_editor).

Markdown panel
--------------
The Markdown tab is editable once output/document.md exists.  A "💾 Save"
button writes changes back to disk.  Running the Assemble step while there
are unsaved edits warns the user first.  Font size and dark/light mode can
be toggled with the buttons above the tab.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: add pdf-converter root to sys.path so we can import config
# ---------------------------------------------------------------------------
PIPELINE_ROOT = Path(__file__).resolve().parent.parent
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

try:
    import config as _cfg
    _DATA_ROOT = (PIPELINE_ROOT / _cfg.DATA_ROOT).resolve()
except Exception:
    _DATA_ROOT = PIPELINE_ROOT / "data"

from PyQt6.QtCore import QEvent, QFileSystemWatcher, QProcess, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor, QFont, QPainter, QPalette, QPen, QPixmap,
    QSyntaxHighlighter, QTextCharFormat,
)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog,
    QGraphicsScene, QGraphicsView,
    QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QStatusBar, QToolBar, QVBoxLayout, QWidget,
)

_SPINNER_FRAMES = ("|", "/", "—", "\\")


# ---------------------------------------------------------------------------
# Pipeline step definitions
# ---------------------------------------------------------------------------
# kind="cli"  → runs: python cli.py {arg} {pdf_path} [+ extras]
# kind="gui"  → opens: python {arg} {proj_dir}   as a separate process
# output      → path relative to project dir checked for completion (None = no dot)
# optional    → True  = step label shown in grey
# engine      → True  = engine selector (surya / tesseract) shown next to Run button
#               (only applicable to the ocr step; analyze always uses surya)

_STEPS = [
    dict(key="render",      label="1. Render pages",       kind="cli", arg="render",
         output="pages.json"),
    dict(key="deskew",      label="1b. Deskew (optional)",        kind="cli", arg="deskew",
         output="deskew.json", optional=True),
    dict(key="analyze",     label="2. Detect layout",             kind="cli", arg="analyze",
         output="boxes.json"),
    dict(key="edit_boxes",  label="  Edit boxes",                 kind="gui", arg="gui/bbox_editor.py"),
    dict(key="extract",     label="3. Extract figures",           kind="cli", arg="extract",
         output="figures.json"),
    dict(key="compress",    label="3b. Compress figures (opt.)",  kind="cli", arg="compress-figures",
         output="compress.json", optional=True),
    dict(key="alt_text",    label="4. Get alt-text",              kind="cli", arg="get-alt-text",
         optional=True),
    dict(key="edit_alt",    label="  Edit alt-text",       kind="gui", arg="gui/alt_text_editor.py"),
    dict(key="ocr",         label="5. Run OCR",            kind="cli", arg="ocr",
         output="ocr_raw.json", engine=True),
    dict(key="edit_ocr",    label="  Edit OCR",            kind="gui", arg="gui/ocr_editor.py"),
    dict(key="edit_tables", label="  Edit tables",         kind="gui", arg="gui/table_editor.py"),
    dict(key="assemble",    label="6. Assemble Markdown",  kind="cli", arg="assemble",
         output="output/document.md"),
    dict(key="export",      label="7. Export",             kind="cli", arg="export",
         output="output/document.html"),
]

# Box overlay colours — match bbox_editor.py
_BOX_COLOURS: dict[str, QColor] = {
    "figures":           QColor(220, 50,  50,  100),
    "tables":            QColor(30,  180, 180, 100),
    "exclusions":        QColor(50,  100, 220, 100),
    "global_exclusions": QColor(30,  160, 255, 100),
    "captions":          QColor(160, 50,  220, 100),
    "notes":             QColor(220, 140, 50,  100),
    "headings":          QColor(50,  200, 120, 100),
}


# ---------------------------------------------------------------------------
# Markdown syntax highlighter
# ---------------------------------------------------------------------------

class MarkdownHighlighter(QSyntaxHighlighter):
    """Markdown syntax highlighter with separate dark and light colour schemes.

    Call ``set_dark_mode(bool)`` to switch; the document is re-highlighted
    immediately.
    """

    # Each entry: (pattern_str, colour_dark, colour_light, bold, italic, mono)
    _RULES_SPEC = [
        # Headings  #, ##, ###
        (r'^#{1,6} .*',
         "#6ab0f5", "#1558b0",   True,  False, False),
        # Bold  **text** or __text__
        (r'\*\*[^*\n]+\*\*',
         "#e8e8e8", "#111111",   True,  False, False),
        (r'__[^_\n]+__',
         "#e8e8e8", "#111111",   True,  False, False),
        # Italic  *text* or _text_
        (r'(?<!\*)\*(?!\*)[^*\n]+(?<!\*)\*(?!\*)',
         "#cccccc", "#444444",   False, True,  False),
        (r'(?<!_)_(?!_)[^_\n]+(?<!_)_(?!_)',
         "#cccccc", "#444444",   False, True,  False),
        # Inline code  `code`
        (r'`[^`\n]+`',
         "#e08050", "#a64200",   False, False, True),
        # Code fence  ```
        (r'^```.*',
         "#888888", "#666666",   False, False, True),
        # Footnote definitions  [^N]: …
        (r'^\[\^[^\]\n]+\]:',
         "#f5c842", "#7a5000",   True,  False, False),
        # Footnote references  [^N]
        (r'\[\^[^\]\n]+\]',
         "#f5c842", "#7a5000",   False, False, False),
        # HTML tags  <figure>, <img …>, <figcaption>, …
        (r'<[^>\n]+>',
         "#4ecf5a", "#1a7a30",   False, False, False),
        # Horizontal rule  ---
        (r'^-{3,}$',
         "#606060", "#aaaaaa",   False, False, False),
        # Image / link  ![…](…)  [text](url)
        (r'!?\[[^\]\n]*\]\([^\)\n]*\)',
         "#88aacc", "#1a5080",   False, False, False),
    ]

    def __init__(self, document, dark: bool = True):
        super().__init__(document)
        self._dark = dark
        self._rules: list[tuple[re.Pattern, QTextCharFormat]] = []
        self._build_rules()

    def set_dark_mode(self, dark: bool) -> None:
        if self._dark != dark:
            self._dark = dark
            self._build_rules()
            self.rehighlight()

    def _build_rules(self) -> None:
        self._rules = []
        for spec in self._RULES_SPEC:
            pattern, col_dark, col_light, bold, italic, mono = spec
            colour = col_dark if self._dark else col_light
            f = QTextCharFormat()
            f.setForeground(QColor(colour))
            if bold:
                f.setFontWeight(QFont.Weight.Bold)
            if italic:
                f.setFontItalic(True)
            if mono:
                f.setFontFamilies(["Courier New", "Menlo", "Monospace"])
            self._rules.append((re.compile(pattern), f))

    def highlightBlock(self, text: str) -> None:
        for pattern, fmt in self._rules:
            for m in pattern.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), fmt)


# ---------------------------------------------------------------------------
# Page viewer — continuous-scroll, zoomable, with box overlays
# ---------------------------------------------------------------------------

class MultiPageViewer(QGraphicsView):
    """Displays all pages stacked vertically in a scrollable, zoomable view.

    Box overlays (figures, tables, exclusions, …) are painted directly onto
    each page's pixmap.  The ◀/▶ navigation buttons scroll the view to the
    previous/next page rather than replacing it.

    ``currentPageChanged(page_number)`` is emitted whenever the page nearest
    the centre of the viewport changes, so the OCR panel can stay in sync.
    """

    PAGE_GAP = 20           # scene pixels between pages
    MAX_DISPLAY_WIDTH = 1400  # scale down pages wider than this to save memory

    currentPageChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setBackgroundBrush(QColor(55, 55, 55))
        self.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        # (page_number, y_top, y_bottom) for hit-testing on scroll
        self._page_bands: list[tuple[int, float, float]] = []
        self._current_page: int | None = None

        self.verticalScrollBar().valueChanged.connect(self._update_current_page)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show_pages(
        self,
        records: list[dict],
        proj_dir: Path | None,
        boxes: dict,
    ) -> None:
        """Rebuild the scene from *records*.

        Preserves the current zoom and scroll position when the scene already
        had content (e.g. a boxes.json refresh), so the view doesn't jump.
        """
        preserve = bool(self._page_bands)
        old_transform = self.transform()
        old_scroll = self.verticalScrollBar().value()

        self._scene.clear()
        self._page_bands.clear()

        if not records or proj_dir is None:
            return

        global_excl = boxes.get("global_exclusions") or []
        y = 0.0
        max_w = 1.0

        for rec in records:
            page_num = rec["page_number"]
            img_path = proj_dir / rec["image_path"]
            if not img_path.exists():
                continue

            pix = QPixmap(str(img_path))
            if pix.isNull():
                continue
            if pix.width() > self.MAX_DISPLAY_WIDTH:
                pix = pix.scaledToWidth(
                    self.MAX_DISPLAY_WIDTH,
                    Qt.TransformationMode.SmoothTransformation,
                )

            page_boxes = boxes.get("pages", {}).get(str(page_num))
            annotated = self._annotate(pix, page_boxes, global_excl)

            item = self._scene.addPixmap(annotated)
            item.setPos(0, y)

            h = float(annotated.height())
            self._page_bands.append((page_num, y, y + h))
            max_w = max(max_w, float(annotated.width()))
            y += h + self.PAGE_GAP

        self._scene.setSceneRect(QRectF(0, 0, max_w, y))

        if preserve:
            self.setTransform(old_transform)
            self.verticalScrollBar().setValue(old_scroll)
        else:
            # Initial load: scale so page width fills the viewport width.
            self.resetTransform()
            vp_w = self.viewport().width()
            if max_w > 0 and vp_w > 0:
                s = (vp_w - 4) / max_w
                self.scale(s, s)
            self.verticalScrollBar().setValue(0)

        self._update_current_page()

    def scroll_to_page(self, page_number: int) -> None:
        """Scroll so that the top of *page_number* is near the top of the view."""
        for pn, y_top, _ in self._page_bands:
            if pn == page_number:
                self.ensureVisible(QRectF(0, y_top, 10, 10), 20, 20)
                return

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _annotate(
        pixmap: QPixmap,
        page_boxes: dict | None,
        global_excl: list,
    ) -> QPixmap:
        if not page_boxes and not global_excl:
            return pixmap
        annotated = QPixmap(pixmap)
        painter = QPainter(annotated)
        W, H = pixmap.width(), pixmap.height()
        merged = dict(page_boxes or {})
        if global_excl:
            merged["global_exclusions"] = global_excl
        pen_w = max(1, W // 700)
        for category, colour in _BOX_COLOURS.items():
            for box in merged.get(category, []):
                x = int(box.get("x", 0) * W)
                y = int(box.get("y", 0) * H)
                w = int(box.get("w", 0) * W)
                h = int(box.get("h", 0) * H)
                painter.fillRect(x, y, w, h, colour)
                painter.setPen(QPen(colour.darker(160), pen_w))
                painter.drawRect(x, y, w, h)
        painter.end()
        return annotated

    def _update_current_page(self) -> None:
        if not self._page_bands:
            return
        centre_y = self.mapToScene(self.viewport().rect().center()).y()
        # Find the band that contains the viewport centre
        for pn, y_top, y_bot in self._page_bands:
            if y_top <= centre_y <= y_bot:
                if pn != self._current_page:
                    self._current_page = pn
                    self.currentPageChanged.emit(pn)
                return
        # Between pages — pick nearest by midpoint
        pn = min(self._page_bands, key=lambda b: abs((b[1] + b[2]) / 2 - centre_y))[0]
        if pn != self._current_page:
            self._current_page = pn
            self.currentPageChanged.emit(pn)

    # ------------------------------------------------------------------
    # Zoom
    # ------------------------------------------------------------------

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            f = pow(1.0015, event.angleDelta().y())
            self.scale(f, f)
            event.accept()
        else:
            super().wheelEvent(event)

    def event(self, event):
        if event.type() == QEvent.Type.NativeGesture:
            if event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                factor = 1.0 + event.value()
                if factor > 0:
                    self.scale(factor, factor)
            return True
        return super().event(event)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class WorkbenchWindow(QMainWindow):
    def __init__(self, pdf_path: Path | None = None):
        super().__init__()
        self._pdf_path: Path | None = None
        self._proj_dir: Path | None = None
        self._process: QProcess | None = None
        self._current_page_idx: int = 0
        self._page_records: list[dict] = []
        self._boxes: dict = {}
        self._md_dirty: bool = False
        self._md_font_size: int = 10
        self._md_dark_mode: bool = True
        self._running_step_key: str | None = None
        self._pending_export_extra: list | None = None  # set when compress runs before export

        # Spinner state (in-log progress indicator)
        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(120)
        self._spin_timer.timeout.connect(self._on_spin_tick)
        self._spin_frame: int = 0
        self._spin_label: str = ""
        self._spin_active: bool = False
        self._spin_pos: int = 0       # char position in log where spinner line begins

        # Widget registries (populated by _build_step_row)
        self._status_labels: dict[str, QLabel] = {}
        self._run_btns: dict[str, QPushButton] = {}
        self._engine_combos: dict[str, QComboBox] = {}
        self._all_run_btns: list[QPushButton] = []

        # File watcher — detects saves from sub-GUIs
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_watched_file_changed)

        self._build_ui()

        if pdf_path and pdf_path.exists():
            self._load_pdf(pdf_path)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.setWindowTitle("pdf-converter Workbench")
        self.resize(1440, 840)

        # Toolbar
        toolbar = QToolBar()
        self.addToolBar(toolbar)
        open_btn = QPushButton("📂 Open PDF")
        open_btn.clicked.connect(self._prompt_open_pdf)
        self._pdf_label = QLabel("  No PDF selected")
        self._pdf_label.setStyleSheet("color: #888;")
        toolbar.addWidget(open_btn)
        toolbar.addWidget(self._pdf_label)

        # Outer vertical splitter: top (3 columns) | bottom (log)
        outer = QSplitter(Qt.Orientation.Vertical)
        self.setCentralWidget(outer)

        # Top: 3-column horizontal splitter
        main_split = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(main_split)

        # ── Left: scrollable pipeline steps ──────────────────────────
        left_outer = QWidget()
        left_outer.setMinimumWidth(200)
        lo_layout = QVBoxLayout(left_outer)
        lo_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        steps_w = QWidget()
        steps_w.setMinimumWidth(0)
        steps_layout = QVBoxLayout(steps_w)
        steps_layout.setContentsMargins(8, 8, 8, 8)
        steps_layout.setSpacing(2)

        # Export option widgets (created here so the export row can reference them)
        self._export_html_cb = QCheckBox("HTML")
        self._export_html_cb.setChecked(True)
        self._export_epub_cb = QCheckBox("EPUB")
        self._self_contained_cb = QCheckBox("self-contained")
        self._self_contained_cb.setChecked(True)
        self._compress_figs_cb = QCheckBox("compress figures")

        for step in _STEPS:
            self._build_step_row(steps_layout, step)

        steps_layout.addStretch()
        scroll.setWidget(steps_w)
        lo_layout.addWidget(scroll)
        main_split.addWidget(left_outer)

        # ── Center: page viewer ───────────────────────────────────────
        center = QWidget()
        c_layout = QVBoxLayout(center)
        c_layout.setContentsMargins(4, 4, 4, 4)
        c_layout.setSpacing(4)

        nav = QHBoxLayout()
        self._prev_page_btn = QPushButton("◀")
        self._prev_page_btn.setFixedWidth(30)
        self._prev_page_btn.setEnabled(False)
        self._prev_page_btn.clicked.connect(self._prev_page)
        self._page_label = QLabel("  No pages  ")
        self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._next_page_btn = QPushButton("▶")
        self._next_page_btn.setFixedWidth(30)
        self._next_page_btn.setEnabled(False)
        self._next_page_btn.clicked.connect(self._next_page)
        nav.addWidget(self._prev_page_btn)
        nav.addWidget(self._page_label, 1)
        nav.addWidget(self._next_page_btn)
        c_layout.addLayout(nav)

        self._page_viewer = MultiPageViewer()
        self._page_viewer.currentPageChanged.connect(self._on_current_page_changed)
        c_layout.addWidget(self._page_viewer)
        main_split.addWidget(center)

        # ── Right: OCR / Markdown tabs ────────────────────────────────
        right = QWidget()
        r_layout = QVBoxLayout(right)
        r_layout.setContentsMargins(4, 4, 4, 4)
        r_layout.setSpacing(4)

        # Button row above the tab widget: zoom + dark-mode (left) | save (right)
        save_row = QHBoxLayout()
        self._md_zoom_out_btn = QPushButton("A−")
        self._md_zoom_out_btn.setFixedWidth(32)
        self._md_zoom_out_btn.setToolTip("Decrease font size (Ctrl+scroll down)")
        self._md_zoom_out_btn.clicked.connect(lambda: self._zoom_md(-1))
        self._md_zoom_in_btn = QPushButton("A+")
        self._md_zoom_in_btn.setFixedWidth(32)
        self._md_zoom_in_btn.setToolTip("Increase font size (Ctrl+scroll up)")
        self._md_zoom_in_btn.clicked.connect(lambda: self._zoom_md(1))
        self._md_dark_btn = QPushButton("☀")
        self._md_dark_btn.setFixedWidth(28)
        self._md_dark_btn.setToolTip("Switch to light mode")
        self._md_dark_btn.clicked.connect(self._toggle_md_dark_mode)
        save_row.addWidget(self._md_zoom_out_btn)
        save_row.addWidget(self._md_zoom_in_btn)
        save_row.addWidget(self._md_dark_btn)
        save_row.addStretch()
        self._save_md_btn = QPushButton("💾 Save Markdown")
        self._save_md_btn.setEnabled(False)
        self._save_md_btn.clicked.connect(self._save_markdown)
        save_row.addWidget(self._save_md_btn)
        r_layout.addLayout(save_row)

        mono = QFont("Courier New", 10)

        self._md_view = QPlainTextEdit()
        self._md_view.setFont(mono)
        self._md_view.setPlaceholderText(
            "Assembled Markdown will appear here after step 6 (Assemble Markdown).\n"
            "You can edit it directly and save with the button above."
        )
        self._md_view.textChanged.connect(self._on_md_text_changed)
        self._md_view.installEventFilter(self)
        self._md_highlighter = MarkdownHighlighter(self._md_view.document(), dark=True)
        self._apply_md_dark_mode()   # set initial palette

        r_layout.addWidget(self._md_view)
        main_split.addWidget(right)
        main_split.setSizes([270, 720, 450])

        # ── Bottom: log ───────────────────────────────────────────────
        log_w = QWidget()
        log_layout = QVBoxLayout(log_w)
        log_layout.setContentsMargins(6, 2, 6, 6)
        log_layout.setSpacing(2)
        log_layout.addWidget(QLabel("Log:"))
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(2000)
        self._log_view.setFont(QFont("Courier New", 9))
        log_layout.addWidget(self._log_view)
        outer.addWidget(log_w)
        outer.setSizes([650, 170])

        self.setStatusBar(QStatusBar())

    def _build_step_row(self, layout: QVBoxLayout, step: dict):
        key = step["key"]
        optional = step.get("optional", False)

        row = QWidget()
        row.setMinimumWidth(0)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)

        if step["kind"] == "gui":
            btn = QPushButton(f"{step['label']}  →")
            btn.setStyleSheet("text-align: left; color: #5599bb; padding: 3px 8px;")
            btn.setEnabled(False)
            btn.setMinimumWidth(0)
            btn.clicked.connect(lambda _, s=step: self._open_gui(s["arg"]))
            h.addWidget(btn)
            self._run_btns[key] = btn
            self._all_run_btns.append(btn)
        else:
            dot = QLabel("●")
            dot.setFixedWidth(14)
            dot.setStyleSheet("color: #666; font-size: 13px;")
            dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._status_labels[key] = dot
            h.addWidget(dot)

            lbl = QLabel(step["label"])
            if optional:
                lbl.setStyleSheet("color: #999;")
            h.addWidget(lbl)                 # natural width — no stretch

            if step.get("engine"):
                combo = QComboBox()
                combo.addItems(["surya", "tesseract"])
                combo.setMinimumWidth(60)
                combo.setMaximumWidth(82)
                self._engine_combos[key] = combo
                h.addSpacing(6)              # small gap between label and engine selector
                h.addWidget(combo)

            h.addStretch(1)                  # push run button to the right edge
            run_btn = QPushButton("▶")
            run_btn.setFixedWidth(28)        # always visible at right edge
            run_btn.setEnabled(False)
            run_btn.setToolTip(f"Run: python cli.py {step['arg']} ...")
            run_btn.clicked.connect(lambda _, s=step: self._on_run_clicked(s))
            h.addWidget(run_btn)
            self._run_btns[key] = run_btn
            self._all_run_btns.append(run_btn)

        layout.addWidget(row)

        # Extra export options — checkboxes stacked in a column
        if key == "export":
            opts = QWidget()
            opts_v = QVBoxLayout(opts)
            opts_v.setContentsMargins(18, 0, 0, 4)
            opts_v.setSpacing(8)
            opts_v.addWidget(self._export_html_cb)
            opts_v.addWidget(self._export_epub_cb)
            opts_v.addWidget(self._self_contained_cb)
            opts_v.addWidget(self._compress_figs_cb)
            layout.addWidget(opts)

    # ------------------------------------------------------------------
    # PDF / project loading
    # ------------------------------------------------------------------

    def _prompt_open_pdf(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open PDF", str(PIPELINE_ROOT), "PDF files (*.pdf)"
        )
        if path:
            self._load_pdf(Path(path))

    def _load_pdf(self, pdf_path: Path):
        self._pdf_path = pdf_path
        self._proj_dir = _DATA_ROOT / pdf_path.stem
        self._proj_dir.mkdir(parents=True, exist_ok=True)
        self._pdf_label.setText(f"  {pdf_path.name}  →  {self._proj_dir}")
        self._pdf_label.setStyleSheet("color: #ddd;")

        self._reload_project_data()
        self._update_watchers()
        self._enable_run_buttons(True)
        self._update_status()
        self._current_page_idx = 0
        self._refresh_center_panel()
        self._refresh_right_panel()

        # Auto-render on first open (pages.json absent means never rendered)
        if not self._page_records:
            self._log(f"New project — automatically starting Render step for {pdf_path.name}…\n")
            self._run_cli_step("render", "render", "pages.json", [])

    def _update_watchers(self):
        """Add existing project files to the file watcher."""
        if self._proj_dir is None:
            return
        candidates = [
            "boxes.json",
            "figures.json",
            "output/document.md",
        ]
        # Remove stale paths first
        existing = self._watcher.files()
        if existing:
            self._watcher.removePaths(existing)
        to_watch = [
            str(self._proj_dir / c)
            for c in candidates
            if (self._proj_dir / c).exists()
        ]
        if to_watch:
            self._watcher.addPaths(to_watch)

    def _on_watched_file_changed(self, path: str):
        """Called when a watched file is modified externally (e.g. by a sub-GUI)."""
        p = Path(path)
        # Some save operations delete + recreate the file; re-add it if it's back.
        if p.exists():
            self._watcher.addPath(path)

        self._reload_project_data()
        self._update_status()

        fname = p.name
        if fname == "boxes.json":
            self._refresh_center_panel()
        elif fname == "document.md":
            self._refresh_md_tab()  # skipped automatically if Markdown panel is dirty

        self.statusBar().showMessage(f"{p.name} updated by external tool — refreshed.", 4000)

    def _reload_project_data(self):
        if self._proj_dir is None:
            return
        p = self._proj_dir / "pages.json"
        self._page_records = json.loads(p.read_text()) if p.exists() else []

        b = self._proj_dir / "boxes.json"
        self._boxes = json.loads(b.read_text()) if b.exists() else {}

    # ------------------------------------------------------------------
    # Status indicators
    # ------------------------------------------------------------------

    def _update_status(self):
        if self._proj_dir is None:
            return
        for step in _STEPS:
            key = step["key"]
            dot = self._status_labels.get(key)
            if dot is None:
                continue
            output = step.get("output")
            done = bool(output and (self._proj_dir / output).exists())
            dot.setText("●")
            dot.setStyleSheet(
                f"color: {'#44bb44' if done else '#666666'}; font-size: 13px;"
            )

    def _enable_run_buttons(self, enabled: bool):
        alive: list = []
        for btn in self._all_run_btns:
            try:
                btn.setEnabled(enabled)
                alive.append(btn)
            except RuntimeError:
                pass  # C++ object already deleted (e.g. window closing)
        self._all_run_btns[:] = alive

    def closeEvent(self, event):
        """Stop the spinner timer before Qt destroys child widgets."""
        self._spin_timer.stop()
        self._spin_active = False
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Running CLI steps
    # ------------------------------------------------------------------

    def _on_run_clicked(self, step: dict):
        # Guard: warn if assemble would overwrite unsaved Markdown edits
        if step["key"] == "assemble" and self._md_dirty:
            reply = QMessageBox.question(
                self, "Unsaved Markdown edits",
                "You have unsaved edits in the Markdown panel.\n"
                "Running Assemble will overwrite output/document.md.\n\n"
                "Save your edits first, or discard them and continue?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )
            if reply == QMessageBox.StandardButton.Save:
                self._save_markdown()
            elif reply == QMessageBox.StandardButton.Cancel:
                return
            # Discard → fall through

        extra: list[str] = []
        if step.get("engine") and step["key"] in self._engine_combos:
            extra += ["--engine", self._engine_combos[step["key"]].currentText()]
        if step["key"] == "export":
            fmts = []
            if self._export_html_cb.isChecked():
                fmts.append("html")
            if self._export_epub_cb.isChecked():
                fmts.append("epub")
            extra += ["--formats", ",".join(fmts) if fmts else "html"]
            if self._self_contained_cb.isChecked():
                extra.append("--self-contained")
            # If "compress figures" is checked, run compress-figures first;
            # _on_finished will fire the export once compression completes.
            if self._compress_figs_cb.isChecked():
                self._pending_export_extra = extra
                self._run_cli_step(
                    "compress", "compress-figures", "compress.json", ["--force"]
                )
                return

        self._run_cli_step(step["key"], step["arg"], step.get("output"), extra)

    def _run_cli_step(self, key: str, cmd: str, output: str | None, extra: list[str]):
        if self._pdf_path is None or self._proj_dir is None:
            return

        force = False
        if output and (self._proj_dir / output).exists():
            reply = QMessageBox.question(
                self, "Output already exists",
                f"'{output}' already exists in the project directory.\n"
                "Re-run this step and overwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            force = True

        args = ["cli.py", cmd, str(self._pdf_path)] + extra
        if force:
            args.append("--force")

        dot = self._status_labels.get(key)
        if dot:
            dot.setText("▶")
            dot.setStyleSheet("color: #ffaa00; font-size: 13px;")

        self._enable_run_buttons(False)
        self._running_step_key = key
        self._log(f"$ python {' '.join(args)}\n")
        if key in ("render", "extract"):
            self._log("(this may take a few minutes)\n")
        spin_label = next(
            (s["label"] for s in _STEPS if s["key"] == key),
            key,
        )
        self._start_spinner(spin_label)

        self._process = QProcess(self)
        self._process.setWorkingDirectory(str(PIPELINE_ROOT))
        self._process.readyReadStandardOutput.connect(self._on_stdout)
        self._process.readyReadStandardError.connect(self._on_stderr)
        self._process.finished.connect(
            lambda code, _status, k=key: self._on_finished(k, code)
        )
        self._process.start(sys.executable, args)

    def _on_stdout(self):
        if not self._process:
            return
        data = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="replace")
        visible_lines = []
        for line in data.splitlines(keepends=True):
            if line.startswith("EXTRACT_PROGRESS "):
                try:
                    current, total = line.split()[1].split("/")
                    base = next(
                        (s["label"] for s in _STEPS if s["key"] == "extract"),
                        "Extracting",
                    )
                    self._spin_label = f"{base} [{current}/{total}]"
                except Exception:
                    pass
            else:
                visible_lines.append(line)
        if visible_lines:
            self._log("".join(visible_lines))

    def _on_stderr(self):
        if self._process:
            self._log(bytes(self._process.readAllStandardError()).decode("utf-8", errors="replace"))

    def _on_finished(self, key: str, exit_code: int):
        self._enable_run_buttons(True)
        self._running_step_key = None
        self._stop_spinner()
        if exit_code == 0:
            self._log("✓ Done (exit 0)\n\n")
            self.statusBar().showMessage(f"Step '{key}' completed successfully.", 4000)
        else:
            self._log(f"✗ Failed (exit {exit_code})\n\n")
            self.statusBar().showMessage(f"Step '{key}' failed — see log for details.", 6000)
        # If compression was triggered by the Export checkbox, now fire export.
        if key == "compress" and self._pending_export_extra is not None:
            extra = self._pending_export_extra
            self._pending_export_extra = None
            if exit_code == 0:
                self._run_cli_step("export", "export", "output/document.html", extra)
                return
            else:
                self._log("Export skipped because compression failed.\n\n")

        self._reload_project_data()
        self._update_watchers()
        self._update_status()
        self._refresh_center_panel()
        self._refresh_right_panel()

    def _log(self, text: str):
        from PyQt6.QtGui import QTextCursor
        if self._spin_active:
            self._erase_spinner()
        self._log_view.moveCursor(QTextCursor.MoveOperation.End)
        self._log_view.insertPlainText(text)
        if self._spin_active:
            self._append_spinner()
        else:
            self._log_view.ensureCursorVisible()

    # ------------------------------------------------------------------
    # Spinner helpers
    # ------------------------------------------------------------------

    def _start_spinner(self, label: str) -> None:
        self._spin_label = label
        self._spin_frame = 0
        self._spin_active = True
        self._append_spinner()
        self._spin_timer.start()

    def _stop_spinner(self) -> None:
        self._spin_timer.stop()
        if self._spin_active:
            self._erase_spinner()
        self._spin_active = False

    def _on_spin_tick(self) -> None:
        self._spin_frame = (self._spin_frame + 1) % len(_SPINNER_FRAMES)
        self._erase_spinner()
        self._append_spinner()

    def _append_spinner(self) -> None:
        from PyQt6.QtGui import QTextCursor
        cursor = self._log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._spin_pos = cursor.position()
        frame = _SPINNER_FRAMES[self._spin_frame]
        self._log_view.insertPlainText(f"{frame} {self._spin_label}…")
        self._log_view.ensureCursorVisible()

    def _erase_spinner(self) -> None:
        from PyQt6.QtGui import QTextCursor
        cursor = self._log_view.textCursor()
        cursor.setPosition(self._spin_pos)
        cursor.movePosition(
            QTextCursor.MoveOperation.End,
            QTextCursor.MoveMode.KeepAnchor,
        )
        cursor.removeSelectedText()

    # ------------------------------------------------------------------
    # Opening sub-GUI tools
    # ------------------------------------------------------------------

    def _open_gui(self, script_rel: str):
        if self._proj_dir is None:
            return
        import subprocess
        script = PIPELINE_ROOT / script_rel
        subprocess.Popen(
            [sys.executable, str(script), str(self._proj_dir)],
            cwd=str(PIPELINE_ROOT),
        )

    # ------------------------------------------------------------------
    # Page navigation (center panel)
    # ------------------------------------------------------------------

    def _on_current_page_changed(self, page_num: int) -> None:
        """Called when the user scrolls to a new page in the continuous viewer."""
        n = len(self._page_records)
        for i, rec in enumerate(self._page_records):
            if rec["page_number"] == page_num:
                self._current_page_idx = i
                break
        self._page_label.setText(f"  Page {page_num} / {n}  ")
        self._prev_page_btn.setEnabled(self._current_page_idx > 0)
        self._next_page_btn.setEnabled(self._current_page_idx < n - 1)

    def _prev_page(self):
        if self._current_page_idx > 0:
            self._current_page_idx -= 1
            page_num = self._page_records[self._current_page_idx]["page_number"]
            self._page_viewer.scroll_to_page(page_num)

    def _next_page(self):
        if self._current_page_idx < len(self._page_records) - 1:
            self._current_page_idx += 1
            page_num = self._page_records[self._current_page_idx]["page_number"]
            self._page_viewer.scroll_to_page(page_num)

    def _refresh_center_panel(self):
        n = len(self._page_records)
        if n == 0:
            self._page_label.setText("  No pages  ")
            self._prev_page_btn.setEnabled(False)
            self._next_page_btn.setEnabled(False)
            self._page_viewer.show_pages([], None, {})
            return

        self._page_viewer.show_pages(self._page_records, self._proj_dir, self._boxes)
        # Nav label/buttons are updated via currentPageChanged once the scene settles

    # ------------------------------------------------------------------
    # Right panel — Markdown
    # ------------------------------------------------------------------

    def _refresh_right_panel(self):
        self._refresh_md_tab()

    def _refresh_md_tab(self):
        if self._proj_dir is None:
            return
        md_path = self._proj_dir / "output" / "document.md"
        if not md_path.exists():
            return
        if self._md_dirty:
            # Don't silently overwrite the user's unsaved edits
            return
        text = md_path.read_text(encoding="utf-8")
        # Block signals so loading doesn't trigger the dirty flag
        self._md_view.blockSignals(True)
        self._md_view.setPlainText(text)
        self._md_view.blockSignals(False)
        self._save_md_btn.setEnabled(True)

    def _on_md_text_changed(self):
        if not self._md_dirty:
            self._md_dirty = True
            self._save_md_btn.setEnabled(True)
            self._save_md_btn.setText("💾 Save Markdown *")

    def _save_markdown(self):
        if self._proj_dir is None:
            return
        md_path = self._proj_dir / "output" / "document.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(self._md_view.toPlainText(), encoding="utf-8")
        self._md_dirty = False
        self._save_md_btn.setText("💾 Save Markdown")
        self.statusBar().showMessage("Markdown saved.", 3000)

    def _zoom_md(self, delta: int):
        self._md_font_size = max(6, min(36, self._md_font_size + delta))
        font = self._md_view.font()
        font.setPointSize(self._md_font_size)
        self._md_view.setFont(font)

    def _toggle_md_dark_mode(self):
        self._md_dark_mode = not self._md_dark_mode
        self._apply_md_dark_mode()

    def _apply_md_dark_mode(self):
        if self._md_dark_mode:
            bg, fg, sel = "#1e1e1e", "#d4d4d4", "#264f78"
            self._md_dark_btn.setText("☀")
            self._md_dark_btn.setToolTip("Switch to light mode")
        else:
            bg, fg, sel = "#ffffff", "#1e1e1e", "#add6ff"
            self._md_dark_btn.setText("🌙")
            self._md_dark_btn.setToolTip("Switch to dark mode")
        palette = self._md_view.palette()
        palette.setColor(QPalette.ColorRole.Base,      QColor(bg))
        palette.setColor(QPalette.ColorRole.Text,      QColor(fg))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(sel))
        self._md_view.setPalette(palette)
        self._md_highlighter.set_dark_mode(self._md_dark_mode)

    def eventFilter(self, obj, event):
        if obj is self._md_view and event.type() == QEvent.Type.Wheel:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self._zoom_md(1 if event.angleDelta().y() > 0 else -1)
                return True
        return super().eventFilter(obj, event)



# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(
        prog="workbench.py",
        description="pdf-converter Workbench — interactive GUI for the full conversion pipeline.",
    )
    parser.add_argument(
        "pdf_file", nargs="?", metavar="PDF_FILE",
        help="Path to the PDF to process (optional; file chooser opens if omitted)",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf_file) if args.pdf_file else None

    app = QApplication(sys.argv)
    app.setApplicationName("pdf-converter")
    window = WorkbenchWindow(pdf_path)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
