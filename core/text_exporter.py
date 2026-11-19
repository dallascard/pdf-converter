"""
text_exporter.py — Export per-page text as a simple JSON array.

Reads the OCR results (edited if available, raw otherwise) and produces a
JSON file where each entry contains a page_number and the headings + body
text for that page joined with newlines.  Figures, tables, captions, and
footnotes are excluded.

Output schema
-------------
.. code-block:: json

    [
        {"page_number": 1, "text": "Heading\\n\\nFirst paragraph text..."},
        {"page_number": 2, "text": "..."}
    ]
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_BODY_TYPES = frozenset({"body", "heading1", "heading2", "heading3"})
_NOTE_TYPES  = frozenset({"footnote"})


def export_text_json(
    project_dir: Path,
    page_records: list[dict],
    output_path: Path,
    force: bool = False,
) -> None:
    """Write per-page heading + body + footnote text to *output_path* as JSON.

    Uses ``ocr_edited.json`` when present (user corrections), otherwise falls
    back to ``ocr_raw.json``.  Captions, figures, and tables are excluded.

    Footnotes are always appended after body/heading text, regardless of their
    y-position on the page (handles side-notes as well as bottom footnotes).
    """
    if output_path.exists() and not force:
        logger.info("%s already exists; skipping (use --force to overwrite).", output_path.name)
        return

    from core.ocr import load_ocr

    ocr_results = load_ocr(project_dir, edited=True)
    ocr_by_page: dict[int, dict] = {r["page_number"]: r for r in ocr_results}

    pages: list[dict] = []
    for rec in page_records:
        page_num = rec["page_number"]
        result = ocr_by_page.get(page_num, {})
        all_lines = result.get("lines", [])

        body_lines = [
            line["text"] for line in all_lines
            if line.get("type") in _BODY_TYPES and line.get("text", "").strip()
        ]
        note_lines = [
            line["text"] for line in all_lines
            if line.get("type") in _NOTE_TYPES and line.get("text", "").strip()
        ]

        pages.append({
            "page_number": page_num,
            "text": "\n".join(body_lines + note_lines),
        })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(pages, indent=2, ensure_ascii=False))
    logger.info("Text JSON written: %s (%d pages).", output_path, len(pages))
