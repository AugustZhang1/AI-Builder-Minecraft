# minecraft-mcp-v2

An in-game AI builder for a Minecraft Java server. An op types `!design <description>`
in chat (optionally with an image link as a reference), an AI designs the build as
OpenSCAD code, the code is rendered to meshes, the meshes are turned into blocks, and
the blocks are placed in front of the player over RCON.

It runs as a separate Python service next to the server. No mods or plugins are needed,
so it works on vanilla, Fabric or modded servers alike.

See [overview.md](overview.md) for how it works in detail.

## How it works

```
chat line in logs/latest.log  (ops only)
  -> player position and facing, over RCON
  -> Claude or Gemini writes OpenSCAD code, one part per block type
  -> OpenSCAD renders a mesh per part
  -> the meshes are voxelized into a block grid
  -> a hologram preview is shown in game
  -> !place builds it with /fill commands over RCON
```

## Commands (ops only)

| Command | What it does |
|---|---|
| `!design <description>` | The AI designs it and shows a preview to place |
| `!design <image link> <description>` | The same, using the picture as a reference |
| `!designfix <changes>` | The AI changes your last design, e.g. `!designfix make the roof taller` |
| `!move` | Moves the preview to where you stand, facing where you look |
| `!place` | Builds the preview where it is (or your last design where you stand) |
| `!pixelart <image link> [width]` | Builds the picture as a flat wall (no AI) |
| `!designstop` | Stops the running build, or removes your preview |
| `!designai [claude\|gemini]` | Shows or switches the AI |
| `!designanim [on\|off]` | Shows or switches the builder crew animation |
| `!designreview [on\|off]` | Shows or switches the AI's check of its own build |
| `!designbest [on\|off]` | Makes two designs and keeps the better one (about twice the AI usage) |
| `!designhelp` | Lists the commands |

Only players in the server's `ops.json` can trigger builds, since every build uses
your AI subscription.

## Requirements

- A Minecraft Java 1.21.x server, on the same machine (the service reads the server's
  `logs/latest.log`, `ops.json` and `server.properties`).
- Python 3.12 or newer.
- OpenSCAD, a recent development snapshot with the Manifold backend
  (`--backend=manifold`). The old 2021.01 release is too slow.
- At least one AI CLI, logged in:
  - [Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude`), which runs
    on your Claude subscription, and/or
  - Google Antigravity (`agy`) for Gemini.

## Install

1. **Enable RCON** in the server's `server.properties`, then restart the server:

   ```properties
   enable-rcon=true
   rcon.port=25575
   rcon.password=<a long random password>
   ```

   Keep the RCON port closed to the internet; the service connects on `127.0.0.1`.

2. **Get the code and its Python libraries:**

   ```bash
   git clone https://github.com/AugustZhang1/minecraft-mcp-v2.git mcmcp
   cd mcmcp
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   ```

3. **Log the AI CLI in** as the user that will run the service: run `claude` and use
   `/login` (and/or log `agy` in).

4. **Point it at your server** with environment variables:

   | Variable | Meaning |
   |---|---|
   | `MCMCP_SERVER_DIR` | The Minecraft server folder |
   | `MCMCP_WORK_DIR` | Where build files and settings are saved |
   | `MCMCP_OPENSCAD` | Path to the OpenSCAD binary (if unset, OpenSCAD runs in Docker with `openscad/openscad:dev`) |

5. **Try it offline** (no server needed). This designs, renders and voxelizes, and
   saves `design.scad`, `blocks.json` and `preview.png` in the work folder:

   ```bash
   venv/bin/python -m mcmcp.main --once "a small stone watchtower"
   ```

6. **Run it:**

   ```bash
   venv/bin/python -m mcmcp.main
   ```

   Then type `!designhelp` in game.

### Run as a service (Linux, systemd)

`deploy/mcmcp.service` is a template. Replace `__USER__` and `__HOME__`, set
`MCMCP_SERVER_DIR` to your server folder, and make sure `claude`/`agy` are on the
service's `PATH` (add an `Environment=PATH=...` line if they are installed in your home
folder). It assumes the server runs as `minecraft.service`, so the builder starts and
stops with it; change `BindsTo`/`After`/`WantedBy` if yours is named differently.

```bash
sudo cp deploy/mcmcp.service /etc/systemd/system/mcmcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now mcmcp
journalctl -u mcmcp -f
```

The unit runs at low priority with CPU and memory caps, so rendering can't starve the
game.

**Uninstall:** `sudo systemctl disable --now mcmcp`, delete the unit file and the
folder. The server is left exactly as before.

## Settings

Everything else is in `mcmcp/config.py`: the AI models, build size limits, placement
speed, banned blocks (fluids, TNT, falling blocks, portals, spawners...), and the
defaults for the animation, review and best-of-two options.
