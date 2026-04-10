"""
layout_analyzer.py — Automatically detect figure regions and exclusion zones.

Two-stage strategy
------------------
1. **Claude Vision pass**: send each page image to Claude and ask it to
   return bounding boxes (as percentages) for:
   - figure regions  (photographs, diagrams, charts, tables-as-images, etc.)
   - candidate exclusion zones (running headers, footers, page numbers)

2. **Rule-based boilerplate consolidation**: after all pages are analysed,
   compare candidate exclusion zones across pages.  Zones that appear at a
   consistent relative position on ≥ BOILERPLATE_MIN_PAGES pages are
   promoted to *confirmed* exclusion zones and optionally applied globally.

Results are written to *project_dir/boxes.json* in a format that the
bounding-box editor GUI can load and that figure_extractor.py consumes.

boxes.json schema
-----------------
.. code-block:: json

    {
        "global_exclusions": [
            {"label": "header", "x": 0.0, "y": 0.0, "w": 1.0, "h": 0.08,
             "apply_to": "all"}
        ],
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

import base64
import json
import logging
import re
from pathlib import Path
import anthropic
from PIL import Image

import config
from core.claude_client import get_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt sent to Claude for each page
# ---------------------------------------------------------------------------

_LAYOUT_PROMPT = """\
You are analyzing a scanned document page image to detect its layout.

Return a JSON object with exactly these four keys:

"figures": a list of non-text visual regions (photographs, drawings, diagrams,
  charts, maps, decorative rules wider than the text column, etc.).
  Do NOT include tables or purely-text regions here.

"tables": a list of regions containing tabular data (grids of rows and columns,
  whether ruled or unruled).  Include text-based tables here even if they have
  no visible borders.

"exclusions": a list of boilerplate text regions to strip before OCR
  (running headers, running footers, page numbers, watermarks).

"captions": a list of regions containing figure or table captions (short
  descriptive text immediately adjacent to a figure or table).

Each item must have:
  "x"  – left edge as fraction of page width   (0.0 = left,  1.0 = right)
  "y"  – top  edge as fraction of page height  (0.0 = top,   1.0 = bottom)
  "w"  – width  as fraction of page width
  "h"  – height as fraction of page height
  "label" – short description (e.g. "photograph", "data table", "header", "fig caption")

Return ONLY the raw JSON object, no markdown fences, no explanation.
Use an empty list for any key that has no matches.
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_layout(
    project_dir: Path,
    page_records: list[dict],
    engine: str = "claude",
    force: bool = False,
) -> dict:
    """
    Run layout analysis on all pages and write *project_dir/boxes.json*.

    *engine* is ``"claude"`` (default) or ``"surya"``.
    If *boxes.json* already exists and *force* is False, the existing file
    is loaded and returned without re-running the analysis.

    Returns the boxes dict.
    """
    boxes_path = project_dir / "boxes.json"

    if boxes_path.exists() and not force:
        logger.info("boxes.json already exists; skipping layout analysis.")
        return json.loads(boxes_path.read_text())

    if engine == "surya":
        raw_pages = _analyze_all_pages_surya(project_dir, page_records)
    else:
        client = get_client()
        raw_pages = {}
        for rec in page_records:
            page_num = str(rec["page_number"])
            img_path = project_dir / rec["image_path"]
            logger.info("  Analyzing layout of page %s (claude) …", page_num)
            raw_pages[page_num] = _analyze_page_claude(client, img_path)

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
# Per-page analysis
# ---------------------------------------------------------------------------

def _analyze_page_claude(client: anthropic.Anthropic, img_path: Path) -> dict:
    """Call Claude Vision on one page image; return parsed layout regions."""
    img_b64 = _encode_image(img_path)

    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=2048,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": img_b64,
                            },
                        },
                        {"type": "text", "text": _LAYOUT_PROMPT},
                    ],
                }
            ],
        )
        raw = response.content[0].text.strip()
        return _parse_layout_response(raw)
    except Exception as exc:
        logger.warning("Layout analysis failed for %s: %s", img_path.name, exc)
        return {"figures": [], "tables": [], "exclusions": [], "captions": []}


def _parse_layout_response(raw: str) -> dict:
    """Parse Claude's JSON response, tolerating minor formatting issues."""
    raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"```$", "", raw, flags=re.MULTILINE)
    raw = raw.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Could not parse layout JSON: %r", raw[:200])
        return {"figures": [], "tables": [], "exclusions": [], "captions": [], "headings": []}

    def _heading(b):
        box = _clamp_box(b)
        box["level"] = int(b.get("level", 1)) if str(b.get("level", 1)).isdigit() else 1
        return box

    return {
        "figures":    [_clamp_box(b) for b in data.get("figures", [])    if _valid_box(b)],
        "tables":     [_clamp_box(b) for b in data.get("tables", [])     if _valid_box(b)],
        "exclusions": [_clamp_box(b) for b in data.get("exclusions", []) if _valid_box(b)],
        "captions":   [_clamp_box(b) for b in data.get("captions", [])   if _valid_box(b)],
        "headings":   [_heading(b)   for b in data.get("headings", [])   if _valid_box(b)],
    }


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
        from PIL import Image as PILImage
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
        images.append(PILImage.open(img_path).convert("RGB"))
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
            # .bbox is a computed property: [x1, y1, x2, y2] in pixel coords
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
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _encode_image(img_path: Path) -> str:
    """Return base64-encoded image bytes."""
    return base64.standard_b64encode(img_path.read_bytes()).decode("utf-8")


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
