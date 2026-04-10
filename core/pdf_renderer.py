"""
pdf_renderer.py — Convert a PDF file into per-page PNG images.

Uses pdf2image (backed by poppler).  Output images are written to
<project_dir>/pages/ and indexed in a JSON manifest.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pdf2image import convert_from_path
from pdf2image.exceptions import PDFInfoNotInstalledError, PDFPageCountError

import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_pdf(pdf_path: Path, project_dir: Path, dpi: int = config.RENDER_DPI) -> list[dict]:
    """
    Render every page of *pdf_path* to a PNG file inside *project_dir/pages/*.

    Returns a list of page records (also written to *project_dir/pages.json*):

    .. code-block:: json

        [
            {
                "page_number": 1,
                "image_path": "pages/page_0001.png",
                "width_px": 1654,
                "height_px": 2339
            },
            ...
        ]

    The ``image_path`` values are relative to *project_dir* so the manifest
    remains portable.
    """
    pages_dir = project_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = project_dir / "pages.json"

    # If already rendered at the same DPI, skip.
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text())
        if existing and _dpi_matches(existing, dpi):
            logger.info("Pages already rendered; skipping render step.")
            return existing

    logger.info("Rendering %s at %d DPI …", pdf_path.name, dpi)

    try:
        pil_images = convert_from_path(
            str(pdf_path),
            dpi=dpi,
            fmt=config.RENDER_FORMAT.lower(),
            thread_count=4,
        )
    except PDFInfoNotInstalledError:
        raise RuntimeError(
            "poppler not found. Install it with: brew install poppler"
        ) from None
    except PDFPageCountError as exc:
        raise RuntimeError(f"Could not read PDF: {exc}") from exc

    records: list[dict] = []
    for idx, img in enumerate(pil_images):
        page_number = idx + 1
        filename = f"page_{page_number:04d}.png"
        out_path = pages_dir / filename
        img.save(out_path, format="PNG")

        records.append(
            {
                "page_number": page_number,
                "image_path": str(Path("pages") / filename),
                "width_px": img.width,
                "height_px": img.height,
                "dpi": dpi,
            }
        )
        logger.debug("  saved %s (%dx%d)", filename, img.width, img.height)

    manifest_path.write_text(json.dumps(records, indent=2))
    logger.info("Rendered %d pages.", len(records))
    return records


def load_page_manifest(project_dir: Path) -> list[dict]:
    """Load an existing pages.json manifest, raising if not found."""
    manifest_path = project_dir / "pages.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"No pages manifest found in {project_dir}. "
            "Run the 'render' step first."
        )
    return json.loads(manifest_path.read_text())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dpi_matches(records: list[dict], dpi: int) -> bool:
    return bool(records) and records[0].get("dpi") == dpi
