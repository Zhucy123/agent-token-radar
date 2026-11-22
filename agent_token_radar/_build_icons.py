"""Regenerate ``agent_token_radar/icons.py`` from official vendor artwork.

Why this script exists
----------------------
``icons.py`` embeds every icon so the dashboard works fully offline. It is a
GENERATED file: this script is the source of truth for where each mark comes
from, and it records that provenance in the generated header.

Source policy, in descending order of preference
------------------------------------------------
1. **simple-icons** (https://simpleicons.org, CC0). Vendors submit their own
   mark, so this is a redistributable official glyph. Fixed 24x24 viewBox and
   a single monochrome path -> emitted as ``kind="paths"``.
2. **The vendor's own site asset**: either the ``<link rel="icon">`` the site
   declares, or a logo file it serves. Used verbatim.
3. **The vendor's shipped application icon** (``.ico`` / ``.png``), when the
   site declares nothing usable.

Nothing is ever drawn by hand here. An agent with no findable official mark
must be left out of ``SPECS`` rather than approximated: the front-end already
falls back to a neutral mark and can flag it.

Output shapes consumed by ``static/app.js``
-------------------------------------------
``kind="paths"``  path data + ``viewBox`` + ``color`` (+ optional
                  ``dark_color`` / ``mode``), rendered as inline <svg>.
``kind="image"``  a base64 data URI, rendered as <img>. ``mono=True`` marks
                  single-tone line art that the CSS inverts for dark mode.
``kind="svg"``    an entire SVG document as a data URI. Needed when the mark
                  depends on gradients / clip paths, which cannot be reduced
                  to path data without a rasteriser.

Usage
-----
    python _build_icons.py            # uses _icon_cache/, downloads as needed
    python _build_icons.py --offline  # cache only (CI / no network)
    python _build_icons.py --refresh  # ignore the cache, re-download everything

Standard library only (``urllib`` + ``base64`` + ``re``). No Pillow, no
rasteriser: this must run on a bare Python 3.11 install.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "_icon_cache"
OUT = HERE / "icons.py"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 agent-token-radar/0.1"
)

# --------------------------------------------------------------------------
# Source specs
#
# Every entry answers: "which agent keys does this mark cover, and where does
# the artwork come from?"  ``aliases`` exist because several adapters are the
# same product surfaced under a different name (a regional build, a CLI vs an
# IDE). They share one mark on purpose -- one product, one logo.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SimpleIcon:
    """A simple-icons slug: monochrome 24x24 glyph, colour in the SVG."""

    slug: str
    dark_color: str | None = None  # flips to white when the mark is near-black


@dataclass(frozen=True)
class SvgPaths:
    """Single-colour paths lifted out of a vendor SVG."""

    url: str
    color: str
    view_box: str | None = None  # None -> read it from the file
    dark_color: str | None = None
    #: ``None`` keeps every path outside <defs>/<clipPath>/<mask>.
    keep_fill: str | None = None
    #: Fit the viewBox to the drawn geometry (removes vendor padding).
    tight_box: bool = False


@dataclass(frozen=True)
class SvgDocument:
    """Whole vendor SVG kept as a document (gradients, masks, clip paths, or an
    ancestor ``<g transform>`` that path extraction cannot reproduce).

    ``tiled`` puts a plate behind the mark, for vendor artwork that brings its
    own opaque background.
    """

    url: str
    #: Strip a full-bleed background rect so the mark sits on our own tile.
    drop_background: bool = True
    tiled: bool = False


@dataclass(frozen=True)
class Raster:
    """Vendor icon bitmap.

    ``tiled`` marks artwork that fills its own square with no usable alpha (a
    full-bleed plate): the stylesheet puts a matching background behind it so a
    white tile does not glare on the dark theme. It is detected automatically
    when left as ``None``.
    """

    url: str
    tiled: bool | None = None


@dataclass(frozen=True)
class Spec:
    keys: tuple[str, ...]
    label: str
    source: SimpleIcon | SvgPaths | SvgDocument | Raster
    extra_keys: tuple[tuple[str, str], ...] = ()  # (key, label) aliases


SPECS: list[Spec] = [
    # ---- simple-icons (vendor-submitted, CC0) ---------------------------
    Spec(("claude",), "Claude Code", SimpleIcon("claude")),
    Spec(("codebuddy",), "CodeBuddy Code", SimpleIcon("codebuddy")),
    Spec(("copilot",), "GitHub Copilot", SimpleIcon("githubcopilot", dark_color="FFFFFF")),
    Spec(("gemini",), "Gemini / Antigravity", SimpleIcon("googlegemini")),
    Spec(("opencode",), "OpenCode", SimpleIcon("opencode", dark_color="FFFFFF")),
    Spec(("qwencode",), "Qwen Code", SimpleIcon("qwen"),
         extra_keys=(("qwen", "Qwen Code"),)),
    Spec(("kimi",), "Kimi Code", SimpleIcon("kimi", dark_color="FFFFFF")),
    Spec(("minimax",), "MiniMax Code", SimpleIcon("minimax")),
    Spec(("dsh",), "DeepSeek Harness", SimpleIcon("deepseek"),
         extra_keys=(("deepseek", "DeepSeek Harness"),)),
    Spec(("windsurf",), "Windsurf", SimpleIcon("windsurf", dark_color="FFFFFF")),
    Spec(("zed",), "Zed", SimpleIcon("zedindustries")),
    Spec(("cursor",), "Cursor", SimpleIcon("cursor", dark_color="FFFFFF")),
    Spec(("mistral",), "Mistral", SimpleIcon("mistralai")),
    Spec(("ollama",), "Ollama", SimpleIcon("ollama", dark_color="FFFFFF")),

    # ---- vendor SVG reduced to paths ------------------------------------
    # OpenAI's mark: the knot only, out of its circular app icon.
    Spec(("codex",), "Codex",
         SvgPaths("https://openai.com/favicon.svg", "000000",
                  view_box="0 0 180 180", dark_color="FFFFFF")),
    # Devin's geometric mark: one black path on a 425x425 canvas, but the art
    # only spans 70..360, so the box is tightened or it renders tiny.
    Spec(("devin",), "Devin",
         SvgPaths("https://devin.ai/favicon.svg", "000000",
                  dark_color="FFFFFF", tight_box=True)),
    # Command Code's pinned-tab SVG is potrace output: the glyph lives inside a
    # <g transform="translate(0,700) scale(0.1,-0.1)">, so it MUST stay a whole
    # document -- extracting the d= alone renders a fragment in the wrong place.
    Spec(("cmdcode",), "Command Code",
         SvgDocument("https://commandcode.ai/favicon/2024/safari-pinned-tab.svg",
                     tiled=True)),
    # Pi ships a prefers-color-scheme aware monochrome mark.
    Spec(("pi",), "Pi Coding Agent",
         SvgPaths("https://pi.dev/favicon.svg", "111111",
                  view_box="0 0 560 560", dark_color="F6F6F6")),

    # ---- vendor SVG kept whole (gradients / masks) ----------------------
    Spec(("muse",), "Muse Code", SvgDocument("https://muse.ai/images/favicon/app-squiggle.svg"),
         extra_keys=(("muse-code", "Muse Code"),)),
    # OpenClaw's mark is a gradient-filled lobster; keep the document intact.
    Spec(("openclaw",), "OpenClaw", SvgDocument("https://openclaw.ai/favicon.svg")),
    Spec(("workbuddy",), "WorkBuddy",
         SvgDocument("https://assets.workbuddy.cn/web/workbuddy/"
                     "7b25e1f24385a28a7c133de175ff1dc2bc6b455e/assets/logo.svg"),
         extra_keys=(("workbuddy-intl", "WorkBuddy AI"),)),

    # ---- vendor bitmaps -------------------------------------------------
    # Grok's favicon SVG is a Figma export using foreignObject + backdrop
    # filter; only the raster app icon reproduces it faithfully.
    Spec(("grok",), "Grok Build", Raster("https://grok.com/images/android-chrome-512x512.png")),
    # Hermes (Nous Research): the agent's own artwork, a grayscale portrait on
    # an opaque plate -- that plate is part of the mark, so it gets a backing.
    Spec(("hermes",), "Hermes",
         Raster("https://nousresearch.com/assets/nous-web/favicon.png", tiled=True)),
    # MiMo ships only a 32px .ico; it is still the vendor's own shipping icon.
    Spec(("mimocode",), "MiMoCode",
         Raster("https://cdn.cnbj1.fds.api.mi-img.com/aife/mimo-blog-fe/doc_build/mimo.ico")),
    # Prime Intellect's site logo is a wordmark, so use the app icon.
    Spec(("prime",), "Prime Agent",
         Raster("https://primeintellect.ai/favicon.ico"),
         extra_keys=(("prime-agent", "Prime Agent"),)),
    # ZCode (GLM harness) serves a transparent 192px tile.
    Spec(("zcode",), "ZCode",
         Raster("https://zcode.z.ai/favicon-192x192.png?v=20260707-transparent")),
    # Qoder: four adapter names, one product. The 412px tile is the only
    # asset that stays legible on both a light and a dark card.
    Spec(("qodercli",), "Qoder CLI",
         Raster("https://img.alicdn.com/imgextra/i4/"
                "O1CN01QkSxiCocd3D0prc8_!!6000000008124-2-tps-412-412.png"),
         extra_keys=(("qoder-ide", "Qoder Desktop"),
                     ("qodercli-cn", "Qoder CN"),
                     ("qoder", "Qoder"))),
    # Doubao: official 192px app tile from ByteDance's CDN.
    Spec(("doubao",), "Doubao Work",
         Raster("https://lf-flow-web-cdn.doubao.com/obj/flow-doubao/"
                "favicon/new-doubao/192x192.png")),
]

#: Neutral fallback mark, drawn on purpose: it is not an agent's logo.
DEFAULT_ICON = {
    "kind": "paths",
    "paths": ["M12 2.9l1.7 5.5 5.5 1.7-5.5 1.7L12 17.3l-1.7-5.5-5.5-1.7 5.5-1.7Z"],
    "viewBox": "0 0 24 24",
    "mode": "stroke",
    "color": "64748B",
    "official": False,
    "label": "Agent",
}

RASTER_SIZE = 64  # px, rendered at 18px; 64 keeps it crisp on HiDPI


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def _cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    tail = re.sub(r"[^A-Za-z0-9._-]+", "_", url.rsplit("/", 1)[-1])[-40:] or "asset"
    return CACHE / f"{tail}.{digest}"


def fetch(url: str, *, offline: bool = False, refresh: bool = False) -> bytes:
    """Return the bytes for ``url``, using ``_icon_cache/`` unless refreshing."""
    CACHE.mkdir(exist_ok=True)
    path = _cache_path(url)
    if path.is_file() and not refresh:
        return path.read_bytes()
    if offline:
        raise SystemExit(f"offline and not cached: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code} for {url}") from e
    except urllib.error.URLError as e:
        raise SystemExit(f"network error for {url}: {e.reason}") from e
    path.write_bytes(data)
    return data


# --------------------------------------------------------------------------
# SVG handling (no XML parser dependency needed for these shapes)
# --------------------------------------------------------------------------

_TAG = re.compile(r"<(/?)([A-Za-z:]+)([^>]*?)(/?)>", re.S)
_ATTR = re.compile(r'([A-Za-z:-]+)\s*=\s*"([^"]*)"')
_OPAQUE_CONTAINERS = {"defs", "clipPath", "mask", "symbol", "pattern", "marker"}


def _attrs(raw: str) -> dict[str, str]:
    return {k: v for k, v in _ATTR.findall(raw)}


def extract_paths(svg: str, *, keep_fill: str | None = None) -> list[str]:
    """Path data visible to a renderer, skipping <defs>/<clipPath>/<mask>.

    Those containers hold reusable geometry (clip rectangles, mask shapes).
    Emitting them as painted paths would draw spurious boxes.

    Paths that depend on an ancestor ``<g transform>`` cannot be extracted
    faithfully (the front-end draws ``d`` data only). Such SVGs must be kept
    as whole documents via :class:`SvgDocument` instead.
    """
    out: list[str] = []
    stack: list[str] = []
    for closing, name, raw, selfclose in _TAG.findall(svg):
        if closing:
            if stack and stack[-1] == name:
                stack.pop()
            elif name in stack:
                while stack and stack.pop() != name:
                    pass
            continue
        if any(tag in _OPAQUE_CONTAINERS for tag in stack):
            if not selfclose:
                stack.append(name)
            continue
        if name == "path":
            a = _attrs(raw)
            d = a.get("d", "").strip()
            fill = a.get("fill", "")
            usable = bool(d)
            if keep_fill is not None and fill.lower() not in (keep_fill.lower(), "currentcolor"):
                usable = False
            if fill.lower() == "none" or fill.lower().startswith("url("):
                usable = False
            if usable:
                out.append(d)
        if not selfclose:
            stack.append(name)
    return out


_NUM = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
#: argument count per path command (SVG 1.1/2 subset we actually emit)
_CMD_ARGS = {"M": 2, "L": 2, "T": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "A": 7, "Z": 0}


def path_bounds(paths: list[str]) -> tuple[float, float, float, float] | None:
    """Conservative bounding box of the coordinates used by ``paths``.

    A curve always stays inside the hull of its control points, so this box
    always contains the drawn ink -- it may be slightly larger, never smaller.
    That makes it safe for ``tight_box``: it trims a vendor's sloppy padding
    without ever clipping a curve.
    """
    xs: list[float] = []
    ys: list[float] = []
    for d in paths:
        x = y = 0.0
        for cmd, raw_args in _split_commands(d):
            up = cmd.upper()
            n = _CMD_ARGS.get(up, 0)
            if n == 0:
                continue
            rel = cmd.islower()
            step = n
            for i in range(0, len(raw_args) - n + 1, step):
                chunk = raw_args[i:i + n]
                if up in ("M", "L", "T"):
                    px, py = chunk[0], chunk[1]
                    if rel:
                        px, py = x + px, y + py
                    x, y = px, py
                    xs.append(x)
                    ys.append(y)
                elif up == "H":
                    x = x + chunk[0] if rel else chunk[0]
                    xs.append(x)
                    ys.append(y)
                elif up == "V":
                    y = y + chunk[0] if rel else chunk[0]
                    xs.append(x)
                    ys.append(y)
                elif up == "A":
                    # rx ry rot large-arc sweep x y -- only the endpoint is a point
                    px, py = chunk[5], chunk[6]
                    if rel:
                        px, py = x + px, y + py
                    x, y = px, py
                    xs.append(x)
                    ys.append(y)
                else:  # C, S, Q: every pair is a control point or an endpoint
                    pts = list(chunk)
                    if rel:
                        pts = [(x + v) if k % 2 == 0 else (y + v) for k, v in enumerate(pts)]
                    xs.extend(pts[0::2])
                    ys.extend(pts[1::2])
                    x, y = pts[-2], pts[-1]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _split_commands(d: str) -> list[tuple[str, list[float]]]:
    """Split path data into ``(command, numbers)`` runs.

    Implicit repetition ("5 5 5 5" after an ``L``) is left to the caller, which
    consumes numbers in ``_CMD_ARGS``-sized chunks.
    """
    out: list[tuple[str, list[float]]] = []
    current: str | None = None
    nums: list[float] = []
    for tok in re.findall(r"[MmZzLlHhVvCcSsQqTtAa]|" + _NUM.pattern, d):
        if tok.isalpha() and tok in _CMD_ARGS:
            if current is not None:
                out.append((current, nums))
            current, nums = tok, []
        else:
            nums.append(float(tok))
    if current is not None:
        out.append((current, nums))
    return out


def svg_viewbox(svg: str) -> str | None:
    m = re.search(r'viewBox\s*=\s*"([^"]+)"', svg)
    if m:
        return m.group(1).strip()
    w = re.search(r'\bwidth\s*=\s*"([\d.]+)(?:px)?"', svg)
    h = re.search(r'\bheight\s*=\s*"([\d.]+)(?:px)?"', svg)
    if w and h:
        return f"0 0 {w.group(1)} {h.group(1)}"
    return None


def _strip_background_rect(svg: str) -> str:
    """Drop a full-bleed background <rect> so the mark gets no own tile.

    Only rects that carry no transform and start at the viewBox origin with a
    solid ``fill`` are removed; decorative rects inside groups are kept.
    """

    def repl(m: re.Match[str]) -> str:
        tag = m.group(0)
        a = _attrs(m.group(1))
        if a.get("transform") or "%" in (a.get("width", "") + a.get("height", "")):
            return tag
        if a.get("x", "0") not in ("0", "0.0") or a.get("y", "0") not in ("0", "0.0"):
            return tag
        fill = a.get("fill", "")
        if fill.startswith("url(") or fill in ("none", ""):
            return tag  # a gradient tile is part of the brand identity
        return ""

    return re.sub(r"<rect\b([^>]*?)/?>", repl, svg)


def svg_to_data_uri(svg: str) -> str:
    # Percent-free base64 keeps the payload copy/paste safe in Python source.
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return "data:image/svg+xml;base64," + b64


def raster_to_data_uri(data: bytes, url: str = "") -> str:
    """Normalise a vendor bitmap to a 64x64 transparent PNG, base64 inlined.

    Uses Pillow when it is importable (it is part of the authoring env) and
    falls back to embedding the original bytes otherwise, so the script still
    runs on a bare interpreter.
    """
    try:
        from PIL import Image  # noqa: PLC0415 - optional authoring dependency
    except ImportError:
        mime = _mime_for(url, data)
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")

    im = Image.open(io.BytesIO(data))
    # Multi-resolution .ico: Pillow exposes the largest frame after seek(0) only
    # if we ask for it, so pick the biggest explicitly.
    best = None
    try:
        for i in range(getattr(im, "n_frames", 1)):
            im.seek(i)
            cur = im.convert("RGBA")
            if best is None or cur.width * cur.height > best.width * best.height:
                best = cur
    except EOFError:
        best = im.convert("RGBA")
    if best is None:
        best = im.convert("RGBA")

    canvas = best.copy()
    canvas.thumbnail((RASTER_SIZE, RASTER_SIZE), Image.LANCZOS)
    frame = Image.new("RGBA", (RASTER_SIZE, RASTER_SIZE), (0, 0, 0, 0))
    frame.alpha_composite(canvas, ((RASTER_SIZE - canvas.width) // 2,
                                   (RASTER_SIZE - canvas.height) // 2))
    buf = io.BytesIO()
    frame.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _mime_for(url: str, data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    if b"<svg" in data[:400]:
        return "image/svg+xml"
    tail = url.rsplit(".", 1)[-1].lower()
    return {"png": "image/png", "ico": "image/x-icon", "svg": "image/svg+xml"}.get(tail, "image/png")


def is_tiled(data_uri: str) -> bool:
    """True when the raster has no meaningful transparency in its corners.

    A square whose corner pixels are opaque means the vendor shipped a plate
    (black tile, white tile, rounded app icon). Those need a background behind
    them on the opposite theme; a cut-out glyph does not.
    """
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return False
    import base64 as _b64

    raw = _b64.b64decode(data_uri.split(",", 1)[1])
    im = Image.open(io.BytesIO(raw)).convert("RGBA")
    w, h = im.size
    corners = [im.getpixel((1, 1)), im.getpixel((w - 2, 1)),
               im.getpixel((1, h - 2)), im.getpixel((w - 2, h - 2))]
    return all(a > 200 for (_, _, _, a) in corners)


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


def build_entry(spec: Spec, *, offline: bool, refresh: bool) -> dict:
    src = spec.source
    if isinstance(src, SimpleIcon):
        url = f"https://cdn.simpleicons.org/{src.slug}"
        svg = fetch(url, offline=offline, refresh=refresh).decode("utf-8", "replace")
        paths = extract_paths(svg)
        if not paths:
            raise SystemExit(f"{spec.keys[0]}: no paths in {url}")
        colour = (re.search(r'fill="(#[0-9A-Fa-f]{3,8})"', svg) or [None, None])[1]
        entry = {
            "kind": "paths",
            "paths": paths,
            "viewBox": svg_viewbox(svg) or "0 0 24 24",
            "mode": "fill",
            "color": (colour or "#000000").lstrip("#"),
            "official": True,
            "label": spec.label,
        }
        if src.dark_color:
            entry["dark_color"] = src.dark_color
        return entry

    if isinstance(src, SvgPaths):
        svg = fetch(src.url, offline=offline, refresh=refresh).decode("utf-8", "replace")
        paths = extract_paths(svg)
        if not paths:
            raise SystemExit(f"{spec.keys[0]}: no usable paths in {src.url}")
        view_box = src.view_box or svg_viewbox(svg) or "0 0 24 24"
        if src.tight_box:
            box = path_bounds(paths)
            if box:
                x0, y0, x1, y1 = box
                # square it up so the mark keeps its proportions when drawn in
                # a square tile, then add a small breathing margin
                side = max(x1 - x0, y1 - y0)
                if side > 0:
                    pad = side * 0.02
                    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                    side += pad * 2
                    view_box = f"{cx - side / 2:.3f} {cy - side / 2:.3f} {side:.3f} {side:.3f}"
        entry = {
            "kind": "paths",
            "paths": paths,
            "viewBox": view_box,
            "mode": "fill",
            "color": src.color,
            "official": True,
            "label": spec.label,
        }
        if src.dark_color:
            entry["dark_color"] = src.dark_color
        return entry

    if isinstance(src, SvgDocument):
        svg = fetch(src.url, offline=offline, refresh=refresh).decode("utf-8", "replace")
        if src.drop_background:
            svg = _strip_background_rect(svg)
        entry = {
            "kind": "svg",
            "src": svg_to_data_uri(svg.strip()),
            "viewBox": svg_viewbox(svg) or "0 0 24 24",
            "official": True,
            "label": spec.label,
        }
        if src.tiled:
            entry["tiled"] = True
        return entry

    if isinstance(src, Raster):
        data = fetch(src.url, offline=offline, refresh=refresh)
        entry = {
            "kind": "image",
            "src": raster_to_data_uri(data, url=src.url),
            "official": True,
            "label": spec.label,
        }
        tiled = src.tiled if src.tiled is not None else is_tiled(raster_to_data_uri(data, url=src.url))
        if tiled:
            entry["tiled"] = True
        return entry

    raise AssertionError(f"unknown source for {spec.keys[0]}")


def render_module(table: dict[str, dict], sources: dict[str, str]) -> str:
    lines = [
        '"""Agent icons (GENERATED by _build_icons.py -- do not edit by hand).',
        "",
        "Every icon is embedded so the dashboard works fully offline. Each mark is",
        "official vendor artwork: a simple-icons glyph (CC0), the icon the vendor's",
        "own site declares, or the app icon the vendor ships. Nothing here is drawn",
        "by hand -- ``official`` says so, and only the neutral ``_default`` mark is",
        'not a product logo.',
        "",
        "Shapes consumed by ``static/app.js``:",
        '  * ``kind="paths"`` -- inline <svg>; ``mode`` is ``fill`` or ``stroke``;',
        '    ``dark_color`` flips near-black marks for the dark theme.',
        '  * ``kind="image"`` -- base64 data URI in <img>; ``mono`` marks line art',
        "    that the stylesheet inverts in dark mode.",
        '  * ``kind="svg"``   -- a whole SVG document as a data URI, for marks that',
        "    rely on gradients / clip paths and cannot be reduced to path data.",
        "",
        "Provenance per agent:",
        *[f"  {k}: {v}" for k, v in sorted(sources.items())],
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Any",
        "",
        '__all__ = ["ICONS", "icon_for", "icons_json"]',
        "",
    ]
    total_bytes = sum(len(str(v)) for v in table.values())
    lines.append(f"# {len(table)} icons, ~{total_bytes // 1024} KB of embedded data.")
    lines.append("ICONS: dict[str, dict[str, Any]] = {")
    for key in ["_default"] + sorted(k for k in table if k != "_default"):
        lines.append(f"    {key!r}: {table[key]!r},")
    lines.append("}")
    lines += [
        "",
        "",
        "def icon_for(agent: str) -> dict[str, Any]:",
        '    """Look up an icon by adapter name, falling back to a neutral mark."""',
        '    return ICONS.get(agent, ICONS["_default"])',
        "",
        "",
        "def icons_json() -> dict[str, dict[str, Any]]:",
        '    """Serialisable icon table for the dashboard (``/api/icons``)."""',
        "    return ICONS",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Regenerate agent_token_radar/icons.py")
    ap.add_argument("--offline", action="store_true", help="use _icon_cache/ only")
    ap.add_argument("--refresh", action="store_true", help="re-download everything")
    ap.add_argument("--check", action="store_true",
                    help="verify icons.py matches the specs, do not write")
    args = ap.parse_args(argv)

    table: dict[str, dict] = {"_default": DEFAULT_ICON}
    sources: dict[str, str] = {"_default": "neutral fallback mark (not a product logo)"}
    problems: list[str] = []

    for spec in SPECS:
        try:
            entry = build_entry(spec, offline=args.offline, refresh=args.refresh)
        except SystemExit as e:
            problems.append(str(e))
            print(f"  FAIL {spec.keys[0]}: {e}", file=sys.stderr)
            continue
        for key in spec.keys:
            table[key] = entry
            sources[key] = describe(spec)
        for key, alias_label in spec.extra_keys:
            table[key] = {**entry, "label": alias_label}
            sources[key] = describe(spec)

    text = render_module(table, sources)

    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.is_file() else ""
        same = current == text
        print("icons.py is up to date" if same else "icons.py is STALE; re-run without --check")
        return 0 if same else 1

    OUT.write_text(text, encoding="utf-8")
    official = sum(1 for k, v in table.items() if k != "_default" and v.get("official"))
    total = len(table) - 1
    print(f"wrote {OUT.name}: {total} icons, {official} official, {len(text) // 1024} KB")
    if problems:
        print(f"{len(problems)} source(s) failed:", file=sys.stderr)
        for p in problems:
            print("  -", p, file=sys.stderr)
        return 1
    return 0


def describe(spec: Spec) -> str:
    src = spec.source
    if isinstance(src, SimpleIcon):
        return f"simple-icons/{src.slug}"
    if isinstance(src, SvgPaths):
        return f"vendor SVG paths {src.url}"
    if isinstance(src, SvgDocument):
        return f"vendor SVG {src.url}"
    if isinstance(src, Raster):
        note = ""
        if src.tiled:
            note = " (tiled)"
        return f"vendor icon {src.url}{note}"
    return "?"


if __name__ == "__main__":
    raise SystemExit(main())
