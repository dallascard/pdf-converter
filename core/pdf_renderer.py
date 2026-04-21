"""
pdf_renderer.py — Convert a PDF file into per-page PNG images.

Uses pdf2image (backed by poppler).  Output images are written to
<project_dir>/pages/ and indexed in a JSON manifest.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pdf2image import convert_from_path, pdfinfo_from_path
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
        n_pages = pdfinfo_from_path(str(pdf_path)).get("Pages", 0)
    except Exception:
        n_pages = 0  # will be filled in after rendering

    try:
        # use_pdftocairo=True: pdftocairo writes PNG files natively in C,
        # bypassing the Python PIL memory layer entirely.  For large-page PDFs
        # this avoids holding all decoded pixel data in memory at once.
        raw_paths = convert_from_path(
            str(pdf_path),
            dpi=dpi,
            output_folder=str(pages_dir),
            output_file="tmp_render",
            paths_only=True,
            fmt="png",
            thread_count=4,
            use_pdftocairo=True,
        )
    except PDFInfoNotInstalledError:
        raise RuntimeError(
            "poppler not found. Install it with: brew install poppler"
        ) from None
    except PDFPageCountError as exc:
        raise RuntimeError(f"Could not read PDF: {exc}") from exc

    # Rename tmp_render files to our page_NNNN.png convention and build manifest.
    n_pages = n_pages or len(raw_paths)
    records: list[dict] = []
    for idx, raw_path in enumerate(sorted(raw_paths)):
        page_number = idx + 1
        filename = f"page_{page_number:04d}.png"
        out_path = pages_dir / filename
        Path(raw_path).rename(out_path)

        from PIL import Image
        with Image.open(out_path) as img:
            w, h = img.width, img.height

        records.append(
            {
                "page_number": page_number,
                "image_path": str(Path("pages") / filename),
                "width_px": w,
                "height_px": h,
                "dpi": dpi,
            }
        )
        logger.info("  page %d / %d  (%dx%d px)", page_number, n_pages, w, h)

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
