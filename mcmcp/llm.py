"""Ask Claude (claude CLI on the user's subscription) for an OpenSCAD design."""
from __future__ import annotations

import base64
import io
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

from PIL import Image

try:
    from . import config, figure
except ImportError:
    from mcmcp import config, figure


class LlmError(Exception):
    pass


class LlmStopped(LlmError):
    """The AI call was stopped with !designstop."""


logger = logging.getLogger("mcmcp")

_CLI_WORKDIR = Path(tempfile.gettempdir()) / "mcmcp-cli"

PART_RE = re.compile(r"^[a-z0-9_]{1,32}$")
BLOCK_ID_RE = re.compile(r"^minecraft:[a-z0-9_]+$")
DETAIL_RE = re.compile(r"^minecraft:[a-z0-9_]+(\[[a-z0-9_]+=[a-z0-9_]+(,[a-z0-9_]+=[a-z0-9_]+)*\])?$")
FENCE_RE = re.compile(r"```([a-zA-Z0-9_-]*)[^\S\r\n]*\r?\n([\s\S]*?)```")
KEEP_RE = re.compile(r"\bKEEP\b")  # the exact word the review asks for
PICK_RE = re.compile(r"\b(?i:pick):?\s+\**([AB])\b")  # "PICK A" or "PICK B" (the letter in capitals)


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
    "Do not use any tools, except to read the files named below. "
    "Do not create or edit files. Reply only with the two fenced blocks described above, "
    "or with KEEP when asked to review, or with PICK A or PICK B (then the corrected design, "
    "if it needs one) when asked to pick."
)

REVIEW_TEXT = (
    "Your design was built. The image is a preview of the result as blocks: "
    "two angled views, then front, side and top views (the player sees the front). "
    "Check it against the request (and the reference image, if any): "
    "1. Does it look like the subject: silhouette, proportions, signature features? "
    "2. Anything broken: a part missing, floating pieces, parts poking through each other, "
    "features merged into blobs, faces or windows that don't read, accidental faces? "
    "3. Large flat single-colour areas, or neighbouring parts in the same tone? "
    "If nothing is clearly wrong, reply with exactly KEEP. "
    "Otherwise fix the 1-3 biggest problems and reply with the complete corrected design "
    "in the same two fenced blocks (same rules and limits), keeping everything that already works."
)

FIGURE_REVIEW_TEXT = (
    "Your design was built. The image shows a preview of the result as blocks: two angled views, then front, side and "
    "top views (the player sees the front), and on the right a close-up of the head from the front. "
    "The builder has already applied your \"pose\" and stamped your \"face\". "
    "Compare the front view and the close-up with the reference image (or the request, if there is none), point by point: "
    "1. Body: does it turn and lean the way the image shows? "
    "2. Head: does it turn and tilt the way the image shows? "
    "3. Face (the close-up): are the eyes, brows and mouth on the face where the image has them, "
    "not on the hair or off its edge, readable, with the right expression? "
    "When a zoomed reference face is shown, compare the close-up with it: eyelids, gaze, brows, mouth corners and markings. "
    "4. Arms, hands, legs and held things: where the image has them? "
    "5. Silhouette, proportions and signature features: anything broken, floating or merged into blobs? "
    "Then check the component inventory at the top of your code, item by item: is every listed component built and showing? "
    "First write one short line per point: what the image shows, and what the build shows. "
    "Then, if all five match and nothing from the inventory is missing, end with exactly KEEP. Otherwise fix the 1-3 "
    "biggest problems (a missing component, a wrong turn, lean or tilt in \"pose\", the face in \"face\") and reply "
    "with the complete corrected design in the same two fenced blocks (same rules and limits), keeping everything "
    "that already works."
)

FIX_TEXT = (
    "Your design was built. The image is a preview of the result as blocks: "
    "two angled views, then front, side and top views (the player sees the front). "
    "The player wants these changes: {feedback}\n"
    "Make them and reply with the complete changed design in the same two fenced blocks "
    "(same rules and limits), keeping everything else as it is."
)


def _review_text(feedback: str | None, raw: str = "") -> str:
    """REVIEW_TEXT (or FIGURE_REVIEW_TEXT for a figure), or FIX_TEXT when the player asked for changes."""
    if feedback is not None and feedback.strip():
        return FIX_TEXT.format(feedback=feedback.strip())
    pose, face = figure.features(raw)
    if pose is not None or face is not None:
        return FIGURE_REVIEW_TEXT
    return REVIEW_TEXT


PICK_TEXT = (
    "Two designs were built from this request: design A and design B. "
    "The images are previews of the results as blocks, A first and then B: "
    "each has two angled views, then front, side and top views (the player sees the front). "
    "Pick the one that best matches the request (and the reference image, if any): "
    "the subject's silhouette, proportions and signature features, nothing broken or floating, "
    "no large flat single-colour areas. "
    "Reply with exactly PICK A or PICK B on the first line. "
    "If the design you picked has clear problems, follow with the complete corrected design "
    "in the same two fenced blocks (same rules and limits), keeping everything that already works. "
    "Otherwise reply with only the PICK line."
)

COLOUR_CHECK_TEXT = """You check the colours of a Minecraft statue made from a picture.
A 3D model was generated from the reference picture and turned into blocks, each block the nearest colour of the
model's texture. The texture is often darker, duller or tinted, so whole areas can end up in the wrong block.
Compare the statue with the reference, area by area (hair, skin, clothes, legs, shoes, accessories), and replace
each block that gives an area the wrong colour or hue with a block that matches the reference.
First write one short line per area: the colour in the reference, the colour on the statue, and the blocks there.
Then end with a JSON object mapping block ids to replacement block ids, e.g.
{"minecraft:nether_bricks": "minecraft:gray_concrete"}. Replacements must come from the allowed list. Replace a
block when its area's colour or hue clearly differs from the reference (too dark, too dull, wrong tint all count);
a swap changes every block of that id, so check the block isn't also used in an area that is right. End with {} if
the colours are right."""

STATUE_REVIEW_TEXT = """You check the shape of a Minecraft statue made from a 3D model of the reference picture.
You get the reference and the statue as blocks (front as in the picture, side, back).
Compare the shape with the picture: missing or extra parts (limbs, hands, hair, accessories), stumps or lumps that
are not in the picture, wrong lengths (hair, cloak, sleeves), wrong pose. Ignore colours (checked separately),
blockiness, and parts the picture does not show.
Reply with up to 5 lines, each starting with "- ", most important first, each one short and concrete (what and where,
e.g. "- The right hand is missing."). If the shape matches, reply only "- Looks right."."""


def _build_gemini_user_text(
    description: str,
    limits: tuple[int, int, int] | int,
    image_png: bytes | None = None,
    review: tuple[str, bytes] | None = None,
    pick: tuple[tuple[str, bytes], tuple[str, bytes]] | None = None,
    work_dir: Path | None = None,
    feedback: str | None = None,
) -> str:
    work = (work_dir or _CLI_WORKDIR).resolve()
    desc = description.strip() if description else ""
    limits_line = _limits_line(limits)
    if image_png and not desc:
        user_text = f"Build what is shown in the image.\n{limits_line}"
    else:
        user_text = f"Design: {desc}\n{limits_line}"

    if image_png:
        user_text += f"\nThe reference image is the file {work / 'ref.png'}. Look at it first."

    # Designs go in files: on the command line they can pass Windows' length limit.
    if review:
        user_text += (
            f"\n\nYour design is the file {work / 'design.txt'}."
            f"\nThe preview image is the file {work / 'preview.png'}. Look at both first."
            f"\n\n{_review_text(feedback, review[0])}"
        )
    if pick:
        user_text += (
            f"\n\nDesign A is the file {work / 'design_a.txt'}, its preview image is the file {work / 'preview_a.png'}."
            f"\nDesign B is the file {work / 'design_b.txt'}, its preview image is the file {work / 'preview_b.png'}."
            f"\nLook at all four first."
            f"\n\n{PICK_TEXT}"
        )
    return user_text


def _build_gemini_prompt(
    description: str,
    limits: tuple[int, int, int] | int,
    image_png: bytes | None = None,
    review: tuple[str, bytes] | None = None,
    pick: tuple[tuple[str, bytes], tuple[str, bytes]] | None = None,
    work_dir: Path | None = None,
    feedback: str | None = None,
) -> str:
    """Builds the prompt for Gemini. The instructions go in instructions.md in work_dir
    because on the command line they pass Windows' limit."""
    work = (work_dir or _CLI_WORKDIR).resolve()
    work.mkdir(parents=True, exist_ok=True)
    (work / "instructions.md").write_text(_prepare_prompt_text().rstrip(), encoding="utf-8")
    user_text = _build_gemini_user_text(
        description, limits, image_png, review=review, pick=pick, work_dir=work_dir, feedback=feedback
    )
    return (
        f"Your instructions are the file {work / 'instructions.md'}. Read all of it first and follow it exactly."
        f"\n\n{GEMINI_RULES}\n\n{user_text}"
    )


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


def _image_part(png: bytes) -> dict[str, Any]:
    """An image block of the stream-json message."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(png).decode("ascii"),
        },
    }


def _build_stdin_payload(
    description: str,
    limits: tuple[int, int, int] | int,
    image_png: bytes | None = None,
    review: tuple[str, bytes] | None = None,
    pick: tuple[tuple[str, bytes], tuple[str, bytes]] | None = None,
    feedback: str | None = None,
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
        content.append(_image_part(image_png))
    content.append({
        "type": "text",
        "text": user_text,
    })

    if review:
        raw, preview_png = review
        content.append({
            "type": "text",
            "text": f"Your design:\n{raw}",
        })
        content.append(_image_part(preview_png))
        content.append({
            "type": "text",
            "text": _review_text(feedback, raw),
        })

    if pick:
        for label, (raw, _) in zip("AB", pick):
            content.append({"type": "text", "text": f"Design {label}:\n{raw}"})
        for label, (_, preview_png) in zip("AB", pick):
            content.append({"type": "text", "text": f"Preview of design {label}:"})
            content.append(_image_part(preview_png))
        content.append({"type": "text", "text": PICK_TEXT})

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


def _run_cli(
    cmd: list[str],
    stdin_payload: str | None,
    work: Path,
    timeout: float,
    provider: str = "claude",
    stop: threading.Event | None = None,
) -> Iterator[str]:
    """Runs a CLI command (Claude or Gemini) and yields text chunks as they arrive.
    Handles process lifecycle, timeouts (raises LlmError), stop events (raises LlmStopped),
    and error exit codes (raises LlmError)."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)

    stdin_mode = subprocess.PIPE if stdin_payload is not None else subprocess.DEVNULL
    delta_fn = _cli_delta if provider == "claude" else _agy_delta
    cli_name = provider
    display_name = "Claude" if provider == "claude" else "Gemini"

    popen_kwargs: dict[str, Any] = {
        "cwd": work,
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

    stopped = False
    if stop is not None:
        def watch_stop():
            nonlocal stopped
            while proc.poll() is None:
                if stop.wait(0.5):
                    stopped = True
                    _kill_process_tree(proc)
                    return

        threading.Thread(target=watch_stop, daemon=True).start()

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

    if stopped:
        raise LlmStopped(f"{display_name} stopped")

    if timed_out:
        raise LlmError(f"{display_name} CLI timed out after {timeout}s")

    if proc.returncode != 0:
        noise = f": {last_noise[-300:]}" if last_noise else ""
        raise LlmError(f"{cli_name} exited with code {proc.returncode}{noise}")


def _cli_text(
    description: str,
    limits: tuple[int, int, int] | int,
    image_png: bytes | None = None,
    timeout: float = config.CLAUDE_TIMEOUT,
    provider: str = "claude",
    review: tuple[str, bytes] | None = None,
    work_dir: Path | None = None,
    pick: tuple[tuple[str, bytes], tuple[str, bytes]] | None = None,
    stop: threading.Event | None = None,
    feedback: str | None = None,
) -> Iterator[str]:
    """Runs the Claude or Gemini CLI and yields text chunks synchronously.
    work_dir is the folder for its files (default _CLI_WORKDIR); calls that run at the same
    time need different ones. If stop gets set, the CLI is killed and LlmStopped is raised."""
    if provider not in config.PROVIDERS:
        raise LlmError(f"Unknown provider: {provider!r}. Must be one of {config.PROVIDERS}")

    work = work_dir or _CLI_WORKDIR
    work.mkdir(parents=True, exist_ok=True)

    if provider == "claude":
        prompt_file = _prepare_prompt_file(work)
        cmd = _cli_command(prompt_file)
        stdin_payload: str | None = _build_stdin_payload(
            description, limits, image_png=image_png, review=review, pick=pick, feedback=feedback
        )
    elif provider == "gemini":
        ref_file = work / "ref.png"
        ref_file.unlink(missing_ok=True)
        if image_png:
            ref_file.write_bytes(image_png)
        preview_file = work / "preview.png"
        preview_file.unlink(missing_ok=True)
        if review:
            (work / "design.txt").write_text(review[0], encoding="utf-8")
            preview_file.write_bytes(review[1])
        if pick:
            for label, (raw, preview_png) in zip("ab", pick):
                (work / f"design_{label}.txt").write_text(raw, encoding="utf-8")
                (work / f"preview_{label}.png").write_bytes(preview_png)
        prompt_text = _build_gemini_prompt(
            description, limits, image_png, review=review, pick=pick, work_dir=work, feedback=feedback
        )
        cmd = _agy_command(prompt_text)
        stdin_payload = None
    else:
        raise LlmError(f"Unknown provider: {provider!r}. Must be one of {config.PROVIDERS}")

    yield from _run_cli(cmd, stdin_payload, work, timeout, provider=provider, stop=stop)


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

    figure.apply_colors(data, blocks)

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
    work_dir: Path | None = None,
    stop: threading.Event | None = None,
) -> tuple[str, dict[str, str], dict[str, dict[str, float]], list[tuple[int, int, int, int, int, int, str]], str]:
    """Returns (scad_text, blocks, mix, details, raw_response).
    blocks maps part name -> block id, in precedence order (later parts override earlier ones).
    work_dir, stop: see _cli_text (default: the shared folder, no stop)."""
    if provider not in config.PROVIDERS:
        raise LlmError(f"Unknown provider: {provider!r}. Must be one of {config.PROVIDERS}")

    extra: dict[str, Any] = {"work_dir": work_dir} if work_dir is not None else {}
    try:
        chunks: list[str] = []
        for chunk in _cli_text(
            description,
            limits,
            image_png=image_png,
            timeout=config.CLAUDE_TIMEOUT,
            provider=provider,
            stop=stop,
            **extra,
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


def _thumbnail(png: bytes) -> bytes:
    """A preview image scaled down to config.IMAGE_MAX_SIDE, as PNG."""
    with Image.open(io.BytesIO(png)) as img:
        img = img.convert("RGB")
        img.thumbnail((config.IMAGE_MAX_SIDE, config.IMAGE_MAX_SIDE))
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()


def review(
    description: str,
    limits: tuple[int, int, int] | int,
    raw: str,
    preview_png: bytes,
    image_png: bytes | None = None,
    provider: str = "claude",
    on_text: Callable[[str], None] | None = None,
    work_dir: Path | None = None,
    stop: threading.Event | None = None,
    feedback: str | None = None,
) -> tuple[str, dict[str, str], dict[str, dict[str, float]], list[tuple[int, int, int, int, int, int, str]], str] | None:
    """Reviews the build preview and either returns None (KEEP) or a corrected design 5-tuple.
    With feedback the AI makes the player's changes instead; a KEEP reply still returns None."""
    if provider not in config.PROVIDERS:
        raise LlmError(f"Unknown provider: {provider!r}. Must be one of {config.PROVIDERS}")

    try:
        thumb_png = _thumbnail(preview_png)

        chunks: list[str] = []
        for chunk in _cli_text(
            description,
            limits,
            image_png=image_png,
            timeout=config.CLAUDE_TIMEOUT,
            provider=provider,
            review=(raw, thumb_png),
            work_dir=work_dir,
            stop=stop,
            feedback=feedback,
        ):
            chunks.append(chunk)
            if on_text:
                on_text(chunk)
        reply = "".join(chunks)
        stripped = reply.strip()
        if "```" not in stripped and KEEP_RE.search(stripped):
            return None
        return parse_reply(reply) + (reply,)
    except LlmError:
        raise
    except Exception as exc:
        raise LlmError(f"Review failed: {exc}") from exc


def pick(
    description: str,
    limits: tuple[int, int, int] | int,
    designs: list[tuple[str, bytes]],
    image_png: bytes | None = None,
    provider: str = "claude",
    work_dir: Path | None = None,
    stop: threading.Event | None = None,
) -> tuple[int, tuple[str, dict[str, str], dict[str, dict[str, float]], list[tuple[int, int, int, int, int, int, str]], str] | None]:
    """Picks the better of two built designs. designs is [(raw_a, preview_png_a), (raw_b, preview_png_b)].
    Returns (index, revised): index 0 (A) or 1 (B), revised None or the picked design's corrected
    design 5-tuple like review returns. A reply without PICK A / PICK B raises LlmError."""
    if provider not in config.PROVIDERS:
        raise LlmError(f"Unknown provider: {provider!r}. Must be one of {config.PROVIDERS}")

    try:
        thumbs = tuple((raw, _thumbnail(preview_png)) for raw, preview_png in designs)
        reply = "".join(_cli_text(
            description,
            limits,
            image_png=image_png,
            timeout=config.CLAUDE_TIMEOUT,
            provider=provider,
            work_dir=work_dir,
            pick=thumbs,
            stop=stop,
        ))
        m = PICK_RE.search(FENCE_RE.sub("", reply))  # not inside a design's code
        if m is None:
            raise LlmError("No PICK A or PICK B in the reply")
        index = "AB".index(m.group(1))
        if "```" not in reply:
            return index, None
        try:
            return index, parse_reply(reply) + (reply,)
        except LlmError as exc:
            logger.warning("Ignoring the corrected design of the pick: %s", exc)
            return index, None
    except LlmError:
        raise
    except Exception as exc:
        raise LlmError(f"Pick failed: {exc}") from exc


def parse_swaps(text: str) -> dict[str, str]:
    """Parse block swaps from LLM response.

    Finds the last {...} object without nested braces in the text and parses
    it as JSON. Only str -> str pairs are kept. Returns {} if none or invalid.
    """
    matches = re.findall(r"\{[^{}]*\}", text)
    if not matches:
        return {}
    try:
        data = json.loads(matches[-1])
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def parse_review(text: str) -> list[str]:
    """Parse shape review bullet points from LLM response.

    Takes lines starting with '- ' (after stripping whitespace), removes
    the '- ' and strips, drops empty items, keeps at most 5, and cuts
    each to 200 characters at a word (ending in "..."). Other text is ignored.
    """
    reviews: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        item = line[2:].strip()
        if not item:
            continue
        reviews.append(item if len(item) <= 200 else item[:197].rsplit(" ", 1)[0] + "...")
        if len(reviews) == 5:
            break
    return reviews


def colour_check(
    reference_png: bytes,
    statue_png: bytes,
    block_lines: list[str],
    allowed: list[str],
    stop: threading.Event | None = None,
    timeout: float = 300,
    work_dir: Path | None = None,
) -> dict[str, str]:
    """Checks the colours of a statue against reference image using Claude CLI.

    Writes COLOUR_CHECK_TEXT to prompt file in work_dir, sends reference image,
    statue views image, block lines and allowed replacement blocks to Claude.
    Returns parsed block swaps mapping old_id -> new_id.
    """
    work = work_dir or _CLI_WORKDIR
    work.mkdir(parents=True, exist_ok=True)

    prompt_file = work / "colour_check.md"
    prompt_file.write_text(COLOUR_CHECK_TEXT, encoding="utf-8")
    cmd = _cli_command(prompt_file)

    blocks_text = (
        "Blocks used (most first; height 0% = feet, 100% = top of the head):\n"
        + "\n".join(block_lines)
        + "\n\nAllowed replacement blocks:\n"
        + ", ".join(allowed)
    )

    content: list[dict[str, Any]] = [
        {"type": "text", "text": "Reference picture:"},
        _image_part(reference_png),
        {"type": "text", "text": "The statue as blocks: front (as in the picture), side, back:"},
        _image_part(statue_png),
        {"type": "text", "text": blocks_text},
    ]

    msg = {
        "type": "user",
        "message": {
            "role": "user",
            "content": content,
        },
    }
    stdin_payload = json.dumps(msg) + "\n"

    try:
        chunks = list(_run_cli(cmd, stdin_payload, work, timeout, provider="claude", stop=stop))
        raw_response = "".join(chunks)
        return parse_swaps(raw_response)
    except LlmError:
        raise
    except Exception as exc:
        raise LlmError(f"Colour check failed: {exc}") from exc


def statue_review(
    reference_png: bytes,
    statue_png: bytes,
    stop: threading.Event | None = None,
    timeout: float = 300,
    work_dir: Path | None = None,
) -> list[str]:
    """Checks the shape of a statue against reference image using Claude CLI.

    Writes STATUE_REVIEW_TEXT to prompt file in work_dir, sends reference image
    and statue views image to Claude. Returns parsed review bullet points.
    """
    work = work_dir or _CLI_WORKDIR
    work.mkdir(parents=True, exist_ok=True)

    prompt_file = work / "statue_review.md"
    prompt_file.write_text(STATUE_REVIEW_TEXT, encoding="utf-8")
    cmd = _cli_command(prompt_file)

    content: list[dict[str, Any]] = [
        {"type": "text", "text": "Reference picture:"},
        _image_part(reference_png),
        {"type": "text", "text": "The statue as blocks: front (as in the picture), side, back:"},
        _image_part(statue_png),
    ]

    msg = {
        "type": "user",
        "message": {
            "role": "user",
            "content": content,
        },
    }
    stdin_payload = json.dumps(msg) + "\n"

    try:
        chunks = list(_run_cli(cmd, stdin_payload, work, timeout, provider="claude", stop=stop))
        raw_response = "".join(chunks)
        return parse_review(raw_response)
    except LlmError:
        raise
    except Exception as exc:
        raise LlmError(f"Statue review failed: {exc}") from exc
