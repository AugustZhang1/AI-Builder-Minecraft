"""mcmcp main service and CLI runner.

Connects chat in Minecraft log to OpenSCAD generation, rendering, voxelization,
and in-game placement via RCON.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator
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
import zlib
from typing import Any

import numpy as np
from PIL import Image

try:
    from . import animate, build, config, llm, pixelart, preview
    from .rcon import Rcon, RconError
except ImportError:
    from mcmcp import animate, build, config, llm, pixelart, preview
    from mcmcp.rcon import Rcon, RconError

logger = logging.getLogger("mcmcp")

STOP = threading.Event()


class PlacementError(Exception):
    pass


CHAT_RE = re.compile(r"\]: (?:\[Not Secure\] )?<([A-Za-z0-9_]{3,16})> (.*)$")
URL_RE = re.compile(r"https?://\S+")
FRANK_RE = re.compile(r"\bfrank\b", re.IGNORECASE)
NARRATION_RE = re.compile(r"^\s*//\s*>\s*(.+?)\s*$")

PROVIDER_NAMES = {"claude": "Claude", "gemini": "Gemini"}

LAST_DESIGNS: dict[str, tuple[np.ndarray, list[str], list[tuple[int, int, int, int, int, int, str]]]] = {}

HELP_LINES = (
    "Commands (ops only):",
    "!design <description> - the AI designs it and builds it in front of you",
    "!design <image link> <description> - the same, using the picture as a reference",
    "!place - builds your last design again where you stand (no AI)",
    "!pixelart <image link> [width] - builds the picture as a flat wall (no AI)",
    "!designstop - stops the build that is running",
    "!designai [claude|gemini] - shows or switches the AI",
    "!designanim [on|off] - shows or switches the builder crew animation",
    "!designreview [on|off] - shows or switches the AI's check of its own build",
    "!designhelp - shows this list",
)


def load_settings() -> dict[str, Any]:
    """Reads config.SETTINGS_FILE ({"provider": ..., "animate": ...}); missing or invalid -> {}."""
    try:
        data = json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_setting(key: str, value: Any) -> None:
    """Sets one key in config.SETTINGS_FILE, keeping the others."""
    data = load_settings()
    data[key] = value
    config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.SETTINGS_FILE.write_text(json.dumps(data), encoding="utf-8")


def get_provider() -> str:
    """Returns the configured AI provider ('claude' or 'gemini'); unknown or missing -> 'claude'."""
    provider = load_settings().get("provider")
    if isinstance(provider, str) and provider.lower() in PROVIDER_NAMES:
        return provider.lower()
    return "claude"


def set_provider(name: str) -> None:
    """Sets the AI provider in config.SETTINGS_FILE."""
    save_setting("provider", name)


def get_animate() -> bool:
    """Whether !design uses the builder crew animation (set with !designanim; default config.ANIMATE)."""
    value = load_settings().get("animate")
    return value if isinstance(value, bool) else config.ANIMATE


def get_review() -> bool:
    """Whether !design checks a preview of its build once (set with !designreview; default config.REVIEW)."""
    value = load_settings().get("review")
    return value if isinstance(value, bool) else config.REVIEW


def _parse_word_command(message: str, word: str) -> tuple[bool, str | None]:
    """Parses `word [arg]` (case-insensitive). Returns (is_command, arg_or_None)."""
    msg = message.strip()
    n = len(word)
    if not (msg.lower().startswith(word) and (len(msg) == n or msg[n].isspace())):
        return False, None
    arg = msg[n:].strip()
    return True, arg if arg else None


def parse_ai_command(message: str) -> tuple[bool, str | None]:
    """Parses a message for the !designai command. Returns (is_command, arg_or_None)."""
    return _parse_word_command(message, "!designai")


def parse_anim_command(message: str) -> tuple[bool, str | None]:
    """Parses a message for the !designanim command. Returns (is_command, arg_or_None)."""
    return _parse_word_command(message, "!designanim")


def parse_review_command(message: str) -> tuple[bool, str | None]:
    """Parses a message for the !designreview command. Returns (is_command, arg_or_None)."""
    return _parse_word_command(message, "!designreview")


def parse_place_command(message: str) -> bool:
    """Parses a message for the !place command (case-insensitive, exact, trimmed)."""
    return message.strip().lower() == "!place"


class Heartbeat:
    """Daemon thread that periodically sends a heartbeat status message."""

    def __init__(
        self,
        send: Callable[[str], Any],
        name: str,
        interval: float | int = config.HEARTBEAT_SECONDS,
    ) -> None:
        self.send = send
        self.name = name
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_time = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            self.stop()
        self._stop_event.clear()
        self._start_time = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval):
            elapsed = max(0, int(time.time() - self._start_time))
            mins = elapsed // 60
            secs = elapsed % 60
            time_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
            try:
                self.send(f"{self.name} is thinking... ({time_str})")
            except Exception as e:
                logger.warning("Heartbeat send failed: %s", e)

    def stop(self) -> None:
        self._stop_event.set()
        t = self._thread
        if t and t.is_alive():
            if threading.current_thread() != t:
                t.join(timeout=1.0)


class Narrator:
    """Parses streaming code chunks for narration comments (// > text) and sends them."""

    def __init__(
        self,
        send: Callable[[str], Any],
        on_first: Callable[[], Any] | None = None,
    ) -> None:
        self.send = send
        self.on_first = on_first
        self._buf = ""
        self._first = True

    def feed(self, chunk: str) -> None:
        self._buf += chunk
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._process_line(line.rstrip("\r"))

    def flush(self) -> None:
        if self._buf:
            line = self._buf.rstrip("\r")
            self._buf = ""
            self._process_line(line)

    def _process_line(self, line: str) -> None:
        m = NARRATION_RE.match(line)
        if m:
            text = m.group(1)[:100]
            if self._first:
                self._first = False
                if self.on_first:
                    try:
                        self.on_first()
                    except Exception as e:
                        logger.warning("Error in on_first callback: %s", e)
            self.send(text)



def sanitize_frank(text: str) -> str:
    """Replaces the word 'frank' (case-insensitive) with 'Fr*nk'."""
    return FRANK_RE.sub("Fr*nk", text)


def send_message(
    rcon: Rcon | None,
    player: str | None,
    msg: str,
    prefix: str = "[design]",
) -> None:
    """Sends a gold tellraw message to the player, sanitizing 'frank'."""
    safe_msg = sanitize_frank(msg)
    full_text = f"{prefix} {safe_msg}" if prefix else safe_msg
    if not rcon or not player:
        logger.info("[tellraw -> %s] %s", player or "<no-player>", full_text)
        return
    payload = json.dumps({"text": full_text, "color": "gold"})
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
    Walks up from min_y in slabs ensuring no slab overlaps its reference.
    Returns an empty list if any slab cannot be checked (e.g. reaches the top).
    """
    min_x, max_x = min(x1, x2), max(x1, x2)
    min_y, max_y = min(y1, y2), max(y1, y2)
    min_z, max_z = min(z1, z2), max(z1, z2)

    top = 319 if dim in ("minecraft:overworld", "overworld") else 255

    layer_area = (max_x - min_x + 1) * (max_z - min_z + 1)
    if layer_area <= 0:
        return []

    commands: list[str] = []
    cur_y = min_y
    while cur_y <= max_y:
        h = min(32768 // layer_area, (top - cur_y + 1) // 2)
        if h < 1:
            return []
        slab_y2 = min(max_y, cur_y + h - 1)
        h_prime = slab_y2 - cur_y + 1
        ry = top - h_prime + 1
        cmd = (
            f"execute in {dim} if blocks {min_x} {cur_y} {min_z} {max_x} {slab_y2} {max_z} "
            f"{min_x} {ry} {min_z} all"
        )
        commands.append(cmd)
        cur_y = slab_y2 + 1

    return commands


def world_limits(origin_y: int, dim: str) -> tuple[int, int, int]:
    """(wide, deep, tall) build limits from view distance and player feet height."""
    reach = (config.view_distance() - 1) * 16 - config.GAP
    wide = deep = reach
    top = 319 if dim in ("minecraft:overworld", "overworld") else 255
    tall = top - origin_y
    if tall < 1:
        raise ValueError("Too close to the top of the world.")
    return (wide, deep, tall)


def start_room_free(
    rcon: Rcon,
    origin: tuple[int, int, int],
    facing: str,
    dim: str,
) -> bool:
    """Quick free-space check before designing: tests one START_ROOM cube (z from 1)."""
    S = config.START_ROOM
    box = (0, 0, 1, S - 1, S - 1, S - 1, 0)
    x1, y1, z1, x2, y2, z2 = build.box_to_world(box, S, origin, facing, config.GAP)
    cmds = room_check_commands(dim, x1, y1, z1, x2, y2, z2)
    if not cmds:
        return False
    for cmd in cmds:
        reply = rcon.command(cmd)
        if "Test passed" not in reply:
            return False
    return True


def room_boxes(
    grid: np.ndarray,
    dboxes: list[tuple[int, int, int, int, int, int, str]],
) -> list[tuple[int, int, int, int, int, int, Any]]:
    """Grid-space boxes for room check: solid mask with z=0 cleared plus detail boxes (z >= 1)."""
    mask = (grid > 0).astype(np.uint8)
    if mask.shape[2] > 0:
        mask[:, :, 0] = 0
    boxes = build.boxes(mask, 32768)
    for db in dboxes:
        x1, y1, z1, x2, y2, z2, _block = db
        if z2 < 1:
            continue
        cz1 = max(1, z1)
        boxes.append((x1, y1, cz1, x2, y2, z2, 1))
    return boxes


def check_room(
    rcon: Rcon,
    dim: str,
    world_boxes: list[tuple[int, int, int, int, int, int]],
) -> str:
    """Checks if world boxes are free. Returns 'ok', 'blocked', or 'not_loaded'."""
    for box in world_boxes:
        x1, y1, z1, x2, y2, z2 = box
        cmds = room_check_commands(dim, x1, y1, z1, x2, y2, z2)
        if not cmds:
            return "blocked"
        for cmd in cmds:
            reply = rcon.command(cmd)
            if "Test passed" in reply:
                continue
            if "not loaded" in reply.lower():
                return "not_loaded"
            return "blocked"
    return "ok"


def check_grid_room(
    rcon: Rcon,
    origin: tuple[int, int, int],
    facing: str,
    dim: str,
    grid_shape: tuple[int, int, int],
    grow: int = 0,
) -> bool:
    """Verifies that the final voxelized grid bounding box, grown by `grow` blocks on each
    side (not up or down), is free of blocks."""
    sx, sy, sz = grid_shape
    real_box = (-grow, -grow, 1, sx - 1 + grow, sy - 1 + grow, sz - 1, 0)
    x1, y1, z1, x2, y2, z2 = build.box_to_world(real_box, sx, origin, facing, config.GAP)
    cmds = room_check_commands(dim, x1, y1, z1, x2, y2, z2)
    if not cmds:
        return False
    for cmd in cmds:
        reply = rcon.command(cmd)
        if "Test passed" not in reply:
            return False
    return True


def download_image(source: str) -> bytes:
    """Downloads or reads an image into raw bytes (URL or local path)."""
    path = Path(source)
    if path.is_file():
        data = path.read_bytes()
        if len(data) > config.IMAGE_MAX_BYTES:
            raise ValueError(f"Image exceeds maximum size ({len(data)} > {config.IMAGE_MAX_BYTES})")
        return data

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
        return bytes(data_arr)


def load_image_png(source: str) -> bytes:
    """Downloads or reads an image, converts to RGB, thumbnails, and encodes to PNG bytes."""
    data = download_image(source)
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        img.thumbnail((config.IMAGE_MAX_SIDE, config.IMAGE_MAX_SIDE))
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()


def estimate_seconds(fill_boxes: list[build.Box]) -> float:
    """Sum of per-command delays for placement boxes."""
    total = 0.0
    for b in fill_boxes:
        bx1, by1, bz1, bx2, by2, bz2, _ = b
        vol = (bx2 - bx1 + 1) * (by2 - by1 + 1) * (bz2 - bz1 + 1)
        total += max(config.MIN_COMMAND_DELAY, vol / config.BLOCKS_PER_SECOND)
    return total


def format_estimate(val: float | list[build.Box]) -> str:
    """Format seconds as 'about N minutes' (>= 90 s) or 'about N seconds'."""
    if isinstance(val, list):
        seconds = estimate_seconds(val)
    else:
        seconds = float(val)
    if seconds >= 90:
        mins = round(seconds / 60)
        return f"about {mins} minutes"
    s = round(seconds)
    return f"about {s} seconds"


class Pacer:
    """Rate limit for placement commands. Waits add up and are slept once they reach one
    game tick, since short sleeps round up to about 15 ms on Windows."""

    def __init__(self) -> None:
        self.owed = 0.0

    def wait(self, vol: int) -> bool:
        """Adds the delay for a command of vol blocks. Returns True if STOP was set."""
        self.owed += max(config.MIN_COMMAND_DELAY, vol / config.BLOCKS_PER_SECOND)
        if self.owed < 0.05:
            return STOP.is_set()
        owed, self.owed = self.owed, 0.0
        return STOP.wait(owed)


def block_rejected(reply: str) -> bool:
    """True if the server refused a command's block (a parse error: unknown block or state).
    Replies are checked by content, not by how they start: all RCON connections share one
    reply buffer, so another client's output (e.g. Frank's) can be mixed into ours."""
    return "<--[HERE]" in reply


def place_grid(
    rcon: Rcon | None,
    player: str | None,
    grid: np.ndarray,
    palette: list[str],
    origin: tuple[int, int, int],
    facing: str,
    dim: str,
    send: Callable[[str], Any],
    driver: Callable[[list[build.Box], Callable[[build.Box], int]], Any] | None = None,
) -> int:
    """Places a voxelized grid into the world via RCON /fill commands.
    With a driver (the build animation), driver(boxes, place_one) decides the order and the
    pace; place_one(box) places one box and returns its block count."""
    solid_count = int(np.count_nonzero(grid))
    if solid_count == 0:
        return 0

    sx = grid.shape[0]
    fill_boxes = build.boxes(grid, config.FILL_MAX_VOLUME)
    substitutions: dict[str, str] = {
        b: config.FALLBACK_BLOCK
        for b in palette[1:]
        if b in config.BANNED_BLOCKS or b.endswith("_concrete_powder")
    }
    if substitutions:
        logger.warning("Banned blocks replaced with %s: %s", config.FALLBACK_BLOCK, sorted(substitutions))
    placed_blocks = 0
    announced_milestones: set[int] = set()
    pacer = Pacer()

    def place_one(b: build.Box) -> int:
        nonlocal placed_blocks
        bx1, by1, bz1, bx2, by2, bz2, val = b
        vol = (bx2 - bx1 + 1) * (by2 - by1 + 1) * (bz2 - bz1 + 1)
        orig_block = palette[val]
        block_to_use = substitutions.get(orig_block, orig_block)
        block_to_use = build.with_persistent_leaves(block_to_use)

        wx1, wy1, wz1, wx2, wy2, wz2 = build.box_to_world(b, sx, origin, facing, config.GAP)
        cmd = f"execute in {dim} run fill {wx1} {wy1} {wz1} {wx2} {wy2} {wz2} {block_to_use}"
        reply = rcon.command(cmd) if rcon else "Successfully filled"

        if "not loaded" in reply.lower():
            raise PlacementError(
                f"Stopped after {placed_blocks:,} blocks: the build area is no longer loaded. Stay close to it."
            )
        if block_rejected(reply):
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
            if rcon:
                rcon.command(retry_cmd)

        placed_blocks += vol
        pct = int((placed_blocks * 100) / solid_count)
        for m in (25, 50, 75):
            if pct >= m and m not in announced_milestones:
                announced_milestones.add(m)
                send(f"Placing blocks... {m}%")
        return vol

    if driver is not None:
        driver(fill_boxes, place_one)
        return placed_blocks

    for b in fill_boxes:
        if STOP.is_set():
            break
        vol = place_one(b)
        if pacer.wait(vol):
            break

    return placed_blocks


def place_details(
    rcon: Rcon | None,
    dboxes: list[tuple[int, int, int, int, int, int, str]],
    size_x: int,
    origin: tuple[int, int, int],
    facing: str,
    dim: str,
) -> int:
    """Places detail boxes in list order via RCON fill commands with rotated states."""
    placed = 0
    pacer = Pacer()
    for db in dboxes:
        if STOP.is_set():
            break
        gx1, gy1, gz1, gx2, gy2, gz2, block = db
        vol = (abs(gx2 - gx1) + 1) * (abs(gy2 - gy1) + 1) * (abs(gz2 - gz1) + 1)
        rotated_block = build.rotate_state(block, facing)
        wx1, wy1, wz1, wx2, wy2, wz2 = build.box_to_world(
            (gx1, gy1, gz1, gx2, gy2, gz2, 0), size_x, origin, facing, config.GAP
        )
        cmd = f"execute in {dim} run fill {wx1} {wy1} {wz1} {wx2} {wy2} {wz2} {rotated_block}"
        reply = rcon.command(cmd) if rcon else "Successfully filled"

        if "not loaded" in reply.lower():
            raise PlacementError(
                f"Stopped after {placed:,} blocks: the build area is no longer loaded. Stay close to it."
            )
        if block_rejected(reply):
            logger.warning(
                "Detail block %r rejected by server (%s). Skipped.",
                rotated_block,
                reply.strip(),
            )
        else:
            placed += vol

        if pacer.wait(vol):
            break

    return placed


def load_blocks_data(
    data: Any,
) -> tuple[dict[str, str], dict[str, dict[str, float]], list[tuple[int, int, int, int, int, int, str]]]:
    """Load (blocks, mix, details) from loaded JSON data."""
    if isinstance(data, dict) and "blocks" in data:
        blocks = data["blocks"]
        mix = llm.parse_mix(data.get("mix", {}))
        details = llm.parse_details(data.get("details", []))
        return blocks, mix, details
    elif isinstance(data, dict):
        return data, {}, []
    return {}, {}, []


def detail_block_count(dboxes: list[tuple[int, int, int, int, int, int, str]]) -> int:
    return sum((x2 - x1 + 1) * (y2 - y1 + 1) * (z2 - z1 + 1) for x1, y1, z1, x2, y2, z2, _ in dboxes)


def voxelize_design(
    scad: str,
    stls: dict[str, Path],
    blocks: dict[str, str],
    mix: dict[str, dict[str, float]],
    details: list[tuple[int, int, int, int, int, int, str]],
    limits: tuple[int, int, int],
    build_dir: Path,
) -> tuple[np.ndarray, list[str], list[tuple[int, int, int, int, int, int, str]], bool]:
    """Voxelize, apply texture mixes, map details and save the previews.
    Returns (grid, palette, dboxes, details_skipped); details are skipped if the design was scaled down."""
    grid, palette, offset, scale = build.voxelize(stls, blocks, limits)
    if mix:
        grid, palette = build.apply_mix(grid, palette, mix, zlib.crc32(scad.encode("utf-8")))
    skipped = scale < 1.0 and bool(details)
    dboxes = [] if skipped else build.detail_boxes(details, offset, grid.shape)

    # Previews never fail a build.
    for name, make in (
        ("preview.png", lambda: (build_dir / "preview.png").write_bytes(
            preview.blocks_png(*build.stamp_details(grid, palette, dboxes)))),
        ("mesh.png", lambda: (build_dir / "mesh.png").write_bytes(preview.mesh_png(stls, blocks))),
        ("mesh.glb", lambda: preview.export_glb(stls, blocks, build_dir / "mesh.glb")),
    ):
        try:
            make()
        except Exception as e:
            logger.warning("Failed to write %s: %s", name, e)
    return grid, palette, dboxes, skipped


def review_design(
    description: str,
    limits: tuple[int, int, int],
    image_png: bytes | None,
    provider: str,
    raw: str,
    build_dir: Path,
    send_fn: Callable[[str], Any],
    provider_name: str,
) -> tuple[str, dict[str, str], dict[str, dict[str, float]], list[tuple[int, int, int, int, int, int, str]], np.ndarray, list[str], list[tuple[int, int, int, int, int, int, str]], bool] | None:
    """Asks the AI to check a preview of its build once; returns revision data or None to keep."""
    try:
        send_fn("Checking the design...")
        preview_png = (build_dir / "preview.png").read_bytes()
        heartbeat = Heartbeat(send_fn, provider_name, config.HEARTBEAT_SECONDS)
        heartbeat.start()
        try:
            rev = llm.review(
                description,
                limits,
                raw,
                preview_png,
                image_png=image_png,
                provider=provider,
            )
        finally:
            heartbeat.stop()

        if rev is None:
            send_fn("Review: kept the design.")
            return None

        rev_scad, rev_blocks, rev_mix, rev_details, rev_raw = rev
        review_dir = build_dir / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / "response.txt").write_text(rev_raw, encoding="utf-8")
        (review_dir / "design.scad").write_text(rev_scad, encoding="utf-8")
        (review_dir / "blocks.json").write_text(
            json.dumps({"blocks": rev_blocks, "mix": rev_mix, "details": rev_details}, indent=2),
            encoding="utf-8",
        )
        stls = build.render_parts(rev_scad, list(rev_blocks), review_dir)
        grid, palette, dboxes, skipped = voxelize_design(
            rev_scad, stls, rev_blocks, rev_mix, rev_details, limits, review_dir
        )
        send_fn("Review: improved the design.")
        return rev_scad, rev_blocks, rev_mix, rev_details, grid, palette, dboxes, skipped
    except Exception as e:
        logger.warning("Review failed: %s", e)
        send_fn("Review failed; building the first design.")
        return None


def place_design(
    rcon: Rcon,
    player: str,
    design: tuple[np.ndarray, list[str], list[tuple[int, int, int, int, int, int, str]]],
    origin: tuple[int, int, int],
    facing: str,
    dim: str,
    start_time: float,
    crew: animate.Crew | None = None,
) -> None:
    """Room check, placing and completion messages shared by run_build and run_place.
    With a crew (the !design animation), scaffolding goes up first if the ring around the build
    is free too, the crew places the build tile by tile, and the scaffolding comes down again
    before the detail blocks."""
    grid, palette, dboxes = design

    # 1. Room check on the real design
    rboxes = room_boxes(grid, dboxes)
    world_boxes = [build.box_to_world(b, grid.shape[0], origin, facing, config.GAP) for b in rboxes]
    status = check_room(rcon, dim, world_boxes)
    if status == "not_loaded":
        send_message(
            rcon,
            player,
            "Part of the build is too far away to load. Stand nearer the middle of the area, or raise your render distance, then type !place.",
        )
        return
    elif status == "blocked":
        X, Y, Z = grid.shape
        send_message(
            rcon,
            player,
            f"The design ({X}x{Y}x{Z}) is blocked here. Move to open ground and type !place.",
        )
        return

    # 2. Placing blocks announcement
    main_count = int(np.count_nonzero(grid))
    total_blocks = main_count + detail_block_count(dboxes)
    send_message(rcon, player, f"Placing {total_blocks:,} blocks...")

    # 3. Placing main grid
    driver = None
    scaffold: animate.Scaffold | None = None
    if crew is not None:
        if check_grid_room(rcon, origin, facing, dim, grid.shape, grow=1):
            scaffold = animate.Scaffold(rcon, dim, origin, facing, grid.shape)
            scaffold.raise_(STOP.wait)
        driver = lambda boxes, place_one: crew.build(grid.shape, boxes, place_one, STOP.wait)
    try:
        placed_main = place_grid(
            rcon,
            player,
            grid,
            palette,
            origin,
            facing,
            dim,
            lambda msg: send_message(rcon, player, msg),
            driver=driver,
        )
    finally:
        if scaffold is not None:
            scaffold.lower()

    # 4. Placing details
    placed_details = 0
    if not STOP.is_set() and dboxes:
        placed_details = place_details(
            rcon,
            dboxes,
            grid.shape[0],
            origin,
            facing,
            dim,
        )

    placed_total = placed_main + placed_details

    # 5. Finished
    if STOP.is_set():
        send_message(rcon, player, f"Stopped after {placed_total} blocks.")
    else:
        elapsed = int(time.time() - start_time)
        mins = elapsed // 60
        secs = elapsed % 60
        send_message(rcon, player, f"Done: {total_blocks} blocks in {mins}m {secs}s.")


def run_place(rcon: Rcon, player: str) -> None:
    """Places the player's last voxelized design at their current position and facing."""
    STOP.clear()
    start_time = time.time()
    try:
        saved = LAST_DESIGNS.get(player.strip().lower())
        if saved is None:
            send_message(rcon, player, "No saved design. Use !design first.")
            return

        pos_reply = rcon.command(f"data get entity {player} Pos")
        rot_reply = rcon.command(f"data get entity {player} Rotation")
        dim_reply = rcon.command(f"data get entity {player} Dimension")

        pos = parse_pos_reply(pos_reply)
        rot = parse_rotation_reply(rot_reply)
        dim = parse_dimension_reply(dim_reply)

        origin = (math.floor(pos[0]), math.floor(pos[1]), math.floor(pos[2]))
        facing = build.facing_from_yaw(rot[0])

        try:
            limits = world_limits(origin[1], dim)
        except ValueError as e:
            send_message(rcon, player, str(e))
            return

        w, d, t = limits
        grid, palette, dboxes = saved
        X, Y, Z = grid.shape
        if X > w or Y > d or Z > t:
            send_message(
                rcon,
                player,
                f"The design ({X}x{Y}x{Z}) does not fit here (limits {w}x{d}x{t}).",
            )
            return

        place_design(rcon, player, saved, origin, facing, dim, start_time)

    except Exception as e:
        logger.exception("Place failed for player %s: %s", player, e)
        err_str = str(e).strip()
        reason = err_str.splitlines()[0] if err_str else type(e).__name__
        if len(reason) > 100:
            reason = reason[:97] + "..."
        send_message(rcon, player, f"Build failed: {reason}")


def run_build(
    rcon: Rcon,
    player: str,
    description: str,
    image_url: str | None = None,
    scad_override: str | None = None,
    blocks_override: Any = None,
    ai: str | None = None,
) -> None:
    """Executes the full in-game design and build pipeline."""
    STOP.clear()
    start_time = time.time()
    crew: animate.Crew | None = None
    survey: animate.Survey | None = None
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

        # 2. World limits
        try:
            limits = world_limits(origin[1], dim)
        except ValueError as e:
            send_message(rcon, player, str(e))
            return
        w, d, t = limits

        # 3. Room check before calling Claude
        if not start_room_free(rcon, origin, facing, dim):
            send_message(
                rcon,
                player,
                "Not enough room here. Stand on open, flat ground and try again.",
            )
            return

        # Animation: builders stand at the start plot and its corners are marked while the AI designs
        if get_animate():
            rcon.command(animate.kill_crew_command())
            crew = animate.Crew(rcon, dim, player, origin, facing, config.CREW_SIZE)
            crew.summon(config.START_ROOM)
            survey = animate.Survey(rcon, animate.corner_particle_commands(dim, origin, facing, config.START_ROOM))
            survey.start()

        # 4. Designing announcement and image download
        provider = (ai.lower() if ai else None) or get_provider()
        provider_name = PROVIDER_NAMES.get(provider, provider.capitalize())
        send_message(
            rcon,
            player,
            f"Designing with {provider_name} (up to {w}x{d}x{t}). This takes a few minutes...",
        )
        image_png: bytes | None = None
        if image_url:
            try:
                image_png = load_image_png(image_url)
            except Exception as e:
                logger.warning("Couldn't load image %s: %s", image_url, e)
                send_message(rcon, player, "Couldn't load that image.")
                return

        # 5. LLM design or override
        if scad_override is not None and blocks_override is not None:
            scad = scad_override
            blocks, mix, details = load_blocks_data(blocks_override)
            raw = "// OpenSCAD design and blocks provided via flags\n"
        else:
            send_fn = lambda msg: send_message(rcon, player, msg)
            heartbeat = Heartbeat(send_fn, provider_name, config.HEARTBEAT_SECONDS)
            narrator = Narrator(send_fn, on_first=heartbeat.stop)
            heartbeat.start()
            try:
                scad, blocks, mix, details, raw = llm.design(
                    description,
                    limits,
                    image_png=image_png,
                    on_text=narrator.feed,
                    provider=provider,
                )
            finally:
                heartbeat.stop()
            narrator.flush()
        if survey is not None:
            survey.stop()

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        build_dir = config.WORK_DIR / f"{timestamp}-{player}"
        build_dir.mkdir(parents=True, exist_ok=True)

        (build_dir / "request.txt").write_text(
            f"Player: {player}\nProvider: {provider}\nLimits: {w}x{d}x{t}\nImage: {image_url or ''}\nDescription: {description}\n",
            encoding="utf-8",
        )
        (build_dir / "response.txt").write_text(raw, encoding="utf-8")
        (build_dir / "design.scad").write_text(scad, encoding="utf-8")
        (build_dir / "blocks.json").write_text(
            json.dumps({"blocks": blocks, "mix": mix, "details": details}, indent=2),
            encoding="utf-8",
        )

        # 6. Rendering and voxelizing once with world limits
        send_message(rcon, player, "Rendering...")
        stls = build.render_parts(scad, list(blocks), build_dir)
        grid, palette, dboxes, skipped = voxelize_design(scad, stls, blocks, mix, details, limits, build_dir)

        if get_review() and (scad_override is None or blocks_override is None) and not STOP.is_set():
            rev = review_design(
                description,
                limits,
                image_png,
                provider,
                raw,
                build_dir,
                lambda msg: send_message(rcon, player, msg),
                provider_name,
            )
            if rev is not None:
                scad, blocks, mix, details, grid, palette, dboxes, skipped = rev

        if skipped:
            send_message(rcon, player, "Scaled the design down to fit; skipped the detail blocks.")

        # 7. Keep it for !place, then check the room and place
        design_tuple = (grid, palette, dboxes)
        LAST_DESIGNS[player.strip().lower()] = design_tuple

        place_design(rcon, player, design_tuple, origin, facing, dim, start_time, crew=crew)

    except Exception as e:
        logger.exception("Build failed for player %s: %s", player, e)
        err_str = str(e).strip()
        reason = err_str.splitlines()[0] if err_str else type(e).__name__
        if len(reason) > 100:
            reason = reason[:97] + "..."
        send_message(rcon, player, f"Build failed: {reason}")
    finally:
        # Errors, refusals and !designstop all end here, so nothing is left behind.
        if survey is not None:
            survey.stop()
        if crew is not None:
            crew.remove()


def run_offline_build(
    description: str,
    image_url: str | None = None,
    size: int | None = None,
    scad_file: str | None = None,
    blocks_file: str | None = None,
    ai: str | None = None,
) -> None:
    """Executes design, rendering, voxelization, and previews without RCON, printing results and timings."""
    t_total_start = time.time()
    reach = (config.view_distance() - 1) * 16 - config.GAP
    if size is None:
        size = reach
    limits = (size, size, size)

    image_png: bytes | None = None
    if image_url:
        image_png = load_image_png(image_url)

    provider = ai.lower() if ai else "gemini"
    if scad_file and blocks_file:
        scad = Path(scad_file).read_text(encoding="utf-8")
        blocks_data = json.loads(Path(blocks_file).read_text(encoding="utf-8"))
        blocks, mix, details = load_blocks_data(blocks_data)
        raw = ""
        t_design = 0.0
    else:
        t0 = time.time()
        scad, blocks, mix, details, raw = llm.design(
            description,
            limits,
            image_png=image_png,
            provider=provider,
        )
        t_design = time.time() - t0

    build_dir = config.WORK_DIR / f"offline-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "blocks.json").write_text(
        json.dumps({"blocks": blocks, "mix": mix, "details": details}, indent=2),
        encoding="utf-8",
    )
    if raw:
        (build_dir / "response.txt").write_text(raw, encoding="utf-8")

    t0 = time.time()
    stls = build.render_parts(scad, list(blocks), build_dir)
    t_render = time.time() - t0

    t0 = time.time()
    grid, palette, dboxes, skipped = voxelize_design(scad, stls, blocks, mix, details, limits, build_dir)
    t_vox = time.time() - t0

    t_review = 0.0
    reviewed = False
    if get_review() and not (scad_file and blocks_file):
        provider_name = PROVIDER_NAMES.get(provider, provider.capitalize())
        t0 = time.time()
        rev = review_design(
            description,
            limits,
            image_png,
            provider,
            raw,
            build_dir,
            print,
            provider_name,
        )
        t_review = time.time() - t0
        if rev is not None:
            reviewed = True
            scad, blocks, mix, details, grid, palette, dboxes, skipped = rev

    t_total = time.time() - t_total_start
    total_blocks = int(np.count_nonzero(grid)) + detail_block_count(dboxes)

    print(f"Block count: {total_blocks}")
    print(f"Detail boxes: {len(dboxes)}" + (" (skipped: design was scaled down)" if skipped else ""))
    print(f"Grid size: {grid.shape}")
    print(f"Previews: preview.png, mesh.png, mesh.glb")
    print(f"Folder: {build_dir}")
    if reviewed:
        print(f"Review folder: {build_dir / 'review'}")
    print("Timings:")
    print(f"  Design:   {t_design:.2f}s")
    print(f"  Render:   {t_render:.2f}s")
    print(f"  Voxelize + previews: {t_vox:.2f}s")
    if get_review() and not (scad_file and blocks_file):
        print(f"  Review:   {t_review:.2f}s")
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


def parse_stop_command(message: str) -> bool:
    """Parses a message for the !designstop command (case-insensitive, exact word)."""
    return message.strip().lower() == "!designstop"


def parse_help_command(message: str) -> bool:
    """Parses a message for the !designhelp command (case-insensitive, exact word)."""
    return message.strip().lower() == "!designhelp"


def parse_pixelart_command(message: str) -> tuple[bool, str | None, int | None]:
    """Parses a message for the !pixelart command.

    Returns (is_command, image_url_or_None, width_or_None).
    If it is a !pixelart command but has invalid arguments, returns (True, None, None).
    """
    msg = message.strip()
    if not (msg.lower().startswith("!pixelart") and (len(msg) == 9 or msg[9].isspace())):
        return False, None, None

    rest = msg[9:].strip()
    match = URL_RE.search(rest)
    if not match:
        return True, None, None

    url = match.group(0)
    tokens = (rest[:match.start()] + " " + rest[match.end():]).split()
    if not tokens:
        return True, url, None
    if len(tokens) == 1:
        try:
            val = int(tokens[0])
            if val > 0:
                return True, url, val
            return True, None, None
        except ValueError:
            return True, None, None

    return True, None, None


def run_pixelart(
    rcon: Rcon,
    player: str,
    source: str,
    width: int | None = None,
) -> None:
    """Executes the pixel art placement pipeline."""
    STOP.clear()
    start_time = time.time()
    try:
        # a. Player Pos/Rotation/Dimension -> origin, facing, top
        pos_reply = rcon.command(f"data get entity {player} Pos")
        rot_reply = rcon.command(f"data get entity {player} Rotation")
        dim_reply = rcon.command(f"data get entity {player} Dimension")

        pos = parse_pos_reply(pos_reply)
        rot = parse_rotation_reply(rot_reply)
        dim = parse_dimension_reply(dim_reply)

        origin = (math.floor(pos[0]), math.floor(pos[1]), math.floor(pos[2]))
        facing = build.facing_from_yaw(rot[0])
        top = 319 if dim in ("minecraft:overworld", "overworld") else 255

        # b. Image download & native_image
        try:
            raw_bytes = download_image(source)
            img = pixelart.native_image(raw_bytes, config.PIXELART_MAX_SIDE)
        except pixelart.PixelArtError as e:
            send_message(rcon, player, str(e), prefix="[pixelart]")
            return
        except Exception as e:
            logger.warning("Couldn't load image %s: %s", source, e)
            send_message(rcon, player, "Couldn't load that image.", prefix="[pixelart]")
            return

        # c. Size selection
        chosen_w: int | None = None
        final_h: int = 0
        if width is not None:
            w, h = pixelart.fit_size(img, width)
            if origin[1] + h - 1 > top - 1 or not check_grid_room(rcon, origin, facing, dim, (w, 1, h)):
                send_message(
                    rcon,
                    player,
                    f"Not enough room for a {w}x{h} picture here. Face open space and try again.",
                    prefix="[pixelart]",
                )
                return
            chosen_w = w
            final_h = h
        else:
            candidate_w = img.width
            last_tried: tuple[int, int] | None = None
            while candidate_w >= config.PIXELART_MIN_WIDTH:
                w, h = pixelart.fit_size(img, candidate_w)
                if origin[1] + h - 1 > top - 1:
                    max_h = top - origin[1]
                    if max_h < 1:
                        last_tried = (w, h)
                        break
                    target_w = max(1, int(candidate_w * max_h / h))
                    w, h = pixelart.fit_size(img, target_w)
                    while target_w > 1 and origin[1] + h - 1 > top - 1:
                        target_w -= 1
                        w, h = pixelart.fit_size(img, target_w)
                    candidate_w = target_w
                    if candidate_w < config.PIXELART_MIN_WIDTH:
                        last_tried = (w, h)
                        break

                last_tried = (w, h)
                if check_grid_room(rcon, origin, facing, dim, (w, 1, h)):
                    chosen_w = w
                    final_h = h
                    break
                candidate_w = int(candidate_w * 0.8)

            if chosen_w is None:
                last_w, last_h = last_tried if last_tried is not None else pixelart.fit_size(img, config.PIXELART_MIN_WIDTH)
                send_message(
                    rcon,
                    player,
                    f"Not enough room for a {last_w}x{last_h} picture here. Face open space and try again.",
                    prefix="[pixelart]",
                )
                return

        # d. Grid and preview artifacts
        grid, palette = pixelart.image_to_grid(img, chosen_w)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        build_dir = config.WORK_DIR / f"{timestamp}-{player}-pixelart"
        build_dir.mkdir(parents=True, exist_ok=True)

        ext = Path(source.split("?")[0]).suffix
        if not ext or len(ext) > 5:
            ext = ".png"
        (build_dir / f"source{ext}").write_bytes(raw_bytes)
        (build_dir / "preview.png").write_bytes(pixelart.preview_png(grid, palette))

        # e. Announce and place
        blocks = int(np.count_nonzero(grid))
        colours = max(0, len(palette) - 1)
        fill_boxes = build.boxes(grid, config.FILL_MAX_VOLUME)
        eta = format_estimate(estimate_seconds(fill_boxes))

        send_message(
            rcon,
            player,
            f"Pixel art: {chosen_w}x{final_h}, {blocks:,} blocks, {colours} colours. Placing ({eta})...",
            prefix="[pixelart]",
        )

        placed = place_grid(
            rcon,
            player,
            grid,
            palette,
            origin,
            facing,
            dim,
            lambda msg: send_message(rcon, player, msg, prefix="[pixelart]"),
        )

        if STOP.is_set():
            send_message(
                rcon,
                player,
                f"Stopped after {placed} blocks.",
                prefix="[pixelart]",
            )
        else:
            elapsed = int(time.time() - start_time)
            mins = elapsed // 60
            secs = elapsed % 60
            send_message(
                rcon,
                player,
                f"Done: {placed} blocks in {mins}m {secs}s.",
                prefix="[pixelart]",
            )

    except Exception as e:
        logger.exception("Pixel art failed for player %s: %s", player, e)
        err_str = str(e).strip()
        reason = err_str.splitlines()[0] if err_str else type(e).__name__
        if len(reason) > 100:
            reason = reason[:97] + "..."
        send_message(rcon, player, f"Pixel art failed: {reason}", prefix="[pixelart]")


def run_offline_pixelart(source: str, width: int | None = None) -> None:
    """Processes pixel art offline without RCON."""
    raw_bytes = download_image(source)
    img = pixelart.native_image(raw_bytes, config.PIXELART_MAX_SIDE)

    w_target = width if width is not None else img.width
    w, h = pixelart.fit_size(img, w_target)
    grid, palette = pixelart.image_to_grid(img, w)

    blocks = int(np.count_nonzero(grid))
    colours = max(0, len(palette) - 1)
    fill_boxes = build.boxes(grid, config.FILL_MAX_VOLUME)
    eta = format_estimate(estimate_seconds(fill_boxes))

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    work_folder = config.WORK_DIR / f"{timestamp}-pixelart"
    work_folder.mkdir(parents=True, exist_ok=True)
    preview_path = work_folder / "preview.png"
    preview_path.write_bytes(pixelart.preview_png(grid, palette))

    print(f"Native size: {img.width}x{img.height}")
    print(f"Final size: {w}x{h}")
    print(f"Block count: {blocks}")
    print(f"Colour count: {colours}")
    print(f"Fill boxes: {len(fill_boxes)}")
    print(f"ETA: {eta}")
    print(f"Preview: {preview_path}")


def handle_chat_line(
    player: str,
    message: str,
    rcon: Rcon | None = None,
    build_lock: threading.Lock | None = None,
) -> None:
    if parse_help_command(message):
        # Anyone may ask; it only lists the commands (which stay op-only).
        for line in HELP_LINES:
            send_message(rcon, player, line)
        return

    is_anim_cmd, anim_arg = parse_anim_command(message)
    if is_anim_cmd:
        if not is_op(player, config.OPS_FILE):
            send_message(rcon, player, "Only ops can use !designanim.")
            return
        if anim_arg is None:
            send_message(rcon, player, f"Builder animation: {'on' if get_animate() else 'off'}.")
        elif anim_arg.lower() in ("on", "off"):
            save_setting("animate", anim_arg.lower() == "on")
            send_message(rcon, player, f"Builder animation turned {anim_arg.lower()}.")
        else:
            send_message(rcon, player, "Usage: !designanim on|off")
        return

    is_review_cmd, review_arg = parse_review_command(message)
    if is_review_cmd:
        if not is_op(player, config.OPS_FILE):
            send_message(rcon, player, "Only ops can use !designreview.")
            return
        if review_arg is None:
            send_message(rcon, player, f"Design review: {'on' if get_review() else 'off'}.")
        elif review_arg.lower() in ("on", "off"):
            save_setting("review", review_arg.lower() == "on")
            send_message(rcon, player, f"Design review turned {review_arg.lower()}.")
        else:
            send_message(rcon, player, "Usage: !designreview on|off")
        return

    if parse_stop_command(message):
        if not is_op(player, config.OPS_FILE):
            send_message(rcon, player, "Only ops can use !designstop.")
            return

        if build_lock is not None and build_lock.locked():
            STOP.set()
            send_message(rcon, player, "Stopping...")
        else:
            send_message(rcon, player, "Nothing is running.")
        return

    is_ai_cmd, ai_arg = parse_ai_command(message)
    if is_ai_cmd:
        if not is_op(player, config.OPS_FILE):
            send_message(rcon, player, "Only ops can use !designai.")
            return

        if ai_arg is None:
            cur = get_provider()
            display = PROVIDER_NAMES.get(cur, "Claude")
            send_message(rcon, player, f"AI: {display}")
            return

        choice = ai_arg.lower()
        if choice in PROVIDER_NAMES:
            set_provider(choice)
            send_message(rcon, player, f"AI set to {PROVIDER_NAMES[choice]}.")
        else:
            send_message(rcon, player, "Usage: !designai claude|gemini")
        return

    is_px_cmd, px_url, px_width = parse_pixelart_command(message)
    if is_px_cmd:
        if not is_op(player, config.OPS_FILE):
            send_message(rcon, player, "Only ops can use !pixelart.", prefix="[pixelart]")
            return

        if not px_url:
            send_message(rcon, player, "Usage: !pixelart <image link> [width]", prefix="[pixelart]")
            return

        if build_lock is not None and not build_lock.acquire(blocking=False):
            send_message(
                rcon,
                player,
                "A build is already running. Try again when it finishes.",
                prefix="[pixelart]",
            )
            return

        def px_worker():
            try:
                run_pixelart(rcon, player, px_url, width=px_width)
            finally:
                if build_lock is not None:
                    build_lock.release()

        t = threading.Thread(target=px_worker, daemon=True)
        t.start()
        return

    if parse_place_command(message):
        if not is_op(player, config.OPS_FILE):
            send_message(rcon, player, "Only ops can use !place.")
            return

        saved = LAST_DESIGNS.get(player.strip().lower())
        if saved is None:
            send_message(rcon, player, "No saved design. Use !design first.")
            return

        if build_lock is not None and not build_lock.acquire(blocking=False):
            send_message(
                rcon,
                player,
                "A build is already running. Try again when it finishes.",
            )
            return

        def place_worker():
            try:
                run_place(rcon, player)
            finally:
                if build_lock is not None:
                    build_lock.release()

        t = threading.Thread(target=place_worker, daemon=True)
        t.start()
        return

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

    if build_lock is not None and not build_lock.acquire(blocking=False):
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
            if build_lock is not None:
                build_lock.release()

    t = threading.Thread(target=worker, daemon=True)
    t.start()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    default_reach = (config.view_distance() - 1) * 16 - config.GAP
    parser = argparse.ArgumentParser(description="Minecraft MCP v2 builder")
    parser.add_argument("--once", type=str, default=None, help="Run one build and exit")
    parser.add_argument("--player", type=str, default=None, help="Player name for position and messages")
    parser.add_argument("--image", type=str, default=None, help="Image URL or local file path")
    parser.add_argument("--size", type=int, default=default_reach, help=f"Max size when no --player (default {default_reach})")
    parser.add_argument("--scad", type=str, default=None, help="OpenSCAD file to use (skip Claude)")
    parser.add_argument("--blocks", type=str, default=None, help="Blocks JSON file to use (skip Claude)")
    parser.add_argument(
        "--ai",
        type=str,
        choices=["claude", "gemini"],
        default=None,
        help="AI provider override for this run (offline runs default to gemini)",
    )
    parser.add_argument("--pixelart", type=str, default=None, help="Image URL or file path for pixel art")
    parser.add_argument("--width", type=int, default=None, help="Width for pixel art")

    args = parser.parse_args()

    if args.pixelart:
        if args.player:
            rcon = Rcon(*config.rcon_settings())
            run_pixelart(rcon, args.player, args.pixelart, width=args.width)
        else:
            run_offline_pixelart(args.pixelart, width=args.width)
        return

    # Determine if running in one-shot mode or service mode
    is_one_shot = (args.once is not None) or (args.scad is not None) or (args.image is not None and args.player is not None)

    if is_one_shot:
        description = args.once if args.once is not None else ""
        if args.player:
            rcon = Rcon(*config.rcon_settings())
            scad_override = Path(args.scad).read_text(encoding="utf-8") if args.scad else None
            blocks_override = None
            if args.blocks:
                blocks_override = json.loads(Path(args.blocks).read_text(encoding="utf-8"))

            run_build(
                rcon,
                args.player,
                description,
                image_url=args.image,
                scad_override=scad_override,
                blocks_override=blocks_override,
                ai=args.ai,
            )
        else:
            run_offline_build(
                description,
                image_url=args.image,
                size=args.size,
                scad_file=args.scad,
                blocks_file=args.blocks,
                ai=args.ai,
            )
        return

    # Service mode
    rcon = Rcon(*config.rcon_settings())
    build_lock = threading.Lock()
    try:
        rcon.command(animate.kill_crew_command())  # builders left over from a crash
    except Exception as e:
        logger.warning("Couldn't remove leftover builders: %s", e)

    logger.info("Starting mcmcp service, tailing %s...", config.LOG_FILE)
    try:
        for raw_line in tail_log(config.LOG_FILE):
            parsed = parse_chat_line(raw_line)
            if parsed:
                player_name, chat_msg = parsed
                handle_chat_line(player_name, chat_msg, rcon=rcon, build_lock=build_lock)
    except KeyboardInterrupt:
        logger.info("Service stopped by user.")


if __name__ == "__main__":
    main()
