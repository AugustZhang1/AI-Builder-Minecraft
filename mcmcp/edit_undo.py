"""One-level undo snapshot per player for !edit."""
from __future__ import annotations

from pathlib import Path
import re
from typing import NamedTuple, TypeAlias

import numpy as np

try:
    from . import config
except ImportError:
    from mcmcp import config

Box: TypeAlias = tuple[int, int, int, int, int, int]

_PLAYER_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


class Undo(NamedTuple):
    dim: str
    box: Box
    grid: np.ndarray
    palette: list[str]
    changed: np.ndarray


def _path(player: str) -> Path:
    if not isinstance(player, str) or not _PLAYER_RE.fullmatch(player):
        raise ValueError("Invalid player name.")
    return Path(config.WORK_DIR) / "undo" / f"{player.lower()}.npz"


def save(player: str, undo: Undo) -> None:
    """Save the undo snapshot for player, replacing any earlier one."""
    path = _path(player)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        dim=np.asarray(undo.dim, dtype=str),
        box=np.asarray(undo.box, dtype=np.int64),
        grid=np.asarray(undo.grid),
        palette=np.asarray(undo.palette, dtype=str),
        changed=np.asarray(undo.changed),
    )


def load(player: str) -> Undo:
    """Load the undo snapshot for player."""
    path = _path(player)
    if not path.exists():
        raise FileNotFoundError(player)
    try:
        with np.load(path, allow_pickle=False) as data:
            dim_array = np.asarray(data["dim"])
            box_array = np.asarray(data["box"])
            grid = np.asarray(data["grid"])
            palette_array = np.asarray(data["palette"])
            changed = np.asarray(data["changed"])
    except Exception as exc:
        raise ValueError("Undo file is invalid.") from exc
    if dim_array.ndim != 0 or not np.issubdtype(dim_array.dtype, np.str_):
        raise ValueError("Undo file is invalid.")
    if box_array.shape != (6,) or not np.issubdtype(box_array.dtype, np.integer):
        raise ValueError("Undo file is invalid.")
    if grid.ndim != 3 or grid.dtype != np.uint16:
        raise ValueError("Undo file is invalid.")
    if changed.dtype != np.bool_ or changed.shape != grid.shape:
        raise ValueError("Undo file is invalid.")
    if palette_array.ndim != 1 or not np.issubdtype(palette_array.dtype, np.str_):
        raise ValueError("Undo file is invalid.")
    palette = [str(value) for value in palette_array.tolist()]
    if not palette or palette[0] != "minecraft:air":
        raise ValueError("Undo file is invalid.")
    box = tuple(int(value) for value in box_array.tolist())
    return Undo(str(dim_array.item()), box, grid, palette, changed)


def clear(player: str) -> None:
    """Delete the undo snapshot for player, if any."""
    _path(player).unlink(missing_ok=True)
