"""
alt_text.py — Generate or import alt-text descriptions for figures.

Two modes
---------
API mode (requires ANTHROPIC_API_KEY):
    Sends the full page image to Claude Vision along with the figure's bounding
    box coordinates and any nearby caption zone coordinates, so Claude can read
    surrounding text and captions for context.  Writes alt-text directly to
    figures.json.

Export/import mode (no API key needed):
    'export-alt-text' writes figures_prompt.json containing each figure's page
    number, bounding box, and nearby caption zones, along with page image paths.
    The user uploads the page images to claude.ai with the prompt from
    prompts/alt_text.md, then imports Claude's response with 'import-alt-text'.
    'import-alt-text' reads the JSON response and merges it into figures.json.

Expected response format for import
------------------------------------
A JSON array (or object with a "figures" key) of entries, each with
an "id" matching a figure id and an "alt_text" string:

    [
      {"id": "fig_1_1", "alt_text": "A bar chart showing ..."},
      {"id": "fig_2_1", "alt_text": "A photograph of ..."}
    ]
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# API auto-mode
# ---------------------------------------------------------------------------

def run_alt_text(project_dir: Path, force: bool = False) -> list[dict]:
    """Call Claude Vision on each figure and write alt-text to figures.json.

    Sends the full page image with bounding box coordinates so Claude can
    use surrounding text and captions as context.
    Skips figures that already have alt-text unless *force* is True.
    Returns the updated figures list.
    """
    from core.claude_client import get_client

    figures_path = project_dir / "figures.json"
    if not figures_path.exists():
        raise FileNotFoundError(
            f"No figures.json in {project_dir}. Run the 'extract' step first."
        )
    figures = json.loads(figures_path.read_text())

    # Load page image paths
    pages_path = project_dir / "pages.json"
    pages_by_num: dict[int, dict] = {}
    if pages_path.exists():
        for rec in json.loads(pages_path.read_text()):
            pages_by_num[rec["page_number"]] = rec

    # Load caption zones from boxes.json
    boxes_path = project_dir / "boxes.json"
    boxes: dict = json.loads(boxes_path.read_text()) if boxes_path.exists() else {}

    client = get_client()
    updated = False

    for rec in figures:
        if rec.get("alt_text") and not force:
            logger.info("  Skipping %s (already has alt-text).", rec["id"])
            continue

        page_num = rec["page_number"]
        page_rec = pages_by_num.get(page_num)
        if not page_rec:
            logger.warning("Page record not found for page %d; skipping %s.", page_num, rec["id"])
            continue

        page_path = project_dir / page_rec["image_path"]
        if not page_path.exists():
            logger.warning("Page image not found: %s", page_path)
            continue

        page_boxes = boxes.get("pages", {}).get(str(page_num), {})
        caption_zones = page_boxes.get("captions", [])

        logger.info("  Generating alt-text for %s (page %d) …", rec["id"], page_num)
        rec["alt_text"] = _call_claude(client, page_path, rec["box"], caption_zones)
        updated = True

    if updated:
        figures_path.write_text(json.dumps(figures, indent=2))
        logger.info("figures.json updated with alt-text.")

    return figures


# ---------------------------------------------------------------------------
# Export prompt for manual claude.ai upload
# ---------------------------------------------------------------------------

def export_alt_text_prompt(project_dir: Path) -> Path:
    """Write figures_prompt.json for manual alt-text generation via claude.ai.

    The JSON file lists each figure's id, page number, bounding box, caption
    zone locations, and the page image path — everything Claude needs to write
    accurate alt-text using context from the full page.
    Returns the path to the written file.
    """
    figures_path = project_dir / "figures.json"
    if not figures_path.exists():
        raise FileNotFoundError(
            f"No figures.json in {project_dir}. Run the 'extract' step first."
        )
    figures = json.loads(figures_path.read_text())

    # Load page image paths
    pages_path = project_dir / "pages.json"
    pages_by_num: dict[int, dict] = {}
    if pages_path.exists():
        for rec in json.loads(pages_path.read_text()):
            pages_by_num[rec["page_number"]] = rec

    # Load caption zones
    boxes_path = project_dir / "boxes.json"
    boxes: dict = json.loads(boxes_path.read_text()) if boxes_path.exists() else {}

    prompt_data = {
        "figures": [
            {
                "id": rec["id"],
                "page_number": rec["page_number"],
                "page_image": pages_by_num.get(rec["page_number"], {}).get("image_path", ""),
                "figure_box": rec["box"],
                "caption_zones": boxes.get("pages", {}).get(
                    str(rec["page_number"]), {}
                ).get("captions", []),
            }
            for rec in figures
        ]
    }

    out_path = project_dir / "figures_prompt.json"
    out_path.write_text(json.dumps(prompt_data, indent=2))
    return out_path


# ---------------------------------------------------------------------------
# Import response from claude.ai
# ---------------------------------------------------------------------------

def import_alt_text_response(project_dir: Path, response_path: Path) -> int:
    """Read a claude.ai JSON response and merge alt-text into figures.json.

    Returns the number of figures updated.
    """
    figures_path = project_dir / "figures.json"
    if not figures_path.exists():
        raise FileNotFoundError(
            f"No figures.json in {project_dir}. Run the 'extract' step first."
        )
    figures = json.loads(figures_path.read_text())

    raw = json.loads(response_path.read_text())
    # Accept either a bare list or {"figures": [...]}
    if isinstance(raw, dict):
        entries = raw.get("figures", [])
    else:
        entries = raw

    by_id = {rec["id"]: rec for rec in figures}
    count = 0
    for entry in entries:
        fig_id = entry.get("id")
        alt = entry.get("alt_text", "").strip()
        if fig_id and fig_id in by_id:
            by_id[fig_id]["alt_text"] = alt
            count += 1
        else:
            logger.warning("import-alt-text: unknown figure id %r — skipped.", fig_id)

    figures_path.write_text(json.dumps(figures, indent=2))
    return count


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _format_box(box: dict) -> str:
    """Format a fractional bbox as a human-readable percentage string."""
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    return (
        f"left {x:.0%}, top {y:.0%}, "
        f"right {x + w:.0%}, bottom {y + h:.0%}"
    )


def _call_claude(
    client,
    page_path: Path,
    fig_box: dict,
    caption_zones: list[dict],
) -> str:
    """Call Claude Vision on a full page image and return alt-text for one figure.

    The prompt tells Claude where the figure is (as % coordinates) and where
    any caption zones are, so it can read surrounding context.
    """
    img_b64 = base64.standard_b64encode(page_path.read_bytes()).decode()

    location_desc = f"The figure is located at: {_format_box(fig_box)}."

    if caption_zones:
        cap_lines = "\n".join(
            f"  - {_format_box(z)}" for z in caption_zones
        )
        caption_desc = (
            f"\nThere may be a caption for this figure in one of these zones "
            f"(as page fractions):\n{cap_lines}\n"
            "Use any visible caption text and surrounding context to inform your description."
        )
    else:
        caption_desc = ""

    prompt = (
        f"You are writing alt-text for a figure in a scanned academic document. "
        f"The full page image is provided.\n\n"
        f"{location_desc}{caption_desc}\n\n"
        f"Write a concise alt-text description of this figure suitable for an "
        f"HTML or EPUB document. "
        f"Maximum {config.ALT_TEXT_MAX_CHARS} characters. "
        "Return only the alt-text, no preamble."
    )

    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=200,
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
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        return response.content[0].text.strip()[:config.ALT_TEXT_MAX_CHARS]
    except Exception as exc:
        logger.warning("Alt-text generation failed for page %s: %s", page_path.name, exc)
        return ""
