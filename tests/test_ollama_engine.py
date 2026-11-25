import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw
import config
from core import ocr

calls = []


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
        self._send({"models": [{"name": "glm-ocr:latest"}, {"name": "qwen3-vl:8b"}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        calls.append(req)
        if req["prompt"].startswith("Table"):
            self._send({"response": "| a | b |\n|---|---|\n| 1 | 2 |"})
        else:
            self._send({"response": "```markdown\nFirst line\n\nSecond line\n```"})


srv = HTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
config.OLLAMA_HOST = f"127.0.0.1:{srv.server_port}"   # no scheme on purpose

# Server/model check
config.OLLAMA_MODEL = "glm-ocr"; ocr._check_ollama()
config.OLLAMA_MODEL = "qwen3-vl:8b"; ocr._check_ollama()
config.OLLAMA_MODEL = "llava"
try:
    ocr._check_ollama(); raise SystemExit("should have failed")
except RuntimeError as e:
    assert "ollama pull llava" in str(e), e
config.OLLAMA_MODEL = "glm-ocr"

# Page: ink in the top region only; bottom region blank (as if masked)
tmp = Path(__import__("tempfile").mkdtemp())
img = Image.new("RGB", (1000, 1000), "white")
ImageDraw.Draw(img).rectangle([150, 150, 400, 180], fill="black")
img.save(tmp / "page.png")
boxes = {
    "paragraphs": [{"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.2},
                   {"x": 0.1, "y": 0.6, "w": 0.8, "h": 0.2}],
    "headings": [],
}
res = ocr._ocr_vlm_page("ollama", tmp / "page.png", 3, boxes)
print(json.dumps(res, indent=1))
assert len(calls) == 1, f"blank region should be skipped, got {len(calls)} calls"
assert calls[0]["prompt"] == "Text Recognition:" and calls[0]["model"] == "glm-ocr"
assert res["engine"] == "ollama:glm-ocr"
assert [l["text"] for l in res["lines"]] == ["First line", "Second line"]
b0, b1 = res["lines"][0]["bbox"], res["lines"][1]["bbox"]
assert abs(b0["y"] - 0.1) < 1e-9 and abs(b0["h"] - 0.1) < 1e-9 and abs(b1["y"] - 0.2) < 1e-9
assert res["lines"][1]["line_id"] == "p3_l002"

# Table
img.save(tmp / "table.png")
assert ocr._ocr_table_vlm("ollama", tmp / "table.png")[1] == "markdown"

# Generic prompt for non-GLM models, and OLLAMA_PROMPT override
config.OLLAMA_MODEL = "qwen3-vl:8b"
assert ocr._vlm_prompt("ollama").startswith("Transcribe all text")
config.OLLAMA_PROMPT = "custom"
assert ocr._vlm_prompt("ollama") == "custom" and ocr._vlm_prompt("ollama", table=True).startswith("Transcribe this table")
print("OK")
