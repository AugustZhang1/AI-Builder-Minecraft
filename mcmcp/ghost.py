"""In-game hologram preview for !design: block display entities showing where and how a build will go.

Every entity is a live thing the server must track and send to clients, so the hologram is capped at a
small number of entities. Big designs are shown in coarser k x k x k cubes instead of full detail.
"""
import re

import numpy as np
from scipy import ndimage

try:
    from . import build, config
except ImportError:
    from mcmcp import build, config

GHOST_TAG = "mcmcp_ghost"


def player_tag(player: str) -> str:
    """Per-player entity tag, so one player's hologram can be removed without touching others."""
    return f"{GHOST_TAG}_{player.strip().lower()}"


def kill_command(player: str | None = None) -> str:
    """Command to kill the hologram entities (only one player's if player is given)."""
    cmd = f"kill @e[type=minecraft:block_display,tag={GHOST_TAG}"
    if player:
        cmd += f",tag={player_tag(player)}"
    return cmd + "]"


def killed_count(reply: str) -> int:
    """Entities killed according to a /kill reply; 0 if none."""
    m = re.search(r"Killed (\d+) entities", reply)
    if m:
        return int(m.group(1))
    return 1 if re.search(r"Killed (?!\d)\S", reply) else 0


def _visible(solid: np.ndarray) -> np.ndarray:
    """Solid cells with at least one air face neighbour (grid edges count as air)."""
    return solid & ~ndimage.binary_erosion(solid)


def coarsen(grid: np.ndarray, k: int) -> np.ndarray:
    """Grid of k x k x k cells. A cell is solid if any voxel in it is; its block is the most common
    among its visible voxels (hidden voxels only break ties and decide cells with no visible voxel)."""
    if k == 1:
        return grid
    X, Y, Z = grid.shape
    g = np.pad(grid, ((0, -X % k), (0, -Y % k), (0, -Z % k)))
    solid = g > 0
    weight = np.where(_visible(solid), k**3 + 1, 1)
    shape = (g.shape[0] // k, k, g.shape[1] // k, k, g.shape[2] // k, k)
    counts = [((g == v) * weight).reshape(shape).sum(axis=(1, 3, 5)) for v in range(1, int(g.max()) + 1)]
    if not counts:
        return np.zeros(shape[0::2], dtype=np.uint8)
    best = np.argmax(counts, axis=0) + 1
    return np.where(solid.reshape(shape).any(axis=(1, 3, 5)), best, 0).astype(np.uint8)


def ghost_boxes(grid: np.ndarray, max_entities: int) -> tuple[int, list[build.Box]]:
    """Smallest k whose coarsened grid needs at most max_entities visible boxes, with those boxes in grid coordinates."""
    X, Y, Z = grid.shape
    k = 1
    while True:
        cg = coarsen(grid, k)
        hidden = ndimage.binary_erosion(cg > 0)
        found = [
            b for b in build.boxes(cg, cg.size)
            if not hidden[b[0]:b[3] + 1, b[1]:b[4] + 1, b[2]:b[5] + 1].all()
        ]
        if len(found) <= max_entities or max(cg.shape) == 1:
            break
        k += 1
    return k, [
        (x1 * k, y1 * k, z1 * k, min((x2 + 1) * k, X) - 1, min((y2 + 1) * k, Y) - 1, min((z2 + 1) * k, Z) - 1, val)
        for x1, y1, z1, x2, y2, z2, val in found
    ]


def summon_commands(
    boxes: list[build.Box],
    palette: list[str],
    size_x: int,
    origin: tuple[int, int, int],
    facing: str,
    dim: str,
    player: str,
) -> list[str]:
    """One block_display summon per box, scaled to the box and placed at its lowest world corner."""
    tags = f'"{GHOST_TAG}","{player_tag(player)}"'
    cmds: list[str] = []
    for box in boxes:
        x1, y1, z1, x2, y2, z2 = build.box_to_world(box, size_x, origin, facing, config.GAP)
        block = palette[box[6]].split("[", 1)[0]
        cmds.append(
            f"execute in {dim} run summon minecraft:block_display {x1}.0 {y1}.0 {z1}.0 "
            f'{{Tags:[{tags}],block_state:{{Name:"{block}"}},brightness:{{sky:15,block:15}},view_range:4f,'
            f"transformation:{{left_rotation:[0f,0f,0f,1f],right_rotation:[0f,0f,0f,1f],translation:[0f,0f,0f],"
            f"scale:[{x2 - x1 + 1}f,{y2 - y1 + 1}f,{z2 - z1 + 1}f]}}}}"
        )
    return cmds
