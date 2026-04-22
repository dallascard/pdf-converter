"""
auto_rotate.py — Detect and correct 90°-increment page rotation.

Algorithm
---------
Surya's LayoutPredictor is run once on each page (no repeated passes at
different rotations).  Because Surya recognises headers by content rather than
position, it labels PageHeader and PageFooter regions correctly even when the
page image is rotated.  The centroid of those boxes tells us which edge of the
image is the "top" of the original document, from which the needed correction
is derived.

If no header/footer boxes are found on a page, the aspect-ratio distribution
of all detected text boxes is used as a fallback: text lines should be wider
than they are tall on a correctly-oriented page.

Falls back to Tesseract word-count if Surya is not installed.

Run this step after Render and before Deskew.  Results are written to
rotate.json; pages.json is updated if any page dimensions change.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

_THUMB_MAX_PX = 1200

# Labels Surya uses for page headers and footers.
_HEADER_LABELS = {"PageHeader", "PageFooter"}

# Labels that represent text lines (aspect ratio should be wide > tall).
_TEXT_LABELS = {"Text", "SectionHeader", "ListItem", "Caption", "Footnote"}

_ROTATIONS = (0, 90, 180, 270)


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------

def detect_rotations_surya(imgs: list[Image.Image]) -> list[int]:
    """Return the correction rotation (0/90/180/270° CCW) for each image.

    Loads Surya's LayoutPredictor once and runs all pages in a single batch.
    Uses PageHeader/PageFooter centroid positions to infer the needed rotation;
    falls back to text-box aspect ratios when no header/footer is detected.
    """
    try:
        from surya.foundation import FoundationPredictor
        from surya.layout import LayoutPredictor
        from surya.settings import settings as surya_settings
    except ImportError:
        raise RuntimeError(
            "surya-ocr is not installed. Run: uv sync --extra surya"
        )

    logger.info("Loading Surya layout model for rotation detection …")
    predictor = LayoutPredictor(
        FoundationPredictor(checkpoint=surya_settings.LAYOUT_MODEL_CHECKPOINT)
    )

    thumbs = [_make_thumb(img).convert("RGB") for img in imgs]
    logger.info("  Running layout detection on %d page(s) …", len(thumbs))
    predictions = predictor(thumbs)

    results = []
    for page_idx, (thumb, pred) in enumerate(zip(thumbs, predictions)):
        W, H = thumb.size
        deg = _infer_rotation(pred.bboxes, W, H)
        logger.debug("  page %d → %d°", page_idx + 1, deg)
        results.append(deg)

    return results


def _infer_rotation(bboxes, W: int, H: int) -> int:
    """Infer the correction angle from Surya bbox detections on one page.

    Strategy:
    1. Find all PageHeader / PageFooter boxes and compute their centroids.
       - Headers should be near the top  → 0°
       - Headers near the bottom         → 180°
       - Headers near the right edge     → 90°  (page was rotated 90° CW)
       - Headers near the left edge      → 270° (page was rotated 90° CCW)
    2. If no header/footer found, look at text-box aspect ratios.
       - Most text boxes wider than tall  → 0° or 180° (use position to pick)
       - Most text boxes taller than wide → 90° or 270°
    3. Default to 0° if evidence is insufficient.
    """
    header_boxes = [b for b in bboxes if b.label in _HEADER_LABELS]

    if header_boxes:
        # Centroid of all header/footer boxes in normalised [0,1] coords.
        cx = sum((b.bbox[0] + b.bbox[2]) / 2 for b in header_boxes) / (W * len(header_boxes))
        cy = sum((b.bbox[1] + b.bbox[3]) / 2 for b in header_boxes) / (H * len(header_boxes))

        dist_top    = cy
        dist_bottom = 1.0 - cy
        dist_left   = cx
        dist_right  = 1.0 - cx

        nearest = min(dist_top, dist_bottom, dist_left, dist_right)

        if nearest == dist_top:
            return 0
        if nearest == dist_bottom:
            return 180
        if nearest == dist_right:
            # Header on right → page was rotated 90° CW → correct with 90° CCW
            return 90
        # Header on left → page was rotated 90° CCW → correct with 270° CCW
        return 270

    # --- Fallback: text-box aspect ratios ---
    text_boxes = [b for b in bboxes if b.label in _TEXT_LABELS]
    if not text_boxes:
        return 0  # no evidence — assume correct

    wide = sum(
        1 for b in text_boxes
        if (b.bbox[2] - b.bbox[0]) > (b.bbox[3] - b.bbox[1])
    )
    tall = len(text_boxes) - wide

    if wide >= tall:
        # Text is mostly horizontal — page is 0° or 180°; can't easily tell,
        # default to 0° (most pages in a document are correctly oriented).
        return 0
    else:
        # Text is mostly vertical — page is 90° or 270°; can't easily tell,
        # default to 90° as a guess (caller can override with --force).
        return 90


# ---------------------------------------------------------------------------
# Fallback: Tesseract word-count
# ---------------------------------------------------------------------------

def detect_rotations_tesseract(imgs: list[Image.Image]) -> list[int]:
    """Fallback: try all 4 rotations per page, pick the one with most words."""
    results = []
    for idx, img in enumerate(imgs):
        thumb = _make_thumb(img)
        scores = [_tesseract_word_count(thumb.rotate(d, expand=True)) for d in _ROTATIONS]
        best = _ROTATIONS[scores.index(max(scores))]
        logger.debug("  page %d word counts %s → %d°", idx + 1, scores, best)
        results.append(best)
    return results


# ---------------------------------------------------------------------------
# Batch driver
# ---------------------------------------------------------------------------

def rotate_pages(
    project_dir: Path,
    page_records: list[dict],
    pages: list[int] | None = None,
    force: bool = False,
) -> list[dict]:
    """Detect and correct rotation for each page, overwriting PNGs in-place.

    Uses Surya's LayoutPredictor (header-position method) if available,
    otherwise falls back to Tesseract word-count.

    *pages* is a 1-based list of page numbers to process; ``None`` = all pages.
    Skips pages already in rotate.json unless *force* is True.

    Returns a list of result dicts and writes rotate.json.  Also updates
    pages.json if any page dimensions change (90°/270° swaps width and height).
    """
    rotate_json = project_dir / "rotate.json"
    pages_json  = project_dir / "pages.json"

    previous: dict[int, dict] = {}
    if rotate_json.exists() and not force:
        try:
            for r in json.loads(rotate_json.read_text()):
                previous[r["page"]] = r
        except Exception:
            pass

    page_filter: set[int] | None = set(pages) if pages is not None else None

    # Split into pages to process vs pages to skip.
    to_process: list[tuple[int, dict]] = []   # (record index, record)
    skip_results: list[dict] = []

    for i, record in enumerate(page_records):
        page_num = record.get("page_number", 0)
        img_path = project_dir / record["image_path"]

        if page_filter is not None and page_num not in page_filter:
            skip_results.append({"page": page_num, "image_path": str(img_path),
                                  "degrees": 0, "skipped": True})
        elif page_num in previous and not force:
            logger.info("Page %d already processed (%d°); skipping.",
                        page_num, previous[page_num]["degrees"])
            skip_results.append({**previous[page_num], "skipped": True})
        elif not img_path.exists():
            logger.warning("Page image not found: %s", img_path)
            skip_results.append({"page": page_num, "image_path": str(img_path),
                                  "degrees": 0, "skipped": True})
        else:
            to_process.append((i, record))

    if not to_process:
        _write_rotate_json(rotate_json, skip_results, previous)
        return skip_results

    loaded_imgs = [
        Image.open(project_dir / page_records[i]["image_path"])
        for i, _ in to_process
    ]

    try:
        best_rotations = detect_rotations_surya(loaded_imgs)
        engine = "surya"
    except RuntimeError:
        logger.info("Surya not available; falling back to Tesseract.")
        best_rotations = detect_rotations_tesseract(loaded_imgs)
        engine = "tesseract"

    processed_results: list[dict] = []
    pages_changed = False

    for (rec_idx, record), img, degrees in zip(to_process, loaded_imgs, best_rotations):
        page_num = record.get("page_number", 0)
        img_path = project_dir / record["image_path"]
        logger.info("Page %d: %d° correction (via %s)", page_num, degrees, engine)

        if degrees != 0:
            corrected = img.rotate(degrees, expand=True)
            corrected.save(img_path)
            if degrees in (90, 270):
                record["width_px"], record["height_px"] = (
                    record.get("height_px", corrected.height),
                    record.get("width_px", corrected.width),
                )
                pages_changed = True
            logger.info("  → rotated and saved.")
        else:
            logger.info("  → already upright, no change.")

        processed_results.append({
            "page": page_num,
            "image_path": str(img_path),
            "degrees": degrees,
            "skipped": False,
        })

    if pages_changed:
        pages_json.write_text(json.dumps(page_records, indent=2))

    all_results = skip_results + processed_results
    _write_rotate_json(rotate_json, all_results, previous)
    return all_results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_thumb(img: Image.Image) -> Image.Image:
    longest = max(img.width, img.height)
    if longest > _THUMB_MAX_PX:
        scale = _THUMB_MAX_PX / longest
        img = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.LANCZOS,
        )
    return img.convert("L")


def _tesseract_word_count(img: Image.Image) -> int:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp_path = f.name
    try:
        img.save(tmp_path)
        result = subprocess.run(
            ["tesseract", tmp_path, "stdout", "--psm", "3"],
            capture_output=True, text=True, timeout=30,
        )
        return len(result.stdout.split())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _write_rotate_json(
    rotate_json: Path,
    results: list[dict],
    previous: dict[int, dict],
) -> None:
    processed_pages = {r["page"] for r in results if not r["skipped"]}
    prev_kept = [v for k, v in previous.items() if k not in processed_pages]
    rotate_json.write_text(json.dumps(prev_kept + [r for r in results if not r["skipped"]], indent=2))
