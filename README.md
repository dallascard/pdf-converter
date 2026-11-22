# pdf-converter

Convert scanned PDF documents to Markdown, HTML, and EPUB using an interactive pipeline built around Surya (or tesseract) and Claude. Handles figures, tables, captions, footnotes/endnotes, headings, alt-text, and exclusion zones, and emphasizes human verification / correction after each stage (layout detection, OCR, alt-text creation, and assembly into Markdown).

## Features

- **Surya layout detection** — figures, tables, and boilerplate (headers, footers, page numbers) are identified automatically before OCR runs; manual annotation and claude.ai import are also supported
- **Surya / Tesseract OCR** — Surya produces better results (especially for math and multilingual text); Tesseract is a lighter fallback that still performs well. Tesseract's pre-neural legacy engine, GOCR and GNU Ocrad (both early-2000s engines) are also available as deliberately weaker, historically representative baselines. Large vision models such as GLM-OCR and Qwen3-VL can be run through a local Ollama server
- **Table extraction** — table regions are cropped, masked out of OCR pages, and OCR'd separately to produce clean Markdown tables
- **Figure extraction** — image regions are cropped and saved separately; OCR never runs on figure interiors
- **Alt-text generation** — optional step to write accessibility descriptions for figures, via Claude API or claude.ai (no API key required)
- **Structure detection** — headings, footnotes/endnotes, and figure captions are identified, tagged, OCRed, and integrated into a easy-to-edit Markdown format
- **Footnote → endnote conversion** — all footnotes/endnotes are collected and appended as a numbered endnote section
- **Multi-format export** — output to `.md`, `.html`, and `.epub`; `--self-contained` embeds images as base64 for a single shareable HTML file
- **Bounding Box Editor** — PyQt6 GUI to review and correct auto-detected figure, table, and exclusion zones
- **OCR Line Editor** — PyQt6 GUI for line-by-line OCR correction, with each line's image crop shown alongside its text field
- **OCR Table Editor** — PyQt6 GUI for table-by-table OCR correction, with each table's image crop shown alongside its Markdown table
- **Alt-text Editor** — PyQt6 GUI for editing alt-text for each figure, shown in context.
- **Auto-rotate** — Automatically detect and correct 90°/180°/270° page rotation using Surya header-position detection, before deskewing
- **Skew correction** — Automatically correct small rotational skew (sub-degree) in scanned pages
- **Image directory input** — Use a directory of TIF/PNG/JPG files as the source instead of a PDF; all downstream steps work without modification
- **Export masked pages** — Export a PDF of the page images with any combination of region types (exclusions, figures, tables, etc.) painted white, useful for downstream processing or review
- **Text JSON export** — Export per-page heading and body text, followed by that page's notes, as a simple JSON array (`output/document.json`); can be run directly after OCR without needing the Assemble step

## Workflow

This system uses an interactive, multi-step pipeline approach, emphasising human oversight at each stage. The **Workbench** (`gui/workbench.py`) is the primary interface — it lets you run every step, inspect page images with bounding-box overlays, and edit the assembled Markdown, all in one window. The CLI (`cli.py`) is available as a fallback for scripting or when you need finer control over individual steps.

The steps involved are:

1. Load the source: either a PDF, or a directory of per-page image files (TIF, PNG, JPG, …). For PDFs, run Render to split into per-page PNGs.
2. Optionally run Auto-rotate to fix pages that are 90°/180°/270° out of orientation.
3. Optionally run Deskew to correct small rotational skew from scanning.
4. Run layout analysis to identify figures, tables, headings, captions, notes, and exclusion zones (or draw these manually if you prefer).
5. Review, correct, and/or add bounding boxes in the Bounding Box Editor.
6. Automatically extract figures and produce masked page images (Confirm layout step).
7. Optionally, generate alt-text for figures (Claude API or claude.ai), then review in the Alt-Text Editor.
8. Run OCR (Surya by default).
9. Optionally, correct OCR lines in the OCR Editor and table content in the Table Editor.
10. Automatically assemble everything into a Markdown document.
11. Edit the Markdown directly in the Workbench (or any external editor) to fix heading levels, caption placement, endnote positions, etc.
12. Export to HTML and/or EPUB, optionally including figures directly in a single output file.
13. Optionally, export masked pages as a PDF — choose which region types (exclusions, figures, tables, etc.) to paint white.
14. Optionally, export per-page text as JSON — headings and body text followed by notes, one entry per page.

Steps that have already produced output are not re-run unless you explicitly choose to overwrite them.

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
# (which requires manually drawing all bounding boxes),
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

### Workbench (recommended)

Open the Workbench and load a PDF — everything else can be done from there:

```bash
python gui/workbench.py
```

The left panel lists every pipeline step in order. Each CLI step shows a status dot (● green = output exists, ▶ yellow = in process, run ● grey = not yet run) and a ▶ Run button; GUI steps open the relevant editor in a separate window. The centre panel shows a continuous zoomable view of all pages with bounding-box overlays once layout analysis has run. The right panel shows editable **Markdown** with syntax highlighting once the assemble markdown step has been run.

Steps whose output already exists will ask whether to overwrite before re-running. Edits saved from any of the sub-editors (Bounding Box Editor, OCR Editor, etc.) are detected automatically and the Workbench refreshes without needing a restart.

---

### Advanced / CLI reference

Each pipeline step can also be run directly from the command line, which is useful for scripting, batch processing, or when you need options not exposed in the Workbench.

```bash
# 1. Render PDF pages to PNG images
python cli.py render my_document.pdf

# Alternative to render: import a directory of per-page images (TIF, PNG, JPG, …)
python cli.py import-pages /path/to/scans/ --project-dir data/my_document/

# 1b. Optionally, fix pages that are 90°/180°/270° out of orientation
python cli.py auto-rotate my_document.pdf

# 1c. Optionally, correct small rotational skew in scanned pages
python cli.py deskew my_document.pdf               # all pages
python cli.py deskew my_document.pdf --pages 2,5   # specific pages only

# 2. Detect layout (figures, tables, exclusion zones)

# Option A: Use Surya (requires installing with --extra surya; see above)
python cli.py analyze my_document.pdf

# Option B: Import layout from claude.ai (no API key required)
#   Upload the PDF with the prompt from prompts/layout_analysis.md, then:
python cli.py import-boxes my_document.pdf claude_response.json

# Option C: Draw boxes manually, after running init-boxes
python cli.py init-boxes my_document.pdf

# 3. Edit bounding boxes
python gui/bbox_editor.py data/my_document/

# 4. Extract figures and produce masked page images ("Confirm layout" in the Workbench).
#    Re-run with --force after any later box edits: OCR reads these masked pages,
#    and refuses to run if boxes.json has changed since (override: --skip-layout-check)
python cli.py extract my_document.pdf

#    Check whether the masked pages still match boxes.json, and whether existing
#    OCR output was made from out-of-date masks
python cli.py check-layout my_document.pdf

# 5. Optionally, generate alt-text for figures

# Option A: Automatically via Claude API (API key required)
python cli.py get-alt-text my_document.pdf

# Option B: Via claude.ai (no API key required)
python cli.py export-boxes my_document.pdf          # writes figures_prompt.json
# … upload to claude.ai, save response as alt_text_response.json …
python cli.py import-alt-text my_document.pdf alt_text_response.json
# Then review / edit in the Alt-Text Editor:
python gui/alt_text_editor.py data/my_document/

# 6. Run OCR

# Option A: using Surya (better accuracy)
python cli.py ocr my_document.pdf --engine surya

# Option B: using tesseract (lighter-weight fallback)
python cli.py ocr my_document.pdf --engine tesseract

# Option C: Tesseract's legacy (pre-neural) engine, a weaker historical baseline
# Needs legacy model files; see "Tesseract legacy engine" below
python cli.py ocr my_document.pdf --engine tesseract-legacy

# Option D: GOCR, an early-2000s engine (macOS: brew install gocr; Debian/Ubuntu: apt-get install gocr)
python cli.py ocr my_document.pdf --engine gocr

# Option E: GNU Ocrad, an early-2000s engine (macOS: brew install ocrad; Debian/Ubuntu: apt-get install ocrad)
python cli.py ocr my_document.pdf --engine ocrad

# Option F: a large vision model served by Ollama (see "Ollama engine" below)
python cli.py ocr my_document.pdf --engine ollama                              # GLM-OCR (default)
python cli.py ocr my_document.pdf --engine ollama --ollama-model qwen3-vl:8b

# Option G: a larger vision model on a GPU server via vLLM (see "vLLM engine" below)
python cli.py ocr --project-dir data/my_document --engine vllm

# 7. Optionally, correct OCR output (line by line and tables)
python gui/ocr_editor.py data/my_document/
python gui/table_editor.py data/my_document/

# 8. Assemble into Markdown
python cli.py assemble my_document.pdf

# 9. Edit output/document.md to fix any remaining issues (see "Editing the Markdown" below)

# 10. Export
python cli.py export my_document.pdf --formats html,epub --self-contained

# 11. Optionally, export page images as a PDF with selected regions masked white
python cli.py export-masked my_document.pdf --mask exclusions
python cli.py export-masked my_document.pdf --mask exclusions --mask figures --mask tables

# 12. Optionally, export per-page text as JSON (can be run right after OCR, before assemble)
python cli.py export-text my_document.pdf
```

Surya requires PyTorch and downloads model weights on first use (~1–2 GB). It is slower than Tesseract on CPU but produces better results, especially for documents with math or non-English text.

#### Tesseract legacy engine

`--engine tesseract-legacy` runs Tesseract with `--oem 0`, the character-pattern engine used before Tesseract 4. It needs legacy model files, which Homebrew's Tesseract does not include. Download them into a separate folder and point `.env` at it:

```bash
mkdir -p ~/tessdata_legacy
curl -L -o ~/tessdata_legacy/eng.traineddata \
  https://github.com/tesseract-ocr/tessdata/raw/main/eng.traineddata
echo 'TESSERACT_LEGACY_TESSDATA=~/tessdata_legacy' >> .env
```

For other languages, download the matching `<lang>.traineddata` into the same folder. Expect typographic ligature characters (`ﬁ`, `ﬂ`) in its output.

#### GOCR

`--engine gocr` runs GOCR and keeps its output as-is, to represent what OCR from that era produced: unrecognised characters stay as `_`, and stray lines GOCR invents (for example from descenders) are kept. Line positions come from GOCR's XML output; where GOCR reports an impossible line box, the box is rebuilt from the line's character positions. Tables use GOCR's plain-text output.

#### Ocrad

`--engine ocrad` runs GNU Ocrad, also keeping its output as-is (`_` for unrecognised characters). Line positions come from its results file (`ocrad -x`), with each line's box covering all of its character boxes. Ocrad sometimes reports a stray, oversized character box, which makes that line's box taller than the text. Tables use Ocrad's plain-text output.

#### Ollama engine

`--engine ollama` sends text to a large vision-language model running in [Ollama](https://ollama.com) on your own machine. Install Ollama, then pull a model:

```bash
ollama pull glm-ocr        # 0.9B document-OCR model, ~2.2 GB (default)
ollama pull qwen3-vl:8b    # 8B general vision model, ~6 GB; slower, often stronger
```

In the Workbench, pick **GLM-OCR (Ollama)** or **Qwen3-VL-8B (Ollama)** from the OCR engine menu. On the command line, choose the model with `--ollama-model`, or set `OLLAMA_MODEL` in `.env`.

These models return plain text rather than positioned lines, so the engine works one region at a time. Each paragraph and heading region from layout detection is cropped from the masked page and sent to the model on its own, and the returned lines are spread evenly down that region as approximate line boxes. Caption, note and table regions are each sent separately too. If a page has no paragraph regions, layout detection runs first (as for the Surya engine); failing that, the whole page is sent at once. Regions left blank by masking are skipped, because these models tend to invent text for empty images.

#### vLLM engine (GPU server)

`--engine vllm` works the same way as `--engine ollama`, but sends regions to a server that speaks the OpenAI-compatible API. That is usually [vLLM](https://docs.vllm.ai) running on a machine with GPUs, but SGLang and LM Studio work too. Several pages are OCR'd at once (`--concurrency`, default 16), so the server can batch requests and keep its GPUs busy.

To run OCR on a GPU server:

1. On your own machine, run every step up to and including `extract`. Make sure layout analysis has produced paragraph regions; the server doesn't need Surya if `boxes.json` already has them.
2. Copy the repository and the project folder (`data/my_document/`) to the server, and run `uv sync` there.
3. Start a model, for example:

   ```bash
   vllm serve Qwen/Qwen3-VL-8B-Instruct --max-model-len 16384                            # one GPU; good first test
   vllm serve Qwen/Qwen3-VL-32B-Instruct --tensor-parallel-size 2 --max-model-len 16384  # split across two GPUs
   ```

   Always set `--max-model-len`. Qwen3-VL defaults to a 262,144-token context, which reserves more GPU memory than an A6000 has; each request here is a single region, so 16384 is plenty. Recent vLLM releases default to CUDA 13 and need a recent NVIDIA driver. With a CUDA 12.x driver, install a `cu129` build: vLLM 0.32.0 is the newest release that has one.

4. In a second shell, run OCR (and, if you want it, the text export):

   ```bash
   python cli.py ocr --project-dir data/my_document --engine vllm --force
   python cli.py export-text --project-dir data/my_document --force
   ```

   With one model served, it is picked automatically; otherwise use `--vllm-model`. Use `--vllm-host` if the server isn't at `http://localhost:8000`.

5. Copy `ocr_raw.json` and `tables.json` (and `output/document.json`) back into the project folder on your own machine. **Delete your local `ocr_edited.json` if there is one**: later steps prefer it over `ocr_raw.json`, so old corrections would otherwise hide the new OCR.

If you can reach the server over SSH while you work, you can skip the copying. Forward the port with `ssh -L 8000:localhost:8000 <server>` and run `--engine vllm` on your own machine.

##### Supported OCR models

Each dedicated OCR model was trained with its own instruction and output format. The engine recognises the model from its name and uses that model's own prompts, request settings and output cleanup, copied from each project's inference code (see `core/vlm_profiles.py`). Recognition is by substring, so keep the model's name in any `--served-model-name`. Any other model gets a plain "transcribe this text" prompt.

| Model | Size | Licence | Serve with |
|---|---|---|---|
| PaddleOCR-VL-1.6 | 0.9B | Apache 2.0 | `vllm serve PaddlePaddle/PaddleOCR-VL-1.6 --trust-remote-code --max-num-batched-tokens 16384 --no-enable-prefix-caching --mm-processor-cache-gb 0` |
| GLM-OCR | 0.9B | MIT | `vllm serve zai-org/GLM-OCR --speculative-config '{"method": "mtp", "num_speculative_tokens": 3}'` |
| OvisOCR2 | 0.8B | Apache 2.0 | `vllm serve ATH-MaaS/OvisOCR2 --max-model-len 16384 --limit-mm-per-prompt '{"image": 1}' --mm-processor-cache-gb 0 --no-enable-prefix-caching` |
| MinerU2.5-Pro | 1.2B | see model card | `vllm serve opendatalab/MinerU2.5-Pro-2605-1.2B` |
| TeleOCR | 1.2B | Apache 2.0 | needs its vLLM plugin, which pins vLLM 0.11.0: use a separate environment (below) |
| dots.mocr | 3B | MIT | `vllm serve rednote-hilab/dots.mocr --trust-remote-code --chat-template-content-format string` |
| Chandra OCR 2 | 5.3B | modified OpenRAIL-M (free for research, personal use and small startups) | `vllm serve datalab-to/chandra-ocr-2 --max-model-len 16384` |
| olmOCR 2 | 7B | Apache 2.0 | `vllm serve allenai/olmOCR-2-7B-1025 --max-model-len 16384` |
| Qwen3-VL (general model, baseline) | 8B / 32B | Apache 2.0 | see above |

TeleOCR's custom model code only works with vLLM 0.11.0, so give it its own environment:

```bash
git clone https://github.com/caipeng328/TeleOCR.git
uv venv ~/teleocr-env --python 3.10 && source ~/teleocr-env/bin/activate
uv pip install vllm==0.11.0 ./TeleOCR/TeleOCR-vllm
vllm serve XingChen-AGI/TeleOCR --max-model-len 16384
```

What differs between models:

- **Prompts:** most use a short task prompt (`OCR:`, `Text Recognition:`, `Please output the text content from the image.`); OvisOCR2, Chandra and olmOCR use their full-page parsing prompt for every region.
- **Request settings:** MinerU and TeleOCR get their system prompt and presence/frequency penalties against repetition; GLM-OCR gets `repetition_penalty 1.1`; everything is greedy (temperature 0).
- **Image preparation:** very elongated crops (such as a single heading line) are padded to at most 50:1, and small crops are upscaled to each model's minimum size.
- **Output:** Markdown and HTML markup is removed from text (Chandra's HTML, `#` headings, `**bold**`, image placeholders), keeping `<math>` for the exporter. Table replies in HTML (Chandra, olmOCR, OvisOCR2) or OTSL (PaddleOCR-VL, MinerU, TeleOCR) are converted to Markdown tables; merged cells become empty cells.
- **Line breaks:** Chandra's prompt asks it to join printed lines into paragraphs, so its regions come back as one line per paragraph.

Set `VLLM_PROMPT` to override a model's text prompt. Reasoning blocks (`<think>…</think>`), a leading YAML header (olmOCR) and a wrapping code fence are stripped from replies. Each reply is capped at `VLLM_MAX_TOKENS`, and a warning is logged when a region hits the cap, which usually means the model fell into a loop.

---

### Editing the Markdown

After `assemble`, open `output/document.md` in any text editor or the workbench GUI and work through the items below. For best results, using an editor such as Macdown which also renders the markdown may be helpful. Finally, re-run `export` once you are happy with it.

#### Heading levels

Surya and Tesseract sometimes assign the wrong heading level, or miss headings entirely and leave them as body text.

- **Wrong level** — change `##` to `#` (or vice versa) as needed.
- **Missed heading** — prefix the line with `#`, `##`, or `###`.
- **Falsely detected heading** — remove the `#` prefix; the line becomes a normal paragraph.

#### Rejoining split paragraphs

OCR sometimes breaks a single paragraph across two elements when there is a column boundary, a figure interruption, or an unusual line gap. The symptom is two short paragraphs that read as one continuous thought. Simply delete the blank line between them (in Markdown, a blank line = paragraph break).

#### Missing footnote markers in body text

The assembler attempts to detect inline footnote markers (superscript digits) automatically, but may miss some. Endnotes are located in text using `[^N]`, and the actual notes appear at the end altogether as `[^N]: ...`

If a `[^N]` reference is absent from the body text, add it manually at the right word:

```markdown
spatial transformations[^2] have become popular.
```

If an endnote is missing at the end of the document, simply add it at the end of the Markdown:

The corresponding endnote `[^2]: …` should already be in the Notes section at the bottom of the file. If the endnote itself is missing, add it there.

#### Figure positioning

Figures are inserted at the Y position where they appear on the page, which does not always match the intended reading position. Move the `<figure>…</figure>` block to where it belongs in the narrative — typically just after the first paragraph that refers to it.

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

#### Table caption placement

Tables with captions are wrapped in `<figure markdown="1">` tags, which allows the Markdown table syntax inside to be rendered correctly alongside a `<figcaption>`. To add a caption to a table without one, use the following format:

```html
<figure markdown="1">
  | Header 1 | Header 2 | | -------- | -------- | | Cell 1 | Cell 2 |

  <figcaption>Table 1: This is a table caption.</figcaption>
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

#### Footnote text errors

OCR of small footnote text is error-prone. Check the Notes section at the end of the file and correct any garbled words. Also check:

- **Extra period at the start** — the assembler strips `1.` markers but may leave a stray `.` if the OCR inserted a space: `. Author name` → `Author name`.
- **Run-together footnotes** — if two footnote texts were merged into one `[^N]: …` entry, split them and add the missing `[^M]: …` entry.
- **Footnote number mismatch** — if a `[^N]` reference in the body has no matching `[^N]: …` at the bottom, either add the missing endnote or renumber to match.
- **Split footnotes** - If a line of a footnote begins with a number (e.g., a page number), it may be interpreted as a separate note, and appear with that number. Note that are split over two pages may also cause problems, and will need to be manually corrected.

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

## GUI tools

### Workbench

The primary interface for the full conversion pipeline. Run every step, inspect pages, and edit the assembled Markdown — all without touching the command line.

```bash
python gui/workbench.py my_document.pdf
python gui/workbench.py          # opens a file chooser
```

| Panel  | Contents                                                                                                                                                                                                                 |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Left   | Pipeline steps in order; each CLI step has a status dot (● green = done, ▶ yellow = running, ● grey = pending), an optional engine selector, and a ▶ Run button; GUI steps open the relevant editor as a separate window |
| Centre | Continuous scrollable view of all pages with bounding-box overlays; Ctrl+scroll or pinch to zoom; ◀/▶ jump to previous/next page                                                                                         |
| Right  | Editable assembled Markdown with syntax highlighting, dark/light mode toggle, and font size controls; appears once the Assemble step has run                                                                             |
| Bottom | Log output from running CLI commands                                                                                                                                                                                     |

- Clicking ▶ when a step's output already exists asks whether to overwrite.
- The Markdown tab is editable; **💾 Save Markdown** writes back to `output/document.md`. Assemble warns you if there are unsaved edits.
- Changes saved from any sub-editor (boxes, OCR, tables) are detected automatically and the Workbench refreshes without a restart.
- The **A− / A+** buttons and Ctrl+scroll adjust the Markdown editor font size. The **☀/🌙** button toggles dark/light mode.

---

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
- **Blue — Exclusion**: boilerplate on this page only (headers, footers, page numbers)
- **Sky blue, dashed — Global Exclusion**: like Exclusion, but applied to every page; draw once and it appears on all pages — move or resize it on any page and it updates everywhere
- **Purple — Caption zone**: lines in this zone are tagged as captions in OCR output
- **Orange — Endnote zone**: lines in this zone are tagged as endnotes in OCR output
- **Green — Heading zone**: lines in this zone are tagged as Heading 1 (# )

### Alt-Text Editor

```bash
python gui/alt_text_editor.py data/my_document/
```

Write and review alt-text descriptions for figure crops. Navigate page by page; all figures detected on a page appear as thumbnails in the middle panel — click one to view its crop at full size and edit the description in the text field below.

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
| `CLAUDE_MODEL`          | `claude-opus-4-6` | Claude model used for alt-text generation                    |
| `RENDER_DPI`            | `200`             | Page render resolution (try 300 for small print)             |
| `TESSERACT_LANG`        | `eng`             | Tesseract language code                                      |
| `TESSERACT_LEGACY_TESSDATA` | —             | Folder with legacy `<lang>.traineddata` files; required for `--engine tesseract-legacy` |
| `SURYA_DEVICE`          | `cpu`             | Device for Surya models: `cpu`, `auto` (GPU if available), `mps`, or `cuda`. CPU avoids GPU out-of-memory errors on long documents |
| `OLLAMA_MODEL`          | `glm-ocr`         | Model for `--engine ollama` (e.g. `qwen3-vl:8b`); must be pulled with `ollama pull` |
| `OLLAMA_HOST`           | `http://localhost:11434` | Address of the Ollama server                           |
| `OLLAMA_PROMPT`         | —                 | Custom text prompt for `--engine ollama`; by default each known OCR model gets its own prompt (`core/vlm_profiles.py`) and other models a plain transcription instruction |
| `OLLAMA_TIMEOUT`        | `600`             | Seconds to wait for each Ollama request                      |
| `VLLM_HOST`             | `http://localhost:8000` | Address of the vLLM / OpenAI-compatible server for `--engine vllm` |
| `VLLM_MODEL`            | —                 | Model for `--engine vllm`; empty = the one model the server serves |
| `VLLM_API_KEY`          | —                 | Only needed if the server was started with `--api-key`       |
| `VLLM_PROMPT`           | —                 | Custom text prompt for `--engine vllm` (same defaults as `OLLAMA_PROMPT`) |
| `VLLM_CONCURRENCY`      | `16`              | Pages OCR'd in parallel with `--engine vllm`                 |
| `VLLM_MAX_TOKENS`       | `4096`            | Output cap per region; stops runaway repetition              |
| `VLLM_TIMEOUT`          | `600`             | Seconds to wait for each request                             |
| `HEADER_ZONE_FRACTION`  | `0.08`            | Top fraction of page considered a potential header           |
| `FOOTER_ZONE_FRACTION`  | `0.08`            | Bottom fraction of page considered a potential footer        |
| `BOILERPLATE_MIN_PAGES` | `3`               | Min pages a zone must appear on to become a global exclusion |
| `ALT_TEXT_MAX_CHARS`    | `300`             | Maximum length of generated alt-text                         |
| `DATA_ROOT`             | `./data`          | Root directory for project folders                           |

---

### Common options

| Option                | Applies to              | Default        | Description                                                              |
| --------------------- | ----------------------- | -------------- | ------------------------------------------------------------------------ |
| `--engine surya`      | `analyze`, `ocr`        | `tesseract`    | Engine to use (`surya`, `tesseract`, `tesseract-legacy`, `gocr`, `ocrad`, `ollama` or `vllm` for OCR; `surya` only for analyze) |
| `--dpi 300`           | `render`                | `200`          | Page render resolution                                                   |
| `--pages 1,3,5`       | `deskew`, `auto-rotate` | all            | Limit to specific pages                                                  |
| `--formats html,epub` | `export`                | `html`         | Output formats                                                           |
| `--title "My Book"`   | `export`                | PDF filename   | Title for HTML/EPUB metadata                                             |
| `--author "J. Smith"` | `export`                | —              | Author for EPUB metadata                                                 |
| `--self-contained`    | `export`                | off            | Embed images as base64 in HTML output                                    |
| `--mask exclusions`   | `export-masked`         | `exclusions`   | Region type to paint white; repeat for multiple types                    |
| `--project-dir /path` | all                     | `data/<stem>/` | Where to store project files                                             |
| `--force`             | most steps              | off            | Re-run step even if output exists                                        |

---

## Project directory layout

Each PDF gets its own project directory under `data/` (or `--project-dir`):

```
data/my_document/
├── pages/                  # Rendered page PNGs (original)
│   ├── page_0001.png
│   └── ...
├── pages_masked/           # Pages with figures and boilerplate masked out (for OCR)
│   ├── masked_0001.png
│   └── layout_stamp.json   # Fingerprint of the boxes each mask was made from (checked before OCR)
├── images/                 # Extracted figure and table crops
│   ├── fig_1_1.png
│   └── ...
├── pages.json              # Page manifest (paths, dimensions, DPI)
├── rotate.json             # Auto-rotate results (written by auto-rotate step)
├── deskew.json             # Deskew results (written by deskew step)
├── boxes.json              # Bounding boxes (figures, tables, exclusion zones, …)
├── figures.json            # Figure metadata (crop paths, alt-text)
├── tables.json             # Table metadata (crop paths, OCR content)
├── ocr_raw.json            # Raw OCR output from Surya/Tesseract
├── ocr_edited.json         # User-corrected OCR (created by OCR editor)
├── structure.json          # Structured document model
└── output/
    ├── document.md
    ├── document.html
    ├── document.epub
    ├── masked_pages.pdf    # Written by export-masked (optional)
    └── document.json       # Written by export-text (optional)
```

## License

This project is released under an [Apache 2.0 license](LICENSE).
