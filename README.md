# md-to-pdf

`md_to_pdf.py` — robust **Markdown → HTML → PDF** pipeline built on
Python-Markdown + WeasyPrint. Installed as the console commands
**`md-to-pdf`** and **`md2pdf`**.

## Usage

```bash
md-to-pdf FILE.md [FILE.md ...]          # PDF written next to each .md
md-to-pdf FILE.md -o /tmp/out.pdf        # explicit output (single input only)
md-to-pdf FILE.md --keep-html            # also keep intermediate .html
md-to-pdf --help
```

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

## Uninstall

```bash
pipx uninstall md-to-pdf
```

## Dependencies

Declared in `pyproject.toml` and installed into the same venv by pipx:
`weasyprint>=62`, `markdown>=3.4`, `beautifulsoup4>=4.11`.

[pipx]: https://pipx.pypa.io/
