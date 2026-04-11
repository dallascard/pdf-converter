# Alt-text generation — claude.ai prompt

## How to use this prompt

1. Run `python cli.py export-alt-text my_document.pdf` to generate `figures_prompt.json`
   in the project directory. This file lists each figure's id, page number, bounding box
   coordinates, and any caption zone locations.

2. Upload the **page images** (from `data/my_document/pages/`) to claude.ai — you only
   need the pages that contain figures (see `page_image` in `figures_prompt.json`).
   Also upload `figures_prompt.json` itself.

3. Paste the prompt below into claude.ai.

4. Save Claude's JSON response to a file (e.g. `alt_text_response.json`).

5. Import the response:
   ```bash
   python cli.py import-alt-text my_document.pdf alt_text_response.json
   ```

---

## Prompt to paste into claude.ai

I am generating alt-text descriptions for figures in a scanned academic document.
I have uploaded the page images and a `figures_prompt.json` file describing each figure.

For each figure in `figures_prompt.json`:
- The figure's location on the page is given as fractional coordinates
  (`left`, `top`, `right`, `bottom` as a percentage of page width/height)
- Caption zones (if any) are also given as fractional coordinates — look for caption
  text in those regions to inform your description

For each figure, write a concise alt-text description suitable for an HTML or EPUB
document. Use any visible caption text and surrounding context on the page to make
the description as informative as possible.

Requirements:
- Maximum 300 characters per description
- Describe what is depicted: the type of image (chart, photograph, diagram, map, etc.),
  its subject, and any key information visible (axis labels, trends, notable features)
- Do not interpret or analyse — describe what is visible
- Do not include "Image of" or "Figure showing" as a prefix

Return a JSON array where each entry has:
- `"id"`: the figure id from `figures_prompt.json`
- `"alt_text"`: the description

Example output format:
```json
[
  {"id": "fig_1_1", "alt_text": "Bar chart showing annual rainfall in mm for five cities from 1980 to 2020, with London consistently highest."},
  {"id": "fig_2_1", "alt_text": "Black and white photograph of a suspension bridge under construction, circa 1930."}
]
```
