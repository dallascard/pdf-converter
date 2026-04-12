"""
assembler.py — Convert the structured document model into Markdown.

Rendering rules
---------------
- heading (level 1–3)  →  ``#`` / ``##`` / ``###``
- paragraph            →  plain text block
- figure + caption     →  ``<figure><img …><figcaption>…</figcaption></figure>``
- figure (no caption)  →  ``<figure><img …></figure>``
- footnote             →  collected and appended as a numbered endnote section
- other                →  plain text

Footnotes become endnotes
--------------------------
All footnote elements are stripped from their in-text positions and appended
at the end of the document under a ``## Notes`` heading.  Numeric footnote
markers (1, 2, 3 …) are used directly as the endnote number so that inline
``[^N]`` references in the body text match ``[^N]: …`` entries in the Notes
section.  Non-numeric markers (†, ‡, …) are assigned sequential numbers.

Inline marker detection: paragraph text is scanned for likely superscript
markers — digits attached directly to the preceding word (``word1``) or
separated from punctuation by a single space (``, 1 more``).  Only digits
that match a known footnote marker are replaced, limiting false positives.
When no inline marker is found in a paragraph, the reference is appended at
the end of the paragraph as a fallback.

Output
------
``project_dir/output/document.md``
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inline footnote-marker detection
# ---------------------------------------------------------------------------

# Pattern S: Surya explicit superscript markup — highest confidence.
# Surya sometimes emits "<sup>N</sup>" when it detects a superscript character.
# Always converted regardless of known_markers.
_MARKER_SUP_TAG = re.compile(r'<sup>(\d{1,2})</sup>')

# Pattern A1: digit(s) attached directly after a letter or closing quote mark.
# Catches:  "structures2 "  "word1,"  'asf"5 '  "quoted\u201d3 "
# Avoids:   "CO2" only if "2" is not a known marker (enforced at call time).
# Closing quotes included: ASCII "  Unicode \u201d (")  \u2019 (')
_MARKER_AFTER_LETTER = re.compile(
    r'(?<=[a-zA-Z\u201d\u2019"])(\d{1,2})(?=[\s,;:.!?]|$)',
    re.MULTILINE,
)

# Pattern A2: digit(s) attached directly after a punctuation mark, but only
# when the character before that punctuation is a letter (2-char lookbehind).
# Catches:  "text.1 "  "phrase,2 "
# Avoids:   "11.1 " — the '.' is preceded by '1' (a digit), not a letter.
_MARKER_AFTER_PUNCT = re.compile(
    r'(?<=[a-zA-Z][,.;:.!?])(\d{1,2})(?=[\s,;:.!?]|$)',
    re.MULTILINE,
)

# Pattern B: digit(s) separated from a punctuation mark by a single space.
# Catches:  ", 1 and"  ". 2 The"
# Group 1 = punctuation, Group 2 = digit(s).
_MARKER_SPACED = re.compile(
    r'([,.;:.!?])\s+(\d{1,2})(?=\s)',
    re.MULTILINE,
)


def _markup_inline_footnote_refs(text: str, known_markers: set[str]) -> tuple[str, set[str]]:
    """Replace likely inline footnote markers in *text* with ``[^N]`` references.

    Only replaces digit strings that appear in *known_markers* (the set of
    footnote markers collected from the document's footnote elements).  This
    limits false positives to cases where the document actually has a footnote
    with that number.

    Returns ``(modified_text, markers_found)`` where *markers_found* is the
    set of marker strings that were replaced.
    """
    found: set[str] = set()

    def _sub_sup(m: re.Match) -> str:
        marker = m.group(1)
        found.add(marker)
        return f"[^{marker}]"

    def _sub_digit(m: re.Match) -> str:
        marker = m.group(1)
        if marker in known_markers:
            found.add(marker)
            return f"[^{marker}]"
        return m.group(0)

    def _sub_spaced(m: re.Match) -> str:
        marker = m.group(2)
        if marker in known_markers:
            found.add(marker)
            return f"{m.group(1)}[^{marker}]"
        return m.group(0)

    text = _MARKER_SUP_TAG.sub(_sub_sup, text)        # highest confidence, first
    text = _MARKER_AFTER_LETTER.sub(_sub_digit, text)
    text = _MARKER_AFTER_PUNCT.sub(_sub_digit, text)
    text = _MARKER_SPACED.sub(_sub_spaced, text)
    return text, found


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
    endnotes: list[dict] = []            # collected footnotes → endnotes
    endnote_counter = 0                  # fallback counter for non-numeric markers
    pending_footnotes: list[dict] = []   # footnotes since last paragraph
    pending_figure: dict | None = None   # figure element buffered until caption decision
    pending_captions: list[str] = []     # caption texts accumulated for pending_figure

    # Pre-collect the set of numeric footnote markers present in this document.
    # Used to constrain inline marker detection to digits that actually correspond
    # to a footnote, reducing false positives.
    known_markers: set[str] = {
        el["marker"]
        for el in elements
        if el.get("kind") == "footnote" and el.get("marker", "").isdigit()
    }

    def _endnote_number(fn: dict) -> str | int:
        """Return the endnote reference number/label for a footnote element.

        Numeric markers are used as-is so that inline ``[^1]`` references
        match ``[^1]: …`` in the Notes section.  Non-numeric markers (†, ‡, …)
        fall back to a sequential counter.
        """
        nonlocal endnote_counter
        marker = fn.get("marker", "")
        if marker.isdigit():
            return int(marker)
        endnote_counter += 1
        return endnote_counter

    def _flush_figure() -> None:
        """Emit the buffered figure element, with or without caption."""
        nonlocal pending_figure
        if pending_figure is None:
            return
        alt = pending_figure.get("alt_text", "") or ""
        crop = pending_figure.get("crop_path", "")
        rel = f"../{crop}" if crop else ""
        if pending_captions:
            cap_text = " ".join(pending_captions)
            lines.append(
                f"\n<figure>\n"
                f'<img src="{_html.escape(rel, quote=True)}"'
                f' alt="{_html.escape(alt, quote=True)}">\n'
                f"<figcaption>{_html.escape(cap_text)}</figcaption>\n"
                f"</figure>"
            )
        else:
            lines.append(
                f"\n<figure>\n"
                f'<img src="{_html.escape(rel, quote=True)}"'
                f' alt="{_html.escape(alt, quote=True)}">\n'
                f"</figure>"
            )
        pending_figure = None
        pending_captions.clear()

    for el in elements:
        kind = el.get("kind")

        if kind == "heading":
            _flush_figure()
            _flush_pending_footnotes(lines, endnotes, pending_footnotes, _endnote_number)
            prefix = "#" * el.get("level", 1)
            lines.append(f"\n{prefix} {el['text'].strip()}\n")

        elif kind == "paragraph":
            _flush_figure()
            text = el.get("text", "").strip()
            if text:
                # Try to detect and mark up inline footnote markers.
                if known_markers:
                    text, inline_found = _markup_inline_footnote_refs(text, known_markers)
                else:
                    inline_found = set()

                lines.append(f"\n{text}")

                # For pending footnotes whose markers were NOT found inline,
                # fall back to appending the reference at the end of the paragraph.
                for fn in pending_footnotes:
                    if fn.get("marker") not in inline_found:
                        num = _endnote_number(fn)
                        fn["_number"] = num
                        endnotes.append(fn)
                        lines[-1] += f"[^{num}]"

                # Move any pending footnotes with inline markers to endnotes too
                # (they have already been referenced in the text).
                for fn in pending_footnotes:
                    if fn.get("marker") in inline_found:
                        fn["_number"] = int(fn["marker"])
                        endnotes.append(fn)

                pending_footnotes.clear()

        elif kind == "figure":
            _flush_figure()
            _flush_pending_footnotes(lines, endnotes, pending_footnotes, _endnote_number)
            pending_figure = el   # buffer; emit once we know if a caption follows

        elif kind == "table":
            _flush_figure()
            _flush_pending_footnotes(lines, endnotes, pending_footnotes, _endnote_number)
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
                if pending_figure is not None:
                    # Accumulate for the buffered figure; _flush_figure() will
                    # emit the <figure>/<figcaption> block.
                    pending_captions.append(text)
                else:
                    # Orphaned caption (no preceding figure) — render as italic.
                    lines.append(f"\n*{text}*")

        elif kind == "footnote":
            # Don't render inline; queue for endnote placement
            pending_footnotes.append(dict(el))

        else:
            _flush_figure()
            text = el.get("text", "").strip()
            if text:
                lines.append(f"\n{text}")

    # Flush anything still buffered at end of document
    _flush_figure()
    _flush_pending_footnotes(lines, endnotes, pending_footnotes, _endnote_number)

    # Append endnotes section, sorted by endnote number
    if endnotes:
        lines.append("\n\n---\n\n## Notes\n")
        for fn in sorted(endnotes, key=lambda f: f.get("_number", 0)):
            num = fn.get("_number", "?")
            text = fn.get("text", "").strip()
            if text:
                lines.append(f"\n[^{num}]: {text}")

    return "\n".join(lines).strip() + "\n"


def _flush_pending_footnotes(
    lines: list[str],
    endnotes: list[dict],
    pending: list[dict],
    endnote_number_fn,
) -> None:
    """
    If there are pending footnotes and no paragraph was emitted to attach
    them to, insert a placeholder reference and move them to endnotes.
    """
    if not pending:
        return
    for fn in pending:
        num = endnote_number_fn(fn)
        fn["_number"] = num
        endnotes.append(fn)
        if lines:
            lines[-1] += f"[^{num}]"
    pending.clear()
