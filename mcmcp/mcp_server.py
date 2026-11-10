"""MCP mode: the builder as an MCP server (stdio), so an MCP client such as Claude
Desktop or Claude Code can design and build in the world.

Run with `python -m mcmcp.main --mcp`. It is its own process, with its own build lock
and last-design memory; don't run builds from it and from chat at the same time.
Each tool waits for the step to finish and returns the chat lines the player saw.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
import re
import threading

from mcp.server.mcpserver import MCPServer

from . import config, main
from .rcon import Rcon

server = MCPServer(
    "mcmcp",
    instructions=(
        "Builds in a Minecraft world. `player` is an online player: builds go in front "
        "of them, and they see progress in chat. design() shows a preview; place() builds it."
    ),
)

_lock = threading.Lock()
_rcon: Rcon | None = None
_log = threading.local()  # chat lines sent during the current tool call
_send_message = main.send_message


def _recording_send(rcon, player, msg, prefix="[design]"):
    lines = getattr(_log, "lines", None)
    if lines is not None:
        lines.append(msg)
    _send_message(rcon, player, msg, prefix)


main.send_message = _recording_send  # the pipeline looks it up at call time


def _get_rcon() -> Rcon:
    global _rcon
    if _rcon is None:
        _rcon = Rcon(*config.rcon_settings())
    return _rcon


_PLAYER_RE = re.compile(r"[A-Za-z0-9_]{3,16}")  # as in chat: the name goes into commands and file names


def _call(fn: Callable[..., None], player: str, *args) -> str:
    """Runs one pipeline step for player under the build lock and returns its chat lines."""
    if not _PLAYER_RE.fullmatch(player):
        return "Invalid player name."
    if not _lock.acquire(blocking=False):
        return "A build is already running. Try again when it finishes, or call stop."
    _log.lines = []
    try:
        fn(_get_rcon(), player, *args)
        return "\n".join(_log.lines) or "Done."
    finally:
        _log.lines = None
        _lock.release()


async def _run(fn: Callable[..., None], player: str, *args) -> str:
    # In a thread, so the server keeps answering (e.g. stop) during a long build.
    return await asyncio.to_thread(_call, fn, player, *args)


@server.tool()
async def design(player: str, description: str, image_url: str | None = None) -> str:
    """Designs a build from a description (and optional reference image link) and shows
    it as a preview in front of the player. Takes a few minutes."""
    return await _run(
        lambda rcon, p: main.run_build(rcon, p, description, image_url=image_url, preview=True),
        player,
    )


@server.tool()
async def fix(player: str, changes: str) -> str:
    """Changes the player's last design as described, e.g. 'make the roof taller'."""
    return await _run(main.run_fix, player, changes)


@server.tool()
async def move(player: str) -> str:
    """Moves the preview to where the player stands, facing where they look."""
    return await _run(main.run_move, player)


@server.tool()
async def place(player: str) -> str:
    """Builds the preview where it is (or the last design where the player stands)."""
    return await _run(main.run_place, player)


@server.tool()
async def pixelart(player: str, image_url: str, width: int | None = None) -> str:
    """Builds a picture as a flat wall in front of the player (no AI)."""
    return await _run(main.run_pixelart, player, image_url, width)


@server.tool()
async def stop(player: str) -> str:
    """Stops the running build, or removes the player's preview."""
    if _lock.locked():
        main.STOP.set()
        return "Stopping..."
    if player.strip().lower() not in main.PREVIEWS:
        return "Nothing is running."
    result = await _run(main.clear_preview, player)
    return "Preview removed." if result == "Done." else result


def run() -> None:
    server.run("stdio")
