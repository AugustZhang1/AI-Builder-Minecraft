"""mcmcp main service and CLI runner.

Connects chat in Minecraft log to OpenSCAD generation, rendering, voxelization,
and in-game placement via RCON.
"""
from __future__ import annotations

import argparse
from collections.abc import Iterator
from datetime import datetime
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
import time
import urllib.request
from typing import Any

import numpy as np
from PIL import Image

try:
    from . import build, config, llm
    from .rcon import Rcon, RconError
except ImportError:
    from mcmcp import build, config, llm
    from mcmcp.rcon import Rcon, RconError

logger = logging.getLogger("mcmcp")

CHAT_RE = re.compile(r"\]: (?:\[Not Secure\] )?<([A-Za-z0-9_]{3,16})> (.*)$")
URL_RE = re.compile(r"https?://\S+")
FRANK_RE = re.compile(r"\bfrank\b", re.IGNORECASE)


def sanitize_frank(text: str) -> str:
    """Replaces the word 'frank' (case-insensitive) with 'Fr*nk'."""
    return FRANK_RE.sub("Fr*nk", text)


def send_message(rcon: Rcon | None, player: str | None, msg: str) -> None:
    """Sends a gold tellraw message to the player, sanitizing 'frank'."""
    safe_msg = sanitize_frank(msg)
    if not rcon or not player:
        logger.info("[tellraw -> %s] %s", player or "<no-player>", safe_msg)
        return
    payload = json.dumps({"text": "[design] " + safe_msg, "color": "gold"})
    cmd = f"tellraw {player} {payload}"
    try:
        rcon.command(cmd)
    except Exception as e:
        logger.warning("Failed to send tellraw to %s: %s", player, e)


def parse_chat_line(line: str) -> tuple[str, str] | None:
    """Extracts (player, message) from a vanilla 1.21.1 log line, or None."""
    m = CHAT_RE.search(line)
    if not m:
        return None
    return m.group(1), m.group(2)


def parse_command(message: str) -> tuple[bool, str, str | None]:
    """Parses a message for the !design command.

    Returns (is_command, description, image_url).
    If message is not a !design command, returns (False, "", None).
    """
    msg = message.strip()
    if not (msg.lower().startswith("!design") and (len(msg) == 7 or msg[7].isspace())):
        return False, "", None

    rest = msg[7:].strip()
    match = URL_RE.search(rest)
    if match:
        image_url = match.group(0)
        desc = (rest[:match.start()] + " " + rest[match.end():]).strip()
        desc = re.sub(r"\s+", " ", desc).strip()
    else:
        image_url = None
        desc = rest

    return True, desc, image_url


def is_op(player: str, ops_file: Path = config.OPS_FILE) -> bool:
    """Checks ops.json for the player name (case-insensitive)."""
    try:
        if not ops_file.exists():
            return False
        data = json.loads(ops_file.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return False
        target = player.strip().lower()
        for item in data:
            if isinstance(item, dict) and item.get("name", "").strip().lower() == target:
                return True
        return False
    except Exception as e:
        logger.warning("Failed to read ops file %s: %s", ops_file, e)
        return False


def parse_pos_reply(reply: str) -> tuple[float, float, float]:
    """Extracts (x, y, z) position coordinates from a 'data get entity ... Pos' reply."""
    if "no entity was found" in reply.lower():
        raise ValueError("No entity was found")
    m = re.search(
        r"\[\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)[dfDF]?\s*,\s*"
        r"([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)[dfDF]?\s*,\s*"
        r"([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)[dfDF]?\s*\]",
        reply,
    )
    if not m:
        raise ValueError(f"Could not parse Pos from reply: {reply!r}")
    return float(m.group(1)), float(m.group(2)), float(m.group(3))


def parse_rotation_reply(reply: str) -> tuple[float, float]:
    """Extracts (yaw, pitch) from a 'data get entity ... Rotation' reply."""
    if "no entity was found" in reply.lower():
        raise ValueError("No entity was found")
    m = re.search(
        r"\[\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)[dfDF]?\s*,\s*"
        r"([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)[dfDF]?\s*\]",
        reply,
    )
    if not m:
        raise ValueError(f"Could not parse Rotation from reply: {reply!r}")
    return float(m.group(1)), float(m.group(2))


def parse_dimension_reply(reply: str) -> str:
    """Extracts dimension string from a 'data get entity ... Dimension' reply."""
    if "no entity was found" in reply.lower():
        raise ValueError("No entity was found")
    m = re.search(r'"([^"]+)"', reply)
    if not m:
        raise ValueError(f"Could not parse Dimension from reply: {reply!r}")
    return m.group(1)


def room_check_commands(
    dim: str,
    x1: int,
    y1: int,
    z1: int,
    x2: int,
    y2: int,
    z2: int,
) -> list[str]:
    """Generates read-only 'execute if blocks' commands to verify the area is free.

    Compares against an all-air reference region at the top of the world.
    Returns an empty list if the box overlaps the reference region.
    """
    min_x, max_x = min(x1, x2), max(x1, x2)
    min_y, max_y = min(y1, y2), max(y1, y2)
    min_z, max_z = min(z1, z2), max(z1, z2)

    top = 319 if dim in ("minecraft:overworld", "overworld") else 255
    if max_y > top - (max_y - min_y) - 1:
        return []

    layer_area = (max_x - min_x + 1) * (max_z - min_z + 1)
    if layer_area <= 0:
        return []

    max_layers = max(1, 32768 // layer_area)
    commands: list[str] = []
    cur_y = min_y
    while cur_y <= max_y:
        slab_y2 = min(max_y, cur_y + max_layers - 1)
        ry = top - (slab_y2 - cur_y)
        cmd = (
            f"execute in {dim} if blocks {min_x} {cur_y} {min_z} {max_x} {slab_y2} {max_z} "
            f"{min_x} {ry} {min_z} all"
        )
        commands.append(cmd)
        cur_y = slab_y2 + 1

    return commands


def find_free_size(
    rcon: Rcon,
    origin: tuple[int, int, int],
    facing: str,
    dim: str,
) -> int | None:
    """Tests sizes in config.ROOM_SIZES (largest first). Returns first free S or None."""
    for S in config.ROOM_SIZES:
        box = (0, 0, 1, S - 1, S - 1, S - 1, 0)
        x1, y1, z1, x2, y2, z2 = build.box_to_world(box, S, origin, facing, config.GAP)
        cmds = room_check_commands(dim, x1, y1, z1, x2, y2, z2)
        if not cmds:
            continue
        free = True
        for cmd in cmds:
            reply = rcon.command(cmd)
            if not reply.startswith("Test passed"):
                free = False
                break
        if free:
            return S
    return None


def check_grid_room(
    rcon: Rcon,
    origin: tuple[int, int, int],
    facing: str,
    dim: str,
    grid_shape: tuple[int, int, int],
) -> bool:
    """Verifies that the final voxelized grid bounding box is free of blocks."""
    sx, sy, sz = grid_shape
    real_box = (0, 0, 1, sx - 1, sy - 1, sz - 1, 0)
    x1, y1, z1, x2, y2, z2 = build.box_to_world(real_box, sx, origin, facing, config.GAP)
    cmds = room_check_commands(dim, x1, y1, z1, x2, y2, z2)
    if not cmds:
        return False
    for cmd in cmds:
        reply = rcon.command(cmd)
        if not reply.startswith("Test passed"):
            return False
    return True


def load_image_png(source: str) -> bytes:
    """Downloads or reads an image, converts to RGB, thumbnails, and encodes to PNG bytes."""
    path = Path(source)
    if path.is_file():
        data = path.read_bytes()
        if len(data) > config.IMAGE_MAX_BYTES:
            raise ValueError(f"Image exceeds maximum size ({len(data)} > {config.IMAGE_MAX_BYTES})")
    else:
        req = urllib.request.Request(
            source,
            headers={"User-Agent": "mcmcp/1.0 (Minecraft MCP Builder)"},
        )
        with urllib.request.urlopen(req, timeout=20.0) as resp:
            data_arr = bytearray()
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                data_arr.extend(chunk)
                if len(data_arr) > config.IMAGE_MAX_BYTES:
                    raise ValueError(f"Image exceeds maximum size of {config.IMAGE_MAX_BYTES} bytes")
            data = bytes(data_arr)

    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        img.thumbnail((config.IMAGE_MAX_SIDE, config.IMAGE_MAX_SIDE))
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()


def run_build(
    rcon: Rcon,
    player: str,
    description: str,
    image_url: str | None = None,
    scad_override: str | None = None,
    blocks_override: dict[str, str] | None = None,
) -> None:
    """Executes the full in-game design and build pipeline."""
    start_time = time.time()
    try:
        # 1. Player info via RCON
        pos_reply = rcon.command(f"data get entity {player} Pos")
        rot_reply = rcon.command(f"data get entity {player} Rotation")
        dim_reply = rcon.command(f"data get entity {player} Dimension")

        pos = parse_pos_reply(pos_reply)
        rot = parse_rotation_reply(rot_reply)
        dim = parse_dimension_reply(dim_reply)

        origin = (math.floor(pos[0]), math.floor(pos[1]), math.floor(pos[2]))
        facing = build.facing_from_yaw(rot[0])

        # 2. Room check before calling Claude
        S = find_free_size(rcon, origin, facing, dim)
        if S is None:
            send_message(
                rcon,
                player,
                "Not enough room here. Stand on open, flat ground and try again.",
            )
            return

        # 3. Designing announcement and image download
        send_message(rcon, player, f"Designing (up to {S} blocks). This takes a few minutes...")
        image_png: bytes | None = None
        if image_url:
            try:
                image_png = load_image_png(image_url)
            except Exception as e:
                logger.warning("Couldn't load image %s: %s", image_url, e)
                send_message(rcon, player, "Couldn't load that image.")
                return

        # 4. LLM design or override
        if scad_override is not None and blocks_override is not None:
            scad = scad_override
            blocks = blocks_override
            raw = "// OpenSCAD design and blocks provided via flags\n"
        else:
            scad, blocks, raw = llm.design(description, S, image_png)

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        build_dir = config.WORK_DIR / f"{timestamp}-{player}"
        build_dir.mkdir(parents=True, exist_ok=True)

        (build_dir / "request.txt").write_text(
            f"Player: {player}\nSize: {S}\nImage: {image_url or ''}\nDescription: {description}\n",
            encoding="utf-8",
        )
        (build_dir / "response.txt").write_text(raw, encoding="utf-8")
        (build_dir / "design.scad").write_text(scad, encoding="utf-8")
        (build_dir / "blocks.json").write_text(json.dumps(blocks, indent=2), encoding="utf-8")

        # 5. Rendering and voxelizing
        send_message(rcon, player, "Rendering...")
        stls = build.render_parts(scad, list(blocks), build_dir)
        grid, palette = build.voxelize(stls, blocks, S)
        solid_count = int(np.count_nonzero(grid))
        if solid_count > config.MAX_BLOCKS:
            send_message(
                rcon,
                player,
                f"Design has too many blocks ({solid_count:,} > {config.MAX_BLOCKS:,}). Refused.",
            )
            return

        # 6. Re-check room for real size
        if not check_grid_room(rcon, origin, facing, dim, grid.shape):
            send_message(rcon, player, "Something moved into the build area. Cancelled.")
            return

        # 7. Placing blocks
        send_message(rcon, player, f"Placing {solid_count} blocks...")
        sx = grid.shape[0]
        fill_boxes = build.boxes(grid, config.FILL_MAX_VOLUME)
        substitutions: dict[str, str] = {}
        placed_blocks = 0
        announced_milestones: set[int] = set()

        for b in fill_boxes:
            bx1, by1, bz1, bx2, by2, bz2, val = b
            vol = (bx2 - bx1 + 1) * (by2 - by1 + 1) * (bz2 - bz1 + 1)
            orig_block = palette[val]
            block_to_use = substitutions.get(orig_block, orig_block)

            wx1, wy1, wz1, wx2, wy2, wz2 = build.box_to_world(b, sx, origin, facing, config.GAP)
            cmd = f"execute in {dim} run fill {wx1} {wy1} {wz1} {wx2} {wy2} {wz2} {block_to_use}"
            reply = rcon.command(cmd)

            if reply.startswith("Successfully filled") or reply.startswith("No blocks were filled"):
                pass
            else:
                logger.warning(
                    "Block %r rejected by server (%s). Substituting %s",
                    block_to_use,
                    reply.strip(),
                    config.FALLBACK_BLOCK,
                )
                substitutions[orig_block] = config.FALLBACK_BLOCK
                retry_cmd = (
                    f"execute in {dim} run fill {wx1} {wy1} {wz1} {wx2} {wy2} {wz2} "
                    f"{config.FALLBACK_BLOCK}"
                )
                rcon.command(retry_cmd)

            placed_blocks += vol
            pct = int((placed_blocks * 100) / solid_count)
            for m in (25, 50, 75):
                if pct >= m and m not in announced_milestones:
                    announced_milestones.add(m)
                    send_message(rcon, player, f"Placing blocks... {m}%")

            delay = max(0.02, vol / config.BLOCKS_PER_SECOND)
            time.sleep(delay)

        # 8. Finished
        elapsed = int(time.time() - start_time)
        mins = elapsed // 60
        secs = elapsed % 60
        send_message(rcon, player, f"Done: {solid_count} blocks in {mins}m {secs}s.")

    except Exception as e:
        logger.exception("Build failed for player %s: %s", player, e)
        err_str = str(e).strip()
        reason = err_str.splitlines()[0] if err_str else type(e).__name__
        if len(reason) > 100:
            reason = reason[:97] + "..."
        send_message(rcon, player, f"Build failed: {reason}")


def run_offline_build(
    description: str,
    image_url: str | None = None,
    size: int = 32,
    scad_file: str | None = None,
    blocks_file: str | None = None,
) -> None:
    """Executes design, rendering, and voxelization without RCON, printing results and timings."""
    t_total_start = time.time()
    image_png: bytes | None = None
    if image_url:
        image_png = load_image_png(image_url)

    if scad_file and blocks_file:
        scad = Path(scad_file).read_text(encoding="utf-8")
        blocks_data = json.loads(Path(blocks_file).read_text(encoding="utf-8"))
        blocks = blocks_data["blocks"] if isinstance(blocks_data, dict) and "blocks" in blocks_data else blocks_data
        t_design = 0.0
    else:
        t0 = time.time()
        scad, blocks, _raw = llm.design(description, size, image_png)
        t_design = time.time() - t0

    build_dir = config.WORK_DIR / f"offline-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    build_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    stls = build.render_parts(scad, list(blocks), build_dir)
    t_render = time.time() - t0

    t0 = time.time()
    grid, palette = build.voxelize(stls, blocks, size)
    t_vox = time.time() - t0

    t_total = time.time() - t_total_start
    solid_count = int(np.count_nonzero(grid))

    print(f"Block count: {solid_count}")
    print(f"Grid size: {grid.shape}")
    print(f"Palette: {palette}")
    print("Timings:")
    print(f"  Design:   {t_design:.2f}s")
    print(f"  Render:   {t_render:.2f}s")
    print(f"  Voxelize: {t_vox:.2f}s")
    print(f"  Total:    {t_total:.2f}s")


def tail_log(log_path: Path) -> Iterator[str]:
    """Yields complete lines from log_path, tailing from the end initially."""
    while not log_path.exists():
        time.sleep(0.5)

    f = open(log_path, "r", encoding="utf-8", errors="replace")
    f.seek(0, os.SEEK_END)
    prev_size = log_path.stat().st_size
    buf = ""

    while True:
        chunk = f.read(4096)
        if chunk:
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                yield line.rstrip("\r")
        else:
            time.sleep(0.5)
            if not log_path.exists():
                f.close()
                while not log_path.exists():
                    time.sleep(0.5)
                f = open(log_path, "r", encoding="utf-8", errors="replace")
                buf = ""
                prev_size = log_path.stat().st_size
                continue

            try:
                cur_stat = log_path.stat()
                same_file = os.path.samestat(cur_stat, os.fstat(f.fileno()))
            except OSError:
                same_file = False

            if not same_file:
                f.close()
                f = open(log_path, "r", encoding="utf-8", errors="replace")
                buf = ""
                prev_size = cur_stat.st_size
            elif cur_stat.st_size < prev_size:
                f.seek(0)
                buf = ""
                prev_size = cur_stat.st_size
            else:
                prev_size = cur_stat.st_size


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Minecraft MCP v2 builder")
    parser.add_argument("--once", type=str, default=None, help="Run one build and exit")
    parser.add_argument("--player", type=str, default=None, help="Player name for position and messages")
    parser.add_argument("--image", type=str, default=None, help="Image URL or local file path")
    parser.add_argument("--size", type=int, default=32, help="Max size when no --player (default 32)")
    parser.add_argument("--scad", type=str, default=None, help="OpenSCAD file to use (skip Claude)")
    parser.add_argument("--blocks", type=str, default=None, help="Blocks JSON file to use (skip Claude)")

    args = parser.parse_args()

    # Determine if running in one-shot mode or service mode
    is_one_shot = (args.once is not None) or (args.scad is not None) or (args.image is not None and args.player is not None)

    if is_one_shot:
        description = args.once if args.once is not None else ""
        if args.player:
            rcon = Rcon(*config.rcon_settings())
            scad_override = Path(args.scad).read_text(encoding="utf-8") if args.scad else None
            blocks_override = None
            if args.blocks:
                raw_b = json.loads(Path(args.blocks).read_text(encoding="utf-8"))
                blocks_override = raw_b["blocks"] if isinstance(raw_b, dict) and "blocks" in raw_b else raw_b

            run_build(
                rcon,
                args.player,
                description,
                image_url=args.image,
                scad_override=scad_override,
                blocks_override=blocks_override,
            )
        else:
            run_offline_build(
                description,
                image_url=args.image,
                size=args.size,
                scad_file=args.scad,
                blocks_file=args.blocks,
            )
        return

    # Service mode
    rcon = Rcon(*config.rcon_settings())
    build_lock = threading.Lock()

    def handle_chat_line(player: str, message: str) -> None:
        is_cmd, desc, img_url = parse_command(message)
        if not is_cmd:
            return

        logger.info("Command from %s: desc=%r img=%r", player, desc, img_url)

        if not is_op(player, config.OPS_FILE):
            send_message(rcon, player, "Only ops can use !design.")
            return

        if not desc and not img_url:
            send_message(
                rcon,
                player,
                "Usage: !design <description>  or  !design <image link> <description>",
            )
            return

        if not build_lock.acquire(blocking=False):
            send_message(
                rcon,
                player,
                "A build is already running. Try again when it finishes.",
            )
            return

        def worker():
            try:
                run_build(rcon, player, desc, image_url=img_url)
            finally:
                build_lock.release()

        t = threading.Thread(target=worker, daemon=True)
        t.start()

    logger.info("Starting mcmcp service, tailing %s...", config.LOG_FILE)
    try:
        for raw_line in tail_log(config.LOG_FILE):
            parsed = parse_chat_line(raw_line)
            if parsed:
                player_name, chat_msg = parsed
                handle_chat_line(player_name, chat_msg)
    except KeyboardInterrupt:
        logger.info("Service stopped by user.")


if __name__ == "__main__":
    main()
