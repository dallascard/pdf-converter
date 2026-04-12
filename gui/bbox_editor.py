"""
bbox_editor.py — PyQt6 GUI for reviewing and editing bounding boxes.

Launch
------
    python gui/bbox_editor.py <project_dir>

Layout
------
  ┌─────────────────────────────────────────────────────────────────┐
  │  Toolbar: [◀ Prev] [Page N / Total] [▶ Next]  [Save] [Apply Global] │
  ├──────────────────────────┬──────────────────────────────────────┤
  │  Page canvas             │  Box list panel                       │
  │  (zoomable, scrollable)  │                                       │
  │                          │  Figures (red):                       │
  │  Drag to create new box  │  ☑  fig_1_1  [Del]                   │
  │  Click box to select     │  ☑  fig_1_2  [Del]                   │
  │  Drag handle to resize   │                                       │
  │                          │  Exclusions (blue):                   │
  │                          │  ☑  header   [Del]                   │
  │                          │  ☑  page_num [Del]                   │
  │                          │                                       │
  │                          │  Global exclusions:                   │
  │                          │  ☑  header   [Del]                   │
  │                          │                                       │
  │                          │  [+ Add Figure] [+ Add Exclusion]     │
  └──────────────────────────┴──────────────────────────────────────┘

Controls
--------
  Left-drag on empty canvas  : draw a new figure box
  Left-drag on a box         : move the box
  Left-drag on a corner handle : resize
  Right-click on a box       : context menu (delete / convert type)
  Scroll wheel               : zoom
  Ctrl+S                     : save
  ←/→ arrow keys             : previous / next page
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import (
    QEvent, QPointF, QRectF, QSizeF, Qt, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QKeySequence, QPainter, QPen, QPixmap,
    QShortcut, QWheelEvent,
)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
    QGraphicsItem, QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene,
    QGraphicsView, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QStatusBar, QTextBrowser, QToolBar,
    QVBoxLayout, QWidget,
)

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

FIGURE_COLOUR    = QColor(220, 50,  50,  160)   # red
EXCLUSION_COLOUR = QColor(50,  100, 220, 160)   # blue
CAPTION_COLOUR   = QColor(160, 50,  220, 160)   # purple
NOTE_COLOUR      = QColor(220, 140, 50,  160)   # orange
TABLE_COLOUR     = QColor(30,  180, 180, 160)   # teal
HEADING_COLOUR   = QColor(50,  200, 120, 160)   # green
HANDLE_SCREEN_PX = 10   # target handle size in screen pixels
MIN_BOX_PX       = 5    # minimum box size (pixels at 1× zoom)


# ---------------------------------------------------------------------------
# BoxItem — a QGraphicsRectItem with resize handles
# ---------------------------------------------------------------------------

class BoxItem(QGraphicsRectItem):
    """
    A draggable, resizable rectangle overlaid on the page image.

    *box_data*  : the dict from boxes.json (has x, y, w, h as fractions)
    *page_w/h*  : pixel dimensions of the page image (for coordinate conversion)
    *box_type*  : "figure" | "exclusion" | "table" | "caption_zone" | "note_zone"
    """

    def __init__(
        self,
        box_data: dict,
        page_w: int,
        page_h: int,
        box_type: str = "figure",
        on_delete=None,
        parent=None,
    ):
        super().__init__(parent)
        self._data = box_data
        self._pw = page_w
        self._ph = page_h
        self._type = box_type
        self._on_delete = on_delete
        self._dragging_handle: Optional[int] = None  # 0-3 corners
        self._drag_start: Optional[QPointF] = None
        self._orig_rect: Optional[QRectF] = None

        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)

        colour = {
            "figure":        FIGURE_COLOUR,
            "exclusion":     EXCLUSION_COLOUR,
            "caption_zone":  CAPTION_COLOUR,
            "note_zone":     NOTE_COLOUR,
            "table":         TABLE_COLOUR,
            "heading_zone":  HEADING_COLOUR,
        }.get(box_type, FIGURE_COLOUR)

        self.setPen(QPen(colour.darker(140), 2))
        self.setBrush(QBrush(colour))
        self._sync_rect_from_data()

    # ------------------------------------------------------------------
    # Coordinate helpers
    # ------------------------------------------------------------------

    def _sync_rect_from_data(self):
        d = self._data
        x = d["x"] * self._pw
        y = d["y"] * self._ph
        w = d["w"] * self._pw
        h = d["h"] * self._ph
        self.setRect(QRectF(x, y, w, h))

    def _sync_data_from_rect(self):
        r = self.rect()
        # Account for item position (when moved)
        pos = self.pos()
        x = (r.x() + pos.x()) / self._pw
        y = (r.y() + pos.y()) / self._ph
        w = r.width() / self._pw
        h = r.height() / self._ph
        self._data["x"] = round(max(0.0, min(1.0, x)), 4)
        self._data["y"] = round(max(0.0, min(1.0, y)), 4)
        self._data["w"] = round(max(0.0, min(1.0 - self._data["x"], w)), 4)
        self._data["h"] = round(max(0.0, min(1.0 - self._data["y"], h)), 4)

    # ------------------------------------------------------------------
    # Handles
    # ------------------------------------------------------------------

    def _handle_size(self) -> float:
        """Handle size in item coordinates, scaled so it's ~HANDLE_SCREEN_PX on screen."""
        views = self.scene().views() if self.scene() else []
        if views:
            scale = views[0].transform().m11()  # horizontal scale factor
            return HANDLE_SCREEN_PX / scale if scale > 0 else HANDLE_SCREEN_PX
        # Fallback: 1.5% of the shorter page dimension
        return max(8.0, min(self._pw, self._ph) * 0.015)

    def _handle_rects(self) -> list[QRectF]:
        """Return QRectF for each of the four corner handles (item coords).

        Handles are placed fully inside the box, with their outer corners
        flush with the box corners, so the entire handle square is clickable.
        """
        r = self.rect()
        hs = self._handle_size()
        return [
            QRectF(r.left(),        r.top(),         hs, hs),  # top-left
            QRectF(r.right() - hs,  r.top(),         hs, hs),  # top-right
            QRectF(r.left(),        r.bottom() - hs, hs, hs),  # bottom-left
            QRectF(r.right() - hs,  r.bottom() - hs, hs, hs),  # bottom-right
        ]

    def boundingRect(self) -> QRectF:
        return super().boundingRect()

    def paint(self, painter: QPainter, option, widget=None):
        super().paint(painter, option, widget)
        # Draw corner handles when selected
        if self.isSelected():
            painter.setPen(QPen(QColor(30, 30, 30), 1))
            painter.setBrush(QBrush(QColor(220, 220, 50)))  # yellow fill, dark border
            for hr in self._handle_rects():
                painter.drawRect(hr)
        # Draw label
        label = self._data.get("label") or self._data.get("id", "")
        if label:
            painter.setPen(QPen(Qt.GlobalColor.white))
            painter.setFont(QFont("sans-serif", 8))
            painter.drawText(self.rect().adjusted(3, 2, 0, 0), label)

    def _handle_at(self, pos: QPointF) -> Optional[int]:
        for i, hr in enumerate(self._handle_rects()):
            if hr.contains(pos):
                return i
        return None

    # ------------------------------------------------------------------
    # Mouse events
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            h = self._handle_at(event.pos())
            if h is not None:
                self._dragging_handle = h
                self._drag_start = event.scenePos()
                self._orig_rect = QRectF(self.rect())
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging_handle is not None:
            delta = event.scenePos() - self._drag_start
            r = QRectF(self._orig_rect)
            h = self._dragging_handle
            if h == 0:   # top-left
                r.setTopLeft(r.topLeft() + delta)
            elif h == 1: # top-right
                r.setTopRight(r.topRight() + delta)
            elif h == 2: # bottom-left
                r.setBottomLeft(r.bottomLeft() + delta)
            else:        # bottom-right
                r.setBottomRight(r.bottomRight() + delta)
            r = r.normalized()
            if r.width() >= MIN_BOX_PX and r.height() >= MIN_BOX_PX:
                self.setRect(r)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging_handle is not None:
            self._dragging_handle = None
            self._sync_data_from_rect()
            event.accept()
            return
        super().mouseReleaseEvent(event)
        # After a move, sync data
        self._sync_data_from_rect()

    def contextMenuEvent(self, event):
        from PyQt6.QtWidgets import QMenu
        menu = QMenu()
        delete_action = menu.addAction("Delete")
        action = menu.exec(event.screenPos())
        if action == delete_action and self._on_delete:
            self._on_delete(self)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._sync_data_from_rect()
        return super().itemChange(change, value)


# ---------------------------------------------------------------------------
# PageCanvas — the zoomable QGraphicsView
# ---------------------------------------------------------------------------

class PageCanvas(QGraphicsView):
    box_created = pyqtSignal(QRectF, str)   # rect (scene), box_type

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self._draw_start: Optional[QPointF] = None
        self._draw_rect_item: Optional[QGraphicsRectItem] = None
        self._new_box_type: str = "figure"
        self._zoom = 1.0
        self._page_w = 1
        self._page_h = 1

    def set_page_image(self, pixmap: QPixmap):
        self._scene.clear()
        self._scene.addPixmap(pixmap)
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        self._page_w = pixmap.width()
        self._page_h = pixmap.height()
        self.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def add_box_item(self, box_data: dict, box_type: str, on_delete=None) -> BoxItem:
        item = BoxItem(box_data, self._page_w, self._page_h, box_type, on_delete=on_delete)
        self._scene.addItem(item)
        return item

    def clear_boxes(self):
        for item in list(self._scene.items()):
            if isinstance(item, BoxItem):
                self._scene.removeItem(item)

    def set_draw_type(self, box_type: str):
        self._new_box_type = box_type

    # Zoom / scroll
    def wheelEvent(self, event: QWheelEvent):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # Ctrl+scroll → zoom, proportional to scroll distance
            delta = event.angleDelta().y()
            factor = pow(1.0015, delta)
            self._zoom *= factor
            self.scale(factor, factor)
            event.accept()
        else:
            # Plain scroll → pan the view normally
            super().wheelEvent(event)

    def event(self, event):
        # Pinch-to-zoom (trackpad native gesture on macOS)
        if event.type() == QEvent.Type.NativeGesture:
            if event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                factor = 1.0 + event.value()
                if factor > 0:
                    self._zoom *= factor
                    self.scale(factor, factor)
                return True
        return super().event(event)

    # Draw new box by dragging on empty canvas
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            pos = self.mapToScene(event.pos())
            item = self._scene.itemAt(pos, self.transform())
            if item is None or isinstance(item, QGraphicsPixmapItem):
                self._draw_start = pos
                self._draw_rect_item = QGraphicsRectItem()
                colour = {
                    "figure":       FIGURE_COLOUR,
                    "table":        TABLE_COLOUR,
                    "caption_zone": CAPTION_COLOUR,
                    "note_zone":    NOTE_COLOUR,
                    "heading_zone": HEADING_COLOUR,
                }.get(self._new_box_type, EXCLUSION_COLOUR)
                self._draw_rect_item.setPen(QPen(colour.darker(140), 2, Qt.PenStyle.DashLine))
                self._draw_rect_item.setBrush(QBrush(colour))
                self._scene.addItem(self._draw_rect_item)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._draw_start is not None and self._draw_rect_item is not None:
            pos = self.mapToScene(event.pos())
            rect = QRectF(self._draw_start, pos).normalized()
            self._draw_rect_item.setRect(rect)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if (
            self._draw_start is not None
            and self._draw_rect_item is not None
            and event.button() == Qt.MouseButton.LeftButton
        ):
            rect = self._draw_rect_item.rect()
            self._scene.removeItem(self._draw_rect_item)
            self._draw_rect_item = None
            self._draw_start = None
            if rect.width() >= MIN_BOX_PX and rect.height() >= MIN_BOX_PX:
                self.box_created.emit(rect, self._new_box_type)
            event.accept()
            return
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
# HelpDialog
# ---------------------------------------------------------------------------

_HELP_HTML = """
<style>
  body  { font-family: sans-serif; font-size: 13px; margin: 12px; }
  h2    { margin-top: 0; }
  h3    { margin: 14px 0 4px 0; }
  table { border-collapse: collapse; width: 100%; }
  td    { padding: 6px 8px; vertical-align: top; }
  .swatch { width: 18px; height: 18px; border-radius: 3px;
            border: 1px solid #888; display: inline-block; }
  .name   { font-weight: bold; white-space: nowrap; }
  kbd   { background: #eee; border: 1px solid #bbb; border-radius: 3px;
          padding: 1px 5px; font-size: 12px; }
  hr    { border: none; border-top: 1px solid #ddd; margin: 14px 0; }
</style>

<h2>Bounding Box Editor — Quick Guide</h2>

<p>Draw boxes over the page image to tell the pipeline what each region
contains. Select a box type in the toolbar, then drag on the canvas to
create a box. Drag the box interior to move it; drag a corner handle to
resize. Right-click a box to delete it.</p>

<h3>Box types</h3>
<table>
  <tr>
    <td><span class="swatch" style="background:#dc3232;"></span></td>
    <td><span class="name">Figure (red)</span><br>
        An image region — chart, photograph, diagram, map, etc.<br>
        The region is <b>cropped</b> to a separate image file and
        <b>skipped by OCR</b> entirely. Use this for anything that
        should appear as an embedded image in the output.</td>
  </tr>
  <tr>
    <td><span class="swatch" style="background:#1eb4b4;"></span></td>
    <td><span class="name">Table (teal)</span><br>
        A tabular region. The crop is OCR'd separately using a
        table-aware engine that produces Markdown table syntax.
        The main OCR pass skips it. Use this for any grid of rows
        and columns.</td>
  </tr>
  <tr>
    <td><span class="swatch" style="background:#3264dc;"></span></td>
    <td><span class="name">Exclusion (blue)</span><br>
        Per-page boilerplate to ignore — running headers, footers,
        page numbers, watermarks. The region is <b>painted white</b>
        before OCR so its text never appears in output.<br>
        <i>Tip: if the same header/footer appears on every page,
        draw it once and use Apply Global Exclusions to copy it
        to all pages.</i></td>
  </tr>
  <tr>
    <td><span class="swatch" style="background:#a032dc;"></span></td>
    <td><span class="name">Caption Zone (purple)</span><br>
        An area containing figure or table captions. OCR lines
        inside are tagged as captions and linked to nearby figures
        in the assembled document. Draw around the caption text
        only, not the figure itself.</td>
  </tr>
  <tr>
    <td><span class="swatch" style="background:#dc8c32;"></span></td>
    <td><span class="name">Endnote Zone (orange)</span><br>
        An area containing footnotes or endnotes (typically at the
        bottom of the page). OCR lines here are collected and
        appended as a numbered Notes section at the end of the
        assembled document.</td>
  </tr>
  <tr>
    <td><span class="swatch" style="background:#32c878;"></span></td>
    <td><span class="name">Heading Zone (green)</span><br>
        An area containing headings that the OCR engine would
        otherwise classify as body text. Lines here are tagged as
        H1/H2/H3 (set the level by editing boxes.json, default H1).
        Use this when automatic heading detection misses a section
        title.</td>
  </tr>
</table>

<hr>

<h3>Keyboard shortcuts</h3>
<table>
  <tr><td><kbd>← →</kbd></td><td>Previous / next page</td></tr>
  <tr><td><kbd>Delete</kbd></td><td>Delete selected box</td></tr>
  <tr><td><kbd>Ctrl+S</kbd></td><td>Save boxes.json</td></tr>
  <tr><td><kbd>Ctrl+scroll</kbd> / pinch</td><td>Zoom in / out</td></tr>
  <tr><td>Scroll (no modifier)</td><td>Pan the page</td></tr>
</table>
"""


class HelpDialog(QDialog):
    """Non-modal help dialog explaining box types and controls."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Bounding Box Editor — Help")
        self.resize(540, 620)
        self.setWindowFlags(
            self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint
        )

        layout = QVBoxLayout(self)
        browser = QTextBrowser()
        browser.setHtml(_HELP_HTML)
        browser.setOpenLinks(False)
        layout.addWidget(browser)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)


# ---------------------------------------------------------------------------
# BoxListPanel — right-hand sidebar
# ---------------------------------------------------------------------------

class BoxListPanel(QWidget):
    delete_requested = pyqtSignal(str, str)   # box_id, box_type

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        self._fig_group = QGroupBox("Figures (red)")
        self._fig_list = QListWidget()
        self._fig_list.setMinimumHeight(24)
        fig_layout = QVBoxLayout(self._fig_group)
        fig_layout.addWidget(self._fig_list)
        layout.addWidget(self._fig_group)

        self._excl_group = QGroupBox("Exclusions — this page (blue)")
        self._excl_list = QListWidget()
        self._excl_list.setMinimumHeight(24)
        excl_layout = QVBoxLayout(self._excl_group)
        excl_layout.addWidget(self._excl_list)
        layout.addWidget(self._excl_group)

        self._table_group = QGroupBox("Tables (teal)")
        self._table_list = QListWidget()
        self._table_list.setMinimumHeight(24)
        table_layout = QVBoxLayout(self._table_group)
        table_layout.addWidget(self._table_list)
        layout.addWidget(self._table_group)

        self._caption_group = QGroupBox("Caption Zones (purple)")
        self._caption_list = QListWidget()
        self._caption_list.setMinimumHeight(24)
        caption_layout = QVBoxLayout(self._caption_group)
        caption_layout.addWidget(self._caption_list)
        layout.addWidget(self._caption_group)

        self._note_group = QGroupBox("Endnote Zones (orange)")
        self._note_list = QListWidget()
        self._note_list.setMinimumHeight(24)
        note_layout = QVBoxLayout(self._note_group)
        note_layout.addWidget(self._note_list)
        layout.addWidget(self._note_group)

        self._heading_group = QGroupBox("Heading Zones (green)")
        self._heading_list = QListWidget()
        self._heading_list.setMinimumHeight(24)
        heading_layout = QVBoxLayout(self._heading_group)
        heading_layout.addWidget(self._heading_list)
        layout.addWidget(self._heading_group)

        btn_row = QHBoxLayout()
        self._add_fig_btn   = QPushButton("+ Figure")
        self._add_excl_btn  = QPushButton("+ Exclusion")
        btn_row.addWidget(self._add_fig_btn)
        btn_row.addWidget(self._add_excl_btn)
        layout.addLayout(btn_row)
        layout.addStretch()

    def refresh(
        self,
        figures: list[dict],
        exclusions: list[dict],
        captions: list[dict] = (),
        notes: list[dict] = (),
        tables: list[dict] = (),
        headings: list[dict] = (),
    ):
        self._fig_list.clear()
        for fig in figures:
            self._fig_list.addItem(fig.get("id", "figure"))

        self._excl_list.clear()
        for ex in exclusions:
            self._excl_list.addItem(ex.get("label", "exclusion"))

        self._table_list.clear()
        for tbl in tables:
            self._table_list.addItem(tbl.get("id", "table"))

        self._caption_list.clear()
        for cap in captions:
            self._caption_list.addItem(cap.get("id", "caption"))

        self._note_list.clear()
        for note in notes:
            self._note_list.addItem(note.get("id", "endnote"))

        self._heading_list.clear()
        for hdg in headings:
            self._heading_list.addItem(hdg.get("id", "heading"))


# ---------------------------------------------------------------------------
# MainWindow
# ---------------------------------------------------------------------------

class BBoxEditor(QMainWindow):
    def __init__(self, project_dir: Path):
        super().__init__()
        self._proj = project_dir
        self._boxes: dict = {}
        self._page_records: list[dict] = []
        self._current_page_idx: int = 0
        self._box_items: list[BoxItem] = []

        self._load_data()
        self._build_ui()
        self._load_page(0)

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _load_data(self):
        pages_path = self._proj / "pages.json"
        boxes_path = self._proj / "boxes.json"

        if not pages_path.exists():
            QMessageBox.critical(self, "Error", f"pages.json not found in {self._proj}")
            sys.exit(1)
        if not boxes_path.exists():
            QMessageBox.critical(self, "Error", f"boxes.json not found in {self._proj}")
            sys.exit(1)

        self._page_records = json.loads(pages_path.read_text())
        self._boxes = json.loads(boxes_path.read_text())

        # Ensure pages dict exists with all keys
        for rec in self._page_records:
            pn = str(rec["page_number"])
            page = self._boxes.setdefault("pages", {}).setdefault(
                pn, {}
            )
            page.setdefault("figures", [])
            page.setdefault("tables", [])
            page.setdefault("exclusions", [])
            page.setdefault("captions", [])
            page.setdefault("notes", [])
            page.setdefault("headings", [])
            page.setdefault("paragraphs", [])  # populated by Surya; not shown in editor

    def _save_data(self):
        boxes_path = self._proj / "boxes.json"
        boxes_path.write_text(json.dumps(self._boxes, indent=2))
        self.statusBar().showMessage("Saved.", 3000)

    def _delete_box_item(self, item: BoxItem):
        page_str = str(self._page_records[self._current_page_idx]["page_number"])
        page_boxes = self._boxes["pages"].get(page_str, {})

        type_to_key = {
            "figure":       "figures",
            "table":        "tables",
            "exclusion":    "exclusions",
            "caption_zone": "captions",
            "note_zone":    "notes",
            "heading_zone": "headings",
        }
        if item._type in type_to_key:
            key = type_to_key[item._type]
            page_boxes[key] = [b for b in page_boxes.get(key, []) if b is not item._data]

        self._canvas._scene.removeItem(item)
        if item in self._box_items:
            self._box_items.remove(item)

        page_data = self._boxes["pages"].get(page_str, {})
        self._panel.refresh(
            page_data.get("figures", []),
            page_data.get("exclusions", []),
            page_data.get("captions", []),
            page_data.get("notes", []),
            page_data.get("tables", []),
            page_data.get("headings", []),
        )

    def _delete_selected(self):
        for item in list(self._canvas._scene.selectedItems()):
            if isinstance(item, BoxItem):
                self._delete_box_item(item)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.setWindowTitle(f"Bounding Box Editor — {self._proj.name}")
        available = QApplication.primaryScreen().availableGeometry()
        h = min(750, available.height() - 30)
        self.resize(1300, h)

        # Toolbar
        toolbar = QToolBar()
        self.addToolBar(toolbar)

        self._prev_btn = QPushButton("◀ Prev")
        self._next_btn = QPushButton("Next ▶")
        self._page_label = QLabel()
        self._save_btn = QPushButton("💾 Save")
        self._help_btn = QPushButton("?")
        self._help_btn.setToolTip("Show help — box types and keyboard shortcuts")
        self._help_btn.setFixedWidth(28)

        toolbar.addWidget(self._prev_btn)
        toolbar.addWidget(self._page_label)
        toolbar.addWidget(self._next_btn)
        toolbar.addSeparator()
        toolbar.addWidget(self._save_btn)
        toolbar.addSeparator()
        toolbar.addWidget(self._help_btn)

        # Draw-mode buttons (mutually exclusive)
        self._draw_fig_btn      = QPushButton("Draw Figure Box")
        self._draw_table_btn    = QPushButton("Draw Table Box")
        self._draw_excl_btn     = QPushButton("Draw Exclusion Box")
        self._draw_caption_btn  = QPushButton("Draw Caption Zone")
        self._draw_note_btn     = QPushButton("Draw Endnote Zone")
        self._draw_heading_btn  = QPushButton("Draw Heading Zone")
        for btn in (self._draw_fig_btn, self._draw_table_btn, self._draw_excl_btn,
                    self._draw_caption_btn, self._draw_note_btn, self._draw_heading_btn):
            btn.setCheckable(True)
        self._draw_fig_btn.setChecked(True)
        toolbar.addSeparator()
        toolbar.addWidget(self._draw_fig_btn)
        toolbar.addWidget(self._draw_table_btn)
        toolbar.addWidget(self._draw_excl_btn)
        toolbar.addWidget(self._draw_caption_btn)
        toolbar.addWidget(self._draw_note_btn)
        toolbar.addWidget(self._draw_heading_btn)

        # Central splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(splitter)

        self._canvas = PageCanvas()
        splitter.addWidget(self._canvas)

        self._panel = BoxListPanel()
        self._panel.setMinimumWidth(220)
        self._panel.setMaximumWidth(340)
        splitter.addWidget(self._panel)
        splitter.setSizes([900, 300])

        self.setStatusBar(QStatusBar())

        # Shortcuts
        QShortcut(QKeySequence("Ctrl+S"),          self, self._save_data)
        QShortcut(QKeySequence(Qt.Key.Key_Left),   self, self._prev_page)
        QShortcut(QKeySequence(Qt.Key.Key_Right),  self, self._next_page)
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self, self._delete_selected)

        self._help_dialog: HelpDialog | None = None

        # Connections
        self._prev_btn.clicked.connect(self._prev_page)
        self._next_btn.clicked.connect(self._next_page)
        self._save_btn.clicked.connect(self._save_data)
        self._help_btn.clicked.connect(self._show_help)
        self._canvas.box_created.connect(self._on_box_created)
        self._draw_fig_btn.toggled.connect(lambda on: self._set_draw_mode("figure", on))
        self._draw_table_btn.toggled.connect(lambda on: self._set_draw_mode("table", on))
        self._draw_excl_btn.toggled.connect(lambda on: self._set_draw_mode("exclusion", on))
        self._draw_caption_btn.toggled.connect(lambda on: self._set_draw_mode("caption_zone", on))
        self._draw_note_btn.toggled.connect(lambda on: self._set_draw_mode("note_zone", on))
        self._draw_heading_btn.toggled.connect(lambda on: self._set_draw_mode("heading_zone", on))
        self._panel._add_fig_btn.clicked.connect(
            lambda: self._hint("Draw a figure box by dragging on the canvas.")
        )
        self._panel._add_excl_btn.clicked.connect(self._switch_to_exclusion_draw)

    # ------------------------------------------------------------------
    # Page navigation
    # ------------------------------------------------------------------

    def _load_page(self, idx: int):
        self._current_page_idx = idx
        rec = self._page_records[idx]
        page_num = rec["page_number"]
        page_str = str(page_num)

        # Load image
        img_path = self._proj / rec["image_path"]
        pixmap = QPixmap(str(img_path))
        self._canvas.set_page_image(pixmap)

        # Load boxes
        page_boxes = self._boxes["pages"].get(page_str, {})
        figures    = page_boxes.get("figures", [])
        tables     = page_boxes.get("tables", [])
        exclusions = page_boxes.get("exclusions", [])
        captions   = page_boxes.get("captions", [])
        notes      = page_boxes.get("notes", [])
        headings   = page_boxes.get("headings", [])

        self._canvas.clear_boxes()
        self._box_items.clear()

        for fig in figures:
            item = self._canvas.add_box_item(fig, "figure", on_delete=self._delete_box_item)
            self._box_items.append(item)
        for tbl in tables:
            item = self._canvas.add_box_item(tbl, "table", on_delete=self._delete_box_item)
            self._box_items.append(item)
        for ex in exclusions:
            item = self._canvas.add_box_item(ex, "exclusion", on_delete=self._delete_box_item)
            self._box_items.append(item)
        for cap in captions:
            item = self._canvas.add_box_item(cap, "caption_zone", on_delete=self._delete_box_item)
            self._box_items.append(item)
        for note in notes:
            item = self._canvas.add_box_item(note, "note_zone", on_delete=self._delete_box_item)
            self._box_items.append(item)
        for hdg in headings:
            item = self._canvas.add_box_item(hdg, "heading_zone", on_delete=self._delete_box_item)
            self._box_items.append(item)

        self._panel.refresh(figures, exclusions, captions, notes, tables, headings)
        self._page_label.setText(f"  Page {page_num} / {len(self._page_records)}  ")
        self._prev_btn.setEnabled(idx > 0)
        self._next_btn.setEnabled(idx < len(self._page_records) - 1)

    def _prev_page(self):
        if self._current_page_idx > 0:
            self._load_page(self._current_page_idx - 1)

    def _next_page(self):
        if self._current_page_idx < len(self._page_records) - 1:
            self._load_page(self._current_page_idx + 1)

    # ------------------------------------------------------------------
    # Box creation
    # ------------------------------------------------------------------

    def _on_box_created(self, rect: QRectF, box_type: str):
        rec = self._page_records[self._current_page_idx]
        page_str = str(rec["page_number"])
        pw = self._canvas._page_w
        ph = self._canvas._page_h

        new_box = {
            "x": round(rect.x() / pw, 4),
            "y": round(rect.y() / ph, 4),
            "w": round(rect.width() / pw, 4),
            "h": round(rect.height() / ph, 4),
            "label": box_type,
        }

        page_boxes = self._boxes["pages"].setdefault(page_str, {})
        for key in ("figures", "tables", "exclusions", "captions", "notes", "headings"):
            page_boxes.setdefault(key, [])

        if box_type == "figure":
            idx = len(page_boxes["figures"])
            new_box["id"] = f"fig_{page_str}_{idx + 1}"
            new_box["alt_text"] = ""
            page_boxes["figures"].append(new_box)
        elif box_type == "table":
            idx = len(page_boxes["tables"])
            new_box["id"] = f"table_{page_str}_{idx + 1}"
            page_boxes["tables"].append(new_box)
        elif box_type == "caption_zone":
            idx = len(page_boxes["captions"])
            new_box["id"] = f"cap_{page_str}_{idx + 1}"
            page_boxes["captions"].append(new_box)
        elif box_type == "note_zone":
            idx = len(page_boxes["notes"])
            new_box["id"] = f"note_{page_str}_{idx + 1}"
            page_boxes["notes"].append(new_box)
        elif box_type == "heading_zone":
            idx = len(page_boxes["headings"])
            new_box["id"] = f"hdg_{page_str}_{idx + 1}"
            new_box["level"] = 1
            page_boxes["headings"].append(new_box)
        else:  # exclusion
            page_boxes["exclusions"].append(new_box)

        # Add visual item
        item = self._canvas.add_box_item(new_box, box_type, on_delete=self._delete_box_item)
        self._box_items.append(item)
        self._panel.refresh(
            page_boxes["figures"],
            page_boxes["exclusions"],
            page_boxes["captions"],
            page_boxes["notes"],
            page_boxes["tables"],
            page_boxes["headings"],
        )

    def _set_draw_mode(self, mode: str, checked: bool):
        if not checked:
            return
        mode_map = {
            "figure":       self._draw_fig_btn,
            "table":        self._draw_table_btn,
            "exclusion":    self._draw_excl_btn,
            "caption_zone": self._draw_caption_btn,
            "note_zone":    self._draw_note_btn,
            "heading_zone": self._draw_heading_btn,
        }
        for m, btn in mode_map.items():
            if m != mode:
                btn.setChecked(False)
        self._canvas.set_draw_type(mode)

    def _switch_to_exclusion_draw(self):
        self._draw_excl_btn.setChecked(True)
        self._hint("Draw an exclusion box by dragging on the canvas.")

    def _hint(self, msg: str):
        self.statusBar().showMessage(msg, 4000)

    def _show_help(self):
        if self._help_dialog is None:
            self._help_dialog = HelpDialog(self)
        self._help_dialog.show()
        self._help_dialog.raise_()
        self._help_dialog.activateWindow()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(
        prog="bbox_editor.py",
        description=(
            "Bounding Box Editor — review and correct figure, table, and exclusion\n"
            "zone annotations in boxes.json before running OCR.\n\n"
            "Box types:\n"
            "  Red    — Figure       image region to crop and exclude from OCR\n"
            "  Teal   — Table        table region; cropped and OCR'd separately\n"
            "  Blue   — Exclusion    per-page boilerplate (headers, footers, page numbers)\n"
            "  Purple — Caption zone OCR lines here are tagged as captions\n"
            "  Orange — Endnote zone  OCR lines here are tagged as endnotes\n"
            "  Green  — Heading zone OCR lines here are tagged as headings (level 1)\n\n"
            "Keyboard shortcuts:\n"
            "  ← →       previous / next page\n"
            "  Delete    delete selected box\n"
            "  Ctrl+S    save boxes.json\n"
            "  Ctrl+scroll / pinch   zoom"
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
    app.setApplicationName("BBox Editor")
    window = BBoxEditor(project_dir)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
