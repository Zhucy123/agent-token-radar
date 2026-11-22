"""Tests for the section jump bar.

Pins the properties that were verified by hand in the browser but that nothing
guarded: the links are real anchors with live hrefs, the labels are translated,
and the data-driven config stays in sync with the section headings it targets.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "agent_token_radar" / "static" / "app.js"
INDEX_HTML = ROOT / "agent_token_radar" / "static" / "index.html"
APP_CSS = ROOT / "agent_token_radar" / "static" / "app.css"
NODE = shutil.which("node")

sys.path.insert(0, str(ROOT))
from agent_token_radar.i18n import STRINGS  # noqa: E402


def _js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _css() -> str:
    return APP_CSS.read_text(encoding="utf-8")


def _nav_sections() -> list[tuple[str, str]]:
    """Parse NAV_SECTIONS out of app.js: [(id, i18n key), ...]."""
    import re

    block = re.search(r"const NAV_SECTIONS = \[(.*?)\n  \];", _js(), re.S)
    assert block, "NAV_SECTIONS not found in app.js"
    return re.findall(r'\{\s*id:\s*"([^"]+)",\s*key:\s*"([^"]+)"\s*\}',
                      block.group(1))


class SectionNavMarkupTests(unittest.TestCase):
    def test_nav_container_exists_before_main_content(self) -> None:
        html = _html()
        self.assertIn('id="sectionNav"', html)
        self.assertLess(html.index('id="sectionNav"'),
                        html.index('id="cards-title"'),
                        "the jump bar must precede the first section")

    def test_nav_has_an_accessible_name(self) -> None:
        html = _html()
        self.assertIn("data-i18n-aria=\"nav.sections\"", html,
                      "a <nav> needs a label to be useful in a landmark list")

    def test_links_are_real_anchors(self) -> None:
        """A div with onclick would lose native anchor jump, right-click copy
        and Tab focus."""
        js = _js()
        block = js[js.index("function renderSectionNav"):]
        block = block[:block.index("\n  }")]
        self.assertIn('document.createElement("a")', block)
        self.assertIn("a.href", block,
                      "the link must set href so the anchor is native")
        self.assertNotIn("onclick", block)


class SectionNavConfigTests(unittest.TestCase):
    def test_every_configured_id_is_a_real_heading(self) -> None:
        html = _html()
        for section_id, _key in _nav_sections():
            self.assertIn(f'id="{section_id}"', html,
                          f"{section_id} is not a heading id in index.html")

    def test_config_covers_every_heading(self) -> None:
        """A new section must not be silently missing from the jump bar."""
        import re

        ids = re.findall(r'<h2 id="([a-z-]+)"', _html())
        configured = {sid for sid, _ in _nav_sections()}
        missing = [i for i in ids if i not in configured]
        self.assertEqual(missing, [],
                         f"headings with no jump link: {missing}")

    def test_labels_are_translated_in_both_languages(self) -> None:
        for _sid, key in _nav_sections():
            for lang in ("zh", "en"):
                self.assertIn(key, STRINGS[lang], f"{key} missing from {lang}")
                self.assertTrue(STRINGS[lang][key].strip(),
                                f"{key} is blank in {lang}")

    def test_chinese_labels_are_short_enough_for_one_row(self) -> None:
        """Six links share the bar; an over-long label pushes the row wide."""
        for _sid, key in _nav_sections():
            self.assertLessEqual(len(STRINGS["zh"][key]), 16,
                                 f"{key} too long for the bar: {STRINGS['zh'][key]}")

    def test_chinese_labels_have_no_untranslated_sentences(self) -> None:
        import re

        # Only nouns that the user's rule keeps in English belong here: token,
        # agent, and the Agent/Model/Provider/Session family. "Top" is NOT one
        # of them -- it is an ordinary English word with a Chinese equivalent,
        # and allowing it once let "Top Session" through as a fully English
        # label in Chinese mode. A label must carry Chinese of its own.
        allowed = {"Agent", "Model", "Session", "Token", "Provider"}
        for _sid, key in _nav_sections():
            value = STRINGS["zh"][key]
            words = re.findall(r"[A-Za-z]{3,}", value)
            leaked = [w for w in words if w not in allowed]
            self.assertEqual(leaked, [],
                             f"{key} leaks English: {leaked}")
            # Being made only of permitted nouns is still not a translation.
            self.assertTrue(
                re.search(r"[\u4e00-\u9fff]", value),
                f"{key} has no Chinese at all: {value!r}")


class SectionNavStyleTests(unittest.TestCase):
    def test_sticky_height_feeds_scroll_padding(self) -> None:
        """Both sticky bars must be accounted for, or a jumped-to heading lands
        underneath them."""
        css = _css()
        self.assertIn("--nav-h", css)
        self.assertIn("scroll-padding-block-start", css)
        # the padding must include both bars
        rule = css[css.index("scroll-padding-block-start"):
                   css.index("scroll-padding-block-start") + 200]
        self.assertIn("--topbar-h", rule)
        self.assertIn("--nav-h", rule)

    def test_touch_target_is_at_least_44px(self) -> None:
        css = _css()
        # Find the .section-nav a rule that sets a block size.
        import re

        block = re.search(r"\.section-nav a \{(.*?)\n  \}", css, re.S)
        self.assertIsNotNone(block, ".section-nav a rule not found")
        body = block.group(1)
        self.assertRegex(body, r"min-block-size:\s*(2\.75rem|44px|3rem)",
                         "the jump targets must be at least 44px tall")

    def test_focus_style_exists(self) -> None:
        self.assertIn(":focus-visible", _css(),
                      "the links need a visible focus indicator")

    def test_forced_colors_is_handled(self) -> None:
        self.assertIn("forced-colors", _css(),
                      "colour-only state must have a forced-colors fallback")

    def test_spy_uses_intersection_observer_not_scroll_listener(self) -> None:
        js = _js()
        self.assertIn("IntersectionObserver", js)
        spy = js[js.index("function initSectionSpy"):]
        spy = spy[:spy.index("\n  }")]
        self.assertNotIn('addEventListener("scroll"', spy,
                         "scroll-spy must not compute offsets on every scroll")

    def test_bottom_fallback_exists(self) -> None:
        """The last section may never reach the spy band on a short page."""
        js = _js()
        spy = js[js.index("function initSectionSpy"):]
        spy = spy[:spy.index("\n  }")]
        self.assertIn("bottomObserver", spy)

    def test_spy_tracks_the_full_visible_set_not_the_callback_delta(self) -> None:
        """IntersectionObserver hands the callback only CHANGED entries.

        Reading ``entries`` as "everything currently in the band" is the
        classic scroll-spy bug and it is invisible in casual testing: the
        highlight looks right while scrolling, but right after jumping to a
        section the next heading enters the band, the callback receives only
        that newcomer, and the highlight jumps ahead one section. Verified in
        the browser: clicking 消耗趋势 highlighted 按 Agent before this fix.
        """
        js = _js()
        spy = js[js.index("function initSectionSpy"):]
        spy = spy[:spy.index("\n  }")]
        # A set that accumulates state across callbacks.
        self.assertIn("inBand", spy, "must keep a running set of visible targets")
        self.assertRegex(spy, r"inBand\.add\(",
                         "must add entering targets to the running set")
        self.assertRegex(spy, r"inBand\.delete\(",
                         "must remove leaving targets from the running set")
        # And it must NOT derive the winner from the callback array alone.
        self.assertNotRegex(
            spy, r"entries\s*\.filter\([^)]*isIntersecting",
            "picking from the callback entries alone drops still-visible targets")
        # The accumulation is worthless if the callback still ends by selecting
        # from `entries`: the bug returns the moment the old two lines are
        # appended, which the assertion above alone did not catch.
        callback = spy[spy.index("new IntersectionObserver"):]
        callback = callback[:callback.index("}, {")]
        self.assertIn("pickTopmost()", callback,
                      "the callback must select from the accumulated set")
        self.assertNotRegex(
            callback, r"setCurrent\(\s*vis(ible)?\s*\[",
            "must not select the first heading of the callback batch")

    def test_spy_gap_comes_from_the_stylesheet_not_a_px_literal(self) -> None:
        """The observer band and scroll-padding must use the same gap.

        scroll-padding-block-start is ``calc(... + var(--space-4))`` and
        --space-4 is 1rem, so the real gap is 11.7px at the smallest text size
        and 20.8px at the largest. A hard-coded ``+ 4`` in the JS therefore
        drifted from where the heading actually lands.
        """
        js = _js()
        spy = js[js.index("function initSectionSpy"):]
        spy = spy[:spy.index("\n  }")]
        self.assertIn("readTokenPx", spy,
                      "the gap must be read from the token, not hard-coded")
        self.assertIn("--space-4", spy,
                      "must read the same token the stylesheet uses")
        self.assertNotRegex(
            spy, r"measureBars\(\)\s*\+\s*4\s*;",
            "a bare px literal drifted from the rem-based scroll padding")

    def test_read_token_px_actually_resolves_rem(self) -> None:
        """Run the real function: a string check would not have caught this.

        ``getPropertyValue("--space-4")`` on an *unregistered* custom property
        returns the raw token stream ``"1rem"``, not a length. The first version
        of readTokenPx did ``parseFloat(raw)``, which is **1** -- so the spy band
        sat 1px under the bars while scroll-padding used 14.4-20.8px. 220 tests
        passed, because the only guard read the source text instead of calling
        the function. This executes it and asserts the converted pixels.
        """
        if not NODE:
            self.skipTest("node not available")
        script = """
            %s
            // Mock a real browser: the raw custom property comes back as a
            // rem token, and the probe element resolves it to px.
            const ROOT_PX = 18.4;
            const document = {
              documentElement: {},
              createElement: () => ({ style: {}, setAttribute() {},
                                      remove() {} }),
              body: { appendChild() {} },
            };
            const getComputedStyle = (el) => ({
              getPropertyValue: (name) =>
                name === "--space-4" ? "1rem" : "",
              // The probe's resolved padding: 1rem at the mocked root size.
              paddingTop: el && el.style && el.style.paddingTop === "1rem"
                ? ROOT_PX + "px" : "0px",
            });
            const readTokenPx = (() => {
              %s
              return readTokenPx;
            })();
            const out = {
              remToken: readTokenPx("--space-4", 16),
              missing: readTokenPx("--nope", 16),
            };
            console.log(JSON.stringify(out));
        """
        js = _js()
        start = js.index("const readTokenPx")
        end = js.index("let lastTopbarH")
        impl = js[start:end]
        code = script % ("", impl)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(code)
            path = fh.name
        try:
            res = subprocess.run([NODE, path], capture_output=True, text=True,
                                 timeout=30)
        finally:
            Path(path).unlink(missing_ok=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        got = json.loads(res.stdout)
        self.assertEqual(
            got["remToken"], 18.4,
            'a rem token must resolve to px, not parseFloat("1rem") == 1')
        self.assertNotEqual(got["remToken"], 1,
                            "regressed to parseFloat on a unitless token")
        self.assertEqual(got["missing"], 16,
                         "an unknown token must fall back, not return NaN")

    def test_aria_labels_are_all_localised(self) -> None:
        """A landmark's accessible name is user-visible in a screen reader.

        ``aria-label="Language"`` stayed English in Chinese mode, so the one
        place the strict zh/en separation had no coverage was the accessibility
        tree: sighted users saw 语言 and screen-reader users heard "Language".
        """
        html = (ROOT / "agent_token_radar" / "static" / "index.html").read_text(
            encoding="utf-8")
        import re

        for m in re.finditer(r'aria-label="([^"]*)"', html):
            # Take the whole opening tag, not just up to the attribute: the
            # data-i18n-aria hook sits after aria-label on some elements, so a
            # cut at the match end would miss it and report a false failure.
            tag_start = html.rfind("<", 0, m.start())
            tag_end = html.find(">", m.end())
            tag = html[tag_start:tag_end]
            self.assertIn(
                "data-i18n-aria", tag,
                f"aria-label is not switched by language: {m.group(1)!r}")

    def test_language_switch_has_a_translated_group_name(self) -> None:
        html = (ROOT / "agent_token_radar" / "static" / "index.html").read_text(
            encoding="utf-8")
        self.assertIn('data-i18n-aria="toolbar.lang"', html)
        self.assertIn("toolbar.lang", STRINGS["zh"])
        self.assertIn("toolbar.lang", STRINGS["en"])

    def test_user_visible_attributes_are_localised(self) -> None:
        """title/alt show up for sighted users, so they must switch too.

        ``aria-hidden`` does not hide a native tooltip: the status dot carried
        ``title="live"`` and showed English on hover in Chinese mode while every
        aria-label around it was translated. The earlier guard only scanned
        aria-label, so this went unnoticed.
        """
        import re

        html = (ROOT / "agent_token_radar" / "static" / "index.html").read_text(
            encoding="utf-8")
        pairs = (("title", "data-i18n-title"), ("alt", "data-i18n-alt"))
        for attr, hook in pairs:
            for m in re.finditer(rf'{attr}="([^"]*)"', html):
                tag_start = html.rfind("<", 0, m.start())
                tag_end = html.find(">", m.end())
                tag = html[tag_start:tag_end]
                self.assertIn(
                    hook, tag,
                    f'{attr}="{m.group(1)}" is not switched by language')

    def test_title_hook_is_actually_applied(self) -> None:
        """Declaring the attribute means nothing without the applier."""
        js = _js()
        self.assertIn("data-i18n-title", js,
                      "applyI18n must handle data-i18n-title")
        self.assertIn("data-i18n-alt", js,
                      "applyI18n must handle data-i18n-alt")


if __name__ == "__main__":
    unittest.main()
