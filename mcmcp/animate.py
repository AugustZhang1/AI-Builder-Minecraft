"""Animated construction helpers for !design: corner markers, scaffolding, and armor-stand crew."""
import logging
import math
import threading
import time
from typing import Any, Callable

try:
    from . import build, config
except ImportError:
    from mcmcp import build, config

logger = logging.getLogger(__name__)

CREW_TAG = "mcmcp_crew"
SURVEY_SECONDS = 2.0
SCAFFOLD_SPACING = 8
SCAFFOLD_LAYER_SECONDS = 0.1
CREW_COLOURS = (0xD87F33, 0x835432, 0x3C44AA)  # orange, brown, blue workwear

TILE_MAX = 8
STEP_SECONDS = 0.2
MAX_BUILD_SECONDS = 180
MIN_CREW = 3
MAX_CREW = 8
TILES_PER_WORKER = 30
CREW_MARKER = True

CREW_YAWS = {
    "south": 0,
    "west": 90,
    "north": 180,
    "east": -90,
}


def kill_crew_command() -> str:
    """Command to kill all MCMCP crew armor stands across all dimensions."""
    return f"kill @e[type=minecraft:armor_stand,tag={CREW_TAG}]"


def corner_particle_commands(
    dim: str,
    origin: tuple[int, int, int],
    facing: str,
    size: int,
) -> list[str]:
    """Commands marking the 4 ground corners of the plot with happy villager particles."""
    corners = [(0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1)]
    cmds: list[str] = []
    for x, y in corners:
        wx, wy, wz = build.to_world(x, y, 0, size, origin, facing, config.GAP)
        cmd = (
            f"execute in {dim} run particle minecraft:happy_villager "
            f"{wx + 0.5} {wy + 1} {wz + 0.5} 0 1 0 0 10"
        )
        cmds.append(cmd)
    return cmds


class Survey:
    """Daemon thread that periodically sends survey commands (e.g. corner particles)."""

    def __init__(self, rcon: Any, commands: list[str], interval: float = SURVEY_SECONDS) -> None:
        self.rcon = rcon
        self.commands = list(commands)
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            self.stop()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _send_all(self) -> None:
        for cmd in self.commands:
            try:
                self.rcon.command(cmd)
            except Exception as e:
                logger.warning("Survey command failed: %s", e)

    def _run(self) -> None:
        self._send_all()
        while not self._stop_event.wait(self.interval):
            self._send_all()

    def stop(self) -> None:
        self._stop_event.set()
        t = self._thread
        if t and t.is_alive():
            if threading.current_thread() != t:
                t.join(timeout=1.0)


def scaffold_columns(shape: tuple[int, int, int]) -> list[tuple[int, int]]:
    """Design (x, y) cells on the ring 1 block outside the grid footprint."""
    sx, sy, _sz = shape
    cols: list[tuple[int, int]] = [
        (-1, -1),
        (sx, -1),
        (-1, sy),
        (sx, sy),
    ]
    for x in range(-1, sx + 1, SCAFFOLD_SPACING):
        cols.append((x, -1))
        cols.append((x, sy))
    for y in range(-1, sy + 1, SCAFFOLD_SPACING):
        cols.append((-1, y))
        cols.append((sx, y))
    return list(dict.fromkeys(cols))


class Scaffold:
    """Temporary scaffolding ring raised bottom-up and lowered top-down."""

    def __init__(
        self,
        rcon: Any,
        dim: str,
        origin: tuple[int, int, int],
        facing: str,
        shape: tuple[int, int, int],
    ) -> None:
        self.rcon = rcon
        self.dim = dim
        self.origin = origin
        self.facing = facing
        self.shape = shape
        self.columns: list[tuple[int, int]] = []

    def raise_(self, wait: Callable[[float], bool]) -> None:
        """Raise scaffolding columns bottom-up after verifying foundations."""
        sx, _sy, sz = self.shape
        candidates = scaffold_columns(self.shape)
        self.columns = []

        for col_x, col_y in candidates:
            wx, wy, wz = build.to_world(col_x, col_y, 0, sx, self.origin, self.facing, config.GAP)
            check_cmd = f"execute in {self.dim} if block {wx} {wy - 1} {wz} #minecraft:replaceable"
            try:
                reply = self.rcon.command(check_cmd)
            except Exception as e:
                logger.warning("Scaffold check failed: %s", e)
                continue
            if reply.startswith("Test passed"):
                continue

            fill_air_cmd = (
                f"execute in {self.dim} run fill {wx} {wy} {wz} {wx} {wy} {wz} "
                f"minecraft:scaffolding[distance=0] replace minecraft:air"
            )
            try:
                reply = self.rcon.command(fill_air_cmd)
            except Exception as e:
                logger.warning("Scaffold fill air failed: %s", e)
                reply = ""

            if not reply.startswith("Successfully filled"):
                fill_grass_cmd = (
                    f"execute in {self.dim} run fill {wx} {wy} {wz} {wx} {wy} {wz} "
                    f"minecraft:scaffolding[distance=0] replace minecraft:short_grass"
                )
                try:
                    reply = self.rcon.command(fill_grass_cmd)
                except Exception as e:
                    logger.warning("Scaffold fill grass failed: %s", e)
                    reply = ""
                if not reply.startswith("Successfully filled"):
                    continue

            self.columns.append((col_x, col_y))

        if not self.columns:
            return

        for z in range(1, sz):
            for col_x, col_y in self.columns:
                wx, wy, wz = build.to_world(col_x, col_y, z, sx, self.origin, self.facing, config.GAP)
                cmd = (
                    f"execute in {self.dim} run fill {wx} {wy} {wz} {wx} {wy} {wz} "
                    f"minecraft:scaffolding[distance=0] replace minecraft:air"
                )
                try:
                    self.rcon.command(cmd)
                except Exception as e:
                    logger.warning("Scaffold raise layer failed: %s", e)

            if wait(SCAFFOLD_LAYER_SECONDS):
                return

    def lower(self) -> None:
        """Tear down scaffolding top-down; safe to call anytime."""
        if not self.columns:
            return
        sx, sy, sz = self.shape

        def _line_fill(p1: tuple[int, int, int], p2: tuple[int, int, int]) -> str:
            w1x, w1y, w1z = build.to_world(p1[0], p1[1], p1[2], sx, self.origin, self.facing, config.GAP)
            w2x, w2y, w2z = build.to_world(p2[0], p2[1], p2[2], sx, self.origin, self.facing, config.GAP)
            x1, x2 = min(w1x, w2x), max(w1x, w2x)
            y1, y2 = min(w1y, w2y), max(w1y, w2y)
            z1, z2 = min(w1z, w2z), max(w1z, w2z)
            return (
                f"execute in {self.dim} run fill {x1} {y1} {z1} {x2} {y2} {z2} "
                f"minecraft:air replace minecraft:scaffolding"
            )

        for z in range(sz - 1, 0, -1):
            lines = [
                ((-1, -1, z), (sx, -1, z)),
                ((-1, sy, z), (sx, sy, z)),
                ((-1, -1, z), (-1, sy, z)),
                ((sx, -1, z), (sx, sy, z)),
            ]
            for p1, p2 in lines:
                cmd = _line_fill(p1, p2)
                try:
                    self.rcon.command(cmd)
                except Exception as e:
                    logger.warning("Scaffold lower layer fill failed: %s", e)
            time.sleep(SCAFFOLD_LAYER_SECONDS)

        for col_x, col_y in self.columns:
            wx, wy, wz = build.to_world(col_x, col_y, 0, sx, self.origin, self.facing, config.GAP)
            cmd = (
                f"execute in {self.dim} run fill {wx} {wy} {wz} {wx} {wy} {wz} "
                f"minecraft:air replace minecraft:scaffolding"
            )
            try:
                self.rcon.command(cmd)
            except Exception as e:
                logger.warning("Scaffold lower ground fill failed: %s", e)


def tiles(boxes: list[build.Box]) -> list[build.Box]:
    """Cut placement boxes into 1-high tiles of at most TILE_MAX x TILE_MAX, sorted by z."""
    result: list[build.Box] = []
    for x1, y1, z1, x2, y2, z2, val in boxes:
        for z in range(z1, z2 + 1):
            for y in range(y1, y2 + 1, TILE_MAX):
                ty2 = min(y + TILE_MAX - 1, y2)
                for x in range(x1, x2 + 1, TILE_MAX):
                    tx2 = min(x + TILE_MAX - 1, x2)
                    result.append((x, y, z, tx2, ty2, z, val))
    return sorted(result, key=lambda b: b[2])


def crew_size(n_tiles: int) -> int:
    """Clamp ceil(n_tiles / TILES_PER_WORKER) between MIN_CREW and MAX_CREW."""
    raw = math.ceil(n_tiles / TILES_PER_WORKER) if TILES_PER_WORKER > 0 else MIN_CREW
    return max(MIN_CREW, min(MAX_CREW, raw))


def tile_top_centre(
    tile: build.Box,
    sx: int,
    origin: tuple[int, int, int],
    facing: str,
) -> tuple[float, float, float]:
    """World coordinate of the top centre of a 1-high design tile."""
    w1x, wy1, w1z, w2x, wy2, w2z = build.box_to_world(tile, sx, origin, facing, config.GAP)
    cx = (w1x + w2x) / 2.0 + 0.5
    top_y = float(wy1 + 1)
    cz = (w1z + w2z) / 2.0 + 0.5
    return (cx, top_y, cz)


class Crew:
    """Armor stand construction crew that poses and builds block designs."""

    def __init__(
        self,
        rcon: Any,
        dim: str,
        player: str,
        origin: tuple[int, int, int],
        facing: str,
        size: int = 3,
    ) -> None:
        self.rcon = rcon
        self.dim = dim
        self.player = player
        self.origin = origin
        self.facing = facing
        self.size = size
        self.plot_size: int | None = None
        self.positions: dict[int, tuple[float, float, float]] = {}
        self.yaws: dict[int, float] = {}
        self.summon_spots: dict[int, tuple[float, float, float]] = {}
        self._arm_flipped: dict[int, bool] = {}
        self._next_member: int = 0

    def summon_pos(self, i: int, n: int, plot_size: int) -> tuple[float, float, float]:
        """Design front edge (y = -2, z = 0) world position for member i of n."""
        xi = int(round(i * (plot_size - 1) / (n - 1))) if n > 1 else 0
        wx, wy, wz = build.to_world(xi, -2, 0, plot_size, self.origin, self.facing, config.GAP)
        return (wx + 0.5, float(wy), wz + 0.5)

    def summon_command(self, i: int, x: float, y: float, z: float, yaw: float) -> str:
        """Command to summon armor stand for crew member i."""
        c = CREW_COLOURS[i % len(CREW_COLOURS)]
        marker_nbt = "Marker:1b," if CREW_MARKER else ""
        return (
            f'execute in {self.dim} run summon minecraft:armor_stand {x} {y} {z} '
            f'{{Tags:["{CREW_TAG}","{CREW_TAG}_{i}"],{marker_nbt}Invulnerable:1b,NoGravity:1b,ShowArms:1b,NoBasePlate:1b,DisabledSlots:4144959,Rotation:[{yaw}f,0f],'
            f'ArmorItems:['
            f'{{id:"minecraft:leather_boots",count:1,components:{{"minecraft:dyed_color":{{rgb:{c}}}}}}},'
            f'{{id:"minecraft:leather_leggings",count:1,components:{{"minecraft:dyed_color":{{rgb:{c}}}}}}},'
            f'{{id:"minecraft:leather_chestplate",count:1,components:{{"minecraft:dyed_color":{{rgb:{c}}}}}}},'
            f'{{id:"minecraft:player_head",count:1,components:{{"minecraft:profile":{{name:"{self.player}"}}}}}}'
            f'],'
            f'HandItems:[{{id:"minecraft:iron_pickaxe",count:1}},{{}}]}}'
        )

    def _summon_member(self, i: int, n: int, plot_size: int) -> None:
        pos = self.summon_pos(i, n, plot_size)
        yaw = float(CREW_YAWS.get(self.facing, 0))
        self.positions[i] = pos
        self.summon_spots[i] = pos
        self.yaws[i] = yaw
        cmd = self.summon_command(i, pos[0], pos[1], pos[2], yaw)
        try:
            self.rcon.command(cmd)
        except Exception as e:
            logger.warning("Crew summon failed for member %d: %s", i, e)

    def summon(self, plot_size: int) -> None:
        """Summon crew members facing the build along the front of the plot."""
        self.plot_size = plot_size
        for i in range(self.size):
            self._summon_member(i, self.size, plot_size)

    def build(
        self,
        shape: tuple[int, int, int],
        boxes: list[build.Box],
        place_one: Callable[[build.Box], int],
        wait: Callable[[float], bool],
    ) -> bool:
        """Animate cartoon crew placing tiles layer by layer. Returns True if stopped."""
        sx, sy, sz = shape
        plot_sz = self.plot_size if self.plot_size is not None else sx

        # 1. ts = tiles(boxes). Summon extra members so crew has crew_size(len(ts)) members
        ts = tiles(boxes)
        needed = crew_size(len(ts))
        if needed > self.size:
            for i in range(self.size, needed):
                self._summon_member(i, needed, plot_sz)
            self.size = needed

        # Ensure initial member positions if summon was not called
        yaw_initial = float(CREW_YAWS.get(self.facing, 0))
        for i in range(self.size):
            if i not in self.positions:
                pos = self.summon_pos(i, self.size, plot_sz)
                self.positions[i] = pos
                self.summon_spots[i] = pos
                self.yaws[i] = yaw_initial

        # 2. batch = max(1, ceil(len(ts) * STEP_SECONDS / MAX_BUILD_SECONDS)): tiles per job.
        batch = max(1, math.ceil(len(ts) * STEP_SECONDS / MAX_BUILD_SECONDS))

        # 3. Group tiles by layer (z). Loop in steps.
        layers: dict[int, list[build.Box]] = {}
        for t in ts:
            layers.setdefault(t[2], []).append(t)
        layer_keys = sorted(layers.keys())
        current_layer_idx = 0

        air_job: tuple[int, list[build.Box], tuple[float, float, float]] | None = None

        while True:
            step_start = time.monotonic()
            blocks_placed_this_step = 0

            # (a) LAND: if a job is in the air (member i, list of tiles):
            if air_job is not None:
                member_i, job_tiles, (lx, ly, lz) = air_job
                yaw = self.yaws[member_i]

                tp_land_cmd = (
                    f"execute in {self.dim} run tp @e[tag={CREW_TAG}_{member_i},limit=1] "
                    f"{lx} {ly} {lz} {yaw} 0"
                )
                try:
                    self.rcon.command(tp_land_cmd)
                except Exception as e:
                    logger.warning("Crew land tp failed: %s", e)

                for tile in job_tiles:
                    blocks_placed_this_step += place_one(tile)

                for tile in job_tiles:
                    tcx, tcy, tcz = tile_top_centre(tile, sx, self.origin, self.facing)
                    poof_cmd = (
                        f"execute in {self.dim} run particle minecraft:poof "
                        f"{tcx} {tcy} {tcz} 0.4 0.4 0.4 0.02 8"
                    )
                    try:
                        self.rcon.command(poof_cmd)
                    except Exception as e:
                        logger.warning("Crew poof particle failed: %s", e)

                is_flipped = self._arm_flipped.get(member_i, False)
                arm_pose = "[-40f,0f,0f]" if is_flipped else "[-110f,0f,0f]"
                self._arm_flipped[member_i] = not is_flipped
                data_cmd = (
                    f"execute in {self.dim} run data merge entity @e[tag={CREW_TAG}_{member_i},limit=1] "
                    f"{{Pose:{{RightArm:{arm_pose}}}}}"
                )
                try:
                    self.rcon.command(data_cmd)
                except Exception as e:
                    logger.warning("Crew arm merge failed: %s", e)

                self.positions[member_i] = (lx, ly, lz)
                air_job = None

            # (b) LIFT: if current layer has no tiles left, move on to next layer
            while current_layer_idx < len(layer_keys) and not layers[layer_keys[current_layer_idx]]:
                current_layer_idx += 1

            if current_layer_idx < len(layer_keys):
                cur_tiles = layers[layer_keys[current_layer_idx]]
                member_i = self._next_member
                self._next_member = (self._next_member + 1) % self.size
                mx, my, mz = self.positions[member_i]

                def _dist_sq(tile: build.Box) -> float:
                    tcx, tcy, tcz = tile_top_centre(tile, sx, self.origin, self.facing)
                    return (tcx - mx) ** 2 + (tcy - my) ** 2 + (tcz - mz) ** 2

                cur_tiles.sort(key=_dist_sq)
                job_tiles = cur_tiles[:batch]
                layers[layer_keys[current_layer_idx]] = cur_tiles[batch:]

                first_tcx, first_tcy, first_tcz = tile_top_centre(job_tiles[0], sx, self.origin, self.facing)
                dx = first_tcx - mx
                dz = first_tcz - mz
                if dx == 0 and dz == 0:
                    yaw = self.yaws.get(member_i, float(CREW_YAWS.get(self.facing, 0)))
                else:
                    yaw = math.degrees(math.atan2(-dx, dz))
                    if yaw == 0.0:
                        yaw = 0.0
                self.yaws[member_i] = yaw

                mid_x = (mx + first_tcx) / 2.0
                mid_y = max(my, first_tcy) + 1.0
                mid_z = (mz + first_tcz) / 2.0

                lift_tp_cmd = (
                    f"execute in {self.dim} run tp @e[tag={CREW_TAG}_{member_i},limit=1] "
                    f"{mid_x} {mid_y} {mid_z} {yaw} 0"
                )
                try:
                    self.rcon.command(lift_tp_cmd)
                except Exception as e:
                    logger.warning("Crew lift tp failed: %s", e)

                self.positions[member_i] = (mid_x, mid_y, mid_z)
                air_job = (member_i, job_tiles, (first_tcx, first_tcy, first_tcz))

            # (c) If nothing is in the air and no tiles are left, stop looping.
            remaining_tiles = any(layers[k] for k in layer_keys[current_layer_idx:])
            if air_job is None and not remaining_tiles:
                break

            # (d) Wait
            elapsed = time.monotonic() - step_start
            target_secs = max(STEP_SECONDS, blocks_placed_this_step / config.BLOCKS_PER_SECOND)
            wait_secs = max(0.0, target_secs - elapsed)
            if wait(wait_secs):
                return True

        # 4. Finish (not stopped)
        finish_yaw = CREW_YAWS.get(self.facing, 0)
        for i in range(self.size):
            spot = self.summon_spots.get(i)
            if spot is None:
                spot = self.summon_pos(i, self.size, plot_sz)
                self.summon_spots[i] = spot
            sp_x, sp_y, sp_z = spot
            self.positions[i] = spot
            self.yaws[i] = float(finish_yaw)

            tp_cmd = (
                f"execute in {self.dim} run tp @e[tag={CREW_TAG}_{i},limit=1] "
                f"{sp_x} {sp_y} {sp_z} {finish_yaw} 0"
            )
            try:
                self.rcon.command(tp_cmd)
            except Exception as e:
                logger.warning("Crew finish tp failed for member %d: %s", i, e)

            cheer_cmd = (
                f"execute in {self.dim} run data merge entity @e[tag={CREW_TAG}_{i},limit=1] "
                f"{{Pose:{{RightArm:[-150f,0f,0f],LeftArm:[-150f,0f,0f]}}}}"
            )
            try:
                self.rcon.command(cheer_cmd)
            except Exception as e:
                logger.warning("Crew cheer merge failed for member %d: %s", i, e)

            poof_cmd = (
                f"execute in {self.dim} run particle minecraft:poof "
                f"{sp_x} {sp_y} {sp_z} 0.4 0.4 0.4 0.02 8"
            )
            try:
                self.rcon.command(poof_cmd)
            except Exception as e:
                logger.warning("Crew finish poof failed for member %d: %s", i, e)

        if wait(1.0):
            return True
        return False

    def remove(self) -> None:
        """Kill all crew armor stands."""
        try:
            self.rcon.command(kill_crew_command())
        except Exception as e:
            logger.warning("Crew remove failed: %s", e)
