"""
exporter.py — Convert the Markdown document to HTML and/or EPUB.

HTML export
-----------
Uses Jinja2 to render a clean, readable HTML file with embedded CSS.
Images are referenced as relative paths (same directory structure as Markdown).

EPUB export
-----------
Uses ebooklib to assemble a standards-compliant EPUB 3 file.
Each top-level heading (H1) becomes a chapter spine item.
"""

from __future__ import annotations

import base64
import logging
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_MATHJAX_CDN = "https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"
_MATHJAX_CACHE = Path(__file__).parent.parent / "assets" / "mathjax.js"


def _get_mathjax_script_tag() -> str:
    """Return a <script> tag containing MathJax.

    On first call, downloads the MathJax bundle from the CDN and caches it to
    assets/mathjax.js so subsequent exports (and offline use) work without
    a network connection.  If the download fails and no cached copy exists,
    falls back to the CDN <script src=...> tag.
    """
    if not _MATHJAX_CACHE.exists():
        _MATHJAX_CACHE.parent.mkdir(parents=True, exist_ok=True)
        try:
            import requests
            logger.info("Downloading MathJax bundle from CDN (one-time) …")
            resp = requests.get(_MATHJAX_CDN, timeout=30)
            resp.raise_for_status()
            _MATHJAX_CACHE.write_bytes(resp.content)
            logger.info("MathJax cached to %s (%d KB).",
                        _MATHJAX_CACHE, len(resp.content) // 1024)
        except Exception as exc:
            logger.warning(
                "Could not download MathJax (%s); falling back to CDN link. "
                "Run the export step with internet access once to cache it.",
                exc,
            )
            return (
                '<script>window.MathJax = {'
                'tex: {inlineMath: [["\\\\(","\\\\)"]], displayMath: [["\\\\[","\\\\]"]]},'
                'options: {skipHtmlTags: ["script","noscript","style","textarea","pre"]},'
                'startup: {typeset: true}'
                '};</script>\n  '
                f'<script src="{_MATHJAX_CDN}" async></script>'
            )

    js = _MATHJAX_CACHE.read_text(encoding="utf-8")
    config = (
        'window.MathJax = {'
        'tex: {inlineMath: [["\\\\(","\\\\)"]], displayMath: [["\\\\[","\\\\]"]]},'
        'options: {skipHtmlTags: ["script","noscript","style","textarea","pre"]},'
        'startup: {typeset: true}'
        '};'
    )
    return f"<script>{config}\n{js}</script>"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export(
    project_dir: Path,
    md_path: Path,
    formats: list[str],
    title: str = "Document",
    author: str = "",
    self_contained: bool = False,
) -> dict[str, Path]:
    """
    Export the Markdown file to the requested formats.

    *formats* is a list containing any of ``"html"`` and ``"epub"``.
    If *self_contained* is True, images in the HTML output are embedded as
    base64 data URIs so the file can be shared as a single standalone file.
    Returns a dict mapping format name → output path.
    """
    output_dir = md_path.parent
    results: dict[str, Path] = {}

    md_text = md_path.read_text(encoding="utf-8")

    if "html" in formats:
        html_path = _export_html(md_text, output_dir, title, author,
                                 self_contained=self_contained)
        results["html"] = html_path

    if "epub" in formats:
        epub_path = _export_epub(md_text, output_dir, project_dir, title, author)
        results["epub"] = epub_path

    return results


# ---------------------------------------------------------------------------
# HTML export
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{
      font-family: Georgia, 'Times New Roman', serif;
      font-size: 1.05rem;
      line-height: 1.7;
      max-width: 48rem;
      margin: 3rem auto;
      padding: 0 1.5rem;
      color: #222;
    }}
    h1, h2, h3 {{ font-family: sans-serif; line-height: 1.25; }}
    h1 {{ font-size: 1.9rem; margin-top: 2.5rem; }}
    h2 {{ font-size: 1.45rem; margin-top: 2rem; }}
    h3 {{ font-size: 1.2rem;  margin-top: 1.5rem; }}
    img {{ max-width: 100%; height: auto; display: block; margin: 1.5rem auto; }}
    figure {{ margin: 1.5rem 0; text-align: center; }}
    figcaption {{ font-size: 0.9rem; color: #555; font-style: italic; margin-top: 0.5rem; }}
    figure table {{ margin: 0 auto; text-align: left; }}
    em  {{ display: block; text-align: center; font-size: 0.9rem; color: #555; }}
    hr  {{ border: none; border-top: 1px solid #ccc; margin: 2.5rem 0; }}
    .footnotes {{ font-size: 0.9rem; color: #444; }}
    .footnotes ol {{ padding-left: 1.5rem; }}
    sup a {{ text-decoration: none; color: #0055aa; }}
    a[href^="#fnref"] {{ color: #0055aa; }}
  </style>
  <!-- MathJax: renders <math> (MathML) and $...$ / $$...$$ (TeX) produced by Surya OCR -->
  {mathjax}
</head>
<body>
{body}
</body>
</html>
"""


def _extract_math(md_text: str) -> tuple[str, dict[str, str]]:
    """Replace <math> blocks with unique placeholders before markdown conversion.

    Surya OCR outputs TeX source inside <math> tags.  The markdown library
    would mangle backslashes and brackets inside them, so we stash each block
    and restore it after conversion.

    Returns (text_with_placeholders, {placeholder: html_span}).
    The restored spans use <span>/<div> with data-mathtype so the MathJax
    config can find them via the tex delimiters we inject.
    """
    stash: dict[str, str] = {}
    counter = [0]

    def replace(m: re.Match) -> str:
        counter[0] += 1
        key = f"\x00MATH{counter[0]}\x00"
        tex = m.group(1)
        is_display = bool(m.group(0).startswith('<math ') and 'display' in m.group(0))
        if is_display:
            stash[key] = f'<div class="math-display">\\[{tex}\\]</div>'
        else:
            stash[key] = f'<span class="math-inline">\\({tex}\\)</span>'
        return key

    # Display math first (more specific pattern)
    md_text = re.sub(
        r'<math\s+display=["\']block["\'][^>]*>(.*?)</math>',
        replace, md_text, flags=re.DOTALL,
    )
    # Remaining inline math
    md_text = re.sub(
        r'<math(?:\s[^>]*)?>(.*?)</math>',
        replace, md_text, flags=re.DOTALL,
    )
    return md_text, stash


def _restore_math(html: str, stash: dict[str, str]) -> str:
    for key, value in stash.items():
        html = html.replace(key, value)
    return html


def _export_html(
    md_text: str, output_dir: Path, title: str, author: str,
    self_contained: bool = False,
) -> Path:
    """Render Markdown → HTML and write to output_dir/document.html."""
    # Stash math blocks before markdown sees them (prevents backslash mangling)
    md_text, math_stash = _extract_math(md_text)

    try:
        import markdown as md_lib

        html_body = md_lib.markdown(
            md_text,
            extensions=[
                "extra",          # tables, fenced code, etc.
                "footnotes",      # [^1]: syntax → proper footnotes
                "toc",            # auto table of contents anchors
                "sane_lists",
            ],
        )
    except ImportError:
        logger.warning("markdown library not installed; falling back to minimal HTML.")
        html_body = _minimal_md_to_html(md_text)

    # Restore math blocks (placeholders → rendered spans with TeX delimiters)
    html_body = _restore_math(html_body, math_stash)

    html = _HTML_TEMPLATE.format(
        title=_escape_html(title),
        body=html_body,
        mathjax=_get_mathjax_script_tag(),
    )

    if self_contained:
        html = _embed_images(html, output_dir)

    out_path = output_dir / "document.html"
    out_path.write_text(html, encoding="utf-8")
    logger.info("HTML written to %s (%s)", out_path,
                "self-contained" if self_contained else "with external images")
    return out_path


def _embed_images(html: str, output_dir: Path) -> str:
    """Replace <img src="..."> paths with base64 data URIs."""
    def replace_src(m: re.Match) -> str:
        src = m.group(1)
        if src.startswith("data:"):
            return m.group(0)   # already embedded
        img_path = (output_dir / src).resolve()
        if not img_path.exists():
            logger.warning("Image not found for embedding: %s", img_path)
            return m.group(0)
        suffix = img_path.suffix.lower().lstrip(".")
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png", "gif": "image/gif",
                "webp": "image/webp"}.get(suffix, "image/png")
        data = base64.standard_b64encode(img_path.read_bytes()).decode()
        return f'src="data:{mime};base64,{data}"'

    return re.sub(r'src="([^"]+)"', replace_src, html)


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _minimal_md_to_html(md_text: str) -> str:
    """Extremely basic Markdown → HTML without the markdown library."""
    lines = md_text.splitlines()
    out = []
    for line in lines:
        if line.startswith("### "):
            out.append(f"<h3>{_escape_html(line[4:])}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{_escape_html(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{_escape_html(line[2:])}</h1>")
        elif line.startswith("!["):
            m = re.match(r"!\[([^\]]*)\]\(([^)]+)\)", line)
            if m:
                out.append(
                    f'<img alt="{_escape_html(m.group(1))}" src="{m.group(2)}">'
                )
        elif line.startswith("*") and line.endswith("*") and len(line) > 2:
            out.append(f"<em>{_escape_html(line[1:-1])}</em>")
        elif line.strip() == "---":
            out.append("<hr>")
        elif line.strip():
            out.append(f"<p>{_escape_html(line)}</p>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# EPUB export
# ---------------------------------------------------------------------------

def _export_epub(
    md_text: str,
    output_dir: Path,
    project_dir: Path,
    title: str,
    author: str,
) -> Path:
    """Render Markdown → EPUB and write to output_dir/document.epub."""
    try:
        from ebooklib import epub
        import markdown as md_lib
    except ImportError as exc:
        raise RuntimeError(
            f"Missing dependency for EPUB export: {exc}. "
            "Install with: pip install ebooklib markdown"
        ) from exc

    book = epub.EpubBook()
    book.set_identifier("pdf-converter-doc")
    book.set_title(title)
    book.set_language("en")
    if author:
        book.add_author(author)

    # Convert full Markdown to HTML (stash math blocks first to prevent mangling)
    md_text_clean, math_stash = _extract_math(md_text)
    full_html = md_lib.markdown(
        md_text_clean,
        extensions=["extra", "footnotes", "toc", "sane_lists"],
    )
    full_html = _restore_math(full_html, math_stash)

    # Split on H1 headings to create chapters
    chapters = _split_into_chapters(full_html)

    epub_chapters = []
    for i, (chapter_title, chapter_html) in enumerate(chapters):
        chap = epub.EpubHtml(
            title=chapter_title,
            file_name=f"chap_{i:03d}.xhtml",
            lang="en",
        )
        chap.content = _wrap_epub_chapter(chapter_title, chapter_html)
        book.add_item(chap)
        epub_chapters.append(chap)

    # Add images
    images_src = project_dir / "images"
    if images_src.exists():
        for img_file in images_src.glob("*.png"):
            with img_file.open("rb") as fh:
                img_item = epub.EpubImage()
                img_item.file_name = f"images/{img_file.name}"
                img_item.media_type = "image/png"
                img_item.content = fh.read()
                book.add_item(img_item)

    # Navigation
    book.toc = epub_chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + epub_chapters

    out_path = output_dir / "document.epub"
    epub.write_epub(str(out_path), book)
    logger.info("EPUB written to %s", out_path)
    return out_path


def _split_into_chapters(html: str) -> list[tuple[str, str]]:
    """
    Split an HTML string on <h1> tags.

    Returns a list of (chapter_title, chapter_html_body) tuples.
    The HTML body does not include the h1 tag itself.
    """
    parts = re.split(r"(<h1[^>]*>.*?</h1>)", html, flags=re.IGNORECASE | re.DOTALL)

    chapters: list[tuple[str, str]] = []
    current_title = "Preface"
    current_body_parts: list[str] = []

    for part in parts:
        if re.match(r"<h1", part, re.IGNORECASE):
            if current_body_parts:
                chapters.append((current_title, "".join(current_body_parts)))
            title_text = re.sub(r"<[^>]+>", "", part).strip()
            current_title = title_text or "Chapter"
            current_body_parts = []
        else:
            current_body_parts.append(part)

    if current_body_parts:
        chapters.append((current_title, "".join(current_body_parts)))

    return chapters if chapters else [("Document", html)]


def _wrap_epub_chapter(title: str, body_html: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<!DOCTYPE html>'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en">'
        "<head>"
        f'<title>{_escape_html(title)}</title>'
        "<style>"
        "body{font-family:serif;font-size:1em;line-height:1.6;margin:1em;}"
        "img{max-width:100%;height:auto;}"
        "figure{margin:1.5em 0;text-align:center;}"
        "figcaption{font-size:0.9em;color:#555;font-style:italic;margin-top:0.4em;}"
        "em{display:block;text-align:center;font-size:0.9em;}"
        "</style>"
        "</head>"
        f"<body><h1>{_escape_html(title)}</h1>{body_html}</body>"
        "</html>"
    )
