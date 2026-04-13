"""
CorridorKey - Neural Green Screen Keyer for After Effects
Requires Golobulus plugin: https://github.com/mobile-bungalow/golobulus-rs

This effect calls the external CorridorKey AI processor.
"""
import numpy as np
import subprocess
import tempfile
import os

# Auto-detect CorridorKey root: this file is in ae_plugin/golobulus/
CORRIDORKEY_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PYTHON_EXE = os.path.join(CORRIDORKEY_ROOT, ".venv", "Scripts", "python.exe")
PROCESSOR = os.path.join(CORRIDORKEY_ROOT, "ae_plugin", "ae_processor.py")


def setup(ctx):
    """Register effect parameters."""
    # Input layer
    ctx.register_image_input("Source")

    # Screen Type: 0=Green, 1=Blue
    ctx.register_int(
        "screen_type",
        display_name="Screen Type (0=Green, 1=Blue)",
        default=0,
        min_val=0,
        max_val=1,
    )

    # Despill strength
    ctx.register_float(
        "despill",
        display_name="Despill Strength",
        default=0.5,
        min_val=0.0,
        max_val=1.0,
    )

    # Edge refiner
    ctx.register_float(
        "refiner",
        display_name="Edge Refiner",
        default=1.0,
        min_val=0.0,
        max_val=1.0,
    )

    # Despeckle toggle
    ctx.register_bool(
        "despeckle",
        display_name="Auto Despeckle",
        default=True,
    )

    # Despeckle size
    ctx.register_int(
        "despeckle_size",
        display_name="Despeckle Min Size (px)",
        default=400,
        min_val=50,
        max_val=2000,
    )


def run(ctx):
    """Process frame through CorridorKey AI."""
    # Get source
    source = ctx.get_input("Source")
    if source is None:
        return

    # Get output buffer
    output = ctx.output()
    h, w = output.shape[:2]

    # Get parameters
    screen = "green" if ctx.get_param("screen_type") == 0 else "blue"
    despill = ctx.get_param("despill")
    refiner = ctx.get_param("refiner")
    despeckle = ctx.get_param("despeckle")
    despeckle_size = ctx.get_param("despeckle_size")

    # Temp files — unique names to avoid collisions with parallel instances
    # PID is always the same inside AE, so use random ID instead
    import uuid
    temp_dir = tempfile.gettempdir()
    unique_id = uuid.uuid4().hex[:12]
    input_path = os.path.join(temp_dir, f"ck_golobulus_in_{unique_id}.png")
    output_path = os.path.join(temp_dir, f"ck_golobulus_out_{unique_id}.png")

    try:
        # Save source frame
        save_png(source, input_path)

        # Build command
        cmd = [
            PYTHON_EXE,
            PROCESSOR,
            input_path,
            output_path,
            "--screen", screen,
            "--despill", str(despill),
            "--refiner", str(refiner),
            "--despeckle", "1" if despeckle else "0",
            "--despeckle-size", str(despeckle_size),
        ]

        # Run processor
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )

        if result.returncode == 0 and os.path.exists(output_path):
            # Load result
            keyed = load_png(output_path)
            if keyed is not None:
                # Copy to output
                np.copyto(output, keyed[:h, :w])
            else:
                import sys
                print("[CorridorKey] WARNING: Failed to load keyed output, passing through original", file=sys.stderr)
                np.copyto(output, source)
        else:
            # Pass through on error but log it
            import sys
            stderr_msg = result.stderr.decode("utf-8", errors="replace") if result.stderr else "unknown error"
            print(f"[CorridorKey] ERROR: Processor failed (rc={result.returncode}): {stderr_msg}", file=sys.stderr)
            np.copyto(output, source)

    except Exception as e:
        # Pass through on error but log it
        import sys
        print(f"[CorridorKey] ERROR: {e}", file=sys.stderr)
        np.copyto(output, source)

    finally:
        # Cleanup
        for f in [input_path, output_path]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except:
                    pass


def save_png(arr, path):
    """Save numpy array as PNG."""
    try:
        from PIL import Image
        # Convert float [0,1] to uint8
        if arr.dtype == np.float32 or arr.dtype == np.float64:
            arr = (np.clip(arr, 0, 1) * 255).astype(np.uint8)
        img = Image.fromarray(arr)
        img.save(path)
        return True
    except ImportError:
        # Fallback using raw file writing
        import struct
        import zlib

        if arr.dtype == np.float32 or arr.dtype == np.float64:
            arr = (np.clip(arr, 0, 1) * 255).astype(np.uint8)

        h, w = arr.shape[:2]
        channels = arr.shape[2] if len(arr.shape) > 2 else 1

        # Simple PNG writer
        def png_chunk(chunk_type, data):
            chunk = chunk_type + data
            return struct.pack('>I', len(data)) + chunk + struct.pack('>I', zlib.crc32(chunk) & 0xffffffff)

        # PNG header
        header = b'\x89PNG\r\n\x1a\n'

        # IHDR
        color_type = 6 if channels == 4 else (2 if channels == 3 else 0)
        ihdr = struct.pack('>IIBBBBB', w, h, 8, color_type, 0, 0, 0)

        # IDAT
        raw_data = b''
        for y in range(h):
            raw_data += b'\x00'  # Filter none
            raw_data += arr[y].tobytes()
        compressed = zlib.compress(raw_data, 9)

        with open(path, 'wb') as f:
            f.write(header)
            f.write(png_chunk(b'IHDR', ihdr))
            f.write(png_chunk(b'IDAT', compressed))
            f.write(png_chunk(b'IEND', b''))

        return True


def load_png(path):
    """Load PNG to numpy array."""
    try:
        from PIL import Image
        img = Image.open(path).convert('RGBA')
        arr = np.array(img).astype(np.float32) / 255.0
        return arr
    except ImportError:
        # Basic PNG reader fallback would go here
        return None
