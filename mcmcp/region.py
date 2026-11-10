"""Read a box of blocks straight from a world's region files."""
from __future__ import annotations

from pathlib import Path
import struct
from typing import NamedTuple
import zlib

import numpy as np

Box = tuple[int, int, int, int, int, int]

AIR = "minecraft:air"
AIRS = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air"}


class RegionError(Exception):
    """The box could not be read from the world."""


class Snapshot(NamedTuple):
    grid: np.ndarray      # uint16 (X, Y, Z), local coordinates
    palette: list[str]    # [0] air, then the other blocks sorted
    locked: np.ndarray    # bool (X, Y, Z): block entities


def region_dir(world_dir: Path, dim: str) -> Path:
    """Folder holding the region files of a dimension."""
    if dim == "minecraft:overworld":
        return Path(world_dir) / "region"
    if dim == "minecraft:the_nether":
        return Path(world_dir) / "DIM-1" / "region"
    if dim == "minecraft:the_end":
        return Path(world_dir) / "DIM1" / "region"
    namespace, _, path = dim.partition(":")
    return Path(world_dir) / "dimensions" / namespace / path / "region"


class _Reader:
    """Big-endian NBT reader that returns plain dicts, lists and numpy arrays."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, fmt: str):
        values = struct.unpack_from(">" + fmt, self.data, self.pos)
        self.pos += struct.calcsize(">" + fmt)
        return values[0]

    def string(self) -> str:
        size = self.take("H")
        text = self.data[self.pos:self.pos + size]
        if len(text) != size:
            raise struct.error("short string")
        self.pos += size
        return text.decode("utf-8", "replace")

    def array(self, dtype: str, size: int) -> np.ndarray:
        width = np.dtype(dtype).itemsize
        raw = self.data[self.pos:self.pos + size * width]
        if len(raw) != size * width:
            raise struct.error("short array")
        self.pos += size * width
        return np.frombuffer(raw, dtype=dtype)

    def payload(self, kind: int):
        if kind == 1:
            return self.take("b")
        if kind == 2:
            return self.take("h")
        if kind == 3:
            return self.take("i")
        if kind == 4:
            return self.take("q")
        if kind == 5:
            return self.take("f")
        if kind == 6:
            return self.take("d")
        if kind == 7:
            return self.array("i1", self.take("i"))
        if kind == 8:
            return self.string()
        if kind == 9:
            item = self.take("B")
            return [self.payload(item) for _ in range(self.take("i"))]
        if kind == 10:
            result = {}
            while True:
                tag = self.take("B")
                if tag == 0:
                    return result
                name = self.string()
                result[name] = self.payload(tag)
        if kind == 11:
            return self.array(">i4", self.take("i"))
        if kind == 12:
            return self.array(">i8", self.take("i")).astype(np.uint64)
        raise struct.error(f"unknown tag {kind}")


def _parse_nbt(data: bytes) -> dict:
    reader = _Reader(data)
    if reader.take("B") != 10:
        raise struct.error("root is not a compound")
    reader.string()
    return reader.payload(10)


def _read_chunk(rdir: Path, cx: int, cz: int) -> dict:
    """Return the NBT of one chunk."""
    path = rdir / f"r.{cx >> 5}.{cz >> 5}.mca"
    if not path.is_file():
        raise RegionError(f"chunk {cx},{cz} is not saved yet (no region file)")
    try:
        with open(path, "rb") as f:
            f.seek(4 * ((cx & 31) + (cz & 31) * 32))
            entry = struct.unpack(">I", f.read(4))[0]
            offset = entry >> 8
            if offset == 0:
                raise RegionError(f"chunk {cx},{cz} is not saved yet")
            f.seek(offset * 4096)
            length, kind = struct.unpack(">IB", f.read(5))
            if kind & 0x80:
                raise RegionError(f"chunk {cx},{cz} is too large to read (.mcc)")
            raw = f.read(length - 1)
        if len(raw) != length - 1:
            raise struct.error("short chunk")
        if kind == 1:
            raw = zlib.decompress(raw, 31)
        elif kind == 2:
            raw = zlib.decompress(raw)
        elif kind == 4:
            raise RegionError(f"chunk {cx},{cz} uses LZ4 compression, which is not supported")
        elif kind != 3:
            raise RegionError(f"chunk {cx},{cz} has unknown compression {kind}")
        chunk = _parse_nbt(raw)
        chunk["sections"]
    except (struct.error, zlib.error, KeyError, IndexError):
        raise RegionError(f"chunk {cx},{cz} is corrupt or has no block data") from None
    return chunk


def _state(entry: dict) -> str:
    name = entry["Name"]
    if name in AIRS:
        return AIR
    props = entry.get("Properties")
    if not props:
        return name
    return name + "[" + ",".join(f"{k}={props[k]}" for k in sorted(props)) + "]"


def _unpack(states: dict, count: int) -> np.ndarray:
    """Return the 4096 palette indexes of a section as (x, y, z)."""
    if count == 1 or "data" not in states:
        return np.zeros((16, 16, 16), dtype=np.int64)
    bits = max(4, (count - 1).bit_length())
    per = 64 // bits
    longs = states["data"]
    i = np.arange(4096)
    shift = ((i % per) * bits).astype(np.uint64)
    values = (longs[i // per] >> shift) & np.uint64((1 << bits) - 1)
    return values.astype(np.int64).reshape(16, 16, 16).transpose(2, 0, 1)


def read_box(world_dir: Path, dim: str, box: Box) -> Snapshot:
    """Read the blocks of an inclusive world box into local coordinates.

    Local x is east, y is north, z is up: world = (x1 + x, y1 + z, z2 - y).
    """
    x1, y1, z1, x2, y2, z2 = box
    if x1 > x2 or y1 > y2 or z1 > z2:
        raise RegionError("box must have min <= max on every axis")
    rdir = region_dir(world_dir, dim)
    names = [AIR]
    ids = {AIR: 0}
    world = np.zeros((x2 - x1 + 1, y2 - y1 + 1, z2 - z1 + 1), dtype=np.int64)
    locked = np.zeros(world.shape, dtype=bool)

    for cx in range(x1 >> 4, (x2 >> 4) + 1):
        for cz in range(z1 >> 4, (z2 >> 4) + 1):
            chunk = _read_chunk(rdir, cx, cz)
            x_lo, x_hi = max(x1, cx * 16), min(x2, cx * 16 + 15)
            z_lo, z_hi = max(z1, cz * 16), min(z2, cz * 16 + 15)
            try:
                for section in chunk["sections"]:
                    sy = int(section["Y"])
                    y_lo, y_hi = max(y1, sy * 16), min(y2, sy * 16 + 15)
                    if y_lo > y_hi or "block_states" not in section:
                        continue
                    states = section["block_states"]
                    remap = []
                    for entry in states["palette"]:
                        state = _state(entry)
                        if state not in ids:
                            ids[state] = len(names)
                            names.append(state)
                        remap.append(ids[state])
                    blocks = np.array(remap, dtype=np.int64)[_unpack(states, len(remap))]
                    world[x_lo - x1:x_hi - x1 + 1, y_lo - y1:y_hi - y1 + 1, z_lo - z1:z_hi - z1 + 1] = blocks[
                        x_lo - cx * 16:x_hi - cx * 16 + 1,
                        y_lo - sy * 16:y_hi - sy * 16 + 1,
                        z_lo - cz * 16:z_hi - cz * 16 + 1,
                    ]
                for entity in chunk.get("block_entities", []):
                    x, y, z = int(entity["x"]), int(entity["y"]), int(entity["z"])
                    if x1 <= x <= x2 and y1 <= y <= y2 and z1 <= z <= z2:
                        locked[x - x1, y - y1, z - z1] = True
            except (struct.error, KeyError, IndexError, TypeError, ValueError):
                raise RegionError(f"chunk {cx},{cz} is corrupt or has no block data") from None

    order = ["minecraft:air"] + sorted(names[1:])
    new_id = np.array([order.index(n) for n in names], dtype=np.uint16)
    grid = new_id[world].transpose(0, 2, 1)[:, ::-1, :]
    return Snapshot(np.ascontiguousarray(grid), order, np.ascontiguousarray(locked.transpose(0, 2, 1)[:, ::-1, :]))
