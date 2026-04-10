"""Test if CorridorKey panel loads correctly."""
import sys
import os

# Add paths relative to this script
_script_dir = os.path.dirname(os.path.abspath(__file__))
_ck_root = os.path.dirname(_script_dir)
sys.path.insert(0, _script_dir)
sys.path.insert(0, _ck_root)

# Add Resolve modules
if sys.platform == "win32":
    resolve_modules = os.path.join(os.environ.get("PROGRAMDATA", "C:/ProgramData"),
        "Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting/Modules")
elif sys.platform == "darwin":
    resolve_modules = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"
else:
    resolve_modules = "/opt/resolve/Developer/Scripting/Modules"
sys.path.insert(0, resolve_modules)

print("Testing imports...")

try:
    import DaVinciResolveScript as dvr
    print("  DaVinciResolveScript: OK")
except Exception as e:
    print(f"  DaVinciResolveScript: FAILED - {e}")

try:
    resolve = dvr.scriptapp("Resolve")
    if resolve:
        print("  Resolve connection: OK")
        fusion = resolve.Fusion()
        if fusion:
            print("  Fusion: OK")
            ui = fusion.UIManager
            if ui:
                print("  UIManager: OK")
            else:
                print("  UIManager: FAILED - None")
        else:
            print("  Fusion: FAILED - None")
    else:
        print("  Resolve connection: FAILED - None")
except Exception as e:
    print(f"  Resolve: FAILED - {e}")

try:
    from core.resolve_bridge import ResolveBridge
    print("  ResolveBridge: OK")
except Exception as e:
    print(f"  ResolveBridge: FAILED - {e}")

try:
    from core.corridorkey_processor import CorridorKeyProcessor
    print("  CorridorKeyProcessor: OK")
except Exception as e:
    print(f"  CorridorKeyProcessor: FAILED - {e}")

try:
    from ui.uimanager_panel import create_corridorkey_panel
    print("  UIManager Panel: OK")
except Exception as e:
    print(f"  UIManager Panel: FAILED - {e}")

print("\nAll imports tested.")
