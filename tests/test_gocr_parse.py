import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from PIL import Image
from core import ocr

SAMPLE = '''#<page x="0" y="0" dx="0" dy="0">
<block x="0" y="0" dx="0" dy="0">
<line x="1216" y="0" dx="-1215" dy="0" value="0">
 <space x="1216" y="683" dx="-840" dy="5" />
 <space x="377" y="683" dx="-4" dy="5" />
 <box x="377" y="683" dx="6" dy="5" value="_" />
 <space x="384" y="1281" dx="24" dy="33" />
 <box x="467" y="1281" dx="21" dy="33" value="p" numac="1" weights="100" achars="p" />
 <box x="1056" y="557" dx="25" dy="35" value="y" numac="1" weights="100" achars="y" />
</line>
<line x="199" y="277" dx="876" dy="32" value="2">
 <space x="202" y="284" dx="-1" dy="21" />
 <box x="213" y="285" dx="14" dy="20" value="_" />
 <box x="225" y="285" dx="7" dy="9" value="&apos;" numac="3" weights="99,94,90" achars="&apos;,t,l" />
 <box x="263" y="286" dx="20" dy="21" value="o" />
 <box x="284" y="273" dx="12" dy="34" value="l" />
 <box x="298" y="286" dx="18" dy="21" value="e" />
 <space x="317" y="281" dx="7" dy="26" />
 <box x="325" y="281" dx="14" dy="26" value="t" />
 <box x="340" y="286" dx="18" dy="21" value="c" />
</line>
<line x="10" y="10" dx="0" dy="0"></line>
</block></page>'''

parsed = ocr._parse_gocr_xml(SAMPLE)
for l in parsed:
    print(l)
assert parsed[0] == {"text": "_ py", "x0": 377, "y0": 557, "x1": 1081, "y1": 1314}, parsed[0]
assert parsed[1] == {"text": "_'ole tc", "x0": 199, "y0": 277, "x1": 1075, "y1": 309}, parsed[1]
assert len(parsed) == 2

ocr._run_gocr = lambda img, xml: SAMPLE
lines = ocr._gocr_lines(Image.new("L", (1700, 2200)))
print(lines)
assert lines[0]["text"] == "_'ole tc" and abs(lines[0]["bbox"]["x"] - 199 / 1700) < 1e-9

res = ocr._ocr_gocr.__wrapped__ if hasattr(ocr._ocr_gocr, "__wrapped__") else None
print("OK")
