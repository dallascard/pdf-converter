"""
alt_text_editor.py — PyQt6 GUI for reviewing and editing figure alt-text.

Launch
------
    python gui/alt_text_editor.py <project_dir>

Layout
------
  ┌──────────────────────────────────────────────────────────────────────┐
  │  Toolbar: [◀ Prev] [Page N / M  (K figures)] [▶ Next]  [Save]       │
  ├─────────────────────┬────────────────────────────────────────────────┤
  │  Figures this page: │  fig_2_1                                       │
  │                     │ ┌──────────────────────────────────────────┐   │
  │  ✓ fig_2_1 [thumb]  │ │  Figure crop (zoomable / pannable)       │   │
  │    fig_2_2 [thumb]  │ │                                          │   │
  │    fig_2_3 [thumb]  │ └──────────────────────────────────────────┘   │
  │                     │  Alt-text:                                      │
  │                     │ ┌──────────────────────────────────────────┐   │
  │                     │ │  A bar chart showing rainfall by …       │   │
  │                     │ └──────────────────────────────────────────┘   │
  └─────────────────────┴────────────────────────────────────────────────┘

Editing
-------
Navigate page by page.  All figures detected on each page appear as
thumbnails in the left panel — click one to load its crop and edit its
alt-text.  A ✓ mark indicates that alt-text has already been written for
that figure.

Changes are flushed to the in-memory record whenever you switch figure or
page.  Saving writes the entire figures.json back to disk.

Keyboard shortcuts
------------------
  Ctrl+S            : save all changes to figures.json
  Ctrl+← / Ctrl+→  : previous / next page
  Ctrl+scroll / pinch : zoom the figure image
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PyQt6.QtCore import QEvent, QRectF, QSize, Qt
from PyQt6.QtGui import (
    QColor, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QSplitter, QStatusBar, QToolBar, QVBoxLayout, QWidget,
    QGraphicsRectItem, QGraphicsScene, QGraphicsView,
)


# ---------------------------------------------------------------------------
# Figure image view — zoomable / pannable
# ---------------------------------------------------------------------------

class FigureImageView(QGraphicsView):
    """Displays a figure crop image; Ctrl+scroll to zoom, scroll to pan."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._zoom = 1.0

    def set_image(self, pixmap: QPixmap | None, highlight: dict | None = None):
        """Display *pixmap*.  If *highlight* is given (``{x, y, w, h}`` as page
        fractions), draw a coloured rectangle over the image to mark that region."""
        self._scene.clear()
        self._zoom = 1.0
        self.resetTransform()
        if pixmap is not None and not pixmap.isNull():
            self._scene.addPixmap(pixmap)
            self._scene.setSceneRect(QRectF(pixmap.rect()))
            if highlight:
                pw, ph = pixmap.width(), pixmap.height()
                rx = highlight.get("x", 0) * pw
                ry = highlight.get("y", 0) * ph
                rw = highlight.get("w", 0) * pw
                rh = highlight.get("h", 0) * ph
                rect_item = QGraphicsRectItem(rx, ry, rw, rh)
                pen = QPen(QColor(220, 50, 50))
                pen.setWidth(max(2, int(pw / 300)))
                rect_item.setPen(pen)
                fill = QColor(220, 50, 50, 40)
                rect_item.setBrush(fill)
                self._scene.addItem(rect_item)
            self.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        else:
            self._scene.setSceneRect(QRectF(0, 0, 400, 300))

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = pow(1.0015, event.angleDelta().y())
            self._zoom *= factor
            self.scale(factor, factor)
            event.accept()
        else:
            super().wheelEvent(event)

    def event(self, event):
        if event.type() == QEvent.Type.NativeGesture:
            if event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                factor = 1.0 + event.value()
                if factor > 0:
                    self._zoom *= factor
                    self.scale(factor, factor)
                return True
        return super().event(event)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class AltTextEditorWindow(QMainWindow):
    def __init__(self, project_dir: Path):
        super().__init__()
        self._proj = project_dir
        self._figures: list[dict] = []   # all figure records (shared references)
        self._pages: list[int] = []      # sorted page numbers that have figures
        self._current_page_idx: int = 0
        self._current_figure: dict | None = None  # reference into self._figures
        self._dirty: bool = False
        self._page_pixmap_cache: dict[int, QPixmap] = {}  # page_number → QPixmap

        self._load_data()
        self._build_ui()

        if self._pages:
            self._load_page(0)
        else:
            self._show_empty()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _load_data(self):
        figures_path = self._proj / "figures.json"
        if not figures_path.exists():
            QMessageBox.critical(
                self, "Error",
                f"figures.json not found in {self._proj}.\n"
                "Run the 'extract' step first to generate figure crops.",
            )
            sys.exit(1)
        self._figures = json.loads(figures_path.read_text())
        seen: set[int] = set()
        for fig in self._figures:
            seen.add(fig.get("page_number", 0))
        self._pages = sorted(seen)

    def _save_data(self):
        self._flush_current()
        path = self._proj / "figures.json"
        path.write_text(json.dumps(self._figures, indent=2))
        self._dirty = False
        self._refresh_list_item(self._fig_list.currentRow())
        self.statusBar().showMessage(f"Saved to {path.name}", 3000)

    def _flush_current(self):
        """Write the editor contents back into the current figure record."""
        if self._current_figure is not None:
            self._current_figure["alt_text"] = self._editor.toPlainText().strip()

    def _figures_for_page(self, page_num: int) -> list[dict]:
        """Return the subset of self._figures for *page_num* (shared references)."""
        return [f for f in self._figures if f.get("page_number") == page_num]

    def _page_pixmap(self, page_num: int) -> QPixmap | None:
        """Load (and cache) the full-page PNG for *page_num*."""
        if page_num in self._page_pixmap_cache:
            return self._page_pixmap_cache[page_num]
        page_path = self._proj / "pages" / f"page_{page_num:04d}.png"
        if not page_path.exists():
            return None
        pm = QPixmap(str(page_path))
        if pm.isNull():
            return None
        self._page_pixmap_cache[page_num] = pm
        return pm

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.setWindowTitle(f"Alt-Text Editor — {self._proj.name}")
        self.resize(1200, 740)

        # --- Toolbar ---
        toolbar = QToolBar()
        self.addToolBar(toolbar)

        self._prev_btn  = QPushButton("◀ Prev")
        self._nav_label = QLabel()
        self._nav_label.setMinimumWidth(260)
        self._nav_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._next_btn  = QPushButton("Next ▶")
        self._save_btn  = QPushButton("💾 Save")

        toolbar.addWidget(self._prev_btn)
        toolbar.addWidget(self._nav_label)
        toolbar.addWidget(self._next_btn)
        toolbar.addSeparator()
        toolbar.addWidget(self._save_btn)

        # --- Outer splitter: page context | figure list | crop + editor ---
        outer = QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(outer)

        # Panel 1: full-page context view
        page_panel = QWidget()
        page_layout = QVBoxLayout(page_panel)
        page_layout.setContentsMargins(6, 6, 6, 6)
        page_layout.setSpacing(4)
        page_layout.addWidget(QLabel("Page context:"))
        self._page_view = FigureImageView()
        page_layout.addWidget(self._page_view)
        outer.addWidget(page_panel)

        # Panel 2: figure thumbnail list
        mid = QWidget()
        mid_layout = QVBoxLayout(mid)
        mid_layout.setContentsMargins(6, 6, 6, 6)
        mid_layout.setSpacing(4)
        mid_layout.addWidget(QLabel("Figures on this page:"))

        self._fig_list = QListWidget()
        self._fig_list.setIconSize(QSize(72, 72))
        self._fig_list.setSpacing(2)
        self._fig_list.currentRowChanged.connect(self._on_figure_selected)
        mid_layout.addWidget(self._fig_list)

        mid.setMinimumWidth(130)
        mid.setMaximumWidth(210)
        outer.addWidget(mid)

        # Panel 3: figure ID label / crop image / alt-text editor
        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(6, 6, 6, 6)
        detail_layout.setSpacing(6)

        self._fig_id_label = QLabel()
        self._fig_id_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        detail_layout.addWidget(self._fig_id_label)

        # Vertical splitter: crop image (top) | editor (bottom)
        edit_split = QSplitter(Qt.Orientation.Vertical)

        self._image_view = FigureImageView()
        edit_split.addWidget(self._image_view)

        editor_container = QWidget()
        ec_layout = QVBoxLayout(editor_container)
        ec_layout.setContentsMargins(0, 0, 0, 0)
        ec_layout.setSpacing(4)
        ec_layout.addWidget(QLabel("Alt-text:"))
        self._editor = QPlainTextEdit()
        self._editor.setPlaceholderText(
            "Describe this figure for screen readers and accessibility.\n\n"
            "A good alt-text explains what the image shows: the type of chart "
            "or map, what data it displays, and any key values or patterns visible."
        )
        self._editor.textChanged.connect(self._on_text_changed)
        ec_layout.addWidget(self._editor)
        edit_split.addWidget(editor_container)
        edit_split.setSizes([480, 160])

        detail_layout.addWidget(edit_split)
        outer.addWidget(detail_panel)
        outer.setSizes([480, 180, 540])

        self.setStatusBar(QStatusBar())

        # Shortcuts
        QShortcut(QKeySequence("Ctrl+S"), self, self._save_data)
        QShortcut(QKeySequence("Ctrl+Left"),  self, self._prev_page)
        QShortcut(QKeySequence("Ctrl+Right"), self, self._next_page)

        self._prev_btn.clicked.connect(self._prev_page)
        self._next_btn.clicked.connect(self._next_page)
        self._save_btn.clicked.connect(self._save_data)

    # ------------------------------------------------------------------
    # Page navigation
    # ------------------------------------------------------------------

    def _load_page(self, page_idx: int):
        self._flush_current()
        self._current_page_idx = page_idx
        self._current_figure = None

        page_num = self._pages[page_idx]
        figs = self._figures_for_page(page_num)

        n_done = sum(1 for f in figs if f.get("alt_text", "").strip())
        self._nav_label.setText(
            f"  Page {page_num}"
            f"  —  {len(figs)} figure{'s' if len(figs) != 1 else ''}"
            f"  ({n_done} / {len(figs)} with alt-text)"
            f"  [{page_idx + 1} / {len(self._pages)} pages]  "
        )
        self._prev_btn.setEnabled(page_idx > 0)
        self._next_btn.setEnabled(page_idx < len(self._pages) - 1)

        self._fig_list.blockSignals(True)
        self._fig_list.clear()
        for fig in figs:
            self._fig_list.addItem(self._make_list_item(fig))
        self._fig_list.blockSignals(False)

        if figs:
            self._fig_list.setCurrentRow(0)
        else:
            self._image_view.set_image(None)
            self._page_view.set_image(None)
            self._editor.blockSignals(True)
            self._editor.clear()
            self._editor.blockSignals(False)
            self._fig_id_label.clear()

    def _make_list_item(self, fig: dict) -> QListWidgetItem:
        """Build a QListWidgetItem with thumbnail icon and status indicator."""
        crop_path = self._proj / fig.get("crop_path", "")
        pixmap = QPixmap(str(crop_path)) if crop_path.exists() else QPixmap()
        if not pixmap.isNull():
            icon = QIcon(pixmap.scaled(
                72, 72,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
        else:
            icon = QIcon()
        has_alt = bool(fig.get("alt_text", "").strip())
        label = ("✓ " if has_alt else "  ") + fig.get("id", "figure")
        return QListWidgetItem(icon, label)

    def _refresh_list_item(self, row: int):
        """Update the ✓ indicator for the given list row after a save."""
        page_num = self._pages[self._current_page_idx]
        figs = self._figures_for_page(page_num)
        if 0 <= row < len(figs):
            self._fig_list.takeItem(row)
            self._fig_list.insertItem(row, self._make_list_item(figs[row]))
            self._fig_list.setCurrentRow(row)

    def _show_empty(self):
        self.setWindowTitle(f"Alt-Text Editor — {self._proj.name} (no figures)")
        self._nav_label.setText("  No figures  ")
        self._prev_btn.setEnabled(False)
        self._next_btn.setEnabled(False)
        self._editor.setReadOnly(True)
        self.statusBar().showMessage("No figures found in figures.json.", 0)

    def _prev_page(self):
        if self._current_page_idx > 0:
            self._load_page(self._current_page_idx - 1)

    def _next_page(self):
        if self._current_page_idx < len(self._pages) - 1:
            self._load_page(self._current_page_idx + 1)

    # ------------------------------------------------------------------
    # Figure selection
    # ------------------------------------------------------------------

    def _on_figure_selected(self, row: int):
        if row < 0:
            return
        self._flush_current()

        page_num = self._pages[self._current_page_idx]
        figs = self._figures_for_page(page_num)
        if row >= len(figs):
            return
        fig = figs[row]
        self._current_figure = fig

        # Load crop image
        crop_path = self._proj / fig.get("crop_path", "")
        if crop_path.exists():
            self._image_view.set_image(QPixmap(str(crop_path)))
        else:
            self._image_view.set_image(None)
            self.statusBar().showMessage(f"Image not found: {crop_path}", 5000)

        # Load page context with highlight box
        page_num = fig.get("page_number", 0)
        page_pm = self._page_pixmap(page_num)
        box = fig.get("box")
        self._page_view.set_image(page_pm, highlight=box)

        # Load alt-text — block signals so loading doesn't set dirty flag
        self._editor.blockSignals(True)
        self._editor.setPlainText(fig.get("alt_text", ""))
        self._editor.blockSignals(False)

        self._fig_id_label.setText(f"<b>{fig.get('id', '')}</b>")

    # ------------------------------------------------------------------
    # Change tracking
    # ------------------------------------------------------------------

    def _on_text_changed(self):
        self._dirty = True

    def closeEvent(self, event):
        if self._dirty:
            reply = QMessageBox.question(
                self, "Unsaved changes",
                "You have unsaved changes. Save before closing?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )
            if reply == QMessageBox.StandardButton.Save:
                self._save_data()
                event.accept()
            elif reply == QMessageBox.StandardButton.Discard:
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(
        prog="alt_text_editor.py",
        description=(
            "Alt-Text Editor — write and review alt-text descriptions for figure crops.\n\n"
            "Navigate page by page. Figures on the current page appear as thumbnails\n"
            "in the left panel — click one to view the crop and edit its alt-text.\n"
            "A ✓ mark indicates that alt-text has already been written.\n\n"
            "Keyboard shortcuts:\n"
            "  Ctrl+← →       previous / next page\n"
            "  Ctrl+S         save to figures.json\n"
            "  Ctrl+scroll / pinch   zoom image"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "project_dir",
        metavar="PROJECT_DIR",
        help="Path to the document project directory (e.g. data/my_document/)",
    )
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    if not project_dir.exists():
        parser.error(f"Project directory not found: {project_dir}")

    app = QApplication(sys.argv)
    app.setApplicationName("Alt-Text Editor")
    window = AltTextEditorWindow(project_dir)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
