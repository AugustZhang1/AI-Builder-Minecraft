"""Settings. Override any path with an environment variable."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Minecraft server folder (logs/latest.log, ops.json, server.properties).
SERVER_DIR = Path(os.environ.get("MCMCP_SERVER_DIR", ROOT / "server"))
LOG_FILE = SERVER_DIR / "logs" / "latest.log"
OPS_FILE = SERVER_DIR / "ops.json"

# Per-build artifacts (.scad, parts.json, STLs, log).
WORK_DIR = Path(os.environ.get("MCMCP_WORK_DIR", ROOT / "work"))

# OpenSCAD command prefix. "{cwd}" is replaced with the build folder, and OpenSCAD is run
# inside that folder with relative file names. By default it runs in Docker; set
# MCMCP_OPENSCAD to a native OpenSCAD binary to use that instead.
_openscad = os.environ.get("MCMCP_OPENSCAD")
OPENSCAD_CMD = [_openscad] if _openscad else [
    "docker", "run", "--rm", "-v", "{cwd}:/w", "-w", "/w", "openscad/openscad:dev", "openscad",
]
OPENSCAD_TIMEOUT = 180  # seconds per part

# Claude CLI (subscription).
CLAUDE_MODEL = "claude-opus-5-5"
CLAUDE_EFFORT = "xhigh"
CLAUDE_TIMEOUT = 900  # seconds for the whole call

# Build limits.
MAX_SIZE = 64                      # blocks per axis
ROOM_SIZES = (64, 48, 32, 24, 16)  # free-space sizes tried, largest first
MAX_BLOCKS = 200_000               # solid blocks per build
GAP = 3                            # blocks between the player and the build's front
FALLBACK_BLOCK = "minecraft:stone"

# Placement rate limit.
FILL_MAX_VOLUME = 4096             # blocks per /fill command (hard game limit is 32,768)
BLOCKS_PER_SECOND = 4000

IMAGE_MAX_BYTES = 8_000_000
IMAGE_MAX_SIDE = 1568              # px, longest side sent to Claude


def rcon_settings() -> tuple[str, int, str]:
    """(host, port, password) read from server.properties."""
    props = {}
    for line in (SERVER_DIR / "server.properties").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            props[k.strip()] = v.strip()
    return "127.0.0.1", int(props.get("rcon.port", 25575)), props["rcon.password"]
