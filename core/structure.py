"""
structure.py — Convert flat OCR line lists into a structured document model.

The document model is a list of *elements* written to
*project_dir/structure.json*:

.. code-block:: json

    [
        {"kind": "heading", "level": 1, "text": "Chapter 1: Introduction",
         "page": 1, "line_id": "p1_l001"},

        {"kind": "paragraph", "text": "Full paragraph text...",
         "page": 1, "line_ids": ["p1_l002", "p1_l003"]},

        {"kind": "figure", "id": "fig_1_1", "page": 1,
         "crop_path": "images/fig_1_1.png", "alt_text": ""},

        {"kind": "caption", "text": "Fig. 1. A caption.",
         "page": 1, "line_id": "p1_l004", "figure_id": "fig_1_1"},

        {"kind": "footnote", "number": 1, "text": "Footnote text.",
         "page": 1, "line_ids": ["p1_l010"]},
    ]

Processing steps
----------------
1. Merge continuation lines within a paragraph.
2. Re-merge heading lines that were split across two OCR lines.
3. Associate captions with figures (nearest figure on the same or adjacent page).
4. Collect footnotes and prepare them for conversion to endnotes.
5. Insert figure elements at the correct position in the flow.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

HEADING_TYPES = {"heading1", "heading2", "heading3"}
HEADING_LEVEL = {"heading1": 1, "heading2": 2, "heading3": 3}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_structure(
    project_dir: Path,
    ocr_results: list[dict],
    figure_records: list[dict],
    table_records: list[dict] | None = None,
    force: bool = False,
) -> list[dict]:
    """
    Build the document structure from OCR results and figure metadata.

    Writes *project_dir/structure.json* and returns the element list.
    """
    struct_path = project_dir / "structure.json"
    if struct_path.exists() and not force:
        logger.info("structure.json exists; skipping structure step.")
        return json.loads(struct_path.read_text())

    # Load paragraph boxes from boxes.json (populated by Surya layout analysis)
    boxes: dict = {}
    boxes_path = project_dir / "boxes.json"
    if boxes_path.exists():
        boxes = json.loads(boxes_path.read_text())

    # Index figures and tables by page for quick lookup
    figs_by_page: dict[int, list[dict]] = {}
    for fig in figure_records:
        figs_by_page.setdefault(fig["page_number"], []).append(fig)

    tbls_by_page: dict[int, list[dict]] = {}
    for tbl in (table_records or []):
        tbls_by_page.setdefault(tbl["page_number"], []).append(tbl)

    elements: list[dict] = []

    for page_result in ocr_results:
        page_num = page_result["page_number"]
        lines = page_result.get("lines", [])
        page_boxes = boxes.get("pages", {}).get(str(page_num), {})
        paragraph_boxes = page_boxes.get("paragraphs") or None

        page_elements = _process_page(lines, page_num, paragraph_boxes)

        # Insert figures at the correct position in the flow based on y coordinate.
        # _process_page tags each element with a temporary "_y" field; figures
        # carry their y from the box record.
        # Insert figures and tables at correct y positions
        insertions = []
        for fig in figs_by_page.get(page_num, []):
            insertions.append({
                "kind": "figure",
                "id": fig["id"],
                "page": page_num,
                "crop_path": fig["crop_path"],
                "alt_text": fig.get("alt_text", ""),
                "_y": fig["box"].get("y", 0),
            })
        for tbl in tbls_by_page.get(page_num, []):
            insertions.append({
                "kind": "table",
                "id": tbl["id"],
                "page": page_num,
                "crop_path": tbl["crop_path"],
                "content": tbl.get("content", ""),
                "content_format": tbl.get("content_format", "preformatted"),
                "_y": tbl["box"].get("y", 0),
            })

        for el in insertions:
            insert_y = el["_y"]
            insert_at = len(page_elements)
            for i, pe in enumerate(page_elements):
                if pe.get("_y", 0) > insert_y:
                    insert_at = i
                    break
            page_elements.insert(insert_at, el)

        # Strip the temporary positioning field before storing
        for el in page_elements:
            el.pop("_y", None)

        elements.extend(page_elements)

    # Associate captions with nearby figures
    elements = _associate_captions(elements)

    struct_path.write_text(json.dumps(elements, indent=2))
    logger.info("Structure built: %d elements.", len(elements))
    return elements


def load_structure(project_dir: Path) -> list[dict]:
    path = project_dir / "structure.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No structure.json in {project_dir}. Run the 'structure' step first."
        )
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Per-page processing
# ---------------------------------------------------------------------------

def _process_page(
    lines: list[dict],
    page_num: int,
    paragraph_boxes: list[dict] | None = None,
) -> list[dict]:
    """Convert a flat list of OCR lines into structured page elements.

    *paragraph_boxes* — optional list of paragraph region boxes (from Surya
    layout analysis).  When supplied, consecutive body lines that fall within
    the same paragraph box are merged into one paragraph element.  Lines not
    matched to any box are grouped by vertical gap (Option A fallback).
    When *paragraph_boxes* is None, vertical gap detection is used for all
    body lines.

    Each element is tagged with a temporary ``_y`` field (the y coordinate of
    its first line) so that ``build_structure`` can interleave figures at the
    correct position in the flow.  The caller strips ``_y`` before storing.
    """
    elements: list[dict] = []
    para_lines: list[dict] = []       # accumulate body lines into paragraphs
    caption_lines: list[dict] = []    # accumulate caption lines into one element
    footnote_lines: list[dict] = []   # accumulate footnote lines

    def _y(line_list: list[dict]) -> float:
        return line_list[0].get("bbox", {}).get("y", 0) if line_list else 0

    def flush_paragraph():
        if not para_lines:
            return
        text = _join_lines(para_lines)
        elements.append({
            "kind": "paragraph",
            "text": text,
            "page": page_num,
            "line_ids": [l["line_id"] for l in para_lines],
            "_y": _y(para_lines),
        })
        para_lines.clear()

    def flush_caption():
        if not caption_lines:
            return
        text = _join_lines(caption_lines)
        elements.append({
            "kind": "caption",
            "text": text,
            "page": page_num,
            "line_ids": [l["line_id"] for l in caption_lines],
            "figure_id": None,  # filled in by _associate_captions
            "_y": _y(caption_lines),
        })
        caption_lines.clear()

    def flush_footnotes():
        if not footnote_lines:
            return
        for fn in _split_footnotes(footnote_lines, page_num):
            fn["_y"] = _y(footnote_lines)
            elements.append(fn)
        footnote_lines.clear()

    for line in lines:
        ltype = line.get("type", "body")
        text = line.get("text", "").strip()
        if not text:
            continue

        if ltype in HEADING_TYPES:
            flush_paragraph()
            flush_caption()
            flush_footnotes()
            elements.append({
                "kind": "heading",
                "level": HEADING_LEVEL[ltype],
                "text": text,
                "page": page_num,
                "line_id": line["line_id"],
                "_y": line.get("bbox", {}).get("y", 0),
            })

        elif ltype == "caption":
            flush_paragraph()
            flush_footnotes()
            caption_lines.append(line)

        elif ltype == "footnote":
            flush_paragraph()
            flush_caption()
            footnote_lines.append(line)

        else:  # body / other
            flush_caption()
            if para_lines and not _continues_paragraph(
                para_lines[-1], line, paragraph_boxes
            ):
                flush_paragraph()
            para_lines.append(line)

    flush_paragraph()
    flush_caption()
    flush_footnotes()
    return elements


# ---------------------------------------------------------------------------
# Paragraph boundary detection
# ---------------------------------------------------------------------------

def _find_paragraph_box_idx(line: dict, paragraph_boxes: list[dict]) -> int | None:
    """Return the index of the paragraph box whose area contains this line's centre."""
    bbox = line.get("bbox", {})
    cx = bbox.get("x", 0) + bbox.get("w", 0) / 2
    cy = bbox.get("y", 0) + bbox.get("h", 0) / 2
    for i, box in enumerate(paragraph_boxes):
        if (box["x"] <= cx <= box["x"] + box["w"]
                and box["y"] <= cy <= box["y"] + box["h"]):
            return i
    return None


def _gap_continues(prev: dict, curr: dict) -> bool:
    """Return True if the vertical gap between two lines is within one line-height.

    A gap larger than 1.5 × the previous line's height is treated as a
    paragraph break.
    """
    prev_bbox = prev.get("bbox", {})
    curr_bbox = curr.get("bbox", {})
    prev_y = prev_bbox.get("y", 0)
    prev_h = prev_bbox.get("h", 0.02)
    curr_y = curr_bbox.get("y", 0)
    gap = curr_y - (prev_y + prev_h)
    return gap <= prev_h * 1.5


def _continues_paragraph(
    prev: dict,
    curr: dict,
    paragraph_boxes: list[dict] | None,
) -> bool:
    """Return True if *curr* continues the same paragraph as *prev*.

    With paragraph boxes (Surya layout):
    - Both in the same box → continues.
    - Both unmatched → fall back to gap detection.
    - One matched, one not → paragraph break.

    Without paragraph boxes → gap detection only.
    """
    if paragraph_boxes:
        prev_idx = _find_paragraph_box_idx(prev, paragraph_boxes)
        curr_idx = _find_paragraph_box_idx(curr, paragraph_boxes)
        if prev_idx is not None and curr_idx is not None:
            return prev_idx == curr_idx
        if prev_idx is None and curr_idx is None:
            return _gap_continues(prev, curr)
        return False  # one matched, one not → break
    return _gap_continues(prev, curr)


# ---------------------------------------------------------------------------
# Line joining
# ---------------------------------------------------------------------------

def _join_lines(lines: list[dict]) -> str:
    """
    Join OCR lines into a single string, handling soft hyphens.

    A line ending with '-' is joined to the next without a space (assuming
    the hyphen is a line-break hyphen).  Otherwise lines are joined with a
    single space.
    """
    parts = [l["text"].strip() for l in lines]
    result = ""
    for i, part in enumerate(parts):
        if i == 0:
            result = part
        elif result.endswith("-"):
            result = result[:-1] + part  # de-hyphenate
        else:
            result = result + " " + part
    return result


# ---------------------------------------------------------------------------
# Footnote splitting
# ---------------------------------------------------------------------------

_FOOTNOTE_START = re.compile(r"^(\d+|[*†‡§¶])\s*")


def _split_footnotes(footnote_lines: list[dict], page_num: int) -> list[dict]:
    """
    Group footnote lines into individual footnote elements.

    A new footnote begins when a line starts with a digit or a recognised
    footnote symbol.
    """
    footnotes: list[dict] = []
    current_lines: list[dict] = []
    current_marker: str | None = None

    for line in footnote_lines:
        m = _FOOTNOTE_START.match(line["text"])
        if m:
            if current_lines:
                footnotes.append(_make_footnote(current_marker, current_lines, page_num))
            current_marker = m.group(1)
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        footnotes.append(_make_footnote(current_marker, current_lines, page_num))

    return footnotes


def _make_footnote(marker: str | None, lines: list[dict], page_num: int) -> dict:
    text = _join_lines(lines)
    # Strip the leading marker from the text
    if marker:
        text = _FOOTNOTE_START.sub("", text, count=1).strip()
    return {
        "kind": "footnote",
        "marker": marker,
        "text": text,
        "page": page_num,
        "line_ids": [l["line_id"] for l in lines],
    }


# ---------------------------------------------------------------------------
# Caption–figure association
# ---------------------------------------------------------------------------

_CAPTION_PATTERN = re.compile(
    r"^\s*(fig(?:ure)?|plate|photo|image|table|chart|graph|diagram)[\s.]*(\d+)",
    re.IGNORECASE,
)


def _associate_captions(elements: list[dict]) -> list[dict]:
    """
    For each caption element, find the nearest figure on the same page and
    link them.  Move the caption immediately after its figure in the element
    list.
    """
    # Build index of figure positions
    fig_positions: dict[str, int] = {}
    for i, el in enumerate(elements):
        if el["kind"] == "figure":
            fig_positions[el["id"]] = i

    result = list(elements)

    for i, el in enumerate(result):
        if el["kind"] != "caption" or el.get("figure_id"):
            continue

        page = el["page"]
        best_fig_id = _find_nearest_figure(result, i, page, fig_positions)
        if best_fig_id:
            el["figure_id"] = best_fig_id

    # Reorder: move each caption to immediately after its figure
    result = _reorder_captions(result)
    return result


def _find_nearest_figure(
    elements: list[dict],
    caption_idx: int,
    page: int,
    fig_positions: dict[str, int],
) -> str | None:
    """
    Find the figure ID closest (by element list distance) to the caption,
    searching on the same page first, then adjacent pages.
    """
    best_id = None
    best_dist = float("inf")

    for fig_id, fig_idx in fig_positions.items():
        fig_el = elements[fig_idx]
        if abs(fig_el["page"] - page) > 1:
            continue
        dist = abs(fig_idx - caption_idx) + abs(fig_el["page"] - page) * 1000
        if dist < best_dist:
            best_dist = dist
            best_id = fig_id

    return best_id


def _reorder_captions(elements: list[dict]) -> list[dict]:
    """Move captions to immediately after their associated figure."""
    # Collect caption indices keyed by figure_id
    caption_map: dict[str, list[int]] = {}
    for i, el in enumerate(elements):
        if el["kind"] == "caption" and el.get("figure_id"):
            caption_map.setdefault(el["figure_id"], []).append(i)

    if not caption_map:
        return elements

    # Remove captions from their current positions, insert after figure
    to_remove = {i for idxs in caption_map.values() for i in idxs}
    new_elements: list[dict] = []

    for i, el in enumerate(elements):
        if i in to_remove:
            continue
        new_elements.append(el)
        if el["kind"] == "figure" and el["id"] in caption_map:
            for cap_idx in caption_map[el["id"]]:
                new_elements.append(elements[cap_idx])

    return new_elements
