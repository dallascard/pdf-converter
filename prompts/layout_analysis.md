# Layout Analysis Prompt

Use this prompt with claude.ai by uploading your PDF and pasting the text below.
Save Claude's response as a `.json` file, then run:

    python cli.py import-boxes <your.pdf> <claude_response.json>

---

Analyze this PDF document and identify the layout regions on each page.

For each page, identify:

1. **Figure regions** — non-text visual content: photographs, drawings, diagrams,
   charts, maps, decorative rules wider than the text column.
   Do NOT include regions that contain only text, even if they are inside a box.

2. **Table regions** — tabular data rendered as a grid or structured layout.
   Include tables whether they are image-based or text-based.

3. **Exclusion zones** — boilerplate text that should be stripped before OCR:
   running headers, running footers, page numbers, watermarks.

Return a single JSON object in exactly this format — no markdown fences, no explanation,
just the raw JSON:

```
{
  "pages": {
    "1": {
      "figures": [
        {
          "id": "fig_1_1",
          "label": "photograph",
          "x": 0.1,
          "y": 0.25,
          "w": 0.8,
          "h": 0.35,
          "alt_text": ""
        }
      ],
      "tables": [
        {
          "id": "table_1_1",
          "label": "table",
          "x": 0.05,
          "y": 0.65,
          "w": 0.9,
          "h": 0.2
        }
      ],
      "exclusions": [
        {
          "label": "page_number",
          "x": 0.42,
          "y": 0.94,
          "w": 0.16,
          "h": 0.04
        }
      ]
    },
    "2": {
      "figures": [],
      "tables": [],
      "exclusions": []
    }
  }
}
```

Coordinate conventions (all values must be between 0.0 and 1.0):

- `x` — left edge as a fraction of page width (0.0 = left, 1.0 = right)
- `y` — top edge as a fraction of page height (0.0 = top, 1.0 = bottom)
- `w` — width as a fraction of page width
- `h` — height as a fraction of page height

ID format:

- Figures: `fig_<page>_<n>` — e.g. `fig_3_1` for the first figure on page 3
- Tables: `table_<page>_<n>` — e.g. `table_3_1` for the first table on page 3

Every page in the document must appear as a key in `pages`, even if it has no figures,
tables, or exclusions (use empty lists in that case).
