# Overview

How the builder works, from a chat message to placed blocks.

## Why a separate service

Everything runs in one Python program next to the Minecraft server. It reads chat from
the server log and sends vanilla commands over RCON. No mod or plugin is involved, so
it can't stop the server from starting, can't conflict with other mods, and doesn't
need rebuilding for new Minecraft versions. Minecraft allows several RCON connections,
so other RCON tools keep working.

## Pipeline

```
chat line in logs/latest.log
  |  ops check (ops.json)
  v
player position, facing and dimension  <-- RCON
  |  quick free-space check, download reference image (optional)
  v
Claude (claude CLI) or Gemini (agy CLI)
  |  returns OpenSCAD code, one module per material part, plus a part -> block map
  v
OpenSCAD renders one mesh per part
  v
voxelize each part with trimesh  -->  block grid
  |  optional review: the AI sees preview images of its build and may fix it once
  v
hologram preview in game  -->  !move / !designfix / !place
  v
merge the grid into /fill boxes  -->  RCON, rate-limited, with progress in chat
```

## Chat

- Commands are plain chat messages, matched in `logs/latest.log`. Only players listed
  in `ops.json` can trigger builds; one build runs at a time.
- Progress is posted with `tellraw`. While the AI works, a heartbeat line appears every
  30 seconds, and as the answer streams in, the AI's narration line for each part is
  shown ("Raising the stone keep...").
- Chat messages are capped at 256 characters, so image links must be short direct links.

## Calling the AI

- **Claude** runs through `claude -p` on the user's subscription, with tools and MCP
  switched off. The prompt goes in on stdin, and a reference image is sent as a
  base64 image in a stream-json message. `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`
  are removed from the child's environment so it never bills the API.
- **Gemini** runs through `agy -p ... --sandbox`. The instructions go in the prompt,
  which tells it to use no tools; a reference image is saved in its working folder.
- `!designai` switches between them. The choice is global and stored in
  `settings.json` in the work folder.
- The rules for the design (output format, building and sculpture techniques, worked
  examples) are in `mcmcp/prompt.md`.

## The OpenSCAD contract

- The AI returns a `.scad` file plus a JSON object whose `blocks` maps each part to a
  full-cube block, e.g. `{"walls": "minecraft:stone_bricks", "roof": "minecraft:dark_oak_planks"}`.
  Optional `mix` gives weighted surface variants (cracked and mossy bricks), and
  optional `details` lists non-full blocks with states (stairs, slabs, doors, lanterns),
  placed last. Invalid entries are dropped instead of failing the design.
- Each part is rendered on its own: `openscad -D 'part="roof"' -o roof.stl design.scad`.
- OpenSCAD is Z-up and Minecraft is Y-up; the design's front is at y = 0, facing the
  player, and 1 unit is 1 block.

## Voxelizing

- Each part's mesh is tested at block centres on one shared grid with
  `mesh.contains`, so enclosed rooms stay empty and a 1-block wall gives exactly one
  layer. `embreex` makes this fast for large builds.
- Later parts override earlier ones (glass and trim override walls).
- Stairs and slabs are added on sloped surfaces to smooth them.
- A design larger than the world limits is scaled down to fit.

## Preview and placement

- Before designing, a read-only check confirms a small cube in front of the player is
  free, so a refusal costs no AI usage. After voxelizing, exactly the cells to be placed
  are checked; the ground layer is ignored, since grass and flowers get built over.
- The design is shown as a hologram of block display entities. Big designs are shown in
  coarser cubes to keep the entity count low. `!move` moves it, `!designfix` asks the AI
  for changes, and `!place` builds it.
- Placement merges runs of the same block into boxes, each sent as
  `execute in <dimension> run fill ...`, split to stay within `/fill` limits and
  rate-limited so the server stays healthy. The build area is force-loaded while
  placing.
- Rejected block ids and banned blocks (fluids, fire, TNT, falling blocks, portals,
  unbreakable blocks, spawners) are replaced with stone.

## Build animation

Vanilla commands only (`mcmcp/animate.py`), switched with `!designanim`:

1. **Survey:** while the AI designs, armor-stand builders stand at the plot and
   particles mark its corners.
2. **Scaffolding** rises around the footprint, only into free air on solid ground.
3. **Building:** the crew hops from tile to tile as each layer is placed, bottom-up.
4. **Teardown:** scaffolding is removed without drops and the builders are killed,
   also on errors and `!designstop`.

Builders are invulnerable, have no hitbox and drop nothing.

## Pixel art

`!pixelart <image link> [width]` builds a picture as a 1-block-thick wall with no AI.
Each pixel maps to the nearest block colour (CIELAB) from `mcmcp/palette.json`, which
holds the average texture colour of blocks that look the same on every face.
Transparent pixels become air. Upscaled sprites are shrunk back to 1 pixel per block,
and the size steps down until the wall fits the free space.

## Statues

`!statue <image link>` sends the picture to a free image-to-3D model on Hugging Face
(Hunyuan3D-2.1 by default; `!statue list` shows the others) and saves the mesh in
`statues/`, so `!statue <name>` builds it again without using the quota. A `.glb` link
is built directly. The mesh is voxelized to the chosen height (default 180 blocks) and
its texture colours are mapped to the block palette. Anime face and eye detectors
(ONNX, downloaded once to `models/`) find the eyes on the front and paint them onto the
blocks, since averaging would blur them away.

With `!statuecheck on`, Claude compares the blocks with the picture: it swaps block
colours that are clearly off and lists shape problems (missing hands, stumps, wrong
lengths) in chat. For a mesh, give the picture as a second link:
`!statue <mesh link or name> <image link>`.

## Code layout

| File | What |
|---|---|
| `mcmcp/main.py` | Chat loop, commands, the build pipeline |
| `mcmcp/llm.py` | Calls the `claude` and `agy` CLIs and parses their output |
| `mcmcp/prompt.md` | The design instructions sent to the AI |
| `mcmcp/build.py` | OpenSCAD rendering, voxelizing, box merging, placement |
| `mcmcp/ghost.py` | The hologram preview |
| `mcmcp/animate.py` | Builder crew, scaffolding and markers |
| `mcmcp/preview.py` | Preview images of a build, for the AI's review |
| `mcmcp/pixelart.py` | `!pixelart` |
| `mcmcp/statue.py` | `!statue`: image-to-3D models and saved meshes |
| `mcmcp/statue_grid.py` | Mesh to blocks for statues, and their preview images |
| `mcmcp/statue_eyes.py` | Finds and paints a statue's eyes |
| `mcmcp/figure.py` | Posing, face and colours for character designs |
| `mcmcp/rcon.py` | A minimal RCON client |
| `mcmcp/config.py` | Settings |
| `deploy/mcmcp.service` | systemd unit template |
