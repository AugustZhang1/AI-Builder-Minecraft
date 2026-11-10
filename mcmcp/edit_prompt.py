"""The AI side of !edit: write a scan folder describing a build, and ask the AI to change it."""
from __future__ import annotations

import json
import string
import threading
from pathlib import Path
from typing import Any, Callable

import numpy as np

try:
    from . import config, interior, llm, preview
except ImportError:
    from mcmcp import config, interior, llm, preview

SYMBOLS = string.ascii_letters + string.digits + "#$%&*+=-/!:;<>~^"  # no ".", "_", "@", "?"
CUT_EVERY = 4  # layers between cutaway pictures
MAX_LOCKED_LISTED = 50
AIR = "minecraft:air"

EDIT_TEXT = """You change an existing Minecraft build for a player.
You get the player's request and index.md, which describes the build: its size, the symbol legend, the locked
blocks and the files. Read the files you need with the Read tool before you edit. They are in your working folder:
outside.png (the build from two angles, then front, side and top), cut-zNN.png (the build cut open at layer NN,
looking down into the rooms) and layer-zNN.txt (one text map per layer). Layers that are all air have no file.
You can only read files; do not run commands or create, edit or delete files.

Coordinates are local to the selected box: x = east, y = north (away from a player facing north), z = up; the box
starts at 0,0,0 and all three axes are inclusive. In a layer map the top row is north (+Y) and the bottom row is
y = 0. Map symbols: a letter or digit is a block id (see the legend, most common first), . is outside air, _ is
air inside a closed room, @ is a locked block, ? is a block id the legend has no symbol for. The maps leave out
block states: stair and slab directions show in the pictures.
Locked blocks (chests and other blocks with contents) cannot be changed: edits on them are ignored.
Stay inside the box; edits outside it are dropped.

Change only what the request asks for and keep everything else as it is. Match the style and palette of the build.
Write block states as if the player faces north: north = +Y, south = -Y, east = +X, west = -X.
A door is ONE lower entry (the code adds the upper half); a bed is two entries, part=foot and part=head.
Place supports before dependent blocks. Use no entities. Use only real Minecraft block ids and states.
No fluids, fire, TNT, sand, gravel, concrete powder, command blocks, spawners, portals or bedrock.
Edits are placed in order and a later edit replaces an earlier one. minecraft:air removes blocks.
While working, write a few short progress lines starting with "// > " (they are shown to the player).
Reply with ONE ```json block: {"edits": [[x1,y1,z1,x2,y2,z2,"minecraft:block[state]"], [x,y,z,"minecraft:block"], ...]}
in local coordinates (boxes are inclusive).
If the request cannot be done, reply {"edits": []} and one line saying why."""

REVIEW_TEXT = (
    "This is one review pass over the first reply below. The edits of the first reply were applied to the build: "
    "result.png and result-cut-zNN.png (in your folder) show the result, in the same style as the pictures of the "
    "original. Read them. Check the result against the request: is it done, does it fit the build, is anything "
    "broken, floating or in the way? If it is right, reply with exactly KEEP. Otherwise reply with a COMPLETE "
    "corrected edits list for the ORIGINAL build (the maps and index.md describe it), not a patch on top of the "
    "first reply, in the same ```json block format."
)


def _base(block: str) -> str:
    return block.split("[", 1)[0]


def _number(name: str) -> int:
    """The layer number in a file name like layer-z08.txt."""
    return int(name.rsplit("z", 1)[1].split(".")[0])


def _picture_grid(grid: np.ndarray, palette: list[str]) -> tuple[np.ndarray, list[str]]:
    """Grid and palette for the pictures: base ids (states kept for slabs and stairs), at most 255 entries, uint8."""
    new_palette = [AIR]
    index: dict[str, int] = {}
    remap = np.zeros(max(len(palette), 1), dtype=np.uint8)
    for i, block in enumerate(palette):
        base = _base(block)
        if base == AIR:
            continue
        key = block if base.endswith(("_slab", "_stairs")) else base
        if key not in index:
            if len(new_palette) < 255:
                index[key] = len(new_palette)
                new_palette.append(key)
            else:
                index[key] = 1  # too many kinds: the rest show as the first one
        remap[i] = index[key]
    return remap[grid], new_palette


def _write_pictures(folder: Path, grid: np.ndarray, palette: list[str], whole: str, cut: str) -> None:
    """whole.png and cut-zNN.png (cut is a format with {z}) for a grid."""
    small, pal = _picture_grid(grid, palette)
    (folder / whole).write_bytes(preview.blocks_png(small, pal))
    for z in range(0, grid.shape[2], CUT_EVERY):
        if small[:, :, : z + 1].any():
            (folder / cut.format(z=z)).write_bytes(preview.blocks_png(small[:, :, : z + 1], pal))


def _inside(grid: np.ndarray, palette: list[str]) -> np.ndarray | None:
    """Mask of the air inside closed walls, or None if the build has none."""
    try:
        found = interior.analyze(grid, palette, [])
    except Exception:
        return None
    if isinstance(found, str):
        return None
    return found.free


def _symbols(grid: np.ndarray, palette: list[str]) -> dict[str, str]:
    """Base block id -> symbol, most common first; ids beyond SYMBOLS get '?'."""
    counts: dict[str, int] = {}
    for i, n in enumerate(np.bincount(grid.ravel(), minlength=len(palette))[: len(palette)]):
        base = _base(palette[i])
        if base != AIR and n:
            counts[base] = counts.get(base, 0) + int(n)
    order = sorted(counts, key=lambda b: -counts[b])
    return {b: SYMBOLS[i] if i < len(SYMBOLS) else "?" for i, b in enumerate(order)}


def _layer_text(chars: np.ndarray, z: int) -> str:
    """One layer map; chars is the (X, Y) array of symbols. Top row = north."""
    X, Y = chars.shape
    xr = range(X)
    lines = [f"z = {z}"]
    lines.append("    " + "".join(str(x // 10 % 10) if x >= 10 else " " for x in xr))
    lines.append("    " + "".join(str(x % 10) for x in xr))
    for y in range(Y - 1, -1, -1):
        lines.append(f"{y:3d} " + "".join(chars[:, y]))
    return "\n".join(lines) + "\n"


def write_scan(folder: Path, grid: np.ndarray, palette: list[str], locked: np.ndarray) -> str:
    """Write the scan folder (index.md, outside.png, cut-zNN.png, layer-zNN.txt) and return the text of index.md."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for old in [*folder.glob("layer-z*.txt"), *folder.glob("cut-z*.png")]:
        old.unlink()
    X, Y, Z = grid.shape

    symbols = _symbols(grid, palette)
    lut = np.array(["." if _base(b) == AIR else symbols.get(_base(b), "?") for b in palette] or ["."])
    is_air = np.array([_base(b) == AIR for b in palette] or [True])[grid]
    inside = _inside(grid, palette)

    air_layers, layer_files = [], []
    for z in range(Z):
        if is_air[:, :, z].all() and not locked[:, :, z].any():
            air_layers.append(z)
            continue
        chars = lut[grid[:, :, z]]
        if inside is not None:
            chars[is_air[:, :, z] & inside[:, :, z]] = "_"
        chars[locked[:, :, z]] = "@"
        name = f"layer-z{z:02d}.txt"
        (folder / name).write_text(_layer_text(chars, z), encoding="utf-8")
        layer_files.append(name)

    _write_pictures(folder, grid, palette, "outside.png", "cut-z{z:02d}.png")
    cuts = sorted(p.name for p in folder.glob("cut-z*.png"))

    lines = [
        "# The build",
        f"Size: {X} x {Y} x {Z} blocks (x = east 0..{X - 1}, y = north 0..{Y - 1}, z = up 0..{Z - 1}).",
        "Coordinates are local to this box; boxes in edits are inclusive. The top row of a layer map is north (+Y).",
        "",
        "## Symbols (most common first)",
        ". = outside air",
    ]
    if inside is not None:
        lines.append("_ = air inside closed walls")
    lines.append("@ = locked block (has contents; it cannot be changed)")
    lines += [f"{sym} = {block}" for block, sym in symbols.items()]
    if "?" in symbols.values():
        lines.append("? = a block id that has no symbol of its own")
    lines += ["", "## Locked blocks"]
    spots = np.argwhere(locked)
    if len(spots) == 0:
        lines.append("none")
    for x, y, z in spots[:MAX_LOCKED_LISTED]:
        lines.append(f"{x},{y},{z} {palette[grid[x, y, z]]}")
    if len(spots) > MAX_LOCKED_LISTED:
        lines.append(f"... and {len(spots) - MAX_LOCKED_LISTED} more")
    lines += ["", "## Files", "outside.png: the build from two angles, then front, side and top"]
    lines += [f"{name}: the build cut open at layer {_number(name)}, looking down" for name in cuts]
    lines += [f"{name}: text map of layer {_number(name)}" for name in layer_files]
    lines.append("Block states are not in the maps; stair and slab directions show in the pictures.")
    lines += ["", "## Air layers (no file)", ", ".join(str(z) for z in air_layers) or "none"]
    text = "\n".join(lines) + "\n"
    (folder / "index.md").write_text(text, encoding="utf-8")
    return text


def write_result(folder: Path, grid: np.ndarray, palette: list[str]) -> None:
    """Pictures of an edited grid: result.png and result-cut-zNN.png. Old result files are removed."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for old in [folder / "result.png", *folder.glob("result-cut-z*.png")]:
        old.unlink(missing_ok=True)
    _write_pictures(folder, grid, palette, "result.png", "result-cut-z{z:02d}.png")


def _task(request: str, viewer: str) -> str:
    text = f"The player's request:\n{request.strip()}"
    if viewer.strip():
        text = f"{viewer.strip()}\n\n{text}"
    return text


def ask(
    request: str,
    folder: Path,
    *,
    viewer: str = "",
    review: str | None = None,
    on_text: Callable[[str], Any] | None = None,
    stop: threading.Event | None = None,
    timeout: float = config.CLAUDE_TIMEOUT,
    provider: str = "claude",
) -> str:
    """Ask the AI to edit the scanned build in folder. Returns the raw reply (also written to edit-reply.txt,
    or edit-review-reply.txt for a review, where review is the first reply)."""
    if provider not in config.PROVIDERS:
        raise llm.LlmError(f"Unknown provider: {provider!r}. Must be one of {config.PROVIDERS}")
    work = Path(folder).resolve()
    prompt_file = work / "edit.md"
    prompt_file.write_text(EDIT_TEXT, encoding="utf-8")
    task = _task(request, viewer)
    if provider == "claude":
        cmd = llm._cli_command(prompt_file)
        cmd[cmd.index("--tools") + 1] = "Read"
        text = f"{task}\n\nindex.md:\n{(work / 'index.md').read_text(encoding='utf-8')}"
        if review is not None:
            text += f"\n\n{REVIEW_TEXT}\n\nFirst reply:\n{review}"
        content = [{"type": "text", "text": text}]
        msg = {"type": "user", "message": {"role": "user", "content": content}}
        stdin_payload: str | None = json.dumps(msg) + "\n"
    else:
        extra = ""
        if review is not None:
            (work / "edit-first-reply.txt").write_text(review, encoding="utf-8")
            extra = f"\n\n{REVIEW_TEXT}\nThe first reply is the file {work / 'edit-first-reply.txt'}."
        prompt = (
            f"Read {prompt_file} first and follow it exactly.\n"
            f"The build is scanned in the folder {work}. Read {work / 'index.md'} first, then the pictures and layer "
            "maps you need.\n"
            "Do not use any tools except to read files in that folder. Do not run commands or create, edit or "
            "delete files. Reply with progress lines followed by ONE JSON fence containing the edits list "
            f"(or KEEP when asked to review).\n\n{task}{extra}"
        )
        cmd = llm._agy_command(prompt)
        stdin_payload = None

    try:
        chunks: list[str] = []
        for chunk in llm._run_cli(cmd, stdin_payload, work, timeout, provider=provider, stop=stop):
            chunks.append(chunk)
            if on_text:
                on_text(chunk)
        reply = "".join(chunks)
        (work / ("edit-review-reply.txt" if review is not None else "edit-reply.txt")).write_text(
            reply, encoding="utf-8"
        )
        return reply
    except llm.LlmError:
        raise
    except Exception as exc:
        raise llm.LlmError(f"Edit failed: {exc}") from exc
