"""The second test server, in proxy mode, for what standalone mode has not (personal keys):
python proxy_fixtures.py OUTPUT_DIR writes its own data root, its configuration
(``imagebio.toml``) and an empty state directory. Independent of make_fixtures.py: both servers
start at the same time. The tests play the proxy by sending X-Forwarded-User."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def main(out: Path) -> None:
    out = out.resolve()
    shutil.rmtree(out, ignore_errors=True)  # no key left over from a previous run
    (out / "data" / "shared").mkdir(parents=True)
    (out / "data" / "shared" / "notes.txt").write_text("not an image\n")
    (out / "imagebio.toml").write_text(
        f'[auth]\nmode = "proxy"\nadmins = ["root"]\n\n[live]\nstate_dir = "{out / "state"}"\n\n'
        f'[[roots]]\nname = "lab"\npath = "{out / "data"}"\n'
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
