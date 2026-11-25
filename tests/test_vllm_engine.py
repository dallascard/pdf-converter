import json
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw
import config
from core import ocr

calls, lock = [], threading.Lock()
active = {"now": 0, "max": 0}
SERVED = ["Qwen/Qwen3-VL-8B-Instruct"]


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        assert self.path == "/v1/models", self.path
        self._send({"data": [{"id": m} for m in SERVED]})

    def do_POST(self):
        assert self.path == "/v1/chat/completions", self.path
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with lock:
            calls.append((req, self.headers.get("Authorization")))
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.05)
        with lock:
            active["now"] -= 1
        content = req["messages"][0]["content"]
        assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
        prompt = content[1]["text"]
        if "table" in prompt.lower():
            text = "| a | b |\n|---|---|\n| 1 | 2 |"
        else:
            text = "<think>hmm</think>\n---\nprimary_language: en\n---\n```\nLine one\nLine two\n```"
        self._send({"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
config.VLLM_HOST = f"http://127.0.0.1:{srv.server_port}"

# --- model selection ---
config.VLLM_MODEL = ""
ocr._check_vllm()
assert config.VLLM_MODEL == SERVED[0]
config.VLLM_MODEL = "other/model"
try:
    ocr._check_vllm(); raise SystemExit("should fail")
except RuntimeError as e:
    assert "not served" in str(e)
SERVED.append("second")
config.VLLM_MODEL = ""
try:
    ocr._check_vllm(); raise SystemExit("should fail")
except RuntimeError as e:
    assert "--vllm-model" in str(e)
SERVED.pop()
config.VLLM_MODEL = ""

# --- fake project: 6 pages, a paragraph region + a blank (masked) region + a note zone, one table ---
proj = Path(__import__("tempfile").mkdtemp()) / "vllm_proj"
shutil.rmtree(proj, ignore_errors=True)
for d in ("pages", "pages_masked", "images"):
    (proj / d).mkdir(parents=True)
recs, pages = [], {}
for n in range(1, 7):
    img = Image.new("RGB", (1000, 1000), "white")
    ImageDraw.Draw(img).rectangle([150, 150, 400, 180], fill="black")
    ImageDraw.Draw(img).rectangle([150, 920, 400, 940], fill="black")
    img.save(proj / f"pages/page_{n:04d}.png")
    img.save(proj / f"pages_masked/masked_{n:04d}.png")
    recs.append({"page_number": n, "image_path": f"pages/page_{n:04d}.png",
                 "masked_image_path": f"pages_masked/masked_{n:04d}.png"})
    pages[str(n)] = {"paragraphs": [{"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.2},
                                    {"x": 0.1, "y": 0.5, "w": 0.8, "h": 0.2}],
                     "notes": [{"x": 0.1, "y": 0.9, "w": 0.8, "h": 0.06}]}
(proj / "boxes.json").write_text(json.dumps({"pages": pages}))
img.crop((100, 100, 500, 300)).save(proj / "images/table_1_1.png")
(proj / "tables.json").write_text(json.dumps([{"id": "table_1_1", "crop_path": "images/table_1_1.png"}]))

config.VLLM_API_KEY = "secret"
config.VLLM_CONCURRENCY = 4
res = ocr.run_ocr(proj, recs, engine="vllm", force=True, check_layout=False)

assert [r["page_number"] for r in res] == [1, 2, 3, 4, 5, 6], "page order kept"
assert all(r["engine"] == "vllm:Qwen/Qwen3-VL-8B-Instruct" for r in res)
p1 = res[0]["lines"]
print(json.dumps(p1, indent=1))
assert [l["text"] for l in p1] == ["Line one", "Line two", "Line one", "Line two"]
assert [l["type"] for l in p1] == ["body", "body", "footnote", "footnote"]
# 6 pages × (1 non-blank paragraph + 1 note zone) + 1 table
assert len(calls) == 13, len(calls)
assert all(auth == "Bearer secret" for _, auth in calls)
assert calls[0][0]["temperature"] == 0 and calls[0][0]["max_tokens"] == config.VLLM_MAX_TOKENS
assert active["max"] > 1, "requests should overlap"
tables = json.loads((proj / "tables.json").read_text())
assert tables[0]["content_format"] == "markdown"
print("max concurrent requests:", active["max"])

# --- prompts ---
config.VLLM_MODEL = "zai-org/GLM-OCR"
assert ocr._vlm_prompt("vllm") == "Text Recognition:"
assert ocr._vlm_prompt("vllm", table=True) == "Table Recognition:"
config.VLLM_PROMPT = "custom"
assert ocr._vlm_prompt("vllm") == "custom"
config.OLLAMA_MODEL = "qwen3-vl:8b"
assert ocr._vlm_prompt("ollama").startswith("Transcribe all text")
print("OK")
