"""The packaged exe must carry a real Windows version resource.

The build shipped once without one: ``EXE()`` in the spec had no ``version=``
argument, so ``AgentTokenRadar.exe`` reported an empty FileVersion/ProductName
while ``Setup.exe`` correctly said 0.0.4.0. Right-click -> Properties -> Details
on the file people actually run showed nothing at all.

That matters beyond cosmetics: Windows file identification, add/remove entries
that point at the exe, and enterprise allow-listing by version all read this
resource. A blank one makes the application look unversioned and can fail a
policy check that a correct build passes.

The version number must also stay in ONE place. It is already mirrored into
pyproject.toml and the .iss by hand; this test asserts the build reads
``__version__`` rather than introducing another literal to forget.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "agent-token-radar-dir.spec"
INIT = ROOT / "agent_token_radar" / "__init__.py"

sys.path.insert(0, str(ROOT))
from agent_token_radar._make_version_info import (  # noqa: E402
    _version_tuple,
    project_version,
    version_file,
)


class VersionResourceWiringTests(unittest.TestCase):
    """The spec must actually pass the resource to EXE()."""

    def test_spec_passes_version_to_exe(self) -> None:
        spec = SPEC.read_text(encoding="utf-8")
        self.assertIn("version=VERSION_RESOURCE", spec,
                      "EXE() without version= produces a blank resource")

    def test_spec_generates_the_resource_from_the_package(self) -> None:
        """A literal in the spec would be a fourth place to forget."""
        spec = SPEC.read_text(encoding="utf-8")
        self.assertIn("make_version_info", spec)
        self.assertIn("version_file", spec)
        # The assignment that feeds EXE() must CALL the generator. Pinning only
        # "version=<literal>" misses a hard-coded `VERSION_RESOURCE = "0.0.4"`,
        # which was the first thing this test failed to catch.
        self.assertRegex(
            spec, r"VERSION_RESOURCE\s*=\s*version_file\(",
            "the resource path must come from version_file(), not a literal")

    def test_version_comes_from_the_package(self) -> None:
        self.assertEqual(project_version(),
                         self._dunder_version())

    def _dunder_version(self) -> str:
        m = re.search(r'^__version__\s*=\s*"([^"]+)"',
                      INIT.read_text(encoding="utf-8"), re.MULTILINE)
        self.assertIsNotNone(m, "__version__ not found")
        return m.group(1)


class VersionTupleTests(unittest.TestCase):
    """Windows needs exactly four 16-bit numbers; a 3-part version must pad."""

    def test_three_part_version_pads_to_four(self) -> None:
        self.assertEqual(_version_tuple("0.0.4"), (0, 0, 4, 0))

    def test_two_part_version_pads_to_four(self) -> None:
        self.assertEqual(_version_tuple("1.2"), (1, 2, 0, 0))

    def test_extra_parts_are_trimmed(self) -> None:
        self.assertEqual(_version_tuple("1.2.3.4.5"), (1, 2, 3, 4))

    def test_suffix_does_not_crash_the_build(self) -> None:
        """A prerelease tag must not fail the packaging step."""
        self.assertEqual(_version_tuple("1.2.3-rc1"), (1, 2, 3, 0))


class GeneratedResourceTests(unittest.TestCase):
    """The written file must be parseable by PyInstaller itself."""

    def test_resource_parses_and_carries_the_fields_explorer_shows(self) -> None:
        try:
            from PyInstaller.utils.win32.versioninfo import (
                load_version_info_from_text_file,
            )
        except ModuleNotFoundError:
            self.skipTest("PyInstaller is not installed")

        path = version_file()
        vi = load_version_info_from_text_file(path)

        version = project_version()
        nums = _version_tuple(version)
        # FixedFileInfo stores the four numbers as two packed DWORDs, not a
        # tuple: MS holds the first two, LS the last two, 16 bits each.
        self.assertEqual((vi.ffi.fileVersionMS, vi.ffi.fileVersionLS),
                         ((nums[0] << 16) | nums[1], (nums[2] << 16) | nums[3]))
        self.assertEqual((vi.ffi.productVersionMS, vi.ffi.productVersionLS),
                         ((nums[0] << 16) | nums[1], (nums[2] << 16) | nums[3]))

        # Collect the StringStruct values by key. Walk only StringFileInfo:
        # VarFileInfo holds plain ints (the translation codepage), not entries.
        fields: dict[str, str] = {}
        for kid in vi.kids:
            if type(kid).__name__ != "StringFileInfo":
                continue
            for table in kid.kids:
                for entry in table.kids:
                    fields[entry.name] = entry.val

        self.assertEqual(fields.get("FileVersion"), version)
        self.assertEqual(fields.get("ProductVersion"), version)
        self.assertEqual(fields.get("ProductName"), "Agent Token Radar")
        self.assertEqual(fields.get("OriginalFilename"), "AgentTokenRadar.exe")
        for key in ("CompanyName", "FileDescription", "LegalCopyright"):
            self.assertTrue(fields.get(key, "").strip(), f"{key} is blank")


if __name__ == "__main__":
    unittest.main()
