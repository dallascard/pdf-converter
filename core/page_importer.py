"""
page_importer.py — Import a directory of image files as project pages.

Replaces the Render step for workflows where pages already exist as individual
image files (TIF, TIFF, PNG, JPG, etc.) rather than a single PDF.

Images are sorted naturally by filename, converted to 8-bit RGB PNG, copied
to <project_dir>/pages/, and indexed in pages.json.

DPI is read from each file's metadata if available (common for scanner TIFs);
otherwise a default of 0 is stored, which downstream steps treat as "unknown".
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

_SUPPORTED_EXTENSIONS = {".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def import_pages(
    image_dir: Path,
    project_dir: Path,
    force: bool = False,
) -> list[dict]:
    """Import image files from *image_dir* into *project_dir/pages/*.

    Files are sorted in natural order (so page10 comes after page9).
    Each image is converted to 8-bit RGB PNG.  DPI is preserved from file
    metadata where available.

    Returns a list of page records (also written to *project_dir/pages.json*).
    """
    pages_dir = project_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = project_dir / "pages.json"

    if manifest_path.exists() and not force:
        logger.info("pages.json already exists; skipping import.")
        return json.loads(manifest_path.read_text())

    image_files = sorted(
        (p for p in image_dir.iterdir() if p.suffix.lower() in _SUPPORTED_EXTENSIONS),
        key=_natural_sort_key,
    )

    if not image_files:
        raise FileNotFoundError(
            f"No supported image files found in {image_dir}.\n"
            f"Supported formats: {', '.join(sorted(_SUPPORTED_EXTENSIONS))}"
        )

    logger.info("Importing %d image(s) from %s …", len(image_files), image_dir)

    records: list[dict] = []
    n = len(image_files)

    for idx, src_path in enumerate(image_files):
        page_number = idx + 1
        filename = f"page_{page_number:04d}.png"
        out_path = pages_dir / filename

        img = Image.open(src_path)

        # Read DPI from metadata (common in scanner TIFs).
        dpi_info = img.info.get("dpi") or img.info.get("resolution")
        dpi = int(round(dpi_info[0])) if dpi_info else 0

        # Normalise to 8-bit RGB.
        if img.mode != "RGB":
            img = img.convert("RGB")

        img.save(out_path, format="PNG")

        records.append({
            "page_number": page_number,
            "image_path": str(Path("pages") / filename),
            "width_px": img.width,
            "height_px": img.height,
            "dpi": dpi,
            "source_file": src_path.name,
        })
        logger.info("  page %d / %d  (%dx%d px, dpi=%s)  ← %s",
                    page_number, n, img.width, img.height,
                    dpi if dpi else "unknown", src_path.name)

    manifest_path.write_text(json.dumps(records, indent=2))
    logger.info("Imported %d pages.", len(records))
    return records


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _natural_sort_key(path: Path) -> list:
    """Sort key that orders numeric parts of filenames numerically."""
    parts = re.split(r"(\d+)", path.stem)
    return [int(p) if p.isdigit() else p.lower() for p in parts]
