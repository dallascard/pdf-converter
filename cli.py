"""
cli.py — Command-line interface for pdf-converter.

Usage examples
--------------

  # Run individual steps
  python cli.py render           my_document.pdf
  python cli.py deskew           my_document.pdf   # optional: fix rotated scans
  python cli.py analyze          my_document.pdf   # Surya layout detection
  python gui/bbox_editor.py      data/my_document/ # review/edit boxes
  python cli.py extract          my_document.pdf
  python cli.py get-alt-text     my_document.pdf   # optional: Claude API auto
  python cli.py export-boxes     my_document.pdf   # optional: export for claude.ai
  python cli.py import-alt-text  my_document.pdf alt_text_response.json
  python cli.py ocr              my_document.pdf --engine surya
  python gui/ocr_editor.py       data/my_document/ # review/edit OCR
  python cli.py assemble         my_document.pdf
  python cli.py export           my_document.pdf --formats html,epub

  # Force re-run of a step even if output already exists
  python cli.py ocr my_document.pdf --force

  # Use a custom project directory (default: data/<pdf-stem>/)
  python cli.py render my_document.pdf --project-dir /path/to/project

Step descriptions
-----------------
  render           PDF → per-page PNG images
  deskew           Straighten rotated page scans (optional; run after render)
  analyze          Page images → layout boxes via Surya (figures + tables + zones)
  init-boxes       Create empty boxes.json for fully manual annotation
  import-boxes     Import boxes.json produced via claude.ai
  extract          Crop figures/tables, produce masked page images for OCR
  get-alt-text     Generate alt-text for figures via Claude API
  export-boxes     Export figure list for manual alt-text via claude.ai
  import-alt-text  Import alt-text response from claude.ai into figures.json
  ocr              Masked pages → structured OCR text (surya or tesseract)
  apply-zones      Re-apply caption/endnote zone classification to existing OCR
  assemble         OCR lines → document model → Markdown (structure.json + document.md)
  export           Markdown → HTML / EPUB
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

import config
from core.pdf_renderer import render_pdf
from core.deskewer import deskew_pages
from core.alt_text import run_alt_text, export_alt_text_prompt, import_alt_text_response
from core.layout_analyzer import analyze_layout
from core.figure_extractor import extract_figures, load_tables
from core.ocr import run_ocr, load_ocr
from core.structure import build_structure
from core.assembler import assemble_markdown
from core.exporter import export

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared options
# ---------------------------------------------------------------------------

def _pdf_argument(func):
    return click.argument("pdf_file", type=click.Path(exists=True, dir_okay=False))(func)


def _project_dir_option(func):
    return click.option(
        "--project-dir", "project_dir",
        default=None,
        help="Directory to store project files. "
             "Defaults to data/<pdf-stem>/",
    )(func)


def _force_option(func):
    return click.option(
        "--force", is_flag=True, default=False,
        help="Re-run this step even if output already exists.",
    )(func)


def _resolve_project_dir(pdf_file: str, project_dir: str | None) -> Path:
    pdf_path = Path(pdf_file)
    if project_dir:
        p = Path(project_dir)
    else:
        p = config.DATA_ROOT / pdf_path.stem
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli():
    """pdf-converter — Convert scanned PDFs to Markdown, HTML, and EPUB."""


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

@cli.command()
@_pdf_argument
@_project_dir_option
@_force_option
@click.option("--dpi", default=config.RENDER_DPI, show_default=True,
              help="Resolution for page rendering.")
def render(pdf_file, project_dir, force, dpi):
    """Render PDF pages to PNG images."""
    pdf_path = Path(pdf_file)
    proj = _resolve_project_dir(pdf_file, project_dir)
    logger.info("Rendering %s → %s", pdf_path.name, proj)
    records = render_pdf(pdf_path, proj, dpi=dpi)
    click.echo(f"Rendered {len(records)} pages to {proj / 'pages'}")


# ---------------------------------------------------------------------------
# deskew
# ---------------------------------------------------------------------------

@cli.command()
@_pdf_argument
@_project_dir_option
@_force_option
@click.option("--pages", default=None,
              help="Comma-separated 1-based page numbers to deskew (default: all).")
@click.option("--max-angle", "max_angle", default=10.0, show_default=True,
              help="Maximum skew angle to search (degrees).")
@click.option("--step", default=0.5, show_default=True,
              help="Angle search step size (degrees). Smaller = more precise but slower.")
def deskew(pdf_file, project_dir, force, pages, max_angle, step):
    """Detect and correct rotational skew in rendered page images.

    Overwrites the page PNGs in-place; re-run 'render' to restore originals.
    Results (detected angle per page) are saved to deskew.json.
    """
    import json
    proj = _resolve_project_dir(pdf_file, project_dir)
    page_records = json.loads((proj / "pages.json").read_text())

    page_filter = None
    if pages:
        try:
            page_filter = [int(p.strip()) for p in pages.split(",")]
        except ValueError:
            raise click.BadParameter("--pages must be comma-separated integers, e.g. 1,3,5")

    results = deskew_pages(proj, page_records, pages=page_filter,
                           max_angle=max_angle, step=step, force=force)

    processed = [r for r in results if not r["skipped"]]
    if processed:
        angles = ", ".join(f"p{r['page']}={r['angle']:+.1f}°" for r in processed)
        click.echo(f"Deskewed {len(processed)} page(s): {angles}")
    else:
        click.echo("No pages deskewed (all already processed or filtered out).")


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------

@cli.command()
@_pdf_argument
@_project_dir_option
@_force_option
def analyze(pdf_file, project_dir, force):
    """Auto-detect figure, table, and exclusion regions via Surya layout detection."""
    import json
    proj = _resolve_project_dir(pdf_file, project_dir)
    pages = json.loads((proj / "pages.json").read_text())
    boxes = analyze_layout(proj, pages, force=force)
    page_boxes = boxes.get("pages", {}).values()
    total_figs  = sum(len(v.get("figures", [])) for v in page_boxes)
    total_tbls  = sum(len(v.get("tables",  [])) for v in boxes.get("pages", {}).values())
    click.echo(
        f"Layout analysis complete: {total_figs} figures, {total_tbls} tables detected."
    )
    click.echo(f"Edit bounding boxes with:  python gui/bbox_editor.py {proj}")


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

@cli.command()
@_pdf_argument
@_project_dir_option
@_force_option
def extract(pdf_file, project_dir, force):
    """Crop figure and table images and produce masked page images for OCR."""
    import json
    proj = _resolve_project_dir(pdf_file, project_dir)
    pages = json.loads((proj / "pages.json").read_text())
    figures, tables = extract_figures(proj, pages, force=force)
    click.echo(f"Extracted {len(figures)} figures, {len(tables)} tables.")


# ---------------------------------------------------------------------------
# alt-text
# ---------------------------------------------------------------------------

@cli.command("get-alt-text")
@_pdf_argument
@_project_dir_option
@_force_option
def alt_text_cmd(pdf_file, project_dir, force):
    """Generate alt-text for all figures using the Claude API.

    Requires ANTHROPIC_API_KEY to be set in .env.
    Skips figures that already have alt-text unless --force is used.
    For use without an API key, see 'export-boxes' and 'import-alt-text'.
    """
    proj = _resolve_project_dir(pdf_file, project_dir)
    figures = run_alt_text(proj, force=force)
    done = sum(1 for f in figures if f.get("alt_text"))
    click.echo(f"Alt-text complete: {done}/{len(figures)} figures have descriptions.")


@cli.command("export-boxes")
@_pdf_argument
@_project_dir_option
def export_alt_text_cmd(pdf_file, project_dir):
    """Export a figure list for manual alt-text generation via claude.ai.

    Writes figures_prompt.json to the project directory listing each figure's
    id, page image, bounding box, and caption zones. Upload the page images and
    that file to claude.ai with the prompt in prompts/alt_text.md, then import
    the response with 'import-alt-text'.
    """
    proj = _resolve_project_dir(pdf_file, project_dir)
    out_path = export_alt_text_prompt(proj)
    click.echo(f"Wrote {out_path}")
    click.echo()
    click.echo("Next steps:")
    click.echo(f"  1. Upload the relevant page images from {proj / 'pages'} to claude.ai")
    click.echo(f"  2. Also upload {out_path} and paste the prompt from prompts/alt_text.md")
    click.echo(f"  3. Save Claude's JSON response, then run:")
    click.echo(f"       python cli.py import-alt-text {Path(pdf_file).name} <response.json>")


@cli.command("import-alt-text")
@_pdf_argument
@_project_dir_option
@click.argument("json_file", type=click.Path(exists=True, dir_okay=False))
def import_alt_text_cmd(pdf_file, project_dir, json_file):
    """Import alt-text from a claude.ai JSON response into figures.json.

    JSON_FILE should contain a JSON array of {"id": ..., "alt_text": ...} entries.
    """
    proj = _resolve_project_dir(pdf_file, project_dir)
    try:
        count = import_alt_text_response(proj, Path(json_file))
    except (json.JSONDecodeError, ValueError) as exc:
        click.echo(f"Error: could not parse JSON — {exc}", err=True)
        sys.exit(1)
    click.echo(f"Imported alt-text for {count} figure(s) → figures.json")


# ---------------------------------------------------------------------------
# ocr
# ---------------------------------------------------------------------------

@cli.command()
@_pdf_argument
@_project_dir_option
@_force_option
@click.option("--engine", default="tesseract", show_default=True,
              type=click.Choice(["tesseract", "surya"]),
              help="OCR engine to use.")
def ocr(pdf_file, project_dir, force, engine):
    """Run OCR on masked page images."""
    import json
    proj = _resolve_project_dir(pdf_file, project_dir)
    pages = json.loads((proj / "pages.json").read_text())
    results = run_ocr(proj, pages, engine=engine, force=force)
    total_lines = sum(len(r.get("lines", [])) for r in results)
    click.echo(f"OCR complete: {len(results)} pages, {total_lines} lines.")
    click.echo(f"Review OCR with:  python gui/ocr_editor.py {proj}")


# ---------------------------------------------------------------------------
# apply-zones
# ---------------------------------------------------------------------------

@cli.command("apply-zones")
@_pdf_argument
@_project_dir_option
def apply_zones(pdf_file, project_dir):
    """Re-apply caption/notes zone classification to existing OCR results.

    Reads boxes.json for the current zone definitions and updates the type
    of any OCR line whose centre falls within a caption or notes zone.
    Writes the result to ocr_edited.json (preserving all other edits).
    No OCR is re-run — useful after editing zones without wanting to redo OCR.
    """
    import json
    from core.ocr import _classify_zone_lines, load_ocr

    proj = _resolve_project_dir(pdf_file, project_dir)

    boxes_path = proj / "boxes.json"
    if not boxes_path.exists():
        click.echo("Error: boxes.json not found.", err=True)
        return

    boxes = json.loads(boxes_path.read_text())
    results = load_ocr(proj, edited=True)

    total = 0
    for page_result in results:
        page_boxes = boxes.get("pages", {}).get(str(page_result["page_number"]), {})
        before = [l["type"] for l in page_result["lines"]]
        _classify_zone_lines(page_result["lines"], page_boxes)
        after = [l["type"] for l in page_result["lines"]]
        total += sum(a != b for a, b in zip(before, after))

    (proj / "ocr_edited.json").write_text(json.dumps(results, indent=2))
    click.echo(f"Zone classification applied: {total} line(s) reclassified → ocr_edited.json")
    click.echo(f"Review with:  python gui/ocr_editor.py {proj}")


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# init-boxes
# ---------------------------------------------------------------------------

@cli.command("init-boxes")
@_pdf_argument
@_project_dir_option
@_force_option
def init_boxes(pdf_file, project_dir, force):
    """Create an empty boxes.json so you can skip layout analysis and annotate manually."""
    import json
    proj = _resolve_project_dir(pdf_file, project_dir)
    boxes_path = proj / "boxes.json"

    if boxes_path.exists() and not force:
        click.echo(f"boxes.json already exists in {proj}. Use --force to overwrite.")
        return

    pages_path = proj / "pages.json"
    pages = {}
    if pages_path.exists():
        for rec in json.loads(pages_path.read_text()):
            pn = str(rec["page_number"])
            pages[pn] = {"figures": [], "tables": [], "exclusions": [], "captions": [], "notes": [], "headings": [], "paragraphs": []}
        click.echo(f"Pre-populated stubs for {len(pages)} pages.")
    else:
        click.echo("pages.json not found — run 'render' first for page stubs. "
                   "Writing empty boxes.json anyway.")

    boxes = {"global_exclusions": [], "pages": pages}
    boxes_path.write_text(json.dumps(boxes, indent=2))
    click.echo(f"boxes.json created in {proj}")
    click.echo(f"Open the bounding box editor with:  python gui/bbox_editor.py {proj}")


# ---------------------------------------------------------------------------
# import-boxes
# ---------------------------------------------------------------------------

@cli.command("import-boxes")
@_pdf_argument
@_project_dir_option
@_force_option
@click.argument("json_file", type=click.Path(exists=True, dir_okay=False))
def import_boxes(pdf_file, project_dir, force, json_file):
    """Import a boxes.json produced by claude.ai (or any external source).

    JSON_FILE should be a file containing the JSON object returned by Claude.
    Coordinates are validated and clamped to [0, 1].  The result is written
    to boxes.json in the project directory.
    """
    import json

    def clamp(val, lo=0.0, hi=1.0):
        return max(lo, min(hi, float(val)))

    def normalise_box(box, page_str=None, fig_idx=None):
        b = {
            "x": clamp(box.get("x", 0)),
            "y": clamp(box.get("y", 0)),
            "w": clamp(box.get("w", 0)),
            "h": clamp(box.get("h", 0)),
            "label": box.get("label", ""),
        }
        b["w"] = clamp(b["w"], hi=1.0 - b["x"])
        b["h"] = clamp(b["h"], hi=1.0 - b["y"])
        if fig_idx is not None:
            b["id"] = box.get("id") or f"fig_{page_str}_{fig_idx + 1}"
            b["alt_text"] = box.get("alt_text", "")
        return b

    proj = _resolve_project_dir(pdf_file, project_dir)
    boxes_path = proj / "boxes.json"

    if boxes_path.exists() and not force:
        click.echo(f"boxes.json already exists in {proj}. Use --force to overwrite.")
        return

    try:
        raw = json.loads(Path(json_file).read_text())
    except json.JSONDecodeError as exc:
        click.echo(f"Error: could not parse JSON — {exc}", err=True)
        sys.exit(1)

    if "pages" not in raw:
        click.echo("Error: JSON must have a top-level 'pages' key.", err=True)
        sys.exit(1)

    boxes = {"pages": {}}

    total_figs = 0
    total_tbls = 0
    for page_str, page_data in raw["pages"].items():
        figures = [
            normalise_box(f, page_str, i)
            for i, f in enumerate(page_data.get("figures", []))
        ]
        tables = [
            normalise_box(t, page_str, i)
            for i, t in enumerate(page_data.get("tables", []))
        ]
        exclusions = [normalise_box(e) for e in page_data.get("exclusions", [])]
        captions   = [normalise_box(c) for c in page_data.get("captions", [])]
        notes      = [normalise_box(n) for n in page_data.get("notes", [])]
        headings   = [normalise_box(h) for h in page_data.get("headings", [])]
        boxes["pages"][page_str] = {
            "figures": figures, "tables": tables,
            "exclusions": exclusions, "captions": captions,
            "notes": notes, "headings": headings,
        }
        total_figs += len(figures)
        total_tbls += len(tables)

    boxes_path.write_text(json.dumps(boxes, indent=2))
    click.echo(
        f"Imported {len(raw['pages'])} pages, {total_figs} figures, {total_tbls} tables"
        f" → {boxes_path}"
    )
    click.echo(f"Review with:  python gui/bbox_editor.py {proj}")


# ---------------------------------------------------------------------------
# assemble
# ---------------------------------------------------------------------------

@cli.command()
@_pdf_argument
@_project_dir_option
@_force_option
def assemble(pdf_file, project_dir, force):
    """Build document structure from OCR output and assemble into Markdown.

    Runs both the structure and assemble steps in sequence.
    Writes structure.json (intermediate) and output/document.md.
    """
    import json
    proj = _resolve_project_dir(pdf_file, project_dir)
    ocr_results = load_ocr(proj, edited=True)
    figures = json.loads((proj / "figures.json").read_text()) if (proj / "figures.json").exists() else []
    tables = load_tables(proj)
    elements = build_structure(proj, ocr_results, figures, table_records=tables, force=force)
    counts: dict[str, int] = {}
    for el in elements:
        counts[el["kind"]] = counts.get(el["kind"], 0) + 1
    summary = ", ".join(f"{v} {k}s" for k, v in sorted(counts.items()))
    click.echo(f"Structure: {summary}")
    out_path = assemble_markdown(proj, elements, force=force)
    click.echo(f"Markdown written to {out_path}")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

@cli.command()
@_pdf_argument
@_project_dir_option
@click.option("--formats", default="html", show_default=True,
              help="Comma-separated list of output formats: html,epub")
@click.option("--title", default="", help="Document title for HTML/EPUB metadata.")
@click.option("--author", default="", help="Author name for EPUB metadata.")
@click.option("--self-contained", "self_contained", is_flag=True, default=False,
              help="Embed images as base64 in HTML for a single shareable file.")
def export_cmd(pdf_file, project_dir, formats, title, author, self_contained):
    """Export Markdown to HTML and/or EPUB."""
    proj = _resolve_project_dir(pdf_file, project_dir)
    md_path = proj / "output" / "document.md"
    if not md_path.exists():
        click.echo("Error: document.md not found. Run the 'assemble' step first.", err=True)
        sys.exit(1)

    fmt_list = [f.strip() for f in formats.split(",") if f.strip()]
    doc_title = title or Path(pdf_file).stem.replace("_", " ").title()

    results = export(proj, md_path, fmt_list, title=doc_title, author=author,
                     self_contained=self_contained)
    for fmt, path in results.items():
        click.echo(f"{fmt.upper()} written to {path}")


# Rename to avoid shadowing built-in
cli.add_command(export_cmd, name="export")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()
