"""OpenSCAD design -> block grid -> /fill boxes -> world coordinates. Pure geometry, no RCON.

Design axes: X = right, Y = away from the player (the front is at y=0), Z = up; 1 unit = 1 block.
Minecraft: Y is up. Player yaw 0 = south (+Z), 90 = west, 180 = north, 270 / -90 = east.
"""
import functools
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess

import numpy as np
from PIL import Image
from scipy import ndimage
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


def _load_meshes(
    stls: dict[str, Path],
    blocks: dict[str, str],
    limits: np.ndarray,
) -> tuple[dict[str, trimesh.Trimesh], float]:
    """Load the mesh of every part in blocks that has a non-empty STL and, if the union is larger
    than limits (wide_x, deep_y, tall_z), scale them all down to fit. Returns (part -> mesh, scale)."""
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

    union_min, union_max = _union_bounds(loaded_meshes)

    # Measure from the grid origin (floor of the min corner) so the scaled design never
    # needs more than limit cells on any axis.
    grid_min = np.floor(union_min)
    extents = union_max - grid_min
    scale = 1.0

    if np.any(extents > limits):
        scale = float(np.min(limits / extents))
        for m in loaded_meshes.values():
            m.apply_translation(-grid_min)
            m.apply_scale(scale)
            m.apply_translation(grid_min)

    return loaded_meshes, scale


def _union_bounds(meshes: dict[str, trimesh.Trimesh]) -> tuple[np.ndarray, np.ndarray]:
    """Min and max corner of all meshes together."""
    return (
        np.min([m.bounds[0] for m in meshes.values()], axis=0),
        np.max([m.bounds[1] for m in meshes.values()], axis=0),
    )


def voxelize(
    stls: dict[str, Path],
    blocks: dict[str, str],
    max_dims: int | tuple[int, int, int],
) -> tuple[np.ndarray, list[str], np.ndarray, float]:
    """Voxelize all parts into one shared grid of shape (X, Y, Z).
    max_dims is an int (same limit on all 3 axes) or a tuple (wide_x, deep_y, tall_z).
    Returns (grid, palette, offset, scale):
    0 = air, i = palette[i], palette[0] == "minecraft:air".
    offset: np.ndarray of 3 ints = design coordinate of grid[0,0,0] after trimming.
    scale: float, 1.0 when not scaled."""
    if isinstance(max_dims, (int, np.integer)):
        limits = np.array([float(max_dims), float(max_dims), float(max_dims)])
    else:
        limits = np.array([float(d) for d in max_dims])

    loaded_meshes, scale = _load_meshes(stls, blocks, limits)
    union_min, union_max = _union_bounds(loaded_meshes)

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

    grid = drop_floaters(grid)
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

    offset = (origin + min_pos).astype(int)
    return trimmed_grid, palette, offset, scale


FLOATER_MAX_BLOCKS = 200    # floating clusters up to this many blocks are dropped,
FLOATER_MAX_SHARE = 0.005   # if they are also at most this share of the whole build


def drop_floaters(grid: np.ndarray) -> np.ndarray:
    """Drop small clusters of blocks that touch neither the main build nor the bottom layer:
    misplaced windows, trims and spikes that would hang in the air. Blocks touching at an
    edge or corner count as connected; the largest cluster is always kept."""
    labels, n = ndimage.label(grid > 0, structure=np.ones((3, 3, 3)))
    if n < 2:
        return grid
    sizes = np.bincount(labels.ravel())
    ground = np.nonzero(np.any(grid > 0, axis=(0, 1)))[0][0]
    drop = sizes <= min(FLOATER_MAX_BLOCKS, FLOATER_MAX_SHARE * sizes[1:].sum())
    drop[0] = False                               # air
    drop[np.argmax(sizes[1:]) + 1] = False        # the main build
    drop[np.unique(labels[:, :, ground])] = False  # anything standing on the bottom layer
    out = grid.copy()
    out[drop[labels]] = 0
    return out


@functools.cache
def stair_slab_variants() -> dict[str, tuple[str | None, str | None]]:
    """Full block id -> (slab id, stairs id) for the blocks that have at least one of them,
    guessed from the ids in block_colors.json (stone_bricks -> stone_brick_slab, oak_planks -> oak_slab)."""
    ids = set(_block_colors())
    variants = {}
    for block in ids - {"minecraft:bamboo", "minecraft:bamboo_block"}:  # the plant and the log, not the planks
        stems = [block, block.removesuffix("s"), block.removesuffix("_planks"), block.removesuffix("_block")]
        slab = next((f"{s}_slab" for s in stems if f"{s}_slab" in ids), None)
        stairs = next((f"{s}_stairs" for s in stems if f"{s}_stairs" in ids), None)
        if slab or stairs:
            variants[block] = (slab, stairs)
    return variants


# The 8 sample points of a cell: sub-cell centres, index = 4 * ix + 2 * iy + iz (iz 0 = low half).
_SAMPLES = np.array([(x, y, z) for x in (0.25, 0.75) for y in (0.25, 0.75) for z in (0.25, 0.75)])
_LOW = _SAMPLES[:, 2] < 0.5
_SIDES = {  # design space: north = +Y, south = -Y, east = +X, west = -X
    "north": _SAMPLES[:, 1] > 0.5,
    "south": _SAMPLES[:, 1] < 0.5,
    "east": _SAMPLES[:, 0] > 0.5,
    "west": _SAMPLES[:, 0] < 0.5,
}
# (is stairs, block state, samples the shape fills); slabs come first so they win ties.
# Stairs facing F are a slab plus the other half on side F: they rise toward F.
_SHAPES = [(False, "type=bottom", _LOW), (False, "type=top", ~_LOW)]
for _half, _slab in (("bottom", _LOW), ("top", ~_LOW)):
    _SHAPES += [(True, f"facing={f},half={_half}", _slab | side) for f, side in _SIDES.items()]
_TEMPLATES = np.array([mask for _, _, mask in _SHAPES])
_IS_STAIRS = np.array([stairs for stairs, _, _ in _SHAPES])


def _neighbour_flags(solid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(has a solid face neighbour, has an air face neighbour) per cell; outside the grid is air."""
    near_solid = np.zeros_like(solid)
    near_air = np.zeros_like(solid)
    for axis in range(3):
        lo = tuple(slice(None, -1) if a == axis else slice(None) for a in range(3))
        hi = tuple(slice(1, None) if a == axis else slice(None) for a in range(3))
        near_solid[lo] |= solid[hi]
        near_solid[hi] |= solid[lo]
        near_air[lo] |= ~solid[hi]
        near_air[hi] |= ~solid[lo]
        near_air[(slice(None),) * axis + (0,)] = True
        near_air[(slice(None),) * axis + (-1,)] = True
    return near_solid, near_air


def _merge_runs(cells: np.ndarray, kinds: np.ndarray) -> list[tuple[int, int, int, int, int, int, int]]:
    """Merge cells (n, 3) of the same kind that touch along one axis into (x1, y1, z1, x2, y2, z2, kind)
    boxes, along whichever of the three axes gives the fewest boxes."""
    best = None
    for axis in range(3):
        a, b = (c for c in range(3) if c != axis)
        order = np.lexsort((cells[:, axis], cells[:, b], cells[:, a], kinds))
        c, k = cells[order], kinds[order]
        start = np.ones(len(c), dtype=bool)
        start[1:] = (
            (k[1:] != k[:-1]) | (c[1:, a] != c[:-1, a]) | (c[1:, b] != c[:-1, b]) | (c[1:, axis] != c[:-1, axis] + 1)
        )
        first = np.nonzero(start)[0]
        if best is None or len(first) < len(best[1]):
            best = (c, k, first)
    c, k, first = best
    last = np.append(first[1:], len(c)) - 1
    return [(*map(int, c[i]), *map(int, c[j]), int(k[i])) for i, j in zip(first, last)]


def smooth(
    stls: dict[str, Path],
    blocks: dict[str, str],
    grid: np.ndarray,
    palette: list[str],
    offset: np.ndarray,
    limits: int | tuple[int, int, int],
) -> tuple[np.ndarray, list[tuple[int, int, int, int, int, int, str]]]:
    """Turn cells on sloped or curved surfaces into stairs and slabs. offset and limits are voxelize's
    (offset of grid[0,0,0] and the size limits, so the meshes are scaled the same way). Each solid
    cell with an air neighbour and each air cell with a solid neighbour is sampled at its 8 sub-cell
    centres and gets the slab or stairs that fits them best, if that is at most 1 sample off and
    clearly better than what the cell is now. Blocks without slab or stairs variants stay as they are.
    Returns (grid with the solid cells that became shapes set to air, boxes in grid coordinates with
    states, e.g. "minecraft:stone_brick_stairs[facing=north,half=bottom]")."""
    meshes, _ = _load_meshes(stls, blocks, np.asarray(limits, dtype=float))
    parts = list(meshes)  # in blocks order: a later part overrides an earlier one
    variants = stair_slab_variants()
    none = (None, None)
    pal_pairs = [variants.get(b.split("[", 1)[0], none) for b in palette]
    part_pairs = [variants.get(blocks[p].split("[", 1)[0], none) for p in parts]
    mats = list(dict.fromkeys(pal_pairs + part_pairs))  # distinct (slab, stairs) pairs
    if all(m == none for m in mats):
        return grid.copy(), []
    pal_mat = np.array([mats.index(m) for m in pal_pairs])
    part_mat = np.array([mats.index(m) for m in part_pairs])
    slab_ok = np.array([m[0] is not None for m in mats])
    stairs_ok = np.array([m[1] is not None for m in mats])

    solid = grid > 0
    near_solid, near_air = _neighbour_flags(solid)
    has_variant = np.array([m != none for m in pal_pairs])
    cand = (solid & near_air & has_variant[grid]) | (~solid & near_solid)
    cells = np.argwhere(cand)
    if len(cells) == 0:
        return grid.copy(), []

    # Sample the candidates against every part: rows are cells, columns the 8 samples.
    pts = (np.asarray(offset) + cells[:, None, :] + _SAMPLES).reshape(-1, 3)
    hits = np.zeros((len(parts), len(pts)), dtype=bool)
    for i, part in enumerate(parts):
        lo, hi = meshes[part].bounds
        near = np.all((pts >= lo) & (pts <= hi), axis=1)
        if np.any(near):
            hits[i, near] = np.asarray(meshes[part].contains(pts[near]), dtype=bool)
    hits = hits.reshape(len(parts), len(cells), 8)
    sampled = hits.any(axis=0)

    # Material: a solid cell's current block, else the part holding most samples (ties: later part).
    was_solid = solid[tuple(cells.T)]
    part_of = len(parts) - 1 - np.argmax(hits.sum(axis=2)[::-1], axis=0)
    material = np.where(was_solid, pal_mat[grid[tuple(cells.T)]], part_mat[part_of])

    # Best shape per cell, ruling out shapes the material has no block for.
    dist = (sampled[:, None, :] != _TEMPLATES).sum(axis=2)
    dist[~stairs_ok[material][:, None] & _IS_STAIRS] = 99
    dist[~slab_ok[material][:, None] & ~_IS_STAIRS] = 99
    best = dist.argmin(axis=1)
    best_dist = dist.min(axis=1)
    now = np.where(was_solid, 8 - sampled.sum(axis=1), sampled.sum(axis=1))  # distance of the cell as it is
    change = (best_dist <= 1) & (best_dist < now)

    out = grid.copy()
    cells = cells[change]
    out[tuple(cells[was_solid[change]].T)] = 0
    if len(cells) == 0:
        return out, []

    # A "kind" is a (material, shape) pair; neighbouring cells of one kind become one box.
    kinds = material[change] * len(_SHAPES) + best[change]
    result = []
    for x1, y1, z1, x2, y2, z2, kind in _merge_runs(cells, kinds):
        slab, stairs = mats[kind // len(_SHAPES)]
        is_stairs, state, _ = _SHAPES[kind % len(_SHAPES)]
        result.append((x1, y1, z1, x2, y2, z2, f"{stairs if is_stairs else slab}[{state}]"))
    return out, result


MIX_MIN_BASE = 0.7  # the base block keeps at least this share of a mixed surface
MIX_MAX_TONE = 60   # variants further than this (RGB distance) from the base's colour are dropped
MIX_PATCH = 3       # variants come in patches of this many blocks per side, not single specks


def _mix_weights(base: str, variants: dict[str, float]) -> dict[str, float]:
    """Weights for one mix: variants whose colour is far from the base's are dropped (unknown
    colours are kept), and the base keeps at least MIX_MIN_BASE. Empty if nothing is left to mix."""
    colours = _block_colors()
    base_rgb = colours.get(base)
    weights = {}
    for var_id, weight in variants.items():
        rgb = colours.get(var_id)
        if var_id != base and base_rgb and rgb and np.linalg.norm(np.subtract(rgb, base_rgb)) > MIX_MAX_TONE:
            continue
        weights[var_id] = float(weight)
    others = sum(w for v, w in weights.items() if v != base)
    if others == 0:
        return {}
    weights[base] = max(weights.get(base, 0.0), others * MIX_MIN_BASE / (1 - MIX_MIN_BASE))
    return weights


def apply_mix(
    grid: np.ndarray,
    palette: list[str],
    mix: dict[str, dict[str, float]],
    seed: int | None = None,
) -> tuple[np.ndarray, list[str]]:
    """Apply texture mixes to surface cells of the grid.
    Returns (new_grid, new_palette). Inputs are not mutated."""
    new_grid = grid.copy()
    new_palette = list(palette)
    palette_map = {b: i for i, b in enumerate(new_palette)}

    # Surface cells: solid cells with at least one neighbour air or outside grid.
    is_solid = new_grid > 0
    hidden = np.zeros_like(is_solid)
    if new_grid.shape[0] > 2 and new_grid.shape[1] > 2 and new_grid.shape[2] > 2:
        hidden[1:-1, 1:-1, 1:-1] = (
            is_solid[1:-1, 1:-1, 1:-1]
            & is_solid[:-2, 1:-1, 1:-1]
            & is_solid[2:, 1:-1, 1:-1]
            & is_solid[1:-1, :-2, 1:-1]
            & is_solid[1:-1, 2:, 1:-1]
            & is_solid[1:-1, 1:-1, :-2]
            & is_solid[1:-1, 1:-1, 2:]
        )
    surface = is_solid & ~hidden

    rng = np.random.default_rng(seed)

    for base_block, variants in mix.items():
        if base_block not in palette_map:
            continue
        weights = _mix_weights(base_block, variants)
        if not weights:
            continue

        for var_id in weights:
            if var_id not in palette_map:
                if len(new_palette) >= 256:
                    raise BuildError("Too many distinct block types (max 255)")
                palette_map[var_id] = len(new_palette)
                new_palette.append(var_id)

        coords = np.argwhere(surface & (new_grid == palette_map[base_block]))
        if len(coords) == 0:
            continue

        # One draw per MIX_PATCH-sized patch, so weathering shows as patches rather than specks.
        _, patch = np.unique(coords // MIX_PATCH, axis=0, return_inverse=True)
        patch = patch.ravel()
        var_indices = np.array([palette_map[v] for v in weights], dtype=new_grid.dtype)
        probs = np.array(list(weights.values())) / sum(weights.values())
        draws = rng.choice(var_indices, size=patch.max() + 1, p=probs)[patch]
        new_grid[tuple(coords.T)] = draws

    return new_grid, new_palette


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


_CLOCKWISE_FACINGS = ["north", "east", "south", "west"]
_FACING_K = {"north": 0, "east": 1, "south": 2, "west": 3}


def rotate_state(block: str, facing: str) -> str:
    """Rotate block states from design space to Minecraft world orientation for facing."""
    if facing not in _FACING_K:
        raise ValueError(f"Unknown facing: {facing!r}")
    k = _FACING_K[facing]
    if k == 0:
        return block

    if "[" not in block or not block.endswith("]"):
        return block

    base_id, inside = block[:-1].split("[", 1)
    if not inside:
        return block

    parts = inside.split(",")
    new_parts = []
    for part in parts:
        p = part.strip()
        if not p:
            continue
        if "=" not in p:
            new_parts.append(p)
            continue
        key, val = p.split("=", 1)
        key = key.strip()
        val = val.strip()
        if key == "facing":
            if val in _CLOCKWISE_FACINGS:
                idx = _CLOCKWISE_FACINGS.index(val)
                new_val = _CLOCKWISE_FACINGS[(idx + k) % 4]
                new_parts.append(f"{key}={new_val}")
            else:
                new_parts.append(p)
        elif key == "axis":
            if k % 2 == 1:
                if val == "x":
                    new_parts.append(f"{key}=z")
                elif val == "z":
                    new_parts.append(f"{key}=x")
                else:
                    new_parts.append(p)
            else:
                new_parts.append(p)
        elif key == "rotation":
            try:
                n = int(val)
                new_parts.append(f"{key}={(n + 4 * k) % 16}")
            except ValueError:
                new_parts.append(p)
        else:
            new_parts.append(p)

    return f"{base_id}[{','.join(new_parts)}]"


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


@functools.cache
def _block_colors() -> dict[str, list[int]]:
    return json.loads(config.BLOCK_COLORS_FILE.read_text(encoding="utf-8"))


def block_color(block_id: str) -> tuple[int, int, int]:
    """Average texture colour of a block (block_colors.json); unknown ids get a stable made-up colour."""
    block_id = block_id.split("[", 1)[0]  # states don't change the colour
    rgb = _block_colors().get(block_id) or hashlib.md5(block_id.encode("utf-8")).digest()[:3]
    return (int(rgb[0]), int(rgb[1]), int(rgb[2]))


def _render_view(
    grid: np.ndarray,
    palette_rgb: np.ndarray,
    proj_axis: int,
    reverse_proj: bool = False,
) -> np.ndarray:
    """Renders a single orthographic view of shape (H, W, 3), one pixel per cell."""
    v = np.moveaxis(grid, proj_axis, -1)
    if reverse_proj:
        v = v[:, :, ::-1]

    D = v.shape[-1]
    solid = v > 0
    has_solid = np.any(solid, axis=-1)
    first_idx = np.argmax(solid, axis=-1)
    block_val = np.take_along_axis(v, first_idx[..., None], axis=-1).squeeze(-1)

    depth = first_idx
    shade = 1.0 - 0.6 * (depth.astype(np.float32) / max(D - 1, 1))

    base_rgb = palette_rgb[block_val]
    shaded_rgb = base_rgb * shade[..., None]
    clipped_rgb = np.clip(np.round(shaded_rgb), 0, 255).astype(np.uint8)

    bg_color = np.array([30, 30, 30], dtype=np.uint8)
    view_rgb = np.where(has_solid[..., None], clipped_rgb, bg_color)

    # Transpose (A, B) to (B, A) and flip rows so up in image is +z (for top: +y)
    view_2d = np.transpose(view_rgb, (1, 0, 2))[::-1, :, :]

    return view_2d


def preview_png(grid: np.ndarray, palette: list[str]) -> bytes:
    """Renders an RGB PNG with three orthographic views (FRONT, SIDE, TOP) side by side."""
    if grid.ndim != 3:
        raise ValueError(f"Expected 3D grid, got shape {grid.shape}")

    palette_rgb = np.zeros((len(palette), 3), dtype=np.float32)
    for i, block_id in enumerate(palette):
        if i == 0 or block_id == "minecraft:air":
            palette_rgb[i] = [30, 30, 30]
        else:
            palette_rgb[i] = block_color(block_id)

    front_px = _render_view(grid, palette_rgb, proj_axis=1, reverse_proj=False)
    side_px = _render_view(grid, palette_rgb, proj_axis=0, reverse_proj=True)
    top_px = _render_view(grid, palette_rgb, proj_axis=2, reverse_proj=True)

    cell = max(4, min(16, 400 // max(grid.shape)))  # px per block: small builds drawn bigger
    views = [np.repeat(np.repeat(v, cell, axis=0), cell, axis=1) for v in (front_px, side_px, top_px)]
    pad = 12
    max_h = max(v.shape[0] for v in views)
    canvas_h = max_h + 2 * pad
    canvas_w = sum(v.shape[1] for v in views) + pad * (len(views) + 1)
    canvas = np.full((canvas_h, canvas_w, 3), 30, dtype=np.uint8)

    x = pad
    for v in views:
        h, w = v.shape[:2]
        y = pad + (max_h - h) // 2
        canvas[y : y + h, x : x + w] = v
        x += w + pad

    img = Image.fromarray(canvas, mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _set_state(block: str, key: str, value: str) -> str:
    """Set one block state, replacing any existing value for key."""
    base_id, _, inside = block.partition("[")
    states = [p for p in inside.rstrip("]").split(",") if p and p.split("=")[0] != key]
    return f"{base_id}[{','.join(states + [f'{key}={value}'])}]"


def with_persistent_leaves(block: str) -> str:
    """Leaves get persistent=true so they don't decay."""
    if block.split("[", 1)[0].endswith("_leaves"):
        return _set_state(block, "persistent", "true")
    return block


def detail_boxes(
    details: list[tuple[int, int, int, int, int, int, str] | tuple[int, int, int, str]],
    offset: np.ndarray | tuple[int, int, int],
    shape: tuple[int, int, int],
) -> list[tuple[int, int, int, int, int, int, str]]:
    """Convert details to grid coordinates, drop out-of-bounds, expand doors, and apply persistent leaves."""
    X, Y, Z = shape
    off_x, off_y, off_z = int(offset[0]), int(offset[1]), int(offset[2])
    result: list[tuple[int, int, int, int, int, int, str]] = []

    for item in details:
        if len(item) == 4:
            x1, y1, z1, block = item
            x2, y2, z2 = x1, y1, z1
        elif len(item) == 7:
            x1, y1, z1, x2, y2, z2, block = item
        else:
            continue

        gx1 = int(x1) - off_x
        gy1 = int(y1) - off_y
        gz1 = int(z1) - off_z
        gx2 = int(x2) - off_x
        gy2 = int(y2) - off_y
        gz2 = int(z2) - off_z

        base_id = block.split("[", 1)[0]
        if base_id.endswith("_door"):
            # Doors: bottom layer at gz1 with half=lower, upper layer at gz1+1 with half=upper.
            # z2 is ignored. If upper layer is outside z range [0, Z], drop the door.
            if not (-1 <= gx1 and gx2 <= X and -1 <= gy1 and gy2 <= Y and 0 <= gz1 and gz1 + 1 <= Z):
                continue
            lower_b = with_persistent_leaves(_set_state(block, "half", "lower"))
            upper_b = with_persistent_leaves(_set_state(block, "half", "upper"))
            result.append((gx1, gy1, gz1, gx2, gy2, gz1, lower_b))
            result.append((gx1, gy1, gz1 + 1, gx2, gy2, gz1 + 1, upper_b))
        else:
            if not (-1 <= gx1 and gx2 <= X and -1 <= gy1 and gy2 <= Y and 0 <= gz1 and gz2 <= Z):
                continue
            b = with_persistent_leaves(block)
            result.append((gx1, gy1, gz1, gx2, gy2, gz2, b))

    return result


def stamp_details(
    grid: np.ndarray,
    palette: list[str],
    dboxes: list[tuple[int, int, int, int, int, int, str]],
) -> tuple[np.ndarray, list[str]]:
    """Stamp detail boxes into a copy of grid, clipped to grid bounds. For previews only.
    Slabs and stairs keep their states in the palette so the preview can draw their shape;
    every other block goes in by its base id."""
    new_grid = grid.copy()
    new_palette = list(palette)
    palette_map = {b: i for i, b in enumerate(new_palette)}

    X, Y, Z = new_grid.shape

    for x1, y1, z1, x2, y2, z2, block in dboxes:
        cx1 = max(0, x1)
        cx2 = min(X - 1, x2)
        cy1 = max(0, y1)
        cy2 = min(Y - 1, y2)
        cz1 = max(0, z1)
        cz2 = min(Z - 1, z2)

        if cx1 > cx2 or cy1 > cy2 or cz1 > cz2:
            continue

        base_id = block.split("[", 1)[0]
        entry = block if base_id.endswith(("_slab", "_stairs")) else base_id
        if entry not in palette_map:
            if len(new_palette) >= 256:
                raise BuildError("Too many distinct block types (max 255)")
            palette_map[entry] = len(new_palette)
            new_palette.append(entry)
        val = palette_map[entry]

        new_grid[cx1 : cx2 + 1, cy1 : cy2 + 1, cz1 : cz2 + 1] = val

    return new_grid, new_palette
