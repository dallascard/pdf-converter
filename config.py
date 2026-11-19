"""
Central configuration for pdf-converter.

Settings are read from environment variables (or a .env file in the project root).
All paths in this file are defaults; the CLI overrides them per-project.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

# ---------------------------------------------------------------------------
# Claude model selection
# ---------------------------------------------------------------------------
# Used for layout analysis, OCR, and alt-text generation.
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-opus-4-6")

# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------
# DPI for page image rendering.  200 is a good balance between quality and
# file size for scanned documents; bump to 300 for very fine print.
RENDER_DPI: int = int(os.getenv("RENDER_DPI", "200"))

# Image format for rendered pages (PNG recommended — lossless).
RENDER_FORMAT: str = os.getenv("RENDER_FORMAT", "PNG")

# ---------------------------------------------------------------------------
# Image compression
# ---------------------------------------------------------------------------
# Figure/table crops larger than this (bytes) are converted to JPEG by the
# optional 'compress-figures' step.  Also used as the in-memory threshold in
# alt_text.py before sending page images to the Claude API (5 MB hard limit).
COMPRESS_MAX_BYTES: int = int(os.getenv("COMPRESS_MAX_BYTES", str(4_500_000)))

# JPEG quality used when compressing oversized images.  85 is visually
# lossless for scanned documents and typically reduces file size 5–10×.
COMPRESS_JPEG_QUALITY: int = int(os.getenv("COMPRESS_JPEG_QUALITY", "85"))

# Maximum pixel length on the longest side for in-memory compression used
# before Claude API calls.  Some PDFs render to very large pixel dimensions
# regardless of RENDER_DPI (e.g. documents using a non-standard UserUnit).
# 2048 is more than enough for Claude Vision to read figures and captions.
COMPRESS_MAX_DIM: int = int(os.getenv("COMPRESS_MAX_DIM", "2048"))

# ---------------------------------------------------------------------------
# Surya device
# ---------------------------------------------------------------------------
# "cpu" (default) avoids MPS out-of-memory errors on long documents.
# "auto" lets Surya pick the GPU (cuda/mps) when available; "cuda"/"mps" force one.
# Exported as TORCH_DEVICE, which Surya reads when it is first imported.
# An explicit TORCH_DEVICE in the environment takes precedence.
SURYA_DEVICE: str = os.getenv("SURYA_DEVICE", "cpu")
if SURYA_DEVICE != "auto":
    os.environ.setdefault("TORCH_DEVICE", SURYA_DEVICE)

# ---------------------------------------------------------------------------
# Surya memory management
# ---------------------------------------------------------------------------
# Each page is passed to Surya's layout predictor individually, with the GPU/MPS
# cache flushed between pages, to avoid out-of-memory errors on long documents.
# SURYA_BATCH_SIZE is kept here for future use but is not currently used by the
# layout analyzer or auto-rotate (both process one page at a time).
SURYA_BATCH_SIZE: int = int(os.getenv("SURYA_BATCH_SIZE", "1"))

# ---------------------------------------------------------------------------
# Layout analysis
# ---------------------------------------------------------------------------
# Minimum fraction of page area for a region to be considered a figure.
MIN_FIGURE_AREA_FRACTION: float = float(os.getenv("MIN_FIGURE_AREA_FRACTION", "0.01"))

# Pages must share a header/footer text block at the same relative position
# on at least this many pages for it to be flagged as boilerplate.
BOILERPLATE_MIN_PAGES: int = int(os.getenv("BOILERPLATE_MIN_PAGES", "3"))

# Fraction of page height that defines the "header zone" (top) and
# "footer zone" (bottom) used by the rule-based boilerplate detector.
HEADER_ZONE_FRACTION: float = float(os.getenv("HEADER_ZONE_FRACTION", "0.08"))
FOOTER_ZONE_FRACTION: float = float(os.getenv("FOOTER_ZONE_FRACTION", "0.08"))

# ---------------------------------------------------------------------------
# OCR masking
# ---------------------------------------------------------------------------
# Colour used to paint out figure regions and exclusion zones before OCR.
MASK_COLOUR: tuple = (255, 255, 255)  # white

# ---------------------------------------------------------------------------
# Tesseract fallback
# ---------------------------------------------------------------------------
TESSERACT_LANG: str = os.getenv("TESSERACT_LANG", "eng")

# Folder holding legacy-capable <lang>.traineddata files (from the
# tesseract-ocr/tessdata repo) for the "tesseract-legacy" engine (--oem 0).
# Homebrew's default tessdata lacks the legacy components.
TESSERACT_LEGACY_TESSDATA: str = os.path.expanduser(os.getenv("TESSERACT_LEGACY_TESSDATA", ""))

# ---------------------------------------------------------------------------
# Ollama (large vision-model OCR)
# ---------------------------------------------------------------------------
# The "ollama" engine sends each layout region to a local Ollama server.
# Pull the model first, e.g.:  ollama pull glm-ocr   /   ollama pull qwen3-vl:8b
OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "glm-ocr")
# Empty = choose automatically (GLM-OCR uses its own "Text Recognition:" prompt).
OLLAMA_PROMPT: str = os.getenv("OLLAMA_PROMPT", "")
OLLAMA_TIMEOUT: int = int(os.getenv("OLLAMA_TIMEOUT", "600"))

# ---------------------------------------------------------------------------
# vLLM (large vision-model OCR on a GPU server)
# ---------------------------------------------------------------------------
# The "vllm" engine works like "ollama" but talks to any OpenAI-compatible
# server (vLLM, SGLang, LM Studio …), e.g.:
#   vllm serve Qwen/Qwen3-VL-8B-Instruct
VLLM_HOST: str = os.getenv("VLLM_HOST", "http://localhost:8000")
# Empty = use the model the server is serving (if it serves exactly one).
VLLM_MODEL: str = os.getenv("VLLM_MODEL", "")
VLLM_API_KEY: str = os.getenv("VLLM_API_KEY", "")   # only if the server was started with --api-key
VLLM_PROMPT: str = os.getenv("VLLM_PROMPT", "")
VLLM_TIMEOUT: int = int(os.getenv("VLLM_TIMEOUT", "600"))
# Cap on output tokens per region; stops a model that falls into a repetition loop.
VLLM_MAX_TOKENS: int = int(os.getenv("VLLM_MAX_TOKENS", "4096"))
# Pages OCR'd in parallel; vLLM batches concurrent requests, so this is what keeps GPUs busy.
VLLM_CONCURRENCY: int = int(os.getenv("VLLM_CONCURRENCY", "16"))

# ---------------------------------------------------------------------------
# Alt-text generation
# ---------------------------------------------------------------------------
# Maximum length (characters) for generated alt-text strings.
ALT_TEXT_MAX_CHARS: int = int(os.getenv("ALT_TEXT_MAX_CHARS", "300"))

# ---------------------------------------------------------------------------
# Output / export
# ---------------------------------------------------------------------------
DEFAULT_EXPORT_FORMATS: list = ["md"]   # md | html | epub

# ---------------------------------------------------------------------------
# Data root — override with CLI --data-dir
# ---------------------------------------------------------------------------
DATA_ROOT: Path = Path(os.getenv("DATA_ROOT", Path(__file__).parent / "data"))
