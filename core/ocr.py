"""
ocr.py — Extract text from masked page images.

Supported engines
-----------------
- Surya  (default when installed; better accuracy, handles math and multilingual)
- Tesseract  (lightweight fallback, no GPU required)
- Tesseract legacy  (pre-LSTM pattern-matching engine, --oem 0; weaker baseline)
- GOCR  (2000s-era engine; output kept as-is, "_" marks unrecognised characters)
- Ocrad  (GNU 2000s-era engine; output kept as-is, "_" marks unrecognised characters)
- Ollama  (large vision model, e.g. GLM-OCR or Qwen3-VL, served by a local Ollama;
  run once per layout region, with line boxes spread evenly within each region)
- vLLM  (the same, against any OpenAI-compatible server — e.g. vLLM on a GPU
  server; pages are OCR'd in parallel so the server can batch requests)

Output — ocr_raw.json
---------------------
A list of page results, one per page:

.. code-block:: json

    [
        {
            "page_number": 1,
            "engine": "surya",
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
When Tesseract is used, bboxes are derived from its word-level data and
every line is typed as "body" (heading detection happens later in structure.py).
"""

from __future__ import annotations

import html
import json
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import config
from core.layout_analyzer import ensure_paragraph_boxes

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_ocr(
    project_dir: Path,
    page_records: list[dict],
    engine: str = "tesseract",
    force: bool = False,
) -> list[dict]:
    """
    Run OCR on all pages and write *project_dir/ocr_raw.json*.

    *engine* is ``"surya"``, ``"tesseract"``, ``"tesseract-legacy"``, ``"gocr"``, ``"ocrad"``, ``"ollama"``, or ``"vllm"``.
    After page OCR, table crops (if any) are also OCR'd and saved to tables.json.

    Returns the list of page OCR results.
    """
    ocr_path = project_dir / "ocr_raw.json"

    if ocr_path.exists() and not force:
        logger.info("ocr_raw.json exists; skipping OCR step.")
        return json.loads(ocr_path.read_text())

    if engine == "tesseract-legacy":
        _check_legacy_tessdata()
    if engine in ("gocr", "ocrad") and shutil.which(engine) is None:
        raise RuntimeError(f"{engine} is not installed (macOS: brew install {engine}).")
    if engine == "ollama":
        _check_ollama()
    if engine == "vllm":
        _check_vllm()

    # Remove stale edited file so downstream steps and the editor use fresh output
    edited_path = project_dir / "ocr_edited.json"
    if force and edited_path.exists():
        edited_path.unlink()
        logger.info("Removed stale ocr_edited.json.")

    # For Surya OCR, ensure paragraph boxes exist in boxes.json so that
    # structure.py can group lines into paragraphs correctly.
    # The vision-model engines also need them: they OCR one paragraph/heading region at a time.
    if engine == "surya":
        ensure_paragraph_boxes(project_dir, page_records)
    elif engine in _VLM_ENGINES:
        try:
            ensure_paragraph_boxes(project_dir, page_records)
        except (RuntimeError, ImportError) as exc:   # e.g. Surya not installed on a GPU server
            logger.warning("No paragraph regions (%s); %s will OCR whole pages.", exc, engine)

    # Load caption/notes zones from boxes.json for line classification
    boxes_path = project_dir / "boxes.json"
    boxes = json.loads(boxes_path.read_text()) if boxes_path.exists() else {}

    # Pre-load Surya models once if needed (expensive)
    surya_models = _load_surya_ocr_models() if engine == "surya" else None

    def ocr_page(rec: dict) -> dict:
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
        page_boxes = boxes.get("pages", {}).get(str(page_num), {})

        if engine in _VLM_ENGINES:
            page_result = _ocr_vlm_page(engine, img_path, page_num, page_boxes)
        elif engine == "surya":
            page_result = _ocr_surya(img_path, page_num, surya_models)
        elif engine == "gocr":
            page_result = _ocr_gocr(img_path, page_num)
        elif engine == "ocrad":
            page_result = _ocr_ocrad(img_path, page_num)
        else:
            page_result = _ocr_tesseract(img_path, page_num, engine)

        # Classify main-page lines that overlap caption/note/heading zones
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

        return page_result

    if engine == "vllm" and config.VLLM_CONCURRENCY > 1:
        # vLLM batches concurrent requests on the GPU; map() keeps page order.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=config.VLLM_CONCURRENCY) as pool:
            results = list(pool.map(ocr_page, page_records))
    else:
        results = [ocr_page(rec) for rec in page_records]

    ocr_path.write_text(json.dumps(results, indent=2))
    logger.info("OCR complete. %d pages processed.", len(results))

    # OCR table crops
    run_table_ocr(project_dir, engine=engine, force=force)

    return results


def run_table_ocr(
    project_dir: Path,
    engine: str = "tesseract",
    force: bool = False,
) -> list[dict]:
    """
    OCR table crops listed in tables.json and store the result in each record.

    Uses Surya's TableRecPredictor for structure-aware Markdown table output.
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

    def ocr_table(rec: dict) -> None:
        if rec.get("content") and not force:
            return
        crop_path = project_dir / rec["crop_path"]
        if not crop_path.exists():
            logger.warning("Table crop not found: %s", crop_path)
            return

        logger.info("    OCR table %s …", rec["id"])
        if engine in _VLM_ENGINES:
            content, fmt = _ocr_table_vlm(engine, crop_path)
        elif engine == "surya":
            content, fmt = _ocr_table_surya(crop_path, surya_models)
        elif engine == "gocr":
            content, fmt = _ocr_table_gocr(crop_path)
        elif engine == "ocrad":
            content, fmt = _ocr_table_ocrad(crop_path)
        else:
            content, fmt = _ocr_table_tesseract(crop_path, engine)

        rec["content"] = content
        rec["content_format"] = fmt

    if engine == "vllm" and config.VLLM_CONCURRENCY > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=config.VLLM_CONCURRENCY) as pool:
            list(pool.map(ocr_table, table_records))
    else:
        for rec in table_records:
            ocr_table(rec)

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
                grid[r][c] = text.replace("|", "\\|")

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


def _ocr_table_tesseract(crop_path: Path, engine: str = "tesseract") -> tuple[str, str]:
    """OCR a table crop with Tesseract, returning preformatted text."""
    try:
        import pytesseract
        from PIL import Image

        img = Image.open(crop_path)
        text = pytesseract.image_to_string(
            img, lang=config.TESSERACT_LANG, config=_tesseract_config(engine),
        ).strip()
        return text, "preformatted"
    except Exception as exc:
        logger.warning("Tesseract table OCR failed for %s: %s", crop_path.name, exc)
        return "", ""


# ---------------------------------------------------------------------------
# Tesseract engine
# ---------------------------------------------------------------------------

def _tesseract_config(engine: str) -> str:
    """Extra Tesseract CLI flags: legacy engine (--oem 0) uses its own tessdata folder."""
    if engine != "tesseract-legacy":
        return ""
    return f'--oem 0 --tessdata-dir "{config.TESSERACT_LEGACY_TESSDATA}"'


def _check_legacy_tessdata() -> None:
    """Fail fast rather than silently producing empty OCR on every page."""
    tessdata = Path(config.TESSERACT_LEGACY_TESSDATA) if config.TESSERACT_LEGACY_TESSDATA else None
    langs = config.TESSERACT_LANG.split("+")
    missing = [l for l in langs if tessdata is None or not (tessdata / f"{l}.traineddata").exists()]
    if missing:
        raise RuntimeError(
            "The tesseract-legacy engine needs legacy traineddata files. Set "
            "TESSERACT_LEGACY_TESSDATA in .env to a folder containing "
            + ", ".join(f"{l}.traineddata" for l in missing)
            + " (download from https://github.com/tesseract-ocr/tessdata)."
        )


def _ocr_tesseract(img_path: Path, page_num: int, engine: str = "tesseract") -> dict:
    """Run Tesseract on a page image and return a page result dict."""
    try:
        import pytesseract
        from PIL import Image

        img = Image.open(img_path)
        data = pytesseract.image_to_data(
            img,
            lang=config.TESSERACT_LANG,
            config=_tesseract_config(engine),
            output_type=pytesseract.Output.DICT,
        )
        W, H = img.size
        lines = _tesseract_data_to_lines(data, W, H, page_num)
        return {"page_number": page_num, "engine": engine, "lines": lines}
    except ImportError:
        logger.error("pytesseract is not installed. Install it with: pip install pytesseract")
        return {"page_number": page_num, "engine": engine, "lines": []}
    except Exception as exc:
        logger.error("Tesseract error on page %d: %s", page_num, exc)
        return {"page_number": page_num, "engine": engine, "lines": []}


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
                "type": "body",
                "bbox": {
                    "x": x0 / W,
                    "y": y0 / H,
                    "w": (x1 - x0) / W,
                    "h": (y1 - y0) / H,
                },
            }
        )

    lines.sort(key=lambda l: l["bbox"]["y"])
    return lines


# ---------------------------------------------------------------------------
# GOCR engine
# ---------------------------------------------------------------------------
# Output is deliberately kept as GOCR produced it (unrecognised characters
# stay as "_", junk lines are kept) so it represents historical OCR quality.

_GOCR_LINE_RE = re.compile(r"<line\b([^>]*)>(.*?)</line>", re.S)
_GOCR_ITEM_RE = re.compile(r"<(box|space)\b([^>]*?)/>")
_GOCR_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


def _run_gocr(img, xml: bool) -> str:
    """Run gocr on a PIL image (sent as greyscale PGM); return its stdout."""
    with tempfile.NamedTemporaryFile(suffix=".pgm", delete=False) as f:
        tmp_path = f.name
    try:
        img.convert("L").save(tmp_path)
        cmd = ["gocr"] + (["-f", "XML"] if xml else []) + ["-i", tmp_path]
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
        if proc.stderr:
            logger.debug("gocr: %s", proc.stderr.decode("utf-8", errors="replace").strip())
        return proc.stdout.decode("utf-8", errors="replace")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _parse_gocr_xml(xml_text: str) -> list[dict]:
    """Parse gocr -f XML output into [{text, x0, y0, x1, y1}] in pixel coords.

    Each <space> becomes one space (matching gocr's plain-text output).  When
    the <line> box is invalid (gocr sometimes reports negative dx/dy), the
    union of the line's character boxes is used instead; lines with no usable
    geometry are dropped.
    """
    result = []
    for line_m in _GOCR_LINE_RE.finditer(xml_text):
        line_attrs = {k: int(v) for k, v in _GOCR_ATTR_RE.findall(line_m.group(1))
                      if v.lstrip("-").isdigit()}
        chars: list[str] = []
        boxes: list[tuple[int, int, int, int]] = []
        for tag, attr_text in _GOCR_ITEM_RE.findall(line_m.group(2)):
            attrs = dict(_GOCR_ATTR_RE.findall(attr_text))
            if tag == "space":
                chars.append(" ")
                continue
            chars.append(html.unescape(attrs.get("value", "")))
            try:
                x, y, dx, dy = (int(attrs[k]) for k in ("x", "y", "dx", "dy"))
            except (KeyError, ValueError):
                continue
            if dx > 0 and dy > 0:
                boxes.append((x, y, x + dx, y + dy))

        text = "".join(chars).strip()
        if not text:
            continue

        lx, ly = line_attrs.get("x", 0), line_attrs.get("y", 0)
        ldx, ldy = line_attrs.get("dx", 0), line_attrs.get("dy", 0)
        if ldx > 0 and ldy > 0:
            x0, y0, x1, y1 = lx, ly, lx + ldx, ly + ldy
        elif boxes:
            x0 = min(b[0] for b in boxes)
            y0 = min(b[1] for b in boxes)
            x1 = max(b[2] for b in boxes)
            y1 = max(b[3] for b in boxes)
        else:
            logger.debug("gocr line dropped (no usable geometry): %r", text)
            continue
        result.append({"text": text, "x0": x0, "y0": y0, "x1": x1, "y1": y1})
    return result


def _pixel_lines_to_frac(parsed: list[dict], W: int, H: int) -> list[dict]:
    """Convert [{text, x0, y0, x1, y1}] pixel lines to [{text, bbox}] fractions, sorted by y."""
    def frac(v: float, total: int) -> float:
        return max(0.0, min(1.0, v / total))

    lines = []
    for l in parsed:
        x0, y0 = frac(l["x0"], W), frac(l["y0"], H)
        x1, y1 = frac(l["x1"], W), frac(l["y1"], H)
        lines.append({"text": l["text"],
                      "bbox": {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}})
    lines.sort(key=lambda l: l["bbox"]["y"])
    return lines


def _gocr_lines(img) -> list[dict]:
    """OCR a PIL image with gocr; return [{text, bbox}] with fractional coords."""
    return _pixel_lines_to_frac(_parse_gocr_xml(_run_gocr(img, xml=True)), *img.size)


def _ocr_gocr(img_path: Path, page_num: int) -> dict:
    """Run GOCR on a page image and return a page result dict."""
    try:
        from PIL import Image
        lines = [
            {"line_id": f"p{page_num}_l{i + 1:03d}", "type": "body", **l}
            for i, l in enumerate(_gocr_lines(Image.open(img_path)))
        ]
        return {"page_number": page_num, "engine": "gocr", "lines": lines}
    except Exception as exc:
        logger.error("GOCR error on page %d: %s", page_num, exc)
        return {"page_number": page_num, "engine": "gocr", "lines": []}


def _ocr_table_gocr(crop_path: Path) -> tuple[str, str]:
    """OCR a table crop with GOCR's plain-text output (preformatted)."""
    try:
        from PIL import Image
        return _run_gocr(Image.open(crop_path), xml=False).strip("\n"), "preformatted"
    except Exception as exc:
        logger.warning("GOCR table OCR failed for %s: %s", crop_path.name, exc)
        return "", ""


# ---------------------------------------------------------------------------
# Ocrad engine
# ---------------------------------------------------------------------------
# Like GOCR, output is kept as Ocrad produced it ("_" for unrecognised characters).

_ORF_LINE_RE = re.compile(r"^line\s+\d+\s+chars\s+(\d+)\s+height\s+\d+")
_ORF_CHAR_RE = re.compile(r"^\s*(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+);\s*(\d+)(.*)$")
_ORF_GUESS_RE = re.compile(r"'(.)'")


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _run_ocrad(img) -> tuple[str, str]:
    """Run ocrad on a PIL image (sent as greyscale PGM); return (text, ORF results)."""
    with tempfile.TemporaryDirectory() as tmp:
        pgm = Path(tmp) / "page.pgm"
        orf = Path(tmp) / "page.orf"
        img.convert("L").save(pgm)
        proc = subprocess.run(
            ["ocrad", "--format=utf8", "-x", str(orf), str(pgm)],
            capture_output=True, timeout=300,
        )
        if proc.returncode != 0:
            raise RuntimeError(_decode(proc.stderr).strip() or f"ocrad exit {proc.returncode}")
        return _decode(proc.stdout), _decode(orf.read_bytes()) if orf.exists() else ""


def _parse_ocrad_orf(orf_text: str) -> list[dict]:
    """Parse an Ocrad ORF file into [{text, x0, y0, x1, y1}] in pixel coords.

    Each character entry is "x y w h; n_guesses, 'c'conf, …"; n_guesses == 0
    becomes "_" (as in Ocrad's text output).  The line box is the union of the
    line's non-space character boxes.
    """
    result = []
    rows = orf_text.splitlines()
    i = 0
    while i < len(rows):
        header = _ORF_LINE_RE.match(rows[i])
        i += 1
        if not header:
            continue
        chars: list[str] = []
        boxes: list[tuple[int, int, int, int]] = []
        for row in rows[i:i + int(header.group(1))]:
            m = _ORF_CHAR_RE.match(row)
            if not m:
                continue
            x, y, w, h, n_guesses = (int(g) for g in m.groups()[:5])
            guess = _ORF_GUESS_RE.search(m.group(6)) if n_guesses else None
            ch = guess.group(1) if guess else "_"
            chars.append(ch)
            if ch != " " and w > 0 and h > 0:
                boxes.append((x, y, x + w, y + h))
        i += int(header.group(1))

        text = "".join(chars).strip()
        if not text or not boxes:
            continue
        result.append({
            "text": text,
            "x0": min(b[0] for b in boxes), "y0": min(b[1] for b in boxes),
            "x1": max(b[2] for b in boxes), "y1": max(b[3] for b in boxes),
        })
    return result


def _ocrad_lines(img) -> list[dict]:
    """OCR a PIL image with ocrad; return [{text, bbox}] with fractional coords."""
    _, orf = _run_ocrad(img)
    return _pixel_lines_to_frac(_parse_ocrad_orf(orf), *img.size)


def _ocr_ocrad(img_path: Path, page_num: int) -> dict:
    """Run Ocrad on a page image and return a page result dict."""
    try:
        from PIL import Image
        lines = [
            {"line_id": f"p{page_num}_l{i + 1:03d}", "type": "body", **l}
            for i, l in enumerate(_ocrad_lines(Image.open(img_path)))
        ]
        return {"page_number": page_num, "engine": "ocrad", "lines": lines}
    except Exception as exc:
        logger.error("Ocrad error on page %d: %s", page_num, exc)
        return {"page_number": page_num, "engine": "ocrad", "lines": []}


def _ocr_table_ocrad(crop_path: Path) -> tuple[str, str]:
    """OCR a table crop with Ocrad's plain-text output (preformatted)."""
    try:
        from PIL import Image
        text, _ = _run_ocrad(Image.open(crop_path))
        return text.strip("\n"), "preformatted"
    except Exception as exc:
        logger.warning("Ocrad table OCR failed for %s: %s", crop_path.name, exc)
        return "", ""


# ---------------------------------------------------------------------------
# Vision-language model engines (Ollama locally, vLLM on a GPU server)
# ---------------------------------------------------------------------------
# These models return text for a whole image rather than positioned lines, so
# each layout region is sent separately and its returned lines are given
# equal-height slices of the region as approximate boxes.  The two engines
# differ only in how a request reaches the model (_vlm_generate).

_VLM_ENGINES = frozenset({"ollama", "vllm"})

_GENERIC_TEXT_PROMPT = (
    "Transcribe all text in this image exactly as written, one output line per "
    "printed line. Keep original spelling and punctuation. Output only the text."
)
_GENERIC_TABLE_PROMPT = "Transcribe this table as a Markdown table. Output only the table."
_FENCE_RE = re.compile(r"^```[\w-]*\n(.*?)\n?```$", re.S)
_THINK_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.S)        # reasoning ("Thinking") variants
_FRONT_MATTER_RE = re.compile(r"^---\n.*?\n---\n?", re.S)        # e.g. olmOCR's YAML header


def _vlm_model(engine: str) -> str:
    return config.VLLM_MODEL if engine == "vllm" else config.OLLAMA_MODEL


def _is_glm_ocr(engine: str) -> bool:
    # Ollama: "glm-ocr[:tag]"; vLLM: Hugging Face id such as "zai-org/GLM-OCR"
    return _vlm_model(engine).split(":")[0].lower().endswith("glm-ocr")


def _vlm_prompt(engine: str, table: bool = False) -> str:
    custom = config.VLLM_PROMPT if engine == "vllm" else config.OLLAMA_PROMPT
    if not table and custom:
        return custom
    if _is_glm_ocr(engine):
        return "Table Recognition:" if table else "Text Recognition:"
    return _GENERIC_TABLE_PROMPT if table else _GENERIC_TEXT_PROMPT


def _http_json(url: str, payload: dict | None = None, timeout: int = 10, headers: dict | None = None):
    """GET (payload None) or POST JSON to *url*; return the decoded response."""
    import urllib.request
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _host_url(host: str, path: str) -> str:
    host = host.rstrip("/")
    if "://" not in host:
        host = "http://" + host
    return host + path


def _png_b64(img) -> str:
    import base64
    import io
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


# -- Ollama -------------------------------------------------------------------

def _check_ollama() -> None:
    """Fail fast if the Ollama server is unreachable or the model isn't pulled."""
    model = config.OLLAMA_MODEL
    try:
        tags = _http_json(_host_url(config.OLLAMA_HOST, "/api/tags"))
        names = [m["name"] for m in tags.get("models", [])]
    except Exception as exc:
        raise RuntimeError(
            f"Cannot reach Ollama at {config.OLLAMA_HOST} ({exc}). "
            "Start the Ollama app, or run: ollama serve"
        )
    wanted = model if ":" in model else f"{model}:latest"
    if model not in names and wanted not in names:
        raise RuntimeError(f"Ollama model '{model}' is not pulled. Run: ollama pull {model}")


def _ollama_generate(img, prompt: str) -> str:
    resp = _http_json(_host_url(config.OLLAMA_HOST, "/api/generate"), {
        "model": config.OLLAMA_MODEL,
        "prompt": prompt,
        "images": [_png_b64(img)],
        "stream": False,
        "options": {"temperature": 0},
    }, timeout=config.OLLAMA_TIMEOUT)
    return resp.get("response", "")


# -- vLLM (any OpenAI-compatible server) --------------------------------------

def _vllm_headers() -> dict:
    return {"Authorization": f"Bearer {config.VLLM_API_KEY}"} if config.VLLM_API_KEY else {}


def _check_vllm() -> None:
    """Fail fast if the server is unreachable; pick or verify the served model."""
    try:
        listing = _http_json(_host_url(config.VLLM_HOST, "/v1/models"), headers=_vllm_headers())
        served = [m["id"] for m in listing.get("data", [])]
    except Exception as exc:
        raise RuntimeError(
            f"Cannot reach a vLLM / OpenAI-compatible server at {config.VLLM_HOST} ({exc}). "
            "Start one with e.g.: vllm serve Qwen/Qwen3-VL-8B-Instruct"
        )
    if not config.VLLM_MODEL:
        if len(served) != 1:
            raise RuntimeError(
                f"The server serves {len(served)} models ({', '.join(served) or 'none'}); "
                "choose one with --vllm-model or VLLM_MODEL."
            )
        config.VLLM_MODEL = served[0]
    elif config.VLLM_MODEL not in served:
        raise RuntimeError(
            f"Model '{config.VLLM_MODEL}' is not served at {config.VLLM_HOST} "
            f"(serving: {', '.join(served) or 'none'})."
        )
    logger.info("vLLM model: %s (%d pages in parallel)", config.VLLM_MODEL, config.VLLM_CONCURRENCY)


def _vllm_generate(img, prompt: str) -> str:
    resp = _http_json(_host_url(config.VLLM_HOST, "/v1/chat/completions"), {
        "model": config.VLLM_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_png_b64(img)}"}},
            {"type": "text", "text": prompt},
        ]}],
        "temperature": 0,
        "max_tokens": config.VLLM_MAX_TOKENS,
    }, timeout=config.VLLM_TIMEOUT, headers=_vllm_headers())
    choice = resp["choices"][0]
    if choice.get("finish_reason") == "length":
        logger.warning("vLLM output hit VLLM_MAX_TOKENS (%d); region text may be truncated "
                       "or the model may have looped.", config.VLLM_MAX_TOKENS)
    return choice["message"].get("content") or ""


# -- Shared -------------------------------------------------------------------

def _vlm_generate(engine: str, img, prompt: str) -> str:
    """Send one image + prompt to the engine's server; return the model's text, unwrapped."""
    text = (_vllm_generate if engine == "vllm" else _ollama_generate)(img, prompt).strip()
    text = _THINK_RE.sub("", text)
    text = _FRONT_MATTER_RE.sub("", text).strip()
    fenced = _FENCE_RE.match(text)
    return fenced.group(1).strip() if fenced else text


def _is_blank(img) -> bool:
    """True if a crop has no dark pixels (e.g. fully masked) — VLMs invent text for these."""
    return img.convert("L").getextrema()[0] > 200


def _vlm_region_lines(engine: str, img, region: dict) -> list[dict]:
    """OCR one region of *img* (fractional box); return lines with approximate boxes.

    Returned bboxes are fractions of *img*; each line gets an equal-height slice
    of the region, in the order the model returned them.
    """
    W, H = img.size
    x0, y0 = int(region["x"] * W), int(region["y"] * H)
    x1, y1 = int((region["x"] + region["w"]) * W), int((region["y"] + region["h"]) * H)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return []
    crop = img.crop((x0, y0, x1, y1))
    if _is_blank(crop):
        return []
    try:
        text = _vlm_generate(engine, crop, _vlm_prompt(engine))
    except Exception as exc:
        logger.warning("%s OCR failed for a region: %s", engine, exc)
        return []

    texts = [t.strip() for t in text.splitlines() if t.strip()]
    slice_h = region["h"] / len(texts) if texts else 0
    return [
        {"text": t,
         "bbox": {"x": region["x"], "y": region["y"] + i * slice_h,
                  "w": region["w"], "h": slice_h}}
        for i, t in enumerate(texts)
    ]


def _ocr_vlm_page(engine: str, img_path: Path, page_num: int, page_boxes: dict) -> dict:
    """OCR a masked page one paragraph/heading region at a time."""
    from PIL import Image

    img = Image.open(img_path).convert("RGB")
    regions = list(page_boxes.get("paragraphs", [])) + list(page_boxes.get("headings", []))
    if not regions:
        logger.info("    page %d: no paragraph/heading regions; sending the whole page.", page_num)
        regions = [{"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}]

    lines: list[dict] = []
    for region in sorted(regions, key=lambda r: (r["y"], r["x"])):
        lines.extend(_vlm_region_lines(engine, img, region))
    lines.sort(key=lambda l: l["bbox"]["y"])
    for i, line in enumerate(lines):
        line["line_id"] = f"p{page_num}_l{i + 1:03d}"
        line["type"] = "body"
    return {"page_number": page_num, "engine": f"{engine}:{_vlm_model(engine)}", "lines": lines}


def _ocr_table_vlm(engine: str, crop_path: Path) -> tuple[str, str]:
    """OCR a table crop; Markdown if every line looks like a table row."""
    try:
        from PIL import Image
        text = _vlm_generate(engine, Image.open(crop_path), _vlm_prompt(engine, table=True))
        rows = [r for r in text.splitlines() if r.strip()]
        is_md = bool(rows) and all(r.lstrip().startswith("|") for r in rows)
        return text, "markdown" if is_md else "preformatted"
    except Exception as exc:
        logger.warning("%s table OCR failed for %s: %s", engine, crop_path.name, exc)
        return "", ""


# ---------------------------------------------------------------------------
# Zone OCR — caption and note zones OCR'd separately from masked pages
# ---------------------------------------------------------------------------

def _ocr_pil_image_lines(img, page_num: int, engine: str, surya_models) -> list[dict]:
    """OCR a PIL Image crop; return [{text, bbox}] with crop-local fractional coords."""
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

    elif engine == "gocr":
        try:
            return _gocr_lines(img)
        except Exception as exc:
            logger.warning("GOCR zone OCR failed: %s", exc)
            return []

    elif engine == "ocrad":
        try:
            return _ocrad_lines(img)
        except Exception as exc:
            logger.warning("Ocrad zone OCR failed: %s", exc)
            return []

    elif engine in _VLM_ENGINES:
        return _vlm_region_lines(engine, img, {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0})

    else:  # tesseract
        try:
            import pytesseract
            from collections import defaultdict
            data = pytesseract.image_to_data(
                img, lang=config.TESSERACT_LANG,
                config=_tesseract_config(engine),
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
