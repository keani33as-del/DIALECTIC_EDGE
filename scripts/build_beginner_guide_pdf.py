"""Render docs/BEGINNER_GUIDE.md to docs/BEGINNER_GUIDE.pdf.

Usage:
    pip install markdown weasyprint
    python3 scripts/build_beginner_guide_pdf.py

The PDF is committed to the repo so the bot can ship it as a `send_document`
attachment without runtime dependencies. Re-run this script whenever the
Markdown source changes.
"""
from __future__ import annotations

from pathlib import Path

import markdown
from weasyprint import CSS, HTML

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "docs" / "BEGINNER_GUIDE.md"
DST = REPO_ROOT / "docs" / "BEGINNER_GUIDE.pdf"


CSS_STRING = """
@page {
    size: A4;
    margin: 18mm 16mm;
    @bottom-center {
        content: "Dialectic Edge — Beginner Guide · стр. " counter(page) " / " counter(pages);
        font-family: 'Helvetica', sans-serif;
        font-size: 9pt;
        color: #888;
    }
}
body {
    font-family: 'Georgia', 'Times New Roman', serif;
    font-size: 10.5pt;
    line-height: 1.55;
    color: #222;
}
h1 {
    font-family: 'Helvetica', sans-serif;
    font-size: 22pt;
    color: #1a1a1a;
    margin-top: 8pt;
    margin-bottom: 14pt;
    border-bottom: 2pt solid #333;
    padding-bottom: 6pt;
    page-break-after: avoid;
}
h2 {
    font-family: 'Helvetica', sans-serif;
    font-size: 15pt;
    color: #2a4d8f;
    margin-top: 20pt;
    margin-bottom: 8pt;
    page-break-after: avoid;
    border-bottom: 1pt solid #c5d2ec;
    padding-bottom: 3pt;
}
h3 {
    font-family: 'Helvetica', sans-serif;
    font-size: 12pt;
    color: #333;
    margin-top: 14pt;
    margin-bottom: 6pt;
    page-break-after: avoid;
}
h4 {
    font-family: 'Helvetica', sans-serif;
    font-size: 11pt;
    color: #444;
    margin-top: 10pt;
    margin-bottom: 5pt;
}
p, li { margin: 4pt 0; }
strong { color: #111; }
table {
    border-collapse: collapse;
    width: 100%;
    margin: 8pt 0;
    font-size: 9.5pt;
}
th, td {
    border: 1pt solid #999;
    padding: 4pt 6pt;
    text-align: left;
    vertical-align: top;
}
th { background: #eef2fb; font-family: 'Helvetica', sans-serif; }
code, pre {
    font-family: 'Menlo', 'Consolas', monospace;
    font-size: 9pt;
    background: #f4f4f4;
    border-radius: 3pt;
}
code { padding: 1pt 3pt; }
pre {
    padding: 8pt 10pt;
    border: 1pt solid #ddd;
    overflow-x: auto;
    line-height: 1.4;
}
blockquote {
    border-left: 3pt solid #c5d2ec;
    padding-left: 10pt;
    color: #555;
    font-style: italic;
}
hr { border: 0; border-top: 1pt solid #ccc; margin: 16pt 0; }
a { color: #2a4d8f; text-decoration: none; }
"""


def main() -> None:
    md_text = SRC.read_text(encoding="utf-8")
    html_body = markdown.markdown(
        md_text,
        extensions=["extra", "toc", "sane_lists"],
    )
    full_html = (
        '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
        "<title>Dialectic Edge — Beginner Guide</title></head>"
        f"<body>{html_body}</body></html>"
    )
    HTML(string=full_html).write_pdf(str(DST), stylesheets=[CSS(string=CSS_STRING)])
    print(f"PDF written: {DST}  ({DST.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
