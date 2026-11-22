"""
vlm_profiles.py — Per-model prompts, request settings and output cleanup
for the vision-language OCR engines (--engine ollama / vllm).

Each OCR model was trained with its own instruction and output format, and
does noticeably worse with a generic "transcribe this" prompt.  A profile is
picked by matching the served model name (lower-cased) against its ``match``
substrings, so keep the model's name in any ``--served-model-name``.

Prompts are copied verbatim from each project's own inference code (sources
noted per profile).  Region crops are sent one at a time, so the page-level
layout prompts some models offer are not used.

Output cleanup turns each model's reply into plain text lines:

- text regions: HTML (Chandra) and Markdown (most others) markup is
  removed, keeping only ``<math>`` (handled downstream like Surya's output);
- tables: HTML (Chandra, olmOCR, OvisOCR2) and OTSL (PaddleOCR-VL, MinerU,
  TeleOCR) are converted to Markdown tables; merged cells become empty cells.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

GENERIC_TEXT_PROMPT = (
    "Transcribe all text in this image exactly as written, one output line per "
    "printed line. Keep original spelling and punctuation. Output only the text."
)
GENERIC_TABLE_PROMPT = "Transcribe this table as a Markdown table. Output only the table."

# MinerU's and TeleOCR's clients use these settings for text and tables
# (plus a no-repeat-ngram logits processor that only works in-process).
_MINERU_TEXT_SAMPLING = {"top_p": 0.01, "presence_penalty": 1.0, "frequency_penalty": 0.05}
_MINERU_TABLE_SAMPLING = {"top_p": 0.01, "presence_penalty": 1.0, "frequency_penalty": 0.005}

# Chandra: chandra/prompts.py (OCR_PROMPT = "OCR this image to HTML." + PROMPT_ENDING)
_CHANDRA_TAGS = ["math", "br", "i", "b", "u", "del", "sup", "sub", "table", "tr", "td", "p", "th",
                 "div", "pre", "h1", "h2", "h3", "h4", "h5", "ul", "ol", "li", "input", "a", "span",
                 "img", "hr", "tbody", "small", "caption", "strong", "thead", "big", "code", "chem"]
_CHANDRA_ATTRS = ["class", "colspan", "rowspan", "display", "checked", "type", "border", "value",
                  "style", "href", "alt", "align", "data-bbox", "data-label"]
_CHANDRA_PROMPT = f"""OCR this image to HTML.

Only use these tags {_CHANDRA_TAGS}, and these attributes {_CHANDRA_ATTRS}.

Guidelines:
* Inline math: Surround math with <math>...</math> tags. Math expressions should be rendered in KaTeX-compatible LaTeX. Use display for block math.
* Tables: Use colspan and rowspan attributes to match table structure.
* Formatting: Maintain consistent formatting with the image, including spacing, indentation, subscripts/superscripts, and special characters.
* Images: Include a description of any images in the alt attribute of an <img> tag. Do not fill out the src property. Describe in detail inside the div tag. Also convert charts to high fidelity data, and convert diagrams to mermaid.
* Forms: Mark checkboxes and radio buttons properly.
* Text: join lines together properly into paragraphs using <p>...</p> tags.  Use <br> tags for line breaks within paragraphs, but only when absolutely necessary to maintain meaning.
* Chemistry: Use <chem>...</chem> tags for chemical formulas with reactive SMILES.
* Lists: Preserve indents and proper list markers.
* Use the simplest possible HTML structure that accurately represents the content of the block.
* Make sure the text is accurate and easy for a human to read and interpret.  Reading order should be correct and natural."""
# Chandra therefore returns one line per paragraph rather than per printed line.

# OvisOCR2: model card (OvisOCR2Parser.prompt), including the leading newline
_OVISOCR2_PROMPT = (
    "\nExtract all readable content from the image in natural human reading order and output "
    "the result as a single Markdown document. For charts or images, represent them using an "
    'HTML image tag: <img src="images/bbox_{left}_{top}_{right}_{bottom}.jpg" />, where left, '
    "top, right, bottom are bounding box coordinates scaled to [0, 1000). Format formulas as "
    "LaTeX. Format tables as HTML: <table>...</table>. Transcribe all other text as standard "
    "Markdown. Preserve the original text without translation or paraphrasing."
)

# olmOCR 2: olmocr/prompts/prompts.py, build_no_anchoring_v4_yaml_prompt()
_OLMOCR_PROMPT = (
    "Attached is one page of a document that you must process. "
    "Just return the plain text representation of this document as if you were reading it "
    "naturally. Convert equations to LateX and tables to HTML.\n"
    "If there are any figures or charts, label them with the following markdown syntax "
    "![Alt text describing the contents of the figure](page_startx_starty_width_height.png)\n"
    "Return your output as markdown, with a front matter section on top specifying values for "
    "the primary_language, is_rotation_valid, rotation_correction, is_table, and is_diagram "
    "parameters."
)

PROFILES: list[dict] = [
    {   # zai-org/GLM-OCR — glmocr/config.yaml (task_prompt_mapping, generation parameters)
        "name": "glm-ocr", "match": ("glm-ocr", "glm_ocr", "glmocr"),
        "text": "Text Recognition:", "table": "Table Recognition:",
        "sampling": {"top_p": 0.00001, "top_k": 1, "repetition_penalty": 1.1},
    },
    {   # PaddlePaddle/PaddleOCR-VL-1.6 — model card / vLLM recipe task mapping
        "name": "paddleocr-vl", "match": ("paddleocr-vl", "paddleocr_vl"),
        "text": "OCR:", "table": "Table Recognition:",
    },
    {   # opendatalab/MinerU2.5-Pro-2605-1.2B — mineru_vl_utils DEFAULT_PROMPTS / sampling params
        "name": "mineru", "match": ("mineru",),
        "system": "You are a helpful assistant.",
        "text": "\nText Recognition:", "table": "\nTable Recognition:",
        "sampling": _MINERU_TEXT_SAMPLING, "table_sampling": _MINERU_TABLE_SAMPLING,
        "min_edge": 28, "max_ratio": 50,
    },
    {   # XingChen-AGI/TeleOCR (formerly NaviDC-OCR) — TeleOCR/vlm_utils/TeleOCR_client.py
        "name": "teleocr", "match": ("teleocr", "navidc"),
        "system": "You are a helpful assistant.",
        "text": "\nPlease output the text content from the image.",
        "table": "\nThis is the image of a table. Please output the table in OTSL format.",
        "sampling": _MINERU_TEXT_SAMPLING, "table_sampling": _MINERU_TABLE_SAMPLING,
        "min_edge": 28, "max_ratio": 50,
    },
    {   # ATH-MaaS/OvisOCR2 — model card; one page-parsing prompt for everything
        "name": "ovisocr2", "match": ("ovisocr",),
        "text": _OVISOCR2_PROMPT, "table": _OVISOCR2_PROMPT,
        "min_pixels": 448 * 448,
    },
    {   # datalab-to/chandra-ocr-2 — chandra/prompts.py, chandra/model/vllm.py
        "name": "chandra", "match": ("chandra",),
        "text": _CHANDRA_PROMPT, "table": _CHANDRA_PROMPT,
        "sampling": {"top_p": 0.1},
        "min_pixels": 1792 * 28,
    },
    {   # rednote-hilab/dots.mocr (and dots.ocr) — dots_mocr/utils/prompts.py "prompt_ocr";
        # inference.py prefixes the image tokens so vLLM doesn't insert a newline
        "name": "dots", "match": ("dots.mocr", "dots.ocr", "dots_mocr", "dotsmocr", "dotsocr"),
        "text": "<|img|><|imgpad|><|endofimg|>Extract the text content from this image.",
        "table": "<|img|><|imgpad|><|endofimg|>Extract the text content from this image.",
    },
    {   # allenai/olmOCR-2-7B-1025 — olmocr v4 YAML prompt (front matter stripped on output)
        "name": "olmocr", "match": ("olmocr",),
        "text": _OLMOCR_PROMPT, "table": _OLMOCR_PROMPT,
    },
]

GENERIC_PROFILE = {"name": "generic", "match": (), "text": GENERIC_TEXT_PROMPT,
                   "table": GENERIC_TABLE_PROMPT}


def profile_for(model: str) -> dict:
    """The profile whose match string appears in *model* (e.g. a Hugging Face id)."""
    name = model.lower()
    for profile in PROFILES:
        if any(m in name for m in profile["match"]):
            return profile
    return GENERIC_PROFILE


def prepare_image(img, profile: dict):
    """Pad very elongated crops and upscale tiny ones, as each model's own client does.

    Qwen-family image processors reject extreme aspect ratios, and a single
    heading line cropped at 200 DPI can be 40× wider than tall.
    """
    from PIL import Image

    img = img.convert("RGB")
    max_ratio = profile.get("max_ratio", 50)
    w, h = img.size
    if max(w, h) / max(1, min(w, h)) > max_ratio:
        nw, nh = (w, -(-w // max_ratio)) if w > h else (-(-h // max_ratio), h)
        padded = Image.new("RGB", (nw, nh), (255, 255, 255))
        padded.paste(img, ((nw - w) // 2, (nh - h) // 2))
        img = padded
    w, h = img.size
    scale = max(profile.get("min_edge", 28) / min(w, h),
                (profile.get("min_pixels", 0) / (w * h)) ** 0.5, 1.0)
    if scale > 1.0:
        img = img.resize((round(w * scale), round(h * scale)), Image.Resampling.BICUBIC)
    return img


# ---------------------------------------------------------------------------
# Output cleanup
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```[\w-]*\n(.*?)\n?```$", re.S)
_THINK_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.S)        # reasoning ("Thinking") variants
_FRONT_MATTER_RE = re.compile(r"^---\n.*?\n---\n?", re.S)        # olmOCR's YAML header
_HTML_BLOCK_RE = re.compile(r"<(p|div|br|h[1-6]|li|span|table)\b", re.I)


def unwrap(text: str) -> str:
    """Strip reasoning blocks, YAML front matter and a wrapping code fence."""
    text = _THINK_RE.sub("", text.strip())
    text = _FRONT_MATTER_RE.sub("", text).strip()
    fenced = _FENCE_RE.match(text)
    return fenced.group(1).strip() if fenced else text


def text_lines(text: str) -> list[str]:
    """A text region's reply → plain lines (HTML and Markdown markup removed)."""
    text = unwrap(text)
    if _HTML_BLOCK_RE.search(text):
        text = re.sub(r"<img\b[^>]*>", "", text, flags=re.I)
        text = re.sub(r"<br\s*/?>|</(p|div|h[1-6]|li|tr)>", "\n", text, flags=re.I)
        text = re.sub(r"</?(?!math\b)[a-zA-Z][^>]*>", "", text)     # keep <math> for the exporter
        text = html.unescape(text)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)              # Markdown image refs
        line = re.sub(r"<img\b[^>]*>", "", line, flags=re.I)
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)                 # heading markers
        line = re.sub(r"(\*\*|__)(.+?)\1", r"\2", line)               # bold
        if line.strip():
            lines.append(line.strip())
    return lines


def table_content(text: str) -> tuple[str, str]:
    """A table crop's reply → (content, content_format) for tables.json."""
    text = unwrap(text)
    rows = None
    if re.search(r"<table\b", text, re.I):
        rows = _html_table_rows(text)
    elif re.search(r"<(fcel|ecel|lcel|ucel|xcel)>", text):
        rows = _otsl_rows(text)
    if rows:
        return _markdown_table(rows), "markdown"
    md_rows = [r for r in text.splitlines() if r.strip()]
    if md_rows and all(r.lstrip().startswith("|") for r in md_rows):
        return text, "markdown"
    return text, "preformatted"


def _cell_text(s: str) -> str:
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"</?(?!math\b)[a-zA-Z][^>]*>", "", s)
    return " ".join(html.unescape(s).split())


def _otsl_rows(text: str) -> list[list[str]]:
    """OTSL: <fcel>text / <ecel> empty / <lcel>,<ucel>,<xcel> merged; rows end with <nl>."""
    rows = []
    for raw in re.split(r"<nl>", text):
        parts = re.split(r"(<fcel>|<ecel>|<lcel>|<ucel>|<xcel>|<ched>|<rhed>|<srow>)", raw)
        cells, tag = [], None
        for part in parts:
            if part.startswith("<") and part.endswith(">") and part[1:-1] in (
                    "fcel", "ecel", "lcel", "ucel", "xcel", "ched", "rhed", "srow"):
                tag = part
                cells.append("")
            elif tag in ("<fcel>", "<ched>", "<rhed>", "<srow>") and cells:
                cells[-1] += part
        if cells:
            rows.append([_cell_text(c) for c in cells])
    return rows


class _TableParser(HTMLParser):
    """Collect the first <table>'s cells, expanding colspan/rowspan into empty cells."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.rows: list[list[str]] = []
        self._cell: list[str] | None = None
        self._span = (1, 1)
        self._pending: dict[int, int] = {}     # column → rows still covered by a rowspan
        self._done = False

    def handle_starttag(self, tag, attrs):
        if self._done:
            return
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th") and self.rows:
            a = dict(attrs)
            self._span = (_int(a.get("colspan")), _int(a.get("rowspan")))
            self._cell = []
        elif self._cell is not None:
            self._cell.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag):
        if self._done:
            return
        if tag in ("td", "th") and self._cell is not None:
            row = self.rows[-1]
            while self._pending.get(len(row), 0) > 0:          # skip columns covered from above
                self._pending[len(row)] -= 1
                row.append("")
            colspan, rowspan = self._span
            for i in range(colspan):
                if rowspan > 1:
                    self._pending[len(row)] = rowspan - 1
                row.append(_cell_text("".join(self._cell)) if i == 0 else "")
            self._cell = None
        elif tag == "tr" and self.rows:
            row = self.rows[-1]
            while self._pending.get(len(row), 0) > 0:
                self._pending[len(row)] -= 1
                row.append("")
        elif tag == "table":
            self._done = True
        elif self._cell is not None:
            self._cell.append(f"</{tag}>")

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_entityref(self, name):
        self.handle_data(f"&{name};")

    def handle_charref(self, name):
        self.handle_data(f"&#{name};")


def _int(v) -> int:
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return 1


def _html_table_rows(text: str) -> list[list[str]]:
    parser = _TableParser()
    parser.feed(text)
    return [r for r in parser.rows if r]


def _markdown_table(rows: list[list[str]]) -> str:
    width = max(len(r) for r in rows)
    rows = [[c.replace("|", "\\|") for c in r] + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)
