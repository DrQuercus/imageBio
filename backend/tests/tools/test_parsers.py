"""Result files written to the published formats, read back. The expected numbers are worked
out here from the numbers written, never by the parsers themselves."""

from __future__ import annotations

import tracemalloc
from pathlib import Path

import numpy as np
import pytest

from imagebio.core.errors import CorruptFileError
from imagebio.tools.parsers import MAX_RESULT_BYTES, parse_ctffind, parse_motion_log, parse_motion_star

CTFFIND4 = """\
# Output from CTFFind version 4.1.14, run on 2026-11-11 10:12:13
# Input file: aligned.mrc ; Number of micrographs: 1
# Pixel size: 0.834 Angstroms ; acceleration voltage: 300.0 keV ; spherical aberration: 2.70 mm ; amplitude contrast: 0.07
# Box size: 512 pixels ; min. res.: 30.0 Angstroms ; max. res.: 5.0 Angstroms ; min. def.: 5000.0 um; max. def. 50000.0 um
# Columns: #1 - micrograph number; #2 - defocus 1 [Angstroms]; #3 - defocus 2; #4 - azimuth of astigmatism; #5 - additional phase shift [radians]; #6 - cross correlation; #7 - spacing (in Angstroms) up to which CTF rings were fit successfully
1.000000 18634.123047 18312.345703 45.123456 0.000000 0.123456 3.456789
"""

CTFFIND5 = """\
# Output from CTFFind version 5.0.2, run on 2026-11-11 10:12:13
# Columns: #1 - micrograph number; #2 - defocus 1 [Angstroms]; #3 - defocus 2; #4 - azimuth of astigmatism; #5 - additional phase shift [radians]; #6 - cross correlation; #7 - spacing (in Angstroms) up to which CTF rings were fit successfully; #8 - Estimated tilt axis angle; #9 - Estimated tilt angle ; #10 Estimated sample thickness (in Angstroms)
1.000000 12000.50 12600.50 -30.25 1.570796 0.2100 2.900 87.5 12.3 420.0
"""


def write(tmp_path: Path, text: str, name: str = "result.txt") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def test_the_fit_of_ctffind_is_read_in_micrometres(tmp_path: Path) -> None:
    fit = parse_ctffind(write(tmp_path, CTFFIND4))
    assert fit == {
        "defocus": pytest.approx((18634.123047 + 18312.345703) / 2 / 1e4, abs=1e-4),
        "defocus_u": pytest.approx(1.8634, abs=1e-4),
        "defocus_v": pytest.approx(1.8312, abs=1e-4),
        "astigmatism": pytest.approx(0.0322, abs=1e-4),
        "angle": pytest.approx(45.1, abs=0.05),
        "cc": pytest.approx(0.123, abs=1e-3),
        "fit": pytest.approx(3.46, abs=0.005),
    }  # no phase shift was looked for: none is reported
    newer = parse_ctffind(write(tmp_path, CTFFIND5))
    assert newer["defocus_u"] == pytest.approx(
        1.26005, abs=1e-4
    )  # the larger of the two, whichever comes first
    assert newer["defocus_v"] == pytest.approx(1.20005, abs=1e-4)
    assert newer["astigmatism"] == pytest.approx(0.06, abs=1e-4) and newer["angle"] == pytest.approx(
        -30.2, abs=0.06
    )
    assert newer["phase"] == pytest.approx(1.571, abs=1e-3)
    assert newer["tilt"] == pytest.approx(12.3) and newer["thickness"] == pytest.approx(42.0)  # 420 Å
    # without the line that names the columns, the first seven are those of every version
    bare = parse_ctffind(write(tmp_path, "1.0 20000.0 21000.0 10.0 0.0 0.3 4.2\n"))
    assert (bare["defocus"], bare["fit"], bare["cc"]) == (2.05, 4.2, 0.3) and "thickness" not in bare
    # several lines (a stack): the last one is the micrograph of this run
    last = parse_ctffind(write(tmp_path, CTFFIND4 + "2.000000 9000.0 9000.0 0.0 0.0 0.5 3.0\n"))
    assert last["defocus"] == 0.9 and last["astigmatism"] == 0.0


@pytest.mark.parametrize(
    "text",
    [
        "",
        "# Columns: #1 - micrograph number; #2 - defocus 1 [Angstroms]\n",  # a header and no fit
        "ctffind: error opening file\n",
        "1.0 nan nan 0.0 0.0 0.0 0.0\n",
        "1.0 inf 18000.0 0.0 0.0 0.1 3.0\n",
        "1.0 -18000.0 18000.0 0.0 0.0 0.1 3.0\n",  # overfocus is not what the program reports
        "1.0 18000.0 0.0 0.0 0.0 0.1 3.0\n",
        "1.0 9e9 9e9 0.0 0.0 0.1 3.0\n",
        "1.0 18000.0 18000.0 0.0\n",  # cut short
        "# Columns: #1 - micrograph number; #2 - something else; #3 - again\n1 2 3 4 5 6 7\n",
    ],
)
def test_what_is_not_a_fit_is_refused(tmp_path: Path, text: str) -> None:
    with pytest.raises(CorruptFileError):
        parse_ctffind(write(tmp_path, text))


def test_a_result_file_out_of_proportion_is_not_read(tmp_path: Path) -> None:
    huge = write(
        tmp_path, "# padding\n" * (MAX_RESULT_BYTES // 10 + 10) + "1.0 18000.0 18000.0 0.0 0.0 0.1 3.0\n"
    )
    with pytest.raises(CorruptFileError, match="too large"):
        parse_ctffind(huge)
    with pytest.raises(CorruptFileError, match="too large"):
        parse_motion_log(huge)
    with pytest.raises(OSError):
        parse_ctffind(tmp_path / "never_written.txt")


def shifts_of(rng: np.random.Generator, frames: int) -> np.ndarray:
    return np.cumsum(rng.normal(0, 0.8, (frames, 2)), axis=0)


def expected(shifts: np.ndarray, scale: float) -> dict[str, float]:
    steps = np.hypot(*np.diff(shifts, axis=0).T)
    return {"total": steps.sum() * scale, "early": steps[:5].sum() * scale, "max_step": steps.max() * scale}


def test_frame_shifts_of_a_relion_style_file_are_summed_up(tmp_path: Path) -> None:
    shifts = shifts_of(np.random.default_rng(5), 40)
    rows = "\n".join(f"{i + 1:12d} {x:12.6f} {y:12.6f}" for i, (x, y) in enumerate(shifts))
    star = (
        "\n# version 30001\n\ndata_general\n\n_rlnImageSizeX 4096\n_rlnImageSizeY 4096\n_rlnImageSizeZ 40\n"
        "_rlnMicrographMovieName movie.eer\n_rlnMicrographBinning 1.000000\n_rlnMicrographOriginalPixelSize 0.834\n\n"
        "data_global_shift\n\nloop_\n_rlnMicrographFrameNumber #1\n_rlnMicrographShiftX #2\n_rlnMicrographShiftY #3\n"
        f"{rows}\n\ndata_local_motion_model\n\nloop_\n_rlnMotionModelCoeffsIdx #1\n_rlnMotionModelCoeff #2\n0 0.1\n"
    )
    path = write(tmp_path, star, "aligned.star")
    in_pixels = parse_motion_star(path)
    want = expected(shifts, 1.0)
    assert in_pixels["unit"] == "px" and in_pixels["frames"] == 40
    for key, value in want.items():
        assert in_pixels[key] == pytest.approx(value, abs=0.006)
    in_angstroms = parse_motion_star(path, 0.834)
    assert in_angstroms["unit"] == "A"
    for key, value in expected(shifts, 0.834).items():
        assert in_angstroms[key] == pytest.approx(value, abs=0.006)

    for broken in (
        "data_global_shift\n\nloop_\n_rlnMicrographFrameNumber #1\n_rlnOther #2\n1 0.0\n",  # no shifts
        "data_global_shift\n\nloop_\n_rlnMicrographShiftX #1\n_rlnMicrographShiftY #2\n0.0 0.0\n",  # one frame
        "data_global_shift\n\nloop_\n_rlnMicrographShiftX #1\n_rlnMicrographShiftY #2\n0.0 0.0\nnan 1.0\n",
        "data_global_shift\n\nloop_\n_rlnMicrographShiftX #1\n_rlnMicrographShiftY #2\n0.0 0.0\nx y\n",
        "not a star file at all",
    ):
        with pytest.raises(CorruptFileError):
            parse_motion_star(write(tmp_path, broken, "broken.star"))


def test_frame_shifts_of_a_motioncor_log_are_summed_up(tmp_path: Path) -> None:
    shifts = shifts_of(np.random.default_rng(6), 25)
    log = "# full frame alignment\n# frame  x-shift  y-shift\n" + "".join(
        f"{i + 1:6d} {x:10.4f} {y:10.4f}\n" for i, (x, y) in enumerate(shifts)
    )
    got = parse_motion_log(write(tmp_path, log, "0-Patch-Full.log"), 1.1)
    assert got["frames"] == 25 and got["unit"] == "A"
    for key, value in expected(shifts, 1.1).items():
        assert got[key] == pytest.approx(value, abs=0.02)  # the log keeps four decimals
    with pytest.raises(CorruptFileError):
        parse_motion_log(write(tmp_path, "# nothing was aligned\n", "empty.log"))
    with pytest.raises(CorruptFileError, match="too many frames"):
        parse_motion_log(write(tmp_path, "1 0.0 0.0\n" * 20_002, "long.log"))


def test_a_file_name_quoted_in_the_result_does_not_pass_for_the_header(tmp_path: Path) -> None:
    """Regression. CTFFIND quotes the name of its input in a comment, and a name is whatever
    somebody called their image: one that holds "columns #600000000" was taken for the line
    that names the columns, and a list of that length was made (gigabytes)."""
    hostile = CTFFIND4.replace(
        "# Input file: aligned.mrc", "# Input file: /data/x columns #600000000 - defocus 1 y.mrc"
    )
    assert "#600000000" in hostile
    tracemalloc.start()
    try:
        fit = parse_ctffind(write(tmp_path, hostile))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert fit["defocus_u"] == pytest.approx(1.8634, abs=1e-4) and peak < 8 * 2**20  # the real header counts
    # and the line that does name the columns cannot ask for a table out of proportion either
    absurd = (
        "# Columns: #1 - micrograph number; #2 - defocus 1; #3 - defocus 2; #999 - spacing; #88888888 - cc\n"
    )
    tracemalloc.start()
    try:
        fit = parse_ctffind(write(tmp_path, absurd + "1.0 20000.0 21000.0 10.0 0.0 0.3 4.2\n"))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert fit["defocus"] == 2.05 and "fit" not in fit and peak < 8 * 2**20


def test_frame_shifts_that_give_no_finite_number_are_not_a_result(tmp_path: Path) -> None:
    """Regression. A pixel size out of all proportion (from a hostile metadata file) made the
    path infinite; the number could not be written to the index, and the scanner that tried
    failed at every cycle from then on."""
    log = write(tmp_path, "1 0.0 0.0\n2 3.0 4.0\n3 6.0 8.0\n", "shifts.log")
    assert parse_motion_log(log, 2.0)["total"] == 20.0
    for absurd in (1.5e308, 1e308):
        with pytest.raises(CorruptFileError, match="make no sense"):
            parse_motion_log(log, absurd)
    for unknown in (float("inf"), float("nan"), 0.0, -1.0):  # not a pixel size: pixels, then
        assert parse_motion_log(log, unknown) == {
            "total": 10.0,
            "early": 10.0,
            "max_step": 5.0,
            "frames": 3,
            "unit": "px",
        }
    huge = write(tmp_path, "1 0.0 0.0\n2 1e308 1e308\n3 -1e308 -1e308\n", "huge.log")
    with pytest.raises(CorruptFileError, match="make no sense"):
        parse_motion_log(huge)
