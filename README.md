# minecraft-mcp-v2

An in-game AI builder for a Minecraft Java server. An op types `!design <description>`
in chat (optionally with an image link as a reference), an AI designs the build as
OpenSCAD code, the code is rendered to meshes, the meshes are turned into blocks, and
the blocks are placed in front of the player over RCON.

It runs as a separate Python program next to the server. No mods or plugins are
needed, so it works on vanilla, Fabric and modded servers alike, and removing it leaves
the server exactly as before.

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
your AI subscription. Image links must be short direct links (chat is capped at 256
characters).

## Requirements

- A Minecraft Java 1.21.x server, on the same machine (the builder reads the server's
  `logs/latest.log`, `ops.json` and `server.properties`).
- Python 3.12 or newer.
- OpenSCAD with the Manifold backend: either Docker (easiest), or a native
  [development snapshot](https://openscad.org/downloads.html#snapshots). The old
  2021.01 release found in most package managers is too slow.
- An AI command-line tool, logged in:
  - [Claude Code](https://www.anthropic.com/claude-code) (`claude`), which runs on your
    Claude subscription (the default), and/or
  - Google Antigravity (`agy`), for Gemini.

The steps below are for Linux. Windows works too for running it by hand (use
`venv\Scripts\python` and Docker Desktop).

## Install

1. **Enable RCON** in the server's `server.properties`, then restart the server:

   ```properties
   enable-rcon=true
   rcon.port=25575
   rcon.password=<a long random password>
   ```

   Keep the RCON port closed to the internet; the builder connects on `127.0.0.1` and
   reads the password from this file.

2. **Get the code and its Python libraries:**

   ```bash
   git clone https://github.com/AugustZhang1/minecraft-mcp-v2.git ~/mcmcp
   cd ~/mcmcp
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   ```

3. **Set up OpenSCAD**, one of:
   - **Docker:** install Docker, let your user run it
     (`sudo usermod -aG docker $USER`, then log in again) and run
     `docker pull openscad/openscad:dev`. Nothing else to configure.
   - **Native:** install a development snapshot so that `openscad` is on your `PATH`,
     or set `MCMCP_OPENSCAD` to its full path.

4. **Install and log in to the AI CLI** as the user that will run the builder:

   ```bash
   curl -fsSL https://claude.ai/install.sh | bash
   claude        # then type /login and follow the link
   ```

   For Gemini, install and log in to `agy` as well, then switch in game with
   `!designai gemini`.

5. **Tell it where your server is:**

   ```bash
   export MCMCP_SERVER_DIR=/path/to/your/minecraft/server
   ```

   | Variable | Meaning | Default |
   |---|---|---|
   | `MCMCP_SERVER_DIR` | The Minecraft server folder | `./server` |
   | `MCMCP_WORK_DIR` | Where build files and settings are saved | `./work` |
   | `MCMCP_OPENSCAD` | Path to a native OpenSCAD binary | unset: use Docker |

6. **Try it offline** (no server needed). This designs, renders and voxelizes, and
   saves `design.scad`, `blocks.json` and `preview.png` in a folder under `work/`:

   ```bash
   venv/bin/python -m mcmcp.main --once "a small stone watchtower" --ai claude
   ```

7. **Run it:**

   ```bash
   venv/bin/python -m mcmcp.main
   ```

   Then type `!designhelp` in game.

### Run it as a service (systemd)

To keep it running in the background and start it at boot, fill in the template
`deploy/mcmcp.service` and install it. Run this from `~/mcmcp`, as the same user and in
the same shell where `claude` works, with your server folder filled in:

```bash
SERVER=/path/to/your/minecraft/server
sed -e "s|__USER__|$USER|g" \
    -e "s|__MCMCP_DIR__|$PWD|g" \
    -e "s|__SERVER_DIR__|$SERVER|g" \
    -e "s|__OPENSCAD__|$(command -v openscad)|g" \
    -e "s|__PATH__|$PATH|g" \
    deploy/mcmcp.service | sudo tee /etc/systemd/system/mcmcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now mcmcp
journalctl -u mcmcp -f
```

If `openscad` isn't installed, `MCMCP_OPENSCAD` is left empty and Docker is used. The
service runs at low priority with CPU and memory caps, so rendering can't starve the
game. If your server itself runs as a systemd service (say `minecraft.service`), you can
add `BindsTo=minecraft.service` and `After=minecraft.service` under `[Unit]`, and set
`WantedBy=minecraft.service`, so the builder starts and stops with it.

**Uninstall:** `sudo systemctl disable --now mcmcp`, then delete
`/etc/systemd/system/mcmcp.service` and the `~/mcmcp` folder. RCON can be switched off
again in `server.properties`.

## Settings

Everything else is in `mcmcp/config.py`: the AI models, build size limits, placement
speed, banned blocks (fluids, TNT, falling blocks, portals, spawners...), and the
defaults for the animation, review and best-of-two options.

## License

MIT, see [LICENSE](LICENSE).
