"""
assembler.py — Convert the structured document model into Markdown.

Rendering rules
---------------
- heading (level 1–3)  →  ``#`` / ``##`` / ``###``
- paragraph            →  plain text block
- figure               →  ``![alt](images/fig_x_y.png)``
- caption              →  *italic* line immediately after its figure
- footnote             →  collected and appended as a numbered endnote section
- other                →  plain text

Footnotes become endnotes
--------------------------
All footnote elements are stripped from their in-text positions and appended
at the end of the document under a ``## Notes`` heading.  Each footnote is
assigned a sequential number (regardless of its original page marker), and a
back-reference anchor is inserted at the point in the body text where the
footnote marker appeared.  (Because the OCR doesn't reliably identify inline
superscript markers, we insert a numbered reference ``[^1]`` after the
paragraph that immediately precedes each footnote group.)

Output
------
``project_dir/output/document.md``
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def assemble_markdown(
    project_dir: Path,
    elements: list[dict],
    force: bool = False,
) -> Path:
    """
    Render *elements* to Markdown and write to *project_dir/output/document.md*.

    Returns the output path.
    """
    output_dir = project_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "document.md"

    if out_path.exists() and not force:
        logger.info("document.md exists; skipping assemble step.")
        return out_path

    md = _render_markdown(elements, project_dir)
    out_path.write_text(md, encoding="utf-8")
    logger.info("Markdown written to %s (%d chars).", out_path, len(md))
    return out_path


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def _render_markdown(elements: list[dict], project_dir: Path) -> str:
    """Convert element list to a Markdown string."""
    lines: list[str] = []
    endnotes: list[dict] = []       # collected footnotes → endnotes
    endnote_counter = 0
    pending_footnotes: list[dict] = []   # footnotes since last paragraph

    # We need to know which element follows each paragraph so we can insert
    # endnote markers.  Build the output in two passes:
    # Pass 1: render everything except footnotes; track where footnote refs go.
    # Pass 2: append the endnote section.

    for el in elements:
        kind = el.get("kind")

        if kind == "heading":
            _flush_pending_footnotes(lines, endnotes, pending_footnotes)
            prefix = "#" * el.get("level", 1)
            lines.append(f"\n{prefix} {el['text'].strip()}\n")

        elif kind == "paragraph":
            text = el.get("text", "").strip()
            if text:
                lines.append(f"\n{text}")
                # Attach any pending footnote references after this paragraph
                for fn in pending_footnotes:
                    endnote_counter += 1
                    fn["_number"] = endnote_counter
                    endnotes.append(fn)
                    lines[-1] += f"[^{endnote_counter}]"
                pending_footnotes.clear()

        elif kind == "figure":
            _flush_pending_footnotes(lines, endnotes, pending_footnotes)
            alt = el.get("alt_text", "") or ""
            crop = el.get("crop_path", "")
            rel = f"../{crop}" if crop else ""
            lines.append(f"\n![{alt}]({rel})")

        elif kind == "table":
            _flush_pending_footnotes(lines, endnotes, pending_footnotes)
            content = el.get("content", "").strip()
            if content:
                if el.get("content_format") == "markdown":
                    lines.append(f"\n{content}")
                else:
                    # Preformatted (Tesseract plain text) — render as code block
                    lines.append(f"\n```\n{content}\n```")
            else:
                # No OCR content yet — fall back to image link
                crop = el.get("crop_path", "")
                rel = f"../{crop}" if crop else ""
                lines.append(f"\n![Table]({rel})")

        elif kind == "caption":
            text = el.get("text", "").strip()
            if text:
                # If the previous output line is already a caption (consecutive
                # captions), append to it rather than starting a new italic block.
                if lines and lines[-1].startswith("\n*") and lines[-1].endswith("*"):
                    lines[-1] = lines[-1][:-1] + " " + text + "*"
                else:
                    lines.append(f"\n*{text}*")

        elif kind == "footnote":
            # Don't render inline; queue for endnote placement
            pending_footnotes.append(dict(el))

        else:
            text = el.get("text", "").strip()
            if text:
                lines.append(f"\n{text}")

    # Flush any trailing footnotes
    _flush_pending_footnotes(lines, endnotes, pending_footnotes)

    # Append endnotes section
    if endnotes:
        lines.append("\n\n---\n\n## Notes\n")
        for fn in endnotes:
            num = fn.get("_number", "?")
            text = fn.get("text", "").strip()
            lines.append(f"\n[^{num}]: {text}")

    return "\n".join(lines).strip() + "\n"


def _flush_pending_footnotes(
    lines: list[str],
    endnotes: list[dict],
    pending: list[dict],
) -> None:
    """
    If there are pending footnotes and no paragraph was emitted to attach
    them to, insert a placeholder reference and move them to endnotes.
    """
    if not pending:
        return
    # No preceding paragraph: attach ref to last non-empty line
    for fn in pending:
        num = len(endnotes) + 1
        fn["_number"] = num
        endnotes.append(fn)
        if lines:
            lines[-1] += f"[^{num}]"
    pending.clear()
