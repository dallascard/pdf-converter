"""
figure_extractor.py — Crop figure regions and produce masked page images.

Two outputs per page
--------------------
1. **Figure crops**: each figure bounding box is cropped from the original
   page image and saved as *project_dir/images/fig_<page>_<n>.png*.

2. **Masked page images**: a copy of the page where every figure region AND
   every exclusion zone (both per-page and global) is painted with
   MASK_COLOUR.  These masked images are what gets sent to the OCR step, so
   Claude never sees figure interiors or boilerplate.

Both sets of paths are recorded in *project_dir/figures.json*:

.. code-block:: json

    [
        {
            "id": "fig_1_1",
            "page_number": 1,
            "crop_path": "images/fig_1_1.png",
            "alt_text": "",
            "box": {"x": 0.1, "y": 0.3, "w": 0.8, "h": 0.4}
        }
    ]

Masked page images are written to *project_dir/pages_masked/* and their
paths are added to the page manifest as ``"masked_image_path"``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from PIL import Image, ImageDraw

import config
from core.layout_analyzer import load_boxes

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_figures(
    project_dir: Path,
    page_records: list[dict],
    force: bool = False,
) -> tuple[list[dict], list[dict]]:
    """
    Crop figure and table regions and write masked page images.

    Returns ``(figure_records, table_records)``.
    Both are also written to ``figures.json`` and ``tables.json``.
    """
    figures_path = project_dir / "figures.json"
    tables_path  = project_dir / "tables.json"
    masked_dir   = project_dir / "pages_masked"
    images_dir   = project_dir / "images"

    if figures_path.exists() and tables_path.exists() and not force:
        logger.info("figures.json and tables.json exist; skipping figure extraction.")
        return (
            json.loads(figures_path.read_text()),
            json.loads(tables_path.read_text()),
        )

    boxes = load_boxes(project_dir)

    masked_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    figure_records: list[dict] = []
    table_records:  list[dict] = []

    for rec in page_records:
        page_num = rec["page_number"]
        page_str = str(page_num)
        img_path = project_dir / rec["image_path"]
        img = Image.open(img_path).convert("RGB")
        W, H = img.size

        page_boxes = boxes.get("pages", {}).get(page_str, {})
        figures              = page_boxes.get("figures", [])
        tables               = page_boxes.get("tables", [])
        per_page_exclusions  = page_boxes.get("exclusions", [])
        global_exclusions    = boxes.get("global_exclusions", [])
        captions             = page_boxes.get("captions", [])
        notes                = page_boxes.get("notes", [])

        # --- Crop figures ---------------------------------------------------
        for fig in figures:
            fig_id = fig.get("id", f"fig_{page_num}_{len(figure_records)+1}")
            crop = _crop_box(img, fig, W, H)
            crop_filename = f"{fig_id}.png"
            crop.save(images_dir / crop_filename, format="PNG")
            figure_records.append({
                "id": fig_id,
                "page_number": page_num,
                "crop_path": str(Path("images") / crop_filename),
                "alt_text": fig.get("alt_text", ""),
                "box": {k: fig[k] for k in ("x", "y", "w", "h")},
            })
            logger.debug("  Cropped figure %s", fig_id)

        # --- Crop tables ----------------------------------------------------
        for tbl in tables:
            tbl_id = tbl.get("id", f"table_{page_num}_{len(table_records)+1}")
            crop = _crop_box(img, tbl, W, H)
            crop_filename = f"{tbl_id}.png"
            crop.save(images_dir / crop_filename, format="PNG")
            table_records.append({
                "id": tbl_id,
                "page_number": page_num,
                "crop_path": str(Path("images") / crop_filename),
                "box": {k: tbl[k] for k in ("x", "y", "w", "h")},
                "content": "",          # filled in by the ocr step
                "content_format": "",   # "markdown" | "preformatted"
            })
            logger.debug("  Cropped table %s", tbl_id)

        # --- Build masked page image ----------------------------------------
        # Mask figures, tables, exclusions, captions, and notes before OCR.
        # Caption and note zones are OCR'd separately in the ocr step so their
        # text isn't merged with the main body text by the OCR engine.
        masked = img.copy()
        draw = ImageDraw.Draw(masked)
        zones_to_mask = figures + tables + per_page_exclusions + global_exclusions + captions + notes
        for zone in zones_to_mask:
            _paint_box(draw, zone, W, H, colour=config.MASK_COLOUR)

        masked_filename = f"masked_{page_num:04d}.png"
        masked.save(masked_dir / masked_filename, format="PNG")
        rec["masked_image_path"] = str(Path("pages_masked") / masked_filename)
        logger.debug("  Masked page %d (%d zones)", page_num, len(zones_to_mask))

    # Persist updated page records (with masked_image_path added)
    (project_dir / "pages.json").write_text(json.dumps(page_records, indent=2))

    figures_path.write_text(json.dumps(figure_records, indent=2))
    tables_path.write_text(json.dumps(table_records, indent=2))
    logger.info("Extracted %d figures, %d tables.", len(figure_records), len(table_records))
    return figure_records, table_records


def load_figures(project_dir: Path) -> list[dict]:
    path = project_dir / "figures.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No figures.json in {project_dir}. Run the 'extract' step first."
        )
    return json.loads(path.read_text())


def load_tables(project_dir: Path) -> list[dict]:
    path = project_dir / "tables.json"
    if not path.exists():
        return []   # tables.json is optional — documents may have none
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Pillow helpers
# ---------------------------------------------------------------------------

def _crop_box(img: Image.Image, box: dict, W: int, H: int) -> Image.Image:
    """Crop a fractional bounding box from *img*."""
    x0 = int(box["x"] * W)
    y0 = int(box["y"] * H)
    x1 = int((box["x"] + box["w"]) * W)
    y1 = int((box["y"] + box["h"]) * H)
    # Clamp
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(W, x1), min(H, y1)
    return img.crop((x0, y0, x1, y1))


def _paint_box(
    draw: ImageDraw.ImageDraw, box: dict, W: int, H: int, colour: tuple
) -> None:
    """Paint a filled rectangle over a fractional bounding box."""
    x0 = int(box["x"] * W)
    y0 = int(box["y"] * H)
    x1 = int((box["x"] + box["w"]) * W)
    y1 = int((box["y"] + box["h"]) * H)
    draw.rectangle([x0, y0, x1, y1], fill=colour)
