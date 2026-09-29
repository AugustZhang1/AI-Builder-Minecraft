"""OpenSCAD design -> block grid -> /fill boxes -> world coordinates. Pure geometry, no RCON.

Design axes: X = right, Y = away from the player (the front is at y=0), Z = up; 1 unit = 1 block.
Minecraft: Y is up. Player yaw 0 = south (+Z), 90 = west, 180 = north, 270 / -90 = east.
"""
from pathlib import Path
import re
import subprocess

import numpy as np
import trimesh

try:
    from . import config
except ImportError:
    import config

Box = tuple[int, int, int, int, int, int, int]  # x1, y1, z1, x2, y2, z2 (inclusive), value

FORBIDDEN_SCAD = ("import(", "include <", "use <", "surface(")
PART_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")

FORWARD_VECTORS: dict[str, tuple[int, int, int]] = {
    "south": (0, 0, 1),
    "west": (-1, 0, 0),
    "north": (0, 0, -1),
    "east": (1, 0, 0),
}


class BuildError(Exception):
    pass


def render_parts(scad_text: str, parts: list[str], build_dir: Path) -> dict[str, Path]:
    """Write design.scad into build_dir and export one STL per part (-D part="name").
    Returns part -> STL path; parts that render empty are left out."""
    if any(pat in scad_text for pat in FORBIDDEN_SCAD) or re.search(
        r"\b(import\s*\(|include\s*<|use\s*<|surface\s*\()", scad_text
    ):
        raise BuildError("OpenSCAD code contains forbidden construct (import, include, use, or surface)")

    for name in parts:
        if not PART_NAME_RE.match(name):
            raise BuildError(f"Invalid part name {name!r}: must match ^[a-z0-9_]{{1,32}}$")

    build_dir = Path(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    scad_file = build_dir / "design.scad"
    scad_file.write_text(scad_text, encoding="utf-8")

    cmd_prefix = [item.replace("{cwd}", str(build_dir)) for item in config.OPENSCAD_CMD]
    stls: dict[str, Path] = {}

    for name in parts:
        stl_path = build_dir / f"{name}.stl"
        if stl_path.exists():
            stl_path.unlink()

        cmd = cmd_prefix + [
            "--backend=manifold",
            "-D",
            f'part="{name}"',
            "-o",
            f"{name}.stl",
            "design.scad",
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=build_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=config.OPENSCAD_TIMEOUT,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            raise BuildError(f"OpenSCAD timed out rendering part {name!r}")
        except Exception as e:
            raise BuildError(f"Failed to execute OpenSCAD for part {name!r}: {e}")

        stdout = proc.stdout or ""
        if "Current top level object is empty" in stdout:
            continue

        if proc.returncode != 0:
            lines = stdout.strip().splitlines()
            last_lines = "\n".join(lines[-10:]) if lines else f"exit code {proc.returncode}"
            raise BuildError(f"OpenSCAD failed on part {name!r} (exit code {proc.returncode}):\n{last_lines}")

        if not stl_path.exists() or stl_path.stat().st_size == 0:
            continue

        stls[name] = stl_path

    return stls


def voxelize(stls: dict[str, Path], blocks: dict[str, str], max_size: int) -> tuple[np.ndarray, list[str]]:
    """Voxelize all parts into one shared grid of shape (X, Y, Z).
    Returns (grid, palette): 0 = air, i = palette[i], palette[0] == "minecraft:air"."""
    loaded_meshes: dict[str, trimesh.Trimesh] = {}
    for part_name, block_id in blocks.items():
        if part_name in stls:
            path = stls[part_name]
            try:
                m = trimesh.load(str(path))
                if isinstance(m, trimesh.Scene):
                    m = trimesh.util.concatenate(m.dump())
                if m is not None and not m.is_empty and len(m.faces) > 0:
                    loaded_meshes[part_name] = m
            except Exception:
                pass

    if not loaded_meshes:
        raise BuildError("No solid geometry to voxelize")

    mins = np.array([m.bounds[0] for m in loaded_meshes.values()])
    maxs = np.array([m.bounds[1] for m in loaded_meshes.values()])
    union_min = np.min(mins, axis=0)
    union_max = np.max(maxs, axis=0)

    # Measure from the grid origin (floor of the min corner) so the scaled design never
    # needs more than max_size cells on any axis.
    grid_min = np.floor(union_min)
    max_extent = float(np.max(union_max - grid_min))

    if max_extent > max_size:
        scale = max_size / max_extent
        for m in loaded_meshes.values():
            m.apply_translation(-grid_min)
            m.apply_scale(scale)
            m.apply_translation(grid_min)
        mins = np.array([m.bounds[0] for m in loaded_meshes.values()])
        maxs = np.array([m.bounds[1] for m in loaded_meshes.values()])
        union_min = np.min(mins, axis=0)
        union_max = np.max(maxs, axis=0)

    origin = np.floor(union_min).astype(int)
    max_bound = np.ceil(union_max).astype(int)
    dims = np.maximum(max_bound - origin, 1)

    grid = np.zeros(dims, dtype=np.uint8)

    palette = ["minecraft:air"]
    block_to_id: dict[str, int] = {}
    for part_name, block_id in blocks.items():
        if part_name in loaded_meshes and block_id not in block_to_id:
            block_to_id[block_id] = len(palette)
            palette.append(block_id)

    if len(palette) > 256:
        raise BuildError("Too many distinct block types (max 255)")

    for part_name, block_id in blocks.items():
        if part_name not in loaded_meshes:
            continue
        mesh = loaded_meshes[part_name]
        val = block_to_id[block_id]

        mb_min = mesh.bounds[0]
        mb_max = mesh.bounds[1]

        i_min = max(0, int(np.floor(mb_min[0] - origin[0])) - 1)
        i_max = min(dims[0], int(np.ceil(mb_max[0] - origin[0])) + 2)

        j_min = max(0, int(np.floor(mb_min[1] - origin[1])) - 1)
        j_max = min(dims[1], int(np.ceil(mb_max[1] - origin[1])) + 2)

        k_min = max(0, int(np.floor(mb_min[2] - origin[2])) - 1)
        k_max = min(dims[2], int(np.ceil(mb_max[2] - origin[2])) + 2)

        if i_min >= i_max or j_min >= j_max or k_min >= k_max:
            continue

        xs = origin[0] + np.arange(i_min, i_max) + 0.5
        ys = origin[1] + np.arange(j_min, j_max) + 0.5
        zs = origin[2] + np.arange(k_min, k_max) + 0.5

        gx, gy, gz = np.meshgrid(xs, ys, zs, indexing="ij")
        pts = np.stack([gx, gy, gz], axis=-1).reshape(-1, 3)

        mask = np.asarray(mesh.contains(pts), dtype=bool)
        if not np.any(mask):
            continue

        sub_mask = mask.reshape(len(xs), len(ys), len(zs))
        idx_i, idx_j, idx_k = np.nonzero(sub_mask)
        grid[i_min + idx_i, j_min + idx_j, k_min + idx_k] = val

    occupied = np.argwhere(grid > 0)
    if len(occupied) == 0:
        raise BuildError("No solid blocks in voxelized geometry")

    min_pos = occupied.min(axis=0)
    max_pos = occupied.max(axis=0)

    trimmed_grid = grid[
        min_pos[0] : max_pos[0] + 1,
        min_pos[1] : max_pos[1] + 1,
        min_pos[2] : max_pos[2] + 1,
    ].copy()

    return trimmed_grid, palette


def _split_box(box: Box, max_volume: int) -> list[Box]:
    x1, y1, z1, x2, y2, z2, val = box
    vol = (x2 - x1 + 1) * (y2 - y1 + 1) * (z2 - z1 + 1)
    if vol <= max_volume:
        return [box]

    dx = x2 - x1 + 1
    dy = y2 - y1 + 1
    dz = z2 - z1 + 1

    if dx >= dy and dx >= dz:
        mid = x1 + dx // 2
        b1 = (x1, y1, z1, mid - 1, y2, z2, val)
        b2 = (mid, y1, z1, x2, y2, z2, val)
    elif dy >= dz:
        mid = y1 + dy // 2
        b1 = (x1, y1, z1, x2, mid - 1, z2, val)
        b2 = (x1, mid, z1, x2, y2, z2, val)
    else:
        mid = z1 + dz // 2
        b1 = (x1, y1, z1, x2, y2, mid - 1, val)
        b2 = (x1, y1, mid, x2, y2, z2, val)

    return _split_box(b1, max_volume) + _split_box(b2, max_volume)


def boxes(grid: np.ndarray, max_volume: int) -> list[Box]:
    """Merge same-value cells into boxes in grid coordinates, each at most max_volume blocks. Air is skipped."""
    X, Y, Z = grid.shape
    visited = np.zeros((X, Y, Z), dtype=bool)
    result: list[Box] = []

    for z in range(Z):
        for y in range(Y):
            for x in range(X):
                val = int(grid[x, y, z])
                if val == 0 or visited[x, y, z]:
                    continue

                # 1. Grow along x
                x2 = x
                while x2 + 1 < X and not visited[x2 + 1, y, z] and grid[x2 + 1, y, z] == val:
                    x2 += 1

                # 2. Grow along y
                y2 = y
                while y2 + 1 < Y:
                    row_v = visited[x : x2 + 1, y2 + 1, z]
                    row_g = grid[x : x2 + 1, y2 + 1, z]
                    if not np.any(row_v) and np.all(row_g == val):
                        y2 += 1
                    else:
                        break

                # 3. Grow along z
                z2 = z
                while z2 + 1 < Z:
                    slice_v = visited[x : x2 + 1, y : y2 + 1, z2 + 1]
                    slice_g = grid[x : x2 + 1, y : y2 + 1, z2 + 1]
                    if not np.any(slice_v) and np.all(slice_g == val):
                        z2 += 1
                    else:
                        break

                visited[x : x2 + 1, y : y2 + 1, z : z2 + 1] = True
                raw_box = (x, y, z, x2, y2, z2, val)
                result.extend(_split_box(raw_box, max_volume))

    return result


def facing_from_yaw(yaw: float) -> str:
    """Snap a Minecraft yaw to "south", "west", "north" or "east"."""
    facings = ["south", "west", "north", "east"]
    return facings[int(round(yaw / 90)) % 4]


def to_world(
    x: int,
    y: int,
    z: int,
    size_x: int,
    origin: tuple[int, int, int],
    facing: str,
    gap: int,
) -> tuple[int, int, int]:
    """Design cell -> Minecraft block position. origin is the player's feet block."""
    if facing not in FORWARD_VECTORS:
        raise ValueError(f"Unknown facing: {facing!r}")
    fx, fy, fz = FORWARD_VECTORS[facing]
    rx, ry, rz = -fz, 0, fx

    ox, oy, oz = origin
    dx = x - size_x // 2
    dy = gap + y

    wx = ox + rx * dx + fx * dy
    wy = oy + z
    wz = oz + rz * dx + fz * dy
    return (int(wx), int(wy), int(wz))


def box_to_world(
    box: Box,
    size_x: int,
    origin: tuple[int, int, int],
    facing: str,
    gap: int,
) -> tuple[int, int, int, int, int, int]:
    """Design box -> Minecraft (x1, y1, z1, x2, y2, z2) with x1<=x2, y1<=y2, z1<=z2."""
    bx1, by1, bz1, bx2, by2, bz2, _val = box
    w1_x, w1_y, w1_z = to_world(bx1, by1, bz1, size_x, origin, facing, gap)
    w2_x, w2_y, w2_z = to_world(bx2, by2, bz2, size_x, origin, facing, gap)
    return (
        min(w1_x, w2_x),
        min(w1_y, w2_y),
        min(w1_z, w2_z),
        max(w1_x, w2_x),
        max(w1_y, w2_y),
        max(w1_z, w2_z),
    )
