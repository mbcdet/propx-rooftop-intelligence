"""Thin shim: the cache builder now lives in the package as ``propx_roofs.cache_build``.

Kept so the historical invocation ``python3 tools/build_cache.py [--verify]`` still works from
a checkout. The real implementation is importable from an installed wheel (RTI-002), and the
preferred entry points are:

    propx-roofs cache-build                    # fetch, then verify   (needs network)
    propx-roofs cache-verify                   # verify only          (offline)
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from propx_roofs.cache_build import (  # noqa: E402,F401 - re-exported for callers of the old path
    BBOX_LAYERS,
    FMZK_FILE,
    TILE_PX,
    CacheError,
    _needed_tiles,
    _prune_stale_tiles,
    _tile_problem,
    cache_hash,
    fetch,
    integrity_block,
    main,
    verify,
)

if __name__ == "__main__":
    raise SystemExit(main())
