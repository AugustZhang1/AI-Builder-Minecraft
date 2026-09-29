"""Ask Claude (claude CLI on the user's subscription) for an OpenSCAD design."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable

try:
    from . import config
except ImportError:
    from mcmcp import config


class LlmError(Exception):
    pass


_CLI_WORKDIR = Path(tempfile.gettempdir()) / "mcmcp-cli"

PART_RE = re.compile(r"^[a-z0-9_]{1,32}$")
BLOCK_ID_RE = re.compile(r"^minecraft:[a-z0-9_]+$")
FENCE_RE = re.compile(r"```([a-zA-Z0-9_-]*)[^\S\r\n]*\r?\n([\s\S]*?)```")


def _prepare_prompt_file(max_size: int, work_dir: Path | None = None) -> Path:
    """Reads mcmcp/prompt.md, substitutes {max_size}, and writes to the temp work dir."""
    target_dir = work_dir or _CLI_WORKDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    template_path = Path(__file__).resolve().parent / "prompt.md"
    content = template_path.read_text(encoding="utf-8")
    content = content.replace("{max_size}", str(max_size))
    prompt_file = target_dir / "prompt.md"
    prompt_file.write_text(content, encoding="utf-8")
    return prompt_file


def _cli_command(prompt_file_or_max_size: Path | str | int | None = None) -> list[str]:
    """Builds the argv for invoking the claude CLI."""
    if prompt_file_or_max_size is None:
        prompt_file = _prepare_prompt_file(config.MAX_SIZE)
    elif isinstance(prompt_file_or_max_size, int):
        prompt_file = _prepare_prompt_file(prompt_file_or_max_size)
    else:
        prompt_file = Path(prompt_file_or_max_size)

    exe = shutil.which("claude")
    if exe is None:
        raise LlmError("`claude` CLI executable not found on PATH")

    cmd = [
        exe, "-p",
        "--model", config.CLAUDE_MODEL,
        "--effort", config.CLAUDE_EFFORT,
        "--system-prompt-file", str(prompt_file),
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--no-session-persistence",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
    ]
    return cmd


def _build_stdin_payload(description: str, max_size: int, image_png: bytes | None = None) -> str:
    """Formats the single stream-json line for stdin."""
    desc = description.strip() if description else ""
    if image_png and not desc:
        user_text = f"Build what is shown in the image.\nMaximum size: {max_size} blocks per axis."
    else:
        user_text = f"Design: {desc}\nMaximum size: {max_size} blocks per axis."

    content: list[dict[str, Any]] = []
    if image_png:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(image_png).decode("ascii"),
            },
        })
    content.append({
        "type": "text",
        "text": user_text,
    })

    msg = {
        "type": "user",
        "message": {
            "role": "user",
            "content": content,
        },
    }
    return json.dumps(msg) + "\n"


def _cli_delta(event: dict) -> str | None:
    """Extracts text delta from a stream-json event or raises LlmError on error events."""
    if not isinstance(event, dict):
        return None

    if event.get("type") == "result" and event.get("is_error"):
        err = event.get("result") or event.get("error") or "Unknown error"
        raise LlmError(f"claude: {err}")

    if event.get("type") == "stream_event":
        inner = event.get("event")
        delta = inner.get("delta") if isinstance(inner, dict) else None
        if not delta and isinstance(event.get("delta"), dict):
            delta = event.get("delta")
        if isinstance(delta, dict) and delta.get("type") == "text_delta":
            return delta.get("text")

    return None


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kills the process and any child processes."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _cli_text(
    description: str,
    max_size: int,
    image_png: bytes | None = None,
    timeout: float = config.CLAUDE_TIMEOUT,
) -> Iterator[str]:
    """Runs the Claude CLI and yields text chunks synchronously."""
    _CLI_WORKDIR.mkdir(parents=True, exist_ok=True)
    prompt_file = _prepare_prompt_file(max_size, _CLI_WORKDIR)
    cmd = _cli_command(prompt_file)
    stdin_payload = _build_stdin_payload(description, max_size, image_png)

    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)

    popen_kwargs: dict[str, Any] = {
        "cwd": _CLI_WORKDIR,
        "env": env,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
    }
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)

    timed_out = False

    def on_timeout():
        nonlocal timed_out
        timed_out = True
        _kill_process_tree(proc)

    timer = threading.Timer(timeout, on_timeout)
    timer.start()

    def feed_stdin():
        try:
            if proc.stdin:
                proc.stdin.write(stdin_payload.encode("utf-8"))
                proc.stdin.flush()
        except OSError:
            pass
        finally:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass

    feeder = threading.Thread(target=feed_stdin, daemon=True)
    feeder.start()

    last_noise = ""
    try:
        if proc.stdout:
            for raw_line in iter(proc.stdout.readline, b""):
                line_str = raw_line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue
                try:
                    event = json.loads(line_str)
                except json.JSONDecodeError:
                    last_noise = line_str or last_noise
                    continue

                if isinstance(event, dict):
                    chunk = _cli_delta(event)
                    if chunk:
                        yield chunk

        proc.wait()
    finally:
        timer.cancel()
        feeder.join(timeout=2.0)
        _kill_process_tree(proc)

    if timed_out:
        raise LlmError(f"Claude CLI timed out after {timeout}s")

    if proc.returncode != 0:
        noise = f": {last_noise[-300:]}" if last_noise else ""
        raise LlmError(f"claude exited with code {proc.returncode}{noise}")


def parse_reply(raw_response: str) -> tuple[str, dict[str, str]]:
    """Parses and validates the json and openscad code blocks from the LLM reply.

    Returns (scad_text, blocks_dict).
    """
    matches = FENCE_RE.findall(raw_response)

    json_blocks = [c for lang, c in matches if lang.lower() == "json"]
    scad_blocks = [c for lang, c in matches if lang.lower() in ("openscad", "scad")]

    if len(json_blocks) == 0:
        raise LlmError("Missing ```json fenced block in response")
    if len(json_blocks) > 1:
        raise LlmError(f"Expected exactly one ```json block, found {len(json_blocks)}")

    if len(scad_blocks) == 0:
        raise LlmError("Missing ```openscad (or ```scad) fenced block in response")
    if len(scad_blocks) > 1:
        raise LlmError(f"Expected exactly one ```openscad block, found {len(scad_blocks)}")

    if len(matches) != 2:
        raise LlmError(f"Expected exactly two fenced blocks (json and openscad), found {len(matches)}")

    try:
        data = json.loads(json_blocks[0].strip())
    except json.JSONDecodeError as exc:
        raise LlmError(f"Invalid JSON in ```json block: {exc}") from exc

    if not isinstance(data, dict):
        raise LlmError("JSON content must be an object")

    if "blocks" not in data or not isinstance(data["blocks"], dict):
        raise LlmError("JSON must contain a 'blocks' object")

    raw_blocks = data["blocks"]
    if not (1 <= len(raw_blocks) <= 12):
        raise LlmError(f"Expected 1-12 parts, got {len(raw_blocks)}")

    blocks: dict[str, str] = {}
    for part, block_id in raw_blocks.items():
        if not isinstance(part, str) or not PART_RE.match(part):
            raise LlmError(f"Invalid part name {part!r}: must match ^[a-z0-9_]{{1,32}}$")
        if not isinstance(block_id, str) or not BLOCK_ID_RE.match(block_id):
            raise LlmError(
                f"Invalid block id {block_id!r} for part {part!r}: must match ^minecraft:[a-z0-9_]+$"
            )
        blocks[part] = block_id

    scad_text = scad_blocks[0].strip()
    if not scad_text:
        raise LlmError("OpenSCAD code block is empty")

    for part in blocks:
        if part not in scad_text:
            raise LlmError(f"Part {part!r} does not appear in OpenSCAD code")

    return scad_text, blocks


def design(
    description: str,
    max_size: int,
    image_png: bytes | None = None,
    on_text: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, str], str]:
    """Returns (scad_text, blocks, raw_response).
    blocks maps part name -> block id, in precedence order (later parts override earlier ones)."""
    try:
        chunks: list[str] = []
        for chunk in _cli_text(description, max_size, image_png=image_png, timeout=config.CLAUDE_TIMEOUT):
            chunks.append(chunk)
            if on_text:
                on_text(chunk)
        raw_response = "".join(chunks)
        scad_text, blocks = parse_reply(raw_response)
        return scad_text, blocks, raw_response
    except LlmError:
        raise
    except Exception as exc:
        raise LlmError(f"Design failed: {exc}") from exc
