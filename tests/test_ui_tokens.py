"""Static checks on the UI tokens (no browser): both themes define the same names, text pairs meet WCAG AA,
every var(--x) in the front end is defined, and the count of hard-coded colors only goes down."""
import pathlib
import re

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
TOKENS = STATIC / "css" / "tokens.css"
COLOR_ALLOWLIST = {"tokens.css", "theme-colors.js"}      # the only files allowed to hold color literals

# Lowered by every task that removes hard-coded colors; Task 8 sets it to 0.
HARDCODED_COLOR_BUDGET = 253

# Custom properties set from JS at runtime (never declared in a stylesheet).
RUNTIME_VARS = {"shell-h"}

_DECL = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;]+);")


def _blocks():
    css = TOKENS.read_text(encoding="utf-8")
    dark = re.search(r":root\s*\{([^}]*)\}", css).group(1)
    light = re.search(r':root\[data-theme="light"\]\s*\{([^}]*)\}', css).group(1)
    return ({k: v.strip() for k, v in _DECL.findall(dark)},
            {k: v.strip() for k, v in _DECL.findall(light)})


def _lum(hex_color):
    h = hex_color.lstrip("#")
    chans = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        chans.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * chans[0] + 0.7152 * chans[1] + 0.0722 * chans[2]


def _ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_both_themes_define_the_same_names():
    dark, light = _blocks()
    theme_independent = {k for k in dark if k not in light}
    assert set(light) <= set(dark), f"light-only tokens: {set(light) - set(dark)}"
    # Only fonts, radii, spacing and motion may be defined once.
    assert all(re.match(r"--(font|r|s|dur|ease)", k) for k in theme_independent), theme_independent


TEXT = ["text-strong", "text", "text-muted"]
SURFACES = ["bg-canvas", "bg-panel", "bg-raised", "bg-hover"]


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_text_on_surfaces_meets_aa(theme):
    dark, light = _blocks()
    tokens = {k[2:]: v for k, v in {**dark, **(light if theme == "light" else {})}.items()}
    bad = []
    for t in TEXT:
        for s in SURFACES:
            if _ratio(tokens[t], tokens[s]) < 4.5:
                bad.append((t, s, round(_ratio(tokens[t], tokens[s]), 2)))
    for s in ["bg-canvas", "bg-panel", "bg-raised"]:
        for t in ["text-faint", "accent", "up", "down", "info", "alt"]:
            if _ratio(tokens[t], tokens[s]) < 4.5:
                bad.append((t, s, round(_ratio(tokens[t], tokens[s]), 2)))
    if _ratio(tokens["accent-fg"], tokens["accent"]) < 4.5:
        bad.append(("accent-fg", "accent", round(_ratio(tokens["accent-fg"], tokens["accent"]), 2)))
    assert not bad, f"{theme}: pairs below 4.5:1 -> {bad}"


def _scan_files():
    for path in sorted(STATIC.rglob("*")):
        if path.suffix in {".css", ".js", ".html"} and path.name not in COLOR_ALLOWLIST:
            yield path


_HEX = re.compile(r"(?<=['\"(:,\s])#[0-9a-fA-F]{3,8}(?![\w-])")
_RGBA = re.compile(r"rgba?\(\s*(?!0\s*,\s*0\s*,\s*0\s*,\s*0\s*\))\d")        # a literal rgb/rgba; not rgba(0,0,0,0), not rgb(${...})
_CSS_BLOCK = re.compile(r"\{[^{}]*\}")


def hardcoded_colors():
    found = []
    for path in _scan_files():
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".css":
            chunks = [m.group(0) for m in _CSS_BLOCK.finditer(text)]
        else:
            chunks = [text]
        for chunk in chunks:
            for rx in (_HEX, _RGBA):
                found += [(path.name, m.group(0)) for m in rx.finditer(chunk)]
    return found


def test_hardcoded_color_count_only_goes_down():
    found = hardcoded_colors()
    assert len(found) <= HARDCODED_COLOR_BUDGET, (
        f"{len(found)} hard-coded colors outside {sorted(COLOR_ALLOWLIST)}; budget {HARDCODED_COLOR_BUDGET}. "
        f"Use a token. First few: {found[:8]}")


def test_every_used_var_is_defined():
    dark, _ = _blocks()
    defined = set(k[2:] for k in dark)
    local = set()
    used = set()
    for path in sorted(STATIC.rglob("*")):
        if path.suffix not in {".css", ".js", ".html"}:
            continue
        text = path.read_text(encoding="utf-8")
        used |= set(re.findall(r"var\(--([a-z0-9-]+)", text))
        if path.name != "tokens.css":
            local |= set(re.findall(r"(?<![\w-])--([a-z0-9-]+)\s*:", text))
    missing = used - defined - local - RUNTIME_VARS
    assert not missing, f"var(--x) used but never defined: {sorted(missing)}"
