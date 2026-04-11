"""
deskewer.py — Detect and correct small rotational skew in scanned page images.

Algorithm
---------
Projection profile method:
1. Convert the page to grayscale and binarize (Otsu threshold approximation).
2. For each candidate angle in [-max_angle, +max_angle] at *step* increments,
   rotate the binary image and compute the variance of the row-sum projection.
3. The angle that maximises projection variance corresponds to the rotation
   that best aligns text baselines with the horizontal axis.
4. Rotate the original (colour) image by that angle with white fill and
   overwrite the source file.

Dependencies: Pillow (core dep) + numpy (transitive dep via surya / torch).
Falls back gracefully if numpy is unavailable — skips deskew with a warning.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core algorithm
# ---------------------------------------------------------------------------

def detect_skew(img: Image.Image, max_angle: float = 10.0, step: float = 0.5) -> float:
    """Return the estimated skew angle (degrees) for *img*.

    A positive angle means the image needs to be rotated counter-clockwise
    to straighten it (i.e. ``img.rotate(angle)`` corrects it).
    Returns 0.0 if numpy is unavailable or no clear skew is detected.
    """
    try:
        import numpy as np
    except ImportError:
        logger.warning("numpy not available; skipping skew detection.")
        return 0.0

    # Work on a small grayscale copy for speed
    thumb = img.convert("L")
    scale = min(1.0, 1200 / max(thumb.width, thumb.height))
    if scale < 1.0:
        thumb = thumb.resize(
            (int(thumb.width * scale), int(thumb.height * scale)),
            Image.LANCZOS,
        )

    gray = np.array(thumb, dtype=np.float32)

    # Binarize: pixels darker than threshold → 1 (ink), rest → 0
    threshold = gray.mean()
    binary = (gray < threshold).astype(np.float32)

    best_angle = 0.0
    best_score = -1.0

    angles = []
    a = -max_angle
    while a <= max_angle + 1e-9:
        angles.append(round(a, 6))
        a += step

    for angle in angles:
        rotated_img = Image.fromarray((binary * 255).astype("uint8")).rotate(
            angle, resample=Image.BICUBIC, fillcolor=0
        )
        rotated = np.array(rotated_img, dtype=np.float32) / 255.0
        projection = rotated.sum(axis=1)          # row sums
        score = float(projection.var())
        if score > best_score:
            best_score = score
            best_angle = angle

    return best_angle


def deskew_image(img: Image.Image, angle: float) -> Image.Image:
    """Rotate *img* by *angle* degrees with white background fill."""
    if abs(angle) < 1e-3:
        return img
    return img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=(255, 255, 255))


# ---------------------------------------------------------------------------
# Batch driver
# ---------------------------------------------------------------------------

def deskew_pages(
    project_dir: Path,
    page_records: list[dict],
    pages: list[int] | None = None,
    max_angle: float = 10.0,
    step: float = 0.5,
    force: bool = False,
) -> list[dict]:
    """Deskew selected pages, overwriting their PNG files in-place.

    *pages* is a 1-based list of page numbers to process.  Pass ``None`` to
    process all pages.

    Returns a list of result dicts: {page, path, angle, skipped}.
    Results are also written to *project_dir/deskew.json*.
    """
    deskew_json = project_dir / "deskew.json"
    previous: dict[str, float] = {}
    if deskew_json.exists() and not force:
        try:
            previous = {r["image_path"]: r["angle"] for r in json.loads(deskew_json.read_text())}
        except Exception:
            pass

    # Normalise page filter to a set of 1-based ints
    page_filter: set[int] | None = None
    if pages is not None:
        page_filter = set(pages)

    results: list[dict] = []

    for record in page_records:
        page_num = record.get("page_number", 0)
        img_path = Path(record["image_path"])
        if not img_path.is_absolute():
            img_path = project_dir / img_path

        if page_filter is not None and page_num not in page_filter:
            results.append({"page": page_num, "image_path": str(img_path), "angle": 0.0, "skipped": True})
            continue

        if str(img_path) in previous and not force:
            logger.info("Page %d already deskewed (%.2f°); skipping.", page_num, previous[str(img_path)])
            results.append({"page": page_num, "image_path": str(img_path),
                            "angle": previous[str(img_path)], "skipped": True})
            continue

        if not img_path.exists():
            logger.warning("Page image not found: %s", img_path)
            results.append({"page": page_num, "image_path": str(img_path), "angle": 0.0, "skipped": True})
            continue

        img = Image.open(img_path)
        angle = detect_skew(img, max_angle=max_angle, step=step)
        logger.info("Page %d: detected skew %.2f°", page_num, angle)

        corrected = deskew_image(img, angle)
        corrected.save(img_path)

        results.append({"page": page_num, "image_path": str(img_path), "angle": angle, "skipped": False})

    # Persist results (merge with skipped entries from previous runs)
    all_results = [r for r in results if not r["skipped"]]
    if deskew_json.exists():
        try:
            existing = json.loads(deskew_json.read_text())
            paths_done = {r["image_path"] for r in all_results}
            all_results = [r for r in existing if r["path"] not in paths_done] + all_results
        except Exception:
            pass
    deskew_json.write_text(json.dumps(all_results, indent=2), encoding="utf-8")

    return results
