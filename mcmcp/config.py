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

# Gemini through the agy CLI (Google Antigravity).
GEMINI_MODEL = "gemini-3.8-flash-high"
PROVIDERS = ("claude", "gemini")
SETTINGS_FILE = WORK_DIR / "settings.json"  # {"provider": "claude" | "gemini"}, set with !designai
HEARTBEAT_SECONDS = 30

# Build limits.
START_ROOM = 16                    # the quick free-space check before designing
GAP = 3                            # blocks between the player and the build's front
FALLBACK_BLOCK = "minecraft:stone"
# Never placed; swapped for FALLBACK_BLOCK. Fluids and fire, TNT, falling blocks (plus every
# *_concrete_powder), portals, blocks survival players can't break, and spawners.
BANNED_BLOCKS = frozenset("minecraft:" + b for b in (
    "water", "lava", "fire", "soul_fire", "bubble_column", "tnt",
    "sand", "red_sand", "gravel", "suspicious_sand", "suspicious_gravel", "dragon_egg",
    "anvil", "chipped_anvil", "damaged_anvil", "pointed_dripstone", "scaffolding", "powder_snow",
    "nether_portal", "end_portal", "end_gateway", "end_portal_frame",
    "bedrock", "barrier", "light", "structure_void", "structure_block", "jigsaw",
    "command_block", "chain_command_block", "repeating_command_block", "reinforced_deepslate",
    "moving_piston", "piston_head", "spawner", "trial_spawner", "vault",
))

# Placement rate limit.
FILL_MAX_VOLUME = 4096             # blocks per /fill command (hard game limit is 32,768)
BLOCKS_PER_SECOND = 20000
MIN_COMMAND_DELAY = 0.002          # seconds between placement commands

# !pixelart (no AI). Sizes come from the image and the free space; these are sanity caps.
PIXELART_MAX_SIDE = 512            # px, longest side after sprite detection
PIXELART_MIN_WIDTH = 8
PALETTE_FILE = Path(__file__).resolve().parent / "palette.json"
BLOCK_COLORS_FILE = Path(__file__).resolve().parent / "block_colors.json"  # build previews

IMAGE_MAX_BYTES = 8_000_000
IMAGE_MAX_SIDE = 1568              # px, longest side sent to Claude


def server_properties() -> dict[str, str]:
    """server.properties as a dict; empty if the file is missing."""
    props = {}
    path = SERVER_DIR / "server.properties"
    if not path.exists():
        return props
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            props[k.strip()] = v.strip()
    return props


def rcon_settings() -> tuple[str, int, str]:
    """(host, port, password) read from server.properties."""
    props = server_properties()
    return "127.0.0.1", int(props.get("rcon.port", 25575)), props["rcon.password"]


def view_distance() -> int:
    """The server's view-distance in chunks (vanilla default 10)."""
    try:
        return int(server_properties().get("view-distance", 10))
    except ValueError:
        return 10
