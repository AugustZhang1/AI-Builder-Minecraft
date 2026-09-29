"""Preview images (isometric screenshot and mesh views) and GLB export."""
from __future__ import annotations

import io
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
import trimesh

try:
    from . import build
except ImportError:
    from mcmcp import build

BG_COLOR = (30, 30, 30)
PAD = 12
LABEL_COLOR = (220, 220, 220)
LIGHT_DIR = np.array([-0.5, -0.5, 1.0], dtype=np.float32)
LIGHT_DIR /= np.linalg.norm(LIGHT_DIR)


def _load_parts(stls: dict[str, Path], blocks: dict[str, str]) -> list[tuple[str, trimesh.Trimesh, tuple[int, int, int]]]:
    """(part, mesh, block colour) for each part that has a non-empty STL; others are skipped."""
    parts = []
    for part_name, block_id in blocks.items():
        if part_name not in stls or not Path(stls[part_name]).exists():
            continue
        try:
            m = trimesh.load(str(stls[part_name]))
            if isinstance(m, trimesh.Scene):
                m = trimesh.util.concatenate(m.dump())
        except Exception:
            continue
        if m is not None and not m.is_empty and len(m.faces) > 0:
            parts.append((part_name, m, build.block_color(block_id.split("[", 1)[0])))
    return parts


def _text_height(draw: ImageDraw.ImageDraw, text: str) -> int:
    bbox = draw.textbbox((0, 0), text)
    return bbox[3] - bbox[1]


def _render_blocks_view(
    grid: np.ndarray,
    palette_rgb: list[tuple[int, int, int]],
    s: int,
) -> Image.Image:
    """Render a single 2:1 dimetric view of a block grid."""
    X, Y, Z = grid.shape
    if X == 0 or Y == 0 or Z == 0:
        return Image.new("RGB", (1, 1), BG_COLOR)

    solid = grid > 0

    top_vis = np.zeros_like(solid, dtype=bool)
    top_vis[:, :, -1] = solid[:, :, -1]
    if Z > 1:
        top_vis[:, :, :-1] = solid[:, :, :-1] & ~solid[:, :, 1:]

    left_vis = np.zeros_like(solid, dtype=bool)
    left_vis[0, :, :] = solid[0, :, :]
    if X > 1:
        left_vis[1:, :, :] = solid[1:, :, :] & ~solid[:-1, :, :]

    front_vis = np.zeros_like(solid, dtype=bool)
    front_vis[:, 0, :] = solid[:, 0, :]
    if Y > 1:
        front_vis[:, 1:, :] = solid[:, 1:, :] & ~solid[:, :-1, :]

    any_vis = top_vis | left_vis | front_vis
    xs, ys, zs = np.nonzero(any_vis)

    view_w = max(1, int(math.ceil((X + Y) * s))) + 1
    view_h = max(1, int(math.ceil((0.5 * (X + Y) + Z) * s))) + 1
    img = Image.new("RGB", (view_w, view_h), BG_COLOR)
    if len(xs) == 0:
        return img

    depths = xs + ys - zs
    order = np.argsort(-depths)
    xs, ys, zs = xs[order], ys[order], zs[order]
    vals = grid[xs, ys, zs]
    tv, lv, fv = top_vis[xs, ys, zs], left_vis[xs, ys, zs], front_vis[xs, ys, zs]

    draw = ImageDraw.Draw(img)
    offset_u = Y * s
    offset_v = (0.5 * (X + Y) + Z) * s

    top_cols = [tuple(min(255, max(0, int(round(c * 1.0)))) for c in col) for col in palette_rgb]
    left_cols = [tuple(min(255, max(0, int(round(c * 0.8)))) for c in col) for col in palette_rgb]
    front_cols = [tuple(min(255, max(0, int(round(c * 0.65)))) for c in col) for col in palette_rgb]
    outl_cols = (
        [tuple(min(255, max(0, int(round(c * 0.5)))) for c in col) for col in palette_rgb]
        if s >= 4
        else None
    )

    for x, y, z, val, t, l, f in zip(xs, ys, zs, vals, tv, lv, fv):
        v_idx = val if val < len(palette_rgb) else 0
        u0 = (x - y) * s + offset_u
        v0 = -((x + y) * 0.5 + z) * s + offset_v
        outl = outl_cols[v_idx] if outl_cols else None

        if t:
            poly_t = [(u0, v0 - s), (u0 + s, v0 - 1.5 * s), (u0, v0 - 2 * s), (u0 - s, v0 - 1.5 * s)]
            draw.polygon(poly_t, fill=top_cols[v_idx], outline=outl)
        if l:
            poly_l = [(u0, v0), (u0 - s, v0 - 0.5 * s), (u0 - s, v0 - 1.5 * s), (u0, v0 - s)]
            draw.polygon(poly_l, fill=left_cols[v_idx], outline=outl)
        if f:
            poly_f = [(u0, v0), (u0 + s, v0 - 0.5 * s), (u0 + s, v0 - 1.5 * s), (u0, v0 - s)]
            draw.polygon(poly_f, fill=front_cols[v_idx], outline=outl)

    return img


def blocks_png(grid: np.ndarray, palette: list[str]) -> bytes:
    """Render isometric (front-left, back-right) and orthographic views to PNG."""
    if grid.ndim != 3:
        raise ValueError(f"Expected 3D grid, got shape {grid.shape}")

    X, Y, Z = grid.shape
    span_u = max(X + Y, 1)
    span_v = max(0.5 * (X + Y) + Z, 1.0)
    s = max(1, min(int(800 / span_u), int(800 / span_v)))

    palette_rgb: list[tuple[int, int, int]] = []
    for i, block_id in enumerate(palette):
        if i == 0 or block_id == "minecraft:air":
            palette_rgb.append(BG_COLOR)
        else:
            base_id = block_id.split("[", 1)[0]
            palette_rgb.append(build.block_color(base_id))

    view_fl = _render_blocks_view(grid, palette_rgb, s)
    view_br = _render_blocks_view(grid[::-1, ::-1, :], palette_rgb, s)

    clean_palette = [b.split("[", 1)[0] for b in palette]
    strip_bytes = build.preview_png(grid, clean_palette)
    strip_img = Image.open(io.BytesIO(strip_bytes))

    top_w = PAD + view_fl.width + PAD + view_br.width + PAD
    avail_w = top_w - 2 * PAD
    if strip_img.width > avail_w:
        scale = avail_w / strip_img.width
        new_h = max(1, int(round(strip_img.height * scale)))
        resample = getattr(Image, "Resampling", Image).BILINEAR
        strip_img = strip_img.resize((avail_w, new_h), resample)

    dummy = Image.new("RGB", (1, 1))
    d = ImageDraw.Draw(dummy)
    label_h = _text_height(d, "front-left")
    label_gap = 4

    label1_y = PAD
    img1_y = label1_y + label_h + label_gap
    top_h = max(view_fl.height, view_br.height)

    label2_y = img1_y + top_h + PAD
    img2_y = label2_y + label_h + label_gap
    sheet_h = img2_y + strip_img.height + PAD
    sheet_w = top_w

    canvas = Image.new("RGB", (sheet_w, sheet_h), BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    x1 = PAD
    x2 = PAD + view_fl.width + PAD
    draw.text((x1, label1_y), "front-left", fill=LABEL_COLOR)
    draw.text((x2, label1_y), "back-right", fill=LABEL_COLOR)
    canvas.paste(view_fl, (x1, img1_y))
    canvas.paste(view_br, (x2, img1_y))

    x3 = PAD
    draw.text((x3, label2_y), "front / side / top", fill=LABEL_COLOR)
    canvas.paste(strip_img, (x3, img2_y))

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def _render_mesh_view(
    tris: np.ndarray,
    base_colors: np.ndarray,
    s: float,
) -> Image.Image:
    """Render a single dimetric view of 3D triangles."""
    e1 = tris[:, 1] - tris[:, 0]
    e2 = tris[:, 2] - tris[:, 0]
    normals = np.cross(e1, e2)
    norm = np.linalg.norm(normals, axis=1, keepdims=True)
    norm[norm == 0] = 1.0
    normals = normals / norm

    dot_c = normals[:, 0] + normals[:, 1] - normals[:, 2]
    facing = dot_c < -1e-6
    if not np.any(facing):
        return Image.new("RGB", (1, 1), BG_COLOR)

    f_tris = tris[facing]
    f_normals = normals[facing]
    f_colors = base_colors[facing]

    u_proj = (f_tris[:, :, 0] - f_tris[:, :, 1]) * s
    v_proj = -((f_tris[:, :, 0] + f_tris[:, :, 1]) * 0.5 + f_tris[:, :, 2]) * s

    min_u = np.min(u_proj)
    max_u = np.max(u_proj)
    min_v = np.min(v_proj)
    max_v = np.max(v_proj)

    offset_u = -min_u
    offset_v = -min_v

    u_screen = u_proj + offset_u
    v_screen = v_proj + offset_v

    view_w = max(1, int(math.ceil(max_u - min_u))) + 1
    view_h = max(1, int(math.ceil(max_v - min_v))) + 1

    centroids = np.mean(f_tris, axis=1)
    depths = centroids[:, 0] + centroids[:, 1] - centroids[:, 2]
    order = np.argsort(-depths)

    n_dot_l = np.sum(f_normals * LIGHT_DIR, axis=1)
    shades = 0.35 + 0.65 * np.maximum(0.0, n_dot_l)
    lit_colors = np.clip(np.round(f_colors * shades[:, None]), 0, 255).astype(np.uint8)

    img = Image.new("RGB", (view_w, view_h), BG_COLOR)
    draw = ImageDraw.Draw(img)

    for idx in order:
        pts = [(u_screen[idx, k], v_screen[idx, k]) for k in range(3)]
        col = (int(lit_colors[idx, 0]), int(lit_colors[idx, 1]), int(lit_colors[idx, 2]))
        draw.polygon(pts, fill=col)

    return img


def mesh_png(stls: dict[str, Path], blocks: dict[str, str]) -> bytes:
    """Render OpenSCAD meshes from front-left and back-right to PNG."""
    loaded_tris: list[np.ndarray] = []
    loaded_colors: list[np.ndarray] = []
    for _name, m, rgb in _load_parts(stls, blocks):
        tris = m.vertices[m.faces]
        loaded_tris.append(tris)
        loaded_colors.append(np.tile(np.array(rgb, dtype=np.float32), (len(tris), 1)))

    dummy = Image.new("RGB", (1, 1))
    d = ImageDraw.Draw(dummy)
    label_h = _text_height(d, "mesh front-left")
    label_gap = 4

    if not loaded_tris:
        canvas = Image.new("RGB", (400, 200), BG_COLOR)
        draw = ImageDraw.Draw(canvas)
        draw.text((PAD, PAD), "mesh front-left", fill=LABEL_COLOR)
        draw.text((200 + PAD, PAD), "mesh back-right", fill=LABEL_COLOR)
        buf = io.BytesIO()
        canvas.save(buf, format="PNG")
        return buf.getvalue()

    all_tris = np.vstack(loaded_tris)
    all_colors = np.vstack(loaded_colors)

    all_verts = all_tris.reshape(-1, 3)
    min_b = np.min(all_verts, axis=0)
    max_b = np.max(all_verts, axis=0)
    center = (min_b + max_b) / 2.0
    cx, cy = center[0], center[1]

    tris_fl = all_tris
    tris_br = np.copy(all_tris)
    tris_br[:, :, 0] = 2.0 * cx - all_tris[:, :, 0]
    tris_br[:, :, 1] = 2.0 * cy - all_tris[:, :, 1]

    verts_fl = tris_fl.reshape(-1, 3)
    u_fl = verts_fl[:, 0] - verts_fl[:, 1]
    v_fl = -(0.5 * (verts_fl[:, 0] + verts_fl[:, 1]) + verts_fl[:, 2])
    span_u_fl = np.max(u_fl) - np.min(u_fl)
    span_v_fl = np.max(v_fl) - np.min(v_fl)

    verts_br = tris_br.reshape(-1, 3)
    u_br = verts_br[:, 0] - verts_br[:, 1]
    v_br = -(0.5 * (verts_br[:, 0] + verts_br[:, 1]) + verts_br[:, 2])
    span_u_br = np.max(u_br) - np.min(u_br)
    span_v_br = np.max(v_br) - np.min(v_br)

    span_u = max(span_u_fl, span_u_br, 1e-6)
    span_v = max(span_v_fl, span_v_br, 1e-6)
    s = min(800.0 / span_u, 800.0 / span_v)

    view_fl = _render_mesh_view(tris_fl, all_colors, s)
    view_br = _render_mesh_view(tris_br, all_colors, s)

    sheet_w = PAD + view_fl.width + PAD + view_br.width + PAD
    top_h = max(view_fl.height, view_br.height)
    sheet_h = PAD + label_h + label_gap + top_h + PAD

    canvas = Image.new("RGB", (sheet_w, sheet_h), BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    x1 = PAD
    x2 = PAD + view_fl.width + PAD
    label_y = PAD
    img_y = label_y + label_h + label_gap

    draw.text((x1, label_y), "mesh front-left", fill=LABEL_COLOR)
    draw.text((x2, label_y), "mesh back-right", fill=LABEL_COLOR)
    canvas.paste(view_fl, (x1, img_y))
    canvas.paste(view_br, (x2, img_y))

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def export_glb(stls: dict[str, Path], blocks: dict[str, str], path: Path | str) -> None:
    """Export parts as a coloured glTF scene in GLB format."""
    scene = trimesh.Scene()
    for part_name, m, rgb in _load_parts(stls, blocks):
        m = m.copy()
        m.visual.vertex_colors = np.tile(np.array([*rgb, 255], dtype=np.uint8), (len(m.vertices), 1))
        scene.add_geometry(m, node_name=part_name, geom_name=part_name)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = scene.export(file_type="glb")
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
