"""Posing, face stamping, colour mapping, and review previews for figure builds."""
from __future__ import annotations

import io
import json
import logging
import math
from pathlib import Path
import re
from typing import Any, NamedTuple

import numpy as np
from PIL import Image, ImageDraw
import trimesh
from trimesh.transformations import rotation_matrix as rot

try:
    from . import build
except ImportError:
    from mcmcp import build

logger = logging.getLogger("mcmcp")

_COLOURS = [
    "white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray",
    "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black",
]

# colors.py candidates WITHOUT "obsidian" (dark navy must not match obsidian)
CANDIDATES: list[str] = (
    [f"{c}_concrete" for c in _COLOURS]
    + [f"{c}_wool" for c in _COLOURS]
    + [f"{c}_terracotta" for c in _COLOURS]
    + ["terracotta"]
    + [
        "quartz_block", "smooth_quartz", "calcite", "snow_block",
        "sandstone", "smooth_sandstone", "red_sandstone", "smooth_red_sandstone", "end_stone",
        "oak_planks", "spruce_planks", "birch_planks", "jungle_planks", "acacia_planks",
        "dark_oak_planks", "mangrove_planks", "cherry_planks", "bamboo_planks", "crimson_planks", "warped_planks",
        "stone", "smooth_stone", "andesite", "polished_andesite", "diorite", "polished_diorite",
        "granite", "polished_granite", "deepslate", "polished_deepslate", "blackstone", "polished_blackstone",
        "tuff", "polished_tuff", "stone_bricks", "bricks", "mud_bricks", "packed_mud", "clay",
        "nether_bricks", "red_nether_bricks", "iron_block", "gold_block", "waxed_copper_block",
        "waxed_exposed_copper", "waxed_weathered_copper", "waxed_oxidized_copper",
        "lapis_block", "diamond_block", "emerald_block", "amethyst_block", "prismarine",
        "prismarine_bricks", "dark_prismarine", "purpur_block", "packed_ice", "blue_ice",
        "moss_block", "honeycomb_block", "nether_wart_block", "warped_wart_block",
    ]
)

TURN: dict[str, int] = {
    "front": 0,
    "slight": 20,
    "three-quarter": 45,
    "side": 90,
    "three-quarter back": 135,
    "back": 180,
}

LEAN: dict[str, int] = {
    "none": 0,
    "slight": 10,
    "strong": 20,
}

FENCE_RE = re.compile(r"```([a-zA-Z0-9_-]*)[^\S\r\n]*\r?\n([\s\S]*?)```")


def _lab(rgb: tuple[float, float, float] | list[int] | np.ndarray) -> tuple[float, float, float]:
    """Convert sRGB (0-255) to CIELAB (D65)."""
    def lin(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(float(v)) for v in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def de2000(l1: tuple[float, float, float], l2: tuple[float, float, float]) -> float:
    """CIEDE2000 colour difference of two CIELAB colours (plain CIELAB distance turns blues purple)."""
    L1, a1, b1 = l1
    L2, a2, b2 = l2
    C1, C2 = math.hypot(a1, b1), math.hypot(a2, b2)
    Cb = (C1 + C2) / 2
    G = 0.5 * (1 - math.sqrt(Cb**7 / (Cb**7 + 25**7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360
    h2p = math.degrees(math.atan2(b2, a2p)) % 360
    dLp, dCp = L2 - L1, C2p - C1p
    dh = h2p - h1p
    if C1p * C2p == 0:
        dh = 0.0
    elif dh > 180:
        dh -= 360
    elif dh < -180:
        dh += 360
    dHp = 2 * math.sqrt(C1p * C2p) * math.sin(math.radians(dh / 2))
    Lbp, Cbp = (L1 + L2) / 2, (C1p + C2p) / 2
    if C1p * C2p == 0:
        hbp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbp = (h1p + h2p) / 2
    else:
        hbp = (h1p + h2p + 360) / 2 if h1p + h2p < 360 else (h1p + h2p - 360) / 2
    T = (
        1
        - 0.17 * math.cos(math.radians(hbp - 30))
        + 0.24 * math.cos(math.radians(2 * hbp))
        + 0.32 * math.cos(math.radians(3 * hbp + 6))
        - 0.20 * math.cos(math.radians(4 * hbp - 63))
    )
    dth = 30 * math.exp(-(((hbp - 275) / 25) ** 2))
    RC = 2 * math.sqrt(Cbp**7 / (Cbp**7 + 25**7))
    SL = 1 + 0.015 * (Lbp - 50) ** 2 / math.sqrt(20 + (Lbp - 50) ** 2)
    SC, SH = 1 + 0.045 * Cbp, 1 + 0.015 * Cbp * T
    RT = -math.sin(math.radians(2 * dth)) * RC
    return math.sqrt((dLp / SL) ** 2 + (dCp / SC) ** 2 + (dHp / SH) ** 2 + RT * (dCp / SC) * (dHp / SH))


def nearest(hex_colour: str) -> str:
    """Find the CANDIDATES block whose average texture colour is closest to hex_colour (CIEDE2000)."""
    block_colors = build._block_colors()
    h = hex_colour.lstrip("#")
    target = _lab((int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)))
    best = min(
        (b for b in CANDIDATES if "minecraft:" + b in block_colors),
        key=lambda b: de2000(_lab(block_colors["minecraft:" + b]), target),
    )
    return "minecraft:" + best


def apply_colors(data: dict, blocks: dict[str, str]) -> None:
    """Apply colors from parsed JSON data to blocks dict in-place."""
    if not isinstance(data, dict):
        return
    colors = data.get("colors")
    if not isinstance(colors, dict):
        return
    for part, colour in colors.items():
        if part in blocks and isinstance(colour, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}", colour):
            try:
                old = blocks[part]
                new = nearest(colour)
                blocks[part] = new
                logger.info("Colour %s: %s %s -> %s", part, colour, old, new)
            except Exception:
                pass


def features(raw: str | None) -> tuple[dict | None, dict | None]:
    """Extract (pose, face) dicts from the first json code fence block in raw."""
    if not raw or not isinstance(raw, str):
        return None, None
    try:
        json_blocks = [c for lang, c in FENCE_RE.findall(raw) if lang.lower() == "json"]
        if not json_blocks:
            return None, None
        data = json.loads(json_blocks[0])
        if not isinstance(data, dict):
            return None, None
        pose = data.get("pose")
        face = data.get("face")
        pose_out = pose if isinstance(pose, dict) and len(pose) > 0 else None
        face_out = face if isinstance(face, dict) and len(face) > 0 else None
        return pose_out, face_out
    except Exception:
        return None, None


class Posed(NamedTuple):
    heads: list[trimesh.Trimesh]  # the posed head part meshes, or all meshes when there are no head parts
    centre: np.ndarray | None     # the posed face centre (design coordinates), None without a face
    tilt: float                   # head tilt in degrees (0 without a pose)
    head_R: np.ndarray            # 3x3 rotation the head went through (identity without a pose)
    grid_min: np.ndarray          # floor of the min corner of all loaded meshes after posing, exactly what
                                  # build._load_meshes uses as the scale-down origin


def degrees(value: Any, table: dict[str, int], what: str) -> float:
    """A pose word (or a number of degrees) as degrees; unknown words are 0."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if value in table:
        return float(table[value])
    logger.warning("pose %s: unknown %r, using 0", what, value)
    return 0.0


def sign(side: Any, what: str) -> int:
    """+1 for 'right', -1 for 'left'."""
    if side in ("right", "left"):
        return 1 if side == "right" else -1
    if side is not None:
        logger.warning("pose %s: unknown side %r, using right", what, side)
    return 1


def pose_meshes(
    meshes: dict[str, trimesh.Trimesh],
    pose: dict,
    face_centre: Any = None,
) -> tuple[np.ndarray | None, float, list[str], np.ndarray]:
    """Pose meshes in place according to pose dict."""
    body = pose.get("body") or {}
    head = pose.get("head") or {}
    head_parts_req = pose.get("head_parts") or []
    heads = [p for p in head_parts_req if p in meshes]
    for p in head_parts_req:
        if p not in meshes:
            logger.warning("pose: head part %r has no mesh, ignored", p)

    lo = np.min([m.bounds[0] for m in meshes.values()], axis=0)
    hi = np.max([m.bounds[1] for m in meshes.values()], axis=0)
    ground0 = lo[2]
    body_yaw = sign(body.get("toward"), "body.toward") * degrees(body.get("turn"), TURN, "body.turn")
    head_yaw = sign(head.get("toward"), "head.toward") * degrees(head.get("turn"), TURN, "head.turn")
    lean = sign(body.get("lean_toward"), "body.lean_toward") * degrees(body.get("lean"), LEAN, "body.lean")
    tilt = sign(head.get("tilt_toward"), "head.tilt_toward") * degrees(head.get("tilt"), LEAN, "head.tilt")
    rel = (head_yaw - body_yaw + 180) % 360 - 180
    if rel == -180:
        rel = 180.0
    if abs(rel) > 120:
        logger.warning("pose: the head is turned %.0f degrees from the body", rel)

    Z, Y = [0, 0, 1], [0, 1, 0]

    def apply(M: np.ndarray, names: list[str]) -> None:
        for n in names:
            meshes[n].apply_transform(M)

    def move(M: np.ndarray, p: np.ndarray | list[float]) -> np.ndarray:
        return (M @ [*p, 1])[:3]

    def bounds(names: list[str] | dict[str, trimesh.Trimesh]) -> tuple[np.ndarray, np.ndarray]:
        part_list = list(names.keys()) if isinstance(names, dict) else names
        return (
            np.min([meshes[n].bounds[0] for n in part_list], axis=0),
            np.max([meshes[n].bounds[1] for n in part_list], axis=0),
        )

    if heads:
        hlo, hhi = bounds(heads)
        neck = np.array(pose.get("neck") or [(hlo[0] + hhi[0]) / 2, (hlo[1] + hhi[1]) / 2, hlo[2]], dtype=float)
    else:
        neck = np.zeros(3)

    centre = None if face_centre is None else np.array(face_centre, dtype=float)
    M1 = rot(math.radians(body_yaw), Z, (lo + hi) / 2)
    apply(M1, list(meshes.keys()))
    neck1 = move(M1, neck)
    head_Ms = [M1]  # the transforms the head went through, in order
    if heads:
        M2 = rot(math.radians(rel), Z, neck1)
        apply(M2, heads)
        head_Ms.append(M2)
    lo2, hi2 = bounds(meshes)
    M3 = rot(math.radians(lean), Y, [(lo2[0] + hi2[0]) / 2, (lo2[1] + hi2[1]) / 2, lo2[2]])
    apply(M3, list(meshes.keys()))
    head_Ms.append(M3)
    if heads:
        M4 = rot(math.radians(tilt - lean), Y, move(M3, neck1))
        apply(M4, heads)
        head_Ms.append(M4)
    dz = ground0 - bounds(meshes)[0][2]
    for m in meshes.values():
        m.apply_translation([0, 0, dz])
    if centre is not None:
        for M in head_Ms:
            centre = move(M, centre)
        centre[2] += dz

    logger.info(
        "pose: body yaw %.0f lean %.0f, head yaw %.0f (rel %.0f) tilt %.0f, neck %s, head parts %s",
        body_yaw,
        lean,
        head_yaw,
        rel,
        tilt,
        np.round(neck, 1).tolist(),
        heads,
    )
    head_R = np.linalg.multi_dot(head_Ms[::-1])[:3, :3] if len(head_Ms) > 1 else head_Ms[0][:3, :3]
    return centre, tilt, heads, head_R


def pose_parts(
    stls: dict[str, Path],
    pose: dict | None,
    face: dict | None,
) -> Posed | None:
    """Pose meshes from STLs and export posed STLs in-place. Returns Posed or None."""
    if not (pose or face) or not stls:
        return None

    meshes: dict[str, trimesh.Trimesh] = {}
    for name, path in stls.items():
        try:
            m = trimesh.load(str(path))
            if isinstance(m, trimesh.Scene):
                m = trimesh.util.concatenate(m.dump())
            if m is not None and not m.is_empty and len(m.faces) > 0:
                meshes[name] = m
        except Exception:
            pass

    if not meshes:
        return None

    centre = None
    if isinstance(face, dict):
        c = face.get("center")
        if isinstance(c, (list, tuple, np.ndarray)) and len(c) == 3:
            try:
                centre = np.array([float(x) for x in c], dtype=float)
            except (ValueError, TypeError):
                centre = None

    if pose:
        centre, tilt, head_names, head_R = pose_meshes(meshes, pose, face_centre=centre)
        for name, m in meshes.items():
            m.export(str(stls[name]))
        heads = [meshes[p] for p in head_names if p in meshes] or list(meshes.values())
    else:
        tilt = 0.0
        head_R = np.eye(3)
        heads = list(meshes.values())

    union_min = np.min([m.bounds[0] for m in meshes.values()], axis=0)
    grid_min = np.floor(union_min)

    return Posed(heads=heads, centre=centre, tilt=tilt, head_R=head_R, grid_min=grid_min)


def paint_face(
    grid: np.ndarray,
    palette: list[str],
    offset: np.ndarray | tuple[int, int, int] | list[int],
    scale: float,
    face: dict,
    posed: Posed,
) -> list[tuple[int, int, int, str]]:
    """Stamp face grid on the frontmost head voxels around the posed face centre."""
    if (
        not isinstance(face, dict)
        or not isinstance(posed, Posed)
        or posed.centre is None
    ):
        logger.warning("face: invalid face data or missing posed face centre")
        return []

    rows = face.get("rows")
    key = face.get("key")
    if (
        not isinstance(rows, list)
        or len(rows) == 0
        or not all(isinstance(r, str) for r in rows)
        or not isinstance(key, dict)
    ):
        logger.warning("face: missing or invalid rows/key")
        return []

    ids: dict[str, str] = {}
    for ch, val in key.items():
        if not (isinstance(ch, str) and len(ch) == 1 and isinstance(val, str)):
            logger.warning("face: invalid key entry %r: %r, skipped", ch, val)
            continue
        if re.fullmatch(r"#[0-9a-fA-F]{6}", val):
            ids[ch] = nearest(val)
        elif re.fullmatch(r"(minecraft:)?[a-z0-9_]+", val):
            ids[ch] = val if val.startswith("minecraft:") else f"minecraft:{val}"
        else:
            logger.warning("face: invalid key value %r for %r, skipped", val, ch)

    W = max(len(r) for r in rows)
    H = len(rows)
    if W == 0:
        logger.warning("face: rows are empty")
        return []
    rows_padded = [r.ljust(W, ".") for r in rows]

    grid_min = posed.grid_min
    if scale != 1.0:
        centre = grid_min + scale * (posed.centre - grid_min)
        head_meshes = []
        for m in posed.heads:
            m_copy = m.copy()
            m_copy.apply_translation(-grid_min)
            m_copy.apply_scale(scale)
            m_copy.apply_translation(grid_min)
            head_meshes.append(m_copy)
    else:
        centre = posed.centre
        head_meshes = posed.heads

    R = posed.head_R
    cx, cy, cz = centre
    phi = math.radians(posed.tilt)
    n = R @ np.array([0.0, -1.0, 0.0])
    a = R @ np.array([1.0, 0.0, 0.0])
    b = R @ np.array([0.0, 0.0, 1.0])
    attached = n[1] < -0.5
    inv = np.linalg.inv([[a[0], b[0]], [a[2], b[2]]]) if attached else None
    w = math.ceil(math.hypot(W, H) / 2 * max(1.0, scale)) + 2

    cells = []  # (i, j, k, block id, gap)
    off_0, off_1, off_2 = int(offset[0]), int(offset[1]), int(offset[2])
    for X in range(math.floor(cx - w), math.ceil(cx + w) + 1):
        for Z in range(math.floor(cz - w), math.ceil(cz + w) + 1):
            i, k = X - off_0, Z - off_2
            if not (0 <= i < grid.shape[0] and 0 <= k < grid.shape[2]):
                continue
            dX, dZ = X + 0.5 - cx, Z + 0.5 - cz
            if attached:
                u, v = inv @ [dX, dZ]
            else:
                u = dX * math.cos(phi) - dZ * math.sin(phi)
                v = dX * math.sin(phi) + dZ * math.cos(phi)
            c = math.floor(u / scale + W / 2)
            r = math.floor(H / 2 - v / scale)
            if not (0 <= c < W and 0 <= r < H) or rows_padded[r][c] not in ids:
                continue
            solid = np.nonzero(grid[i, :, k])[0]
            if len(solid):
                j = int(solid[0])
                gap = abs(off_1 + j + 0.5 - (cy + u * a[1] + v * b[1])) if attached else 0.0
                cells.append((i, j, k, ids[rows_padded[r][c]], gap))

    if not cells:
        logger.info(
            "face: %s mode, painted 0 cells, 0 skipped as not head, 0 skipped by depth",
            "attached" if attached else "view",
        )
        return []

    pts = np.array(
        [[off_0 + i + 0.5, off_1 + j + 0.5, off_2 + k + 0.5] for i, j, k, _, _ in cells]
    ).reshape(-1, 3)
    inside = np.zeros(len(cells), dtype=bool)
    for m in head_meshes:
        inside |= build._contains(m, pts)

    painted: list[tuple[int, int, int, str]] = []
    deep = 0
    palette_full_warned = False

    for (i, j, k, block, gap), ok in zip(cells, inside):
        if not ok:
            continue
        if gap > 3:
            deep += 1
            continue
        if block not in palette:
            if len(palette) >= 256:
                if not palette_full_warned:
                    logger.warning("face: palette full (256 blocks), skipping face blocks")
                    palette_full_warned = True
                continue
            palette.append(block)
        grid[i, j, k] = palette.index(block)
        painted.append((i, j, k, block))

    logger.info(
        "face: %s mode, painted %d cells, %d skipped as not head, %d skipped by depth",
        "attached" if attached else "view",
        len(painted),
        int((~inside).sum()),
        deep,
    )
    return painted


def restore_face(
    grid: np.ndarray,
    palette: list[str],
    boxes: list,
    painted: list,
) -> tuple[np.ndarray, list]:
    """Restore painted cells after smooth and drop boxes in front of them."""
    for i, j, k, block in painted:
        if block in palette:
            grid[i, j, k] = palette.index(block)
    kept_boxes = [
        b
        for b in boxes
        if not any(
            b[0] <= i <= b[3] and b[2] <= k <= b[5] and b[1] <= j
            for i, j, k, _ in painted
        )
    ]
    return grid, kept_boxes


def head_closeup(
    grid: np.ndarray,
    palette: list[str],
    offset: np.ndarray | tuple[int, int, int] | list[int],
    scale: float,
    posed: Posed,
) -> bytes | None:
    """The front view of the head region (~600 px tall) as PNG bytes."""
    if not posed.heads:
        return None

    if scale != 1.0:
        raw_lo = np.min([m.bounds[0] for m in posed.heads], axis=0)
        raw_hi = np.max([m.bounds[1] for m in posed.heads], axis=0)
        p1 = posed.grid_min + scale * (raw_lo - posed.grid_min)
        p2 = posed.grid_min + scale * (raw_hi - posed.grid_min)
        head_lo = np.minimum(p1, p2)
        head_hi = np.maximum(p1, p2)
    else:
        head_lo = np.min([m.bounds[0] for m in posed.heads], axis=0)
        head_hi = np.max([m.bounds[1] for m in posed.heads], axis=0)

    off = np.asarray(offset)
    lo = head_lo - 3 - off
    hi = head_hi + 3 - off
    i0, i1 = max(0, math.floor(lo[0])), min(grid.shape[0], math.ceil(hi[0]))
    k0, k1 = max(0, math.floor(lo[2])), min(grid.shape[2], math.ceil(hi[2]))
    if i0 >= i1 or k0 >= k1:
        return None

    subgrid = grid[i0:i1, :, k0:k1]
    if subgrid.size == 0 or not np.any(subgrid > 0):
        return None

    rgb = np.array(
        [[30, 30, 30] if n == 0 else build.block_color(b) for n, b in enumerate(palette)],
        dtype=np.float32,
    )
    view = build._render_view(subgrid, rgb, proj_axis=1)
    if view.shape[0] == 0 or view.shape[1] == 0:
        return None

    cell = max(1, round(600 / view.shape[0]))
    img = Image.fromarray(np.repeat(np.repeat(view, cell, axis=0), cell, axis=1), mode="RGB")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def zoomed_reference(box: Any, ref_png: bytes) -> bytes | None:
    """Crop the reference to box [x0, y0, x1, y1] (fractions across from left, up from bottom),
    padded by 30% of box size on each side and clamped, scaled to ~600px tall."""
    if not (
        isinstance(box, (list, tuple))
        and len(box) == 4
        and all(isinstance(v, (int, float)) for v in box)
    ):
        logger.warning("invalid face box %r, skipped", box)
        return None

    x0, y0, x1, y1 = box
    min_x, max_x = min(x0, x1), max(x0, x1)
    min_y, max_y = min(y0, y1), max(y0, y1)
    if min_x >= max_x or min_y >= max_y or min_x < 0 or min_y < 0 or max_x > 1 or max_y > 1:
        logger.warning("invalid face box %r, skipped", box)
        return None

    try:
        with Image.open(io.BytesIO(ref_png)) as img:
            W, H = img.size
            left_col, right_col = min_x * W, max_x * W
            top_row, bottom_row = (1.0 - max_y) * H, (1.0 - min_y) * H
            bw, bh = right_col - left_col, bottom_row - top_row
            pad_x, pad_y = 0.3 * bw, 0.3 * bh
            c_left = max(0, int(round(left_col - pad_x)))
            c_right = min(W, int(round(right_col + pad_x)))
            c_top = max(0, int(round(top_row - pad_y)))
            c_bottom = min(H, int(round(bottom_row + pad_y)))
            if c_right <= c_left or c_bottom <= c_top:
                logger.warning("face box %r cropped to empty, skipped", box)
                return None
            cropped = img.crop((c_left, c_top, c_right, c_bottom))
            scale = 600.0 / cropped.height
            new_w = max(1, round(cropped.width * scale))
            scaled = cropped.resize((new_w, 600), Image.LANCZOS)
            out = io.BytesIO()
            scaled.save(out, format="PNG")
            return out.getvalue()
    except Exception as e:
        logger.warning("failed to crop reference image: %s", e)
        return None


def composite(preview_png: bytes, panels: list[tuple[str, bytes]] | bytes | bytearray) -> bytes:
    """The preview on the left, then panels on the right (top-aligned, labelled), on the preview's dark background."""
    if isinstance(panels, (bytes, bytearray)):
        panels = [("head close-up (front)", panels)]

    try:
        left = Image.open(io.BytesIO(preview_png)).convert("RGB")
    except Exception:
        return preview_png

    images: list[tuple[str, Image.Image]] = []
    for lbl, png in panels:
        if png:
            try:
                img = Image.open(io.BytesIO(png)).convert("RGB")
                images.append((lbl, img))
            except Exception:
                pass

    if not images:
        return preview_png

    pad = 12
    total_w = left.width + pad + sum(img.width + pad for _, img in images) + pad
    max_h = max([left.height] + [img.height + 3 * pad for _, img in images])
    canvas = Image.new("RGB", (total_w, max_h), (30, 30, 30))
    canvas.paste(left, (0, 0))
    draw = ImageDraw.Draw(canvas)
    x = left.width + 2 * pad
    for label, img in images:
        draw.text((x, pad), label, fill=(235, 235, 235))
        canvas.paste(img, (x, 3 * pad))
        x += img.width + pad
    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()

