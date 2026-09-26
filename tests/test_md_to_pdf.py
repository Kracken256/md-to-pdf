"""Tests for md_to_pdf.

Run them with the interpreter that has the runtime dependencies installed::

    python -m unittest discover -s tests -v
    # or, from a pipx-style venv:
    ~/.local/share/pipx/venvs/md-to-pdf/bin/python -m unittest discover -s tests -v

They cover the parts that are easy to get subtly wrong: list indentation
normalisation, the table column sizing algorithm, the Markdown extension setup
and theme CSS generation.  The LaTeX math tests only run when ``latex`` and
``dvisvgm`` are on PATH.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

import md_to_pdf as m


def _render(md_text: str, **kwargs) -> BeautifulSoup:
    """Render Markdown through the real pipeline and return the parsed body."""
    options = kwargs.pop("options", None) or m.RenderOptions()
    html = m.build_html(md_text, "Test", options=options, **kwargs)
    return BeautifulSoup(html, "html.parser").body


class ListIndentationTests(unittest.TestCase):
    """Nested lists must nest, whatever indentation the author used."""

    def _render(self, md_text: str) -> BeautifulSoup:
        return _render(md_text, options=m.RenderOptions(
            markdown=m.build_markdown_setup(highlight=False)
        ))

    def _nesting_depth(self, soup) -> int:
        def walk(node, depth: int) -> int:
            deepest = depth
            for item in node.find_all("li", recursive=False):
                for child in item.find_all(["ul", "ol"], recursive=False):
                    deepest = max(deepest, walk(child, depth + 1))
            return deepest

        return max(
            (walk(list_el, 1) for list_el in soup.find_all(["ul", "ol"])),
            default=0,
        )

    def test_two_space_indent_nests(self):
        soup = self._render("- a\n  - b\n")
        self.assertEqual(self._nesting_depth(soup), 2)
        self.assertIsNotNone(soup.select_one("li > ul > li"))

    def test_three_space_indent_nests(self):
        soup = self._render("1. a\n   1. b\n")
        self.assertEqual(self._nesting_depth(soup), 2)
        self.assertIsNotNone(soup.select_one("li > ol > li"))

    def test_deep_mixed_nesting(self):
        md = (
            "1. one\n"
            "2. two\n"
            "   1. two a\n"
            "   2. two b\n"
            "      - deep bullet\n"
            "      - another\n"
            "        1. deeper\n"
            "        2. and more\n"
            "3. three\n"
        )
        soup = self._render(md)
        self.assertEqual(self._nesting_depth(soup), 4)
        top_level = soup.select_one("ol")
        self.assertEqual(len(top_level.find_all("li", recursive=False)), 3)

    def test_four_space_indent_nests(self):
        soup = self._render("* a\n    * b\n")
        self.assertEqual(self._nesting_depth(soup), 2)

    def test_normalised_indentation_is_exact_multiple_of_four(self):
        normalised = m._normalise_list_indentation("- a\n  - b\n    - c\n")
        self.assertEqual(normalised, "- a\n    - b\n        - c\n")

    def test_top_level_list_is_untouched(self):
        source = "- a\n- b\n- c\n"
        self.assertEqual(m._normalise_list_indentation(source), source)

    def test_baseline_indent_is_preserved_so_admonitions_keep_their_list(self):
        md = (
            '!!! note "T"\n'
            "    Body text:\n"
            "\n"
            "    - one\n"
            "    - two\n"
        )
        soup = self._render(md)
        admonition = soup.select_one("div.admonition")
        self.assertIsNotNone(admonition)
        self.assertIsNotNone(admonition.select_one("ul"))
        self.assertIsNone(soup.find("ul", recursive=False))

    def test_nested_list_inside_admonition(self):
        md = (
            '!!! note "T"\n'
            "    - one\n"
            "      - nested\n"
        )
        soup = self._render(md)
        self.assertIsNotNone(
            soup.select_one("div.admonition li > ul > li")
        )

    def test_blockquote_lists_nest(self):
        soup = self._render("> - a\n>   - b\n>     - c\n")
        self.assertEqual(self._nesting_depth(soup), 3)
        self.assertIsNotNone(soup.select_one("blockquote li > ul > li > ul > li"))

    def test_paragraph_ends_the_list_context(self):
        md = "- a\n  - b\n\nParagraph.\n\n    - not a list item\n"
        normalised = m._normalise_list_indentation(md)
        self.assertIn("\n    - not a list item\n", normalised)

    def test_fenced_code_indentation_is_untouched(self):
        md = "```\n  - a\n    - b\n```\n"
        self.assertEqual(m._normalise_list_indentation(md), md)

    def test_list_in_code_fence_is_untouched(self):
        md = "- a\n  - b\n\n```text\n  - c\n    - d\n```\n"
        normalised = m._normalise_list_indentation(md)
        self.assertIn("```text\n  - c\n    - d\n```", normalised)
        self.assertIn("- a\n    - b\n", normalised)


class TableColumnSizingTests(unittest.TestCase):
    """The min/max-content column sizer."""

    def setUp(self):
        self.fonts = m._default_table_fonts()
        self.available = m._default_table_content_em()

    def _table(self, html: str):
        return BeautifulSoup(html, "html.parser").table

    def _widths_em(self, table) -> list[float]:
        cols = table.find("colgroup").find_all("col")
        percents = [
            float(col["style"].split(":")[1].strip().rstrip("%")) for col in cols
        ]
        self.assertAlmostEqual(sum(percents), 100.0, places=2)
        return [self.available * pct / 100.0 for pct in percents]

    def test_default_a4_content_width(self):
        self.assertAlmostEqual(self.available, 66.14, places=1)

    def test_room_to_spare_fills_the_available_width(self):
        widths = m.compute_column_widths([(4.0, 6.0), (8.0, 10.0)], 60.0)
        self.assertAlmostEqual(sum(widths), 60.0, places=6)

    def test_tight_table_never_goes_below_min_content(self):
        metrics = [(20.0, 90.0), (5.0, 40.0), (3.0, 10.0)]
        widths = m.compute_column_widths(metrics, 40.0)
        for width, (low, _) in zip(widths, metrics):
            self.assertGreaterEqual(width, low + m._TABLE_PADDING_EM - 1e-9)

    def test_min_content_scales_down_when_nothing_fits(self):
        widths = m.compute_column_widths([(30.0, 30.0), (30.0, 30.0)], 10.0)
        self.assertAlmostEqual(sum(widths), 10.0, places=6)
        self.assertAlmostEqual(widths[0], widths[1], places=6)

    def test_percentages_include_padding_and_respect_limits(self):
        percents = m._to_percentages([1.0, 1.0, 60.0])
        self.assertEqual(round(sum(percents), 2), 100.0)
        for pct in percents:
            self.assertGreaterEqual(pct, m._TABLE_MIN_COLUMN_PCT)
            self.assertLessEqual(pct, m._TABLE_MAX_COLUMN_PCT)

    def test_percentages_sum_to_exactly_one_hundred(self):
        percents = m._to_percentages([1.0, 2.0, 3.0, 20.0])
        self.assertEqual(round(sum(percents), 2), 100.0)

    def test_single_column_fills_the_table(self):
        self.assertEqual(m._to_percentages([3.0]), [100.0])

    def test_many_columns_still_add_up(self):
        # 30 columns cannot all respect the 2.5% floor: the total must win.
        percents = m._to_percentages([1.0] * 30)
        self.assertEqual(round(sum(percents), 2), 100.0)
        self.assertEqual(len(percents), 30)

    def test_bound_enforcement_preserves_the_total(self):
        for values in ([1.0, 1.0, 60.0], [50.0, 1.0, 1.0], [12.0, 1.0, 1.0]):
            with self.subTest(values=values):
                scaled = [100.0 * v / sum(values) for v in values]
                floored = m._enforce_minimum(
                    scaled, m._TABLE_MIN_COLUMN_PCT
                )
                self.assertAlmostEqual(sum(floored), 100.0, places=6)
                capped = m._enforce_maximum(floored, m._TABLE_MAX_COLUMN_PCT)
                self.assertAlmostEqual(sum(capped), 100.0, places=6)

    def test_unbreakable_token_always_fits_its_column(self):
        table = self._table(
            "<table><tr><th>ID</th><th>Description</th></tr>"
            "<tr><td>VD-HS-01</td><td>A long description that can wrap "
            "across several lines quite happily.</td></tr></table>"
        )
        m.set_table_column_widths(table.parent, self.available, self.fonts)
        table = self._table(str(table))
        width, _ = self._widths_em(table)
        metrics = m._cell_metrics_em(table.find("td"), self.fonts)
        self.assertGreaterEqual(width, metrics[0] + m._TABLE_PADDING_EM - 0.01)
        self.assertIn("mdtp-nowrap", table.find("td").get("class", []))

    def test_numeric_column_is_right_aligned(self):
        table = self._table(
            "<table><tr><th>Item</th><th>Value</th></tr>"
            "<tr><td>Voltage</td><td>12.5</td></tr>"
            "<tr><td>Current</td><td>1,204.75</td></tr></table>"
        )
        m.set_table_column_widths(table.parent, self.available, self.fonts)
        table = self._table(str(table))
        cells = table.find_all("tr")[1].find_all("td")
        self.assertNotIn("mdtp-num", cells[0].get("class", []))
        self.assertIn("mdtp-num", cells[1].get("class", []))
        header = table.find("th", string="Value")
        self.assertIn("mdtp-num", header.get("class", []))

    def test_text_column_is_not_right_aligned(self):
        table = self._table(
            "<table><tr><th>Item</th><th>Pass</th></tr>"
            "<tr><td>Voltage</td><td>Yes</td></tr>"
            "<tr><td>Current</td><td>No</td></tr></table>"
        )
        m.set_table_column_widths(table.parent, self.available, self.fonts)
        table = self._table(str(table))
        for cell in table.find_all("tr")[1].find_all("td"):
            self.assertNotIn("mdtp-num", cell.get("class", []))

    def test_colspan_is_accounted_for(self):
        table = self._table(
            '<table><tr><th colspan="2">Group</th><th>C</th></tr>'
            "<tr><td>a</td><td>bbbbbbbbbb</td><td>c</td></tr></table>"
        )
        m.set_table_column_widths(table.parent, self.available, self.fonts)
        table = self._table(str(table))
        self.assertEqual(len(table.find("colgroup").find_all("col")), 3)

    def test_author_supplied_colgroup_is_respected(self):
        table = self._table(
            '<table><colgroup><col style="width: 90%"><col style="width: 10%">'
            "</colgroup><tr><td>a</td><td>b</td></tr></table>"
        )
        m.set_table_column_widths(table.parent, self.available, self.fonts)
        cols = self._table(str(table)).find("colgroup").find_all("col")
        self.assertEqual(
            [c["style"] for c in cols], ["width: 90%", "width: 10%"]
        )

    def test_no_column_is_narrower_than_its_longest_token(self):
        """The core promise: no ID or URL ever has to be broken in half."""
        markdown = (
            "| A | B |\n|---|---|\n| VD-HS-01 | A long description that "
            "would like to wrap but must not force the ID to break |\n\n"
            "| # | Parameter | Symbol | Unit | Min | Typ | Max | Note |\n"
            "|---|-----------|--------|------|-----|-----|-----|------|\n"
            "| 1 | Supply voltage | V_CC | V | 11.4 | 12.0 | 12.6 | Reg |\n"
            "| 2 | Operating temperature | T_op | °C | -40 | 25 | 85 | Load |\n"
            "| 3 | Serial number | SN | - | - | - | - | SN-000-12345 |\n\n"
            "| Component | Spec |\n|-----------|------|\n"
            "| `hdl_parser` | `--strict --jobs=8` |\n"
            "| 中文测试 | 中文列内容也应该正确测量宽度 |\n\n"
            "| Long token | Value |\n|------------|-------|\n"
            "| https://example.com/a/very/long/path/that/keeps/going/on | 1 |\n\n"
            "| Single |\n|--------|\n| only one column |\n"
        )
        body = _render(markdown)
        tables = body.find_all("table")
        self.assertEqual(len(tables), 5)
        for index, table in enumerate(tables):
            widths = self._widths_em(table)
            ncols = len(widths)
            mins = [0.0] * ncols
            for row in table.find_all("tr"):
                column = 0
                for cell in row.find_all(["th", "td"]):
                    low, _ = m._cell_metrics_em(cell, self.fonts)
                    span = m._colspan(cell)
                    for slot in range(column, min(column + span, ncols)):
                        mins[slot] = max(mins[slot], low / span)
                    column += span
            for slot, width in enumerate(widths):
                with self.subTest(table=index, column=slot):
                    self.assertGreaterEqual(
                        width,
                        mins[slot] + m._TABLE_PADDING_EM - 0.01,
                    )

    def test_every_table_row_adds_up_to_one_hundred_percent(self):
        body = _render(
            "| A | B | C |\n|---|---|---|\n| 1 | 2 | 3 |\n\n"
            "| Only |\n|------|\n| x |\n"
        )
        for table in body.find_all("table"):
            percents = [
                float(c["style"].split(":")[1].strip().rstrip("%"))
                for c in table.find("colgroup").find_all("col")
            ]
            self.assertEqual(round(sum(percents), 2), 100.0)


class TextMetricTests(unittest.TestCase):
    """Character width measurement used by the column sizer."""

    def test_numeric_detection(self):
        for text in ("1", "12.5", "1,204.75", "-5", "(1,234)", "85 °C",
                     "5%", "12 ms", "$1,000"):
            self.assertTrue(m._is_numeric(text), text)
        for text in ("", "Yes", "No", "n/a", "VD-HS-01", "1 of 3", "TBD"):
            self.assertFalse(m._is_numeric(text), text)

    def test_first_font_family(self):
        self.assertEqual(
            m._first_font_family("'DejaVu Sans', 'Noto Sans', sans-serif"),
            "DejaVu Sans",
        )
        self.assertEqual(m._first_font_family("serif"), "serif")

    def test_metrics_measure_the_real_font(self):
        metrics = m.TextMetrics("DejaVu Sans")
        if not metrics._advances:
            self.skipTest("fontconfig/fontTools unavailable")
        width = sum(metrics.width(ch) for ch in "VD-HS-01")
        self.assertAlmostEqual(width, 4.8, delta=0.4)

    def test_fallback_estimate_covers_wide_characters(self):
        self.assertGreater(m._char_width_em("中", False), 0.9)

    def test_page_geometry_helpers(self):
        self.assertAlmostEqual(m._length_mm("1in"), 25.4, places=4)
        self.assertAlmostEqual(m._length_mm("10mm"), 10.0, places=4)
        self.assertAlmostEqual(
            m.table_content_em("A4", "16mm 14mm 18mm 14mm", "7.8pt"),
            66.14,
            delta=0.5,
        )
        # Letter is wider than A4, so it must fit more em.
        self.assertGreater(
            m.table_content_em("Letter", "16mm 14mm 18mm 14mm", "7.8pt"),
            m.table_content_em("A4", "16mm 14mm 18mm 14mm", "7.8pt"),
        )


class MarkdownExtensionTests(unittest.TestCase):
    """The enabled extension set and the GFM extras."""

    def test_strikethrough(self):
        body = _render("~~gone~~\n")
        self.assertIsNotNone(body.find("del"))

    def test_task_list_uses_ballot_boxes(self):
        body = _render("- [x] done\n- [ ] todo\n")
        boxes = body.select(".task-list-item-checkbox")
        self.assertEqual([b.text for b in boxes], ["\u2611", "\u2610"])
        self.assertIsNotNone(body.select_one("ul.task-list"))

    def test_mixed_list_keeps_bullets_on_plain_items(self):
        body = _render("- plain\n- [x] task\n")
        self.assertIsNone(body.select_one("ul.task-list"))
        self.assertEqual(len(body.select(".task-list-item")), 1)

    def test_task_list_with_nested_items(self):
        body = _render("- [ ] parent\n  - [x] child\n")
        self.assertEqual(len(body.select(".task-list-item-checkbox")), 2)
        self.assertIsNotNone(body.select_one("li.task-list-item > ul > li"))

    def test_footnotes(self):
        body = _render("Text[^1]\n\n[^1]: Note\n")
        self.assertIsNotNone(body.select_one("a.footnote-ref"))
        self.assertIsNotNone(body.select_one("div.footnote"))

    def test_definition_list(self):
        body = _render("Term\n:   Definition\n")
        self.assertIsNotNone(body.select_one("dl > dt"))
        self.assertIsNotNone(body.select_one("dl > dd"))

    def test_admonition(self):
        body = _render('!!! warning "Careful"\n    Body\n')
        self.assertIsNotNone(body.select_one("div.admonition.warning"))

    def test_abbreviation(self):
        body = _render("*[HTML]: Hyper Text Markup Language\n\nHTML\n")
        self.assertEqual(body.find("abbr")["title"], "Hyper Text Markup Language")

    def test_table_of_contents_gets_anchors(self):
        body = _render("[TOC]\n\n## Section\n")
        self.assertIsNotNone(body.select_one("div.toc a[href='#section']"))
        self.assertIsNotNone(body.select_one("h2#section"))

    def test_smarty_typography_can_be_disabled(self):
        options = m.RenderOptions(
            markdown=m.build_markdown_setup(remove=["smarty"], highlight=False)
        )
        body = _render('"quoted"\n', options=options)
        self.assertIn('"quoted"', body.get_text())

    def test_default_extensions_include_the_basics(self):
        setup = m.build_markdown_setup(highlight=False)
        for name in ("extra", "sane_lists", "toc", "admonition"):
            self.assertIn(name, setup.extensions)

    def test_unknown_extension_is_rejected_early(self):
        with self.assertRaises(Exception):
            m.build_markdown_setup(add=["definitely-not-an-extension"])

    def test_extension_can_be_disabled(self):
        setup = m.build_markdown_setup(remove=["toc", "extra"], highlight=False)
        self.assertNotIn("toc", setup.extensions)
        self.assertNotIn("extra", setup.extensions)

    def test_highlighting_follows_pygments_availability(self):
        setup = m.build_markdown_setup()
        self.assertEqual(setup.highlighting, m._PygmentsFormatter is not None)

    def test_fenced_code_renders_without_pygments(self):
        setup = m.build_markdown_setup(highlight=False)
        body = _render(
            "```python\nx = 1\n```\n",
            options=m.RenderOptions(markdown=setup),
        )
        self.assertIsNotNone(body.find("pre"))
        self.assertIn("x = 1", body.get_text())


class ThemeTests(unittest.TestCase):
    """Theme registry and CSS generation."""

    def test_every_theme_builds_a_complete_stylesheet(self):
        for name in m.available_themes():
            css = m.build_css(name)
            self.assertIn("@page", css)
            self.assertIn("counter(page)", css)
            self.assertIn("@bottom-left", css)

    def test_theme_tokens_are_complete(self):
        base = set(m._BASE_TOKENS)
        for name in m.available_themes():
            tokens = m.theme_tokens(name)
            self.assertTrue(base <= set(tokens), name)
            for value in tokens.values():
                self.assertIsInstance(value, str)

    def test_theme_names_are_documented(self):
        for name in m.available_themes():
            self.assertTrue(m._THEME_DESCRIPTIONS.get(name))

    def test_footer_can_be_switched_off(self):
        css = m.build_css("classic", show_footer=False)
        self.assertNotIn("@bottom-left", css)

    def test_footer_text_is_escaped(self):
        css = m.build_css("classic", footer_left='a "quoted" \\ text')
        self.assertIn('content: "a \\"quoted\\" \\\\ text";', css)

    def test_overrides_win_over_theme_defaults(self):
        css = m.build_css("classic", page_size="Letter", body_size="11pt")
        self.assertIn("size: Letter;", css)
        self.assertIn("font-size: 11pt;", css)

    def test_extra_css_is_appended_last(self):
        css = m.build_css("classic", extra_css="/* sentinel */")
        self.assertTrue(css.rstrip().endswith("/* sentinel */"))

    def test_unknown_theme_raises(self):
        with self.assertRaises(KeyError):
            m.build_css("does-not-exist")

    def test_every_theme_resolves_table_fonts(self):
        for name in m.available_themes():
            options = m.RenderOptions(theme=name)
            self.assertIsInstance(options.table_fonts(), m.FontPair)
            self.assertGreater(options.table_content_em(), 40.0)


class TitleTests(unittest.TestCase):
    """The ``<title>`` picked out of the document."""

    def test_first_h1_wins(self):
        self.assertEqual(m.extract_title("# Hello\n", "fallback"), "Hello")

    def test_markup_is_stripped(self):
        self.assertEqual(m.extract_title("# **Bold** `code`\n", "f"), "Bold code")

    def test_fenced_code_is_ignored(self):
        md = "```\n# not a title\n```\n\n# Real title\n"
        self.assertEqual(m.extract_title(md, "fallback"), "Real title")

    def test_fallback_is_used(self):
        self.assertEqual(m.extract_title("No heading\n", "fallback"), "fallback")


class MathTests(unittest.TestCase):
    """Math scanning, without needing a LaTeX installation."""

    def test_math_is_skipped_in_code(self):
        stripped, snippets = m.extract_math("`$a$` and $b$\n\n```\n$c$\n```\n")
        self.assertEqual(len(snippets), 1)
        self.assertIn("$a$", stripped)

    def test_escaped_dollar_is_literal(self):
        body = _render(r"Price: \$5 and \$10" + "\n")
        self.assertIn("$5 and $10", body.get_text())

    def test_math_is_left_alone_when_disabled(self):
        body = _render(
            "$$\nx = 1\n$$\n", math_cfg=m.MathConfig(enabled=False)
        )
        self.assertIn("$$", body.get_text())


class EndToEndTests(unittest.TestCase):
    """One conversion through WeasyPrint, to prove the pipeline still runs."""

    def test_pdf_is_written_and_styled(self):
        source = (
            "# Title\n\n[TOC]\n\n## Section\n\n"
            "1. one\n2. two\n   1. nested\n\n"
            "| ID | Value |\n|----|-------|\n| VD-01 | 12.5 |\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            md_path = tmp_path / "doc.md"
            md_path.write_text(source, encoding="utf-8")
            out_path = tmp_path / "doc.pdf"
            options = m.RenderOptions(
                markdown=m.build_markdown_setup(highlight=False)
            )
            m.render_one(
                md_path,
                tmp_path,
                output=out_path,
                keep_html=True,
                math_cfg=m.MathConfig(enabled=False),
                options=options,
            )
            self.assertTrue(out_path.is_file())
            self.assertTrue(out_path.read_bytes().startswith(b"%PDF"))
            self.assertGreater(out_path.stat().st_size, 2000)
            html = (tmp_path / "doc.html").read_text(encoding="utf-8")
            self.assertIn("<colgroup>", html)
            self.assertIn("mdtp-num", html)


class NestedCodeBlockTests(unittest.TestCase):
    """Fenced code inside a list item or a blockquote.

    Python-Markdown's ``fenced_code`` only runs on top-level blocks, so without
    the extraction step these blocks come out as inline code spans or escape the
    list entirely.
    """

    def test_fence_under_list_item_two_space_indent(self):
        body = _render("- item\n\n  ```python\n  x = 1\n  ```\n")
        pre = body.select_one("li > div.codehilite pre")
        self.assertIsNotNone(pre)
        self.assertIn("x = 1", pre.get_text())

    def test_fence_under_list_item_four_space_indent(self):
        body = _render("- item\n\n    ```python\n    x = 1\n    ```\n")
        self.assertIsNotNone(body.select_one("li > div.codehilite pre"))

    def test_fence_under_ordered_list_item(self):
        md = "1. Install:\n\n   ```bash\n   pip install x\n   ```\n\n2. Run.\n"
        body = _render(md)
        self.assertIsNotNone(body.select_one("ol > li div.codehilite pre"))
        self.assertEqual(len(body.select("ol > li")), 2)

    def test_fence_inside_blockquote(self):
        body = _render("> ```python\n> x = 1\n> ```\n")
        self.assertIsNotNone(body.select_one("blockquote div.codehilite pre"))

    def test_fence_under_nested_list_item(self):
        md = "- a\n  - b\n\n    ```py\n    x = 1\n    ```\n"
        body = _render(md)
        self.assertIsNotNone(body.select_one("li > ul > li div.codehilite pre"))

    def test_list_nesting_survives_a_nested_code_block(self):
        md = "- item\n\n  ```py\n  x = 1\n  ```\n\n  - still nested\n"
        body = _render(md)
        self.assertIsNotNone(body.select_one("li > div.codehilite pre"))
        self.assertIsNotNone(body.select_one("li > ul > li"))
        self.assertIn("still nested", body.get_text())

    def test_top_level_fence_is_untouched(self):
        body = _render("Text.\n\n```python\nx = 1\n```\n")
        self.assertIsNone(body.select_one("li pre"))
        self.assertIsNotNone(body.select_one("body > div.codehilite pre"))

    def test_extraction_leaves_other_fences_alone(self):
        md = "Text.\n\n```python\nx = 1\n```\n\n> quoted\n"
        text, fences = m.extract_nested_fences(md)
        self.assertEqual(fences, [])
        self.assertEqual(text, md)

    def test_indented_code_block_after_a_paragraph_is_untouched(self):
        md = "Text.\n\n    ```\n    x = 1\n    ```\n"
        text, fences = m.extract_nested_fences(md)
        self.assertEqual(fences, [])
        self.assertEqual(text, md)

    def test_highlighting_is_kept_for_nested_fences(self):
        body = _render("- item\n\n  ```python\n  def f():\n      pass\n  ```\n")
        pre = body.select_one("li pre")
        self.assertIsNotNone(pre)
        if m._PygmentsFormatter is None:
            self.skipTest("Pygments is not installed")
        self.assertIsNotNone(pre.find("span"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
