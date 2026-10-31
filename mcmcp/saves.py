"""Persistent saved voxel designs."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import TypeAlias

import numpy as np

try:
    from . import config
except ImportError:
    from mcmcp import config

Box: TypeAlias = tuple[int, int, int, int, int, int, str]
Design: TypeAlias = tuple[np.ndarray, list[str], list[Box]]

_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def valid_name(name: str) -> bool:
    """Return whether name is safe and valid for a saved design."""
    return bool(isinstance(name, str) and _NAME_RE.fullmatch(name))


def _path(name: str) -> Path:
    if not valid_name(name):
        raise ValueError("Invalid saved design name.")
    return Path(config.WORK_DIR) / "saved" / f"{name}.npz"


def save(name: str, design: Design) -> bool:
    """Save design under name and return whether an existing file was replaced."""
    path = _path(name)
    replaced = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    grid, palette, dboxes = design
    dboxes_json = json.dumps([list(box) for box in dboxes], separators=(",", ":"))
    np.savez_compressed(
        path,
        grid=np.asarray(grid),
        palette=np.asarray(palette, dtype=str),
        dboxes=np.asarray(dboxes_json),
    )
    return replaced


def load(name: str) -> Design:
    """Load a saved design by name."""
    path = _path(name)
    if not path.exists():
        raise FileNotFoundError(name)
    try:
        with np.load(path, allow_pickle=False) as data:
            grid = np.asarray(data["grid"])
            palette_array = np.asarray(data["palette"])
            palette = [str(value) for value in palette_array.tolist()]
            raw = np.asarray(data["dboxes"]).item()
        parsed = json.loads(str(raw))
    except Exception as exc:
        raise ValueError("Saved design is invalid.") from exc
    if grid.ndim != 3 or not np.issubdtype(grid.dtype, np.integer):
        raise ValueError("Saved design is invalid.")
    if palette_array.ndim != 1 or not np.issubdtype(palette_array.dtype, np.str_):
        raise ValueError("Saved design is invalid.")
    if not isinstance(parsed, list):
        raise ValueError("Saved design is invalid.")
    dboxes: list[Box] = []
    for box in parsed:
        if (
            not isinstance(box, (list, tuple))
            or len(box) != 7
            or not all(isinstance(value, int) and not isinstance(value, bool) for value in box[:6])
            or not isinstance(box[6], str)
        ):
            raise ValueError("Saved design is invalid.")
        dboxes.append(tuple(box))
    return grid, palette, dboxes


def list_saved() -> list[str]:
    """Return valid saved design names in sorted order."""
    directory = Path(config.WORK_DIR) / "saved"
    if not directory.is_dir():
        return []
    return sorted(
        path.stem
        for path in directory.iterdir()
        if path.is_file() and path.suffix == ".npz" and valid_name(path.stem)
    )

