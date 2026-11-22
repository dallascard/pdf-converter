"""
Memory diagnostic for Surya layout detection.

Usage:
    python scripts/diagnose_layout_memory.py data/my_book/pages/page_0027.png --repeat 15
    python scripts/diagnose_layout_memory.py data/my_book/ --limit 40
    python scripts/diagnose_layout_memory.py data/my_book/ --limit 40 --no-empty-cache

Single image + --repeat: if memory still climbs, the growth is inside Surya/torch,
not caused by different page contents.
Project dir: runs pages in order, as the analyze step does.
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402,F401  — sets TORCH_DEVICE from SURYA_DEVICE before Surya loads


def _mem_report() -> str:
    parts = []
    try:
        import psutil
        parts.append(f"rss={psutil.Process().memory_info().rss / 2**30:.2f}G")
    except ImportError:
        pass
    try:
        import torch
        if torch.backends.mps.is_available():
            parts.append(f"mps_alloc={torch.mps.current_allocated_memory() / 2**30:.2f}G")
            parts.append(f"mps_driver={torch.mps.driver_allocated_memory() / 2**30:.2f}G")
        elif torch.cuda.is_available():
            parts.append(f"cuda_alloc={torch.cuda.memory_allocated() / 2**30:.2f}G")
            parts.append(f"cuda_reserved={torch.cuda.memory_reserved() / 2**30:.2f}G")
    except Exception as exc:
        parts.append(f"(torch mem unavailable: {exc})")
    return "  ".join(parts)


def _empty_cache() -> None:
    import torch
    if torch.backends.mps.is_available():
        torch.mps.empty_cache()
    elif torch.cuda.is_available():
        torch.cuda.empty_cache()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="A page image, or a project directory containing pages.json")
    ap.add_argument("--repeat", type=int, default=10, help="Repetitions for a single image")
    ap.add_argument("--limit", type=int, default=None, help="Max pages for a project directory")
    ap.add_argument("--no-empty-cache", action="store_true", help="Skip gc + empty_cache between calls")
    args = ap.parse_args()

    target = Path(args.target)
    if target.is_dir():
        records = json.loads((target / "pages.json").read_text())
        paths = [target / r["image_path"] for r in records][: args.limit]
    else:
        paths = [target] * args.repeat

    import importlib.metadata as md
    import torch
    from surya.foundation import FoundationPredictor
    from surya.layout import LayoutPredictor
    from surya.settings import settings

    print(f"surya-ocr {md.version('surya-ocr')}, torch {torch.__version__}, "
          f"transformers {md.version('transformers')}")
    for key in sorted(settings.model_dump()):
        if any(s in key for s in ("LAYOUT", "DEVICE", "DTYPE", "BATCH")):
            print(f"  {key} = {getattr(settings, key)}")

    print(f"\nbefore load:  {_mem_report()}")
    predictor = LayoutPredictor(FoundationPredictor(checkpoint=settings.LAYOUT_MODEL_CHECKPOINT))
    model = predictor.model
    n_params = sum(p.numel() for p in model.parameters())
    print(f"model: {n_params / 1e6:.0f}M params, device={model.device}, dtype={model.dtype}")
    print(f"after load:   {_mem_report()}\n")

    for i, path in enumerate(paths, 1):
        img = Image.open(path).convert("RGB")
        t0 = time.time()
        (pred,) = predictor([img])
        n_boxes = len(pred.bboxes)
        img.close()
        del img, pred
        if not args.no_empty_cache:
            gc.collect()
            _empty_cache()
        print(f"[{i:3d}] {path.name}  boxes={n_boxes:3d}  {time.time() - t0:5.1f}s  {_mem_report()}",
              flush=True)


if __name__ == "__main__":
    sys.exit(main())
