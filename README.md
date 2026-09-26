# md-to-pdf

`md_to_pdf.py` — robust **Markdown → HTML → PDF** pipeline built on
Python-Markdown + WeasyPrint. Installed as the console commands
**`md-to-pdf`** and **`md2pdf`**.

## Usage

```bash
md-to-pdf FILE.md [FILE.md ...]          # PDF written next to each .md
md-to-pdf FILE.md -o /tmp/out.pdf        # explicit output (single input only)
md-to-pdf FILE.md --keep-html            # also keep intermediate .html
md-to-pdf --list-themes
md-to-pdf --help
```

## Themes

Five print-optimised themes are built in. Every colour, font and metric is a
token, so switching themes never changes the document structure — only the
look.

| Theme | Look |
| --- | --- |
| `classic` *(default)* | Corporate navy blue on a DejaVu Serif body |
| `modern` | Sans-serif, indigo accent, light table headers |
| `academic` | Times-like serif with navy accents — papers and specifications |
| `minimal` | Grayscale, hairline rules, nothing decorative |
| `github` | The familiar GitHub Markdown look, print-optimised |

```bash
md-to-pdf SPEC.md --theme modern
md-to-pdf SPEC.md --css house-style.css        # repeatable; appended last
md-to-pdf SPEC.md --page-size Letter --font-size 10
md-to-pdf SPEC.md --footer "Acme Corp — Confidential"
md-to-pdf SPEC.md --no-footer                  # no footer, no page numbers
```

`--css` is appended after the theme (and after the syntax-highlighting rules),
so it is the right place for a house style or a one-off tweak.

## Markdown support

Everything useful for a printed document is enabled out of the box:

| Feature | Extension |
| --- | --- |
| Tables (GFM pipe tables) | `extra` |
| Fenced code + syntax highlighting | `extra` + `codehilite` (Pygments) |
| Footnotes | `extra` |
| Definition lists | `extra` |
| Abbreviations (`*[HTML]: ...`) | `extra` |
| Attribute lists (`{: .class #id }`) | `extra` |
| Raw HTML, including `markdown="1"` | `extra` |
| Table of contents (`[TOC]`) | `toc` |
| Admonitions (`!!! note`) | `admonition` |
| Smart quotes, dashes, ellipses | `smarty` |
| Task lists (`- [x]`) and `~~strikethrough~~` | built into this tool |

```bash
md-to-pdf SPEC.md -x meta,wikilinks     # enable more extensions
md-to-pdf SPEC.md -X smarty             # disable one of the defaults
md-to-pdf SPEC.md --no-highlight        # plain code blocks
md-to-pdf SPEC.md --highlight-style monokai
```

The `[TOC]` marker becomes a table of contents with **dot leaders and real page
numbers** (WeasyPrint's `leader()` + `target-counter()`), abbreviations are
expanded inline as `HTML (Hyper Text Markup Language)` because print has no
hover, and task-list checkboxes become ballot boxes (`☐` / `☑`).

### Nested lists

Python-Markdown only nests a list item when it is indented by *four* spaces,
while almost every other implementation — and almost every author — uses two or
three. This tool rewrites list indentation to exactly `4 × depth` before
parsing, so lists nest the way they are written:

```markdown
1. First item
2. Second item
   1. Nested ordered       ← two or three spaces both work
      - Deep bullet
        - Deeper still
3. Third item
```

Indentation relative to an enclosing block is preserved, so lists keep their
indentation inside blockquotes and `!!! admonition` blocks, and fenced code is
never touched.

### Code inside lists and blockquotes

Python-Markdown's fenced-code extension only understands fences at the top
level of a document: a fence indented under a list item, or written inside a
blockquote, normally comes out as an inline code span — or escapes the list
entirely. This tool renders those blocks on their own and puts them back, so
they are real, highlighted code blocks in the right place:

````markdown
1. Install it:

   ```bash
   pip install md-to-pdf
   ```

2. Run it.
````

```markdown
> ```python
> print("also inside blockquotes")
> ```
```

## Table column sizing

Tables are measured and given a `<colgroup>` of content-aware widths, using the
same min/max-content algorithm browsers apply to `table-layout: auto`:

1. every column is measured in `em` — the widest *unbreakable token* (an
   ID like `VD-HS-01`, a URL) is its hard minimum, the widest *natural line* is
   the width at which nothing has to wrap;
2. the printable width is shared out: when there is room to spare, every column
   gets its preferred width and the leftover space is split equally (so no cell
   ever wraps needlessly); when space is tight, columns start from their minimum
   and share the remainder in proportion to `max − min`, which puts the space
   where wrapping actually happens.

Character widths are read from the **real font files** (via fontconfig and
fontTools) rather than guessed, so the measurement matches what WeasyPrint
renders. Two refinements come on top: all-numeric columns are right-aligned,
and single-token columns (IDs, dates, `SN-000-12345`) are marked `nowrap` when
they received enough width, so a value is never split across two lines.

## Math

LaTeX math is typeset by a real LaTeX installation and embedded as vector SVG.

| In Markdown | Meaning |
| --- | --- |
| `$E = mc^2$` | inline math |
| `\(E = mc^2\)` | inline math |
| `$$E = mc^2$$` | display math |
| `\[ E = mc^2 \]` | display math |

* Math is sized in `em`, so it scales with the surrounding font size (including
  inside small table cells), sits exactly on the text baseline, and stays
  vector-sharp at any zoom — no raster images in the PDF.
* A `$` inside fenced code blocks and inline `` `code` `` is never treated as
  math, and `\$` is always a literal dollar sign, so `$5 and $10` is safe.
* `align` / `gather` are rewritten to `aligned` / `gathered` automatically, so
  multi-line display math works inside `\[ ... \]`.
* `mathtools`, `mhchem` (`\ce{}`), `siunitx`, `cancel`, `xcolor`, `array`,
  `mathrsfs`, `esint`, `upgreek` and `stmaryrd` are loaded automatically when
  they are installed.

Requires `latex` and `dvisvgm` on `PATH` (Debian/Ubuntu):

```bash
sudo apt install texlive-latex-base texlive-latex-recommended \
                 texlive-latex-extra dvisvgm
```

```bash
md-to-pdf FILE.md --no-math                     # leave $...$ as plain text
md-to-pdf FILE.md --math-jobs 4                 # parallel LaTeX processes
md-to-pdf FILE.md --math-preamble macros.tex    # extra \newcommand definitions
```

A formula with a LaTeX error only affects itself: it stays in the PDF as
literal LaTeX (highlighted), the reason is printed on stderr, and the rest of
the document still converts. If LaTeX is missing entirely, math is left as
plain text and a warning is printed.

## How it is installed

The package is installed **non-editable** via [pipx], so the code is copied
into its own private virtualenv at
`~/.local/share/pipx/venvs/md-to-pdf/`. That makes the command entirely
self-contained: deleting/moving this source folder does **not** break it.

`~/.local/bin/md-to-pdf` and `~/.local/bin/md2pdf` are symlinks into that venv.
`~/.local/bin` is already on `$PATH`.

Why not `--editable`: an editable install is only a *pointer* to this folder.
If this folder is ever deleted or cleaned up, the command dies with
`ModuleNotFoundError: No module named 'md_to_pdf'` (exactly what happened
before). Use `--editable` only while actively developing.

## Reinstalling

After editing `md_to_pdf.py` or `pyproject.toml`, re-install so the venv copy
is refreshed:

```bash
pipx install --force /home/wesjo/Documents/Projects/md-to-pdf
```

> **Note for the VS Code integrated terminal.** The VS Code *snap* exports
> `XDG_DATA_HOME=/home/wesjo/snap/code/<rev>/.local/share`, which makes pipx
> invent a completely different (empty) pipx home — so `pipx list` there
> reports "nothing has been installed with pipx". Point it at the real home:

```bash
env XDG_DATA_HOME=/home/wesjo/.local/share \
    PIPX_HOME=/home/wesjo/.local/share/pipx \
    PIPX_BIN_DIR=/home/wesjo/.local/bin \
    pipx install --force /home/wesjo/Documents/Projects/md-to-pdf
```

The same prefix is needed for `pipx list`, `pipx uninstall md-to-pdf`, etc.
A normal desktop terminal (outside the snap) needs none of this.

## Development

The test suite uses the standard library `unittest` runner and needs no extra
dependencies, but it does import the module (so WeasyPrint must be importable):

```bash
# in the development venv / the pipx venv
python -m unittest discover -s tests -v
```

It covers list indentation normalisation, the table column-sizing algorithm and
its bounds handling, the extension setup, the theme token/CSS generation,
title extraction and one end-to-end conversion through WeasyPrint.

## Uninstall

```bash
pipx uninstall md-to-pdf
```

## Dependencies

Declared in `pyproject.toml` and installed into the same venv by pipx:
`weasyprint>=62`, `markdown>=3.4`, `beautifulsoup4>=4.11`, `pygments>=2.10`.

`pygments` is optional at runtime: without it fenced code blocks are still
rendered, just without syntax colouring (`md-to-pdf --no-highlight` skips it
deliberately). Table measurement uses `fontTools` (already a WeasyPrint
dependency) and `fc-match` from fontconfig when they are available, and falls
back to estimated character widths when they are not.

[pipx]: https://pipx.pypa.io/
