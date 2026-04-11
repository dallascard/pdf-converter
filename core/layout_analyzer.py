"""
layout_analyzer.py — Automatically detect figure regions and exclusion zones.

Uses Surya's layout detection model to identify:
- figure regions  (photographs, diagrams, charts)
- table regions
- candidate exclusion zones (running headers, footers, page numbers)
- caption zones, footnote zones, heading zones, paragraph zones

Results are written to *project_dir/boxes.json* in a format that the
bounding-box editor GUI can load and that figure_extractor.py consumes.

boxes.json schema
-----------------
.. code-block:: json

    {
        "pages": {
            "1": {
                "figures": [
                    {"id": "fig_1_1", "x": 0.1, "y": 0.3, "w": 0.8, "h": 0.4,
                     "alt_text": ""}
                ],
                "exclusions": [
                    {"label": "page_number", "x": 0.4, "y": 0.93,
                     "w": 0.2, "h": 0.05}
                ]
            }
        }
    }

All coordinates are fractions of page width/height (0.0–1.0).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_layout(
    project_dir: Path,
    page_records: list[dict],
    force: bool = False,
) -> dict:
    """
    Run Surya layout analysis on all pages and write *project_dir/boxes.json*.

    If *boxes.json* already exists and *force* is False, the existing file
    is loaded and returned without re-running the analysis.

    Returns the boxes dict.
    """
    boxes_path = project_dir / "boxes.json"

    if boxes_path.exists() and not force:
        logger.info("boxes.json already exists; skipping layout analysis.")
        return json.loads(boxes_path.read_text())

    raw_pages = _analyze_all_pages_surya(project_dir, page_records)

    boxes = {
        "pages": {
            pn: {
                "figures": [
                    _make_region_id("fig", pn, i, fig)
                    for i, fig in enumerate(data.get("figures", []))
                ],
                "tables": [
                    _make_region_id("table", pn, i, tbl)
                    for i, tbl in enumerate(data.get("tables", []))
                ],
                "exclusions": data.get("exclusions", []),
                "captions": data.get("captions", []),
                "notes": data.get("notes", []),
                "headings": data.get("headings", []),
                "paragraphs": data.get("paragraphs", []),
            }
            for pn, data in raw_pages.items()
        },
    }

    boxes_path.write_text(json.dumps(boxes, indent=2))
    logger.info("Layout analysis complete. Results written to boxes.json.")
    return boxes


def load_boxes(project_dir: Path) -> dict:
    """Load existing boxes.json, raising if absent."""
    boxes_path = project_dir / "boxes.json"
    if not boxes_path.exists():
        raise FileNotFoundError(
            f"No boxes.json in {project_dir}. Run the 'analyze' step first."
        )
    return json.loads(boxes_path.read_text())


def save_boxes(project_dir: Path, boxes: dict) -> None:
    """Persist (possibly GUI-edited) boxes dict back to boxes.json."""
    (project_dir / "boxes.json").write_text(json.dumps(boxes, indent=2))


def ensure_paragraph_boxes(project_dir: Path, page_records: list[dict]) -> dict:
    """
    Ensure every page in boxes.json has Surya paragraph boxes.

    If paragraph boxes are already present on at least one page, the existing
    boxes.json is returned as-is.  Otherwise Surya layout detection is run
    on all pages, the paragraph boxes are added to boxes.json, and the
    updated dict is returned.

    This is called automatically by run_ocr() when engine=="surya" so that
    structure.py can use paragraph boxes for paragraph assembly even if the
    user ran layout analysis with a different engine (or skipped it).
    """
    boxes_path = project_dir / "boxes.json"
    if not boxes_path.exists():
        return {}

    boxes = json.loads(boxes_path.read_text())
    pages = boxes.get("pages", {})

    # Already populated — nothing to do
    if any(page.get("paragraphs") for page in pages.values()):
        return boxes

    logger.info(
        "No paragraph boxes in boxes.json; running Surya layout to detect them …"
    )
    raw_pages = _analyze_all_pages_surya(project_dir, page_records)

    for pn, data in raw_pages.items():
        pages.setdefault(pn, {})["paragraphs"] = data.get("paragraphs", [])

    boxes_path.write_text(json.dumps(boxes, indent=2))
    total = sum(len(p.get("paragraphs", [])) for p in pages.values())
    logger.info("Paragraph boxes added to boxes.json (%d total).", total)
    return boxes


# ---------------------------------------------------------------------------
# Surya layout engine
# ---------------------------------------------------------------------------

# Surya label → our box category  (labels from surya/layout/label.py LAYOUT_PRED_RELABEL)
_SURYA_LABEL_MAP = {
    "Picture":         "figure",
    "Figure":          "figure",
    "Table":           "table",
    "Caption":         "caption",
    "Footnote":        "note",
    "PageHeader":      "exclusion",
    "PageFooter":      "exclusion",
    "TableOfContents": "exclusion",
    "SectionHeader":   "heading",
    "Text":            "paragraph",
    "ListItem":        "paragraph",
    # Equation, Code, Form — ignored; OCR handles them
}


def _analyze_all_pages_surya(project_dir: Path, page_records: list[dict]) -> dict[str, dict]:
    """Run Surya layout detection on all pages and return raw_pages dict."""
    try:
        from surya.foundation import FoundationPredictor
        from surya.layout import LayoutPredictor
        from surya.settings import settings as surya_settings
    except ImportError:
        raise RuntimeError(
            "surya-ocr is not installed. Run: uv sync --extra surya"
        )

    logger.info("Loading Surya layout model …")
    layout_predictor = LayoutPredictor(
        FoundationPredictor(checkpoint=surya_settings.LAYOUT_MODEL_CHECKPOINT)
    )

    images = []
    page_nums = []
    for rec in page_records:
        img_path = project_dir / rec["image_path"]
        images.append(Image.open(img_path).convert("RGB"))
        page_nums.append(str(rec["page_number"]))

    logger.info("  Running Surya layout detection on %d pages …", len(images))
    predictions = layout_predictor(images)

    raw_pages: dict[str, dict] = {}
    for page_num, img, pred in zip(page_nums, images, predictions):
        W, H = img.size
        result: dict[str, list] = {
            "figures": [], "tables": [], "exclusions": [],
            "captions": [], "notes": [], "headings": [], "paragraphs": [],
        }
        for bbox_obj in pred.bboxes:
            label = bbox_obj.label
            category = _SURYA_LABEL_MAP.get(label)
            if category is None:
                continue
            x1, y1, x2, y2 = bbox_obj.bbox
            box = _clamp_box({
                "x": x1 / W,
                "y": y1 / H,
                "w": (x2 - x1) / W,
                "h": (y2 - y1) / H,
                "label": label.lower(),
            })
            # Pad figures and tables slightly so they're easier to review/adjust
            if category in ("figure", "table"):
                box = _pad_box(box, pad=0.005)
            # Surya can't determine heading level — default to 1
            if category == "heading":
                box["level"] = 1
            if _valid_box(box):
                result[category + "s"].append(box)
        raw_pages[page_num] = result
        logger.info("  Page %s: %d figures, %d tables, %d exclusions, %d paragraphs",
                    page_num,
                    len(result["figures"]),
                    len(result["tables"]),
                    len(result["exclusions"]),
                    len(result["paragraphs"]))

    return raw_pages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pad_box(b: dict, pad: float = 0.005) -> dict:
    """Expand a box outward by `pad` (fraction of page) on each side."""
    return _clamp_box({
        **b,
        "x": b["x"] - pad,
        "y": b["y"] - pad,
        "w": b["w"] + 2 * pad,
        "h": b["h"] + 2 * pad,
    })


def _valid_box(b: dict) -> bool:
    return all(k in b for k in ("x", "y", "w", "h")) and b["w"] > 0 and b["h"] > 0


def _clamp_box(b: dict) -> dict:
    """Clamp all coordinates to [0, 1]."""
    b = dict(b)
    b["x"] = max(0.0, min(1.0, float(b["x"])))
    b["y"] = max(0.0, min(1.0, float(b["y"])))
    b["w"] = max(0.0, min(1.0 - b["x"], float(b["w"])))
    b["h"] = max(0.0, min(1.0 - b["y"], float(b["h"])))
    return b


def _make_region_id(prefix: str, page_num: str, idx: int, box: dict) -> dict:
    """Add a stable ID to a region box dict."""
    result = {"id": f"{prefix}_{page_num}_{idx + 1}", **box}
    if prefix == "fig":
        result.setdefault("alt_text", "")
    return result
