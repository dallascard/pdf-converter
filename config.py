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
