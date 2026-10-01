"""Find anime face and eyes on a 3D statue mesh and paint them onto the front blocks.

Renders front-view projections of mesh parts, runs YOLO face and eye detection,
and recolours the front-most blocks in the eye regions based on skin, iris, and dark lash lines.
"""
from __future__ import annotations

import logging
import math
import os
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
import trimesh

try:
    from . import config, pixelart
except ImportError:
    from mcmcp import config, pixelart

logger = logging.getLogger(__name__)

MODELS: dict[str, tuple[str, str, float]] = {
    "face": ("deepghs/anime_face_detection", "face_detect_v1.4_s", 0.7),
    "eye": ("deepghs/anime_eye_detection", "eye_detect_v1.0_s", 0.3),
}

_SESSION_CACHE: dict[str, Any] = {}


def _get_texture_image(mesh: trimesh.Trimesh) -> Image.Image | None:
    """Return PIL Image from TextureVisuals (baseColorTexture or image), or None."""
    visual = getattr(mesh, "visual", None)
    if visual is None:
        return None
    mat = getattr(visual, "material", None)
    if mat is not None:
        img = getattr(mat, "baseColorTexture", None) or getattr(mat, "image", None)
        if img is not None:
            return img
    img = getattr(visual, "image", None)
    if img is not None:
        return img
    return None


def _get_vertex_colors(mesh: trimesh.Trimesh) -> np.ndarray | None:
    """Return vertex colors array (N, 3+) if defined on ColorVisuals, or None."""
    visual = getattr(mesh, "visual", None)
    if visual is None:
        return None
    if visual.kind in ("vertex", "color") or isinstance(visual, trimesh.visual.ColorVisuals):
        vc = getattr(visual, "vertex_colors", None)
        if vc is not None and len(vc) > 0:
            return np.asarray(vc)
    return None


def _has_color(mesh: trimesh.Trimesh) -> bool:
    """True if mesh has either an image texture or explicit vertex colours."""
    visual = getattr(mesh, "visual", None)
    if visual is None or visual.kind is None:
        return False
    return _get_texture_image(mesh) is not None or _get_vertex_colors(mesh) is not None


def _overlap(
    a: tuple[float, float, float, float, float],
    b: tuple[float, float, float, float, float],
) -> bool:
    """True if bounding boxes a and b have positive intersection area."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w > 0 and h > 0


def _session(kind: str) -> Any:
    """Cached onnxruntime.InferenceSession for 'face' or 'eye'.

    Downloads model ONNX from HuggingFace to config.MODEL_DIR if missing.
    """
    if kind in _SESSION_CACHE:
        return _SESSION_CACHE[kind]

    repo, name, _ = MODELS[kind]
    path = config.MODEL_DIR / f"{name}.onnx"
    if not path.exists():
        config.MODEL_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(str(path) + ".tmp")
        url = f"https://huggingface.co/{repo}/resolve/main/{name}/model.onnx"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "mcmcp/1.0 (AI-Builder-Minecraft)"},
        )
        resp = urllib.request.urlopen(req, timeout=120)
        if hasattr(resp, "__enter__"):
            with resp:
                content = resp.read()
        elif hasattr(resp, "read"):
            content = resp.read()
        else:
            content = resp

        with open(tmp_path, "wb") as f:
            f.write(content)
        os.replace(tmp_path, path)

    import onnxruntime

    sess = onnxruntime.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    _SESSION_CACHE[kind] = sess
    return sess


def _yolo_post(
    out: np.ndarray,
    conf: float,
    iou: float,
    old_size: tuple[int, int],
    new_size: tuple[int, int],
) -> list[tuple[float, float, float, float, float]]:
    """Filter YOLO outputs, xywh -> xyxy, NMS, and scale back to original image size.

    out: array of shape (4 + classes, N)
    old_size: (width, height)
    new_size: (width, height)
    Returns: list of (x0, y0, x1, y1, score) sorted by score descending.
    """
    old_w, old_h = old_size
    new_w, new_h = new_size

    if out.ndim != 2 or out.shape[0] < 5 or out.shape[1] == 0:
        return []

    class_scores = out[4:, :]
    max_scores = np.max(class_scores, axis=0)

    keep_mask = max_scores > conf
    if not np.any(keep_mask):
        return []

    selected_out = out[:, keep_mask]
    scores = max_scores[keep_mask]

    cx = selected_out[0]
    cy = selected_out[1]
    w = selected_out[2]
    h = selected_out[3]
    x0 = cx - w / 2.0
    y0 = cy - h / 2.0
    x1 = cx + w / 2.0
    y1 = cy + h / 2.0

    order = np.argsort(-scores)
    keep_indices: list[int] = []

    while len(order) > 0:
        i = order[0]
        keep_indices.append(i)
        if len(order) == 1:
            break

        rest = order[1:]
        xx0 = np.maximum(x0[i], x0[rest])
        yy0 = np.maximum(y0[i], y0[rest])
        xx1 = np.minimum(x1[i], x1[rest])
        yy1 = np.minimum(y1[i], y1[rest])

        inter_w = np.maximum(0.0, xx1 - xx0 + 1.0)
        inter_h = np.maximum(0.0, yy1 - yy0 + 1.0)
        has_overlap = (xx1 >= xx0) & (yy1 >= yy0)
        inter = np.where(has_overlap, inter_w * inter_h, 0.0)

        area_i = (x1[i] - x0[i] + 1.0) * (y1[i] - y0[i] + 1.0)
        area_rest = (x1[rest] - x0[rest] + 1.0) * (y1[rest] - y0[rest] + 1.0)
        union = np.maximum(area_i + area_rest - inter, 1e-12)
        iou_vals = inter / union

        survived = iou_vals <= iou
        order = rest[survived]

    scale_x = float(old_w) / float(new_w)
    scale_y = float(old_h) / float(new_h)

    results: list[tuple[float, float, float, float, float]] = []
    for k in keep_indices:
        bx0 = float(np.clip(x0[k] * scale_x, 0.0, old_w))
        by0 = float(np.clip(y0[k] * scale_y, 0.0, old_h))
        bx1 = float(np.clip(x1[k] * scale_x, 0.0, old_w))
        by1 = float(np.clip(y1[k] * scale_y, 0.0, old_h))
        results.append((bx0, by0, bx1, by1, float(scores[k])))

    return results


def _detect(
    kind: str,
    rgb: np.ndarray,
    conf: float,
) -> list[tuple[float, float, float, float, float]]:
    """Run face or eye detector on RGB image array (HxWx3 uint8).

    Returns list of (x0, y0, x1, y1, score) in input pixels, sorted by score descending.
    """
    old_h, old_w = rgb.shape[:2]
    if old_h <= 0 or old_w <= 0:
        return []

    im = Image.fromarray(rgb).resize((640, 640), resample=Image.Resampling.BICUBIC)
    data = (np.asarray(im, dtype=np.float32) / 255.0).transpose((2, 0, 1))[None]

    sess = _session(kind)
    out = sess.run(["output0"], {"images": data})[0][0]

    _, _, nms_iou = MODELS[kind]
    return _yolo_post(out, conf, nms_iou, (old_w, old_h), (640, 640))


def _colours(mesh: trimesh.Trimesh, tri: np.ndarray, pts: np.ndarray) -> np.ndarray | None:
    """Colour of each point pts[i] on triangle tri[i]: the texture at its UV, or the blended vertex
    colours. None if the mesh has neither."""
    bary = trimesh.triangles.points_to_barycentric(mesh.triangles[tri], pts)
    img_tex = _get_texture_image(mesh)
    uv_arr = getattr(mesh.visual, "uv", None)
    if img_tex is not None and uv_arr is not None and len(uv_arr) > 0:
        tex = np.asarray(img_tex.convert("RGB"))
        th, tw = tex.shape[:2]
        uv = (uv_arr[mesh.faces[tri]] * bary[:, :, None]).sum(axis=1)
        row = np.clip(((1.0 - (uv[:, 1] % 1.0)) * (th - 1)).astype(int), 0, th - 1)
        col = np.clip(((uv[:, 0] % 1.0) * (tw - 1)).astype(int), 0, tw - 1)
        return tex[row, col, :3]
    vc = _get_vertex_colors(mesh)
    if vc is None:
        return None
    cols = (vc[mesh.faces[tri]][:, :, :3].astype(np.float32) * bary[:, :, None]).sum(axis=1)
    return np.clip(cols, 0, 255).astype(np.uint8)


def _render(
    parts: list[trimesh.Trimesh],
    x0: float,
    x1: float,
    z0: float,
    z1: float,
    ppb: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Front view of the parts on (X, Z): one ray per pixel along +Y, the nearest hit (smallest Y)
    gives the colour. Exact, so no gaps where the back would show through.

    Returns (rgb uint8 HxWx3 on white, hit bool HxW).
    """
    W = max(1, int(math.ceil((x1 - x0) * ppb)))
    H = max(1, int(math.ceil((z1 - z0) * ppb)))
    img = np.full((H, W, 3), 255, dtype=np.uint8)
    hit = np.zeros((H, W), dtype=bool)
    depth = np.full((H, W), np.inf)
    rows, cols = np.mgrid[0:H, 0:W]
    px = x0 + (cols.ravel() + 0.5) / ppb
    pz = z1 - (rows.ravel() + 0.5) / ppb

    for part in parts:
        if not _has_color(part) or len(part.faces) == 0:
            continue
        lo, hi = part.bounds
        idx = np.nonzero((px >= lo[0]) & (px <= hi[0]) & (pz >= lo[2]) & (pz <= hi[2]))[0]
        if len(idx) == 0:
            continue
        origins = np.column_stack([px[idx], np.full(len(idx), lo[1] - 1.0), pz[idx]])
        dirs = np.tile([0.0, 1.0, 0.0], (len(idx), 1))
        locs, ray_i, tri = part.ray.intersects_location(origins, dirs, multiple_hits=False)
        if len(ray_i) == 0:
            continue
        rgb = _colours(part, tri, locs)
        if rgb is None:
            continue
        r = rows.ravel()[idx[ray_i]]
        c = cols.ravel()[idx[ray_i]]
        front = locs[:, 1] < depth[r, c]
        r, c = r[front], c[front]
        depth[r, c] = locs[front, 1]
        img[r, c] = rgb[front]
        hit[r, c] = True

    return img, hit


def _paint(
    grid: np.ndarray,
    palette: list[str],
    rgb2: np.ndarray,
    hit2: np.ndarray,
    eye_boxes: list[tuple[float, float, float, float, float]],
    skin_rgb: np.ndarray,
    skin_lab: np.ndarray,
    x0: float,
    z1: float,
    ppb2: float,
) -> tuple[np.ndarray, list[str], int]:
    """Paint eye blocks onto the front-most solid voxels of grid.

    Returns (new_grid, new_palette, n_painted).
    """
    grid = grid.copy()
    palette = list(palette)
    blocks, labs = pixelart.get_palette_data()
    n_painted = 0
    seen_columns: set[tuple[int, int]] = set()
    img_h, img_w = rgb2.shape[:2]

    for bx0, by0, bx1, by1, _ in eye_boxes:
        x_min = int(math.floor(x0 + bx0 / ppb2)) - 1
        x_max = int(math.floor(x0 + bx1 / ppb2)) + 1
        z_min = int(math.floor(z1 - by1 / ppb2)) - 1
        z_max = int(math.floor(z1 - by0 / ppb2)) + 1

        for X in range(x_min, x_max + 1):
            if X < 0 or X >= grid.shape[0]:
                continue
            for Z in range(z_min, z_max + 1):
                if Z < 0 or Z >= grid.shape[2]:
                    continue
                if (X, Z) in seen_columns:
                    continue
                seen_columns.add((X, Z))

                ys = np.nonzero(grid[X, :, Z])[0]
                if len(ys) == 0:
                    continue
                front_y = int(ys[0])

                c0 = max(0, int((X - x0) * ppb2))
                c1 = min(img_w, int((X + 1 - x0) * ppb2))
                r0 = max(0, int((z1 - Z - 1) * ppb2))
                r1 = min(img_h, int((z1 - Z) * ppb2))

                if c1 <= c0 or r1 <= r0:
                    continue

                col_hit = hit2[r0:r1, c0:c1]
                if not np.any(col_hit):
                    continue
                pix = rgb2[r0:r1, c0:c1][col_hit]
                if len(pix) == 0:
                    continue

                lab = pixelart.rgb_to_lab(pix)
                dark = lab[:, 0] < min(40.0, skin_lab[0] - 30.0)  # 40: the tested value for pale skin
                dE = np.linalg.norm(lab - skin_lab, axis=1)
                iris = (~dark) & (dE > 20.0)
                skin = (~dark) & (~iris)

                dark_mean = float(dark.mean())
                iris_mean = float(iris.mean())
                skin_mean = float(skin.mean())

                if dark_mean >= 0.2:
                    dark_L = lab[dark, 0]
                    col_rgb = pix[dark][dark_L <= np.median(dark_L)].mean(axis=0)
                elif iris_mean >= 0.3:
                    col_rgb = pix[iris].mean(axis=0)
                elif skin_mean >= 0.5:
                    col_rgb = skin_rgb
                else:
                    continue

                chosen_lab = pixelart.rgb_to_lab(np.asarray(col_rgb, dtype=np.float64)[None])
                b_idx = int(pixelart.find_nearest_palette(chosen_lab, labs)[0])
                block_id = blocks[b_idx]

                if block_id in palette:
                    pal_idx = palette.index(block_id)
                else:
                    if len(palette) >= 256:
                        continue
                    pal_idx = len(palette)
                    palette.append(block_id)

                grid[X, front_y, Z] = pal_idx
                n_painted += 1

    return grid, palette, n_painted


def _cheek_skin(
    rgb2: np.ndarray,
    hit2: np.ndarray,
    eye_boxes: list[tuple[float, float, float, float, float]],
) -> np.ndarray | None:
    """Median colour of hit pixels in strips below the eye boxes, or None if < 20 pixels."""
    img_h, img_w = rgb2.shape[:2]
    all_px: list[np.ndarray] = []
    for x0, y0, x1, y1, _ in eye_boxes:
        h = y1 - y0
        r0 = max(0, min(img_h, int(y1 + 0.3 * h)))
        r1 = max(0, min(img_h, int(y1 + 1.3 * h)))
        c0 = max(0, min(img_w, int(x0)))
        c1 = max(0, min(img_w, int(x1)))
        if r1 <= r0 or c1 <= c0:
            continue
        sub_rgb = rgb2[r0:r1, c0:c1]
        sub_hit = hit2[r0:r1, c0:c1]
        px = sub_rgb[sub_hit]
        if len(px) > 0:
            all_px.append(px)

    if not all_px:
        return None
    pooled = np.concatenate(all_px, axis=0)
    if len(pooled) < 20:
        return None
    return np.asarray(np.median(pooled, axis=0), dtype=float)


def paint_eyes(
    grid: np.ndarray,
    palette: list[str],
    parts: list[trimesh.Trimesh],
) -> tuple[np.ndarray, list[str], int]:
    """Find face and eyes on mesh parts and paint eye blocks on the front of grid.

    Returns (grid, palette, n_painted).
    Never raises; returns inputs unchanged with 0 on any error or missing face/eyes/skin.
    """
    try:
        if not parts or not any(_has_color(p) for p in parts):
            logger.info("No part has colour; skipping eye painting")
            return grid, palette, 0

        grid_copy = grid.copy()
        palette_copy = list(palette)

        # 1. Render 1 (whole statue)
        x0 = 0.0
        x1 = float(grid.shape[0])
        z0 = 0.0
        z1 = float(grid.shape[2])
        if x1 <= 0 or z1 <= 0:
            return grid, palette, 0

        ppb = min(1000.0 / z1, 1500.0 / x1)
        rgb1, hit1 = _render(parts, x0, x1, z0, z1, ppb)

        faces = _detect("face", rgb1, 0.5)
        if not faces:
            logger.info("No face found; skipping eye painting")
            return grid, palette, 0

        best_face = max(faces, key=lambda f: f[4])
        fx0, fy0, fx1, fy1, _ = best_face

        # 2. Face box in grid units
        face_gx0 = x0 + fx0 / ppb
        face_gx1 = x0 + fx1 / ppb
        face_gz0 = z1 - fy1 / ppb
        face_gz1 = z1 - fy0 / ppb

        face_w = face_gx1 - face_gx0
        face_h = face_gz1 - face_gz0
        if face_w <= 1e-6 or face_h <= 1e-6:
            return grid, palette, 0

        # 3. Render 2 (face region)
        pad_x = 0.3 * face_w
        pad_z = 0.3 * face_h
        r2_x0 = max(0.0, face_gx0 - pad_x)
        r2_x1 = min(x1, face_gx1 + pad_x)
        r2_z0 = max(0.0, face_gz0 - pad_z)
        r2_z1 = min(z1, face_gz1 + pad_z)

        ppb2 = 350.0 / face_w
        rgb2, hit2 = _render(parts, r2_x0, r2_x1, r2_z0, r2_z1, ppb2)

        # 4. Face box in render-2 pixels & eye detection
        f2_x0 = (face_gx0 - r2_x0) * ppb2
        f2_x1 = (face_gx1 - r2_x0) * ppb2
        f2_y0 = (r2_z1 - face_gz1) * ppb2
        f2_y1 = (r2_z1 - face_gz0) * ppb2

        h2, w2 = rgb2.shape[:2]
        crop_x0 = max(0, int(round(f2_x0)))
        crop_y0 = max(0, int(round(f2_y0)))
        crop_x1 = min(w2, int(round(f2_x1)))
        crop_y1 = min(h2, int(round(f2_y1)))

        cands = list(_detect("eye", rgb2, 0.15))
        if crop_x1 > crop_x0 and crop_y1 > crop_y0:
            crop = rgb2[crop_y0:crop_y1, crop_x0:crop_x1]
            crop_eyes = _detect("eye", crop, 0.15)
            for cx0, cy0, cx1, cy1, sc in crop_eyes:
                cands.append((cx0 + crop_x0, cy0 + crop_y0, cx1 + crop_x0, cy1 + crop_y0, sc))

        cands.sort(key=lambda b: b[4], reverse=True)
        eye_boxes: list[tuple[float, float, float, float, float]] = []
        for b in cands:
            bcx = (b[0] + b[2]) / 2.0
            bcy = (b[1] + b[3]) / 2.0
            if f2_x0 <= bcx <= f2_x1 and f2_y0 <= bcy <= f2_y1:
                if not any(_overlap(b, kept) for kept in eye_boxes):
                    eye_boxes.append(b)
                    if len(eye_boxes) == 2:
                        break

        if not eye_boxes:
            logger.info("No eye found; skipping eye painting")
            return grid, palette, 0

        # 5. Skin: the cheeks below the eyes (a mask, scarf or hand can cover the lower face)
        skin_rgb = _cheek_skin(rgb2, hit2, eye_boxes)
        if skin_rgb is None:
            f2_w = f2_x1 - f2_x0
            f2_h = f2_y1 - f2_y0
            skin_x0 = max(0, int(round(f2_x0 + 0.25 * f2_w)))
            skin_x1 = min(w2, int(round(f2_x0 + 0.75 * f2_w)))
            skin_y0 = max(0, int(round(f2_y0 + 0.5 * f2_h)))
            skin_y1 = min(h2, int(round(f2_y1)))

            if skin_x1 <= skin_x0 or skin_y1 <= skin_y0:
                logger.info("Fewer than 20 skin pixels; skipping eye painting")
                return grid, palette, 0

            sub_rgb = rgb2[skin_y0:skin_y1, skin_x0:skin_x1]
            sub_hit = hit2[skin_y0:skin_y1, skin_x0:skin_x1]
            skin_px = sub_rgb[sub_hit]
            if len(skin_px) < 20:
                logger.info("Fewer than 20 skin pixels; skipping eye painting")
                return grid, palette, 0

            skin_rgb = np.median(skin_px, axis=0)

        skin_lab = pixelart.rgb_to_lab(skin_rgb[None])[0]

        # 6. Paint
        return _paint(grid_copy, palette_copy, rgb2, hit2, eye_boxes, skin_rgb, skin_lab, r2_x0, r2_z1, ppb2)

    except Exception as e:
        logger.warning("Eyes not painted: %s", e)
        return grid, palette, 0
