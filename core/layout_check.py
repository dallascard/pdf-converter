"""
layout_check.py — Detect masked page images that no longer match boxes.json.

The extract ("Confirm layout") step paints figures, tables, exclusions,
captions and notes white on a copy of each page; OCR reads those copies.
If boxes are edited afterwards without re-running extract, OCR silently
uses the old masks.

Two checks
----------
- **Fast** (used before OCR): extract writes *pages_masked/layout_stamp.json*,
  a fingerprint per page of the mask-relevant boxes plus the page image's
  file size.  Comparing it with the current boxes.json is instant.
- **Thorough** (``check-layout``, or before OCR when there is no stamp yet):
  each masked page is redrawn from the current boxes.json and compared
  pixel by pixel with the file in *pages_masked/*.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from PIL import Image, ImageChops

from core.layout_analyzer import load_boxes

logger = logging.getLogger(__name__)

STAMP_PATH = Path("pages_masked") / "layout_stamp.json"

# Box types painted white by extract (global_exclusions apply to every page)
_MASK_KEYS = ("figures", "tables", "exclusions", "captions", "notes")


class StaleLayoutError(RuntimeError):
    """Masked pages are missing or out of date with boxes.json."""


def mask_zones(page_boxes: dict, global_exclusions: list[dict]) -> list[tuple[str, dict]]:
    """The (type, box) pairs extract paints white on one page, in painting order."""
    zones = [(key, z) for key in ("figures", "tables", "exclusions") for z in page_boxes.get(key, [])]
    zones += [("global_exclusions", z) for z in global_exclusions]
    zones += [(key, z) for key in ("captions", "notes") for z in page_boxes.get(key, [])]
    return zones


def _fingerprint(project_dir: Path, rec: dict, boxes: dict) -> str:
    page_boxes = boxes.get("pages", {}).get(str(rec["page_number"]), {})
    zones = [[k, [round(z[c], 6) for c in ("x", "y", "w", "h")]]
             for k, z in mask_zones(page_boxes, boxes.get("global_exclusions", []))]
    img_path = project_dir / rec["image_path"]
    size = img_path.stat().st_size if img_path.exists() else None   # changes if deskew/rotate rewrote the page
    return hashlib.sha256(json.dumps([zones, size]).encode()).hexdigest()[:16]


def write_layout_stamp(project_dir: Path, page_records: list[dict], boxes: dict | None = None) -> None:
    """Record the fingerprint of each page's masks (called by extract)."""
    boxes = load_boxes(project_dir) if boxes is None else boxes
    stamp = {str(r["page_number"]): _fingerprint(project_dir, r, boxes) for r in page_records}
    (project_dir / STAMP_PATH).write_text(json.dumps(stamp, indent=2))


def stale_pages_fast(project_dir: Path, page_records: list[dict]) -> list[int] | None:
    """Pages whose masks don't match boxes.json, via the stamp; None if there is no stamp."""
    stamp_path = project_dir / STAMP_PATH
    if not stamp_path.exists():
        return None
    stamp = json.loads(stamp_path.read_text())
    boxes = load_boxes(project_dir)
    return [r["page_number"] for r in page_records
            if not _has_masked_image(project_dir, r)
            or stamp.get(str(r["page_number"])) != _fingerprint(project_dir, r, boxes)]


def _has_masked_image(project_dir: Path, rec: dict) -> bool:
    rel = rec.get("masked_image_path")
    return bool(rel) and (project_dir / rel).exists()


def compare_masks(project_dir: Path, page_records: list[dict]) -> list[dict]:
    """Redraw each page's masks from boxes.json and compare with pages_masked/.

    Returns one dict per problem page:
    ``{"page": N, "problem": "missing" | "differs", "details": [str, ...]}``.
    """
    from core.figure_extractor import mask_page

    boxes = load_boxes(project_dir)
    problems = []
    for rec in page_records:
        page_num = rec["page_number"]
        if not _has_masked_image(project_dir, rec):
            problems.append({"page": page_num, "problem": "missing", "details": []})
            continue
        original = Image.open(project_dir / rec["image_path"]).convert("RGB")
        actual = Image.open(project_dir / rec["masked_image_path"]).convert("RGB")
        if original.size != actual.size:
            problems.append({"page": page_num, "problem": "differs",
                             "details": ["page image size changed since the masks were made "
                                         "(re-rendered, rotated or deskewed?)"]})
            continue
        page_boxes = boxes.get("pages", {}).get(str(page_num), {})
        expected = mask_page(original, page_boxes, boxes.get("global_exclusions", []))
        if ImageChops.difference(expected, actual).getbbox() is None:
            continue

        # Which current boxes aren't painted in the old masked image?
        details = []
        W, H = actual.size
        for kind, z in mask_zones(page_boxes, boxes.get("global_exclusions", [])):
            region = (int(z["x"] * W), int(z["y"] * H),
                      int((z["x"] + z["w"]) * W), int((z["y"] + z["h"]) * H))
            if ImageChops.difference(expected.crop(region), actual.crop(region)).getbbox():
                details.append(f"{kind} box at y={z['y']:.2f} is not masked (added or moved since)")
        if not details:
            details.append("masked area that no longer has a box (a box was removed or shrunk since)")
        problems.append({"page": page_num, "problem": "differs", "details": details})
    return problems


def ensure_masks_current(project_dir: Path, page_records: list[dict]) -> None:
    """Raise StaleLayoutError if OCR would read missing or out-of-date masked pages."""
    stale = stale_pages_fast(project_dir, page_records)
    if stale is None:
        # Project confirmed before stamps existed: check the pixels once, then stamp.
        logger.info("Checking masked pages against boxes.json …")
        stale = [p["page"] for p in compare_masks(project_dir, page_records)]
        if not stale:
            write_layout_stamp(project_dir, page_records)
    if stale:
        raise StaleLayoutError(
            f"Masked pages are missing or don't match boxes.json on {len(stale)} page(s) "
            f"({_page_list(stale)}). Run 'Confirm layout' (cli: extract --force) first, "
            "or pass --skip-layout-check to OCR them anyway."
        )


def layout_report(project_dir: Path, page_records: list[dict]) -> tuple[list[dict], list[str]]:
    """Thorough check plus what it means for existing OCR output.

    Returns ``(problems, summary_lines)``.  The OCR verdict uses file
    modification times, which copying a project without preserving
    them (e.g. plain ``scp``) resets.
    """
    problems = compare_masks(project_dir, page_records)
    if not problems:
        # Matching now — refresh the stamp so the fast check before OCR agrees.
        write_layout_stamp(project_dir, page_records)
    bad = [p["page"] for p in problems]

    def mtime(rel: str) -> float | None:
        path = project_dir / rel
        return path.stat().st_mtime if path.exists() else None

    boxes_t = mtime("boxes.json")
    ocr_t = mtime("ocr_raw.json")
    mask_ts = [mtime(r["masked_image_path"]) for r in page_records if _has_masked_image(project_dir, r)]
    oldest_mask_t = min(mask_ts) if mask_ts else None

    lines = []
    if not bad:
        lines.append(f"Masked pages match boxes.json on all {len(page_records)} pages.")
    else:
        lines.append(f"Masked pages do NOT match boxes.json on {len(bad)} page(s): {_page_list(bad)}.")

    if ocr_t is None:
        lines.append("No OCR output yet.")
    elif bad and boxes_t is not None and ocr_t > boxes_t:
        lines.append("OCR ran after boxes.json was last saved, so it used out-of-date masks on "
                     "those pages. Run Confirm layout, then OCR again.")
    elif bad:
        lines.append("boxes.json was saved after the last OCR run. Can't tell whether that OCR "
                     "used matching masks; run Confirm layout, then OCR again to pick up the edits.")
    elif oldest_mask_t is not None and ocr_t < oldest_mask_t:
        lines.append("OCR is older than the masked pages (layout was re-confirmed after OCR). "
                     "Run OCR again to use the current masks.")
    else:
        lines.append("OCR ran after the current masks were made: it used up-to-date masks.")
    return problems, lines


def _page_list(pages: list[int], limit: int = 15) -> str:
    shown = ", ".join(str(p) for p in pages[:limit])
    return shown + (f" … +{len(pages) - limit} more" if len(pages) > limit else "")
