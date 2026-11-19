"""
masked_exporter.py — Export page images as a PDF with selected regions masked white.

Useful for producing a clean copy of the document with e.g. headers/footers,
figures, or other zones painted out — without re-running OCR.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from PIL import Image, ImageDraw

from core.layout_analyzer import load_boxes

logger = logging.getLogger(__name__)

_MASK_COLOUR = (255, 255, 255)

ALL_MASK_TYPES = frozenset(
    {"figures", "tables", "exclusions", "captions", "notes", "headings"}
)


def export_masked_pages(
    project_dir: Path,
    page_records: list[dict],
    mask_types: set[str],
    output_path: Path,
    force: bool = False,
) -> None:
    """Render page images with *mask_types* zones painted white and save as PDF.

    *mask_types* is a subset of ``ALL_MASK_TYPES``.  "exclusions" also
    includes any ``global_exclusions`` defined in boxes.json.

    Output is a multi-page PDF written to *output_path*.
    """
    if output_path.exists() and not force:
        logger.info("%s already exists; skipping (use --force to overwrite).", output_path.name)
        return

    unknown = set(mask_types) - ALL_MASK_TYPES
    if unknown:
        raise ValueError(f"Unknown mask type(s): {', '.join(sorted(unknown))}")

    if not mask_types:
        raise ValueError("At least one mask type must be specified.")

    boxes = load_boxes(project_dir)
    global_excl: list[dict] = boxes.get("global_exclusions") or []

    output_path.parent.mkdir(parents=True, exist_ok=True)

    pages_pil: list[Image.Image] = []
    n = len(page_records)

    for rec in page_records:
        page_num = rec["page_number"]
        img = Image.open(project_dir / rec["image_path"]).convert("RGB")
        W, H = img.size
        draw = ImageDraw.Draw(img)

        page_data: dict = boxes.get("pages", {}).get(str(page_num), {})

        for mask_type in mask_types:
            zones: list[dict] = list(page_data.get(mask_type, []))
            if mask_type == "exclusions":
                zones += global_excl
            for zone in zones:
                _paint_white(draw, zone, W, H)

        pages_pil.append(img)
        logger.info("  Page %d / %d masked.", page_num, n)

    if not pages_pil:
        raise ValueError("No pages found in project.")

    pages_pil[0].save(
        output_path,
        format="PDF",
        save_all=True,
        append_images=pages_pil[1:],
        resolution=200,
    )
    logger.info("Masked PDF written: %s (%d pages).", output_path, len(pages_pil))


def _paint_white(draw: ImageDraw.ImageDraw, box: dict, W: int, H: int) -> None:
    x0 = int(box["x"] * W)
    y0 = int(box["y"] * H)
    x1 = int((box["x"] + box["w"]) * W)
    y1 = int((box["y"] + box["h"]) * H)
    draw.rectangle([x0, y0, x1, y1], fill=_MASK_COLOUR)
