"""
CorridorKey Effect for Golobulus (After Effects)
Neural Green Screen Keyer

Install Golobulus from: https://github.com/mobile-bungalow/golobulus-rs
Place this file in your Golobulus effects folder.
"""
import numpy as np
import subprocess
import tempfile
import os
from pathlib import Path

# Auto-detect CorridorKey root: this file is in ae_plugin/golobulus/
CORRIDORKEY_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PYTHON_EXE = os.path.join(CORRIDORKEY_ROOT, ".venv", "Scripts", "python.exe")
PROCESSOR_SCRIPT = os.path.join(CORRIDORKEY_ROOT, "ae_plugin", "ae_processor.py")


def setup(ctx):
    """Register inputs and parameters."""
    # Image input
    ctx.register_image_input("Source")

    # Screen type dropdown
    ctx.register_int(
        "screen_type",
        display_name="Screen Type",
        default=0,
        min_val=0,
        max_val=1,
        # 0 = Green, 1 = Blue
    )

    # Despill slider
    ctx.register_float(
        "despill",
        display_name="Despill",
        default=0.5,
        min_val=0.0,
        max_val=1.0,
    )

    # Refiner slider
    ctx.register_float(
        "refiner",
        display_name="Edge Refiner",
        default=1.0,
        min_val=0.0,
        max_val=1.0,
    )

    # Despeckle checkbox
    ctx.register_bool(
        "despeckle",
        display_name="Auto Despeckle",
        default=True,
    )

    # Despeckle size
    ctx.register_int(
        "despeckle_size",
        display_name="Despeckle Size",
        default=400,
        min_val=50,
        max_val=2000,
    )


def run(ctx):
    """Process frame through CorridorKey."""
    # Get input image
    source = ctx.get_input("Source")
    if source is None:
        return

    # Get parameters
    screen_type = "green" if ctx.get_param("screen_type") == 0 else "blue"
    despill = ctx.get_param("despill")
    refiner = ctx.get_param("refiner")
    despeckle = ctx.get_param("despeckle")
    despeckle_size = ctx.get_param("despeckle_size")

    # Get output buffer
    output = ctx.output()

    # Convert to uint8 for saving
    source_uint8 = (np.clip(source, 0, 1) * 255).astype(np.uint8)

    # Temp files — unique names to avoid collisions with parallel instances
    temp_dir = tempfile.gettempdir()
    unique_id = os.getpid()
    input_path = os.path.join(temp_dir, f"ck_ae_input_{unique_id}.png")
    output_path = os.path.join(temp_dir, f"ck_ae_output_{unique_id}.png")

    try:
        # Save input (RGBA)
        try:
            from PIL import Image
            img = Image.fromarray(source_uint8, 'RGBA')
            img.save(input_path)
        except ImportError:
            import cv2
            cv2.imwrite(input_path, cv2.cvtColor(source_uint8, cv2.COLOR_RGBA2BGRA))

        # Call CorridorKey processor
        cmd = [
            PYTHON_EXE,
            PROCESSOR_SCRIPT,
            input_path,
            output_path,
            "--screen", screen_type,
            "--despill", str(despill),
            "--refiner", str(refiner),
            "--despeckle", "1" if despeckle else "0",
            "--despeckle-size", str(despeckle_size),
        ]

        result = subprocess.run(cmd, capture_output=True, timeout=60)

        if result.returncode == 0 and os.path.exists(output_path):
            # Load result
            try:
                from PIL import Image
                result_img = Image.open(output_path).convert('RGBA')
                result_array = np.array(result_img).astype(np.float32) / 255.0
            except ImportError:
                import cv2
                result_bgra = cv2.imread(output_path, cv2.IMREAD_UNCHANGED)
                result_array = cv2.cvtColor(result_bgra, cv2.COLOR_BGRA2RGBA).astype(np.float32) / 255.0

            # Copy to output
            np.copyto(output, result_array)
        else:
            # On error, pass through original
            np.copyto(output, source)

    except Exception as e:
        # On error, pass through original
        np.copyto(output, source)

    finally:
        # Always cleanup temp files
        for f in [input_path, output_path]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
