"""Operations of !edit: check the AI's edits, apply them to a block grid, box the changes up."""
from __future__ import annotations

import numpy as np

try:
    from . import build, llm
except ImportError:
    from mcmcp import build, llm

Op = tuple[int, int, int, int, int, int, str]
AIR = "minecraft:air"
REMOVED_BLOCK = "minecraft:red_stained_glass"  # how a removed block shows in the hologram


class EditError(Exception):
    pass


def parse_ops(reply: str, shape: tuple[int, int, int]) -> list[Op]:
    """The edits of the last ```json block of the reply, checked and clipped to the shape.
    Doors become a lower and an upper op, leaves persistent. Raises EditError if the reply has no usable edits list."""
    try:
        edits = llm.parse_interior(reply)
    except llm.LlmError as exc:
        raise EditError(str(exc)) from exc
    ops: list[Op] = []
    for x1, y1, z1, x2, y2, z2, block in edits:
        if block.split("[", 1)[0].endswith("_door"):
            if z1 + 1 >= shape[2]:  # the upper half would stick out
                continue
            ops.append((x1, y1, z1, x2, y2, z1, build.with_persistent_leaves(build._set_state(block, "half", "lower"))))
            ops.append((x1, y1, z1 + 1, x2, y2, z1 + 1, build.with_persistent_leaves(build._set_state(block, "half", "upper"))))
        else:
            ops.append((x1, y1, z1, x2, y2, z2, build.with_persistent_leaves(block)))
    clipped: list[Op] = []
    for x1, y1, z1, x2, y2, z2, block in ops:
        lo = (max(0, x1), max(0, y1), max(0, z1))
        hi = (min(shape[0] - 1, x2), min(shape[1] - 1, y2), min(shape[2] - 1, z2))
        if all(a <= b for a, b in zip(lo, hi)):
            clipped.append((*lo, *hi, block))
    return clipped


def apply_ops(
    grid: np.ndarray,
    palette: list[str],
    ops: list[Op],
    locked: np.ndarray,
) -> tuple[np.ndarray, list[str], np.ndarray]:
    """Apply the ops in order (later wins) to a copy of the grid; locked cells keep their block.
    Returns the new grid, the new palette and a bool grid of the cells whose block string changed."""
    new_grid = grid.copy()
    new_palette = list(palette)
    index = {}
    for i, block in enumerate(new_palette):
        index.setdefault(block, i)
    for x1, y1, z1, x2, y2, z2, block in ops:
        if block.split("[", 1)[0] == AIR:
            block = AIR
        if block not in index:
            index[block] = len(new_palette)
            new_palette.append(block)
        region = (slice(x1, x2 + 1), slice(y1, y2 + 1), slice(z1, z2 + 1))
        free = ~locked[region]
        new_grid[region][free] = index[block]
    changed = np.array(new_palette)[new_grid] != np.array(palette)[grid]
    return new_grid, new_palette, changed


def change_boxes(grid: np.ndarray, palette: list[str], changed: np.ndarray, max_volume: int) -> list[Op]:
    """The changed cells of the edited grid as boxes, bottom layer first. Air is included
    (a removed block is a box of minecraft:air)."""
    shifted = np.where(changed, grid.astype(np.int32) + 1, 0)  # build.boxes skips 0, which would be air
    return [(*box[:6], palette[box[6] - 1]) for box in build.boxes(shifted, max_volume)]


def ghost_design(grid: np.ndarray, palette: list[str], changed: np.ndarray) -> tuple[np.ndarray, list[str]]:
    """The changed cells only, as a grid and palette of base ids for the hologram. 0 = nothing;
    a removed block (now air) shows as REMOVED_BLOCK."""
    ghost_palette = [AIR]
    ghost = np.zeros(grid.shape, dtype=np.uint8)
    for value in np.unique(grid[changed]):
        base = palette[value].split("[", 1)[0]
        if base == AIR:
            base = REMOVED_BLOCK
        if base not in ghost_palette:
            ghost_palette.append(base)
        ghost[changed & (grid == value)] = ghost_palette.index(base)
    return ghost, ghost_palette
