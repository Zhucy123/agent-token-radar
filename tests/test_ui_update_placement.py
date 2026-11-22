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

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "agent_token_radar" / "static" / "index.html"
APP_JS = ROOT / "agent_token_radar" / "static" / "app.js"
APP_CSS = ROOT / "agent_token_radar" / "static" / "app.css"

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
