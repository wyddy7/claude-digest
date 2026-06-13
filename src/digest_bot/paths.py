"""Filesystem anchors for the digest_bot package.

`config/` and `migrations/` deliberately live at the REPO ROOT, not inside
the package — the homelab bind-mount maps `…/repo/config → /app/config`, and
the package itself lives at `/app/src/digest_bot/`. So a `Path(__file__).parent`
anchor (which would point inside the package) is wrong after the src-layout
move. This module locates the repo root robustly and exposes `CONFIG_DIR`.

Local layout:     <repo>/src/digest_bot/paths.py  → repo root = parents[2]
Container layout: /app/src/digest_bot/paths.py     → repo root = /app  (parents[2]),
                  with config at /app/config and the bind-mount on top.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()


def _repo_root() -> Path:
    """Walk up until we find the dir that holds config/ (or pyproject.toml).

    Falls back to parents[2] (the known src-layout depth) if no marker is
    found — both local and container layouts satisfy the marker walk, so the
    fallback is defensive only.
    """
    for parent in _HERE.parents:
        if (parent / "config").is_dir() or (parent / "pyproject.toml").is_file():
            return parent
    return _HERE.parents[2]


REPO_ROOT = _repo_root()
CONFIG_DIR = REPO_ROOT / "config"
