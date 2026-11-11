"""The CTF estimate, on micrographs made here from the textbook formula with a known defocus."""

from __future__ import annotations

import math

import numpy as np
import pytest

from fixtures.ctf import electron_wavelength, micrograph
from imagebio.core.errors import InvalidRequestError
from imagebio.proc.ctf import ctf_zeros, estimate_ctf, wavelength


def test_the_wavelength_of_the_electron() -> None:
    for kv in (80.0, 120.0, 200.0, 300.0):
        assert wavelength(kv) == pytest.approx(electron_wavelength(kv), rel=1e-5)
    assert wavelength(300.0) == pytest.approx(0.019687, abs=2e-6)  # the value every textbook gives
    assert wavelength(200.0) == pytest.approx(0.025079, abs=2e-6)


@pytest.mark.parametrize(
    ("apix", "kv", "defocus"),
    [
        (0.83, 300.0, 0.6),
        (0.83, 300.0, 1.5),
        (0.83, 300.0, 3.0),
        (1.2, 300.0, 1.0),
        (1.2, 300.0, 4.5),
        (1.06, 200.0, 2.2),
        (0.5, 300.0, 1.2),
    ],
)
def test_the_defocus_of_a_micrograph_is_found_within_two_percent(apix, kv, defocus) -> None:
    """Acceptance (M7): the defocus of synthetic images of known CTF is found within 2 %."""
    rng = np.random.default_rng(int(1000 * defocus + 10 * apix))
    image = micrograph((2048, 2048), apix, defocus, rng, kv=kv)
    found = estimate_ctf(image, apix, kv=kv)
    assert found is not None
    assert found.defocus_um == pytest.approx(defocus, rel=0.02)
    assert found.correlation > 0.8 and found.score > 9
    assert 3.4 < found.fit_resolution < 6.0  # the rings are there as far as they are looked for
    assert (found.apix, found.kv, found.cs_mm, found.amplitude) == (apix, kv, 2.7, 0.07)


def test_the_microscope_matters() -> None:
    """The same rings read with another voltage or aberration give another defocus: the
    parameters are not decoration."""
    rng = np.random.default_rng(51)
    image = micrograph((2048, 2048), 1.0, 2.0, rng, kv=200.0, cs_mm=1.4, amplitude=0.1)
    right = estimate_ctf(image, 1.0, kv=200.0, cs_mm=1.4, amplitude=0.1)
    assert right is not None and right.defocus_um == pytest.approx(2.0, rel=0.01)
    wrong = estimate_ctf(image, 1.0, kv=300.0, cs_mm=1.4, amplitude=0.1)
    # at 300 kV the wavelength is shorter: the same rings need a larger defocus
    assert wrong is not None and wrong.defocus_um == pytest.approx(2.0 * 0.025079 / 0.019687, rel=0.03)
    bigger_pixel = estimate_ctf(image, 1.1, kv=200.0, cs_mm=1.4, amplitude=0.1)
    assert bigger_pixel is not None and bigger_pixel.defocus_um == pytest.approx(2.0 * 1.1**2, rel=0.03)


def test_what_blurs_the_rings_shortens_the_fit() -> None:
    rng = np.random.default_rng(52)
    sharp = estimate_ctf(micrograph((2048, 2048), 0.83, 1.5, rng), 0.83)
    damped = estimate_ctf(micrograph((2048, 2048), 0.83, 1.5, rng, b_factor=300.0), 0.83)
    weak = estimate_ctf(micrograph((2048, 2048), 0.83, 1.5, rng, signal=0.1), 0.83)
    astigmatic = estimate_ctf(micrograph((2048, 2048), 0.83, 1.5, rng, astigmatism_um=0.1), 0.83)
    assert sharp is not None and damped is not None and weak is not None and astigmatic is not None
    for other in (damped, weak, astigmatic):
        assert other.defocus_um == pytest.approx(1.5, rel=0.04)  # still the right defocus, to the mean
        assert other.correlation < sharp.correlation - 0.1 and other.fit_resolution >= sharp.fit_resolution
    # an envelope that kills the fine rings, and rings smeared by astigmatism: found less far
    assert damped.fit_resolution > sharp.fit_resolution + 1.0
    assert astigmatic.fit_resolution > sharp.fit_resolution + 1.0


def test_no_rings_no_defocus() -> None:
    rng = np.random.default_rng(53)
    for apix in (0.83, 1.2, 0.5):
        for _ in range(4):
            assert estimate_ctf(rng.normal(0, 1, (2048, 2048)).astype(np.float32), apix) is None
            assert estimate_ctf(rng.poisson(3, (1024, 1024)).astype(np.uint16), apix) is None
    # rings too faint to be told from noise are not given a defocus either
    assert estimate_ctf(micrograph((2048, 2048), 0.83, 1.8, rng, signal=0.02), 0.83) is None
    # a defocus outside the bounds asked for is not found
    image = micrograph((2048, 2048), 0.83, 3.0, rng)
    assert estimate_ctf(image, 0.83, max_defocus_um=2.0) is None
    assert estimate_ctf(image, 0.83, min_defocus_um=2.5).defocus_um == pytest.approx(3.0, rel=0.01)  # type: ignore[union-attr]


def test_sizes_holes_and_bad_parameters() -> None:
    rng = np.random.default_rng(54)
    small = estimate_ctf(micrograph((640, 800), 1.2, 1.5, rng), 1.2)  # a 512-pixel spectrum
    assert small is not None and small.tile == 512 and small.defocus_um == pytest.approx(1.5, rel=0.02)
    assert estimate_ctf(micrograph((400, 400), 1.2, 1.5, rng), 1.2) is None  # too small a field
    large = micrograph((2600, 3000), 0.83, 2.0, rng)  # measured on its central field
    large[:200] = 0
    large[5, 7], large[1300, 1500] = np.nan, np.inf
    found = estimate_ctf(large, 0.83)
    assert found is not None and found.tile == 1024 and found.defocus_um == pytest.approx(2.0, rel=0.02)
    assert estimate_ctf(np.zeros((1024, 1024), np.float32), 1.0) is None
    image = micrograph((1024, 1024), 1.0, 1.5, rng)
    for bad in (
        {"apix": 0.0},
        {"apix": float("nan")},
        {"kv": 5.0},
        {"cs_mm": -1.0},
        {"amplitude": 0.9},
        {"min_defocus_um": 0.0},
        {"min_defocus_um": 3.0, "max_defocus_um": 2.0},
    ):
        kwargs = {"apix": 1.0, **bad}
        with pytest.raises(InvalidRequestError):
            estimate_ctf(image, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(InvalidRequestError):
        estimate_ctf(np.zeros((2, 600, 600), np.float32), 1.0)


def test_the_zeros_of_the_fit_are_those_of_the_formula() -> None:
    rng = np.random.default_rng(55)
    found = estimate_ctf(micrograph((2048, 2048), 1.0, 2.0, rng), 1.0)
    assert found is not None
    zeros = ctf_zeros(found, finest=4.0)
    assert zeros == sorted(zeros, reverse=True) and zeros[-1] >= 4.0 and len(zeros) > 10
    lam = electron_wavelength(300.0)
    for d in zeros:  # each one is where sin(chi + phase) vanishes
        k2 = 1 / (d * d)
        chi = math.pi * lam * k2 * (found.defocus_um * 1e4 - 0.5 * 2.7e7 * lam * lam * k2) + math.asin(0.07)
        assert abs(math.sin(chi)) < 0.02
    # the first zero of 2 µm at 300 kV: 1/sqrt(λ Δf) to first order, about 19.5 Å
    assert zeros[0] == pytest.approx(
        math.sqrt(lam * 2.0e4) * (1 / math.sqrt(1 - math.asin(0.07) / math.pi)), rel=0.02
    )
    body = found.to_json()
    assert body["zeros"][0] == pytest.approx(zeros[0], abs=1e-3) and body["defocus_um"] == round(
        found.defocus_um, 4
    )
