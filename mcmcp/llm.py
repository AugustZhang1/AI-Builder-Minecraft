"""Ask Claude (claude CLI on the user's subscription) for an OpenSCAD design."""
from __future__ import annotations

import base64
import json
import logging
import math
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


logger = logging.getLogger("mcmcp")

_CLI_WORKDIR = Path(tempfile.gettempdir()) / "mcmcp-cli"

PART_RE = re.compile(r"^[a-z0-9_]{1,32}$")
BLOCK_ID_RE = re.compile(r"^minecraft:[a-z0-9_]+$")
DETAIL_RE = re.compile(r"^minecraft:[a-z0-9_]+(\[[a-z0-9_]+=[a-z0-9_]+(,[a-z0-9_]+=[a-z0-9_]+)*\])?$")
FENCE_RE = re.compile(r"```([a-zA-Z0-9_-]*)[^\S\r\n]*\r?\n([\s\S]*?)```")


def _limits_line(limits: tuple[int, int, int] | int) -> str:
    """The limits sentence of the user text; an int means the same limit on all three axes."""
    w, d, t = (limits, limits, limits) if isinstance(limits, int) else limits
    return (
        f"Limits: up to {w} blocks wide (X), {d} deep (Y) and {t} tall (Z). "
        "Build at the size the description asks for; if it gives none, pick a size that suits the subject."
    )


def _prepare_prompt_text() -> str:
    """Reads mcmcp/prompt.md."""
    template_path = Path(__file__).resolve().parent / "prompt.md"
    return template_path.read_text(encoding="utf-8")


def _prepare_prompt_file(work_dir: Path | None = None) -> Path:
    """Reads mcmcp/prompt.md and writes to the temp work dir."""
    target_dir = work_dir or _CLI_WORKDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    content = _prepare_prompt_text()
    prompt_file = target_dir / "prompt.md"
    prompt_file.write_text(content, encoding="utf-8")
    return prompt_file


def _cli_command(prompt_file: Path | str | None = None) -> list[str]:
    """Builds the argv for invoking the claude CLI."""
    if prompt_file is None:
        p_file = _prepare_prompt_file()
    else:
        p_file = Path(prompt_file)

    exe = shutil.which("claude")
    if exe is None:
        raise LlmError("`claude` CLI executable not found on PATH")

    cmd = [
        exe, "-p",
        "--model", config.CLAUDE_MODEL,
        "--effort", config.CLAUDE_EFFORT,
        "--system-prompt-file", str(p_file),
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


GEMINI_RULES = (
    "Do not use any tools, except to view the reference image file named below if there is one. "
    "Do not create or edit files. Reply only with the two fenced blocks described above."
)


def _build_gemini_user_text(
    description: str,
    limits: tuple[int, int, int] | int,
    image_png: bytes | None = None,
    ref_path: Path | None = None,
) -> str:
    desc = description.strip() if description else ""
    limits_line = _limits_line(limits)
    if image_png and not desc:
        user_text = f"Build what is shown in the image.\n{limits_line}"
    else:
        user_text = f"Design: {desc}\n{limits_line}"

    if image_png:
        target_path = (ref_path or (_CLI_WORKDIR / "ref.png")).resolve()
        user_text += f"\nThe reference image is the file {target_path}. Look at it first."
    return user_text


def _build_gemini_prompt(
    description: str,
    limits: tuple[int, int, int] | int,
    image_png: bytes | None = None,
    ref_file: Path | None = None,
) -> str:
    system_prompt = _prepare_prompt_text().rstrip()
    ref_path = (ref_file or (_CLI_WORKDIR / "ref.png")).resolve()
    user_text = _build_gemini_user_text(description, limits, image_png, ref_path=ref_path)
    return f"{system_prompt}\n\n{GEMINI_RULES}\n\n{user_text}"


def _agy_command(prompt_text: str) -> list[str]:
    """Builds the argv for invoking the agy CLI."""
    exe = shutil.which("agy")
    if exe is None:
        raise LlmError("`agy` CLI not found on PATH")

    cmd = [
        exe, "-p", prompt_text,
        "--model", config.GEMINI_MODEL,
        "--output-format", "stream-json",
        "--sandbox",
    ]
    return cmd


def _build_stdin_payload(
    description: str,
    limits: tuple[int, int, int] | int,
    image_png: bytes | None = None,
) -> str:
    """Formats the single stream-json line for stdin."""
    desc = description.strip() if description else ""
    limits_line = _limits_line(limits)
    if image_png and not desc:
        user_text = f"Build what is shown in the image.\n{limits_line}"
    else:
        user_text = f"Design: {desc}\n{limits_line}"

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


def _agy_delta(event: dict) -> str | None:
    """Extracts text delta from an agy stream-json event or raises LlmError on error results."""
    if not isinstance(event, dict):
        return None

    evt_type = event.get("event")

    if evt_type == "result":
        res = event.get("result")
        if isinstance(res, dict):
            status = res.get("status")
            if status != "SUCCESS":
                err = res.get("error") or status or "Unknown error"
                raise LlmError(f"gemini: {err}")
        return None

    if evt_type == "step_update":
        step = event.get("step_update")
        if isinstance(step, dict) and step.get("step_type") == "agent_response":
            delta = step.get("text_delta")
            if isinstance(delta, str):
                return delta

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
    limits: tuple[int, int, int] | int,
    image_png: bytes | None = None,
    timeout: float = config.CLAUDE_TIMEOUT,
    provider: str = "claude",
) -> Iterator[str]:
    """Runs the Claude or Gemini CLI and yields text chunks synchronously."""
    if provider not in config.PROVIDERS:
        raise LlmError(f"Unknown provider: {provider!r}. Must be one of {config.PROVIDERS}")

    _CLI_WORKDIR.mkdir(parents=True, exist_ok=True)

    if provider == "claude":
        prompt_file = _prepare_prompt_file(_CLI_WORKDIR)
        cmd = _cli_command(prompt_file)
        stdin_payload: str | None = _build_stdin_payload(description, limits, image_png)
        stdin_mode = subprocess.PIPE
        delta_fn = _cli_delta
        cli_name = "claude"
    elif provider == "gemini":
        ref_file = _CLI_WORKDIR / "ref.png"
        ref_file.unlink(missing_ok=True)
        if image_png:
            ref_file.write_bytes(image_png)
        prompt_text = _build_gemini_prompt(description, limits, image_png, ref_file=ref_file)
        cmd = _agy_command(prompt_text)
        stdin_payload = None
        stdin_mode = subprocess.DEVNULL
        delta_fn = _agy_delta
        cli_name = "gemini"
    else:
        raise LlmError(f"Unknown provider: {provider!r}. Must be one of {config.PROVIDERS}")

    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)

    popen_kwargs: dict[str, Any] = {
        "cwd": _CLI_WORKDIR,
        "env": env,
        "stdin": stdin_mode,
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

    feeder = None
    if stdin_payload is not None:
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
                    chunk = delta_fn(event)
                    if chunk:
                        yield chunk

        proc.wait()
    finally:
        timer.cancel()
        if feeder is not None:
            feeder.join(timeout=2.0)
        _kill_process_tree(proc)

    if timed_out:
        display_name = "Claude" if provider == "claude" else "Gemini"
        raise LlmError(f"{display_name} CLI timed out after {timeout}s")

    if proc.returncode != 0:
        noise = f": {last_noise[-300:]}" if last_noise else ""
        raise LlmError(f"{cli_name} exited with code {proc.returncode}{noise}")


def is_banned(block_id: str) -> bool:
    """True if base block id (before '[') is in config.BANNED_BLOCKS or ends with '_concrete_powder'."""
    if not isinstance(block_id, str):
        return True
    base_id = block_id.split("[", 1)[0].strip()
    return base_id in config.BANNED_BLOCKS or base_id.endswith("_concrete_powder")


def parse_mix(raw: Any) -> dict[str, dict[str, float]]:
    """Parse and validate texture mixes. An invalid mix is dropped and logged."""
    if not isinstance(raw, dict):
        logger.warning("Invalid mix: expected object/dict, got %s", type(raw).__name__)
        return {}

    result: dict[str, dict[str, float]] = {}
    for base_id, variants in raw.items():
        if not isinstance(base_id, str) or not BLOCK_ID_RE.match(base_id):
            logger.warning("Dropping mix: invalid base block id %r", base_id)
            continue
        if is_banned(base_id):
            logger.warning("Dropping mix: banned base block id %r", base_id)
            continue
        if not isinstance(variants, dict):
            logger.warning("Dropping mix for %r: variants must be a dict, got %s", base_id, type(variants).__name__)
            continue
        if not (1 <= len(variants) <= 6):
            logger.warning("Dropping mix for %r: expected 1-6 variants, got %d", base_id, len(variants))
            continue

        valid = True
        parsed_variants: dict[str, float] = {}
        for var_id, weight in variants.items():
            if not isinstance(var_id, str) or not BLOCK_ID_RE.match(var_id):
                logger.warning("Dropping mix for %r: invalid variant id %r", base_id, var_id)
                valid = False
                break
            if is_banned(var_id):
                logger.warning("Dropping mix for %r: banned variant id %r", base_id, var_id)
                valid = False
                break
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight <= 0 or not math.isfinite(weight):
                logger.warning("Dropping mix for %r: invalid weight %r for variant %r", base_id, weight, var_id)
                valid = False
                break
            parsed_variants[var_id] = float(weight)

        if valid:
            result[base_id] = parsed_variants

    return result


def parse_details(raw: Any) -> list[tuple[int, int, int, int, int, int, str]]:
    """Parse and validate detail blocks/boxes. Invalid entries are dropped and logged."""
    if not isinstance(raw, list):
        logger.warning("Invalid details: expected list, got %s", type(raw).__name__)
        return []

    result: list[tuple[int, int, int, int, int, int, str]] = []
    for entry in raw:
        if not isinstance(entry, (list, tuple)):
            logger.warning("Dropping detail entry: expected list/tuple, got %s", type(entry).__name__)
            continue

        if len(entry) == 4:
            x, y, z, block = entry
            x1 = x2 = x
            y1 = y2 = y
            z1 = z2 = z
        elif len(entry) == 7:
            x1, y1, z1, x2, y2, z2, block = entry
        else:
            logger.warning("Dropping detail entry: expected 4 or 7 elements, got %d", len(entry))
            continue

        coords = (x1, y1, z1, x2, y2, z2)
        if any(isinstance(c, bool) or not isinstance(c, int) for c in coords):
            logger.warning("Dropping detail entry: coordinates must be ints (no bools): %r", coords)
            continue

        if z1 < 0 or z2 < 0:
            logger.warning("Dropping detail entry: z coordinates must be >= 0: z1=%r, z2=%r", z1, z2)
            continue

        if not isinstance(block, str):
            logger.warning("Dropping detail entry: block must be a string, got %s", type(block).__name__)
            continue

        clean_block = re.sub(r"\s+", "", block)
        if not DETAIL_RE.match(clean_block):
            logger.warning("Dropping detail entry: block %r does not match DETAIL_RE", clean_block)
            continue

        if is_banned(clean_block):
            logger.warning("Dropping detail entry: banned block %r", clean_block)
            continue

        sx1, sx2 = min(x1, x2), max(x1, x2)
        sy1, sy2 = min(y1, y2), max(y1, y2)
        sz1, sz2 = min(z1, z2), max(z1, z2)

        volume = (sx2 - sx1 + 1) * (sy2 - sy1 + 1) * (sz2 - sz1 + 1)
        if volume > 64:
            logger.warning("Dropping detail entry: box volume %d exceeds 64 blocks", volume)
            continue

        result.append((sx1, sy1, sz1, sx2, sy2, sz2, clean_block))

    return result


def parse_reply(
    raw_response: str,
) -> tuple[str, dict[str, str], dict[str, dict[str, float]], list[tuple[int, int, int, int, int, int, str]]]:
    """Parses and validates the json and openscad code blocks from the LLM reply.

    Returns (scad_text, blocks_dict, mix_dict, details_list).
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
    if not (1 <= len(raw_blocks) <= 16):
        raise LlmError(f"Expected 1-16 parts, got {len(raw_blocks)}")

    blocks: dict[str, str] = {}
    for part, block_id in raw_blocks.items():
        if not isinstance(part, str) or not PART_RE.match(part):
            raise LlmError(f"Invalid part name {part!r}: must match ^[a-z0-9_]{{1,32}}$")
        if not isinstance(block_id, str) or not BLOCK_ID_RE.match(block_id):
            raise LlmError(
                f"Invalid block id {block_id!r} for part {part!r}: must match ^minecraft:[a-z0-9_]+$"
            )
        blocks[part] = block_id

    mix = parse_mix(data.get("mix", {}))
    details = parse_details(data.get("details", []))

    scad_text = scad_blocks[0].strip()
    if not scad_text:
        raise LlmError("OpenSCAD code block is empty")

    for part in blocks:
        if part not in scad_text:
            raise LlmError(f"Part {part!r} does not appear in OpenSCAD code")

    return scad_text, blocks, mix, details


def design(
    description: str,
    limits: tuple[int, int, int] | int,
    image_png: bytes | None = None,
    on_text: Callable[[str], None] | None = None,
    provider: str = "claude",
) -> tuple[str, dict[str, str], dict[str, dict[str, float]], list[tuple[int, int, int, int, int, int, str]], str]:
    """Returns (scad_text, blocks, mix, details, raw_response).
    blocks maps part name -> block id, in precedence order (later parts override earlier ones)."""
    if provider not in config.PROVIDERS:
        raise LlmError(f"Unknown provider: {provider!r}. Must be one of {config.PROVIDERS}")

    try:
        chunks: list[str] = []
        for chunk in _cli_text(
            description,
            limits,
            image_png=image_png,
            timeout=config.CLAUDE_TIMEOUT,
            provider=provider,
        ):
            chunks.append(chunk)
            if on_text:
                on_text(chunk)
        raw_response = "".join(chunks)
        scad_text, blocks, mix, details = parse_reply(raw_response)
        return scad_text, blocks, mix, details, raw_response
    except LlmError:
        raise
    except Exception as exc:
        raise LlmError(f"Design failed: {exc}") from exc
