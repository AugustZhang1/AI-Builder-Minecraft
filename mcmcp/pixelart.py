"""Image -> flat block picture (no AI). Grid format matches build.voxelize: shape (W, 1, H),
0 = air, palette[0] == "minecraft:air", z = 0 is the bottom row of the picture.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
from PIL import Image

try:
    from . import config
except ImportError:
    import config


class PixelArtError(Exception):
    pass


_PALETTE_CACHE: dict[str, list[int]] | None = None
_PALETTE_DATA: tuple[list[str], np.ndarray] | None = None


def load_palette(palette_path: Path | str | None = None) -> dict[str, list[int]]:
    """Load palette.json mapping block IDs to [R, G, B] values."""
    global _PALETTE_CACHE
    if palette_path is None:
        if _PALETTE_CACHE is not None:
            return _PALETTE_CACHE
        palette_path = config.PALETTE_FILE
    with open(palette_path, "r", encoding="utf-8") as f:
        data: dict[str, list[int]] = json.load(f)
    if palette_path == config.PALETTE_FILE or palette_path is None:
        _PALETTE_CACHE = data
    return data


PALETTE = load_palette()


def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """Convert sRGB array in [0, 255] to CIELAB (D65 illuminant)."""
    v = rgb.astype(np.float64) / 255.0
    # sRGB -> linear sRGB
    linear = np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)

    # linear sRGB -> XYZ (D65)
    # fmt: off
    m_srgb_to_xyz = np.array([
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ], dtype=np.float64)
    # fmt: on
    xyz = linear @ m_srgb_to_xyz.T

    # Normalize by D65 reference white
    d65 = np.array([0.95047, 1.00000, 1.08883], dtype=np.float64)
    t = xyz / d65

    delta = 6.0 / 29.0
    delta_cubed = delta ** 3  # approx 0.008856

    f_t = np.where(t > delta_cubed, np.cbrt(t), (t / (3.0 * delta ** 2)) + (4.0 / 29.0))

    fx = f_t[..., 0]
    fy = f_t[..., 1]
    fz = f_t[..., 2]

    l_star = 116.0 * fy - 16.0
    a_star = 500.0 * (fx - fy)
    b_star = 200.0 * (fy - fz)

    return np.stack([l_star, a_star, b_star], axis=-1)


def get_palette_data() -> tuple[list[str], np.ndarray]:
    """Returns cached (block_id_list, lab_array_of_shape_(K, 3))."""
    global _PALETTE_DATA
    if _PALETTE_DATA is None:
        palette_dict = load_palette()
        blocks = list(palette_dict.keys())
        rgbs = np.array(list(palette_dict.values()), dtype=np.uint8)
        labs = rgb_to_lab(rgbs)
        _PALETTE_DATA = (blocks, labs)
    return _PALETTE_DATA


def find_nearest_palette(pixels_lab: np.ndarray, palette_lab: np.ndarray) -> np.ndarray:
    """Vectorized nearest palette colour matching in CIELAB."""
    result = np.empty(len(pixels_lab), dtype=np.int32)
    for i in range(0, len(pixels_lab), 32768):
        chunk = pixels_lab[i:i + 32768]
        dists = np.sum((chunk[:, None, :] - palette_lab[None, :, :]) ** 2, axis=2)
        result[i:i + len(chunk)] = np.argmin(dists, axis=1)
    return result


def native_image(image_bytes: bytes, max_side: int) -> Image.Image:
    """Open an image as RGBA. If it is an upscaled sprite, shrink it back to its original
    pixels; trim fully transparent borders; downscale (keeping aspect) if a side > max_side."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as raw_img:
            try:
                raw_img.seek(0)
            except (AttributeError, EOFError):
                pass
            img = raw_img.convert("RGBA")
    except Exception as e:
        raise PixelArtError(f"Cannot open image: {e}") from e

    arr = np.array(img, dtype=np.uint8)
    if arr.size == 0 or not np.any(arr[:, :, 3] >= 128):
        raise PixelArtError("Image has no opaque pixel")

    h, w = arr.shape[:2]

    # Sprite detection on untrimmed image:
    # Find largest k from 64 down to 2 dividing width and height such that every k x k cell is consistent
    # A cell is consistent if >= 85% of its texels are within 48 of the cell's median colour
    # (tolerates JPEG ringing at edges) and its opacity is uniform. The shrunk sprite must keep
    # at least 8 pixels on its shorter side, so flat-colour images are not collapsed.
    for k in range(min(64, h // 8, w // 8), 1, -1):
        if h % k != 0 or w % k != 0:
            continue
        cells = arr.reshape(h // k, k, w // k, k, 4).transpose(0, 2, 1, 3, 4).reshape(h // k, w // k, k * k, 4)
        opaque = cells[..., 3] >= 128
        if not np.all(np.all(opaque, axis=2) | np.all(~opaque, axis=2)):
            continue
        median = np.median(cells[..., :3], axis=2, keepdims=True)
        close = np.all(np.abs(cells[..., :3] - median) <= 48, axis=3)
        if np.all(close.mean(axis=2) >= 0.85):
            arr = np.concatenate([median[:, :, 0, :], cells[:, :, :1, 3]], axis=2).astype(np.uint8)
            break

    # Trim fully transparent border rows/columns (alpha < 128)
    opaque_mask = arr[:, :, 3] >= 128
    if not np.any(opaque_mask):
        raise PixelArtError("Image has no opaque pixel after sprite detection")

    rows = np.any(opaque_mask, axis=1)
    cols = np.any(opaque_mask, axis=0)
    rmin = int(np.argmax(rows))
    rmax = len(rows) - 1 - int(np.argmax(rows[::-1]))
    cmin = int(np.argmax(cols))
    cmax = len(cols) - 1 - int(np.argmax(cols[::-1]))
    arr = arr[rmin : rmax + 1, cmin : cmax + 1, :]

    img = Image.fromarray(arr, mode="RGBA")

    # Downscale if longest side > max_side keeping aspect
    cur_max = max(img.width, img.height)
    if cur_max > max_side:
        scale = max_side / cur_max
        new_w = max(1, round(img.width * scale))
        new_h = max(1, round(img.height * scale))
        img = img.resize((new_w, new_h), resample=Image.Resampling.BOX)

    final_arr = np.array(img, dtype=np.uint8)
    if not np.any(final_arr[:, :, 3] >= 128):
        raise PixelArtError("Image has no opaque pixel after downscaling")

    return img


def fit_size(img: Image.Image, width: int) -> tuple[int, int]:
    """(width, height) of the picture in blocks for a given width, keeping the aspect ratio."""
    width = max(1, width)
    height = max(1, round(width * img.height / img.width))
    return width, height


def image_to_grid(img: Image.Image, width: int) -> tuple[np.ndarray, list[str]]:
    """Resize to `width` blocks wide and map every opaque pixel to the closest block colour."""
    w, h = fit_size(img, width)
    if w == img.width:
        resized = img.convert("RGBA")
    elif w < img.width:
        resized = img.resize((w, h), resample=Image.Resampling.BOX).convert("RGBA")
    else:
        resized = img.resize((w, h), resample=Image.Resampling.NEAREST).convert("RGBA")

    arr = np.array(resized, dtype=np.uint8)
    alpha = arr[:, :, 3]
    opaque_mask = alpha >= 128

    if not np.any(opaque_mask):
        raise PixelArtError("Image has no opaque pixel")

    opaque_rgb = arr[opaque_mask, :3]
    opaque_lab = rgb_to_lab(opaque_rgb)

    palette_blocks, palette_lab = get_palette_data()
    nearest_indices = find_nearest_palette(opaque_lab, palette_lab)

    unique_indices, inverse = np.unique(nearest_indices, return_inverse=True)
    used_palette = ["minecraft:air"] + [palette_blocks[i] for i in unique_indices]

    dtype = np.uint16 if len(used_palette) > 255 else np.uint8
    block_grid_2d = np.zeros((h, w), dtype=dtype)
    block_grid_2d[opaque_mask] = (inverse + 1).astype(dtype)

    grid = np.zeros((w, 1, h), dtype=dtype)
    # x is col, z is h - 1 - row
    grid[:, 0, :] = np.flipud(block_grid_2d).T

    return grid, used_palette


def preview_png(grid: np.ndarray, palette: list[str]) -> bytes:
    """A PNG of the block picture, each block drawn as an 8x8 swatch."""
    if grid.ndim == 3:
        w = grid.shape[0]
        h = grid.shape[2]
        block_indices = grid[:, 0, ::-1].T
    elif grid.ndim == 2:
        w = grid.shape[0]
        h = grid.shape[1]
        block_indices = grid[:, ::-1].T
    else:
        raise ValueError(f"Unexpected grid shape: {grid.shape}")

    palette_rgb_dict = load_palette()
    swatch_palette = np.zeros((len(palette), 4), dtype=np.uint8)
    for i, block_id in enumerate(palette):
        if i == 0 or block_id == "minecraft:air":
            swatch_palette[i] = [0, 0, 0, 0]
        else:
            rgb = palette_rgb_dict.get(block_id, [128, 128, 128])
            swatch_palette[i] = [rgb[0], rgb[1], rgb[2], 255]

    pixels_1x1 = swatch_palette[block_indices]  # shape (h, w, 4)
    swatches = np.repeat(np.repeat(pixels_1x1, 8, axis=0), 8, axis=1)  # shape (8h, 8w, 4)
    img = Image.fromarray(swatches, mode="RGBA")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
