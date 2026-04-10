"""
ocr_editor.py — PyQt6 GUI for line-by-line OCR correction.

Launch
------
    python gui/ocr_editor.py <project_dir>

Layout
------
  ┌──────────────────────────────────────────────────────────────────┐
  │  Toolbar: [◀ Prev Page] [Page N / Total] [▶ Next Page]  [Save]  │
  ├──────────────────────────┬───────────────────────────────────────┤
  │  Page thumbnail          │  Line-by-line editor                  │
  │  (full page, scrollable) │                                       │
  │                          │  ┌───────────┬──────────────────────┐ │
  │  Clicking a line in the  │  │ Line crop │  [Text field]  [Type]│ │
  │  editor highlights it    │  ├───────────┼──────────────────────┤ │
  │  on the page thumbnail.  │  │ Line crop │  [Text field]  [Type]│ │
  │                          │  ├───────────┼──────────────────────┤ │
  │                          │  │ ...       │  ...                 │ │
  │                          │  └───────────┴──────────────────────┘ │
  └──────────────────────────┴───────────────────────────────────────┘

Each row in the editor shows:
  - A crop of that line from the (unmasked) page image
  - An editable text field pre-filled with the OCR result
  - A drop-down for the line type (body / heading1 / heading2 / heading3 /
    footnote / caption / other)

Keyboard shortcuts
------------------
  Tab / Shift+Tab  : move between text fields
  Ctrl+S           : save edited OCR to ocr_edited.json
  Ctrl+← / Ctrl+→  : previous / next page
  Ctrl+D           : delete the selected line
  Ctrl+Enter       : insert a new empty line after the current one
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor, QKeySequence, QPainter, QPen, QPixmap, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFrame, QGraphicsPixmapItem,
    QGraphicsRectItem, QGraphicsScene, QGraphicsView,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy,
    QSplitter, QStatusBar, QToolBar, QVBoxLayout, QWidget,
)
from PIL import Image

LINE_CROP_HEIGHT = 48     # pixels — height of each line strip in the editor

LINE_TYPES = ["body", "heading1", "heading2", "heading3", "footnote", "caption", "other"]

HIGHLIGHT_COLOUR = QColor(255, 200, 50, 180)


# ---------------------------------------------------------------------------
# AspectLabel — QLabel that scales its pixmap to fit width, preserving ratio
# ---------------------------------------------------------------------------

class AspectLabel(QLabel):
    """Displays a pixmap scaled to the label's available width, aspect-ratio correct."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._src: Optional[QPixmap] = None
        self.setMinimumWidth(1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

    def sizeHint(self) -> QSize:
        return QSize(1, LINE_CROP_HEIGHT)

    def setSourcePixmap(self, pixmap: QPixmap):
        self._src = pixmap
        self._refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self):
        if self._src is None or self.width() < 1:
            return
        scaled = self._src.scaled(
            self.width(), LINE_CROP_HEIGHT,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(scaled)


# ---------------------------------------------------------------------------
# Page thumbnail with line highlight
# ---------------------------------------------------------------------------

class PageThumbnail(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._highlight: Optional[QGraphicsRectItem] = None
        self._page_h = 1
        self._page_w = 1
        self._zoom = 1.0

    def set_image(self, pixmap: QPixmap):
        self._scene.clear()
        self._highlight = None
        self._scene.addPixmap(pixmap)
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        self._page_w = pixmap.width()
        self._page_h = pixmap.height()
        self.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def highlight_line(self, bbox: dict):
        """Draw a yellow rectangle over the given fractional bbox."""
        if self._highlight is not None:
            self._scene.removeItem(self._highlight)
            self._highlight = None

        if not bbox:
            return

        x = bbox.get("x", 0) * self._page_w
        y = bbox.get("y", 0) * self._page_h
        w = bbox.get("w", 1) * self._page_w
        h = bbox.get("h", 0.02) * self._page_h

        rect_item = QGraphicsRectItem(x, y, w, h)
        pen = QPen(QColor(255, 160, 0), 3)
        rect_item.setPen(pen)
        rect_item.setBrush(HIGHLIGHT_COLOUR)
        self._scene.addItem(rect_item)
        self._highlight = rect_item

        # Scroll so the highlighted region is visible
        self.ensureVisible(x, y, w, h, 20, 80)

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
# LineRow — one row in the editor scroll area
# ---------------------------------------------------------------------------

class LineRow(QFrame):
    """A single-line editor row: [image crop] [text field] [type selector]"""

    focused = pyqtSignal(int)   # row index
    changed = pyqtSignal(int)   # row index (text or type changed)
    delete_requested = pyqtSignal(int)
    insert_requested = pyqtSignal(int)

    def __init__(self, row_idx: int, line_data: dict, page_img: Image.Image, parent=None):
        super().__init__(parent)
        self._idx = row_idx
        self._data = line_data

        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setLineWidth(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)

        # --- Line image crop (full width, aspect-ratio correct) --------------
        crop_label = AspectLabel()
        crop_label.setFixedHeight(LINE_CROP_HEIGHT)
        crop_pixmap = self._make_crop_pixmap(page_img, line_data.get("bbox", {}))
        crop_label.setSourcePixmap(crop_pixmap)
        layout.addWidget(crop_label)

        # --- Controls row: text field + type selector + delete --------------
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)

        self._text_edit = QLineEdit(line_data.get("text", ""))
        self._text_edit.setPlaceholderText("(empty)")
        self._text_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._text_edit.focusInEvent = self._on_focus
        self._text_edit.textChanged.connect(self._on_text_changed)
        controls.addWidget(self._text_edit)

        self._type_combo = QComboBox()
        self._type_combo.addItems(LINE_TYPES)
        current_type = line_data.get("type", "body")
        idx = LINE_TYPES.index(current_type) if current_type in LINE_TYPES else 0
        self._type_combo.setCurrentIndex(idx)
        self._type_combo.setFixedWidth(100)
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        controls.addWidget(self._type_combo)

        del_btn = QPushButton("✕")
        del_btn.setFixedWidth(28)
        del_btn.setToolTip("Delete this line (Ctrl+D)")
        del_btn.clicked.connect(lambda: self.delete_requested.emit(self._idx))
        controls.addWidget(del_btn)

        layout.addLayout(controls)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_crop_pixmap(page_img: Image.Image, bbox: dict) -> QPixmap:
        W, H = page_img.size
        x0 = int(bbox.get("x", 0) * W)
        y0 = int(bbox.get("y", 0) * H)
        x1 = int((bbox.get("x", 0) + bbox.get("w", 1)) * W)
        y1 = int((bbox.get("y", 0) + bbox.get("h", 0.02)) * H)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(W, x1), min(H, y1)

        if x1 <= x0 or y1 <= y0:
            px = QPixmap(1, LINE_CROP_HEIGHT)
            px.fill(QColor(230, 230, 230))
            return px

        crop = page_img.crop((x0, y0, x1, y1))

        # Scale to LINE_CROP_HEIGHT, preserving aspect ratio
        aspect = (x1 - x0) / max(1, y1 - y0)
        target_w = max(1, int(LINE_CROP_HEIGHT * aspect))
        crop = crop.resize((target_w, LINE_CROP_HEIGHT), Image.LANCZOS)

        # Convert PIL → QPixmap
        from PyQt6.QtGui import QImage
        crop_rgb = crop.convert("RGB")
        data = crop_rgb.tobytes("raw", "RGB")
        qimg = QImage(data, crop_rgb.width, crop_rgb.height,
                      crop_rgb.width * 3, QImage.Format.Format_RGB888)
        return QPixmap.fromImage(qimg)

    def get_data(self) -> dict:
        """Return the line dict with updated text and type."""
        return {
            **self._data,
            "text": self._text_edit.text(),
            "type": self._type_combo.currentText(),
        }

    def set_highlighted(self, highlighted: bool):
        if highlighted:
            self.setStyleSheet("QFrame { background-color: #fff8d0; }")
        else:
            self.setStyleSheet("")

    def focus_text(self):
        self._text_edit.setFocus()

    # ------------------------------------------------------------------
    # Internal slots
    # ------------------------------------------------------------------

    def _on_focus(self, event):
        self.focused.emit(self._idx)
        QLineEdit.focusInEvent(self._text_edit, event)

    def _on_text_changed(self):
        self._data["text"] = self._text_edit.text()
        self.changed.emit(self._idx)

    def _on_type_changed(self):
        self._data["type"] = self._type_combo.currentText()
        self.changed.emit(self._idx)


# ---------------------------------------------------------------------------
# LineEditorPanel — the right-hand scrollable list of LineRows
# ---------------------------------------------------------------------------

class LineEditorPanel(QScrollArea):
    line_focused = pyqtSignal(int)   # row index

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setSpacing(2)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.addStretch()
        self.setWidget(self._container)

        self._rows: list[LineRow] = []
        self._page_data: Optional[dict] = None
        self._page_img: Optional[Image.Image] = None
        self._focused_idx: int = -1

    def load_page(self, page_data: dict, page_img: Image.Image):
        """Clear and repopulate rows for the given page OCR data."""
        self._page_data = page_data
        self._page_img = page_img
        self._rebuild_rows()

    def _rebuild_rows(self):
        # Remove existing rows
        for row in self._rows:
            self._layout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()
        self._focused_idx = -1

        lines = self._page_data.get("lines", []) if self._page_data else []

        for i, line in enumerate(lines):
            row = LineRow(i, line, self._page_img)
            row.focused.connect(self._on_row_focused)
            row.delete_requested.connect(self._on_delete_row)
            row.insert_requested.connect(self._on_insert_row)
            # Insert before the stretch at the end
            self._layout.insertWidget(self._layout.count() - 1, row)
            self._rows.append(row)

    def get_lines(self) -> list[dict]:
        return [row.get_data() for row in self._rows]

    def _on_row_focused(self, idx: int):
        if self._focused_idx >= 0 and self._focused_idx < len(self._rows):
            self._rows[self._focused_idx].set_highlighted(False)
        self._focused_idx = idx
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_highlighted(True)
        self.line_focused.emit(idx)

    def _on_delete_row(self, idx: int):
        if 0 <= idx < len(self._rows):
            row = self._rows.pop(idx)
            self._layout.removeWidget(row)
            row.deleteLater()
            # Re-number remaining rows
            for i, r in enumerate(self._rows):
                r._idx = i

    def _on_insert_row(self, after_idx: int):
        new_line = {
            "line_id": f"manual_{after_idx}",
            "text": "",
            "type": "body",
            "bbox": {},
        }
        insert_pos = after_idx + 1
        row = LineRow(insert_pos, new_line, self._page_img)
        row.focused.connect(self._on_row_focused)
        row.delete_requested.connect(self._on_delete_row)
        row.insert_requested.connect(self._on_insert_row)
        self._rows.insert(insert_pos, row)
        self._layout.insertWidget(insert_pos, row)
        # Re-number
        for i, r in enumerate(self._rows):
            r._idx = i
        QTimer.singleShot(50, row.focus_text)

    def get_focused_bbox(self) -> dict:
        if 0 <= self._focused_idx < len(self._rows):
            return self._rows[self._focused_idx]._data.get("bbox", {})
        return {}


# ---------------------------------------------------------------------------
# MainWindow
# ---------------------------------------------------------------------------

class OCREditor(QMainWindow):
    def __init__(self, project_dir: Path):
        super().__init__()
        self._proj = project_dir
        self._page_records: list[dict] = []
        self._ocr_results: list[dict] = []
        self._current_idx: int = 0

        self._load_data()
        self._build_ui()
        self._load_page(0)

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _load_data(self):
        pages_path = self._proj / "pages.json"
        edited_path = self._proj / "ocr_edited.json"
        raw_path    = self._proj / "ocr_raw.json"

        if not pages_path.exists():
            QMessageBox.critical(self, "Error", f"pages.json not found in {self._proj}")
            sys.exit(1)

        ocr_path = edited_path if edited_path.exists() else raw_path
        if not ocr_path.exists():
            QMessageBox.critical(self, "Error",
                "No OCR data found. Run the 'ocr' step first.")
            sys.exit(1)

        self._page_records = json.loads(pages_path.read_text())
        self._ocr_results  = json.loads(ocr_path.read_text())

        # Index OCR results by page number for fast lookup
        self._ocr_by_page: dict[int, dict] = {
            r["page_number"]: r for r in self._ocr_results
        }

    def _save_data(self):
        # Collect edited lines from current page before saving
        self._flush_current_page()
        edited_path = self._proj / "ocr_edited.json"
        edited_path.write_text(json.dumps(self._ocr_results, indent=2))
        self.statusBar().showMessage(f"Saved to {edited_path.name}", 3000)

    def _flush_current_page(self):
        """Write editor panel lines back into ocr_results for current page."""
        if not self._ocr_results:
            return
        page_num = self._page_records[self._current_idx]["page_number"]
        page_result = self._ocr_by_page.get(page_num)
        if page_result is not None:
            page_result["lines"] = self._editor_panel.get_lines()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.setWindowTitle(f"OCR Editor — {self._proj.name}")
        self.resize(1400, 900)

        # Toolbar
        toolbar = QToolBar()
        self.addToolBar(toolbar)

        self._prev_btn  = QPushButton("◀ Prev Page")
        self._next_btn  = QPushButton("Next Page ▶")
        self._page_label = QLabel()
        self._save_btn  = QPushButton("💾 Save")

        toolbar.addWidget(self._prev_btn)
        toolbar.addWidget(self._page_label)
        toolbar.addWidget(self._next_btn)
        toolbar.addSeparator()
        toolbar.addWidget(self._save_btn)

        # Splitter: thumbnail | editor
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(splitter)

        self._thumbnail = PageThumbnail()
        self._thumbnail.setMinimumWidth(300)
        self._thumbnail.setMaximumWidth(500)
        splitter.addWidget(self._thumbnail)

        self._editor_panel = LineEditorPanel()
        splitter.addWidget(self._editor_panel)
        splitter.setSizes([380, 1000])

        self.setStatusBar(QStatusBar())

        # Shortcuts
        QShortcut(QKeySequence("Ctrl+S"),     self, self._save_data)
        QShortcut(QKeySequence("Ctrl+Left"),  self, self._prev_page)
        QShortcut(QKeySequence("Ctrl+Right"), self, self._next_page)

        # Connections
        self._prev_btn.clicked.connect(self._prev_page)
        self._next_btn.clicked.connect(self._next_page)
        self._save_btn.clicked.connect(self._save_data)
        self._editor_panel.line_focused.connect(self._on_line_focused)

    # ------------------------------------------------------------------
    # Page navigation
    # ------------------------------------------------------------------

    def _load_page(self, idx: int):
        # Save edits for the page we're leaving
        if self._current_idx != idx:
            self._flush_current_page()

        self._current_idx = idx
        rec = self._page_records[idx]
        page_num = rec["page_number"]

        # Load the unmasked page image for line crops
        img_path = self._proj / rec["image_path"]
        page_img = Image.open(img_path).convert("RGB")

        # Load page thumbnail
        pixmap = QPixmap(str(img_path))
        self._thumbnail.set_image(pixmap)

        # Load OCR data
        page_result = self._ocr_by_page.get(page_num, {"page_number": page_num, "lines": []})
        self._editor_panel.load_page(page_result, page_img)

        self._page_label.setText(f"  Page {page_num} / {len(self._page_records)}  ")
        self._prev_btn.setEnabled(idx > 0)
        self._next_btn.setEnabled(idx < len(self._page_records) - 1)

    def _prev_page(self):
        if self._current_idx > 0:
            self._load_page(self._current_idx - 1)

    def _next_page(self):
        if self._current_idx < len(self._page_records) - 1:
            self._load_page(self._current_idx + 1)

    # ------------------------------------------------------------------
    # Line focus → thumbnail highlight
    # ------------------------------------------------------------------

    def _on_line_focused(self, row_idx: int):
        bbox = self._editor_panel.get_focused_bbox()
        self._thumbnail.highlight_line(bbox)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(
        prog="ocr_editor.py",
        description=(
            "OCR Line Editor — review and correct OCR text line by line.\n\n"
            "Each row shows a crop of the OCR line alongside an editable text\n"
            "field and a type selector.  Edits are saved to ocr_edited.json,\n"
            "leaving the original ocr_raw.json untouched.\n\n"
            "Line types:\n"
            "  body              regular paragraph text\n"
            "  heading1/2/3      section heading (level 1–3)\n"
            "  caption           figure or table caption\n"
            "  footnote          footnote (collected into endnotes on assembly)\n"
            "  other             ignored in structure / assembly steps\n\n"
            "Keyboard shortcuts:\n"
            "  Ctrl+← →    previous / next page\n"
            "  Ctrl+S      save to ocr_edited.json\n"
            "  Tab         move to next text field\n"
            "  Ctrl+Enter  insert new empty line after current\n"
            "  Ctrl+D      delete current line"
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
    app.setApplicationName("OCR Editor")
    window = OCREditor(project_dir)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
