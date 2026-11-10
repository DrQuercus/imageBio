from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from imagebio.core.config import RootConfig
from imagebio.core.errors import AccessDeniedError, InvalidRequestError, NotFoundError
from imagebio.fs.roots import LOCAL_USER, RootSet, User

ALICE = User("alice", frozenset({"cryoem"}))
BOB = User("bob", frozenset({"lm"}))
NOBODY = User("nobody")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """
    base/
      cryo/        root "cryo"   (group cryoem)
        session/movie.mrc, session/notes.txt, session/.hidden
      light/       root "light"  (group lm)
        img.tif
      public/      root "public" (everyone)
        readme.png
      outside/secret.mrc         (in no root)
    """
    base = tmp_path / "base"
    for d in ("cryo/session", "light", "public", "outside"):
        (base / d).mkdir(parents=True)
    (base / "cryo/session/movie.mrc").write_bytes(b"m" * 10)
    (base / "cryo/session/notes.txt").write_text("n")
    (base / "cryo/session/.hidden").write_text("h")
    (base / "light/img.tif").write_bytes(b"t" * 20)
    (base / "public/readme.png").write_bytes(b"p")
    (base / "outside/secret.mrc").write_bytes(b"s")
    return base


@pytest.fixture
def roots(tree: Path) -> RootSet:
    return RootSet([
        RootConfig("cryo", tree / "cryo", frozenset({"cryoem"})),
        RootConfig("light", tree / "light", frozenset({"lm"})),
        RootConfig("public", tree / "public"),
    ])  # fmt: skip


def denied(roots: RootSet, logical: object, user: User = ALICE) -> None:
    with pytest.raises(AccessDeniedError) as e:
        roots.resolve(logical, user)  # type: ignore[arg-type]
    assert str(e.value) == "access denied"  # one generic message: nothing leaks


# ── resolution ───────────────────────────────────────────────────────────────


def test_resolve_files_and_directories(roots: RootSet, tree: Path):
    r = roots.resolve("cryo/session/movie.mrc", ALICE)
    assert r.real == (tree / "cryo/session/movie.mrc").resolve() and r.is_file and not r.is_dir
    assert r.logical == "cryo/session/movie.mrc" and r.root.name == "cryo"
    top = roots.resolve("cryo", ALICE)
    assert top.is_dir and top.logical == "cryo"
    # trailing and duplicate slashes are normalised
    assert roots.resolve("cryo//session///movie.mrc", ALICE).logical == "cryo/session/movie.mrc"
    assert roots.resolve("cryo/session/", ALICE).logical == "cryo/session"


@pytest.mark.parametrize("bad", [
    "", "/", "/etc/passwd", "/cryo/session", "..", ".", "cryo/..", "cryo/../outside/secret.mrc",
    "cryo/session/../../outside/secret.mrc", "../cryo", "cryo/./session", "cryo/session/..",
    "cryo\\session", "cryo/session\\..\\..", "cryo/a\x00b", "cryo/a\nb", "cryo/\x7f", "cryo/\t",
    "unknown/file", "outside/secret.mrc", "CRYO/session", "cryo/" + "a" * 256, "x" * 5000,
    None, 42, b"cryo",
])  # fmt: skip
def test_bad_syntax_and_unknown_roots_are_denied(roots: RootSet, bad: object):
    denied(roots, bad)


def test_missing_targets_are_not_found_only_inside_the_roots(roots: RootSet):
    with pytest.raises(NotFoundError) as e:
        roots.resolve("cryo/session/missing.mrc", ALICE)
    assert str(e.value) == "not found: cryo/session/missing.mrc"
    with pytest.raises(NotFoundError):
        roots.resolve("cryo/nodir/deeper/file.mrc", ALICE)
    with pytest.raises(NotFoundError):
        roots.resolve("cryo/session/movie.mrc/child", ALICE)  # a file used as a directory


def test_group_visibility(roots: RootSet):
    assert [r.name for r in roots.visible_roots(ALICE)] == ["cryo", "public"]
    assert [r.name for r in roots.visible_roots(BOB)] == ["light", "public"]
    assert [r.name for r in roots.visible_roots(NOBODY)] == ["public"]
    assert [r.name for r in roots.visible_roots(LOCAL_USER)] == ["cryo", "light", "public"]
    assert roots.resolve("light/img.tif", BOB).is_file
    assert roots.resolve("light/img.tif", LOCAL_USER).is_file
    # a forbidden root answers exactly like an unknown one, whether the file exists or not
    denied(roots, "light/img.tif", ALICE)
    denied(roots, "light/missing.tif", ALICE)
    denied(roots, "light", NOBODY)


def test_symlink_escapes_are_denied_whatever_exists_behind(roots: RootSet, tree: Path):
    s = tree / "cryo/session"
    os.symlink(tree / "outside/secret.mrc", s / "abs_file.mrc")
    os.symlink("../../outside/secret.mrc", s / "rel_file.mrc")
    os.symlink(tree / "outside", s / "dir_link")
    os.symlink(s / "dir_link", s / "chained")
    os.symlink("/etc", s / "etc")
    for path in ("abs_file.mrc", "rel_file.mrc", "dir_link", "dir_link/secret.mrc", "chained/secret.mrc",
                 "dir_link/does_not_exist", "etc/passwd", "etc/no_such_file_here"):  # fmt: skip
        denied(roots, f"cryo/session/{path}")  # same answer for existing and missing targets


def test_symlinks_inside_the_roots_follow_the_users_rights(roots: RootSet, tree: Path):
    s = tree / "cryo/session"
    os.symlink(s / "movie.mrc", s / "alias.mrc")
    os.symlink(tree / "public/readme.png", s / "to_public.png")
    os.symlink(tree / "light/img.tif", s / "to_light.tif")
    assert roots.resolve("cryo/session/alias.mrc", ALICE).real == (s / "movie.mrc").resolve()
    assert roots.resolve("cryo/session/to_public.png", ALICE).is_file  # another root she can see
    denied(roots, "cryo/session/to_light.tif", ALICE)  # a root she cannot see
    assert roots.resolve("cryo/session/to_light.tif", LOCAL_USER).is_file


def test_loops_and_broken_links(roots: RootSet, tree: Path):
    s = tree / "cryo/session"
    os.symlink(s / "loop_b", s / "loop_a")
    os.symlink(s / "loop_a", s / "loop_b")
    os.symlink(s / "nowhere.mrc", s / "broken.mrc")
    for name in ("loop_a", "loop_a/x", "broken.mrc"):
        with pytest.raises(NotFoundError):
            roots.resolve(f"cryo/session/{name}", ALICE)


def test_a_root_that_is_itself_a_symlink(tree: Path):
    os.symlink(tree / "cryo", tree / "cryo_link")
    rs = RootSet([RootConfig("data", tree / "cryo_link")])
    assert rs.resolve("data/session/movie.mrc", NOBODY).real == (tree / "cryo/session/movie.mrc").resolve()


def test_unavailable_root(tree: Path):
    rs = RootSet([RootConfig("gone", tree / "not_mounted"), RootConfig("public", tree / "public")])
    gone = rs.visible_roots(NOBODY)[0]
    assert not rs.available(gone) and rs.available(rs.visible_roots(NOBODY)[1])
    with pytest.raises(NotFoundError, match="data root 'gone' is not available"):
        rs.resolve("gone/file", NOBODY)
    with pytest.raises(ValueError):
        RootSet([RootConfig("a", tree), RootConfig("a", tree)])


def test_odd_but_legal_names(roots: RootSet, tree: Path):
    s = tree / "cryo/session"
    for name in ("with space.mrc", "-dash.mrc", "ünï¢ødé.mrc", "a..b.mrc", "...", "cryo", "light"):
        (s / name).write_bytes(b"x")
        assert roots.resolve(f"cryo/session/{name}", ALICE).is_file, name


def test_error_messages_never_contain_server_paths(roots: RootSet, tree: Path):
    for logical in ("cryo/missing", "cryo/../x", "light/img.tif", "/etc/passwd", "cryo/session/movie.mrc/x"):
        with pytest.raises((AccessDeniedError, NotFoundError)) as e:
            roots.resolve(logical, ALICE)
        assert str(tree) not in str(e.value) and str(tree.parent) not in str(e.value)


@settings(max_examples=400, deadline=None, derandomize=True)
@given(st.lists(
    st.sampled_from(["cryo", "session", "..", ".", "", "outside", "secret.mrc", "movie.mrc", "escape", "/",
                     "\\", "light", "%2e%2e", "..%2f", "\x00", " ", "public"]),
    min_size=1, max_size=8,
))  # fmt: skip
def test_no_path_ever_resolves_outside_the_visible_roots(
    tmp_path_factory: pytest.TempPathFactory, parts: list[str]
):
    base = tmp_path_factory.getbasetemp() / "prop"
    if not base.exists():
        (base / "cryo/session").mkdir(parents=True)
        (base / "light").mkdir()
        (base / "outside").mkdir()
        (base / "cryo/session/movie.mrc").write_bytes(b"m")
        (base / "outside/secret.mrc").write_bytes(b"s")
        os.symlink(base / "outside", base / "cryo/escape")
    rs = RootSet(
        [
            RootConfig("cryo", base / "cryo", frozenset({"cryoem"})),
            RootConfig("light", base / "light", frozenset({"lm"})),
        ]
    )
    logical = "/".join(parts)
    try:
        resolved = rs.resolve(logical, ALICE)
    except (AccessDeniedError, NotFoundError):
        return
    real_root = os.path.realpath(base / "cryo")
    assert str(resolved.real) == real_root or str(resolved.real).startswith(real_root + os.sep)


# ── listing ──────────────────────────────────────────────────────────────────


def test_listing_order_kinds_and_flags(roots: RootSet, tree: Path):
    s = tree / "cryo/session"
    for name in ("img10.mrc", "img2.mrc", "Img1.mrc", "zeta"):
        (s / name).write_bytes(b"12345") if "." in name else (s / name).mkdir()
    (s / "alpha").mkdir()
    listing = roots.list_dir("cryo/session", ALICE, supports=lambda n: n.endswith(".mrc"))
    assert [e.name for e in listing.entries] == [
        "alpha",
        "zeta",
        "Img1.mrc",
        "img2.mrc",
        "img10.mrc",
        "movie.mrc",
        "notes.txt",
    ]
    assert listing.path == "cryo/session" and listing.parent == "cryo" and listing.total == 7
    by_name = {e.name: e for e in listing.entries}
    assert by_name["alpha"].kind == "dir" and by_name["alpha"].size == 0 and not by_name["alpha"].supported
    movie = by_name["movie.mrc"]
    assert (movie.kind, movie.size, movie.supported, movie.is_symlink) == ("file", 10, True, False)
    assert movie.path == "cryo/session/movie.mrc" and abs(movie.mtime - time.time()) < 60
    assert not by_name["notes.txt"].supported
    assert roots.list_dir("cryo", ALICE).parent is None  # top of a root
    hidden = roots.list_dir("cryo/session", ALICE, show_hidden=True)
    assert ".hidden" in [e.name for e in hidden.entries]
    js = listing.to_json()
    assert js["entries"][0] == {
        "name": "alpha", "path": "cryo/session/alpha", "kind": "dir", "size": 0,
        "mtime": by_name["alpha"].mtime, "is_symlink": False, "supported": False,
    }  # fmt: skip


def test_listing_handles_links_without_breaking_or_leaking(roots: RootSet, tree: Path):
    s = tree / "cryo/session"
    os.symlink(s / "nowhere.mrc", s / "broken.mrc")
    os.symlink(tree / "outside/secret.mrc", s / "escape.mrc")
    os.symlink(tree / "outside", s / "escape_dir")
    os.symlink(tree / "outside/nothing_here", s / "broken_outside")
    os.symlink(s / "movie.mrc", s / "alias.mrc")
    os.symlink(tree / "light/img.tif", s / "to_light.tif")
    (s / "bad\nname.mrc").write_bytes(b"x")
    listing = roots.list_dir("cryo/session", ALICE)
    names = {e.name: e for e in listing.entries}
    assert set(names) == {"alias.mrc", "broken.mrc", "movie.mrc", "notes.txt"}
    assert names["broken.mrc"].kind == "broken" and names["broken.mrc"].is_symlink
    assert (
        names["alias.mrc"].kind == "file" and names["alias.mrc"].is_symlink and names["alias.mrc"].size == 10
    )
    # the local user sees every root, hence the link into "light"
    assert "to_light.tif" in {e.name for e in roots.list_dir("cryo/session", LOCAL_USER).entries}


def test_listing_errors_and_pagination(roots: RootSet, tree: Path):
    with pytest.raises(InvalidRequestError, match=r"not a directory: cryo/session/movie\.mrc"):
        roots.list_dir("cryo/session/movie.mrc", ALICE)
    with pytest.raises(AccessDeniedError):
        roots.list_dir("light", ALICE)
    with pytest.raises(NotFoundError):
        roots.list_dir("cryo/nope", ALICE)
    d = tree / "public/many"
    d.mkdir()
    for i in range(25):
        (d / f"f{i}.png").write_bytes(b"")
    page = roots.list_dir("public/many", NOBODY, offset=10, limit=5)
    assert [e.name for e in page.entries] == [f"f{i}.png" for i in range(10, 15)]
    assert (page.total, page.offset, page.limit) == (25, 10, 5)
    assert roots.list_dir("public/many", NOBODY, offset=100).entries == ()
    for kw in ({"offset": -1}, {"limit": 0}, {"limit": 10_001}, {"offset": 1.5}, {"limit": True}):
        with pytest.raises(InvalidRequestError):
            roots.list_dir("public/many", NOBODY, **kw)  # type: ignore[arg-type]


def test_large_directories_list_quickly(roots: RootSet, tree: Path):
    d = tree / "public/big"
    d.mkdir()
    for i in range(5000):
        (d / f"FoilHole_{i}_Data.mrc").touch()
    t0 = time.perf_counter()
    listing = roots.list_dir("public/big", NOBODY, limit=100)
    elapsed = time.perf_counter() - t0
    assert listing.total == 5000 and len(listing.entries) == 100
    assert (
        listing.entries[1].name == "FoilHole_1_Data.mrc"
        and listing.entries[10].name == "FoilHole_10_Data.mrc"
    )
    assert elapsed < 1.0, f"{elapsed:.2f} s for 5000 entries"
