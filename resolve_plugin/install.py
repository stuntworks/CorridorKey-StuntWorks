# Last modified: 2026-04-13 | Change: HRCS retrofit (documentation only, no logic changes) | Full history: git log
"""
CorridorKey DaVinci Resolve Plugin Installer
Installs the plugin to the Resolve Scripts folder.

WHAT IT DOES: Installs or uninstalls the CorridorKey plugin into DaVinci Resolve's Scripts folder.
    Copies core files, writes a config pointer to the CorridorKey engine root, and creates a
    launcher script that Resolve discovers in its Workspace > Scripts menu.
DEPENDS-ON: Resolve must be installed (needs its Scripts/Utility folder on disk).
    Source tree must contain core/, ui/, and resolve_corridorkey.py alongside this file.
AFFECTS: Resolve's Utility scripts folder — overwrites CorridorKey/ dir and CorridorKey.py launcher.
"""
import os
import sys
import shutil
from pathlib import Path


# WHAT IT DOES: Returns the platform-specific path to Resolve's Fusion/Scripts folder.
# ISOLATED: Pure path lookup, no side effects.
def get_resolve_scripts_path():
    """Get DaVinci Resolve scripts directory for current platform."""
    if sys.platform == "win32":
        # Windows - Scripts are in Fusion/Scripts folder
        programdata = os.environ.get("PROGRAMDATA", "C:/ProgramData")
        return Path(programdata) / "Blackmagic Design/DaVinci Resolve/Fusion/Scripts"
    elif sys.platform == "darwin":
        # macOS
        return Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts")
    else:
        # Linux
        return Path("/opt/resolve/Fusion/Scripts")


# WHAT IT DOES: Copies plugin files into Resolve's Utility folder, writes a config file
#   pointing back to the CorridorKey engine root, and creates a top-level launcher script.
# DEPENDS-ON: get_resolve_scripts_path(), source files (core/, ui/, resolve_corridorkey.py)
# AFFECTS: Resolve Scripts/Utility/CorridorKey/ dir, Scripts/Utility/CorridorKey.py launcher,
#   and corridorkey_path.txt config inside the installed dir.
# DANGER ZONE FRAGILE: The launcher_content string is written verbatim as a .py file
#   that Resolve will execute. Any syntax error in that string silently breaks the plugin launch.
def install():
    """Install CorridorKey plugin to DaVinci Resolve."""
    src_dir = Path(__file__).parent
    scripts_path = get_resolve_scripts_path()

    # Install to Utility folder (available from all pages)
    dest_dir = scripts_path / "Utility" / "CorridorKey"

    print("CorridorKey DaVinci Resolve Plugin Installer")
    print("=" * 50)
    print(f"Source: {src_dir}")
    print(f"Destination: {dest_dir}")
    print()

    # Check if Resolve scripts folder exists
    if not scripts_path.exists():
        print(f"Error: Resolve scripts folder not found at {scripts_path}")
        print("Make sure DaVinci Resolve is installed.")
        return False

    # Check write permissions
    test_dir = scripts_path / "Utility"
    test_dir.mkdir(parents=True, exist_ok=True)
    try:
        test_file = test_dir / ".ck_install_test"
        test_file.write_text("test")
        test_file.unlink()
    except PermissionError:
        print(f"Error: No write permission to {test_dir}")
        print("Try running as administrator.")
        return False

    # Warn before overwriting existing install
    if dest_dir.exists():
        print(f"Existing installation found at: {dest_dir}")
        response = input("Overwrite? [y/N]: ").strip().lower()
        if response != "y":
            print("Installation cancelled.")
            return False

    # Create destination directory
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Files and folders to copy
    items_to_copy = [
        "core",
        "ui",
        "resolve_corridorkey.py",
    ]

    # Copy files
    for item in items_to_copy:
        src_path = src_dir / item
        dest_path = dest_dir / item

        if not src_path.exists():
            print(f"Warning: {item} not found, skipping")
            continue

        if src_path.is_dir():
            if dest_path.exists():
                shutil.rmtree(dest_path)
            shutil.copytree(src_path, dest_path)
            print(f"  Copied directory: {item}")
        else:
            shutil.copy2(src_path, dest_path)
            print(f"  Copied file: {item}")

    # Write config file pointing to CorridorKey root
    ck_root = src_dir.parent
    config_path = dest_dir / "corridorkey_path.txt"
    config_path.write_text(str(ck_root))
    print(f"  Config: {config_path}")

    # Create launcher script at top level
    launcher_path = scripts_path / "Utility" / "CorridorKey.py"
    launcher_content = '''"""
CorridorKey Neural Green Screen
Launch from: Workspace > Scripts > CorridorKey
"""
import sys
import os

# Add CorridorKey plugin to path
plugin_dir = os.path.join(os.path.dirname(__file__), "CorridorKey")
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

# Read CorridorKey root from config
config_path = os.path.join(plugin_dir, "corridorkey_path.txt")
if os.path.exists(config_path):
    with open(config_path) as f:
        corridorkey_root = f.read().strip()
    if corridorkey_root not in sys.path:
        sys.path.insert(0, corridorkey_root)

# Launch plugin
from resolve_corridorkey import main
main()
'''

    with open(launcher_path, "w") as f:
        f.write(launcher_content)
    print(f"  Created launcher: {launcher_path.name}")

    print()
    print("Installation complete!")
    print()
    print("To use CorridorKey in DaVinci Resolve:")
    print("  1. Restart DaVinci Resolve")
    print("  2. Open a project with green screen footage")
    print("  3. Go to: Workspace > Scripts > CorridorKey")
    print()
    print("Note: Make sure external scripting is enabled:")
    print("  Preferences > System > General > External scripting using: Local")

    return True


# WHAT IT DOES: Removes the installed CorridorKey plugin folder, launcher, and config from Resolve's Scripts dir.
# DEPENDS-ON: get_resolve_scripts_path()
# AFFECTS: Deletes Scripts/Utility/CorridorKey/, Scripts/Utility/CorridorKey.py, and corridorkey_path.txt
def uninstall():
    """Remove CorridorKey plugin from DaVinci Resolve."""
    scripts_path = get_resolve_scripts_path()
    dest_dir = scripts_path / "Utility" / "CorridorKey"
    launcher_path = scripts_path / "Utility" / "CorridorKey.py"
    config_path = scripts_path / "Utility" / "corridorkey_path.txt"

    print("Uninstalling CorridorKey...")

    if dest_dir.exists():
        shutil.rmtree(dest_dir)
        print(f"  Removed: {dest_dir}")

    if launcher_path.exists():
        launcher_path.unlink()
        print(f"  Removed: {launcher_path}")

    if config_path.exists():
        config_path.unlink()
        print(f"  Removed: {config_path}")

    print("Uninstall complete!")
    return True


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--uninstall":
        uninstall()
    else:
        install()
