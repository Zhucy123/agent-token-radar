"""The dashboard's Settings dialog: data-driven, persisted, no first-paint flash.

Three things are easy to get wrong here and all of them are silent:

* **A setting that renders but does not persist.** The change applies to the
  live page and looks right, then the next reload reverts it and the user
  reports "it doesn't remember my choice".
* **A setting that persists but is not applied before first paint.** app.js is
  deferred, so anything it applies arrives after the browser has already
  painted; the page shows the default size for a frame and then jumps (FOUC).
  The inline <head> script exists only for this, and its table must agree with
  the one in app.js or the two will drift.
* **A setting that changes the page but not the canvas.** Canvas2D bakes its
  font at draw time, so a typeface or size change that does not trigger a
  redraw leaves the chart axis labels on the old font -- the same class of bug
  the prefers-color-scheme listener was added for.

These tests read ``app.js``/``index.html`` as text for the wiring, then run the
real ``SETTINGS`` table through Node to check the option values, the CSS custom
property each one writes, and that every option has both translations.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "agent_token_radar" / "static" / "app.js"
INDEX_HTML = ROOT / "agent_token_radar" / "static" / "index.html"
NODE = shutil.which("node")

#: Typeface names that ARE the same word in both languages: a font called
#: "Arial" is "Arial" in Chinese too. Exempt from the zh != en label rule --
#: but only these, and only because the name carries no translatable text.
#: Anything with a parenthetical description is NOT listed here, so a real
#: sentence leaking untranslated still fails.
_PROPER_NOUN_KEYS = {
    "settings.ff.arial",
    "settings.ff.times",
    "settings.ff.simhei",
    "settings.ff.simsun",
    "settings.ff.kaiti",
    "settings.ff.fangsong",
    "settings.ff.notoSans",
}


def _js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


class SettingsMarkupTests(unittest.TestCase):
    """The dialog must be a native modal, and every control needs a name."""

    def test_settings_button_opens_the_dialog(self) -> None:
        html = _html()
        self.assertIn('id="settingsDialog"', html)
        self.assertIn('commandfor="settingsDialog"', html)
        self.assertIn('command="show-modal"', html,
                      "the button must open the dialog as a modal")

    def test_button_has_an_accessible_name(self) -> None:
        """An icon-only button with no label is announced as just "button"."""
        html = _html()
        m = re.search(r'<button[^>]*id="settingsBtn"[^>]*>', html, re.S)
        self.assertIsNotNone(m, "settings button not found")
        self.assertIn("data-i18n-aria", m.group(0),
                      "icon-only trigger needs an aria-label from the string table")

    def test_dialog_is_native_and_not_reimplemented(self) -> None:
        html = _html()
        m = re.search(r"<dialog[^>]*settingsDialog[^>]*>", html)
        self.assertIsNotNone(m, "settingsDialog is not a native <dialog>")
        # The native modal already provides these; adding them is a bug.
        self.assertNotIn('role="dialog"', m.group(0))
        self.assertNotIn("aria-modal", m.group(0))

    def test_dialog_has_a_title_reference(self) -> None:
        html = _html()
        m = re.search(r"<dialog[^>]*settingsDialog[^>]*>", html)
        self.assertIn("aria-labelledby", m.group(0),
                      "a dialog needs an accessible name from its heading")

    def test_body_is_a_container_not_hard_coded_fields(self) -> None:
        """Adding a setting must be a config entry, not new markup."""
        html = _html()
        self.assertIn('id="settingsBody"', html)
        # No setting-specific markup should exist in the HTML at all.
        self.assertNotIn('data-setting="fs"', html)
        self.assertNotIn('data-setting="ff"', html)


class SettingsInlineBootstrapTests(unittest.TestCase):
    """The <head> script is what prevents the first-paint flash."""

    def test_inline_script_exists_before_the_deferred_bundle(self) -> None:
        html = _html()
        inline = html.find("atr.fs")
        deferred = html.find('src="/static/app.js"')
        self.assertGreater(inline, -1, "no inline bootstrap reading atr.fs")
        self.assertGreater(deferred, -1)
        self.assertLess(inline, deferred,
                        "the inline script must come before the deferred app.js, "
                        "or the first paint shows the default size")

    def test_bootstrap_matches_the_js_table(self) -> None:
        """Two copies of the option table; drift means the flash comes back."""
        html = _html()
        js = _js()

        html_fs = re.search(r"var FS = \{(.*?)\};", html, re.S)
        self.assertIsNotNone(html_fs, "FS map missing from the inline script")
        html_sizes = dict(re.findall(r"(\w+):\s*(\d+)", html_fs.group(1)))

        js_sizes = dict(re.findall(
            r"\{ value: \"(\w+)\", pct: (\d+)", js))
        self.assertEqual(html_sizes, js_sizes,
                         "the inline FS map and SETTINGS disagree; the page would "
                         "render one size and settle on another")

        # Same for the typeface stacks: compare the option values, not the long
        # CSS strings (which are allowed to differ in quoting).
        html_ff = re.search(r"var FF = \{(.*?)\};", html, re.S)
        self.assertIsNotNone(html_ff, "FF map missing from the inline script")
        html_faces = set(re.findall(r"^\s*(\w+):\s*'", html_ff.group(1), re.M))
        js_faces = set(re.findall(r'value: "(\w+)",\s*\n\s*label: "settings\.ff\.', js))
        self.assertEqual(html_faces, js_faces,
                         "the inline FF map and SETTINGS offer different typefaces")


class SettingsConfigTests(unittest.TestCase):
    """Static checks on the SETTINGS table and its consumers."""

    def test_settings_are_data_driven(self) -> None:
        js = _js()
        self.assertIn("const SETTINGS = [", js)
        self.assertIn("for (const s of SETTINGS)", js,
                      "apply/read/reset must walk the config, not hardcode keys")

    def test_every_setting_declares_storage_and_apply(self) -> None:
        js = _js()
        for key in ("key:", "options:", "fallback:", "apply("):
            self.assertIn(key, js, f"SETTINGS entries must declare {key}")

    def test_changing_a_setting_redraws_the_chart(self) -> None:
        """Canvas bakes its font; a CSS-only change would leave it stale."""
        js = _js()
        m = re.search(r"function applySetting\(setting, value\) \{(.*?)\n  \}",
                      js, re.S)
        self.assertIsNotNone(m, "applySetting not found")
        self.assertIn("renderChart", m.group(1),
                      "applySetting must redraw the canvas chart")

    def test_chart_reads_the_font_token(self) -> None:
        js = _js()
        self.assertIn("--font-ui", js,
                      "the canvas font must follow the chosen typeface")
        self.assertNotIn('ctx.font = "0.6875rem system-ui', js,
                         "a hardcoded chart font would ignore the setting")


@unittest.skipIf(NODE is None, "node not available")
class SettingsBehaviourTests(unittest.TestCase):
    """Run the real SETTINGS table through Node against a small DOM stub."""

    def _run(self, expr: str) -> object:
        script = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
function extract(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`${name} not found`);
  let i = src.indexOf("{", start), depth = 0;
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}
// Pull the SETTINGS literal out verbatim so its apply() closures come along.
const sStart = src.indexOf("const SETTINGS = [");
let i = src.indexOf("[", sStart), depth = 0, sEnd = -1;
for (; i < src.length; i++) {
  if (src[i] === "[") depth++;
  else if (src[i] === "]") { depth--; if (depth === 0) { sEnd = i + 1; break; } }
}
const settingsLiteral = src.slice(src.indexOf("[", sStart), sEnd);

const root = {
  style: {
    _p: {},
    set fontSize(v) { this._fs = v; },
    get fontSize() { return this._fs; },
    setProperty(k, v) { this._p[k] = v; },
    getPropertyValue(k) { return this._p[k] || ""; },
    // The text-colour "default" and the outline's "off" step REMOVE the
    // property so the theme default takes over again; a stub without this made
    // both settings throw.
    removeProperty(k) { const v = this._p[k]; delete this._p[k]; return v || ""; },
  },
  dataset: {},
};
const document = { documentElement: root, querySelector: () => null };
const state = { trend: null };
let chartCalls = 0;
function renderChart() { chartCalls++; }

const SETTINGS = new Function("document", "state", "renderChart",
  `return ${settingsLiteral};`)(document, state, renderChart);

""" + expr

        with tempfile.TemporaryDirectory(prefix="atr_settings_") as tmp:
            path = Path(tmp) / "probe.js"
            path.write_text(script, encoding="utf-8")
            out = subprocess.run(
                [NODE, str(path), str(APP_JS)],
                capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
        if out.returncode != 0:
            raise AssertionError(f"node failed:\n{out.stderr}\n{out.stdout}")
        return json.loads(out.stdout)

    def test_font_size_options_and_percentages(self) -> None:
        got = self._run("""
const s = SETTINGS.find(x => x.key === "fs");
console.log(JSON.stringify({
  values: s.options.map(o => o.value),
  pcts: s.options.map(o => o.pct),
  fallback: s.fallback,
}));
""")
        self.assertEqual(got["values"], ["small", "normal", "large", "xlarge"])
        self.assertEqual(got["pcts"], [90, 100, 115, 130])
        self.assertEqual(got["fallback"], "normal",
                         "the default must be today's look, not a different size")
        self.assertEqual(got["pcts"][got["values"].index(got["fallback"])], 100,
                         "the fallback must map to 100% so nothing changes by default")

    def test_font_size_apply_writes_root_size(self) -> None:
        got = self._run("""
const s = SETTINGS.find(x => x.key === "fs");
const out = {};
for (const o of s.options) {
  s.apply.call(s, o.value);
  out[o.value] = { style: document.documentElement.style.fontSize,
                   attr: document.documentElement.dataset.fs };
}
console.log(JSON.stringify(out));
""")
        self.assertEqual(got["xlarge"]["style"], "130%")
        self.assertEqual(got["xlarge"]["attr"], "xlarge")
        self.assertEqual(got["normal"]["style"], "100%")
        self.assertEqual(got["small"]["style"], "90%")

    def test_unknown_value_falls_back_instead_of_blanking(self) -> None:
        """A stale or hand-edited localStorage value must not break the page."""
        got = self._run("""
const s = SETTINGS.find(x => x.key === "fs");
s.apply.call(s, "nonsense");
console.log(JSON.stringify({style: document.documentElement.style.fontSize}));
""")
        self.assertEqual(got["style"], "100%",
                         "an unknown value must fall back to the default size")

    def test_typefaces_are_installed_only_and_end_in_a_generic(self) -> None:
        got = self._run("""
const s = SETTINGS.find(x => x.key === "ff");
console.log(JSON.stringify(s.options.map(o => ({v: o.value, stack: o.stack}))));
""")
        values = [o["v"] for o in got]
        # The list grew deliberately; spot-check the shape rather than pinning
        # every value, so adding a face does not require editing this test.
        for required in ("system", "segoe", "georgia", "yahei", "simsun",
                         "kaiti", "cascadia"):
            self.assertIn(required, values)
        self.assertGreaterEqual(len(values), 20, "expected a broad typeface list")
        self.assertEqual(len(values), len(set(values)), "duplicate option value")
        for opt in got:
            stack = opt["stack"]
            self.assertTrue(
                stack.rstrip().endswith(("sans-serif", "serif", "monospace")),
                f"{opt['v']} must end in a generic family: {stack}")
            # No webfonts: the project makes no network requests.
            self.assertNotIn("url(", stack)
            self.assertNotIn("http", stack)
            # Anti-tofu: every stack names a CJK-capable face before the generic,
            # so Chinese cannot fall through to whatever the browser picks.
            self.assertTrue(
                any(cjk in stack for cjk in
                    ("YaHei", "SimSun", "SimHei", "KaiTi", "FangSong",
                     "DengXian", "Noto Sans SC")),
                f"{opt['v']} has no CJK fallback: {stack}")

    def test_every_typeface_declares_a_group(self) -> None:
        """The dropdown is grouped; an option with no group would land in the
        previous <optgroup> and appear under the wrong heading."""
        got = self._run("""
const s = SETTINGS.find(x => x.key === "ff");
console.log(JSON.stringify(s.options.map(o => ({v: o.value, g: o.group || null}))));
""")
        missing = [o["v"] for o in got if not o["g"]]
        self.assertEqual(missing, [], "options without a group")

    def test_typeface_groups_are_all_translated(self) -> None:
        from agent_token_radar.i18n import STRINGS

        got = self._run("""
const s = SETTINGS.find(x => x.key === "ff");
console.log(JSON.stringify([...new Set(s.options.map(o => o.group))]));
""")
        for lang, table in STRINGS.items():
            for key in got:
                self.assertIn(key, table, f"missing {key} in {lang}")

    def test_typeface_uses_a_dropdown_control(self) -> None:
        """19 faces cannot be a segmented row; the config must ask for a select."""
        got = self._run("""
const s = SETTINGS.find(x => x.key === "ff");
console.log(JSON.stringify({type: s.type, count: s.options.length}));
""")
        self.assertEqual(got["type"], "select")
        self.assertGreater(got["count"], 8,
                           "a long list must select, not segment")

    def test_typeface_apply_writes_the_font_token(self) -> None:
        got = self._run("""
const s = SETTINGS.find(x => x.key === "ff");
const out = {};
for (const o of s.options) {
  s.apply.call(s, o.value);
  out[o.value] = document.documentElement.style.getPropertyValue("--font-ui");
}
console.log(JSON.stringify(out));
""")
        for value, token in got.items():
            self.assertTrue(token, f"{value} did not write --font-ui")
        self.assertIn("monospace", got["cascadia"])
        self.assertIn("Georgia", got["georgia"])

    # ---- text style: weight, italic, colour, outline -------------------

    def test_every_font_weight_in_the_stylesheet_is_scaled(self) -> None:
        """The multiplier only works if NOTHING sets a bare weight.

        A single ont-weight: 600 anywhere would ignore the setting on that
        element and, worse, break the ladder (that element would stop tracking
        the rest). This is the guard for that.
        """
        css = (ROOT / "agent_token_radar" / "static" / "app.css").read_text(
            encoding="utf-8")
        # Strip comments first: the explanatory text mentions font-weight: 600.
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        bare = [
            (i, line.strip())
            for i, line in enumerate(css.splitlines(), 1)
            if re.search(r"font-weight:\s*\d{3}\s*;", line)
        ]
        self.assertEqual(
            bare, [],
            "these declarations bypass --fw-scale: " + repr(bare))

    def test_body_and_cells_declare_a_weight(self) -> None:
        """400 is the implicit initial value, so a calc() on <html> never
        reaches body text or table cells unless those rules declare it."""
        css = (ROOT / "agent_token_radar" / "static" / "app.css").read_text(
            encoding="utf-8")
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        body = re.search(r"\n  body \{(.*?)\n  \}", css, re.S)
        self.assertIsNotNone(body, "body rule not found")
        self.assertIn("--fw-scale", body.group(1),
                      "body must declare a scaled weight")
        cells = re.search(r"\n  th, td \{(.*?)\n  \}", css, re.S)
        self.assertIsNotNone(cells, "th, td rule not found")
        self.assertIn("--fw-scale", cells.group(1),
                      "table cells must declare a scaled weight")

    def test_weight_is_a_multiplier_not_an_absolute(self) -> None:
        """A bare absolute weight would flatten the page's weight ladder.

        Every font-weight rule reads `calc(<n> * var(--fw-scale, 1))`, so the
        setting must write a scale factor. Measured in-browser: `bolder` on
        <html> leaves declared weights alone and only moves body/td (400 ->
        700), which inverts the hierarchy; a bare `600` collapsed six sampled
        elements to three distinct weights.
        """
        got = self._run("""
const s = SETTINGS.find(x => x.key === "fw");
const out = {};
for (const o of s.options) {
  s.apply.call(s, o.value);
  out[o.value] = document.documentElement.style.getPropertyValue("--fw-scale");
}
console.log(JSON.stringify(out));
""")
        for value, scale in got.items():
            self.assertTrue(scale, f"{value} did not write --fw-scale")
            # Must be a pure number: calc() multiplies by it, so "600" or
            # "bolder" here would be a category error.
            self.assertRegex(scale, r"^\d+(\.\d+)?$",
                             f"{value} scale is not a number: {scale}")
        # The scale must be ordered so the steps actually differ.
        order = ["light", "normal", "medium", "bold"]
        values = [float(got[k]) for k in order]
        self.assertEqual(values, sorted(values),
                         f"weight steps are not ordered: {values}")
        self.assertEqual(float(got["normal"]), 1.0,
                         "the default step must be exactly 1 (no change)")

    def test_weight_never_exceeds_the_css_limit(self) -> None:
        """The heaviest declared weight is 700; scale * 700 must stay <= 1000,
        or the browser clamps and the top steps become indistinguishable."""
        got = self._run("""
const s = SETTINGS.find(x => x.key === "fw");
console.log(JSON.stringify(s.options.map(o => [o.value, o.scale])));
""")
        heaviest_declared = 700
        for value, scale in got:
            product = float(scale) * heaviest_declared
            self.assertLessEqual(product, 1000.0,
                                 f"{value} would clamp at {product}")

    def test_italic_writes_the_style_token(self) -> None:
        got = self._run("""
const s = SETTINGS.find(x => x.key === "fst");
const out = {};
for (const o of s.options) {
  s.apply.call(s, o.value);
  out[o.value] = document.documentElement.style.getPropertyValue("--font-style-ui");
}
console.log(JSON.stringify(out));
""")
        self.assertEqual(got, {"normal": "normal", "italic": "italic"})

    def test_text_colour_default_clears_the_token(self) -> None:
        """Choosing "default" must REMOVE the override, not write an empty
        colour -- otherwise the page stops following dark/light mode."""
        got = self._run("""
const s = SETTINGS.find(x => x.key === "ink");
s.apply.call(s, "blue");
const afterBlue = document.documentElement.style.getPropertyValue("--ink-ui");
s.apply.call(s, "default");
const afterDefault = document.documentElement.style.getPropertyValue("--ink-ui");
console.log(JSON.stringify({afterBlue, afterDefault}));
""")
        self.assertTrue(got["afterBlue"], "blue did not set --ink-ui")
        self.assertEqual(got["afterDefault"], "",
                         "default must clear --ink-ui so the theme wins again")

    def test_every_text_colour_has_a_dark_value(self) -> None:
        """Each tone must declare light-dark(...) so it stays readable when the
        OS theme flips."""
        got = self._run("""
const s = SETTINGS.find(x => x.key === "ink");
console.log(JSON.stringify(s.options.map(o => [o.value, o.ink || ""])));
""")
        for value, ink in got:
            if value == "default":
                self.assertEqual(ink, "",
                                 "the default must not hard-code a colour")
                continue
            self.assertIn("light-dark(", ink,
                          f"{value} has no dark-mode value: {ink}")

    def test_outline_is_off_by_default_and_uses_the_tokens(self) -> None:
        got = self._run("""
const s = SETTINGS.find(x => x.key === "stroke");
const out = {};
for (const o of s.options) {
  s.apply.call(s, o.value);
  out[o.value] = {
    w: document.documentElement.style.getPropertyValue("--stroke-w"),
    c: document.documentElement.style.getPropertyValue("--stroke-c"),
  };
}
console.log(JSON.stringify(out));
""")
        self.assertEqual(got["off"]["w"], "0",
                         "the outline must be zero-width by default")
        # Widest step must stay subtle: a heavy outline blurs CJK glyphs.
        widest = max(float(v["w"].replace("px", ""))
                     for v in got.values() if v["w"].endswith("px"))
        self.assertLessEqual(widest, 1.5,
                             f"outline too heavy for CJK text: {widest}px")
        for value, v in got.items():
            if value == "off":
                continue
            self.assertTrue(v["c"], f"{value} set a width but no colour")

    def test_applying_a_setting_redraws_the_chart(self) -> None:
        """Run the shipped applySetting body, not a copy of it."""
        js = _js()
        m = re.search(r"function applySetting\(setting, value\) \{(.*?)\n  \}",
                      js, re.S)
        body = m.group(1)
        got = self._run(f"""
state.trend = {{days: [], agents: [], series: {{}}}};
function applySetting(setting, value) {{{body}
}}
applySetting(SETTINGS[0], "large");
console.log(JSON.stringify({{calls: chartCalls}}));
""")
        self.assertEqual(got["calls"], 1,
                         "changing a setting must trigger exactly one redraw")

    def test_no_redraw_when_there_is_no_chart_yet(self) -> None:
        js = _js()
        m = re.search(r"function applySetting\(setting, value\) \{(.*?)\n  \}",
                      js, re.S)
        body = m.group(1)
        got = self._run(f"""
state.trend = null;
function applySetting(setting, value) {{{body}
}}
applySetting(SETTINGS[0], "large");
console.log(JSON.stringify({{calls: chartCalls}}));
""")
        self.assertEqual(got["calls"], 0,
                         "applying settings during boot must not draw a chart "
                         "before data exists")


class SettingsTranslationTests(unittest.TestCase):
    """Chinese mode must show Chinese only; every label needs both."""

    def test_every_option_label_is_translated(self) -> None:
        import sys
        sys.path.insert(0, str(ROOT))
        from agent_token_radar.i18n import STRINGS

        js = _js()
        keys = set(re.findall(r'"(settings\.[\w.]+)"', js))
        labels = {k for k in keys
                  if k.startswith(("settings.fs.", "settings.ff."))
                  or k in ("settings.title", "settings.reset",
                           "settings.fontSize", "settings.fontFamily",
                           "settings.close", "settings.open")}
        self.assertTrue(labels, "no settings keys found in app.js")
        for key in sorted(labels):
            for lang in ("zh", "en"):
                self.assertIn(key, STRINGS[lang],
                              f"{key} missing from {lang}")
            # A typeface NAME is a proper noun and is allowed to be identical in
            # both languages ("Arial" is "Arial"). What must differ is anything
            # carrying an explanation -- a bare name has no translatable text.
            if key in _PROPER_NOUN_KEYS:
                continue
            self.assertNotEqual(STRINGS["zh"][key], STRINGS["en"][key],
                                f"{key} is identical in both languages")

    def test_proper_noun_labels_are_still_present_in_both(self) -> None:
        """The exemption above must not become a place to hide a missing key."""
        import sys
        sys.path.insert(0, str(ROOT))
        from agent_token_radar.i18n import STRINGS

        for key in _PROPER_NOUN_KEYS:
            for lang in ("zh", "en"):
                self.assertIn(key, STRINGS[lang], f"{key} missing from {lang}")
                self.assertTrue(STRINGS[lang][key].strip(),
                                f"{key} is blank in {lang}")

    def test_chinese_labels_have_no_latin_sentences(self) -> None:
        import sys
        sys.path.insert(0, str(ROOT))
        from agent_token_radar.i18n import STRINGS

        # Only proper nouns may stay Latin; a run of 4+ Latin letters in a
        # Chinese string means a sentence leaked through untranslated.
        #
        # Font family names are proper nouns by definition: "Segoe UI" is not
        # translated into Chinese, it is written in Latin because that is its
        # name. They are allowed here as single words; a phrase like "Windows
        # interface font" is not, which is why that was reworded to 系统界面字体.
        allowed = {"Agent", "Model", "Provider", "Session", "Token", "token",
                   "Segoe", "Arial", "Verdana", "Tahoma", "Trebuchet", "Calibri",
                   "Candara", "Corbel", "Georgia", "Times", "Roman", "Cambria",
                   "Palatino", "Linotype", "Noto", "Sans", "Cascadia", "Code",
                   "Consolas", "Lucida", "Console"}
        for key, value in STRINGS["zh"].items():
            if not key.startswith(("settings.", "toolbar.")):
                continue
            words = re.findall(r"[A-Za-z]{4,}", value)
            leaked = [w for w in words if w not in allowed]
            self.assertEqual(leaked, [],
                             f"{key} leaks English into Chinese mode: {leaked}")


if __name__ == "__main__":
    unittest.main()
