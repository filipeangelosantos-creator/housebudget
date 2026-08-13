"""Things pinned to the top of the window have to clear what's already there.

On a phone the navigation sits at the bottom, so a sticky element only has to
clear the top bar. On a wide window the nav moves up and stacks under it, and
anything pinned to --top-h that still assumes one bar lands underneath the nav
— which has a higher z-index, so it doesn't overlap it, it vanishes behind it.
That failure is invisible at phone width, which is where it gets tested.

These read the stylesheet rather than a rendered page: there is no browser in
the test suite, and the arithmetic is the part that goes wrong.
"""
import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parent.parent / "app" / "static"
       / "style.css").read_text(encoding="utf-8")


def block_after(text: str, start: int) -> str:
    """The {...} body beginning at or after `start`, braces balanced."""
    depth, begin = 0, None
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
            if depth == 1:
                begin = i + 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[begin:i]
    raise AssertionError("unbalanced braces in style.css")


def wide_block() -> str:
    m = re.search(r"@media\s*\(min-width:\s*800px\)", CSS)
    assert m, "no wide-screen media query"
    return block_after(CSS, m.end())


def px(rule: str, prop: str) -> int:
    # Not \b: there is no word boundary before the "--" of a custom property.
    m = re.search(rf"(?<![\w-]){re.escape(prop)}:\s*(\d+)px", rule)
    assert m, f"no {prop} in {rule!r}"
    return int(m.group(1))


def rule_body(selector: str) -> str | None:
    m = re.search(rf"(?<![\w.-]){re.escape(selector)}\s*\{{([^}}]*)\}}", CSS)
    return m.group(1) if m else None


def test_the_pin_offset_clears_the_nav_once_it_moves_to_the_top():
    """Regression: --top-h stayed at the phone's 45px on wide screens, so the
    budget verdict and the activity filters pinned behind the top nav and
    disappeared as soon as you scrolled."""
    wide = wide_block()
    nav = re.search(r"\.nav\s*\{([^}]*)\}", wide)
    assert nav, "the wide layout no longer restyles .nav — check this still holds"
    needed = px(nav.group(1), "top") + px(nav.group(1), "height")

    m = re.search(r"--top-h:\s*(\d+)px", wide)
    assert m, "--top-h is not redefined for the wide layout"
    assert int(m.group(1)) >= needed, (
        f"--top-h is {m.group(1)}px but the nav ends at {needed}px, so anything "
        "pinned to it sits behind the nav")


def test_the_phone_offset_clears_the_top_bar():
    root = block_after(CSS, re.search(r"^:root", CSS, re.M).end())
    top_h = px(root, "--top-h")
    bar = re.search(r"\.topbar\s*\{([^}]*)\}", CSS)
    padding = px(bar.group(1), "padding")
    assert top_h >= padding * 2, "the top bar is taller than the offset under it"


@pytest.mark.parametrize("selector", [".filter-bar", ".bal-strip"])
def test_what_must_stay_in_view_pins_to_the_shared_offset(selector):
    """A literal here works on one screen size and hides on the other."""
    body = rule_body(selector)
    assert body, f"{selector} is gone — did it stop being sticky?"
    assert "position: sticky" in body
    assert "top: var(--top-h)" in body, (
        f"{selector} pins to a literal instead of --top-h, so it only clears "
        "the header on one of the two layouts")
