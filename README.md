# pdf-converter

Convert scanned PDF documents to Markdown, HTML, and EPUB using Claude Vision for OCR and layout analysis.

## Features

- **Claude Vision OCR** — sends each page to Claude for high-quality text extraction, with Tesseract or [Surya](https://github.com/VikParuchuri/surya) as local fallbacks
- **Automated layout detection** — figures, tables, and boilerplate (headers, footers, page numbers) are identified automatically before OCR runs
- **Table extraction** — table regions are cropped, masked out of OCR pages, and OCR'd separately to produce clean Markdown tables
- **Figure extraction** — image regions are cropped and saved separately; OCR never runs on figure interiors
- **Alt-text generation** — optional Claude Vision pass to write accessibility descriptions for each figure
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

# Create virtual environment and install all dependencies
uv sync

# Alternatively, to enable Surya as an option, instead run with --extra surya (Note that this will be more resource intensive)
uv sync --extra surya

# Activate the environment
source .venv/bin/activate

# Optionally, copy the environment template and add your Anthropic API key to enable use of the Anthropic API (funds required)
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY=sk-ant-...
```

---

## Usage

### Step-by-step pipeline

Each stage can be run independently, which is useful for re-running a single step after manual corrections. Some stage have multiple options

```bash
# 1. Render PDF pages to PNG images
python cli.py render my_document.pdf

# 2. Auto-detect figure regions and boilerplate exclusion zones (requires API key)

# Option 1 [recommended]: Use Anthropic API (API key and funds required)
python cli.py analyze my_document.pdf
# Option 2 : Upload the PDF to [claude.ai](https://claude.ai) with the prompt from `prompts/layout_analysis.md`, copy the JSON response into a file, then import it. (This will also create alt-text; no API key required):
python cli.py import-boxes my_document.pdf claude_response.json
# Option 3: Run locally (Surya required)
python cli.py analyze my_document.pdf --engine surya
# Option 4: Skip the layout analysis, and just create and empty boxes file (to be drawn manually)
python cli.py init-boxes my_document.pdf

# 3. Open the bounding box editor to review/correct detections
python gui/bbox_editor.py data/my_document/

# 4. Crop figures and produce masked page images
# Option 1: Just extract the figures
python cli.py extract my_document.pdf
# Option 2: Also create alt-text (Anthropic API key and funds required)
python cli.py extract my_document.pdf --alt-text

# 5. Run OCR on the masked pages
# Option 1 [recommended]: Use Tesseract
python cli.py ocr my_document.pdf --engine tesseract
# Option 2: Use Surya (slower, better for math, multilingual, and challenging documents)
python cli.py ocr my_document.pdf --engine surya
# Option 3: Use Anthropic API (API key and funds required)
python cli.py ocr my_document.pdf

# 6. Open the OCR editor to correct text line mistakes
python gui/ocr_editor.py data/my_document/

# 6b. Optaionally, open the table editor to correct table OCR (image + Markdown side by side)
python gui/table_editor.py data/my_document/

# 7. Build the document structure (paragraphs, headings, footnotes…)
python cli.py structure my_document.pdf

# 8. Assemble Markdown
python cli.py assemble my_document.pdf

# 8b. Edit the Markdown (in data/<dir>/output/document.md) to correct structural and other issues in any Markdown editor

# 9. Export to HTML and/or EPUB
python cli.py export my_document.pdf --formats html,epub
```

Edits made in the GUI tools are saved to `ocr_edited.json` and `boxes.json` in the project directory and are automatically used by subsequent pipeline steps.

In both cases, continue the pipeline from the `extract` step once you're happy with `boxes.json`.

Surya requires PyTorch and will download model weights on first use (~1–2 GB). It is slower than Claude on CPU but produces good results and does not require an API key.

---

### Combined pipeline

```bash
python cli.py convert my_document.pdf
```

This runs all steps in sequence and writes output to `data/my_document/output/`.

### Full pipeline with interactive bounding box review

```bash
python cli.py convert my_document.pdf --pause-after-analyze
```

Stops after layout analysis so you can open the bounding box editor, review the detected figure and exclusion zones, then press Enter to continue.

### Common options

| Option                   | Default        | Description                                   |
| ------------------------ | -------------- | --------------------------------------------- |
| `--formats md,html,epub` | `md`           | Output formats                                |
| `--engine tesseract`     | `claude`       | OCR engine: `claude`, `tesseract`, or `surya` |
| `--alt-text`             | off            | Generate alt-text for figures                 |
| `--dpi 300`              | `200`          | Page render resolution                        |
| `--project-dir /path`    | `data/<stem>/` | Where to store project files                  |
| `--force`                | off            | Re-run step even if output exists             |
| `--title "My Book"`      | PDF filename   | Title for HTML/EPUB metadata                  |
| `--author "J. Smith"`    | —              | Author for EPUB metadata                      |
| `--self-contained`       | off            | Embed images as base64 in HTML output         |

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
- **Orange — Notes zone**: lines in this zone are tagged as footnotes in OCR output

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
