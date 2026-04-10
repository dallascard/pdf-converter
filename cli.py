"""
cli.py — Command-line interface for pdf-converter.

Usage examples
--------------

  # Full pipeline (all steps)
  python cli.py convert my_document.pdf

  # Run individual steps
  python cli.py render    my_document.pdf
  python cli.py analyze   my_document.pdf
  python cli.py extract   my_document.pdf
  python cli.py ocr       my_document.pdf
  python cli.py structure my_document.pdf
  python cli.py assemble  my_document.pdf
  python cli.py export    my_document.pdf --formats html epub

  # Force re-run of a step even if output already exists
  python cli.py ocr my_document.pdf --force

  # Use a custom project directory (default: data/<pdf-stem>/)
  python cli.py convert my_document.pdf --project-dir /path/to/project

  # Use tesseract instead of Claude for OCR
  python cli.py ocr my_document.pdf --engine tesseract

  # Generate alt-text for figures during extraction
  python cli.py extract my_document.pdf --alt-text

  # Export specific formats
  python cli.py export my_document.pdf --formats html epub

Step descriptions
-----------------
  render    PDF → per-page PNG images
  analyze   Page images → layout boxes (figures + tables + exclusion zones)
  extract   Crop figures and tables, mask pages
  ocr       Masked pages → structured OCR text; table crops → tables.json
  structure OCR lines → document model (paragraphs, headings, footnotes…)
  assemble  Document model → Markdown
  export    Markdown → HTML / EPUB
  convert   All of the above in sequence
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

import config
from core.pdf_renderer import render_pdf
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
# analyze
# ---------------------------------------------------------------------------

@cli.command()
@_pdf_argument
@_project_dir_option
@_force_option
@click.option("--engine", default="claude", show_default=True,
              type=click.Choice(["claude", "surya"]),
              help="Layout analysis engine.")
def analyze(pdf_file, project_dir, force, engine):
    """Auto-detect figure, table, and exclusion regions (layout analysis)."""
    import json
    proj = _resolve_project_dir(pdf_file, project_dir)
    pages = json.loads((proj / "pages.json").read_text())
    boxes = analyze_layout(proj, pages, engine=engine, force=force)
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
@click.option("--alt-text", "alt_text", is_flag=True, default=False,
              help="Generate alt-text for each figure via Claude.")
def extract(pdf_file, project_dir, force, alt_text):
    """Crop figure and table images and produce masked page images for OCR."""
    import json
    proj = _resolve_project_dir(pdf_file, project_dir)
    pages = json.loads((proj / "pages.json").read_text())
    figures, tables = extract_figures(proj, pages, force=force, generate_alt_text=alt_text)
    click.echo(f"Extracted {len(figures)} figures, {len(tables)} tables.")


# ---------------------------------------------------------------------------
# ocr
# ---------------------------------------------------------------------------

@cli.command()
@_pdf_argument
@_project_dir_option
@_force_option
@click.option("--engine", default="claude", show_default=True,
              type=click.Choice(["claude", "tesseract", "surya"]),
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

@cli.command()
@_pdf_argument
@_project_dir_option
@_force_option
def structure(pdf_file, project_dir, force):
    """Parse OCR output into the structured document model."""
    import json
    proj = _resolve_project_dir(pdf_file, project_dir)
    ocr_results = load_ocr(proj, edited=True)
    figures = json.loads((proj / "figures.json").read_text()) if (proj / "figures.json").exists() else []
    tables  = load_tables(proj)
    elements = build_structure(proj, ocr_results, figures, table_records=tables, force=force)
    counts: dict[str, int] = {}
    for el in elements:
        counts[el["kind"]] = counts.get(el["kind"], 0) + 1
    summary = ", ".join(f"{v} {k}s" for k, v in sorted(counts.items()))
    click.echo(f"Structure: {summary}")


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

    boxes = {"pages": pages}
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
    """Assemble the document model into Markdown."""
    import json
    proj = _resolve_project_dir(pdf_file, project_dir)
    elements = json.loads((proj / "structure.json").read_text())
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
# convert (full pipeline)
# ---------------------------------------------------------------------------

@cli.command()
@_pdf_argument
@_project_dir_option
@_force_option
@click.option("--dpi", default=config.RENDER_DPI, show_default=True)
@click.option("--engine", default="claude", show_default=True,
              type=click.Choice(["claude", "tesseract", "surya"]))
@click.option("--alt-text", "alt_text", is_flag=True, default=False,
              help="Generate alt-text for figures.")
@click.option("--formats", default="md", show_default=True,
              help="Comma-separated export formats: md,html,epub")
@click.option("--title", default="", help="Document title.")
@click.option("--author", default="", help="Author name.")
@click.option("--self-contained", "self_contained", is_flag=True, default=False,
              help="Embed images as base64 in HTML for a single shareable file.")
@click.option("--pause-after-analyze", is_flag=True, default=False,
              help="Pause after layout analysis so you can edit bounding boxes "
                   "before extraction and OCR continue.")
def convert(pdf_file, project_dir, force, dpi, engine, alt_text, formats,
            title, author, self_contained, pause_after_analyze):
    """Run the full pipeline: render → analyze → extract → ocr → structure → assemble → export."""
    import json
    pdf_path = Path(pdf_file)
    proj = _resolve_project_dir(pdf_file, project_dir)
    doc_title = title or pdf_path.stem.replace("_", " ").title()

    click.echo(f"\n=== pdf-converter ===")
    click.echo(f"PDF    : {pdf_path}")
    click.echo(f"Project: {proj}")
    click.echo(f"Engine : {engine}\n")

    # Step 1: Render
    click.echo("[1/7] Rendering pages …")
    pages = render_pdf(pdf_path, proj, dpi=dpi)
    click.echo(f"      {len(pages)} pages rendered.\n")

    # Step 2: Layout analysis
    click.echo("[2/7] Analyzing layout …")
    boxes = analyze_layout(proj, pages, engine=engine, force=force)
    page_data = boxes.get("pages", {}).values()
    total_figs = sum(len(v.get("figures", [])) for v in page_data)
    total_tbls = sum(len(v.get("tables",  [])) for v in boxes.get("pages", {}).values())
    click.echo(f"      {total_figs} figures, {total_tbls} tables detected.\n")

    if pause_after_analyze:
        click.echo(
            f"  >> Layout analysis complete.  Edit bounding boxes with:\n"
            f"       python gui/bbox_editor.py {proj}\n"
            f"  Then press Enter to continue …"
        )
        input()

    # Step 3: Extract figures + mask pages
    click.echo("[3/7] Extracting figures and masking pages …")
    figures, tables = extract_figures(proj, pages, force=force, generate_alt_text=alt_text)
    # Reload pages (extract_figures updates masked_image_path)
    pages = json.loads((proj / "pages.json").read_text())
    click.echo(f"      {len(figures)} figures, {len(tables)} tables extracted.\n")

    # Step 4: OCR
    click.echo(f"[4/7] Running OCR ({engine}) …")
    ocr_results = run_ocr(proj, pages, engine=engine, force=force)
    total_lines = sum(len(r.get("lines", [])) for r in ocr_results)
    click.echo(f"      {total_lines} lines recognised.\n")

    click.echo(
        f"  Tip: review and correct OCR with:\n"
        f"       python gui/ocr_editor.py {proj}\n"
        f"  Tip: review and correct table OCR with:\n"
        f"       python gui/table_editor.py {proj}\n"
    )

    # Step 5: Structure
    click.echo("[5/7] Building document structure …")
    # Use edited OCR if available
    ocr_results = load_ocr(proj, edited=True)
    elements = build_structure(proj, ocr_results, figures, table_records=tables, force=force)
    click.echo(f"      {len(elements)} elements.\n")

    # Step 6: Assemble Markdown
    click.echo("[6/7] Assembling Markdown …")
    md_path = assemble_markdown(proj, elements, force=force)
    click.echo(f"      {md_path}\n")

    # Step 7: Export
    fmt_list = [f.strip() for f in formats.split(",") if f.strip()]
    if fmt_list and fmt_list != ["md"]:
        click.echo(f"[7/7] Exporting ({', '.join(fmt_list)}) …")
        results = export(proj, md_path, [f for f in fmt_list if f != "md"],
                         title=doc_title, author=author,
                         self_contained=self_contained)
        for fmt, path in results.items():
            click.echo(f"      {fmt.upper()}: {path}")
    else:
        click.echo("[7/7] Export skipped (Markdown only).")

    click.echo(f"\nDone!  Output in {proj / 'output'}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()
