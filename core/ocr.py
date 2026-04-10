"""
ocr.py — Extract text from masked page images.

Primary engine : Claude Vision (sends masked page PNG, gets structured JSON)
Fallback engine: Tesseract (via pytesseract)

Output — ocr_raw.json
---------------------
A list of page results, one per page:

.. code-block:: json

    [
        {
            "page_number": 1,
            "engine": "claude",
            "lines": [
                {
                    "line_id": "p1_l001",
                    "text": "Chapter 1: Introduction",
                    "type": "heading1",
                    "bbox": {"x": 0.1, "y": 0.05, "w": 0.8, "h": 0.03}
                },
                ...
            ]
        }
    ]

``type`` values
    heading1 | heading2 | heading3 | body | footnote | caption | other

``bbox`` is fractional (0–1), relative to the *original* (unmasked) page.
When Tesseract is used as fallback, bboxes are derived from Tesseract's
word-level data and every line is typed as "body" (heading detection happens
later in structure.py).
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path

import config
from core.claude_client import get_client
from core.layout_analyzer import ensure_paragraph_boxes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Claude prompt
# ---------------------------------------------------------------------------

_OCR_PROMPT = """\
You are performing OCR on a scanned document page.
The image has already been pre-processed: figure regions, table regions, and
page boilerplate (headers, footers, page numbers) have been masked out with
white rectangles.  Transcribe only the visible text.

Return a JSON object with a single key "lines" containing a list of line
objects. Each object must have:

  "text"  – the transcribed text of this line (preserve original spelling,
             hyphenation, and punctuation; do NOT merge hyphenated line-breaks
             across lines)
  "type"  – one of: heading1, heading2, heading3, body, footnote, caption, other
  "bbox"  – bounding box as fractions of page dimensions:
              {"x": <left>, "y": <top>, "w": <width>, "h": <height>}

Rules for "type":
- heading1: chapter titles or the largest headings on the page
- heading2: section headings
- heading3: sub-section headings
- footnote: text that appears at the bottom of the text area, typically
            introduced by a superscript number or symbol
- caption: a line that labels a figure, table, or plate (e.g. "Fig. 3.")
- body: all other regular paragraph text
- other: anything that does not fit the above categories

Preserve reading order (top-to-bottom, then left-to-right for multi-column).
Return ONLY the raw JSON object, no markdown fences, no explanation.
If the page has no visible text, return {"lines": []}.
"""

_TABLE_OCR_PROMPT = """\
You are performing OCR on a cropped table region from a scanned document.
Transcribe the table as a GitHub-flavored Markdown table.

Rules:
- Use | to separate columns and --- for the header separator row.
- Preserve all cell content faithfully, including numbers, units, and symbols.
- If the table has no clear header row, use the first row as the header.
- If the table structure is too complex or unclear for Markdown, output the
  cell content as plain text rows, one row per line, cells separated by " | ".

Return ONLY the Markdown table (or plain-text fallback), no explanation.
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_ocr(
    project_dir: Path,
    page_records: list[dict],
    engine: str = "claude",
    force: bool = False,
) -> list[dict]:
    """
    Run OCR on all pages and write *project_dir/ocr_raw.json*.

    *engine* is ``"claude"``, ``"tesseract"``, or ``"surya"``.
    If Claude fails for a page, Tesseract is used automatically as fallback.
    After page OCR, table crops (if any) are also OCR'd and saved to tables.json.

    Returns the list of page OCR results.
    """
    ocr_path = project_dir / "ocr_raw.json"

    if ocr_path.exists() and not force:
        logger.info("ocr_raw.json exists; skipping OCR step.")
        return json.loads(ocr_path.read_text())

    # Remove stale edited file so downstream steps and the editor use fresh output
    edited_path = project_dir / "ocr_edited.json"
    if force and edited_path.exists():
        edited_path.unlink()
        logger.info("Removed stale ocr_edited.json.")

    # For Surya OCR, ensure paragraph boxes exist in boxes.json so that
    # structure.py can group lines into paragraphs correctly.
    if engine == "surya":
        ensure_paragraph_boxes(project_dir, page_records)

    # Load caption/notes zones from boxes.json for line classification
    boxes_path = project_dir / "boxes.json"
    boxes = json.loads(boxes_path.read_text()) if boxes_path.exists() else {}

    # Pre-load Surya models once if needed (expensive)
    surya_models = _load_surya_ocr_models() if engine == "surya" else None

    results: list[dict] = []

    for rec in page_records:
        page_num = rec["page_number"]
        masked_rel = rec.get("masked_image_path")
        if masked_rel:
            img_path = project_dir / masked_rel
        else:
            img_path = project_dir / rec["image_path"]
            logger.warning(
                "Page %d has no masked image; OCR will run on the original.", page_num
            )

        logger.info("  OCR page %d (%s) …", page_num, engine)

        if engine == "claude":
            page_result = _ocr_claude(img_path, page_num)
            if page_result is None:
                logger.warning("  Claude failed for page %d; falling back to Tesseract.", page_num)
                page_result = _ocr_tesseract(img_path, page_num)
        elif engine == "surya":
            page_result = _ocr_surya(img_path, page_num, surya_models)
        else:
            page_result = _ocr_tesseract(img_path, page_num)

        # Classify main-page lines that overlap caption/note/heading zones
        page_boxes = boxes.get("pages", {}).get(str(page_num), {})
        _classify_zone_lines(page_result["lines"], page_boxes)

        # OCR caption and note zones from the original (unmasked) page image,
        # then fold the typed lines back into the page result in y order.
        orig_img_path = project_dir / rec["image_path"]
        zone_lines = _ocr_zone_crops(
            orig_img_path, page_boxes, page_num, engine, surya_models,
            len(page_result["lines"]),
        )
        if zone_lines:
            combined = page_result["lines"] + zone_lines
            combined.sort(key=lambda l: l["bbox"]["y"])
            page_result["lines"] = combined

        results.append(page_result)

    ocr_path.write_text(json.dumps(results, indent=2))
    logger.info("OCR complete. %d pages processed.", len(results))

    # OCR table crops
    run_table_ocr(project_dir, engine=engine, force=force)

    return results


def run_table_ocr(
    project_dir: Path,
    engine: str = "claude",
    force: bool = False,
) -> list[dict]:
    """
    OCR table crops listed in tables.json and store the result in each record.

    Uses a table-specific prompt for Claude/Surya to produce Markdown tables.
    Tesseract output is stored as preformatted text.
    Updates tables.json in place.
    """
    tables_path = project_dir / "tables.json"
    if not tables_path.exists():
        return []

    table_records = json.loads(tables_path.read_text())
    if not table_records:
        return []

    # Skip if all records already have content and not forcing
    if not force and all(r.get("content") for r in table_records):
        logger.info("Table OCR already complete; skipping.")
        return table_records

    logger.info("  Running table OCR on %d table(s) …", len(table_records))

    surya_models = _load_surya_ocr_models() if engine == "surya" else None

    for rec in table_records:
        if rec.get("content") and not force:
            continue
        crop_path = project_dir / rec["crop_path"]
        if not crop_path.exists():
            logger.warning("Table crop not found: %s", crop_path)
            continue

        logger.info("    OCR table %s …", rec["id"])
        if engine == "claude":
            content, fmt = _ocr_table_claude(crop_path)
        elif engine == "surya":
            content, fmt = _ocr_table_surya(crop_path, surya_models)
        else:
            content, fmt = _ocr_table_tesseract(crop_path)

        rec["content"] = content
        rec["content_format"] = fmt

    tables_path.write_text(json.dumps(table_records, indent=2))
    logger.info("Table OCR complete.")
    return table_records


def load_ocr(project_dir: Path, edited: bool = False) -> list[dict]:
    """
    Load OCR results.  If *edited* is True, load ocr_edited.json if it
    exists, otherwise fall back to ocr_raw.json.
    """
    if edited:
        edited_path = project_dir / "ocr_edited.json"
        if edited_path.exists():
            return json.loads(edited_path.read_text())

    raw_path = project_dir / "ocr_raw.json"
    if not raw_path.exists():
        raise FileNotFoundError(
            f"No ocr_raw.json in {project_dir}. Run the 'ocr' step first."
        )
    return json.loads(raw_path.read_text())


def save_edited_ocr(project_dir: Path, results: list[dict]) -> None:
    """Persist user-edited OCR results to ocr_edited.json."""
    (project_dir / "ocr_edited.json").write_text(json.dumps(results, indent=2))


# ---------------------------------------------------------------------------
# Claude Vision engine
# ---------------------------------------------------------------------------

def _ocr_claude(img_path: Path, page_num: int) -> dict | None:
    """Return a page result dict, or None on failure."""
    try:
        client = get_client()
        img_b64 = base64.standard_b64encode(img_path.read_bytes()).decode()

        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=4096,
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
                        {"type": "text", "text": _OCR_PROMPT},
                    ],
                }
            ],
        )
        raw = response.content[0].text.strip()
        lines = _parse_ocr_response(raw, page_num)
        return {"page_number": page_num, "engine": "claude", "lines": lines}
    except Exception as exc:
        logger.error("Claude OCR error on page %d: %s", page_num, exc)
        return None


def _parse_ocr_response(raw: str, page_num: int) -> list[dict]:
    """Parse Claude's JSON OCR response into a list of line dicts."""
    raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"```$", "", raw, flags=re.MULTILINE)
    raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Could not parse OCR JSON for page %d: %r", page_num, raw[:200])
        return []

    lines = []
    for idx, item in enumerate(data.get("lines", [])):
        line_id = f"p{page_num}_l{idx + 1:03d}"
        bbox = item.get("bbox", {})
        lines.append(
            {
                "line_id": line_id,
                "text": str(item.get("text", "")).strip(),
                "type": _normalise_type(item.get("type", "body")),
                "bbox": {
                    "x": float(bbox.get("x", 0)),
                    "y": float(bbox.get("y", 0)),
                    "w": float(bbox.get("w", 1)),
                    "h": float(bbox.get("h", 0.02)),
                },
            }
        )
    return lines


def _normalise_type(t: str) -> str:
    valid = {"heading1", "heading2", "heading3", "body", "footnote", "caption", "other"}
    t = str(t).lower().strip()
    return t if t in valid else "body"


# ---------------------------------------------------------------------------
# Surya OCR engine
# ---------------------------------------------------------------------------

def _load_surya_ocr_models():
    """Load Surya OCR models (expensive — call once and reuse)."""
    try:
        from surya.detection import DetectionPredictor
        from surya.foundation import FoundationPredictor
        from surya.recognition import RecognitionPredictor
    except ImportError:
        raise RuntimeError("surya-ocr is not installed. Run: uv sync --extra surya")

    logger.info("Loading Surya OCR models …")
    foundation_predictor = FoundationPredictor()
    det_predictor = DetectionPredictor()
    rec_predictor = RecognitionPredictor(foundation_predictor)
    return det_predictor, rec_predictor


def _ocr_surya(img_path: Path, page_num: int, surya_models) -> dict:
    """Run Surya OCR on a single page image."""
    try:
        from PIL import Image
        from surya.common.surya.schema import TaskNames

        det_predictor, rec_predictor = surya_models
        img = Image.open(img_path).convert("RGB")
        W, H = img.size

        predictions = rec_predictor(
            [img],
            task_names=[TaskNames.ocr_with_boxes],
            det_predictor=det_predictor,
        )
        pred = predictions[0]

        lines = []
        for idx, tl in enumerate(pred.text_lines):
            x1, y1, x2, y2 = tl.bbox
            lines.append({
                "line_id": f"p{page_num}_l{idx + 1:03d}",
                "text": tl.text.strip(),
                "type": "body",
                "bbox": {
                    "x": x1 / W,
                    "y": y1 / H,
                    "w": (x2 - x1) / W,
                    "h": (y2 - y1) / H,
                },
            })

        lines.sort(key=lambda l: l["bbox"]["y"])
        return {"page_number": page_num, "engine": "surya", "lines": lines}

    except Exception as exc:
        logger.error("Surya OCR error on page %d: %s", page_num, exc)
        return {"page_number": page_num, "engine": "surya", "lines": []}


# ---------------------------------------------------------------------------
# Table OCR helpers
# ---------------------------------------------------------------------------

def _ocr_table_claude(crop_path: Path) -> tuple[str, str]:
    """OCR a table crop with Claude, returning (content, format)."""
    try:
        client = get_client()
        img_b64 = base64.standard_b64encode(crop_path.read_bytes()).decode()
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=2048,
            messages=[{
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
                    {"type": "text", "text": _TABLE_OCR_PROMPT},
                ],
            }],
        )
        content = response.content[0].text.strip()
        # Strip any markdown fences Claude added despite instructions
        content = re.sub(r"^```[a-z]*\n?", "", content, flags=re.MULTILINE)
        content = re.sub(r"```$", "", content, flags=re.MULTILINE).strip()
        fmt = "markdown" if "|" in content else "preformatted"
        return content, fmt
    except Exception as exc:
        logger.warning("Claude table OCR failed for %s: %s", crop_path.name, exc)
        return "", ""


def _ocr_table_surya(crop_path: Path, surya_models) -> tuple[str, str]:
    """OCR a table crop with Surya's dedicated TableRecPredictor.

    Detects row/column/cell structure with TableRecPredictor, crops each
    cell, batch-OCRs them, and assembles a GitHub-flavored Markdown table.
    Falls back to plain line OCR (preformatted) if structure detection
    finds no cells.
    """
    try:
        from PIL import Image
        from surya.common.surya.schema import TaskNames

        det_predictor, rec_predictor = surya_models
        img = Image.open(crop_path).convert("RGB")
        W, H = img.size

        # --- Table structure detection --------------------------------------
        table_rec = _get_surya_table_rec_predictor()
        result = table_rec([img])[0]

        if not result.cells or not result.rows or not result.cols:
            # No structure detected — fall back to plain line OCR
            preds = rec_predictor(
                [img], task_names=[TaskNames.ocr_with_boxes],
                det_predictor=det_predictor,
            )
            lines = [tl.text.strip() for tl in preds[0].text_lines if tl.text.strip()]
            return "\n".join(lines), "preformatted"

        num_rows = len(result.rows)
        num_cols = len(result.cols)

        # Sort cells by (row_id, col_id) for consistent ordering
        cells = sorted(
            result.cells,
            key=lambda c: (c.row_id, c.col_id if c.col_id is not None else 0),
        )

        # --- Crop each cell and batch-OCR ----------------------------------
        cell_crops: list = []
        for cell in cells:
            x1, y1, x2, y2 = [int(v) for v in cell.bbox]
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(W, x2), min(H, y2)
            cell_crops.append(img.crop((x1, y1, x2, y2)) if x2 > x1 and y2 > y1 else None)

        valid_idx = [i for i, c in enumerate(cell_crops) if c is not None]
        valid_crops = [cell_crops[i] for i in valid_idx]
        cell_texts: list[str] = [""] * len(cells)

        if valid_crops:
            # Use a full-image bbox per crop so Surya OCRs the whole cell
            bboxes_per_crop = [[[0, 0, c.width, c.height]] for c in valid_crops]
            ocr_preds = rec_predictor(
                valid_crops,
                task_names=[TaskNames.ocr_without_boxes] * len(valid_crops),
                bboxes=bboxes_per_crop,
            )
            for idx, pred in zip(valid_idx, ocr_preds):
                cell_texts[idx] = " ".join(
                    tl.text.strip() for tl in pred.text_lines if tl.text.strip()
                )

        # --- Build row-column text grid ------------------------------------
        grid: list[list[str]] = [[""] * num_cols for _ in range(num_rows)]
        for cell, text in zip(cells, cell_texts):
            r = cell.row_id
            c = cell.col_id if cell.col_id is not None else 0
            if 0 <= r < num_rows and 0 <= c < num_cols:
                grid[r][c] = text.replace("|", "\\|")  # escape pipes in cell text

        # --- Format as Markdown table -------------------------------------
        header_row_ids = {row.row_id for row in result.rows if row.is_header}
        header_idx = min(header_row_ids) if header_row_ids else 0

        sep = "| " + " | ".join("---" for _ in range(num_cols)) + " |"

        def md_row(cells_text: list[str]) -> str:
            return "| " + " | ".join(t or " " for t in cells_text) + " |"

        md_lines: list[str] = []
        sep_inserted = False
        for r, row in enumerate(grid):
            md_lines.append(md_row(row))
            if r == header_idx:
                md_lines.append(sep)
                sep_inserted = True

        if not sep_inserted and md_lines:
            md_lines.insert(1, sep)

        return "\n".join(md_lines), "markdown"

    except Exception as exc:
        logger.warning("Surya table OCR failed for %s: %s", crop_path.name, exc)
        return "", ""


# Module-level singleton — loaded once on first table OCR call
_surya_table_rec_predictor = None


def _get_surya_table_rec_predictor():
    """Lazily load Surya's TableRecPredictor (cached as module-level singleton)."""
    global _surya_table_rec_predictor
    if _surya_table_rec_predictor is None:
        from surya.table_rec import TableRecPredictor
        logger.info("Loading Surya TableRecPredictor …")
        _surya_table_rec_predictor = TableRecPredictor()
    return _surya_table_rec_predictor



def _ocr_table_tesseract(crop_path: Path) -> tuple[str, str]:
    """OCR a table crop with Tesseract, returning preformatted text."""
    try:
        import pytesseract
        from PIL import Image

        img = Image.open(crop_path)
        text = pytesseract.image_to_string(img, lang=config.TESSERACT_LANG).strip()
        return text, "preformatted"
    except Exception as exc:
        logger.warning("Tesseract table OCR failed for %s: %s", crop_path.name, exc)
        return "", ""


# ---------------------------------------------------------------------------
# Tesseract fallback engine
# ---------------------------------------------------------------------------

def _ocr_tesseract(img_path: Path, page_num: int) -> dict:
    """Run Tesseract on a page image and return a page result dict."""
    try:
        import pytesseract
        from PIL import Image

        img = Image.open(img_path)
        data = pytesseract.image_to_data(
            img,
            lang=config.TESSERACT_LANG,
            output_type=pytesseract.Output.DICT,
        )
        W, H = img.size
        lines = _tesseract_data_to_lines(data, W, H, page_num)
        return {"page_number": page_num, "engine": "tesseract", "lines": lines}
    except ImportError:
        logger.error("pytesseract is not installed. Install it with: pip install pytesseract")
        return {"page_number": page_num, "engine": "tesseract", "lines": []}
    except Exception as exc:
        logger.error("Tesseract error on page %d: %s", page_num, exc)
        return {"page_number": page_num, "engine": "tesseract", "lines": []}


def _tesseract_data_to_lines(data: dict, W: int, H: int, page_num: int) -> list[dict]:
    """
    Convert Tesseract word-level output to our line format.

    Tesseract returns individual words; we group them by (block_num, par_num,
    line_num) to reconstruct lines.
    """
    from collections import defaultdict

    line_buckets: dict[tuple, list] = defaultdict(list)
    n = len(data["text"])

    for i in range(n):
        word = data["text"][i].strip()
        conf = int(data["conf"][i])
        if not word or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        line_buckets[key].append(
            {
                "word": word,
                "x": data["left"][i],
                "y": data["top"][i],
                "w": data["width"][i],
                "h": data["height"][i],
            }
        )

    lines = []
    for idx, (key, words) in enumerate(sorted(line_buckets.items())):
        text = " ".join(w["word"] for w in words)
        x0 = min(w["x"] for w in words)
        y0 = min(w["y"] for w in words)
        x1 = max(w["x"] + w["w"] for w in words)
        y1 = max(w["y"] + w["h"] for w in words)

        lines.append(
            {
                "line_id": f"p{page_num}_l{idx + 1:03d}",
                "text": text,
                "type": "body",  # Tesseract has no heading detection
                "bbox": {
                    "x": x0 / W,
                    "y": y0 / H,
                    "w": (x1 - x0) / W,
                    "h": (y1 - y0) / H,
                },
            }
        )

    # Sort by vertical position
    lines.sort(key=lambda l: l["bbox"]["y"])
    return lines


# ---------------------------------------------------------------------------
# Zone OCR — caption and note zones OCR'd separately from masked pages
# ---------------------------------------------------------------------------

def _ocr_pil_image_lines(img, page_num: int, engine: str, surya_models) -> list[dict]:
    """OCR a PIL Image crop; return [{text, bbox}] with crop-local fractional coords."""
    import io
    W, H = img.size

    if engine == "surya":
        from surya.common.surya.schema import TaskNames
        det_predictor, rec_predictor = surya_models
        preds = rec_predictor(
            [img],
            task_names=[TaskNames.ocr_with_boxes],
            det_predictor=det_predictor,
        )
        lines = []
        for tl in preds[0].text_lines:
            x1, y1, x2, y2 = tl.bbox
            lines.append({
                "text": tl.text.strip(),
                "bbox": {"x": x1 / W, "y": y1 / H, "w": (x2 - x1) / W, "h": (y2 - y1) / H},
            })
        lines.sort(key=lambda l: l["bbox"]["y"])
        return lines

    elif engine == "tesseract":
        try:
            import pytesseract
            from collections import defaultdict
            data = pytesseract.image_to_data(
                img, lang=config.TESSERACT_LANG,
                output_type=pytesseract.Output.DICT,
            )
            buckets: dict = defaultdict(list)
            for i in range(len(data["text"])):
                word = data["text"][i].strip()
                conf = int(data["conf"][i])
                if not word or conf < 0:
                    continue
                key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
                buckets[key].append({
                    "word": word,
                    "x": data["left"][i], "y": data["top"][i],
                    "w": data["width"][i], "h": data["height"][i],
                })
            lines = []
            for key, words in sorted(buckets.items()):
                x0 = min(w["x"] for w in words)
                y0 = min(w["y"] for w in words)
                x1 = max(w["x"] + w["w"] for w in words)
                y1 = max(w["y"] + w["h"] for w in words)
                lines.append({
                    "text": " ".join(w["word"] for w in words),
                    "bbox": {"x": x0 / W, "y": y0 / H, "w": (x1 - x0) / W, "h": (y1 - y0) / H},
                })
            lines.sort(key=lambda l: l["bbox"]["y"])
            return lines
        except Exception as exc:
            logger.warning("Tesseract zone OCR failed: %s", exc)
            return []

    else:  # claude
        try:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            img_b64 = base64.standard_b64encode(buf.getvalue()).decode()
            client = get_client()
            response = client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64", "media_type": "image/png", "data": img_b64,
                        }},
                        {"type": "text", "text": _OCR_PROMPT},
                    ],
                }],
            )
            raw = response.content[0].text.strip()
            parsed = _parse_ocr_response(raw, page_num)
            return [{"text": l["text"], "bbox": l["bbox"]} for l in parsed if l["text"]]
        except Exception as exc:
            logger.warning("Claude zone OCR failed: %s", exc)
            return []


def _ocr_zone_crops(
    orig_img_path: Path,
    page_boxes: dict,
    page_num: int,
    engine: str,
    surya_models,
    line_id_offset: int,
) -> list[dict]:
    """OCR caption and note zone crops from the original (unmasked) page image.

    Returns fully-formed line dicts (line_id, text, type, bbox) with
    page-level fractional coordinates, ready to be merged into the page result.
    """
    from PIL import Image as PILImage

    caption_zones = page_boxes.get("captions", [])
    note_zones    = page_boxes.get("notes", [])
    all_zones = [(z, "caption") for z in caption_zones] + \
                [(z, "footnote") for z in note_zones]
    if not all_zones:
        return []

    orig_img = PILImage.open(orig_img_path).convert("RGB")
    W, H = orig_img.size

    result_lines: list[dict] = []
    line_counter = line_id_offset

    for zone_idx, (zone, line_type) in enumerate(all_zones):
        x0 = int(zone["x"] * W)
        y0 = int(zone["y"] * H)
        x1 = int((zone["x"] + zone["w"]) * W)
        y1 = int((zone["y"] + zone["h"]) * H)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(W, x1), min(H, y1)
        if x1 <= x0 or y1 <= y0:
            continue

        crop = orig_img.crop((x0, y0, x1, y1))
        raw_lines = _ocr_pil_image_lines(crop, page_num, engine, surya_models)

        for raw in raw_lines:
            if not raw.get("text", "").strip():
                continue
            lb = raw.get("bbox", {})
            # Convert crop-local fractional bbox → page-level fractional bbox
            page_bbox = {
                "x": zone["x"] + lb.get("x", 0) * zone["w"],
                "y": zone["y"] + lb.get("y", 0) * zone["h"],
                "w": lb.get("w", 1.0) * zone["w"],
                "h": lb.get("h", 0.02) * zone["h"],
            }
            line_counter += 1
            result_lines.append({
                "line_id": f"p{page_num}_z{zone_idx + 1}_l{line_counter:03d}",
                "text": raw["text"].strip(),
                "type": line_type,
                "bbox": page_bbox,
            })

    return result_lines


# ---------------------------------------------------------------------------
# Zone-based line classification (caption / note zones from boxes.json)
# ---------------------------------------------------------------------------

def _classify_zone_lines(lines: list[dict], page_boxes: dict) -> None:
    """
    Override the type of OCR lines whose centres fall within a zone defined
    in boxes.json.  Zones take precedence over whatever the OCR engine labelled.

    Heading zones  → type "heading1" / "heading2" / "heading3" (level from zone)
    Caption zones  → type "caption"  (attached to nearest figure by structure.py)
    Note zones     → type "footnote" (collected as endnotes by assembler.py)

    This is most useful for Surya/Tesseract output, which labels everything "body".
    Claude Vision usually classifies these correctly on its own, but zone overrides
    still apply so GUI-drawn zones take precedence.
    """
    heading_zones = page_boxes.get("headings", [])
    caption_zones = page_boxes.get("captions", [])
    note_zones    = page_boxes.get("notes", [])

    if not heading_zones and not caption_zones and not note_zones:
        return

    for line in lines:
        bbox = line.get("bbox", {})
        matched_heading = _matching_zone(bbox, heading_zones)
        if matched_heading is not None:
            level = int(matched_heading.get("level", 1))
            level = max(1, min(3, level))
            line["type"] = f"heading{level}"
        elif _centre_in_zones(bbox, caption_zones):
            line["type"] = "caption"
        elif _centre_in_zones(bbox, note_zones):
            line["type"] = "footnote"


def _matching_zone(bbox: dict, zones: list[dict]):
    """Return the first zone whose centre contains the line centre, or None."""
    if not zones:
        return None
    cx = bbox.get("x", 0) + bbox.get("w", 0) / 2
    cy = bbox.get("y", 0) + bbox.get("h", 0) / 2
    for zone in zones:
        zx, zy = zone.get("x", 0), zone.get("y", 0)
        zw, zh = zone.get("w", 0), zone.get("h", 0)
        if zx <= cx <= zx + zw and zy <= cy <= zy + zh:
            return zone
    return None


def _centre_in_zones(bbox: dict, zones: list[dict]) -> bool:
    """Return True if the bbox centre falls within any of the given zones."""
    cx = bbox.get("x", 0) + bbox.get("w", 0) / 2
    cy = bbox.get("y", 0) + bbox.get("h", 0) / 2
    for z in zones:
        if z["x"] <= cx <= z["x"] + z["w"] and z["y"] <= cy <= z["y"] + z["h"]:
            return True
    return False
