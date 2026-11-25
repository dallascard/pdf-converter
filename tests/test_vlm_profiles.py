import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw
import config
from core import ocr, vlm_profiles as vp

# --- matching (Hugging Face ids, served names, Ollama names) ---
cases = {
    "zai-org/GLM-OCR": "glm-ocr", "glm-ocr": "glm-ocr", "glm-ocr:latest": "glm-ocr",
    "PaddlePaddle/PaddleOCR-VL-1.6": "paddleocr-vl",
    "opendatalab/MinerU2.5-Pro-2605-1.2B": "mineru",
    "XingChen-AGI/TeleOCR": "teleocr", "StarDoc-AI/TeleOCR": "teleocr",
    "ATH-MaaS/OvisOCR2": "ovisocr2",
    "datalab-to/chandra-ocr-2": "chandra", "chandra": "chandra",
    "rednote-hilab/dots.mocr": "dots", "rednote-hilab/dots.ocr": "dots",
    "allenai/olmOCR-2-7B-1025": "olmocr", "allenai/olmOCR-2-7B-1025-FP8": "olmocr",
    "Qwen/Qwen3-VL-32B-Instruct": "generic", "qwen3-vl:8b": "generic",
}
for model, want in cases.items():
    assert vp.profile_for(model)["name"] == want, (model, vp.profile_for(model)["name"])

# --- image preparation ---
line = Image.new("RGB", (1400, 20), "white")
out = vp.prepare_image(line, vp.profile_for("mineru"))
assert out.size[0] / out.size[1] <= 50.01 and min(out.size) >= 28, out.size
tiny = Image.new("RGB", (300, 60), "white")
out = vp.prepare_image(tiny, vp.profile_for("ATH-MaaS/OvisOCR2"))
assert out.size[0] * out.size[1] >= 448 * 448 - 1000, out.size
assert vp.prepare_image(Image.new("RGB", (800, 600)), vp.GENERIC_PROFILE).size == (800, 600)

# --- text cleanup ---
chandra = ('<div data-label="Text"><p>It was the best of times, it was the <i>worst</i> of times;'
           '<br>it was the age of wisdom &amp; folly, <math>x^2</math></p>'
           '<p>Second paragraph.</p><img alt="ornament"></div>')
assert vp.text_lines(chandra) == [
    "It was the best of times, it was the worst of times;",
    "it was the age of wisdom & folly, <math>x^2</math>",
    "Second paragraph."], vp.text_lines(chandra)
olm = "---\nprimary_language: en\nis_rotation_valid: True\n---\n## CHAPTER I\n\nThe **first** line\n![fig](page_1_2_3_4.png)\nsecond line"
assert vp.text_lines(olm) == ["CHAPTER I", "The first line", "second line"], vp.text_lines(olm)
assert vp.text_lines("<think>hm</think>```\na\n* footnote star stays\n```") == ["a", "* footnote star stays"]

# --- tables ---
html_t = ('<table><tr><th>Year</th><th colspan="2">Crops</th></tr>'
          '<tr><td rowspan="2">1850</td><td>Wheat</td><td>3 | 4</td></tr>'
          '<tr><td>Oats</td><td>5</td></tr></table>')
content, fmt = vp.table_content(html_t)
assert fmt == "markdown"
assert content.splitlines() == ["| Year | Crops |  |", "|---|---|---|",
                                 "| 1850 | Wheat | 3 \\| 4 |", "|  | Oats | 5 |"], content
otsl = "<fcel>Name<fcel>Age<lcel><nl><fcel>Ann<fcel>30<ecel><nl><ucel><fcel>31<fcel>x<nl>"
content, fmt = vp.table_content(otsl)
assert fmt == "markdown" and content.splitlines() == [
    "| Name | Age |  |", "|---|---|---|", "| Ann | 30 |  |", "|  | 31 | x |"], content
assert vp.table_content("| a | b |\n|---|---|\n| 1 | 2 |")[1] == "markdown"
assert vp.table_content("a   b\n1   2")[1] == "preformatted"

# --- requests reaching a fake vLLM server ---
seen = []


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send({"data": [{"id": config.VLLM_MODEL}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append(req)
        self._send({"choices": [{"message": {"content": "<fcel>a<fcel>b<nl><fcel>1<fcel>2<nl>"},
                                 "finish_reason": "stop"}]})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
config.VLLM_HOST = f"http://127.0.0.1:{srv.server_port}"
config.VLLM_PROMPT = ""

img = Image.new("RGB", (600, 200), "white")
ImageDraw.Draw(img).rectangle([50, 50, 300, 80], fill="black")
tmp = str(__import__("pathlib").Path(__import__("tempfile").mkdtemp()) / "table_crop.png")
img.save(tmp)

config.VLLM_MODEL = "XingChen-AGI/TeleOCR"
from pathlib import Path
content, fmt = ocr._ocr_table_vlm("vllm", Path(tmp))
req = seen[-1]
assert req["messages"][0] == {"role": "system", "content": "You are a helpful assistant."}
assert req["messages"][1]["content"][1]["text"] == "\nThis is the image of a table. Please output the table in OTSL format."
assert req["frequency_penalty"] == 0.005 and req["presence_penalty"] == 1.0 and req["temperature"] == 0
assert fmt == "markdown" and content.startswith("| a | b |"), content

ocr._vlm_region_lines("vllm", img, {"x": 0, "y": 0, "w": 1, "h": 1})
req = seen[-1]
assert req["messages"][1]["content"][1]["text"] == "\nPlease output the text content from the image."
assert req["frequency_penalty"] == 0.05

config.VLLM_MODEL = "zai-org/GLM-OCR"
ocr._vlm_region_lines("vllm", img, {"x": 0, "y": 0, "w": 1, "h": 1})
req = seen[-1]
assert len(req["messages"]) == 1 and req["messages"][0]["content"][1]["text"] == "Text Recognition:"
assert req["repetition_penalty"] == 1.1 and req["top_k"] == 1

config.VLLM_MODEL = "Qwen/Qwen3-VL-32B-Instruct"
ocr._vlm_region_lines("vllm", img, {"x": 0, "y": 0, "w": 1, "h": 1})
req = seen[-1]
assert req["messages"][0]["content"][1]["text"].startswith("Transcribe all text")
assert "presence_penalty" not in req
print("OK")
