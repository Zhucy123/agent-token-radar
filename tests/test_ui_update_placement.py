"""Where the update check lives, and how it is wired.

The control started inside the Settings dialog and was moved to the toolbar.
That is a placement decision the user made explicitly, so it is pinned here:
a future refactor that quietly folds it back into Settings, or that swaps the
version label for an ambiguous icon, should fail rather than ship.

Why the placement matters enough to test: Settings is a list of *preferences*
you configure, and the update state is a *fact* you glance at. It was also one
more reason to open a modal, when the answer ("you are current") is one word.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "agent_token_radar" / "static" / "index.html"
APP_JS = ROOT / "agent_token_radar" / "static" / "app.js"
APP_CSS = ROOT / "agent_token_radar" / "static" / "app.css"
NODE = shutil.which("node")

sys.path.insert(0, str(ROOT))
from agent_token_radar.i18n import STRINGS  # noqa: E402


def _html() -> str:
    return INDEX.read_text(encoding="utf-8")


def _js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _css() -> str:
    return APP_CSS.read_text(encoding="utf-8")


def _settings_dialog() -> str:
    """The <dialog id="settingsDialog"> subtree only."""
    html = _html()
    start = html.index('id="settingsDialog"')
    end = html.index("</dialog>", start)
    return html[start:end]


def _controls() -> str:
    """The toolbar's .controls block only."""
    html = _html()
    start = html.index('class="controls"')
    # The block ends at the closing </div> that balances it; the status <p>
    # follows it, which is a reliable enough terminator here.
    end = html.index('<p id="status"', start)
    return html[start:end]


class UpdatePlacementTests(unittest.TestCase):
    def test_button_is_in_the_toolbar(self) -> None:
        self.assertIn('id="updateBtn"', _controls(),
                      "the update control belongs in the toolbar")

    def test_button_is_not_in_the_settings_dialog(self) -> None:
        dialog = _settings_dialog()
        self.assertNotIn("updateBtn", dialog,
                         "the update control must not live in Settings")
        self.assertNotIn("updatePanel", dialog,
                         "the update panel must not live in Settings")

    def test_settings_dialog_has_no_update_text_at_all(self) -> None:
        """Moving the markup but leaving a stray heading would still show it."""
        dialog = _settings_dialog()
        self.assertNotIn("update.title", dialog)
        self.assertNotIn("update.privacy", dialog)

    def test_settings_still_lists_its_real_preferences(self) -> None:
        """The move must not have taken a real setting with it."""
        self.assertIn('id="settingsBody"', _settings_dialog())


class UpdateControlTests(unittest.TestCase):
    def test_control_shows_the_version_not_an_icon(self) -> None:
        """An icon here competes with Refresh and reads as refresh/download/info.

        The version string states the answer outright, which is why the button
        carries text rather than an SVG.
        """
        controls = _controls()
        start = controls.index('id="updateBtn"')
        end = controls.index("</button>", start)
        button = controls[start:end]
        self.assertIn('id="updateVersion"', button)
        self.assertNotIn("<svg", button,
                         "the update button must not be icon-only")

    def test_popover_uses_aria_expanded(self) -> None:
        controls = _controls()
        start = controls.index('id="updateBtn"')
        end = controls.index("</button>", start)
        button = controls[start:end]
        self.assertIn('aria-expanded="false"', button)
        self.assertIn('aria-controls="updatePanel"', button)

    def test_popover_starts_hidden(self) -> None:
        controls = _controls()
        start = controls.index('id="updatePanel"')
        tag_end = controls.index(">", start)
        self.assertIn("hidden", controls[start:tag_end])

    def test_toggle_and_dismissal_are_wired(self) -> None:
        js = _js()
        self.assertIn("setOpen", js)
        # Escape and outside-click are what a popover is expected to do.
        self.assertIn('"Escape"', js)
        self.assertIn("pointerdown", js)
        self.assertIn("aria-expanded", js)

    def test_badge_marks_an_available_update(self) -> None:
        js = _js()
        self.assertIn('$("updateBadge")', js)
        self.assertIn('"update_available"', js)

    def test_opening_is_what_asks_and_it_is_not_forced(self) -> None:
        """A page load must not become a network request.

        The unforced call reads the daily cache; only the explicit button inside
        the panel passes force.
        """
        js = _js()
        start = js.index("function initUpdateBox")
        # To the end of the function, not a fixed character count: the function
        # grew when the in-app update was added, and a fixed slice silently
        # started testing a prefix of it.
        end = js.index("\n  /* ---", start)
        block = js[start:end]
        self.assertIn("ask(false)", block,
                      "opening must read the cache, not force a request")
        self.assertIn("ask(true)", block,
                      "the explicit button is what forces a fresh check")
        # The unforced call is the one made when the panel opens; forcing there
        # would turn every page load into a request.
        open_handler = block[block.index('toggle.addEventListener("click"'):]
        open_handler = open_handler[:open_handler.index("});")]
        self.assertIn("ask(false)", open_handler,
                      "opening the panel must not force a network request")
        self.assertNotIn("ask(true)", open_handler,
                         "opening the panel must not force a network request")


class UpdateStyleTests(unittest.TestCase):
    def test_panel_is_absolutely_positioned_popover(self) -> None:
        css = _css()
        start = css.index(".update-panel")
        block = css[start:start + 700]
        self.assertIn("position: absolute", block)
        self.assertIn("z-index", block,
                      "a popover must sit above the content it overlaps")

    def test_panel_is_width_capped_to_the_viewport(self) -> None:
        """On a narrow window a fixed-width panel would hang off the edge."""
        css = _css()
        start = css.index(".update-panel")
        block = css[start:start + 700]
        self.assertIn("min(", block)
        self.assertIn("100vw", block)

    def test_touch_target_is_at_least_44px(self) -> None:
        css = _css()
        start = css.index("@media (pointer: coarse)")
        block = css[start:]
        self.assertIn(".update-btn", block,
                      "the toolbar button needs a 44px touch target")

    def test_available_state_is_not_colour_only(self) -> None:
        """Colour alone fails for colour-blind users and in forced-colors mode."""
        js = _js()
        self.assertIn("update.available", js,
                      "the panel must also state the update in words")


class UpdateFailureMessageTests(unittest.TestCase):
    """A failure must never render as "Update failed:" with nothing after it.

    This shipped once: the source build's POST /api/update/apply answers 409,
    api() throws on non-2xx, and the old catch block only re-enabled the button
    -- so the panel was left showing whatever it had, and the direct-branch path
    formatted an empty ``message`` into the template. A blank reason reads as
    "the app broke and will not say how", which is worse than a generic line.

    The formatter is executed here, not merely matched: a string check cannot
    tell an empty-reason fallback from a template that still renders blanks.
    """

    def _run_formatter(self, cases: list[str]) -> dict:
        """Execute the real failureText() from app.js against the given inputs."""
        if not NODE:
            self.skipTest("node not available")
        js = _js()
        start = js.index("const failureText")
        end = js.index("\n    };", start) + len("\n    };")
        impl = js[start:end]
        script = """
            const STRINGS = %s;
            const t = (key, vars) => {
              let s = STRINGS[key] || key;
              for (const [k, v] of Object.entries(vars || {})) {
                s = s.split("{" + k + "}").join(v);
              }
              return s;
            };
            %s
            const cases = %s;
            const out = cases.map((c) => failureText(c));
            console.log(JSON.stringify(out));
        """
        code = script % (
            json.dumps({"update.applyFailed": STRINGS["zh"]["update.applyFailed"],
                        "update.applyFailedNoDetail":
                            STRINGS["zh"]["update.applyFailedNoDetail"]},
                       ensure_ascii=False),
            impl,
            json.dumps(cases),
        )
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(code)
            path = fh.name
        try:
            # Node writes UTF-8; without an explicit encoding Python decodes
            # with the ANSI code page (GBK here) and the reader thread dies on
            # the Chinese text, leaving stdout as None.
            res = subprocess.run([NODE, path], capture_output=True, text=True,
                                 encoding="utf-8", timeout=30)
        finally:
            Path(path).unlink(missing_ok=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(res.stdout, f"no output from node: {res.stderr!r}")
        return json.loads(res.stdout)

    def test_empty_reason_does_not_render_a_dangling_colon(self) -> None:
        """The actual bug: ``更新失败：`` with nothing behind it."""
        out = self._run_formatter(["", None, "   "])
        for text in out:
            self.assertNotRegex(
                text, r"[：:]\s*$",
                f"a blank reason left a dangling colon: {text!r}")
            self.assertIn(STRINGS["zh"]["update.applyFailedNoDetail"], text)

    def test_a_real_reason_is_still_shown(self) -> None:
        """The fallback must not swallow the useful case."""
        out = self._run_formatter(["安装包地址不是 HTTPS，已拒绝下载"])
        self.assertIn("HTTPS", out[0])

    def test_both_apply_paths_use_the_formatter(self) -> None:
        """Polling a failed download and a rejected POST are different paths.

        Fixing only one leaves the other able to show a blank.
        """
        js = _js()
        self.assertEqual(
            js.count("failureText("), 3,
            "expected the definition plus both apply paths to call it")

    def test_a_rejected_apply_reads_the_reason_from_the_body(self) -> None:
        """A 409 must show the reason, not 'Update failed: 409 Conflict'.

        api() throws on any non-2xx and drops the JSON. The apply button now
        uses fetch so the body (reason: not_installed / no_update / ...)
        reaches failureText and can be translated.
        """
        js = _js()
        start = js.index('fetch("/api/update/apply", { method: "POST" })')
        block = js[start:start + 1200]
        self.assertIn("body.reason", block,
                      "the 409 body carries the reason code")
        self.assertIn("failureText(", block)
        self.assertIn("catch (e)", block,
                      "a network failure must still bind the error")

    def test_no_detail_message_exists_in_both_languages(self) -> None:
        for lang in ("zh", "en"):
            self.assertIn("update.applyFailedNoDetail", STRINGS[lang])
            self.assertTrue(STRINGS[lang]["update.applyFailedNoDetail"].strip())

    def test_error_codes_have_a_sentence_in_both_languages(self) -> None:
        """The codes the server now sends must not fall back to a raw id."""
        keys = [
            "update.alreadyLatest", "update.noInstaller",
            "update.errNotHttps", "update.errRedirectNotHttps",
            "update.errTooLarge", "update.errDownloadFailed",
            "update.errEmptyFile", "update.errSizeMismatch",
            "update.errMissingFile",
        ]
        for key in keys:
            for lang in ("zh", "en"):
                self.assertIn(key, STRINGS[lang], f"{key} missing from {lang}")
                self.assertTrue(STRINGS[lang][key].strip(), f"{key} blank in {lang}")
            self.assertRegex(STRINGS["zh"][key], r"[一-鿿]",
                             f"{key} has no Chinese in zh")
            self.assertNotRegex(STRINGS["en"][key], r"[一-鿿]",
                                f"{key} leaks Chinese into en")


class UpdateTranslationTests(unittest.TestCase):
    def test_every_update_key_exists_in_both_languages(self) -> None:
        keys = sorted(set(re.findall(r'"(update\.[\w.]+)"', _js() + _html())))
        self.assertTrue(keys, "no update keys referenced")
        for key in keys:
            for lang in ("zh", "en"):
                self.assertIn(key, STRINGS[lang], f"{key} missing from {lang}")
                self.assertTrue(STRINGS[lang][key].strip(),
                                f"{key} is blank in {lang}")

    def test_chinese_labels_carry_chinese(self) -> None:
        """The project rule: Chinese mode shows Chinese.

        'Token', 'Agent', 'Model', 'Session', 'GitHub', 'URL' and version
        numbers are the allowed exceptions.
        """
        allowed = re.compile(
            r"\b(Token|token|Agent|Model|Provider|Session|GitHub|Top|URL)\b")
        for key, value in STRINGS["zh"].items():
            if not key.startswith("update."):
                continue
            residue = allowed.sub("", value)
            residue = re.sub(r"\{[^}]*\}", "", residue)   # placeholders
            residue = re.sub(r"[A-Za-z]{2,}", "", residue)
            self.assertTrue(
                re.search(r"[\u4e00-\u9fff]", value),
                f"{key} has no Chinese at all: {value!r}")


if __name__ == "__main__":
    unittest.main()
