"""
table_editor.py — PyQt6 GUI for reviewing and editing table OCR content.

Launch
------
    python gui/table_editor.py <project_dir>

Layout
------
  ┌──────────────────────────────────────────────────────────────────────┐
  │  Toolbar: [◀ Prev] [Table N / M — page P] [▶ Next]  [Save]          │
  ├──────────────────────────┬───────────────────────────────────────────┤
  │  Table image             │  Table:  table_1_1      Format: [md ▼]   │
  │  (crop, zoom/pan)        │ ┌────────────────────────────────────────┐│
  │                          │ │  | Col A | Col B |                     ││
  │  Ctrl+scroll to zoom     │ │  |-------|-------|                     ││
  │  Scroll to pan           │ │  | ...   | ...   |                     ││
  │                          │ ├────────────────────────────────────────┤│
  │                          │ │  Rendered preview (HTML table)         ││
  │                          │ └────────────────────────────────────────┘│
  └──────────────────────────┴───────────────────────────────────────────┘

Editing
-------
The right panel contains a monospace plain-text editor (top) and a live
rendered preview of the Markdown table (bottom).  The preview updates as you
type and shows the table as it will appear in the assembled document.

Changes are flushed into memory when you navigate away or save.  Saving
writes the entire tables.json back to disk.

When you manually edit content the Format selector is left unchanged so you
can explicitly mark it "markdown" once the table is correct.

Keyboard shortcuts
------------------
  Ctrl+S           : save all changes to tables.json
  Ctrl+← / Ctrl+→  : previous / next table (only when editor is not focused;
                     inside the editor these keys move to line start/end as normal)
  Ctrl++ / Ctrl+-  : increase / decrease editor font size
"""

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

from PyQt6.QtCore import QEvent, QRectF, Qt
from PyQt6.QtGui import (
    QFont, QKeySequence, QPainter, QPixmap, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy,
    QSplitter, QStatusBar, QTextBrowser, QToolBar, QVBoxLayout, QWidget,
    QGraphicsScene, QGraphicsView,
)


CONTENT_FORMATS = ["markdown", "preformatted"]

_SEP_ROW = re.compile(r"^\|[\s|:\-]+\|$")

_PREVIEW_CSS = """
<style>
body { margin: 6px; font-family: sans-serif; font-size: 13px; }
table { border-collapse: collapse; }
th, td { border: 1px solid #aaa; padding: 4px 10px; }
th { background: #e8e8e8; font-weight: bold; }
tr:nth-child(even) td { background: #f7f7f7; }
.no-table { color: #888; font-style: italic; }
</style>
"""


def _md_table_to_html(md: str) -> str:
    """Convert a GitHub-flavoured Markdown table string to an HTML snippet.

    Returns an empty string if no table rows are found.
    """
    lines = md.strip().splitlines()
    rows = [l.rstrip() for l in lines if l.strip().startswith("|")]
    if not rows:
        return ""

    header_cells: list[str] | None = None
    body_rows: list[list[str]] = []
    past_sep = False

    for row in rows:
        if _SEP_ROW.match(row.strip()):
            past_sep = True
            continue
        cells = [html.escape(c.strip()) for c in row.split("|")[1:-1]]
        if header_cells is None and not past_sep:
            header_cells = cells
        else:
            body_rows.append(cells)

    parts = ["<table>"]
    if header_cells:
        parts.append("<thead><tr>")
        parts.extend(f"<th>{c}</th>" for c in header_cells)
        parts.append("</tr></thead>")
    parts.append("<tbody>")
    for row_cells in body_rows:
        parts.append("<tr>")
        parts.extend(f"<td>{c}</td>" for c in row_cells)
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Table image view — zoomable / pannable
# ---------------------------------------------------------------------------

class TableImageView(QGraphicsView):
    """Displays a table crop image; Ctrl+scroll to zoom, scroll to pan."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._zoom = 1.0

    def set_image(self, pixmap: QPixmap | None):
        self._scene.clear()
        self._zoom = 1.0
        self.resetTransform()
        if pixmap is not None and not pixmap.isNull():
            self._scene.addPixmap(pixmap)
            self._scene.setSceneRect(QRectF(pixmap.rect()))
            self.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        else:
            self._scene.setSceneRect(QRectF(0, 0, 400, 200))

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

class TableEditorWindow(QMainWindow):
    def __init__(self, project_dir: Path):
        super().__init__()
        self._proj = project_dir
        self._tables: list[dict] = []
        self._current_idx: int = -1   # -1 = nothing loaded yet
        self._dirty: bool = False

        self._load_data()
        self._build_ui()

        if self._tables:
            self._load_table(0)
        else:
            self._show_empty()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _load_data(self):
        tables_path = self._proj / "tables.json"
        if not tables_path.exists():
            QMessageBox.critical(
                self, "Error",
                f"tables.json not found in {self._proj}.\n"
                "Run the 'extract' step first to generate table crops.",
            )
            sys.exit(1)
        self._tables = json.loads(tables_path.read_text())

    def _save_data(self):
        self._flush_current()
        tables_path = self._proj / "tables.json"
        tables_path.write_text(json.dumps(self._tables, indent=2))
        self._dirty = False
        self.statusBar().showMessage(f"Saved to {tables_path.name}", 3000)

    def _flush_current(self):
        """Write the editor contents back into the current table record."""
        if not self._tables or self._current_idx < 0:
            return
        tbl = self._tables[self._current_idx]
        tbl["content"] = self._editor.toPlainText()
        tbl["content_format"] = self._format_combo.currentText()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.setWindowTitle(f"Table Editor — {self._proj.name}")
        self.resize(1400, 800)

        # --- Toolbar ---
        toolbar = QToolBar()
        self.addToolBar(toolbar)

        self._prev_btn  = QPushButton("◀ Prev")
        self._nav_label = QLabel()
        self._nav_label.setMinimumWidth(220)
        self._nav_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._next_btn  = QPushButton("Next ▶")
        self._save_btn  = QPushButton("💾 Save")

        toolbar.addWidget(self._prev_btn)
        toolbar.addWidget(self._nav_label)
        toolbar.addWidget(self._next_btn)
        toolbar.addSeparator()
        toolbar.addWidget(self._save_btn)

        # --- Main splitter: image | editor ---
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(splitter)

        # Left: table crop image
        self._image_view = TableImageView()
        self._image_view.setMinimumWidth(300)
        splitter.addWidget(self._image_view)

        # Right: meta bar + plain-text editor
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(6, 6, 6, 6)
        right_layout.setSpacing(6)

        # Meta bar: table ID and format selector
        meta_bar = QHBoxLayout()
        meta_bar.addWidget(QLabel("Table:"))
        self._id_label = QLabel()
        self._id_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        meta_bar.addWidget(self._id_label)
        meta_bar.addStretch()
        meta_bar.addWidget(QLabel("Format:"))
        self._format_combo = QComboBox()
        self._format_combo.addItems(CONTENT_FORMATS)
        self._format_combo.setToolTip(
            "markdown      — content is a GitHub-flavored Markdown table\n"
            "preformatted  — content is plain text (rendered as a code block in output)"
        )
        self._format_combo.setFixedWidth(130)
        self._format_combo.currentIndexChanged.connect(self._on_format_changed)
        meta_bar.addWidget(self._format_combo)
        right_layout.addLayout(meta_bar)

        # Vertical splitter: monospace editor on top, rendered preview below
        edit_split = QSplitter(Qt.Orientation.Vertical)

        self._editor = QPlainTextEdit()
        mono = QFont("Courier New")
        mono.setPointSize(11)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._editor.setFont(mono)
        self._editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._editor.setPlaceholderText(
            "Table OCR content will appear here.\n\n"
            "Edit the Markdown table directly — for example:\n\n"
            "| Column A | Column B |\n"
            "|----------|----------|\n"
            "| value    | value    |\n\n"
            "Set Format to 'markdown' once the table is correct."
        )
        self._editor.textChanged.connect(self._on_text_changed)
        self._editor.textChanged.connect(self._update_preview)
        edit_split.addWidget(self._editor)

        self._preview = QTextBrowser()
        self._preview.setOpenLinks(False)
        edit_split.addWidget(self._preview)
        edit_split.setSizes([420, 200])

        right_layout.addWidget(edit_split)

        splitter.addWidget(right)
        splitter.setSizes([580, 820])

        self.setStatusBar(QStatusBar())

        # Keyboard shortcuts
        # Ctrl+S fires from anywhere in the window.
        # Ctrl+Left/Right for table navigation is handled in keyPressEvent so
        # it only fires when the text editor does NOT have focus — preserving
        # normal cursor-movement behaviour inside the editor.
        QShortcut(QKeySequence("Ctrl+S"), self, self._save_data)
        QShortcut(QKeySequence("Ctrl+="), self, self._increase_font)
        QShortcut(QKeySequence("Ctrl++"), self, self._increase_font)
        QShortcut(QKeySequence("Ctrl+-"), self, self._decrease_font)

        # Connections
        self._prev_btn.clicked.connect(self._prev_table)
        self._next_btn.clicked.connect(self._next_table)
        self._save_btn.clicked.connect(self._save_data)

    # ------------------------------------------------------------------
    # Table navigation
    # ------------------------------------------------------------------

    def _load_table(self, table_idx: int):
        # Flush edits from the table we're leaving
        if self._tables:
            self._flush_current()

        self._current_idx = table_idx
        tbl = self._tables[table_idx]

        # Navigation bar
        self._nav_label.setText(
            f"  Table {table_idx + 1} / {len(self._tables)}"
            f"  —  page {tbl.get('page_number', '?')}  "
        )
        self._prev_btn.setEnabled(table_idx > 0)
        self._next_btn.setEnabled(table_idx < len(self._tables) - 1)

        # Table ID
        self._id_label.setText(tbl.get("id", ""))

        # Crop image
        crop_path = tbl.get("crop_path", "")
        img_path = self._proj / crop_path if crop_path else None
        if img_path and img_path.exists():
            self._image_view.set_image(QPixmap(str(img_path)))
        else:
            self._image_view.set_image(None)
            if crop_path:
                self.statusBar().showMessage(
                    f"Image not found: {img_path}", 5000
                )

        # Content — block signals so loading doesn't mark as dirty
        self._editor.blockSignals(True)
        self._editor.setPlainText(tbl.get("content", ""))
        self._editor.blockSignals(False)

        # Format
        fmt = tbl.get("content_format", "preformatted")
        fmt_idx = CONTENT_FORMATS.index(fmt) if fmt in CONTENT_FORMATS else 1
        self._format_combo.blockSignals(True)
        self._format_combo.setCurrentIndex(fmt_idx)
        self._format_combo.blockSignals(False)

        # Refresh rendered preview
        self._update_preview()

    def _show_empty(self):
        self.setWindowTitle(f"Table Editor — {self._proj.name} (no tables)")
        self._nav_label.setText("  No tables  ")
        self._prev_btn.setEnabled(False)
        self._next_btn.setEnabled(False)
        self._editor.setReadOnly(True)
        self._editor.setPlaceholderText(
            "No tables found in tables.json.\n\n"
            "Add table bounding boxes in the Bounding Box Editor, "
            "then re-run the 'extract' step."
        )
        self.statusBar().showMessage("No tables found.", 0)

    def _prev_table(self):
        if self._current_idx > 0:
            self._load_table(self._current_idx - 1)

    def _next_table(self):
        if self._current_idx < len(self._tables) - 1:
            self._load_table(self._current_idx + 1)

    # ------------------------------------------------------------------
    # Keyboard navigation (table switching)
    # ------------------------------------------------------------------

    def keyPressEvent(self, event):
        """Ctrl+Left/Right switch tables — but only when the editor lacks focus.

        When the Markdown editor has focus it consumes Ctrl+Arrow itself
        (standard line-start/end movement), so this method is only reached
        when focus is on the toolbar or another non-text widget.
        """
        ctrl = event.modifiers() & Qt.KeyboardModifier.ControlModifier
        if ctrl:
            if event.key() == Qt.Key.Key_Left:
                self._prev_table()
                return
            if event.key() == Qt.Key.Key_Right:
                self._next_table()
                return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Font size
    # ------------------------------------------------------------------

    def _increase_font(self):
        font = self._editor.font()
        font.setPointSize(min(font.pointSize() + 1, 32))
        self._editor.setFont(font)

    def _decrease_font(self):
        font = self._editor.font()
        font.setPointSize(max(font.pointSize() - 1, 6))
        self._editor.setFont(font)

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    def _update_preview(self):
        text = self._editor.toPlainText()
        table_html = _md_table_to_html(text)
        if table_html:
            body = table_html
        else:
            body = "<p class='no-table'>No Markdown table found — edit the content above.</p>"
        self._preview.setHtml(_PREVIEW_CSS + body)

    # ------------------------------------------------------------------
    # Change tracking
    # ------------------------------------------------------------------

    def _on_text_changed(self):
        self._dirty = True

    def _on_format_changed(self):
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
        prog="table_editor.py",
        description=(
            "Table Editor — review and correct table OCR content.\n\n"
            "Shows each table's crop image on the left alongside a monospace\n"
            "plain-text editor on the right.  Edit Markdown table syntax directly\n"
            "and set the Format to 'markdown' when the table is correct.\n\n"
            "Format values:\n"
            "  markdown      content is GitHub-flavored Markdown (| Col | Col |)\n"
            "                rendered as a table in the assembled document\n"
            "  preformatted  content is plain text, rendered as a code block\n\n"
            "Keyboard shortcuts:\n"
            "  Ctrl+← →       previous / next table (when editor not focused)\n"
            "  Ctrl+S         save to tables.json\n"
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
    app.setApplicationName("Table Editor")
    window = TableEditorWindow(project_dir)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
