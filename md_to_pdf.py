#!/usr/bin/env python3
"""md_to_pdf.py — robust Markdown -> HTML -> PDF pipeline.

Given one or more Markdown files this script produces a matching ``*.pdf`` --
either next to the source file or at an explicit ``--output`` path.  It works
in two stages:

    1. Markdown -> HTML   (Python-Markdown, with GFM-style pipe tables,
                           fenced code blocks, "sane" lists and attribute
                           lists enabled).
    2. HTML      -> PDF   (WeasyPrint, with print-optimised CSS: repeating
                           table headers, content-aware column sizing, page
                           numbers, and Unicode/box-drawing-safe fonts).

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
    md_to_pdf.py MD_FILE [MD_FILE ...] [-o OUTPUT.pdf] [--keep-html]
                         [--no-math] [--math-preamble FILE] [--math-jobs N]

Examples:
    md_to_pdf.py SPECIFICATION.md
    md_to_pdf.py SPECIFICATION.md STYLE_GUIDE.md
    md_to_pdf.py README.md -o /tmp/readme.pdf

Dependencies (all on PyPI):
    pip install weasyprint markdown beautifulsoup4

Math additionally needs a LaTeX installation and ``dvisvgm`` on PATH --
on Debian/Ubuntu: texlive-latex-base, texlive-latex-recommended,
texlive-latex-extra and dvisvgm.  Without them the formulas are left in the
output as literal LaTeX instead of failing the conversion.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import functools
import html as _html
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
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
        "    /tmp/md2pdf-venv/bin/pip install weasyprint markdown beautifulsoup4\n"
        "    /tmp/md2pdf-venv/bin/python md_to_pdf.py /path/to/file.md\n\n" % exc
    )
    sys.exit(2)

# WeasyPrint logs a fair amount of harmless CSS warnings; only surface errors.
logging.getLogger("weasyprint").setLevel(logging.ERROR)
logging.getLogger("fontTools").setLevel(logging.ERROR)


CSS = (
    r"""
@page {
    size: A4;
    margin: 16mm 14mm 18mm 14mm;
    @bottom-left {
        content: "Veribus Design \2014  Confidential";
        font-family: 'DejaVu Sans', sans-serif;
        font-size: 7pt;
        color: #8a939c;
    }
    @bottom-right {
        content: "Page " counter(page) " of " counter(pages);
        font-family: 'DejaVu Sans', sans-serif;
        font-size: 7pt;
        color: #8a939c;
    }
}

* { box-sizing: border-box; }

body {
    margin: 0;
    font-family: 'DejaVu Serif', 'DejaVu Sans', serif;
    font-size: 9.5pt;
    line-height: 1.45;
    color: #1a1d21;
}

/* ---------- headings ---------- */
h1, h2, h3, h4, h5, h6 {
    font-family: 'DejaVu Sans', sans-serif;
    color: #0b3d66;
    break-after: avoid;
    page-break-after: avoid;
    line-height: 1.2;
    margin: 1em 0 0.4em 0;
}
h1 {
    font-size: 17pt;
    margin-top: 0;
    padding-bottom: 4pt;
    border-bottom: 2pt solid #0b3d66;
}
h2 {
    font-size: 13pt;
    margin-top: 1.2em;
    padding-bottom: 3pt;
    border-bottom: 0.8pt solid #c9d4dd;
}
h3 { font-size: 11pt; color: #14538a; }
h4 { font-size: 10pt; color: #1a1d21; }

/* ---------- body text ---------- */
p {
    margin: 0.5em 0;
    text-align: justify;
    hyphens: auto;
}
ul, ol {
    margin: 0.4em 0 0.7em 0;
    padding-left: 1.6em;
}
li { margin: 0.18em 0; }
strong { color: #0b3d66; }
a { color: #14538a; }

hr {
    border: none;
    border-top: 0.8pt solid #c9d4dd;
    margin: 1.1em 0;
}
"""
    r"""
/* ---------- inline & block code ---------- */
code {
    font-family: 'DejaVu Sans Mono', 'Noto Sans Mono', 'Liberation Mono', monospace;
    font-size: 8.2pt;
    background: #f2f4f6;
    border: 0.5pt solid #e0e4e8;
    border-radius: 2pt;
    padding: 0 2pt;
    white-space: pre-wrap;
    overflow-wrap: break-word;
}
pre {
    font-family: 'DejaVu Sans Mono', 'Noto Sans Mono', 'Liberation Mono', monospace;
    font-size: 7.9pt;
    line-height: 1.3;
    color: #1a1d21;
    background: #f7f9fa;
    border: 0.6pt solid #d5dbe0;
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
}

/* ---------- tables ---------- */
table {
    width: 100%;
    border-collapse: collapse;
    table-layout: fixed;
    margin: 0.7em 0 1em 0;
    font-family: 'DejaVu Sans', sans-serif;
    font-size: 7.8pt;
    line-height: 1.35;
}
thead { display: table-header-group; }
th {
    background: #0b3d66;
    color: #ffffff;
    text-align: left;
    font-weight: bold;
    border: 0.5pt solid #0b3d66;
    padding: 3pt 4pt;
    vertical-align: top;
    overflow-wrap: break-word;
    hyphens: manual;
}
td {
    border: 0.5pt solid #b9c4cc;
    padding: 3pt 4pt;
    vertical-align: top;
    overflow-wrap: break-word;
    hyphens: manual;
}
tbody tr:nth-child(even) { background: #f2f6f9; }
tbody tr {
    break-inside: avoid;
    page-break-inside: avoid;
}
table code {
    font-size: 7.4pt;
    background: #eef1f4;
    border-color: #dde3e8;
}

/* ---------- math (LaTeX -> SVG) ---------- */
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
    color: #a11b1b;
    background: #fdf3f3;
    border-color: #e8c4c4;
}
"""
)


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


def _cell_weight(cell) -> float:
    """Balanced column 'importance' for a single cell.

    Uses the longest whitespace-separated token (so short IDs like
    ``VD-HS-01`` are never squeezed into breaking) plus a square-root of the
    total text length (so very long prose columns get more room without
    dominating the layout).
    """
    text = cell.get_text(" ", strip=True)
    longest_token = max((len(t) for t in text.split()), default=0)
    return max(longest_token, 1.5 * math.sqrt(len(text)), 1.0)


def _table_cells(row):
    return row.find_all(["th", "td"])


def set_table_column_widths(soup) -> None:
    """Give every table content-aware ``<colgroup>`` widths.

    With ``table-layout: fixed`` WeasyPrint honours explicit column widths, so
    we derive a sensible proportion per column from the longest text in each
    column.  This keeps narrow ID/field columns from stealing space from the
    long "Pass"/"Description" columns and guarantees nothing overflows.
    """
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        ncols = max((len(_table_cells(r)) for r in rows), default=0)
        if ncols <= 0:
            continue

        weights = [0.0] * ncols
        for row in rows:
            cells = _table_cells(row)
            is_header = row.find("th") is not None and row.find("td") is None
            for idx, cell in enumerate(cells[:ncols]):
                weight = _cell_weight(cell)
                if is_header:
                    weight *= 1.25
                if weight > weights[idx]:
                    weights[idx] = weight

        # Prevent any column from collapsing to nothing.
        weights = [max(w, 5.0) for w in weights]
        total = sum(weights)
        widths = (
            [100.0 / ncols] * ncols
            if total <= 0
            else [100.0 * w / total for w in weights]
        )

        # Hard percentage floor so a short ID column never gets crushed.
        min_pct = 6.0
        widths = [max(p, min_pct) for p in widths]
        s = sum(widths)
        widths = [p * 100.0 / s for p in widths]

        colgroup = soup.new_tag("colgroup")
        for pct in widths:
            col = soup.new_tag("col")
            col["style"] = f"width: {pct:.2f}%"
            colgroup.append(col)
        table.insert(0, colgroup)


def extract_title(md_text: str, fallback: str) -> str:
    """Return the first H1 (stripped of markup) or ``fallback``."""
    match = re.search(r"(?m)^#\s+(.+?)\s*$", md_text)
    if match:
        title = re.sub(r"[*_`]", "", match.group(1)).strip()
        if title:
            return title
    return fallback


def build_html(
    md_text: str,
    title: str,
    math_cfg: MathConfig | None = None,
    tmp_dir: Path | None = None,
) -> str:
    """Render Markdown to a complete, styled HTML document."""
    snippets: list[MathSnippet] = []
    if math_cfg is not None and math_cfg.enabled:
        md_text, snippets = extract_math(md_text)
    else:
        # Keep ``\$`` a literal dollar sign even when math is switched off.
        md_text = _restore_escaped_dollars(_protect_escaped_dollars(md_text))

    body = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "sane_lists", "attr_list"],
        output_format="html5",
    )

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
    set_table_column_widths(soup)
    body_html = soup.select_one("div.markdown-body").decode_contents()

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{_html.escape(title)}</title>\n"
        f"<style>{CSS}</style>\n"
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
) -> Path:
    """Convert a single Markdown file to the given output PDF path."""
    md_text = md_path.read_text(encoding="utf-8")
    title = extract_title(md_text, md_path.stem)
    html_doc = build_html(md_text, title, math_cfg=math_cfg, tmp_dir=tmp_dir)

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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert one or more Markdown files into matching PDFs "
                    "(Markdown -> HTML -> PDF).",
        epilog="Example: md_to_pdf.py SPECIFICATION.md STYLE_GUIDE.md",
    )
    parser.add_argument(
        "files",
        nargs="+",
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

    if args.output and len(args.files) != 1:
        parser.error("--output requires exactly one input file")

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
