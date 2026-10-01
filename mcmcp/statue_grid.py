"""Mesh (.glb) -> Minecraft block grid, for !statue.

Voxelizes a textured or untextured 3D model into a block grid, maps surface
colours to Minecraft blocks in Lab space, and provides utilities for colour checks.
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage
from scipy.spatial import cKDTree
import trimesh

try:
    from . import build, config, llm, pixelart
    from .build import BuildError
except ImportError:
    from mcmcp import build, config, llm, pixelart
    from mcmcp.build import BuildError

SURFACE_SPACING = 0.3  # blocks between surface samples, so every bit of surface lands in a block


def glb_to_grid(
    glb: Path | str,
    limits: tuple[int, int, int],
    height: int | None = None,
) -> tuple[np.ndarray, list[str]]:
    """Turns a .glb mesh into a block grid (the build.voxelize format: uint8, 0 = air,
    palette[0] == "minecraft:air").
    - Pieces far from the main body (stray specks) are dropped; pieces within its bounds are kept.
    - glTF Y-up becomes Z-up, so the glTF front (+Z) is the build's front (-Y).
    - One uniform scale: `height` blocks tall (default config.STATUE_HEIGHT), capped by limits.
    - Every bit of surface becomes a block, then enclosed space is filled, so thin open
      surfaces (cloth, ribbons, staffs) and meshes that aren't watertight still come out solid.
    - Each block takes the average colour of its nearest surface samples, matched to the nearest
      block in Lab (pixelart's palette). A mesh without texture or colours is all white concrete.
    Raises BuildError if the file can't be read or has no geometry."""
    height = height or config.STATUE_HEIGHT
    try:
        scene = trimesh.load(str(glb), force="scene")
    except Exception as e:
        raise BuildError(f"Couldn't read the mesh: {e}") from e

    meshes: list[trimesh.Trimesh] = []
    for name in scene.graph.nodes_geometry:
        tf, gname = scene.graph[name]
        g = scene.geometry[gname]
        if not isinstance(g, trimesh.Trimesh) or len(g.faces) == 0:
            continue
        g = g.copy()
        g.apply_transform(tf)
        if g.visual.kind == "texture":
            g.visual = g.visual.to_color()  # sample the texture at each vertex
        meshes.append(g)
    if not meshes:
        raise BuildError("No solid geometry in the mesh")
    coloured = any(g.visual.kind is not None for g in meshes)
    m = trimesh.util.concatenate(meshes)

    # Pieces = vertices joined by edges, on a welded copy (UV seams split the vertices). Keep the
    # largest piece and every piece whose centre lies within its bounds (staffs, ornaments).
    w = trimesh.Trimesh(m.vertices, m.faces, process=False)
    w.merge_vertices(merge_tex=True, merge_norm=True)
    groups = sorted(trimesh.graph.connected_components(w.edges_unique, nodes=np.arange(len(w.vertices))),
                    key=len, reverse=True)
    lo, hi = w.vertices[groups[0]].min(axis=0), w.vertices[groups[0]].max(axis=0)
    keep_vertex = np.zeros(len(w.vertices), bool)
    for g in groups:
        centre = w.vertices[g].mean(axis=0)
        if g is groups[0] or (np.all(centre >= lo) and np.all(centre <= hi)):
            keep_vertex[g] = True
    m.update_faces(keep_vertex[w.faces].all(axis=1))
    m.remove_unreferenced_vertices()

    m.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))  # Y-up -> Z-up
    m.apply_translation(-m.bounds[0])
    ext = np.maximum(m.extents, 1e-9)
    m.apply_scale(min(height / ext[2], limits[0] / ext[0], limits[1] / ext[1], limits[2] / ext[2]))

    # Surface samples (and the vertices, for slivers) mark their blocks; then fill enclosed space.
    pts, fi = trimesh.sample.sample_surface_even(m, int(m.area / SURFACE_SPACING**2) + 1000)
    cells = np.floor(np.vstack([pts, m.vertices])).astype(int)
    dims = np.minimum(cells.max(axis=0) + 1, np.asarray(limits))
    cells = np.minimum(cells, dims - 1)
    solid = np.zeros(dims, bool)
    solid[tuple(cells.T)] = True
    solid = ndimage.binary_fill_holes(solid)
    idxs = np.argwhere(solid)

    if not coloured:
        grid = solid.astype(np.uint8)
        return grid, ["minecraft:air", "minecraft:white_concrete"]

    cols = m.visual.vertex_colors[:, :3].astype(np.float32)[m.faces[fi]].mean(axis=1)
    _, near = cKDTree(pts).query(idxs + 0.5, k=min(8, len(pts)))
    rgb = cols[near.reshape(len(idxs), -1)].mean(axis=1)
    blocks, labs = pixelart.get_palette_data()
    idx = pixelart.find_nearest_palette(pixelart.rgb_to_lab(rgb), labs)
    used = sorted(set(idx.tolist()))
    remap = np.zeros(len(blocks), np.uint8)
    remap[used] = np.arange(1, len(used) + 1)
    grid = np.zeros(dims, np.uint8)
    grid[tuple(idxs.T)] = remap[idx]
    return grid, ["minecraft:air"] + [blocks[i] for i in used]


def block_lines(grid: np.ndarray, palette: list[str]) -> list[str]:
    """Summary of blocks used: count and height range (10th-90th percentile of z), most first."""
    Z = grid.shape[2] if grid.ndim == 3 else 1
    denom = max(Z - 1, 1)
    items: list[tuple[int, str]] = []
    for i in range(1, len(palette)):
        pts = np.argwhere(grid == i)
        n = len(pts)
        if n == 0:
            continue
        lo, hi = np.percentile(pts[:, 2], [10, 90]) / denom * 100
        items.append((n, f"{palette[i]}: {n} blocks, height {lo:.0f}-{hi:.0f}%"))
    items.sort(key=lambda t: (-t[0], t[1]))
    return [text for _, text in items]


def apply_swaps(palette: list[str], swaps: dict[str, str]) -> tuple[list[str], int]:
    """Replace block IDs in palette based on swaps dictionary.

    A swap applies only if:
    - old id is in palette (and not at index 0)
    - new id is in pixelart.load_palette()
    - not llm.is_banned(new_id)

    Returns (new_palette, count_of_applied_swaps).
    """
    if not palette:
        return [], 0
    if not swaps:
        return list(palette), 0

    allowed = pixelart.load_palette()
    valid_swaps: dict[str, str] = {}
    for old_id, new_id in swaps.items():
        if not isinstance(old_id, str) or not isinstance(new_id, str):
            continue
        if old_id not in palette[1:]:
            continue
        if new_id not in allowed:
            continue
        if llm.is_banned(new_id):
            continue
        valid_swaps[old_id] = new_id

    new_palette = [palette[0]] + [valid_swaps.get(b, b) for b in palette[1:]]
    return new_palette, len(valid_swaps)


def views_png(grid: np.ndarray, palette: list[str]) -> bytes:
    """Render front, side, and back views side by side as PNG bytes for colour check."""
    if grid.ndim != 3:
        raise ValueError(f"Expected 3D grid, got shape {grid.shape}")

    pal_rgb = np.array(
        [[30, 30, 30]] + [build.block_color(b) for b in palette[1:]],
        dtype=np.float32,
    )
    views = []
    # front (as the image), side, back
    for ax, rev, flip in ((1, False, False), (0, True, False), (1, True, True)):
        v = build._render_view(grid, pal_rgb, ax, rev)
        views.append(v[:, ::-1] if flip else v)

    H = max(v.shape[0] for v in views)
    cell = 6
    W = sum(v.shape[1] for v in views) * cell + 40
    canvas = Image.new("RGB", (W, H * cell), (30, 30, 30))
    x = 0
    for v in views:
        im = Image.fromarray(v).resize((v.shape[1] * cell, v.shape[0] * cell), Image.NEAREST)
        canvas.paste(im, (x, H * cell - im.height))
        x += im.width + 20

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()
