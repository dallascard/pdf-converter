import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from core import ocr

ORF = """# Ocr Results File. Created by GNU Ocrad version 0.29
source file ocrtest.pgm
total text blocks 1
text block 1 0 0 1216 2144
lines 22
line 1 chars 34 height 33
335 216 26 22; 1, 'R'0
361 217  8 11; 1, 'T'0
371 203 10 35; 1, 'i'0
383 212 13 26; 1, 't'0
398 203 24 35; 1, 'h'0
422 210 24 28; 1, ' '0
446 212 14 26; 1, 't'0
462 204 22 35; 1, 'h'0
488 218 18 21; 1, 'e'0
506 211 18 28; 1, ' '0
524 204 21 35; 0
537 218 23 34; 1, 'y'0
563 204 28 35; 1, 'n'0
593 204 17 36; 1, '?'0
612 217 37 23; 1, 'm'0
649 212 20 28; 1, ' '0
669 219 20 21; 1, 'o'0
691 206 18 34; 1, 'F'0
709 212 20 28; 1, ' '0
729 206 20 35; 1, 'd'0
753 214 20 27; 0
778 221 19 34; 1, 'p'0
797 127  6  3; 1, '-'0
801 207 11 34; 1, 'l'0
815 207 10 35; 1, 'i'0
827 220 17 22; 1, 'c'0
848 206  9 35; 1, 'i'0
858  62 14 180; 1, 'i'0
874 220 24 34; 1, 'y'0
898 213 17 28; 1, ' '0
915 215 13 27; 1, 't'0
932 207 21 35; 1, 'h'0
957 220 16 23; 1, 'a'0
976 215 12 28; 1, 't'0
line 2 chars 4 height 30
300 300 10 20; 2, '''90, ','10
311 300 10 20; 1, 'é'0
322 300 10 20; 1, ' '0
333 300 10 20; 0
line 3 chars 1 height 30
400 400 10 20; 1, ' '0
"""

parsed = ocr._parse_ocrad_orf(ORF)
for l in parsed:
    print(l)
assert parsed[0] == {"text": "RTith the _yn?m oF d_p-liciiy that",
                     "x0": 335, "y0": 62, "x1": 988, "y1": 255}, parsed[0]
assert parsed[1] == {"text": "'é _", "x0": 300, "y0": 300, "x1": 343, "y1": 320}, parsed[1]
assert len(parsed) == 2
print("OK")
