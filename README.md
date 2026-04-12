# pdf-converter

Convert scanned PDF documents to Markdown, HTML, and EPUB using Claude Vision for OCR and layout analysis.

## Features

- **Surya layout detection** — figures, tables, and boilerplate (headers, footers, page numbers) are identified automatically before OCR runs; manual annotation and claude.ai import are also supported
- **Surya / Tesseract OCR** — Surya produces better results (especially for math and multilingual text); Tesseract is a lighter fallback requiring no GPU
- **Table extraction** — table regions are cropped, masked out of OCR pages, and OCR'd separately to produce clean Markdown tables
- **Figure extraction** — image regions are cropped and saved separately; OCR never runs on figure interiors
- **Alt-text generation** — optional step to write accessibility descriptions for figures, via Claude API or claude.ai (no API key required)
- **Structure detection** — headings, footnotes, and figure captions are identified and tagged
- **Footnote → endnote conversion** — all footnotes are collected and appended as a numbered endnote section
- **Multi-format export** — output to `.md`, `.html`, and `.epub`; `--self-contained` embeds images as base64 for a single shareable HTML file
- **Bounding Box Editor** — PyQt6 GUI to review and correct auto-detected figure, table, and exclusion zones
- **OCR Line Editor** — PyQt6 GUI for line-by-line OCR correction, with each line's image crop shown alongside its text field

---

## Requirements

### System dependencies

**macOS:**

```bash
brew install poppler tesseract
```

**Linux (Debian/Ubuntu):**

```bash
sudo apt-get install poppler-utils tesseract-ocr tesseract-ocr-eng
```

`poppler` provides `pdfinfo`/`pdftoppm` (used by `pdf2image`).  
`tesseract` is the local OCR fallback engine.

Note that both poppler and tesseract are installed automatically in the container provided here.

### Python

Python 3.11 or later. The project uses [uv](https://docs.astral.sh/uv/) for package management.

Install `uv` if you don't have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Installation

```bash
cd pdf-converter

# Standard install — includes Surya (required for 'analyze' and recommended for 'ocr')
# Note: downloads PyTorch and Surya model weights (~1-2 GB on first use)
uv sync --extra surya

# Minimal install — no Surya; use 'init-boxes' or 'import-boxes' for layout annotation,
# and '--engine tesseract' for OCR
uv sync

# Activate the environment
source .venv/bin/activate

# Optional: copy the environment template and add your Anthropic API key
# (only needed for alt-text generation)
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY=sk-ant-...
```

---

## Usage

### Step-by-step pipeline

Each stage can be run independently, which is useful for re-running a single step after manual corrections. Some stages have multiple options.

```bash
# 1. Render PDF pages to PNG images
python cli.py render my_document.pdf

# 2. Optionally, correct rotational skew in scanned pages
python cli.py deskew my_document.pdf               # all pages
python cli.py deskew my_document.pdf --pages 2,5   # specific pages only

# 3. Detect layout (figures, tables, exclusion zones)
# Option A: Use Surya (requires installing with --extra surya; see above)
python cli.py analyze my_document.pdf
# Option B: Use claude.ai model for layout detection (no API key required, but less reliable than Surya):
#   Upload the PDF with the prompt from prompts/layout_analysis.md, then:
python cli.py import-boxes my_document.pdf claude_response.json
# Option C: Draw boxes manually instead
python cli.py init-boxes my_document.pdf

# 4. Edit bounding boxes in the editor to review/correct detections
python gui/bbox_editor.py data/my_document/

# 5. Extract final figures and produce masked page images
python cli.py extract my_document.pdf

# 6. Optionally, generate alt-text for figures
# Option A: Automatically via Claude API (API key and funds required)
python cli.py get-alt-text my_document.pdf
# Option B (step 1): Export boxes for uploading to claude.ai (no API key required)
python cli.py export-boxes my_document.pdf  # writes figures_prompt.json
# Option B (step 2): Upload pages, boxes, and prompt (in prompts/alt_text.md), and save Claude's response as alt_text_response.json
# Option B (step 3): save and then import alt-text from claude.ai
python cli.py import-alt-text my_document.pdf alt_text_response.json

# 6b. Optionally, open the alt-text editor to review and edit figure descriptions
python gui/alt_text_editor.py data/my_document/

# 7. Run OCR on all pages
# Option A: Use Surya (better accuracy, handles math and multilingual text; may produce more complex output)
python cli.py ocr my_document.pdf --engine surya
# Option B: Use Tesseract (lighter-weight; reliable for simple documents)
python cli.py ocr my_document.pdf --engine tesseract

# 8. Optionally, open the OCR editor to correct text line mistakes
python gui/ocr_editor.py data/my_document/

# 9. Optionally, open the table editor to correct table OCR (image + Markdown side by side)
python gui/table_editor.py data/my_document/

# 10. Assemble Markdown (builds document structure and writes output/document.md)
python cli.py assemble my_document.pdf

# 11. Edit the Markdown to fix any remaining issues (see "Editing the Markdown" below)

# 12. Export to HTML and/or EPUB
python cli.py export my_document.pdf --formats html,epub
```

Edits made in the GUI tools are saved to `ocr_edited.json` and `boxes.json` in the project directory and are automatically used by subsequent pipeline steps.

Surya requires PyTorch and will download model weights on first use (~1–2 GB). It is slower than Tesseract on CPU but produces significantly better results, especially for documents with math or non-English text.

---

### Editing the Markdown

After `assemble`, open `output/document.md` in any text editor and work through the items below. Re-run `export` once you are happy with it.

#### Heading levels

Surya and Tesseract sometimes assign the wrong heading level, or miss headings entirely and leave them as body text.

- **Wrong level** — change `##` to `#` (or vice versa) as needed.
- **Missed heading** — prefix the line with `#`, `##`, or `###`.
- **Falsely detected heading** — remove the `#` prefix; the line becomes a normal paragraph.

```markdown
## Introduction ← was ### Introduction; promoted to

# Part II ← was body text; added

This is not a heading. ← was ## This is not a heading.
```

#### Rejoining split paragraphs

OCR sometimes breaks a single paragraph across two elements when there is a column boundary, a figure interruption, or an unusual line gap. The symptom is two short paragraphs that read as one continuous thought.

Delete the blank line between them (in Markdown, a blank line = paragraph break):

```markdown
This is the first half of a paragraph that was

split by the OCR engine.
```

→

```markdown
This is the first half of a paragraph that was
split by the OCR engine.
```

If a word at the join is hyphenated across lines, also remove the hyphen:

```markdown
...the value-by-area car-
togram is unique...
```

→ `...the value-by-area cartogram is unique...`

#### Missing footnote markers in body text

The assembler attempts to detect inline footnote markers (superscript digits) automatically, but may miss some. If a `[^N]` reference is absent from the body text, add it manually at the right word:

```markdown
spatial transformations[^2] have become popular.
```

The corresponding endnote `[^2]: …` should already be in the Notes section at the bottom of the file. If the endnote itself is missing, add it there.

#### Figure positioning

Figures are inserted at the Y position where they appear on the page, which does not always match the intended reading position. Move the `<figure>…</figure>` block (or bare `![…](…)` for uncaptioned figures) to where it belongs in the narrative — typically just after the first paragraph that refers to it.

#### Figure caption placement

Captions are linked to their figure by proximity. Occasionally a caption is attached to the wrong figure, or two captions are merged into one. Check each `<figcaption>` block against the source PDF.

- **Wrong figure** — cut the `<figcaption>…</figcaption>` line out of one `<figure>` block and paste it into the correct one.
- **Merged captions** — split the text at the correct boundary and create a second `<figcaption>` in the appropriate figure block.
- **Missing caption** — add a `<figcaption>` line inside the `<figure>` block:

```html
<figure>
  <img src="../images/fig_3_1.png" alt="A map of rainfall distribution." />
  <figcaption>Figure 3.1 Mean annual rainfall, 1980–2020.</figcaption>
</figure>
```

#### Bold and italic

Use standard Markdown syntax, which exports correctly to `<strong>` and `<em>` in HTML:

```markdown
**bold text**
_italic text_
**_bold and italic_**
```

Prefer `*asterisks*` over `_underscores_` — underscores inside words (e.g. variable names) can cause unintended italics in some renderers.

Common things to mark up: book or journal titles (_Cartography: Thematic Map Design_), introduced terms (**value-by-area map**), and emphasis that was conveyed by italic type in the original.

#### Footnote text errors

OCR of small footnote text is error-prone. Check the Notes section at the end of the file and correct any garbled words. Also check:

- **Extra period at the start** — the assembler strips `1.` markers but may leave a stray `.` if the OCR inserted a space: `. Author name` → `Author name`.
- **Run-together footnotes** — if two footnote texts were merged into one `[^N]: …` entry, split them and add the missing `[^M]: …` entry.
- **Footnote number mismatch** — if a `[^N]` reference in the body has no matching `[^N]: …` at the bottom, either add the missing endnote or renumber to match.

#### Lists and enumerations

Markdown ordered and unordered lists require a blank line before the first item and consistent indentation for nested items. OCR often outputs list items as plain paragraphs. Convert them:

```markdown
1. First item
2. Second item
3. Third item

- Bullet point
- Another point
```

Nested lists need four spaces of indentation:

```markdown
1. Top-level item
   1. Sub-item
   2. Sub-item
```

#### Equations

Surya outputs TeX math inside `<math>` tags, which the exporter converts to MathJax `\(…\)` (inline) or `\[…\]` (display) spans for rendering in HTML. If the TeX is garbled, correct it directly in the Markdown:

- Inline math: `\(E = mc^2\)`
- Display math: `\[E = mc^2\]`

If Tesseract was used (which has no math support), equations appear as garbled text or are missing entirely. In that case, retype the TeX by hand or leave a placeholder.

---

### Common options

| Option                | Applies to       | Default        | Description                                                              |
| --------------------- | ---------------- | -------------- | ------------------------------------------------------------------------ |
| `--engine surya`      | `analyze`, `ocr` | `tesseract`    | Engine to use (`surya` or `tesseract` for OCR; `surya` only for analyze) |
| `--dpi 300`           | `render`         | `200`          | Page render resolution                                                   |
| `--pages 1,3,5`       | `deskew`         | all            | Limit to specific pages                                                  |
| `--formats html,epub` | `export`         | `html`         | Output formats                                                           |
| `--title "My Book"`   | `export`         | PDF filename   | Title for HTML/EPUB metadata                                             |
| `--author "J. Smith"` | `export`         | —              | Author for EPUB metadata                                                 |
| `--self-contained`    | `export`         | off            | Embed images as base64 in HTML output                                    |
| `--project-dir /path` | all              | `data/<stem>/` | Where to store project files                                             |
| `--force`             | most steps       | off            | Re-run step even if output exists                                        |

---

## Project directory layout

Each PDF gets its own project directory under `data/` (or `--project-dir`):

```
data/my_document/
├── pages/                  # Rendered page PNGs (original)
│   ├── page_0001.png
│   └── ...
├── pages_masked/           # Pages with figures and boilerplate masked out
│   └── masked_0001.png
├── images/                 # Extracted figure crops
│   ├── fig_1_1.png
│   └── ...
├── pages.json              # Page manifest (paths, dimensions)
├── boxes.json              # Bounding boxes (figures, tables, exclusion zones)
├── figures.json            # Figure metadata (crop paths, alt-text)
├── tables.json             # Table metadata (crop paths, OCR content)
├── ocr_raw.json            # Raw OCR output from Claude/Tesseract/Surya
├── ocr_edited.json         # User-corrected OCR (created by OCR editor)
├── structure.json          # Structured document model
└── output/
    ├── document.md
    ├── document.html
    └── document.epub
```

---

## GUI tools

### Bounding Box Editor

```bash
python gui/bbox_editor.py data/my_document/
```

| Action         | How                                              |
| -------------- | ------------------------------------------------ |
| Draw a new box | Select a box type button, then drag on the page  |
| Move a box     | Drag its interior                                |
| Resize a box   | Drag a corner handle (visible when selected)     |
| Delete a box   | Right-click → Delete, or select and press Delete |
| Zoom           | Ctrl+scroll or trackpad pinch                    |
| Pan            | Scroll (no modifier)                             |
| Navigate pages | ← → arrow keys, or Prev/Next buttons             |
| Save           | Ctrl+S or the Save button                        |

**Box types:**

- **Red — Figure**: image region to be cropped and excluded from OCR
- **Teal — Table**: table region; cropped, excluded from page OCR, and OCR'd separately
- **Blue — Exclusion**: boilerplate on this page (headers, footers, page numbers)
- **Purple — Caption zone**: lines in this zone are tagged as captions in OCR output
- **Orange — Endnote zone**: lines in this zone are tagged as endnotes in OCR output

### Alt-Text Editor

```bash
python gui/alt_text_editor.py data/my_document/
```

Write and review alt-text descriptions for figure crops. Navigate page by page; all figures detected on a page appear as thumbnails in the left panel — click one to view its crop at full size and edit the description in the text field below.

| Action         | How                                         |
| -------------- | ------------------------------------------- |
| Navigate pages | Ctrl+← / Ctrl+→ or Prev/Next buttons        |
| Select figure  | Click thumbnail in left panel               |
| Edit alt-text  | Type in the text field below the crop image |
| Zoom image     | Ctrl+scroll or trackpad pinch               |
| Pan image      | Scroll (no modifier), or drag               |
| Save           | Ctrl+S or the Save button                   |

A ✓ mark next to a thumbnail indicates that alt-text has already been written for that figure. Changes are written back to `figures.json` and are used by the `export` step to populate `alt` attributes in the HTML output.

### OCR Line Editor

```bash
python gui/ocr_editor.py data/my_document/
```

| Action           | How                                                                     |
| ---------------- | ----------------------------------------------------------------------- |
| Edit a line      | Click the text field and type                                           |
| Change line type | Use the type drop-down (body / heading1-3 / footnote / caption / other) |
| Delete a line    | Click ✕ button on that row                                              |
| Navigate pages   | Ctrl+← / Ctrl+→                                                         |
| Save             | Ctrl+S or the Save button                                               |

Clicking on a line highlights its position on the page thumbnail on the left. Edits are saved to `ocr_edited.json`, leaving the original `ocr_raw.json` untouched.

### Table Editor

```bash
python gui/table_editor.py data/my_document/
```

Shows each table's crop image alongside a monospace plain-text editor for its OCR content. Ideal for correcting or re-typing Markdown table syntax.

| Action          | How                                                        |
| --------------- | ---------------------------------------------------------- |
| Navigate tables | Ctrl+← / Ctrl+→ or Prev/Next buttons                       |
| Edit content    | Type directly in the right panel (monospace, no line-wrap) |
| Set format      | Use the Format drop-down: `markdown` or `preformatted`     |
| Zoom image      | Ctrl+scroll or trackpad pinch                              |
| Pan image       | Scroll (no modifier), or drag                              |
| Save            | Ctrl+S or the Save button                                  |

Changes are written back to `tables.json`. Set Format to `markdown` once you've corrected the table — it will then be rendered as a proper Markdown table in the assembled document rather than a code block.

---

## Configuration

All settings are in `config.py` and can be overridden via environment variables in `.env`:

| Variable                | Default           | Description                                                  |
| ----------------------- | ----------------- | ------------------------------------------------------------ |
| `ANTHROPIC_API_KEY`     | —                 | **Required.** Your Anthropic API key                         |
| `CLAUDE_MODEL`          | `claude-opus-4-6` | Claude model for OCR and analysis                            |
| `RENDER_DPI`            | `200`             | Page render resolution (try 300 for small print)             |
| `TESSERACT_LANG`        | `eng`             | Tesseract language code                                      |
| `HEADER_ZONE_FRACTION`  | `0.08`            | Top fraction of page considered a potential header           |
| `FOOTER_ZONE_FRACTION`  | `0.08`            | Bottom fraction of page considered a potential footer        |
| `BOILERPLATE_MIN_PAGES` | `3`               | Min pages a zone must appear on to become a global exclusion |
| `ALT_TEXT_MAX_CHARS`    | `300`             | Maximum length of generated alt-text                         |
| `DATA_ROOT`             | `./data`          | Root directory for project folders                           |

---

## Development container

A devcontainer configuration is included (`.devcontainer/`). It provides:

- Node.js 20, Claude Code CLI, `uv`, `poppler-utils`, and `tesseract-ocr` pre-installed

After opening the repo in VS Code with the Dev Containers extension, run:

```bash
cd pdf-converter
uv sync
cp .env.example .env
# add your API key to .env
```
