"""Geometry for !interior: find the inside of a design, describe it as text maps, apply the AI's edits."""
from __future__ import annotations

import io
from typing import NamedTuple

import numpy as np
from PIL import Image
from scipy import ndimage

try:
    from . import build, config, preview
except ImportError:
    from mcmcp import build, config, preview

Box = tuple[int, int, int, int, int, int, str]
FULL = np.ones((3, 3, 3), dtype=bool)
LETTERS = "abcdefghijklmnpqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"  # no "o": it marks an opening


class Inside(NamedTuple):
    stamped: np.ndarray   # build.stamp_details result (air boxes stamped as 0)
    palette: list[str]    # palette of `stamped`
    outside: np.ndarray   # bool, grid shape
    skin: np.ndarray      # bool, grid shape (skin zone incl. locked old dboxes)
    free: np.ndarray      # bool, grid shape
    locked: list[bool]    # one per old dbox: True = touches the skin, kept


def _clip(box: Box, shape: tuple[int, int, int]) -> tuple[slice, slice, slice]:
    """Box as a slice into an array of this shape (empty if the box is outside)."""
    return tuple(slice(max(0, box[i]), min(shape[i] - 1, box[i + 3]) + 1) for i in range(3))


def _bounds(mask: np.ndarray, margin: int = 0) -> tuple[slice, slice, slice]:
    """Bounding box of the True cells, grown by margin and clipped to the array."""
    sl = []
    for axis in range(3):
        other = tuple(a for a in range(3) if a != axis)
        idx = np.flatnonzero(mask.any(axis=other))
        sl.append(slice(max(0, idx[0] - margin), min(mask.shape[axis], idx[-1] + 1 + margin)))
    return tuple(sl)


def analyze(
    grid: np.ndarray,
    palette: list[str],
    dboxes: list[Box],
    skin: int = config.INTERIOR_SKIN,
    max_cells: int = config.INTERIOR_MAX_CELLS,
) -> Inside | str:
    """Split a design into outside, outer shell (skin) and free inside. A string is a chat message
    saying why there is nothing to redo."""
    stamped, pal = build.stamp_details(grid, palette, dboxes)
    pad = 3
    solid = np.pad(stamped > 0, pad)
    solid[:, :, :pad] = True  # the ground is not an entrance
    # Plug doors and windows up to about 4 wide, so the building counts as closed.
    closed = ndimage.binary_closing(solid, structure=FULL, iterations=2)
    labels, _ = ndimage.label(~closed)  # 6-connectivity
    border = np.zeros(closed.shape, dtype=bool)
    border[:pad], border[-pad:] = True, True
    border[:, :pad], border[:, -pad:] = True, True
    border[:, :, -pad:] = True
    touching = np.unique(labels[border])
    outside = np.isin(labels, touching[touching > 0])
    # The same without the closing: recesses (panels between proud posts) are filled by the
    # closing, so the blocks touching the real outside air are locked too.
    labels, _ = ndimage.label(~solid)
    touching = np.unique(labels[border])
    real_outside = np.isin(labels, touching[touching > 0])
    # dilate before cropping: the walls may sit right at the grid's edge
    near = ndimage.binary_dilation(outside, structure=FULL, iterations=skin)
    near_real = solid & ndimage.binary_dilation(real_outside, structure=FULL, iterations=skin)
    cut = (slice(pad, -pad),) * 3
    outside = outside[cut]
    skin_zone = ~outside & (near | near_real)[cut]
    skin_zone[:, :, 0] |= stamped[:, :, 0] > 0  # the ground floor stays (the AI may still replace it)
    locked = []
    for box in dboxes:
        sl = _clip(box, grid.shape)
        touches = box[6] != "minecraft:air" and bool(skin_zone[sl].any())
        locked.append(touches)
        if touches:
            skin_zone[sl] = True
    free = ~outside & ~skin_zone

    if not free.any():
        return "There is no inside to redo: the build is solid, too small, or has no closed walls."
    cells = int(np.prod([s.stop - s.start for s in _bounds(free, 1)]))
    if cells > max_cells:
        return "The inside is too big to redo in one go."
    return Inside(stamped, pal, outside, skin_zone, free, locked)


def maps_text(inside: Inside) -> str:
    """One ASCII map per z layer of the free region (plus a margin), in grid coordinates.
    Top row = +Y (away from the player), bottom row = y 0. Letters are locked blocks, '.' outside,
    'o' an opening in the shell, '_' free (empty and yours)."""
    xs, ys, zs = _bounds(inside.free, 1)
    # an opening is shell air next to the inside; other shell air (pockets the closing filled) is outside
    opening = inside.skin & (inside.stamped == 0) & ndimage.binary_dilation(inside.free)
    letters: dict[str, str] = {}
    for v in np.unique(inside.stamped[inside.skin]):
        base = inside.palette[v].split("[", 1)[0]
        if v and base not in letters:
            letters[base] = LETTERS[len(letters)] if len(letters) < len(LETTERS) else "#"
    lines = ["Legend: . outside, o opening in the shell (air), _ free (empty, yours to fill)"]
    lines += [f"{ch} = {base}" for base, ch in letters.items()]
    xr = range(xs.start, xs.stop)
    for z in range(zs.start, zs.stop):
        lines.append("")
        lines.append(f"z = {z}")
        lines.append("    " + "".join(str(x // 10 % 10) if x >= 10 else " " for x in xr))
        lines.append("    " + "".join(str(x % 10) for x in xr))
        for y in range(ys.stop - 1, ys.start - 1, -1):
            row = []
            for x in xr:
                v = inside.stamped[x, y, z]
                if inside.free[x, y, z]:
                    row.append("_")
                elif v:
                    row.append(letters[inside.palette[v].split("[", 1)[0]])
                elif opening[x, y, z]:
                    row.append("o")
                else:
                    row.append(".")
            lines.append(f"{y:3d} " + "".join(row))
    return "\n".join(lines)


def apply(
    grid: np.ndarray,
    palette: list[str],
    dboxes: list[Box],
    inside: Inside,
    edits: list,
) -> tuple[np.ndarray, list[str], list[Box]] | None:
    """Empty the free region, drop the old detail boxes in it and add the edits. None if no edits."""
    if not edits:
        return None
    new_grid = grid.copy()
    new_grid[inside.free] = 0
    new_boxes = [
        box for box, locked in zip(dboxes, inside.locked)
        if locked or not inside.free[_clip(box, grid.shape)].any()
    ]
    for box in build.detail_boxes(edits, (0, 0, 0), grid.shape):
        new_boxes += build._split_box(box, config.FILL_MAX_VOLUME)
    return new_grid, list(palette), new_boxes


def cutaways_png(grid: np.ndarray, palette: list[str], dboxes: list[Box], free: np.ndarray) -> bytes:
    """Cuts every 5 layers over the free region's height (everything above the cut removed),
    rendered one under the other, top cut first."""
    stamped, pal = build.stamp_details(grid, palette, dboxes)
    zs = _bounds(free)[2]
    images = [
        Image.open(io.BytesIO(preview.blocks_png(stamped[:, :, : c + 1], pal)))
        for c in range(zs.stop - 1, zs.start - 1, -5)
    ]
    canvas = Image.new("RGB", (max(i.width for i in images), sum(i.height for i in images)), preview.BG_COLOR)
    y = 0
    for img in images:
        canvas.paste(img, (0, y))
        y += img.height
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def checks(
    grid: np.ndarray,
    palette: list[str],
    dboxes: list[Box],
    inside: Inside,
    edits: list,
) -> tuple[int, int]:
    """(blocks not connected to the ground, non-air edit cells more than 1 block outside the shell)."""
    stamped, _ = build.stamp_details(grid, palette, dboxes)
    labels, _ = ndimage.label(stamped > 0, structure=FULL)
    grounded = np.unique(labels[:, :, 0])
    floating = int(((labels > 0) & ~np.isin(labels, grounded)).sum())

    # allowed = within 1 block of anything that is not outside; 2 cells of padding so edits
    # up to 1 cell past the grid can be looked up
    allowed = ndimage.binary_dilation(np.pad(~inside.outside, 2), structure=FULL)
    far = 0
    for x1, y1, z1, x2, y2, z2, block in build.detail_boxes(edits, (0, 0, 0), grid.shape):
        if block.split("[", 1)[0] == "minecraft:air":
            continue
        vol = (x2 - x1 + 1) * (y2 - y1 + 1) * (z2 - z1 + 1)
        far += vol - int(allowed[x1 + 2 : x2 + 3, y1 + 2 : y2 + 3, z1 + 2 : z2 + 3].sum())
    return floating, far
