# Setup instructions for an AI agent

These steps are for an AI coding agent (Claude Code, Gemini CLI, Codex...) to install
this builder for a user. The agent does the work. The user only logs in to the AI
accounts when asked and approves changes to their Minecraft server.

For what the builder does, read [README.md](README.md) first.

## Rules

- **Work on the machine that runs the Minecraft server.** The builder reads the server's
  log files, so it must run on the same machine. If you are on a different machine, stop
  and tell the user.
- **The server may have players on it.** Don't stop, restart or change the server, and
  don't edit anything in its folder, without the user's OK for that exact step. The only
  server change needed is enabling RCON in `server.properties`.
- **Never open the RCON port to the internet.** Don't touch the firewall. The builder
  connects on `127.0.0.1`.
- Ask before each `sudo` step, and say what it does. `sudo` may ask the user for their
  password.
- If a step fails, show the user the error and fix it before moving on.
- Say at the start what you are going to do, in a few lines, and report after each step.

The commands below are for Linux (tested on Ubuntu 24.04). On Windows, use the same steps
with Docker Desktop, `py -3` for Python, `venv\Scripts\python` instead of
`venv/bin/python`, and skip step 8.

## 1. Ask the user

- Where the Minecraft server folder is (the one with `server.properties`). Offer to
  search for it: `find / -name server.properties -not -path '*/proc/*' 2>/dev/null`.
- Their Minecraft player name, to check they are an op.
- Whether they want Gemini (through Google Antigravity) as well as Claude. Claude alone
  is enough.

Check the folder has `server.properties` and `logs/latest.log`. Check the player is in
`ops.json`; if not, tell the user to type `op <name>` in the server console. Only ops can
use the builder.

## 2. Install the system tools

Check what is already there first: `git --version`, `python3 --version` (3.12 or newer),
`docker --version`.

On Ubuntu or Debian, install anything missing:

```bash
sudo apt-get update
sudo apt-get install -y git python3-venv docker.io
sudo usermod -aG docker $USER
docker pull openscad/openscad:dev   # may need: sg docker -c "docker pull openscad/openscad:dev"
```

The new `docker` group only applies to new logins. Until the user logs in again, run
Docker commands (and step 6) through `sg docker -c "..."`. The service in step 8 gets
the group on its own.

On other systems, install the same things with the system's package manager.

## 3. Get the code

```bash
git clone https://github.com/AugustZhang1/AI-Builder-Minecraft.git ~/mcmcp
cd ~/mcmcp
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Run everything below from `~/mcmcp`.

## 4. Enable RCON

Read the server's `server.properties`. RCON is ready if it has `enable-rcon=true` and a
non-empty `rcon.password`. If so, skip to step 5.

If not, ask the user before changing it. Once they agree, set these three lines (keep any
existing `rcon.port`), with a new random password from `openssl rand -hex 16`:

```properties
enable-rcon=true
rcon.port=25575
rcon.password=<the random password>
```

The change only takes effect after a server restart. **Don't restart it yourself.** Tell
the user it needs a restart, and wait until they say it's done. Then check RCON answers:

```bash
MCMCP_SERVER_DIR=/path/to/server venv/bin/python -c "from mcmcp import config; from mcmcp.rcon import Rcon; r = Rcon(*config.rcon_settings()); r.connect(); print(r.command('list'))"
```

It should print the list of online players.

## 5. The AI CLIs (the user logs in)

**Claude Code** (required):

1. If `claude` isn't installed: `curl -fsSL https://claude.ai/install.sh | bash`.
2. Check it's logged in, without an API key, since builds should run on the user's
   Claude subscription:

   ```bash
   env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN claude -p "Reply with just OK"
   ```

3. If that fails with a login error, ask the user to open a terminal on this machine, as
   the same user, run `claude`, type `/login` and follow the link. Wait until they say
   they're done, then run the check again.

If you are Claude Code running on this machine as this user, it is already installed
and logged in; just run the check.

**Gemini** (only if the user wants it):

1. Install Google Antigravity: `curl -fsSL https://antigravity.google/cli/install.sh | bash`.
2. Ask the user to run `agy` once in their own terminal and sign in with Google.
3. Check it: `agy -p "Reply with just OK"`.

**Statues from pictures** (optional, for `!statue <image link>`): ask the user for a
free Hugging Face access token (huggingface.co, Settings, Access Tokens, read access),
and put it in a `.env` file in the project folder as `HF_TOKEN=<token>`
(see `.env.example`). Never print or commit it. Without it, `!statue` still builds
`.glb` meshes.

## 6. Test without the server

This designs a small build, renders it and turns it into blocks, without touching the
server. It takes a few minutes and uses a little of the user's Claude plan.

```bash
MCMCP_SERVER_DIR=/path/to/server venv/bin/python -m mcmcp.main --once "a small stone watchtower" --ai claude
```

It should end with `Folder: .../work/offline-...`, a folder holding `design.scad`,
`blocks.json` and `preview.png`. Show the user `preview.png` if you can.

## 7. Choose how to run it

Ask the user:

- **As a service** (recommended on Linux): runs in the background and starts at boot.
  Go to step 8.
- **By hand:** they start it when they want with
  `MCMCP_SERVER_DIR=/path/to/server venv/bin/python -m mcmcp.main`. Skip to step 9.

## 8. Install the service (Linux with systemd)

Run this from `~/mcmcp` in the same shell where the `claude` check passed, so its `PATH`
is saved into the service. Put in the real server folder:

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
```

Check it started: `journalctl -u mcmcp -n 30 --no-pager` should show
`Starting mcmcp service, tailing .../logs/latest.log`. If it shows an error, fix it and
run `sudo systemctl restart mcmcp`.

If the Minecraft server runs as its own systemd service (look for it with
`systemctl list-units --type=service | grep -iE 'minecraft|mc'`), ask the user whether
the builder should start and stop with it. If yes, add under `[Unit]`:
`BindsTo=<that>.service` and `After=<that>.service`, change `WantedBy=` to
`<that>.service`, then run `sudo systemctl daemon-reload` and
`sudo systemctl reenable mcmcp`.

## 9. Hand over to the user

Tell the user:

- In game, type `!designhelp` to see the commands, then try
  `!design a small stone watchtower`. It shows a preview; `!place` builds it.
- Only ops can use it, and every `!design` uses their Claude plan.
- Logs: `journalctl -u mcmcp -f` (service), or the terminal (by hand).
- Settings are in `mcmcp/config.py`. Restart the builder after changing them.
- To remove it: `sudo systemctl disable --now mcmcp`, delete
  `/etc/systemd/system/mcmcp.service` and `~/mcmcp`. RCON can be switched off again in
  `server.properties`.
