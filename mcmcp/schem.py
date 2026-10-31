"""Small Sponge schematic v2 writer."""
from __future__ import annotations

import gzip
from pathlib import Path
import struct

import numpy as np

try:
    from . import build
except ImportError:
    from mcmcp import build


def _name(name: str) -> bytes:
    encoded = name.encode("utf-8")
    return struct.pack(">H", len(encoded)) + encoded


def _tag(kind: int, name: str, payload: bytes) -> bytes:
    return bytes((kind,)) + _name(name) + payload


def _compound(items: list[bytes]) -> bytes:
    return b"".join(items) + b"\x00"


def _int(value: int) -> bytes:
    return struct.pack(">i", value)


def _short(value: int) -> bytes:
    return struct.pack(">H", value)


def _int_array(values: list[int]) -> bytes:
    return _int(len(values)) + b"".join(_int(v) for v in values)


def _byte_array(value: bytes) -> bytes:
    return _int(len(value)) + value


def _empty_list(kind: int = 10) -> bytes:
    return bytes((kind,)) + _int(0)


def _varint(value: int) -> bytes:
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def write_schem(
    path: str | Path,
    grid: np.ndarray,
    palette: list[str],
    dboxes: list[tuple[int, int, int, int, int, int, str]],
) -> int:
    """Write a gzipped Sponge schematic and return its non-air block count.

    Grid coordinates are (x, y, z), with z up and +y away from the player. The
    schematic uses (x, y, z) = (grid x, grid z, max-y-grid y).
    """
    if grid.ndim != 3:
        raise ValueError("grid must be three-dimensional")
    sx, sy, sz = (int(n) for n in grid.shape)
    if not (0 < sx <= 65535 and 0 < sy <= 65535 and 0 < sz <= 65535):
        raise ValueError("schematic dimensions must be between 1 and 65535")

    stamped, stamped_palette = build.stamp_details(grid, palette, dboxes, preserve_states=True)
    export_palette: list[str] = []
    remap: dict[int, int] = {}
    by_name: dict[str, int] = {}
    for old, block in enumerate(stamped_palette):
        block = build.with_persistent_leaves(block)
        new = by_name.setdefault(block, len(export_palette))
        if new == len(export_palette):
            export_palette.append(block)
        remap[old] = new
    if "minecraft:air" not in by_name:
        by_name["minecraft:air"] = len(export_palette)
        export_palette.append("minecraft:air")

    block_data = bytearray()
    non_air = 0
    for grid_z in range(sz):
        for grid_y in range(sy - 1, -1, -1):
            for grid_x in range(sx):
                index = remap[int(stamped[grid_x, grid_y, grid_z])]
                block_data.extend(_varint(index))
                if export_palette[index] != "minecraft:air":
                    non_air += 1

    palette_tag = _compound(
        [_tag(3, block, _int(index)) for index, block in enumerate(export_palette)]
    )
    root = _compound(
        [
            _tag(3, "Version", _int(2)),
            _tag(3, "DataVersion", _int(3955)),
            _tag(2, "Width", _short(sx)),
            _tag(2, "Height", _short(sz)),
            _tag(2, "Length", _short(sy)),
            _tag(11, "Offset", _int_array([0, 0, 0])),
            _tag(10, "Palette", palette_tag),
            _tag(3, "PaletteMax", _int(len(export_palette))),
            _tag(7, "BlockData", _byte_array(bytes(block_data))),
            _tag(9, "BlockEntities", _empty_list()),
            _tag(9, "Entities", _empty_list()),
            _tag(10, "Metadata", _compound([])),
        ]
    )
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(destination, "wb") as output:
        output.write(bytes((10,)) + _name("Schematic") + root)
    return non_air
