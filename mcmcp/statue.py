"""!statue: image-to-3D models on free Hugging Face Spaces, and the saved .glb meshes."""
from __future__ import annotations

import datetime
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any, Callable

try:
    from . import config
except ImportError:
    import config


class StatueError(Exception):
    """Chat-ready error message, one line, at most 100 chars."""

    def __init__(self, message: str = ""):
        msg = " ".join(str(message).splitlines()).strip()
        if len(msg) > 100:
            msg = msg[:97] + "..."
        super().__init__(msg)


class StatueStopped(StatueError):
    """Raised when statue generation is stopped by the player."""
    pass


def is_glb(data: bytes) -> bool:
    """Return True if data starts with b'glTF'."""
    return bool(data and data[:4] == b"glTF")


def mesh_files() -> list[str]:
    """Sorted names without .glb of *.glb in config.STATUE_DIR; [] if the folder doesn't exist."""
    path = Path(config.STATUE_DIR)
    if not path.is_dir():
        return []
    names = [
        p.name[:-4]
        for p in path.iterdir()
        if p.is_file() and p.name.lower().endswith(".glb")
    ]
    return sorted(names)


def find_mesh(name: str) -> Path | None:
    """Case-insensitive exact match of name (with or without .glb) against mesh_files().

    Returns Path to the .glb file in config.STATUE_DIR, or None if not found or if
    name contains '/', '\\', or '..'.
    """
    if not name or "/" in name or "\\" in name or ".." in name:
        return None

    target = name[:-4] if name.lower().endswith(".glb") else name
    target_lower = target.lower()

    path = Path(config.STATUE_DIR)
    for mf in mesh_files():
        if mf.lower() == target_lower:
            return path / f"{mf}.glb"
    return None


def save_mesh(glb: Path) -> str:
    """Create config.STATUE_DIR, copy glb to statue-<MMDD-HHMM>.glb (adds -2, -3... if taken).

    Returns the name without .glb.
    """
    path = Path(config.STATUE_DIR)
    path.mkdir(parents=True, exist_ok=True)

    base = f"statue-{datetime.datetime.now().strftime('%m%d-%H%M')}"
    candidate = base
    target = path / f"{candidate}.glb"
    counter = 2
    while target.exists():
        candidate = f"{base}-{counter}"
        target = path / f"{candidate}.glb"
        counter += 1

    shutil.copyfile(Path(glb), target)
    return candidate


def _extract_path(item: Any) -> str:
    """Extract file path string from Gradio output (str, Path, or dict with 'value' or 'path')."""
    if isinstance(item, (str, Path)):
        return str(item)
    if isinstance(item, dict):
        if item.get("path"):
            return str(item["path"])
        if item.get("value"):
            return str(item["value"])
    return str(item)


def _handle_file(path: str | Path) -> Any:
    """Wrap file path with gradio_client.handle_file if available, else str."""
    try:
        from gradio_client import handle_file
        return handle_file(str(path))
    except (ImportError, AttributeError):
        return str(path)


def _get_client(space: str) -> Any:
    """Create a gradio_client.Client for the given Space."""
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise StatueError("No Hugging Face token. Add HF_TOKEN to .env.")
    from gradio_client import Client
    return Client(space, token=token, verbose=False)


def _cancel_job(job: Any) -> None:
    """Safely cancel a gradio_client Job."""
    try:
        job.cancel()
    except Exception:
        pass


def _handle_job_error(e: Exception) -> Any:
    """Convert job exceptions into chat-ready StatueError."""
    if isinstance(e, StatueError):
        raise e
    text = str(e).strip()
    if "quota" in text.lower():
        match = re.search(r"try again in\s+([^.\n\r]+)", text, re.IGNORECASE)
        if match:
            wait_time = match.group(1).strip().rstrip(".")
            raise StatueError(f"Out of free model time today. Try again in {wait_time}.")
        raise StatueError("Out of free model time today.")
    first_line = text.splitlines()[0].strip() if text else "Unknown error"
    prefix = "The model failed: "
    max_len = 100 - len(prefix)
    if len(first_line) > max_len:
        first_line = first_line[:max_len - 3] + "..."
    raise StatueError(f"{prefix}{first_line}")


def _wait_job(job: Any, stop: threading.Event | None = None) -> Any:
    """Wait for a submit job to finish, handling cancellation, timeout, and errors."""
    timeout = config.STATUE_TIMEOUT
    start_time = time.time()
    wait_interval = min(1.0, max(0.01, timeout))

    while True:
        if stop is not None and stop.is_set():
            _cancel_job(job)
            raise StatueStopped("Stopped.")

        try:
            done = job.done()
        except Exception as e:
            _handle_job_error(e)

        if done:
            break

        if (time.time() - start_time) > timeout:
            _cancel_job(job)
            raise StatueError("The model took too long.")

        if stop is not None:
            if stop.wait(wait_interval):
                _cancel_job(job)
                raise StatueStopped("Stopped.")
        else:
            time.sleep(wait_interval)

    try:
        return job.result()
    except Exception as e:
        _handle_job_error(e)


def _run(
    space_or_client: str | Any,
    api_name: str,
    args: tuple | list | dict | None = None,
    stop: threading.Event | None = None,
    client: Any = None,
    **kwargs: Any,
) -> Any:
    """Submit a task to a Gradio Space and wait for the result."""
    if not os.environ.get("HF_TOKEN"):
        raise StatueError("No Hugging Face token. Add HF_TOKEN to .env.")

    if client is not None:
        c = client
    elif isinstance(space_or_client, str):
        c = _get_client(space_or_client)
    else:
        c = space_or_client

    submit_args: tuple = ()
    submit_kwargs = dict(kwargs)
    submit_kwargs["api_name"] = api_name

    if args is not None:
        if isinstance(args, dict):
            submit_kwargs.update(args)
        elif isinstance(args, (list, tuple)):
            submit_args = tuple(args)
        else:
            submit_args = (args,)

    try:
        job = c.submit(*submit_args, **submit_kwargs)
    except Exception as e:
        _handle_job_error(e)

    return _wait_job(job, stop=stop)


def _model_hunyuan(
    image_path: Path,
    out_dir: Path,
    stop: threading.Event | None = None,
    client: Any = None,
) -> Path:
    """Generate 3D model using tencent/Hunyuan3D-2.1."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    res = _run(
        "tencent/Hunyuan3D-2.1",
        "/generation_all",
        stop=stop,
        client=client,
        image=_handle_file(image_path),
        steps=30,
        guidance_scale=5.0,
        seed=1234,
        octree_resolution=256,
        check_box_rembg=True,
        num_chunks=8000,
        randomize_seed=False,
    )
    target = res[1] if isinstance(res, (list, tuple)) and len(res) > 1 else res
    glb_path = _extract_path(target)
    out_file = out_dir / "model.glb"
    shutil.copyfile(glb_path, out_file)
    return out_file


def _model_hunyuan2(
    image_path: Path,
    out_dir: Path,
    stop: threading.Event | None = None,
    client: Any = None,
) -> Path:
    """Generate 3D model using tencent/Hunyuan3D-2."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    res = _run(
        "tencent/Hunyuan3D-2",
        "/generation_all",
        stop=stop,
        client=client,
        image=_handle_file(image_path),
        steps=30,
        guidance_scale=5.0,
        seed=1234,
        octree_resolution=256,
        check_box_rembg=True,
        num_chunks=8000,
        randomize_seed=False,
    )
    target = res[1] if isinstance(res, (list, tuple)) and len(res) > 1 else res
    glb_path = _extract_path(target)
    out_file = out_dir / "model.glb"
    shutil.copyfile(glb_path, out_file)
    return out_file


def _model_trellis(
    image_path: Path,
    out_dir: Path,
    stop: threading.Event | None = None,
    client: Any = None,
) -> Path:
    """Generate 3D model using microsoft/TRELLIS.2."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    c = client if client is not None else _get_client("microsoft/TRELLIS.2")
    _run(c, "/start_session", stop=stop)
    pre = _run(c, "/preprocess_image", args=[_handle_file(image_path)], stop=stop)
    pre_path = _extract_path(pre)
    _run(c, "/image_to_3d", args=[_handle_file(pre_path), 0, "1024"], stop=stop)
    res = _run(c, "/extract_glb", args=[200000, 2048], stop=stop)
    target = res[1] if isinstance(res, (list, tuple)) and len(res) > 1 else res
    glb_path = _extract_path(target)
    out_file = out_dir / "model.glb"
    shutil.copyfile(glb_path, out_file)
    return out_file


def _model_fast(
    image_path: Path,
    out_dir: Path,
    stop: threading.Event | None = None,
    client: Any = None,
) -> Path:
    """Generate 3D model using stabilityai/stable-fast-3d."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    res = _run(
        "stabilityai/stable-fast-3d",
        "/run_button",
        args=[_handle_file(image_path)],
        stop=stop,
        client=client,
    )
    target = res[1] if isinstance(res, (list, tuple)) and len(res) > 1 else res
    glb_path = _extract_path(target)
    out_file = out_dir / "model.glb"
    shutil.copyfile(glb_path, out_file)
    return out_file


MODELS: dict[str, tuple[str, Callable[[Path, Path, threading.Event | None], Path]]] = {
    "hunyuan": (
        "Hunyuan3D-2.1 (Tencent), the best tested; about 1 a day free",
        _model_hunyuan,
    ),
    "trellis": (
        "TRELLIS.2 (Microsoft)",
        _model_trellis,
    ),
    "hunyuan2": (
        "Hunyuan3D-2 (Tencent), the older version",
        _model_hunyuan2,
    ),
    "fast": (
        "Stable Fast 3D (Stability AI), quick and simple; several a day free",
        _model_fast,
    ),
}


def generate(
    model: str,
    image_png: bytes,
    out_dir: Path,
    stop: threading.Event | None = None,
) -> Path:
    """Generate a 3D model (.glb) from an image.

    Writes reference.png in out_dir, calls the model function,
    and returns out_dir / "model.glb".
    """
    model_key = model.lower().strip()
    if model_key not in MODELS:
        raise StatueError(f"Unknown model: {model}")

    if not os.environ.get("HF_TOKEN"):
        raise StatueError("No Hugging Face token. Add HF_TOKEN to .env.")

    if stop is not None and stop.is_set():
        raise StatueStopped("Stopped.")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ref_path = out_dir / "reference.png"
    ref_path.write_bytes(image_png)

    _desc, func = MODELS[model_key]
    return func(ref_path, out_dir, stop)
