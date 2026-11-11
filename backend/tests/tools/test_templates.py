from __future__ import annotations

import re

import pytest

from imagebio.tools.templates import (
    PLACEHOLDERS,
    MissingValueError,
    UnsafeValueError,
    check_template,
    fill,
    number,
)


def test_a_template_names_only_what_is_known() -> None:
    for name in PLACEHOLDERS:
        check_template(f"--x={{{name}}}/y")
    check_template("-Patch")
    check_template("{out}/aligned.mrc", allowed={"out"})
    for bad in (
        "{moovie}",
        "{out}/{nope}",
        "{",
        "a}b",
        "{{out}}",
        "{OUT}",
        "{out",
        "{0}",
        "{}",
        "x\ny",
        "x\0",
    ):
        with pytest.raises(ValueError):
            check_template(bad)
    with pytest.raises(ValueError, match=re.escape("unknown placeholder {movie}")):
        check_template("{movie}", allowed={"out", "image"})


def test_a_filled_argument_stays_one_argument() -> None:
    values = {"movie": "/data/my session/a b; rm -rf $(x) `y` 'z'.mrc", "out": "/work/1", "apix": "0.834"}
    assert fill("{movie}", values) == values["movie"]  # spaces, quotes, dollars: all of it, as it is
    assert fill("--in={movie}:{apix}", values) == f"--in={values['movie']}:0.834"
    assert fill("{out}/sum.mrc", values) == "/work/1/sum.mrc" and fill("-Patch", values) == "-Patch"
    # what a value holds is not looked at again: a placeholder inside a file name stays text
    assert fill("{movie}", {"movie": "/data/{out}.mrc", "out": "/work/1"}) == "/data/{out}.mrc"


def test_what_is_not_known_or_not_safe_stops_the_run() -> None:
    with pytest.raises(MissingValueError) as missing:
        fill("{movie} {apix}", {"movie": "/m.mrc", "apix": None})
    assert missing.value.name == "apix"
    with pytest.raises(MissingValueError):
        fill("{dose}", {})
    # a line break in a file name would be one more answer typed to the program
    for hostile in (
        "/data/a\nno\n.mrc",
        "/data/a\r.mrc",
        "/data/a\0.mrc",
        "/data/a\x1b[2J.mrc",
        "/data/\x7f.mrc",
    ):
        with pytest.raises(UnsafeValueError) as unsafe:
            fill("{image}", {"image": hostile})
        assert unsafe.value.name == "image"


def test_numbers_are_written_as_programs_read_them() -> None:
    assert number(0.834) == "0.834" and number(300.0) == "300" and number(2.7) == "2.7"
    assert number(40) == "40" and number(1.2345678) == "1.23457"
    assert number(0.00001234) == "0.000012" and number(1e7) == "10000000.000000"  # never an exponent
    assert number(None) is None and number(True) is None
