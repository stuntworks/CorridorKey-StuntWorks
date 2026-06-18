"""CorridorKey MCP Server — headless wire/greenscreen removal via Claude."""
import subprocess
import sys
import json
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from . import engine_bridge  # relative import

mcp = FastMCP("corridorkey")


@mcp.tool()
def ck_status() -> dict:
    """Return engine availability, root path, processor path, and Python version."""
    root = engine_bridge._discover_root()
    processor = engine_bridge.get_processor_path()
    return {
        "available": engine_bridge.engine_available(),
        "root": root,
        "processor": processor,
        "python": sys.version,
    }


@mcp.tool()
def ck_process_frame(
    input_path: str,
    output_path: str,
    frame_number: int = 0,
    screen_type: str = "green",
    despill: float = 1.0,
    use_refiner: bool = True,
) -> dict:
    """Key a single PNG frame and write the result to output_path.

    Args:
        input_path: Absolute path to the source PNG.
        output_path: Absolute path for the keyed output PNG.
        frame_number: Informational only (not passed to processor; caller pre-extracts the frame).
        screen_type: "green" or "blue".
        despill: Despill strength 0.0-2.0.
        use_refiner: Set False to skip the refiner pass (faster, lower quality).
    """
    processor = engine_bridge.get_processor_path()
    if processor is None:
        return {"success": False, "output": "", "stdout": "", "stderr": "CorridorKey engine not found — set CORRIDORKEY_ROOT"}

    cmd = [
        sys.executable, processor,
        "single", input_path, output_path,
        "--screen", screen_type,
        "--despill", str(despill),
    ]
    if not use_refiner:
        cmd += ["--refiner", "0"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return {
            "success": result.returncode == 0,
            "output": output_path,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "output": "", "stdout": "", "stderr": "Process timed out after 300s"}
    except Exception as exc:
        return {"success": False, "output": "", "stdout": "", "stderr": str(exc)}


@mcp.tool()
def ck_process_clip(
    input_path: str,
    output_folder: str,
    start_frame: int = 0,
    end_frame: int = -1,
    screen_type: str = "green",
    despill: float = 1.0,
    use_refiner: bool = True,
    fps: float = 24.0,
) -> dict:
    """Key a frame range from a video clip and write PNGs to output_folder.

    Args:
        input_path: Absolute path to the source video.
        output_folder: Folder where keyed PNGs will be written.
        start_frame: First frame index (0-based).
        end_frame: Last frame index inclusive. -1 = process to end of clip.
        screen_type: "green" or "blue".
        despill: Despill strength 0.0-2.0.
        use_refiner: Set False to skip the refiner pass.
        fps: Frames per second of the source clip.
    """
    processor = engine_bridge.get_processor_path()
    if processor is None:
        return {"success": False, "output_folder": "", "stdout": "", "stderr": "CorridorKey engine not found — set CORRIDORKEY_ROOT"}

    cmd = [
        sys.executable, processor,
        "batch", input_path, output_folder,
        "--screen", screen_type,
        "--despill", str(despill),
        "--start-frame", str(start_frame),
        "--fps", str(fps),
    ]
    if end_frame > 0:
        cmd += ["--end-frame", str(end_frame)]
    if not use_refiner:
        cmd += ["--refiner", "0"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        return {
            "success": result.returncode == 0,
            "output_folder": output_folder,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "output_folder": "", "stdout": "", "stderr": "Process timed out after 3600s"}
    except Exception as exc:
        return {"success": False, "output_folder": "", "stdout": "", "stderr": str(exc)}


@mcp.tool()
def ck_postprocess(
    session_dir: str,
    output_path: str,
    background_path: str = "",
    despill: float = 1.0,
) -> dict:
    """Apply despill + optional background composite to a cached session dir.

    The session_dir must already contain fg.png + alpha.png written by a prior
    cache or single pass. Reads those, applies despill, composites over background
    if provided, and writes the final PNG to output_path.

    Args:
        session_dir: Session directory containing fg.png + alpha.png.
        output_path: Absolute path for the composited output PNG.
        background_path: Optional path to a background PNG. If empty, uses checker.
        despill: Despill strength 0.0-2.0.
    """
    processor = engine_bridge.get_processor_path()
    if processor is None:
        return {"success": False, "output": "", "stdout": "", "stderr": "CorridorKey engine not found — set CORRIDORKEY_ROOT"}

    cmd = [
        sys.executable, processor,
        "postproc", session_dir, output_path,
        "--despill", str(despill),
    ]
    if background_path:
        cmd += ["--background", background_path]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return {
            "success": result.returncode == 0,
            "output": output_path,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "output": "", "stdout": "", "stderr": "Process timed out after 300s"}
    except Exception as exc:
        return {"success": False, "output": "", "stdout": "", "stderr": str(exc)}


if __name__ == "__main__":
    mcp.run()
