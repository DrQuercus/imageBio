"""``/metrics``: what a monitoring system needs to know of a running server, in the text
format Prometheus reads.

Nothing here names a file, a directory or a user: watched directories are numbered. The route
is off unless the configuration turns it on (``[server] metrics = true``), and then answers
only a client on this computer, or one that presents the token of the environment variable
``IMAGEBIO_METRICS_TOKEN`` (a scraper on another machine or in another container).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from imagebio import __version__

__all__ = ["CONTENT_TYPE", "TOKEN_VARIABLE", "render_metrics"]

CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"
TOKEN_VARIABLE: Final = "IMAGEBIO_METRICS_TOKEN"  # noqa: S105 - the name of a variable, not a secret

Sample = tuple[Mapping[str, str], float]


def _metric(name: str, kind: str, text: str, samples: Iterable[Sample]) -> list[str]:
    lines = [f"# HELP {name} {text}", f"# TYPE {name} {kind}"]
    for labels, value in samples:
        inside = ",".join(f'{key}="{labels[key]}"' for key in sorted(labels))
        number = repr(float(value)) if value != int(value) else str(int(value))
        lines.append(f"{name}{{{inside}}} {number}" if inside else f"{name} {number}")
    return lines


def render_metrics(caches: Mapping[str, Mapping[str, int]], live: Mapping[str, Any]) -> str:
    """The exposition text. ``caches``: ``PlaneService.cache_stats()``; ``live``:
    ``LiveService.metrics()`` (empty when the Live mode was never used)."""
    planes, datasets = caches["planes"], caches["datasets"]
    out: list[str] = []
    out += _metric("imagebio_info", "gauge", "Version of imageBio.", [({"version": __version__}, 1)])
    out += _metric(
        "imagebio_plane_cache_bytes", "gauge", "Bytes of pixels held in memory.", [({}, planes["bytes"])]
    )
    out += _metric(
        "imagebio_plane_cache_max_bytes", "gauge", "Limit of the plane cache.", [({}, planes["max_bytes"])]
    )
    out += _metric(
        "imagebio_plane_cache_requests_total",
        "counter",
        "Planes asked of the cache, by outcome.",
        [({"outcome": "hit"}, planes["hits"]), ({"outcome": "miss"}, planes["misses"])],
    )
    out += _metric(
        "imagebio_plane_cache_evictions_total",
        "counter",
        "Planes dropped to make room.",
        [({}, planes["evictions"])],
    )
    out += _metric("imagebio_open_datasets", "gauge", "Files held open.", [({}, datasets["open"])])
    out += _metric("imagebio_dataset_opens_total", "counter", "Files opened.", [({}, datasets["opens"])])
    if not live:
        return "\n".join(out) + "\n"
    out += _metric(
        "imagebio_live_watches",
        "gauge",
        "Watches of the Live mode, by state.",
        [({"state": state}, count) for state, count in sorted(live["watches"].items())],
    )
    roots: list[Mapping[str, Any]] = live["roots"]
    per_root = (
        ("scan_age_seconds", "Seconds since the last scan cycle of a watched directory.", "age"),
        ("scan_duration_seconds", "Duration of its last scan cycle.", "duration"),
        ("scan_failing", "1 when its last scan cycle failed.", "failing"),
        ("pending_files", "Files being followed until they are complete.", "pending"),
        ("exposures", "Exposures in its index.", "exposures"),
    )
    for name, text, key in per_root:
        samples = [({"root": str(r["id"])}, r[key]) for r in roots if r.get(key) is not None]
        out += _metric(f"imagebio_live_{name}", "gauge", text, samples)
    out += _metric(
        "imagebio_live_index_bytes", "gauge", "Size of the Live index.", [({}, live["index_bytes"])]
    )
    out += _metric(
        "imagebio_live_thumbnail_cache_bytes",
        "gauge",
        "Thumbnails kept in the index.",
        [({}, live["thumb_bytes"])],
    )
    return "\n".join(out) + "\n"
