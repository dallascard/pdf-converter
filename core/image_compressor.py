"""
image_compressor.py — Compress figure and table crop images.

Why this exists
---------------
Figure and table crops extracted from high-DPI scans can be several MB each.
This step converts any crop in *project_dir/images/* that exceeds a file-size
threshold from PNG to JPEG, making HTML/EPUB exports significantly smaller —
especially when using ``--self-contained`` (base64 embedding).

Page images (in *pages/*) are NOT touched: keeping them lossless preserves
OCR quality.  For the Claude API alt-text step, page images that exceed the
5 MB limit are compressed in-memory at call time (see ``alt_text.py``).

After compression, *figures.json* and *tables.json* are updated with the new
``.jpg`` crop paths so the assembler and exporter pick them up automatically.
A summary is written to *project_dir/compress.json*; re-running the step skips
already-compressed crops unless ``--force`` is used.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

from PIL import Image

import config

logger = logging.getLogger(__name__)


def compress_figures(
    project_dir: Path,
    max_bytes: int = config.COMPRESS_MAX_BYTES,
    jpeg_quality: int = config.COMPRESS_JPEG_QUALITY,
    force: bool = False,
) -> dict:
    """Compress oversized figure and table crop images in *project_dir/images/*.

    Strategy
    --------
    1. If the crop is already under *max_bytes* — leave it alone.
    2. Convert to JPEG at *jpeg_quality* (usually 5–10× smaller for scanned
       content with no visible quality loss).
    3. If the JPEG is still over *max_bytes* (very large crops), scale
       dimensions down by 25 % repeatedly until it fits.

    *figures.json* and *tables.json* are updated in-place with the new paths.
    Returns a summary dict ``{"figures": [...], "tables": [...]}``.
    """
    compress_path = project_dir / "compress.json"

    previous: dict[str, dict] = {}
    if compress_path.exists() and not force:
        try:
            prev = json.loads(compress_path.read_text())
            for r in prev.get("figures", []) + prev.get("tables", []):
                previous[r["original_path"]] = r
        except Exception:
            pass

    fig_results = _compress_crop_list(
        project_dir, "figures.json", "crop_path",
        max_bytes, jpeg_quality, previous, force,
    )
    tbl_results = _compress_crop_list(
        project_dir, "tables.json", "crop_path",
        max_bytes, jpeg_quality, previous, force,
    )

    summary = {"figures": fig_results, "tables": tbl_results}
    compress_path.write_text(json.dumps(summary, indent=2))

    n_fig = sum(1 for r in fig_results if r["action"] != "ok")
    n_tbl = sum(1 for r in tbl_results if r["action"] != "ok")
    logger.info(
        "Compression complete: %d figure crop(s), %d table crop(s) converted.",
        n_fig, n_tbl,
    )
    return summary


def compress_image_bytes(
    data: bytes,
    max_bytes: int = config.COMPRESS_MAX_BYTES,
    jpeg_quality: int = config.COMPRESS_JPEG_QUALITY,
    max_dim: int = config.COMPRESS_MAX_DIM,
) -> tuple[bytes, str]:
    """Compress *data* in-memory to fit within *max_bytes* and *max_dim* pixels.

    Returns ``(compressed_bytes, media_type)`` where *media_type* is
    ``"image/jpeg"`` if compression was applied, or ``"image/png"`` if the
    original was already small enough.

    Both constraints are applied:
    - *max_bytes*: file-size ceiling (bytes)
    - *max_dim*: maximum pixel length on the longest side

    Used by ``alt_text.py`` to compress page images before Claude API calls
    without modifying any files on disk.  Some PDFs render to very large pixel
    dimensions regardless of the DPI setting (e.g. documents that use a
    non-standard UserUnit), so capping dimensions is essential.
    """
    img = Image.open(io.BytesIO(data)).convert("RGB")
    orig_w, orig_h = img.width, img.height

    # Fast path: already small enough in both dimensions and byte size.
    if len(data) <= max_bytes and max(orig_w, orig_h) <= max_dim:
        return data, "image/png"

    # --- pixel-dimension cap (applied once, before the byte-size loop) ---
    longest = max(img.width, img.height)
    if longest > max_dim:
        dim_scale = max_dim / longest
        img = img.resize(
            (max(1, int(img.width * dim_scale)), max(1, int(img.height * dim_scale))),
            Image.LANCZOS,
        )
        logger.debug(
            "In-memory resize: %dx%d → %dx%d (longest side capped at %dpx)",
            orig_w, orig_h, img.width, img.height, max_dim,
        )

    # --- byte-size loop ---
    scale = 1.0

    while True:
        if scale < 1.0:
            w = max(1, int(img.width * scale))
            h = max(1, int(img.height * scale))
            candidate = img.resize((w, h), Image.LANCZOS)
        else:
            candidate = img

        buf = io.BytesIO()
        candidate.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
        if buf.tell() <= max_bytes or scale < 0.2:
            break
        scale *= 0.75

    logger.debug(
        "In-memory compression: %s → %s (scale=%.0f%%)",
        _fmt_bytes(len(data)), _fmt_bytes(buf.tell()), scale * 100,
    )
    return buf.getvalue(), "image/jpeg"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compress_crop_list(
    project_dir: Path,
    json_filename: str,
    path_field: str,
    max_bytes: int,
    jpeg_quality: int,
    previous: dict,
    force: bool,
) -> list[dict]:
    json_path = project_dir / json_filename
    if not json_path.exists():
        return []

    records = json.loads(json_path.read_text())
    results: list[dict] = []
    changed = False

    for rec in records:
        rel = rec.get(path_field, "")
        img_path = project_dir / rel

        if not img_path.exists():
            results.append({"original_path": rel, "action": "missing"})
            continue

        if rel in previous and not force:
            results.append(previous[rel])
            continue

        original_bytes = img_path.stat().st_size
        if original_bytes <= max_bytes:
            logger.info("  %s: %s — under threshold, skipping.",
                        img_path.name, _fmt_bytes(original_bytes))
            results.append({"original_path": rel, "action": "ok",
                            "original_bytes": original_bytes,
                            "final_bytes": original_bytes})
            continue

        logger.info("  %s: %s — compressing …",
                    img_path.name, _fmt_bytes(original_bytes))

        img = Image.open(img_path).convert("RGB")
        scale = 1.0

        while True:
            candidate = img.resize(
                (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                Image.LANCZOS,
            ) if scale < 1.0 else img

            buf = io.BytesIO()
            candidate.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
            if buf.tell() <= max_bytes or scale < 0.2:
                break
            scale *= 0.75

        jpg_rel = str(Path(rel).with_suffix(".jpg"))
        jpg_path = project_dir / jpg_rel
        jpg_path.write_bytes(buf.getvalue())

        if jpg_path != img_path:
            img_path.unlink()

        rec[path_field] = jpg_rel
        changed = True

        action = "scaled+jpeg" if scale < 1.0 else "jpeg"
        logger.info("    → %s  (%s)", jpg_rel, _fmt_bytes(jpg_path.stat().st_size))
        results.append({
            "original_path": rel,
            "final_path": jpg_rel,
            "action": action,
            "original_bytes": original_bytes,
            "final_bytes": jpg_path.stat().st_size,
            "scale": round(scale, 3),
        })

    if changed:
        json_path.write_text(json.dumps(records, indent=2))

    return results


def _fmt_bytes(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    if n >= 1_000:
        return f"{n / 1_000:.0f} KB"
    return f"{n} B"
