#!/usr/bin/env python3
"""md_to_pdf.py — robust Markdown -> HTML -> PDF pipeline.

Given one or more Markdown files this script produces a matching ``*.pdf`` --
either next to the source file or at an explicit ``--output`` path.  It works
in two stages:

    1. Markdown -> HTML   (Python-Markdown, with the full GFM-style extension
                           set: pipe tables, fenced + highlighted code,
                           footnotes, definition lists, admonitions, a
                           ``[TOC]`` table of contents with dot leaders and
                           page numbers, abbreviations, attribute lists and
                           raw HTML.)
    2. HTML      -> PDF   (WeasyPrint, with print-optimised CSS: repeating
                           table headers, content-aware column sizing, page
                           numbers, and Unicode/box-drawing-safe fonts).

A rich, professional look is available through selectable themes
(``--theme``): ``classic``, ``modern``, ``academic``, ``minimal`` and
``github``.  Any theme can be extended or replaced with ``--css FILE``.

Nested lists are the one place where Python-Markdown deviates from
CommonMark: it requires nested items to be indented by *four* spaces, while
almost every other Markdown implementation accepts two (and most people write
two or three).  ``_normalise_list_indentation`` rewrites list indentation to
four spaces per level before parsing, so ``-``/``1.`` lists nest exactly as
written regardless of the source indentation width.

Table columns are sized with a min/max content-width algorithm: every column
is measured in "em" from its longest unbreakable token (the hard minimum) and
its longest natural line (the preferred width), then the available page width
is divided between the columns.  Short columns stay narrow, prose columns get
the space they need, unbreakable IDs never break, and numeric columns are
right-aligned automatically.

LaTeX math is supported: ``$...$`` / ``\\(...\\)`` inline and ``$$...$$`` /
``\\[...\\]`` display.  Every formula is typeset by a real LaTeX installation
and embedded in the HTML as a vector SVG image.  This is done server-side
because WeasyPrint has no JavaScript engine (so MathJax/KaTeX cannot run) and
no MathML support, while it renders SVG perfectly.

Math is skipped inside fenced code blocks and inline code spans, and ``\\$``
always stays a literal dollar sign, so prices like "$5 and $10" are safe.

Intermediate HTML is written to a temporary directory (``$TMPDIR`` or
``/tmp``) and removed afterwards, so the source tree ends up containing only
the original Markdown files and their generated PDFs.

Usage:
    md_to_pdf.py MD_FILE [MD_FILE ...] [-o OUTPUT.pdf] [--theme NAME]
                         [--css FILE] [--keep-html] [--no-math]
                         [--math-preamble FILE] [--math-jobs N]
                         [-x EXTENSION] [-X EXTENSION]

Examples:
    md_to_pdf.py SPECIFICATION.md
    md_to_pdf.py SPECIFICATION.md STYLE_GUIDE.md --theme modern
    md_to_pdf.py README.md -o /tmp/readme.pdf --page-size Letter
    md_to_pdf.py --list-themes

Dependencies (all on PyPI):
    pip install weasyprint markdown beautifulsoup4 pygments

``pygments`` is optional: without it fenced code blocks are still rendered,
just without syntax colouring.

Math additionally needs a LaTeX installation and ``dvisvgm`` on PATH --
on Debian/Ubuntu: texlive-latex-base, texlive-latex-recommended,
texlive-latex-extra and dvisvgm.  Without them the formulas are left in the
output as literal LaTeX instead of failing the conversion.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import html as _html
import logging
import os
import re
import shutil
import string
import subprocess
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as _etree
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

try:
    import markdown
    from bs4 import BeautifulSoup
    from weasyprint import HTML
except ImportError as exc:  # pragma: no cover - helpful early failure
    sys.stderr.write(
        "\nMissing dependency: %s\n\n"
        "Install the required packages, for example:\n\n"
        "    python3 -m venv /tmp/md2pdf-venv\n"
        "    /tmp/md2pdf-venv/bin/pip install weasyprint markdown \\\n"
        "                               beautifulsoup4 pygments\n"
        "    /tmp/md2pdf-venv/bin/python md_to_pdf.py /path/to/file.md\n\n" % exc
    )
    sys.exit(2)

try:  # Pygments is optional: syntax highlighting when it is installed.
    from pygments.formatters import HtmlFormatter as _PygmentsFormatter
except ImportError:  # pragma: no cover - optional dependency
    _PygmentsFormatter = None

# WeasyPrint logs a fair amount of harmless CSS warnings; only surface errors.
logging.getLogger("weasyprint").setLevel(logging.ERROR)
logging.getLogger("fontTools").setLevel(logging.ERROR)


# --------------------------------------------------------------------------- #
#  Themes
# --------------------------------------------------------------------------- #
#
# The whole print stylesheet is generated from a flat set of named tokens, so a
# theme is nothing but a dict of overrides on top of ``_BASE_TOKENS``.  The
# tokens are substituted into ``_CSS_TEMPLATE`` -- a ``string.Template`` -- which
# means an undefined token is a ``KeyError`` at build time instead of silently
# missing CSS.

_FOOTER_TEMPLATE = string.Template(
    r"""
    @bottom-left {
        content: $footer_left;
        font-family: $footer_font;
        font-size: $footer_size;
        color: $footer_color;
    }
    @bottom-right {
        content: $footer_right;
        font-family: $footer_font;
        font-size: $footer_size;
        color: $footer_color;
    }"""
)

_SANS = "'Lato', 'Noto Sans', 'DejaVu Sans', sans-serif"
_MONO = ("'JetBrains Mono', 'DejaVu Sans Mono', 'Noto Sans Mono', "
         "'Liberation Mono', monospace")

_BASE_TOKENS: dict[str, str] = {
    # -- page furniture ---------------------------------------------------- #
    "page_size": "A4",
    "page_margin": "16mm 14mm 18mm 14mm",
    "footer_font": "'DejaVu Sans', 'Noto Sans', sans-serif",
    "footer_size": "7pt",
    "footer_color": "#8a939c",
    "footer_left": "Veribus Design \u2014  Confidential",
    "footer_right": 'Page " counter(page) " of " counter(pages)',
    # -- typography -------------------------------------------------------- #
    "body_font": "'DejaVu Serif', 'Noto Serif', 'Liberation Serif', serif",
    "head_font": "'Lato', 'Noto Sans', 'DejaVu Sans', sans-serif",
    "mono_font": _MONO,
    "body_size": "9.5pt",
    "body_line_height": "1.45",
    "body_align": "justify",
    "head_weight": "700",
    "text_color": "#1a1d21",
    "muted_color": "#6b7480",
    "heading_color": "#0b3d66",
    "h1_color": "#0b3d66",
    "h2_color": "#0b3d66",
    "h3_color": "#14538a",
    "h4_color": "#1a1d21",
    "h1_size": "17pt",
    "h2_size": "13pt",
    "h3_size": "11pt",
    "h4_size": "10pt",
    "h5_size": "9.5pt",
    "strong_color": "#0b3d66",
    "link_color": "#14538a",
    "marker_color": "#0b3d66",
    "mark_bg": "#fde68a",
    "rule_color": "#c9d4dd",
    "h1_rule": "#0b3d66",
    "h1_rule_width": "2pt",
    # -- quotes / code ----------------------------------------------------- #
    "quote_bar": "#c9d4dd",
    "quote_bg": "#f6f8fa",
    "quote_text": "#3d454d",
    "code_size": "8.2pt",
    "code_bg": "#f2f4f6",
    "code_border": "#e0e4e8",
    "pre_size": "7.9pt",
    "pre_line_height": "1.3",
    "pre_text": "#1a1d21",
    "pre_bg": "#f7f9fa",
    "pre_border": "#d5dbe0",
    "kbd_bg": "#f2f4f6",
    # -- tables ------------------------------------------------------------ #
    "table_font": "'DejaVu Sans', 'Noto Sans', sans-serif",
    "table_head_font": "'DejaVu Sans', 'Noto Sans', sans-serif",
    "table_size": "7.8pt",
    "th_bg": "#0b3d66",
    "th_fg": "#ffffff",
    "th_border": "#0b3d66",
    "table_border": "#b9c4cc",
    "table_zebra": "#f2f6f9",
    "table_code_size": "7.4pt",
    "table_code_bg": "#eef1f4",
    "table_code_border": "#dde3e8",
    # -- table of contents / footnotes ------------------------------------- #
    "toc_size": "9pt",
    "toc_bg": "#f6f8fa",
    "toc_border": "#dde3e8",
    "toc_accent": "#0b3d66",
    "footnote_size": "8.2pt",
    "footnote_color": "#4a5259",
    # -- admonitions ------------------------------------------------------- #
    "adm_bg": "#f6f8fa",
    "adm_border": "#dde3e8",
    "adm_info": "#0b3d66",
    "adm_warn": "#9a6700",
    "adm_warn_bg": "#fff8e5",
    "adm_danger": "#a11b1b",
    "adm_danger_bg": "#fdf3f3",
    "adm_tip": "#14663f",
    "adm_tip_bg": "#eefbf3",
    # -- inline math errors ------------------------------------------------ #
    "error_text": "#a11b1b",
    "error_bg": "#fdf3f3",
    "error_border": "#e8c4c4",
}

_THEMES: dict[str, dict[str, str]] = {}

# ``classic`` is the original look and the default, so it is pure defaults.
_THEMES["classic"] = {}

_THEMES["modern"] = {
    "body_font": _SANS,
    "head_font": _SANS,
    "body_line_height": "1.5",
    "text_color": "#1f2430",
    "muted_color": "#6b7280",
    "heading_color": "#1e1b4b",
    "h1_color": "#1e1b4b",
    "h2_color": "#312e81",
    "h3_color": "#4338ca",
    "h4_color": "#312e81",
    "strong_color": "#312e81",
    "link_color": "#4338ca",
    "marker_color": "#6366f1",
    "mark_bg": "#fde68a",
    "rule_color": "#e5e7eb",
    "h1_rule": "#6366f1",
    "h1_rule_width": "3pt",
    "quote_bar": "#6366f1",
    "quote_bg": "#f8f7ff",
    "quote_text": "#3f3d56",
    "code_bg": "#f1f3f9",
    "code_border": "#e0e4ef",
    "code_size": "8.3pt",
    "pre_bg": "#f7f8fc",
    "pre_border": "#e0e4ef",
    "kbd_bg": "#f1f3f9",
    "th_bg": "#eef2ff",
    "th_fg": "#1e1b4b",
    "th_border": "#c7d2fe",
    "table_border": "#dbe0ea",
    "table_zebra": "#f8fafc",
    "table_code_bg": "#eef1f8",
    "table_code_border": "#dde3ee",
    "toc_bg": "#f8f7ff",
    "toc_border": "#e0e4ef",
    "toc_accent": "#6366f1",
    "adm_bg": "#f8f7ff",
    "adm_border": "#e0e4ef",
    "adm_info": "#4338ca",
    "adm_warn": "#b45309",
    "adm_warn_bg": "#fffbeb",
    "adm_danger": "#b91c1c",
    "adm_danger_bg": "#fef2f2",
    "adm_tip": "#047857",
    "adm_tip_bg": "#ecfdf5",
    "footer_color": "#9aa1ad",
}


# ``academic`` pairs a Times-like serif with a Helvetica-like sans for heads.
_THEMES["academic"] = {
    "body_font": "'TeX Gyre Termes', 'Noto Serif', 'Liberation Serif', serif",
    "head_font": "'TeX Gyre Heros', 'Lato', 'Noto Sans', sans-serif",
    "mono_font": _MONO,
    "body_size": "10pt",
    "body_line_height": "1.5",
    "text_color": "#14181f",
    "muted_color": "#5b6470",
    "heading_color": "#14213d",
    "h1_color": "#14213d",
    "h2_color": "#1d3557",
    "h3_color": "#2a4d6e",
    "h4_color": "#1d3557",
    "h5_size": "10pt",
    "strong_color": "#14213d",
    "link_color": "#1d4e89",
    "marker_color": "#5b6470",
    "rule_color": "#cdd3da",
    "h1_rule": "#14213d",
    "quote_bar": "#8a9aab",
    "quote_bg": "#f7f8f9",
    "quote_text": "#3a434e",
    "code_size": "8.4pt",
    "pre_size": "8.1pt",
    "table_size": "8pt",
    "table_code_size": "7.6pt",
    "th_bg": "#e8ecf1",
    "th_fg": "#14213d",
    "th_border": "#b7c2cd",
    "table_border": "#c3ccd5",
    "table_zebra": "#f6f8fa",
    "toc_bg": "#f7f8f9",
    "toc_border": "#d6dbe1",
    "toc_accent": "#14213d",
    "adm_bg": "#f7f8f9",
    "adm_border": "#d6dbe1",
    "adm_info": "#1d4e89",
    "adm_warn": "#8a5a00",
    "adm_danger": "#8c1c13",
    "adm_tip": "#25603f",
}

# ``minimal``: grayscale, hairline rules, nothing decorative.
_THEMES["minimal"] = {
    "body_font": _SANS,
    "head_font": _SANS,
    "body_size": "9pt",
    "body_line_height": "1.45",
    "body_align": "left",
    "head_weight": "600",
    "text_color": "#18181b",
    "muted_color": "#71717a",
    "heading_color": "#18181b",
    "h1_color": "#18181b",
    "h2_color": "#27272a",
    "h3_color": "#3f3f46",
    "h4_color": "#18181b",
    "strong_color": "#18181b",
    "link_color": "#3f3f46",
    "marker_color": "#a1a1aa",
    "mark_bg": "#f0f0f0",
    "rule_color": "#e4e4e7",
    "h1_rule": "#a1a1aa",
    "h1_rule_width": "1pt",
    "quote_bar": "#d4d4d8",
    "quote_bg": "#fafafa",
    "quote_text": "#52525b",
    "code_bg": "#f4f4f5",
    "code_border": "#e4e4e7",
    "pre_bg": "#fafafa",
    "pre_border": "#e4e4e7",
    "kbd_bg": "#f4f4f5",
    "th_bg": "#f4f4f5",
    "th_fg": "#18181b",
    "th_border": "#d4d4d8",
    "table_border": "#e4e4e7",
    "table_zebra": "transparent",
    "toc_bg": "#fafafa",
    "toc_border": "#e4e4e7",
    "toc_accent": "#a1a1aa",
    "adm_bg": "#fafafa",
    "adm_border": "#e4e4e7",
    "adm_info": "#3f3f46",
    "adm_warn": "#7c5c00",
    "adm_warn_bg": "#fefce8",
    "adm_danger": "#991b1b",
    "adm_danger_bg": "#fef2f2",
    "adm_tip": "#3f6212",
    "adm_tip_bg": "#f7fee7",
    "footer_color": "#a1a1aa",
}

# ``github``: the familiar GitHub Markdown look, led by its light table headers.
_THEMES["github"] = {
    "body_font": _SANS,
    "head_font": _SANS,
    "body_line_height": "1.5",
    "body_align": "left",
    "head_weight": "600",
    "text_color": "#1f2328",
    "muted_color": "#59636e",
    "heading_color": "#1f2328",
    "h1_color": "#1f2328",
    "h2_color": "#1f2328",
    "h3_color": "#1f2328",
    "h4_color": "#1f2328",
    "strong_color": "#1f2328",
    "link_color": "#0969da",
    "marker_color": "#59636e",
    "mark_bg": "#fff8c5",
    "rule_color": "#d1d9e0",
    "h1_rule": "#d1d9e0",
    "h1_rule_width": "1pt",
    "quote_bar": "#d1d9e0",
    "quote_bg": "#ffffff",
    "quote_text": "#59636e",
    "code_bg": "#eff1f3",
    "code_border": "#eff1f3",
    "pre_bg": "#f6f8fa",
    "pre_border": "#d1d9e0",
    "kbd_bg": "#f6f8fa",
    "th_bg": "#f6f8fa",
    "th_fg": "#1f2328",
    "th_border": "#d1d9e0",
    "table_border": "#d1d9e0",
    "table_zebra": "transparent",
    "toc_bg": "#f6f8fa",
    "toc_border": "#d1d9e0",
    "toc_accent": "#0969da",
    "adm_bg": "#f6f8fa",
    "adm_border": "#d1d9e0",
    "adm_info": "#0969da",
    "adm_warn": "#9a6700",
    "adm_warn_bg": "#fff8c5",
    "adm_danger": "#cf222e",
    "adm_danger_bg": "#ffebe9",
    "adm_tip": "#1a7f37",
    "adm_tip_bg": "#dafbe1",
    "footer_color": "#8c959f",
}

_THEME_DESCRIPTIONS = {
    "classic": "Corporate blue on a serif body - the original look (default).",
    "modern": "Sans-serif with an indigo accent and light table headers.",
    "academic": "Times-like serif, navy accents - papers and specifications.",
    "minimal": "Grayscale, hairline rules, no decoration. Dense and quiet.",
    "github": "The familiar GitHub Markdown look, print-optimised.",
}

# --------------------------------------------------------------------------- #
#  Print stylesheet
# --------------------------------------------------------------------------- #
#
# WeasyPrint implements a useful slice of CSS Paged Media, and the stylesheet
# leans on it where it pays off: ``leader()`` + ``target-counter()`` render the
# ``[TOC]`` table of contents with dot leaders and real page numbers,
# ``break-inside: avoid`` keeps rows, code blocks and admonitions in one piece,
# and ``@bottom-*`` margin boxes carry the page numbers.

_CSS_TEMPLATE = string.Template(
    r"""
@page {
    size: $page_size;
    margin: $page_margin;$page_footer
}

* { box-sizing: border-box; }

body {
    margin: 0;
    font-family: $body_font;
    font-size: $body_size;
    line-height: $body_line_height;
    color: $text_color;
    orphans: 2;
    widows: 2;
}

/* ---------------------------------------------------------------- headings */
h1, h2, h3, h4, h5, h6 {
    font-family: $head_font;
    color: $heading_color;
    font-weight: $head_weight;
    break-after: avoid;
    page-break-after: avoid;
    break-inside: avoid;
    line-height: 1.2;
    margin: 1em 0 0.4em 0;
}
h1 {
    font-size: $h1_size;
    color: $h1_color;
    margin-top: 0;
    padding-bottom: 4pt;
    border-bottom: $h1_rule_width solid $h1_rule;
}
h2 {
    font-size: $h2_size;
    color: $h2_color;
    margin-top: 1.2em;
    padding-bottom: 3pt;
    border-bottom: 0.8pt solid $rule_color;
}
h3 { font-size: $h3_size; color: $h3_color; }
h4 { font-size: $h4_size; color: $h4_color; }
h5, h6 { font-size: $h5_size; color: $h4_color; letter-spacing: 0.02em; }
h6 { text-transform: uppercase; letter-spacing: 0.06em; }

/* --------------------------------------------------------------- body text */
p { margin: 0.5em 0; text-align: $body_align; hyphens: auto; }
ul, ol { margin: 0.4em 0 0.7em 0; padding-left: 1.5em; }
li { margin: 0.18em 0; }
li > p { margin: 0.25em 0; }
ul ul, ul ol, ol ul, ol ol { margin: 0.15em 0 0.25em 0; }
li > ul, li > ol { margin-bottom: 0.15em; }
li::marker { color: $marker_color; }
strong, b { color: $strong_color; }
a { color: $link_color; text-decoration: none; overflow-wrap: break-word; }
del { color: $muted_color; }
mark { background: $mark_bg; padding: 0 1pt; color: inherit; }
small { font-size: 0.85em; color: $muted_color; }
sub, sup { font-size: 0.72em; line-height: 0; }
hr { border: none; border-top: 0.8pt solid $rule_color; margin: 1.1em 0; }

/* -------------------------------------------------------------- task lists */
ul.task-list { list-style: none; padding-left: 0.25em; }
li.task-list-item { list-style: none; }
.task-list-item-checkbox {
    font-family: $mono_font;
    color: $link_color;
    margin-right: 0.4em;
}

/* ------------------------------------------------------------------ quotes */
blockquote {
    margin: 0.7em 0;
    padding: 0.15em 0.9em;
    border-left: 3pt solid $quote_bar;
    background: $quote_bg;
    color: $quote_text;
    break-inside: avoid;
}
blockquote > p:first-child { margin-top: 0.35em; }
blockquote > p:last-child { margin-bottom: 0.35em; }

/* -------------------------------------------------------------------- code */
code {
    font-family: $mono_font;
    font-size: $code_size;
    background: $code_bg;
    border: 0.5pt solid $code_border;
    border-radius: 2pt;
    padding: 0 2pt;
    white-space: pre-wrap;
    overflow-wrap: break-word;
}
pre {
    font-family: $mono_font;
    font-size: $pre_size;
    line-height: $pre_line_height;
    color: $pre_text;
    background: $pre_bg;
    border: 0.6pt solid $pre_border;
    border-radius: 3pt;
    padding: 6pt 8pt;
    margin: 0.7em 0;
    white-space: pre-wrap;
    overflow-wrap: break-word;
    break-inside: avoid;
    page-break-inside: avoid;
}
pre code {
    background: none;
    border: none;
    padding: 0;
    border-radius: 0;
    font-size: inherit;
    white-space: inherit;
}
.codehilite {
    background: $pre_bg;
    border: 0.6pt solid $pre_border;
    border-radius: 3pt;
    padding: 6pt 8pt;
    margin: 0.7em 0;
    break-inside: avoid;
    page-break-inside: avoid;
}
.codehilite pre {
    background: none;
    border: none;
    border-radius: 0;
    padding: 0;
    margin: 0;
}
.codehilite .lineno { color: $muted_color; }
kbd {
    font-family: $mono_font;
    font-size: 0.88em;
    border: 0.5pt solid $code_border;
    border-bottom-width: 1.4pt;
    border-radius: 2pt;
    padding: 0 2pt;
    background: $kbd_bg;
}

/* ------------------------------------------------------------------ tables */
table {
    width: 100%;
    border-collapse: collapse;
    table-layout: fixed;
    margin: 0.7em 0 1em 0;
    font-family: $table_font;
    font-size: $table_size;
    line-height: 1.35;
}
thead { display: table-header-group; }
tfoot { display: table-footer-group; }
th {
    background: $th_bg;
    color: $th_fg;
    font-family: $table_head_font;
    text-align: left;
    font-weight: bold;
    border: 0.5pt solid $th_border;
    padding: 3pt 4pt;
    vertical-align: top;
    overflow-wrap: break-word;
    hyphens: manual;
}
td {
    border: 0.5pt solid $table_border;
    padding: 3pt 4pt;
    vertical-align: top;
    overflow-wrap: break-word;
    hyphens: manual;
}
tbody tr:nth-child(even) { background: $table_zebra; }
tbody tr { break-inside: avoid; page-break-inside: avoid; }
table code {
    font-size: $table_code_size;
    background: $table_code_bg;
    border-color: $table_code_border;
}
td.mdtp-num, th.mdtp-num { text-align: right; }
td.mdtp-nowrap, th.mdtp-nowrap { white-space: nowrap; overflow-wrap: normal; }

/* ------------------------------------------------------- definition lists */
dl { margin: 0.6em 0; }
dt {
    font-family: $head_font;
    font-weight: bold;
    color: $strong_color;
    margin-top: 0.5em;
    break-after: avoid;
    page-break-after: avoid;
}
dd { margin: 0.15em 0 0.45em 1.4em; }
dd > p { margin: 0.2em 0; }

/* ----------------------------------------------------- table of contents */
.toc {
    font-family: $head_font;
    font-size: $toc_size;
    margin: 0.6em 0 1.3em 0;
    padding: 0.55em 0.9em 0.55em 0.8em;
    background: $toc_bg;
    border: 0.6pt solid $toc_border;
    border-left: 3pt solid $toc_accent;
    border-radius: 3pt;
    break-inside: avoid;
}
.toc > ul { margin: 0; padding-left: 0; list-style: none; }
.toc ul { margin: 0.15em 0; padding-left: 1.1em; }
.toc li { margin: 0.12em 0; }
.toc a { color: $text_color; text-decoration: none; }
.toc a::after {
    content: leader('.') target-counter(attr(href), page);
    color: $muted_color;
    font-weight: normal;
}

/* ------------------------------------------------------------- footnotes */
.footnote {
    font-size: $footnote_size;
    color: $footnote_color;
    margin-top: 1.4em;
    padding-top: 0.4em;
    border-top: 0.6pt solid $rule_color;
    break-inside: avoid;
}
.footnote hr { display: none; }
.footnote ol { padding-left: 1.4em; margin: 0.3em 0; }
.footnote li { margin: 0.15em 0; }
.footnote p { margin: 0.15em 0; text-align: left; }
a.footnote-ref { font-size: 0.72em; text-decoration: none; color: $link_color; }
a.footnote-backref { text-decoration: none; color: $muted_color; }

/* ----------------------------------------------------------- admonitions */
.admonition {
    margin: 0.8em 0;
    padding: 0.5em 0.8em;
    border: 0.6pt solid $adm_border;
    border-left: 3pt solid $adm_info;
    border-radius: 3pt;
    background: $adm_bg;
    break-inside: avoid;
    page-break-inside: avoid;
}
.admonition > p { margin: 0.25em 0; }
.admonition-title {
    font-family: $head_font;
    font-weight: bold;
    color: $adm_info;
    margin: 0 0 0.25em 0;
    letter-spacing: 0.01em;
}
.admonition.warning, .admonition.caution, .admonition.attention {
    border-left-color: $adm_warn;
    background: $adm_warn_bg;
}
.admonition.warning > .admonition-title,
.admonition.caution > .admonition-title,
.admonition.attention > .admonition-title { color: $adm_warn; }
.admonition.danger, .admonition.error, .admonition.failure, .admonition.bug {
    border-left-color: $adm_danger;
    background: $adm_danger_bg;
}
.admonition.danger > .admonition-title,
.admonition.error > .admonition-title,
.admonition.failure > .admonition-title,
.admonition.bug > .admonition-title { color: $adm_danger; }
.admonition.tip, .admonition.success, .admonition.hint {
    border-left-color: $adm_tip;
    background: $adm_tip_bg;
}
.admonition.tip > .admonition-title,
.admonition.success > .admonition-title,
.admonition.hint > .admonition-title { color: $adm_tip; }

/* --------------------------------------------------------- abbreviations */
abbr[title] { text-decoration: underline dotted $muted_color; }
abbr[title]::after {
    content: " (" attr(title) ")";
    color: $muted_color;
    font-size: 0.9em;
}

/* ---------------------------------------------------------- misc elements */
img { max-width: 100%; height: auto; }
figure { margin: 0.8em 0; text-align: center; break-inside: avoid; }
figcaption {
    font-family: $head_font;
    font-size: 0.88em;
    color: $muted_color;
    margin-top: 0.3em;
}

/* -------------------------------------------------- math (LaTeX -> SVG) */
img.mdtp-math {
    /* Sizes and baseline offset are set inline per formula, in em, so the
       math always scales with the surrounding font size. */
    max-width: none;
    vertical-align: baseline;
}
p.math-display {
    text-align: center;
    margin: 0.75em 0;
    break-inside: avoid;
    page-break-inside: avoid;
}
code.mdtp-math-error {
    color: $error_text;
    background: $error_bg;
    border-color: $error_border;
}

$extra_css
""")
def available_themes() -> list[str]:
    """Return the known theme names in a stable, documented order."""
    return ["classic", "modern", "academic", "minimal", "github"]


def theme_tokens(theme: str) -> dict[str, str]:
    """Return the fully merged token set for *theme*."""
    if theme not in _THEMES:
        raise KeyError(theme)
    tokens = dict(_BASE_TOKENS)
    tokens.update(_THEMES[theme])
    return tokens


def _css_string(text: str) -> str:
    """Quote *text* as a CSS string literal (backslashes and quotes escaped)."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_css(
    theme: str = "classic",
    *,
    page_size: str | None = None,
    page_margin: str | None = None,
    body_size: str | None = None,
    footer_left: str | None = None,
    show_footer: bool = True,
    extra_css: str = "",
) -> str:
    """Render the complete print stylesheet for *theme*.

    ``page_size``/``page_margin``/``body_size`` override the theme's own values,
    ``footer_left`` replaces the running footer text and ``extra_css`` is
    appended verbatim -- that is where ``--css`` and the Pygments rules land.
    """
    if theme not in _THEMES:
        raise KeyError(theme)
    tokens = theme_tokens(theme)
    if page_size:
        tokens["page_size"] = page_size
    if page_margin:
        tokens["page_margin"] = page_margin
    if body_size:
        tokens["body_size"] = body_size
    if footer_left is not None:
        tokens["footer_left"] = footer_left
    # The footer text always travels as a CSS string, the right-hand side is
    # already a counter() expression.
    tokens["footer_left"] = _css_string(tokens["footer_left"])
    tokens["page_footer"] = (
        _FOOTER_TEMPLATE.substitute(tokens) if show_footer else ""
    )
    tokens["extra_css"] = extra_css.strip()
    return _CSS_TEMPLATE.substitute(tokens)





# --------------------------------------------------------------------------- #
#  Markdown extensions
# --------------------------------------------------------------------------- #
#
# Python-Markdown ships a broad extension set; the pipeline enables everything
# that is useful for a printed document and styles the result: tables, fenced +
# highlighted code, footnotes, definition lists, abbreviations, admonitions,
# attribute lists, raw HTML and a ``[TOC]`` with real page numbers.
#
# Two GFM behaviours have no built-in extension and are supplied here:
# ``~~strikethrough~~`` and ``- [x]`` task-list checkboxes (rendered as ballot
# boxes, which is the only thing that reads correctly on paper).

_LIST_ITEM_RE = re.compile(
    r"^(?P<quote>(?:[ \t]{0,3}>[ \t]?)*)"
    r"(?P<indent>[ \t]*)"
    r"(?P<marker>[-*+]|\d{1,9}[.)])(?P<space>[ \t]+)"
    r"(?P<body>\S.*)$"
)
"""A list-item line: optional blockquote markers, indentation, bullet or
number, spacing, then content."""

_BLOCKQUOTE_LINE_RE = re.compile(r"^[ \t]{0,3}>")


def _indent_width(indent: str) -> int:
    """Return the visual width of *indent*, counting a tab as four columns."""
    return 4 * indent.count("\t") + indent.count(" ")


class _ListIndentNormaliser:
    """Rewrites list indentation onto the four-space grid Python-Markdown needs.

    The state (which list levels are currently open, and their source
    indentation) is kept across chunks, so a list that is interrupted by a
    fenced code block keeps its nesting afterwards.
    """

    def __init__(self) -> None:
        # One stack of source indents per blockquote depth.
        self._stacks: dict[int, list[int]] = {}

    def text(self, chunk: str) -> str:
        """Normalise the indentation of the list items in one text chunk."""
        out: list[str] = []
        for line in chunk.splitlines(keepends=True):
            match = _LIST_ITEM_RE.match(line)
            if match is None:
                out.append(line)
                # A non-blank line that starts in column zero and is not
                # blockquote content ends the list; anything indented (or
                # quoted) is a continuation of the item above it.
                if (
                    line.strip()
                    and not line[:1].isspace()
                    and not _BLOCKQUOTE_LINE_RE.match(line)
                ):
                    self._stacks.clear()
                continue

            quote = match.group("quote")
            indent = _indent_width(match.group("indent"))
            stack = self._stacks.setdefault(quote.count(">"), [])
            while stack and indent < stack[-1]:
                stack.pop()
            if not stack or indent > stack[-1]:
                stack.append(indent)
            depth = len(stack) - 1
            new_indent = stack[0] + 4 * depth
            out.append(
                quote + " " * new_indent + line[len(quote):].lstrip(" \t")
            )
        return "".join(out)


def _normalise_list_chunk(text: str) -> str:
    """Normalise one isolated non-code chunk (convenience wrapper)."""
    return _ListIndentNormaliser().text(text)


def _normalise_list_indentation(md_text: str) -> str:
    """Make nested lists nest, whatever indentation width the author used.

    Python-Markdown derives a list item's nesting level from ``indent // 4`` and
    only nests it when that level is exactly one deeper than its parent, so the
    indentation it is given has to be *exactly* ``baseline + 4 * depth``.  A
    stack of the source indentation widths of the levels that are currently open
    supplies the depth; any deeper item opens a new level and any shallower item
    closes levels until it fits.  That reproduces CommonMark's relative nesting
    for 2-, 3- and 4-space indented input alike.

    Indentation is preserved relative to the block the list lives in (a
    blockquote, an admonition or any indented container), because those
    containers are part of the Markdown syntax: the first item of a list
    establishes its baseline indent and everything nests relative to that.

    Lists inside blockquotes get their own stack per quote depth, so a quoted
    nested list is rewritten the same way an unquoted one is.  Fenced code is
    handed through byte-for-byte, and does not interrupt a list.
    """
    normaliser = _ListIndentNormaliser()
    pieces: list[str] = []
    for is_code, chunk in _split_fenced_blocks(md_text):
        pieces.append(chunk if is_code else normaliser.text(chunk))
    return "".join(pieces)


_TASK_MARKERS = ("\u2610", "\u2611")  # U+2610 ballot box / U+2611 checked
# Both glyphs exist in DejaVu Sans Mono, the font the checkbox CSS asks for.


class StrikethroughExtension(markdown.extensions.Extension):
    """GFM ``~~text~~`` -> ``<del>text</del>``."""

    def extendMarkdown(self, md):  # noqa: N802 - Python-Markdown API
        md.inlinePatterns.register(
            markdown.inlinepatterns.SimpleTagInlineProcessor(
                r"(\~\~)(.+?)(\~\~)", "del"
            ),
            "mdtp-strikethrough",
            175,
        )


class _TaskListTreeprocessor(markdown.treeprocessors.Treeprocessor):
    """Turn ``- [x] item`` into a ballot-box glyph plus the item text."""

    _TASK_RE = re.compile(r"^\[([ xX])\][ \t]+")

    def run(self, root):  # noqa: D102 - Python-Markdown API
        for parent in root.iter():
            if parent.tag not in ("ul", "ol"):
                continue
            items = [child for child in parent if child.tag == "li"]
            tasks = 0
            for item in items:
                holder = item.find("p")
                if holder is None:
                    holder = item
                text = holder.text or ""
                match = self._TASK_RE.match(text)
                if match is None:
                    continue
                checkbox = _etree.Element("span")
                checkbox.set("class", "task-list-item-checkbox")
                checkbox.text = _TASK_MARKERS[match.group(1).lower() == "x"]
                checkbox.tail = text[match.end():]
                holder.text = ""
                holder.insert(0, checkbox)
                item.set("class", "task-list-item")
                tasks += 1
            # Only a list made up entirely of task items loses its bullets; a
            # mixed list keeps them and the checkbox items hide their own.
            if tasks and tasks == len(items):
                parent.set("class", "task-list")
        return root


class TaskListExtension(markdown.extensions.Extension):
    """GFM ``- [ ]`` / ``- [x]`` task lists."""

    def extendMarkdown(self, md):  # noqa: N802 - Python-Markdown API
        md.treeprocessors.register(
            _TaskListTreeprocessor(md), "mdtp-task-list", 5
        )


# --------------------------------------------------------------------------- #
#  Fenced code nested in a list or blockquote
# --------------------------------------------------------------------------- #
#
# Python-Markdown's ``fenced_code`` only runs on top-level blocks.  A fence
# indented under a list item -- or written inside a blockquote -- comes out as
# an inline code span (``<p><code>python x = 1</code></p>``) or escapes the list
# altogether, both of which are common ways to write documentation and both of
# which are obviously wrong.
#
# Those blocks are cut out before parsing and returned as standalone Markdown,
# so the caller can render each one on its own (with highlighting, exactly like
# a top-level block) and substitute the result back in through a placeholder.
# The placeholder keeps the original indentation and blockquote prefix, so the
# rest of the document -- including the list the block lives in -- still parses
# exactly as it would have.

_NESTED_FENCE_PLACEHOLDER = "MDTPFENCESTART{}MDTPFENCEEND"
"""Stand-in for an extracted fenced code block.  Letters only, so no part of the
Markdown pipeline (smarty, math, lists, ...) touches it."""

_FENCE_OPEN_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>[^\n]*)$"
)


def _split_quote(line: str) -> tuple[int, str]:
    """Return the blockquote depth of *line* and the line without its quotes."""
    depth = 0
    rest = line
    while True:
        match = re.match(r"[ \t]{0,3}>[ \t]?", rest)
        if match is None:
            return depth, rest
        depth += 1
        rest = rest[match.end():]


def _fence_is_nested(quote: int, indent: str, previous: str) -> bool:
    """True when a fenced block sits inside a list item or a blockquote.

    A top-level fence is left alone: Python-Markdown already renders it
    correctly.  So is an *unindented* fence after a list item (that really does
    end the list) and an indented fence after an ordinary paragraph (that really
    is an indented code block).
    """
    if quote:
        return True
    if not indent:
        return False
    if not previous.strip():
        return False
    return (
        _LIST_ITEM_RE.match(previous) is not None
        or previous[:1] in (" ", "\t")
    )


def _closing_fence_re(marker: str) -> re.Pattern:
    """Regex matching the line that closes a fence opened with *marker*."""
    return re.compile(
        r"^[ \t]*" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*$"
    )


def _find_fence_end(
    lines: list[str], start: int, closing: re.Pattern
) -> int:
    """Return the index of a fence's closing line, or ``len(lines)``."""
    index = start
    while index < len(lines):
        if closing.match(_split_quote(lines[index])[1].rstrip("\r\n")):
            return index
        index += 1
    return len(lines)


def _strip_common_indent(text: str) -> str:
    """Remove the indentation shared by every non-blank line of *text*."""
    lines = text.splitlines(keepends=True)
    indents = [
        re.match(r"[ \t]*", line).group(0) for line in lines if line.strip()
    ]
    if not indents:
        return text
    common = os.path.commonprefix(indents)
    if not common:
        return text
    return "".join(
        line[len(common):] if line.strip() else line for line in lines
    )


def extract_nested_fences(md_text: str) -> tuple[str, list[str]]:
    """Pull out fenced code blocks that are nested in a list or blockquote.

    Returns the document with each such block replaced by a placeholder, plus
    the blocks as standalone Markdown (fence and all) ready to be rendered.
    Everything else -- including fences that are left alone -- is handed through
    byte for byte.

    Call this *after* :func:`_normalise_list_indentation`, so the surrounding
    list items already sit on the four-space grid: the placeholder is then
    placed at the content indentation of the list item the block belongs to
    (``item indent + 4``), which is exactly what Python-Markdown needs to keep
    the substitution inside that item.
    """
    lines = md_text.splitlines(keepends=True)
    out: list[str] = []
    fences: list[str] = []
    previous = ""
    item_indent: dict[int, int | None] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        quote, rest = _split_quote(line)
        match = _FENCE_OPEN_RE.match(rest)
        if match is None:
            out.append(line)
            if line.strip():
                previous = line
                item = _LIST_ITEM_RE.match(line)
                if item is not None:
                    item_indent[quote] = _indent_width(item.group("indent"))
                elif not line[:1].isspace() and not quote:
                    item_indent.clear()
            index += 1
            continue

        marker = match.group("fence")
        closing = _closing_fence_re(marker)
        end = _find_fence_end(lines, index + 1, closing)

        if not _fence_is_nested(quote, match.group("indent"), previous):
            # Hand the whole block through untouched, closing fence included, so
            # its body is never scanned again.
            out.extend(lines[index:min(end + 1, len(lines))])
            previous = lines[end] if end < len(lines) else line
            index = end + 1
            continue

        body = lines[index + 1:end]
        code = _strip_common_indent(
            "".join(_split_quote(raw)[1] for raw in body)
        )
        fences.append(
            f"{marker}{match.group('info').rstrip()}\n{code}{marker}\n"
        )
        # Keep the blockquote markers, and indent the placeholder far enough to
        # stay inside the list item the block belongs to.
        indent = _indent_width(match.group("indent"))
        owner = item_indent.get(quote)
        if owner is not None:
            indent = max(indent, owner + 4)
        out.append(
            line[: len(line) - len(rest)]
            + " " * indent
            + _NESTED_FENCE_PLACEHOLDER.format(len(fences) - 1)
            + "\n"
        )
        previous = lines[end] if end < len(lines) else line
        index = end + 1
    return "".join(out), fences


def _render_nested_fence(fence: str, options: RenderOptions) -> str:
    """Render one extracted fence on its own, as a normal top-level code block."""
    return markdown.markdown(
        fence,
        extensions=list(options.markdown.extensions),
        extension_configs=options.markdown.extension_configs,
        output_format="html5",
    ).strip()


DEFAULT_EXTENSIONS: tuple[str, ...] = (
    # ``extra`` bundles tables, fenced_code, attr_list, def_list, footnotes,
    # abbr and md_in_html, so they do not need to be listed separately.
    "extra",
    "sane_lists",
    "smarty",
    "toc",
    "admonition",
)
"""Built-in Python-Markdown extensions enabled for every document.

``sane_lists`` requires a blank line between items and stops ``2024.`` at the
start of a line from turning into an ordered list; ``smarty`` produces curly
quotes, proper dashes and ellipses; ``toc`` powers ``[TOC]`` (and gives every
heading an ``id``); ``admonition`` powers ``!!! note`` blocks.
"""

HIGHLIGHT_EXTENSION = "codehilite"
"""Extension that turns fenced code into Pygments-highlighted markup."""

_DEFAULT_HIGHLIGHT_STYLE = "friendly"
"""Pygments style used when ``--highlight-style`` is not given (light, and it
prints well on paper)."""

_PYGMENTS_BLOCK_RE = re.compile(r"(?m)^\.codehilite\s*\{[^}]*\}\s*$")


def highlight_css(style: str | None = None) -> str:
    """Return Pygments' rules for ``.codehilite``, or '' without Pygments.

    The plain ``.codehilite { ... }`` block is dropped so the theme keeps
    control of the code background and border; only the token colours survive.
    """
    if _PygmentsFormatter is None:
        return ""
    formatter = _PygmentsFormatter(style=style or _DEFAULT_HIGHLIGHT_STYLE)
    return _PYGMENTS_BLOCK_RE.sub(
        "", formatter.get_style_defs(".codehilite")
    ).strip()


@dataclass(frozen=True)
class MarkdownSetup:
    """Everything needed to configure one Python-Markdown conversion."""

    extensions: tuple
    extension_configs: dict
    highlighting: bool


def build_markdown_setup(
    *,
    add: Iterable[str] = (),
    remove: Iterable[str] = (),
    highlight: bool = True,
) -> MarkdownSetup:
    """Assemble the extension list for ``markdown.markdown()``.

    ``add``/``remove`` come straight from ``-x``/``-X`` on the command line and
    are applied on top of ``DEFAULT_EXTENSIONS``.  ``codehilite`` is added when
    Pygments is available and not disabled.  Extensions are instantiated here,
    so an unknown name raises immediately and the CLI can report it before
    converting anything.
    """
    removed = set(remove)
    names = [name for name in DEFAULT_EXTENSIONS if name not in removed]
    for name in add:
        if name not in names:
            names.append(name)
    # Resolve the names now so a typo is reported before anything is converted.
    markdown.Markdown(extensions=names)

    highlighting = (
        highlight
        and _PygmentsFormatter is not None
        and HIGHLIGHT_EXTENSION not in removed
    )
    if highlighting and HIGHLIGHT_EXTENSION not in names:
        names.append(HIGHLIGHT_EXTENSION)

    extensions: list = [StrikethroughExtension(), TaskListExtension()]
    extensions.extend(names)
    configs = {
        HIGHLIGHT_EXTENSION: {"guess_lang": False, "css_class": "codehilite"},
        "toc": {"toc_depth": 6},
    }
    return MarkdownSetup(tuple(extensions), configs, highlighting)


# --------------------------------------------------------------------------- #
#  LaTeX math
# --------------------------------------------------------------------------- #
#
# ``$...$`` / ``\(...\)`` (inline) and ``$$...$$`` / ``\[...\]`` (display) are
# typeset by LaTeX and embedded as SVG images: WeasyPrint has no JavaScript
# engine (so MathJax/KaTeX cannot run) and no MathML support, but it renders
# SVG very well.
#
# Per formula:
#
#     latex  ->  DVI  ->  dvisvgm --exact --no-fonts  ->  SVG  ->  data: URI
#
# ``--no-fonts`` turns every glyph into a filled ``<path>``, so the PDF carries
# no font dependency and no raster image: math stays vector-sharp at any zoom.
#
# Every formula gets its own LaTeX process (run in parallel).  Batch-rendering
# them into one document is not safe: dvisvgm silently returns page 1 when
# asked for a page that does not exist, so the moment one formula failed to
# typeset the rest would be silently mis-assigned.  One process per formula lets
# a broken formula break only itself -- it is reported on stderr and left in the
# document as literal LaTeX.
#
# The ``preview`` package crops the page tightly and tells dvisvgm the exact
# height/depth split, which ends up in the SVG ``viewBox``; that is where the
# baseline offset comes from.

_MATH_BASE_PT = 10.0
"""LaTeX base font size (``\\documentclass[10pt]``).  SVG dimensions are divided
by this to become ``em``, so math scales with the surrounding text size."""

_MATH_MAX_DISPLAY_EM = 54.0
"""Display formulas wider than this are scaled down to fit the page: A4 minus
the 14mm side margins of the print CSS is 182mm, ~54em at the 9.5pt body size."""

_MATH_PLACEHOLDER = "MDTPMATHSTART{}MDTPMATHEND"
"""Placeholder standing in for a formula while Python-Markdown runs.  Letters
and digits only and fully delimited, so it neither collides with real text nor
partially matches a higher-numbered placeholder."""

_ESCAPED_DOLLAR_RE = re.compile(r"(?<!\\)\\\$")
"""Matches an escaped dollar sign, ``\\$``."""

_MATH_ESCAPED_DOLLAR = "MDTPESCAPEDDOLLAR"
"""Sentinel standing in for ``\\$`` while the math regex runs, so an escaped
dollar can never open a formula.  Python-Markdown does not treat ``$`` as an
escapable character, so without this the PDF would show a stray backslash."""

_MATH_OPTIONAL_PACKAGES = (
    "mathtools", "mhchem", "siunitx", "cancel", "xcolor", "array",
    "mathrsfs", "esint", "upgreek", "stmaryrd",
)
"""Extra packages loaded when installed, confirmed by a real test compile and
dropped again when they do not work -- see ``_probe_preamble``."""

_MATH_ENV_RENAMES = {
    "align": "aligned", "align*": "aligned",
    "gather": "gathered", "gather*": "gathered",
}
_MATH_ENV_STRIP = ("equation", "equation*", "displaymath", "math")

_MATH_TOKEN_RE = re.compile(
    r"(?P<code>`+)[\s\S]*?(?P=code)"                        # inline code span
    r"|(?P<display_dollar>\$\$(?P<dd>[\s\S]+?)\$\$)"        # $$ ... $$
    r"|(?P<display_bracket>\\\[(?P<db>[\s\S]+?)\\\])"       # \[ ... \]
    r"|(?P<inline_paren>\\\((?P<ip>[\s\S]+?)\\\))"          # \( ... \)
    r"|(?P<inline_dollar>"                                  # $ ... $
    r"(?<!\\)\$(?![\s$])"
    r"(?P<id>(?:\\[\s\S]|[^\\$\n]|\n(?!\s*\n))*?)"
    r"(?<![\\\s])\$(?![0-9])"
    r")"
)
"""The trailing ``(?P=code)`` finds the next run of the *same* backtick group,
so ``code`` is only ever set on an inline code span -- ``extract_math`` uses
that to leave code completely alone.  Code spans are listed first so a ``$``
inside code is never math.  The inline ``$...$`` alternative follows the usual
conventions: no whitespace directly inside the delimiters, no blank line in the
body, the closing ``$`` not followed by a digit (so "$5 and $10" is left alone)
and ``\\$`` escapes."""


@dataclass(frozen=True)
class MathSnippet:
    """A single formula lifted out of the Markdown source."""

    index: int
    source: str          # LaTeX body, delimiters already removed
    display: bool        # True for $$...$$ and \[...\]
    token: str           # placeholder that stands in for it in the Markdown


@dataclass
class MathRender:
    """Result for one formula: an ``<img>`` tag, or an error to report."""

    img: str | None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.img is not None


@dataclass
class MathConfig:
    """Whether/how to render math, plus the resolved LaTeX preamble."""

    enabled: bool = True
    jobs: int = 8
    extra_preamble: str = ""
    latex: str | None = None
    dvisvgm: str | None = None
    preamble: str | None = None
    _probed: bool = False

    @property
    def usable(self) -> bool:
        """True when math can actually be converted."""
        return bool(self.enabled and self.latex and self.dvisvgm)


def _have_sty(package: str) -> bool:
    """Return True if ``package.sty`` is installed (cached)."""
    kpsewhich = shutil.which("kpsewhich")
    if kpsewhich is None:
        return False
    try:
        return subprocess.run(
            [kpsewhich, f"{package}.sty"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=20, check=False,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _build_preamble(optional: list[str], extra: str) -> str:
    """Assemble the LaTeX preamble shared by every formula."""
    lines = [
        r"\documentclass[10pt]{article}",
        r"\usepackage[T1]{fontenc}",
        r"\usepackage{amsmath,amssymb,amsfonts}",
        r"\usepackage{bm}",
    ]
    if optional:
        lines.append(r"\usepackage{" + ",".join(optional) + "}")
    lines += [
        r"\usepackage[active,tightpage]{preview}",
        r"\PreviewEnvironment{preview}",
        r"\setlength\PreviewBorder{0pt}",
        r"\pagestyle{empty}",
    ]
    if extra.strip():
        lines.append(extra.strip())
    lines.append(r"\begin{document}")
    return "\n".join(lines) + "\n"


def _normalise_display_math(body: str) -> str:
    """Make display environments usable inside ``\\[ ... \\]``.

    ``align``/``gather`` cannot be nested in display math, but their inner forms
    (``aligned``/``gathered``) can, so they are renamed.  Equation numbers are
    not rendered by this pipeline, so tags are dropped.  Blank lines are
    collapsed because LaTeX forbids a paragraph break inside math.
    """
    for name in _MATH_ENV_STRIP:
        body = body.replace(r"\begin{" + name + "}", "")
        body = body.replace(r"\end{" + name + "}", "")
    for old, new in _MATH_ENV_RENAMES.items():
        body = body.replace(r"\begin{" + old + "}", r"\begin{" + new + "}")
        body = body.replace(r"\end{" + old + "}", r"\end{" + new + "}")
    body = re.sub(r"\\tag\*?\{[^{}]*\}", "", body)
    body = re.sub(r"\\(?:notag|nonumber)\b", "", body)
    body = re.sub(r"\n\s*\n+", "\n", body)
    return body.strip()


def _math_source(snippet: MathSnippet) -> str:
    """Wrap a snippet body in the delimiters for its display mode."""
    if snippet.display:
        return "\\[\n" + _normalise_display_math(snippet.source) + "\n\\]"
    return "\\(" + snippet.source + "\\)"


def _math_document(preamble: str, snippet: MathSnippet) -> str:
    """Build the complete one-formula LaTeX source, ready to compile."""
    return (
        preamble
        + "\\begin{preview}" + _math_source(snippet) + "\n\\end{preview}\n"
        + "\\end{document}\n"
    )


def _parse_svg_geometry(svg: str) -> tuple[float, float, float] | None:
    """Return ``(width_pt, height_pt, depth_pt)`` from a dvisvgm SVG.

    In the ``viewBox`` one user unit is one point and ``min-y`` is how far the
    ink rises above the baseline, so ``min-y + height`` is the depth below it --
    exactly the ``vertical-align`` offset needed to sit on the text baseline.
    """
    view_box = re.search(
        r"viewBox=['\"]([-\d.]+)[ ,]+([-\d.]+)[ ,]+([\d.]+)[ ,]+([\d.]+)['\"]",
        svg[:2000],
    )
    if not view_box:
        return None
    min_y, width, height = (float(view_box.group(i)) for i in (2, 3, 4))
    if width <= 0 or height <= 0:
        return None
    return width, height, max(min_y + height, 0.0)


def _latex_first_error(workdir: Path) -> str:
    """Return the first LaTeX error message from the run's log."""
    try:
        text = (workdir / "formula.log").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return "LaTeX failed"
    for line in text.splitlines():
        if line.startswith("!"):
            return line.lstrip("! ").strip()
    return "LaTeX failed"


def _render_math_snippet(
    snippet: MathSnippet,
    workdir: Path,
    preamble: str,
    latex: str | None,
    dvisvgm: str | None,
) -> MathRender:
    """Typeset one formula and build its ``<img>`` tag."""
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "formula.tex").write_text(
            _math_document(preamble, snippet), encoding="utf-8"
        )
        done = subprocess.run(
            [latex, "-interaction=nonstopmode", "-halt-on-error", "formula.tex"],
            cwd=str(workdir), stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT, timeout=120, check=False,
        )
        if done.returncode != 0:
            return MathRender(None, _latex_first_error(workdir))

        subprocess.run(
            [dvisvgm, "--exact", "--no-fonts", "-o", "formula.svg", "formula.dvi"],
            cwd=str(workdir), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=120, check=False,
        )
        svg_path = workdir / "formula.svg"
        if not svg_path.exists():
            return MathRender(None, "dvisvgm produced no SVG")
        svg = svg_path.read_text(encoding="utf-8")
    except (OSError, subprocess.SubprocessError) as exc:
        return MathRender(None, f"{type(exc).__name__}: {exc}")

    geometry = _parse_svg_geometry(svg)
    if geometry is None:
        return MathRender(None, "could not read the SVG geometry")

    width_em = geometry[0] / _MATH_BASE_PT
    height_em = geometry[1] / _MATH_BASE_PT
    depth_em = geometry[2] / _MATH_BASE_PT
    if snippet.display and width_em > _MATH_MAX_DISPLAY_EM:
        scale = _MATH_MAX_DISPLAY_EM / width_em
        width_em *= scale
        height_em *= scale
        depth_em *= scale

    style = f"width:{width_em:.6f}em;height:{height_em:.6f}em"
    if not snippet.display:
        style += f";vertical-align:-{depth_em:.6f}em"
    classes = "mdtp-math mdtp-display" if snippet.display else "mdtp-math"
    alt_source = snippet.source
    if len(alt_source) > 80:
        alt_source = alt_source[:77] + "..."
    payload = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return MathRender(
        f'<img class="{classes}" style="{style}" '
        f'alt="{_html.escape(alt_source, quote=True)}" '
        f'src="data:image/svg+xml;base64,{payload}">'
    )


def _probe_preamble(cfg: MathConfig, tmp_dir: Path) -> str | None:
    """Return a preamble that really compiles, or None when LaTeX is unusable.

    Optional packages are tried first, then the minimal set, then -- when the
    user passed ``--math-preamble`` -- without it.  This keeps a conflicting or
    half-installed TeX Live from taking the whole document down.
    """
    if cfg._probed:
        return cfg.preamble
    cfg._probed = True

    optional = [p for p in _MATH_OPTIONAL_PACKAGES if _have_sty(p)]
    candidates = [_build_preamble(optional, cfg.extra_preamble)]
    if optional:
        candidates.append(_build_preamble([], cfg.extra_preamble))
    if cfg.extra_preamble.strip():
        candidates.append(_build_preamble([], ""))

    probe = MathSnippet(-1, "x", False, "")
    for number, preamble in enumerate(candidates):
        result = _render_math_snippet(
            probe, tmp_dir / f"math-probe{number}", preamble,
            cfg.latex, cfg.dvisvgm,
        )
        if result.ok:
            cfg.preamble = preamble
            return preamble
    return None


def render_math(
    snippets: list[MathSnippet], tmp_dir: Path, cfg: MathConfig
) -> dict[int, MathRender]:
    """Render every snippet in parallel, keyed by snippet index."""
    if not snippets:
        return {}
    if not cfg.usable:
        reason = "math not rendered: 'latex' and 'dvisvgm' are both required"
        return {s.index: MathRender(None, reason) for s in snippets}

    preamble = _probe_preamble(cfg, tmp_dir)
    if preamble is None:
        reason = "LaTeX could not compile the preamble"
        return {s.index: MathRender(None, reason) for s in snippets}

    results: dict[int, MathRender] = {}
    workers = max(1, min(cfg.jobs, len(snippets)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {
            pool.submit(
                _render_math_snippet, snippet, tmp_dir / f"math{snippet.index}",
                preamble, cfg.latex, cfg.dvisvgm,
            ): snippet
            for snippet in snippets
        }
        for future in concurrent.futures.as_completed(pending):
            snippet = pending[future]
            try:
                results[snippet.index] = future.result()
            except Exception as exc:  # noqa: BLE001 - never lose a formula
                results[snippet.index] = MathRender(
                    None, f"{type(exc).__name__}: {exc}"
                )
    return results


def _split_fenced_blocks(md_text: str) -> list[tuple[bool, str]]:
    """Split Markdown into ``(is_fenced_code, text)`` segments.

    Fenced code blocks keep their exact source, so a ``$`` inside them survives
    untouched.  Everything else is safe to scan for math.
    """
    segments: list[tuple[bool, str]] = []
    buffer: list[str] = []
    lines = md_text.splitlines(keepends=True)
    index = 0

    def flush() -> None:
        if buffer:
            segments.append((False, "".join(buffer)))
            buffer.clear()

    while index < len(lines):
        opener = re.match(r"[ \t]{0,3}(`{3,}|~{3,})", lines[index])
        if opener is None:
            buffer.append(lines[index])
            index += 1
            continue

        flush()
        marker = opener.group(1)
        block = [lines[index]]
        index += 1
        closing = re.compile(
            r"[ \t]{0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*$"
        )
        while index < len(lines):
            block.append(lines[index])
            index += 1
            if closing.match(block[-1].rstrip("\n")):
                break
        segments.append((True, "".join(block)))

    flush()
    return segments


def _protect_escaped_dollars(md_text: str) -> str:
    """Hide ``\\$`` from the math regex behind a sentinel."""
    return _ESCAPED_DOLLAR_RE.sub(_MATH_ESCAPED_DOLLAR, md_text)


def _restore_escaped_dollars(md_text: str) -> str:
    """Turn the sentinels back into plain dollar signs."""
    return md_text.replace(_MATH_ESCAPED_DOLLAR, "$")


def extract_math(md_text: str) -> tuple[str, list[MathSnippet]]:
    """Replace every math expression with a placeholder token.

    Returns the rewritten Markdown plus the snippets in document order.  Code
    (fenced blocks and inline spans) is never touched, so a ``$`` inside a code
    sample cannot trigger math rendering.
    """
    snippets: list[MathSnippet] = []

    def replace(match: re.Match) -> str:
        if match.group("code") is not None:
            return match.group(0)

        # Lifted from _MATH_TOKEN_RE, in the same order as its alternatives.
        for group, display in (
            ("dd", True), ("db", True),    # $$...$$    \[...\]
            ("ip", False), ("id", False),  # \(...\)    $...$
        ):
            body = match.group(group)
            if body is None:
                continue
            body = body.strip()
            if not body:
                break                      # empty delimiters: leave as-is
            token = _MATH_PLACEHOLDER.format(len(snippets))
            snippets.append(MathSnippet(len(snippets), body, display, token))
            return token
        return match.group(0)

    pieces: list[str] = []
    for is_code, chunk in _split_fenced_blocks(md_text):
        if is_code:
            pieces.append(chunk)           # code is never scanned for math
            continue
        rewritten = _MATH_TOKEN_RE.sub(
            replace, _protect_escaped_dollars(chunk)
        )
        pieces.append(_restore_escaped_dollars(rewritten))
    return "".join(pieces), snippets


def apply_math(
    body_html: str,
    snippets: list[MathSnippet],
    results: dict[int, MathRender],
    quiet: bool = False,
) -> str:
    """Swap placeholder tokens for the rendered SVG images.

    A formula that failed to typeset is replaced by its literal LaTeX in a
    highlighted ``<code>``, so the document still carries the formula and the
    author can see exactly what went wrong.  ``quiet`` suppresses the per-formula
    report; it is used when the whole backend is missing and a single warning has
    already been printed.
    """
    for snippet in snippets:
        rendered = results.get(snippet.index)
        if rendered is None or not rendered.ok:
            reason = rendered.error if rendered is not None else "not rendered"
            if not quiet:
                first_line = (snippet.source.splitlines() or [""])[0][:70]
                print(f"  [math] {reason}: {first_line}", file=sys.stderr)
            replacement = (
                '<code class="mdtp-math-error" '
                f'title="{_html.escape(reason, quote=True)}">'
                f"{_html.escape(snippet.source)}</code>"
            )
        else:
            replacement = rendered.img or ""
        body_html = body_html.replace(snippet.token, replacement)
    return body_html


def _promote_display_math(soup) -> None:
    """Centre paragraphs that hold nothing but a display formula."""
    for img in soup.select("img.mdtp-math.mdtp-display"):
        parent = img.parent
        if parent is None or parent.name != "p":
            continue
        others = [
            node for node in parent.contents
            if node is not img
            and (getattr(node, "name", None) or str(node).strip())
        ]
        if others:
            continue
        classes = [c for c in (parent.get("class") or []) if c != "mdtp-display"]
        parent["class"] = classes + ["math-display"]


# --------------------------------------------------------------------------- #
#  Table column sizing
# --------------------------------------------------------------------------- #
#
# WeasyPrint sizes columns with the CSS table algorithm, but ``table-layout:
# fixed`` makes it obey explicit widths and ignore its own heuristics, so this
# module measures every table itself and hands WeasyPrint a ``<colgroup>``.
# The widths come from the same min/max-content algorithm browsers use for
# ``table-layout: auto``, which is the closest thing to "perfect" there is:
# every column ends up as wide as it needs to be, and no wider.
#
# Two widths are measured per column, in em at the table font size:
#
#   min -- the widest single unbreakable token ("VD-HS-01", a URL).  Below
#          this the column would have to split a word, so it is a hard floor.
#   max -- the widest natural line once the cell is allowed to wrap.  This is
#          the width at which no cell in the column has to wrap at all.
#
# The printable width is then distributed exactly the way CSS does it:
#
#   * room to spare   -> every column gets ``max`` and the leftover space is
#                        shared out *equally*.  Proportional sharing would keep
#                        widening the widest column for no reason; sharing it
#                        equally keeps every cell on one line (no wrapping at
#                        all) and makes short tables look deliberate.
#   * not enough room -> every column starts at ``min`` and what is left over
#                        is shared in proportion to ``max - min``, so the space
#                        goes where wrapping actually happens.
#   * nothing fits    -> all columns are scaled down from ``min``, still
#                        proportionally.
#
# Two print-only refinements sit on top: columns whose data cells are all
# numeric are right-aligned, and single-token columns that did receive their
# full ``max`` are marked ``nowrap`` so an ID or a date is never split in half.

_CHAR_WIDTH_NARROW = 0.30
_CHAR_WIDTH_WIDE = 0.88
_CHAR_WIDTH_DIGIT = 0.55
_CHAR_WIDTH_UPPER = 0.67
_CHAR_WIDTH_LOWER = 0.52
_CHAR_WIDTH_SPACE = 0.28
_CHAR_WIDTH_MONO = 0.60
_CHAR_WIDTH_CJK = 1.00

_NARROW_CHARS = frozenset("iljtfr.,;:!|'`\"()[]{}-/\\")
_WIDE_CHARS = frozenset("MWmw@&%")

_MONO_TAGS = frozenset({"code", "pre", "kbd", "samp", "tt"})
_BLOCK_TAGS = frozenset({
    "p", "div", "ul", "ol", "li", "dl", "dt", "dd", "table", "tr", "blockquote",
    "pre", "figure", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6",
})

_TABLE_PADDING_EM = 1.20
"""Horizontal cell padding (4pt per side) plus borders, in em at 7.8pt."""

_TABLE_SAFETY = 1.03
"""Small over-estimate so a rounding error never forces an unwanted break."""

_TABLE_NOWRAP_SAFETY_EM = 0.25
"""Extra room (em) a nowrap column must have before ``nowrap`` is applied."""

_TABLE_MIN_COLUMN_PCT = 2.5
_TABLE_MAX_COLUMN_PCT = 95.0
"""Soft print limits.  The floor keeps a two-character column from collapsing
into its own border; the ceiling only stops a degenerate all-in-one column.
Neither is allowed to break the 100% total."""

_PAGE_WIDTH_MM = {
    "a3": 297.0, "a4": 210.0, "a5": 148.0, "a6": 105.0, "b4": 250.0,
    "b5": 176.0, "letter": 215.9, "legal": 215.9, "tabloid": 279.4,
    "ledger": 279.4, "executive": 184.2,
}

_NUMERIC_CELL_RE = re.compile(
    r"^[-(+\u2212$\u20ac\u00a3\u00a5\s]*\d[\d,._\u00a0]*"
    r"(?:\s*[a-zA-Z%/°]{0,6})?\)?$"
)


def _char_width_em(char: str, mono: bool) -> float:
    """Approximate the width of *char* in em for the table font."""
    if char.isspace() or char == "\u00a0":
        return _CHAR_WIDTH_MONO if mono else _CHAR_WIDTH_SPACE
    if mono:
        return _CHAR_WIDTH_MONO
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return _CHAR_WIDTH_CJK
    if char in _NARROW_CHARS:
        return _CHAR_WIDTH_NARROW
    if char in _WIDE_CHARS:
        return _CHAR_WIDTH_WIDE
    if char.isdigit():
        return _CHAR_WIDTH_DIGIT
    if char.isupper():
        return _CHAR_WIDTH_UPPER
    return _CHAR_WIDTH_LOWER


_GENERIC_FAMILIES = frozenset({
    "serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui",
    "ui-monospace", "ui-sans-serif", "ui-serif", "math",
})


def _first_font_family(stack: str) -> str:
    """Return the first concrete family name from a CSS font stack."""
    for part in stack.split(","):
        name = part.strip().strip("'\"")
        if name and name.lower() not in _GENERIC_FAMILIES:
            return name
    first = stack.split(",")[0].strip().strip("'\"") if stack else ""
    return first or "sans-serif"


def _font_file(family: str, bold: bool = False) -> str | None:
    """Return the font file fontconfig picks for *family*, if any."""
    if shutil.which("fc-match") is None:
        return None
    pattern = f"{family}:bold" if bold else family
    try:
        result = subprocess.run(
            ["fc-match", "--format", "%{file}", pattern],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return None
    path = result.stdout.strip()
    return path if path and Path(path).is_file() else None


class TextMetrics:
    """Per-character advance widths (in em) for one font.

    The widths are read straight out of the font file fontconfig selects, so
    the measurements match the glyphs WeasyPrint will actually lay out.  When
    fontTools or fontconfig are missing, :func:`_char_width_em` estimates the
    width instead -- the layout is still sane, just a little less exact.
    """

    def __init__(self, family: str, bold: bool = False) -> None:
        self._advances: dict[str, float] = {}
        path = _font_file(family, bold)
        if path is None:
            return
        try:
            from fontTools.ttLib import TTFont

            with TTFont(path, lazy=True) as font:
                units = font["head"].unitsPerEm or 1000
                cmap = font.getBestCmap() or {}
                metrics = font["hmtx"].metrics
                self._advances = {
                    chr(code): metrics[name][0] / units
                    for code, name in cmap.items()
                    if name in metrics
                }
        except Exception:  # noqa: BLE001 - fall back to the estimate
            self._advances = {}

    def width(self, char: str, mono: bool = False) -> float:
        """Advance width of *char* in em, with an estimate as a fallback."""
        advance = self._advances.get(char)
        return advance if advance is not None else _char_width_em(char, mono)


@dataclass(frozen=True)
class FontPair:
    """Width metrics for the three fonts a table can render with."""

    body: TextMetrics
    head: TextMetrics
    mono: TextMetrics

    def width(self, char: str, *, mono: bool = False, bold: bool = False) -> float:
        """Advance width of *char* in em for the requested font."""
        if mono:
            return self.mono.width(char, mono=True)
        return (self.head if bold else self.body).width(char)


_FONT_PAIR_CACHE: dict[tuple[str, str, str], FontPair] = {}


def table_fonts(
    body_family: str, head_family: str, mono_family: str
) -> FontPair:
    """Return (and cache) width metrics for the three table fonts."""
    key = (body_family, head_family, mono_family)
    pair = _FONT_PAIR_CACHE.get(key)
    if pair is None:
        pair = FontPair(
            body=TextMetrics(body_family),
            head=TextMetrics(head_family, bold=True),
            mono=TextMetrics(mono_family),
        )
        _FONT_PAIR_CACHE[key] = pair
    return pair


def _default_table_fonts() -> FontPair:
    """Metrics for the default theme's table, header and mono fonts."""
    return table_fonts("DejaVu Sans", "DejaVu Sans", "DejaVu Sans Mono")


def _inline_math_width_em(img) -> float:
    """Return the em width of an inline math ``<img>`` from its style attribute."""
    match = re.search(r"width:\s*([\d.]+)em", img.get("style") or "")
    return float(match.group(1)) if match else 0.0


def _cell_lines_em(cell, fonts: FontPair) -> list[list[tuple[float, bool]]]:
    """Split a cell into natural lines of ``(width_em, is_space)`` units.

    Explicit ``<br>``, block children, expanded abbreviations, inline math and
    monospace spans are all honoured, so the measurement reflects what
    WeasyPrint will lay out.
    """
    bold = getattr(cell, "name", None) == "th"
    lines: list[list[tuple[float, bool]]] = [[]]

    def newline() -> None:
        if lines[-1]:
            lines.append([])

    def emit(text: str, mono: bool) -> None:
        lines[-1].extend(
            (
                fonts.width(char, mono=mono, bold=bold),
                char.isspace() or char == "\u00a0",
            )
            for char in text
        )

    def walk(node, mono: bool) -> None:
        if isinstance(node, str):          # bs4.NavigableString subclasses str
            if node:
                emit(node, mono)
            return
        name = getattr(node, "name", None)
        if name is None:
            return
        if name == "br":
            newline()
            return
        block = name in _BLOCK_TAGS
        if block:
            newline()
        if name == "img" and "mdtp-math" in (node.get("class") or []):
            width = _inline_math_width_em(node)
            if width:
                lines[-1].append((width, False))
        elif name == "abbr" and node.get("title"):
            for child in node.children:
                walk(child, mono)
            # The stylesheet renders an abbreviation as "HTML (Hyper Text ...)".
            emit(f" ({node['title']})", mono)
        else:
            inner_mono = mono or name in _MONO_TAGS
            for child in node.children:
                walk(child, inner_mono)
        if block:
            newline()

    for child in cell.children:
        walk(child, False)
    return [line for line in lines if line]


def _cell_metrics_em(cell, fonts: FontPair) -> tuple[float, float]:
    """Return ``(min_content, max_content)`` in em for one cell.

    ``min_content`` is the widest unbreakable token (what the column cannot go
    below without splitting a word), ``max_content`` the widest natural line.
    """
    min_content = 0.0
    max_content = 0.0
    for line in _cell_lines_em(cell, fonts):
        max_content = max(max_content, sum(width for width, _ in line))
        token = 0.0
        for width, is_space in line:
            if is_space:
                min_content = max(min_content, token)
                token = 0.0
            else:
                token += width
        min_content = max(min_content, token)
    return min_content * _TABLE_SAFETY, max_content * _TABLE_SAFETY


def _is_numeric(text: str) -> bool:
    """True when *text* reads as a number, optionally with a unit or percent."""
    return bool(text) and bool(_NUMERIC_CELL_RE.match(text))


def _table_cells(row):
    return row.find_all(["th", "td"])


def _length_mm(value: str) -> float:
    """Convert a CSS length (``14mm``, ``1cm``, ``0.5in``, ``12pt``) to mm."""
    match = re.match(r"^\s*([\d.]+)\s*(mm|cm|in|pt|px)?\s*$", value)
    if not match:
        return 0.0
    factor = {
        "mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72.0, "px": 25.4 / 96.0,
    }[match.group(2) or "mm"]
    return float(match.group(1)) * factor


def table_content_em(page_size: str, page_margin: str, table_size: str) -> float:
    """Return the printable width in em at the given table font size."""
    width_mm = _PAGE_WIDTH_MM.get(page_size.strip().lower(), 210.0)
    parts = page_margin.split()
    left = right = 0.0
    if len(parts) == 1:
        left = right = _length_mm(parts[0])
    elif len(parts) == 2:
        left = right = _length_mm(parts[1])
    elif len(parts) == 3:
        left = right = _length_mm(parts[1])
    elif len(parts) >= 4:
        right, left = _length_mm(parts[1]), _length_mm(parts[3])
    usable_mm = max(width_mm - left - right, 40.0)
    size_pt = _length_mm(table_size) * 72.0 / 25.4
    return usable_mm / 25.4 * 72.0 / (size_pt if size_pt > 0 else 7.8)


def compute_column_widths(
    metrics: list[tuple[float, float]], available_em: float
) -> list[float]:
    """Distribute *available_em* over columns, CSS min/max-content style.

    ``metrics`` holds one ``(min_content, max_content)`` pair per column, in em
    and excluding padding.  The returned widths are in em and include the
    per-cell padding, so they can be turned straight into percentages.
    """
    count = len(metrics)
    if count == 0:
        return []
    usable = max(available_em - _TABLE_PADDING_EM * count, 0.0)
    minimum = [max(min_content, 0.0) for min_content, _ in metrics]
    preferred = [
        max(max_content, min_content) for min_content, max_content in metrics
    ]
    total_min = sum(minimum)
    total_preferred = sum(preferred)

    if total_preferred <= 0.0:              # every cell is empty
        content = [usable / count] * count
    elif total_preferred <= usable:         # nothing has to wrap at all
        # Share the leftover space equally: every column then keeps its whole
        # preferred width, so no cell ever wraps unnecessarily.
        content = [
            width + (usable - total_preferred) / count for width in preferred
        ]
    elif total_min >= usable:               # not even the tokens fit
        content = [usable * width / total_min for width in minimum]
    else:
        span = total_preferred - total_min
        extra = usable - total_min
        content = [
            low + extra * ((high - low) / span)
            for low, high in zip(minimum, preferred)
        ]
    return [width + _TABLE_PADDING_EM for width in content]


def _enforce_minimum(percents: list[float], floor: float) -> list[float]:
    """Raise columns below *floor*, taking the difference from the others.

    The total is preserved, so the row still adds up to 100%.  If *floor* is
    unreachable for every column at once (more columns than fit), the widths are
    left as they are instead of giving up the total.
    """
    values = list(percents)
    for _ in range(len(values) + 1):
        below = [i for i, value in enumerate(values) if value < floor - 1e-9]
        if not below:
            break
        above = [i for i, value in enumerate(values) if value > floor + 1e-9]
        surplus = sum(values[i] - floor for i in above)
        if surplus <= 1e-9:
            break
        missing = sum(floor - values[i] for i in below)
        moved = min(missing, surplus)
        for i in below:
            values[i] += moved * (floor - values[i]) / missing
        for i in above:
            values[i] -= moved * (values[i] - floor) / surplus
    return values


def _enforce_maximum(percents: list[float], ceiling: float) -> list[float]:
    """Pull columns above *ceiling* down, giving the difference to the others.

    The total is preserved.  When every column is above the ceiling there is
    nothing to give the excess to, so the widths are left alone: filling the
    table exactly is more important than the cosmetic cap.
    """
    values = list(percents)
    for _ in range(len(values) + 1):
        above = [i for i, value in enumerate(values) if value > ceiling + 1e-9]
        if not above:
            break
        below = [i for i, value in enumerate(values) if value < ceiling - 1e-9]
        headroom = sum(ceiling - values[i] for i in below)
        if headroom <= 1e-9:
            break
        excess = sum(values[i] - ceiling for i in above)
        moved = min(excess, headroom)
        for i in above:
            values[i] -= moved * (values[i] - ceiling) / excess
        for i in below:
            values[i] += moved * (ceiling - values[i]) / headroom
    return values


def _to_percentages(widths: list[float]) -> list[float]:
    """Turn em widths into percentages that add up to exactly 100."""
    count = len(widths)
    total = sum(widths)
    if count == 0:
        return []
    if total <= 0:
        return [100.0 / count] * count
    percents = [100.0 * width / total for width in widths]
    percents = _enforce_minimum(percents, _TABLE_MIN_COLUMN_PCT)
    percents = _enforce_maximum(percents, _TABLE_MAX_COLUMN_PCT)
    percents = [round(pct, 2) for pct in percents]
    # Push the rounding residual into the widest column so the row adds up to
    # exactly 100% and the table never overflows its container.
    residual = round(100.0 - sum(percents), 2)
    if residual:
        widest = max(range(len(percents)), key=percents.__getitem__)
        percents[widest] = round(percents[widest] + residual, 2)
    return percents


def _colspan(cell) -> int:
    """Return a cell's colspan, tolerating missing or non-numeric values."""
    try:
        return max(1, int(cell.get("colspan", 1)))
    except (TypeError, ValueError):
        return 1


def _default_table_content_em() -> float:
    """Printable width assuming the built-in A4 page and table font size."""
    return table_content_em("A4", "16mm 14mm 18mm 14mm", "7.8pt")


def set_table_column_widths(
    soup,
    available_em: float | None = None,
    fonts: FontPair | None = None,
) -> None:
    """Give every table a content-aware ``<colgroup>`` and per-cell classes.

    ``available_em`` is the printable width in em at the table font size (see
    :func:`table_content_em`) and ``fonts`` the width metrics of the fonts the
    theme renders tables with.  Columns are sized by
    :func:`compute_column_widths`; on top of that, all-numeric columns are
    right-aligned and single-token columns are pinned with ``nowrap`` when they
    actually received enough width for it.

    A table that already carries its own ``<colgroup>`` or ``<col>`` is left
    alone, so hand-written HTML widths still win.
    """
    tables = soup.find_all("table")
    if not tables:
        return
    if fonts is None:
        fonts = _default_table_fonts()
    elif callable(fonts):              # resolve lazily, tables are not cheap
        fonts = fonts()
    if available_em is None:
        available_em = _default_table_content_em()

    for table in tables:
        if table.find("colgroup") is not None or table.find("col") is not None:
            continue
        rows = table.find_all("tr")
        if not rows:
            continue
        ncols = max(
            (sum(_colspan(cell) for cell in _table_cells(row)) for row in rows),
            default=0,
        )
        if ncols <= 0:
            continue

        minimum = [0.0] * ncols
        preferred = [0.0] * ncols
        numeric = [0] * ncols
        filled = [0] * ncols
        for row in rows:
            is_header = row.find("th") is not None and row.find("td") is None
            column = 0
            for cell in _table_cells(row):
                span = _colspan(cell)
                low, high = _cell_metrics_em(cell, fonts)
                text = cell.get_text(" ", strip=True)
                for slot in range(column, min(column + span, ncols)):
                    # A spanning cell shares its requirement between its slots.
                    minimum[slot] = max(minimum[slot], low / span)
                    preferred[slot] = max(preferred[slot], high / span)
                    if not is_header:
                        filled[slot] += 1
                        numeric[slot] += _is_numeric(text)
                column += span

        widths = compute_column_widths(
            list(zip(minimum, preferred)), available_em
        )
        colgroup = soup.new_tag("colgroup")
        for pct in _to_percentages(widths):
            col = soup.new_tag("col")
            col["style"] = f"width: {pct:.2f}%"
            colgroup.append(col)
        table.insert(0, colgroup)

        nowrap = [
            abs(preferred[slot] - minimum[slot]) < 0.01
            and widths[slot]
            >= minimum[slot] + _TABLE_PADDING_EM + _TABLE_NOWRAP_SAFETY_EM
            for slot in range(ncols)
        ]
        right = [
            filled[slot] > 0 and numeric[slot] == filled[slot]
            for slot in range(ncols)
        ]
        for row in rows:
            column = 0
            for cell in _table_cells(row):
                classes = list(cell.get("class") or [])
                if right[min(column, ncols - 1)]:
                    classes.append("mdtp-num")
                if nowrap[min(column, ncols - 1)]:
                    classes.append("mdtp-nowrap")
                if classes:
                    cell["class"] = classes
                column += _colspan(cell)


def extract_title(md_text: str, fallback: str) -> str:
    """Return the first H1 (stripped of markup) or *fallback*.

    Fenced code is skipped, so a ``# comment`` inside a shell sample is never
    mistaken for the document title.
    """
    for is_code, chunk in _split_fenced_blocks(md_text):
        if is_code:
            continue
        match = re.search(r"(?m)^#[ \t]+(.+?)[ \t]*$", chunk)
        if not match:
            continue
        title = re.sub(r"[*_`~]", "", match.group(1)).strip()
        if title:
            return title
    return fallback


@dataclass
class RenderOptions:
    """Everything the HTML stage needs beyond the Markdown source and title."""

    theme: str = "classic"
    page_size: str | None = None
    page_margin: str | None = None
    body_size: str | None = None
    footer_left: str | None = None
    show_footer: bool = True
    custom_css: str = ""
    highlight_style: str | None = None
    markdown: MarkdownSetup = field(default_factory=build_markdown_setup)

    def table_content_em(self) -> float:
        """Printable width in em at the theme's table font size."""
        tokens = theme_tokens(self.theme)
        return table_content_em(
            self.page_size or tokens["page_size"],
            self.page_margin or tokens["page_margin"],
            tokens["table_size"],
        )

    def table_fonts(self) -> FontPair:
        """Width metrics for the theme's table, header and mono fonts."""
        tokens = theme_tokens(self.theme)
        return table_fonts(
            _first_font_family(tokens["table_font"]),
            _first_font_family(tokens["table_head_font"]),
            _first_font_family(tokens["mono_font"]),
        )

    def css(self) -> str:
        """The complete ``<style>`` payload: theme, Pygments rules, user CSS."""
        extra: list[str] = []
        if self.markdown.highlighting:
            extra.append(highlight_css(self.highlight_style))
        if self.custom_css.strip():
            extra.append(self.custom_css)
        return build_css(
            self.theme,
            page_size=self.page_size,
            page_margin=self.page_margin,
            body_size=self.body_size,
            footer_left=self.footer_left,
            show_footer=self.show_footer,
            extra_css="\n".join(extra),
        )


def build_html(
    md_text: str,
    title: str,
    *,
    math_cfg: MathConfig | None = None,
    tmp_dir: Path | None = None,
    options: RenderOptions | None = None,
) -> str:
    """Render Markdown to a complete, styled HTML document."""
    options = options or RenderOptions()

    # Nest lists the way the author wrote them, whatever the indent width.
    md_text = _normalise_list_indentation(md_text)

    # Fenced code nested in a list or blockquote is pulled out after the list
    # items have been put on the four-space grid, so the placeholder can be
    # indentated to stay inside the item it belongs to.  Python-Markdown's
    # fenced_code only runs on top-level blocks, so these would otherwise come
    # out as inline code spans (or escape the list entirely).
    md_text, nested_fences = extract_nested_fences(md_text)

    snippets: list[MathSnippet] = []
    if math_cfg is not None and math_cfg.enabled:
        md_text, snippets = extract_math(md_text)
    else:
        # Keep ``\$`` a literal dollar sign even when math is switched off.
        md_text = _restore_escaped_dollars(_protect_escaped_dollars(md_text))

    body = markdown.markdown(
        md_text,
        extensions=list(options.markdown.extensions),
        extension_configs=options.markdown.extension_configs,
        output_format="html5",
    )

    for index, fence in enumerate(nested_fences):
        placeholder = _NESTED_FENCE_PLACEHOLDER.format(index)
        block = _render_nested_fence(fence, options)
        # Drop the paragraph the placeholder sits in: the block replaces it.
        body = body.replace(f"<p>{placeholder}</p>", block)
        body = body.replace(placeholder, block)

    if snippets and math_cfg is not None and tmp_dir is not None:
        body = apply_math(
            body,
            snippets,
            render_math(snippets, tmp_dir, math_cfg),
            quiet=not math_cfg.usable,
        )

    soup = BeautifulSoup(
        f'<div class="markdown-body">{body}</div>', "html.parser"
    )
    if snippets:
        _promote_display_math(soup)
    set_table_column_widths(
        soup, options.table_content_em(), options.table_fonts
    )
    body_html = soup.select_one("div.markdown-body").decode_contents()

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{_html.escape(title)}</title>\n"
        f"<style>{options.css()}</style>\n"
        "</head>\n"
        f"<body>\n{body_html}\n</body>\n"
        "</html>\n"
    )


def _slug(text: str) -> str:
    """Return a filesystem-safe slug for use in temporary HTML file names."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return slug or "document"


def render_one(
    md_path: Path,
    tmp_dir: Path,
    *,
    output: Path,
    keep_html: bool,
    index: int = 0,
    math_cfg: MathConfig | None = None,
    options: RenderOptions | None = None,
) -> Path:
    """Convert a single Markdown file to the given output PDF path."""
    md_text = md_path.read_text(encoding="utf-8")
    title = extract_title(md_text, md_path.stem)
    html_doc = build_html(
        md_text, title, math_cfg=math_cfg, tmp_dir=tmp_dir, options=options
    )

    # Unique name per input so repeated runs / identical stems never clash.
    html_path = tmp_dir / f"{index:03d}-{_slug(md_path.stem)}.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html_doc, encoding="utf-8")

    output.parent.mkdir(parents=True, exist_ok=True)
    # Resolve relative links/images against the Markdown file's own directory.
    HTML(filename=str(html_path), base_url=str(md_path.parent)).write_pdf(
        str(output)
    )

    if keep_html:
        shutil.copy2(html_path, output.with_suffix(".html"))
    return output


def _split_extension_names(values: Iterable[str]) -> list[str]:
    """Flatten repeated, possibly comma-separated ``-x``/``-X`` values."""
    names: list[str] = []
    for value in values:
        for name in value.split(","):
            name = name.strip()
            if name and name not in names:
                names.append(name)
    return names


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert one or more Markdown files into matching PDFs "
                    "(Markdown -> HTML -> PDF).",
        epilog="Example: md_to_pdf.py SPECIFICATION.md STYLE_GUIDE.md",
    )
    parser.add_argument(
        "files",
        nargs="*",
        metavar="MD_FILE",
        help="Markdown file(s) to convert. Each PDF is written next to its "
             "source unless --output is given.",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="PDF_FILE",
        help="Output PDF path. Only valid with a single input file; defaults "
             "to the input path with a .pdf suffix.",
    )
    parser.add_argument(
        "--theme",
        choices=available_themes(),
        default="classic",
        help="Visual theme (default: classic). See --list-themes.",
    )
    parser.add_argument(
        "--list-themes",
        action="store_true",
        help="Print the available themes and exit.",
    )
    parser.add_argument(
        "--page-size",
        metavar="SIZE",
        help="Page size (default from the theme, usually A4). Any CSS size "
             "works: A4, Letter, '210mm 297mm'.",
    )
    parser.add_argument(
        "--page-margin",
        metavar="CSS",
        help="Page margins as CSS lengths, e.g. '18mm 16mm' (default from the "
             "theme).",
    )
    parser.add_argument(
        "--font-size",
        type=float,
        metavar="PT",
        help="Body font size in points (default from the theme, usually 9.5).",
    )
    parser.add_argument(
        "--footer",
        metavar="TEXT",
        help="Text for the left-hand running footer (default from the theme).",
    )
    parser.add_argument(
        "--no-footer",
        action="store_true",
        help="Omit the running footer (page numbers included).",
    )
    parser.add_argument(
        "--css",
        action="append",
        default=[],
        metavar="FILE",
        help="Extra CSS file appended after the theme (repeatable).",
    )
    parser.add_argument(
        "-x",
        "--extension",
        action="append",
        default=[],
        dest="extensions",
        metavar="NAME",
        help="Extra Python-Markdown extension to enable, comma-separated "
             "names allowed (repeatable).",
    )
    parser.add_argument(
        "-X",
        "--no-extension",
        action="append",
        default=[],
        dest="disabled_extensions",
        metavar="NAME",
        help="Built-in extension to disable, comma-separated names allowed "
             "(repeatable).",
    )
    parser.add_argument(
        "--highlight-style",
        metavar="NAME",
        help="Pygments style for fenced code (default: friendly). "
             "Use --no-highlight to switch highlighting off.",
    )
    parser.add_argument(
        "--no-highlight",
        dest="highlight",
        action="store_false",
        help="Do not syntax-highlight fenced code blocks.",
    )
    parser.add_argument(
        "--keep-html",
        action="store_true",
        help="Also write the intermediate .html next to each PDF (off by default).",
    )
    parser.add_argument(
        "--no-math",
        dest="math",
        action="store_false",
        help="Leave $...$ and $$...$$ as plain text (no LaTeX rendering).",
    )
    parser.add_argument(
        "--math-preamble",
        metavar="FILE",
        help="LaTeX file whose contents are inserted into the preamble before "
             "\\begin{document} for every formula (e.g. \\newcommand lines).",
    )
    parser.add_argument(
        "--math-jobs",
        type=int,
        metavar="N",
        help="Parallel LaTeX processes used to render formulas (default: 8).",
    )
    args = parser.parse_args(argv)

    if args.list_themes:
        for name in available_themes():
            print(f"  {name:<10} {_THEME_DESCRIPTIONS.get(name, '')}")
        return 0

    if not args.files:
        parser.error("at least one MD_FILE is required")
    if args.output and len(args.files) != 1:
        parser.error("--output requires exactly one input file")

    # --- extensions and styling -------------------------------------------- #
    added = _split_extension_names(args.extensions)
    removed = _split_extension_names(args.disabled_extensions)
    try:
        markdown_setup = build_markdown_setup(
            add=added, remove=removed, highlight=args.highlight
        )
    except Exception as exc:  # noqa: BLE001 - bad name/config, report cleanly
        parser.error(f"invalid extension: {exc}")

    custom_css_parts: list[str] = []
    for css_file in args.css:
        css_path = Path(css_file).expanduser()
        if not css_path.is_file():
            parser.error(f"--css: '{css_path}' is not a file")
        custom_css_parts.append(css_path.read_text(encoding="utf-8"))

    if args.highlight_style and not markdown_setup.highlighting:
        print(
            "warning: --highlight-style has no effect - syntax highlighting "
            "is disabled or Pygments is not installed",
            file=sys.stderr,
        )

    options = RenderOptions(
        theme=args.theme,
        page_size=args.page_size,
        page_margin=args.page_margin,
        body_size=f"{args.font_size:g}pt" if args.font_size else None,
        footer_left=args.footer,
        show_footer=not args.no_footer,
        custom_css="\n".join(custom_css_parts),
        highlight_style=args.highlight_style,
        markdown=markdown_setup,
    )

    extra_preamble = ""
    if args.math_preamble:
        preamble_file = Path(args.math_preamble).expanduser()
        if not preamble_file.is_file():
            parser.error(f"--math-preamble: '{preamble_file}' is not a file")
        extra_preamble = preamble_file.read_text(encoding="utf-8")

    math_cfg = MathConfig(
        enabled=args.math,
        jobs=max(1, args.math_jobs or min(8, os.cpu_count() or 1)),
        extra_preamble=extra_preamble,
        latex=shutil.which("latex"),
        dvisvgm=shutil.which("dvisvgm"),
    )
    if math_cfg.enabled and not math_cfg.usable:
        print(
            "warning: math will not be rendered - 'latex' and 'dvisvgm' are "
            "both required (see --no-math)",
            file=sys.stderr,
        )

    # Validate every input up front so we fail fast before writing anything.
    md_files: list[Path] = []
    for raw in args.files:
        path = Path(raw).expanduser()
        if not path.is_file():
            print(f"error: '{path}' is not a file", file=sys.stderr)
            return 2
        md_files.append(path.resolve())

    output = Path(args.output).expanduser().resolve() if args.output else None

    tmp_dir = Path(tempfile.mkdtemp(prefix="md2pdf-"))
    ok = 0
    failed = []
    for index, md_path in enumerate(md_files):
        out_path = output if output is not None else md_path.with_suffix(".pdf")
        try:
            render_one(
                md_path,
                tmp_dir,
                output=out_path,
                keep_html=args.keep_html,
                index=index,
                math_cfg=math_cfg,
                options=options,
            )
            ok += 1
            print(f"  [ok]   {md_path}  ->  {out_path}")
        except Exception as exc:  # noqa: BLE001 - keep going on per-file errors
            failed.append((str(md_path), exc))
            print(f"  [FAIL] {md_path}: {exc}", file=sys.stderr)

    shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"\nDone: {ok} PDF{'s' if ok != 1 else ''} generated, "
          f"{len(failed)} failed.")
    for path, exc in failed:
        print(f"  - {path}: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
