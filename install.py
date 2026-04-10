#!/usr/bin/env python3
"""
CorridorKey Plugin — Unified Installer
Installs to DaVinci Resolve, After Effects, and/or Premiere Pro.

Usage:
    python install.py           # Interactive — detect and choose
    python install.py --all     # Install to all detected apps
    python install.py --resolve # Resolve only
    python install.py --adobe   # AE + Premiere only
    python install.py --uninstall
"""
import os
import sys
import shutil
import argparse
from pathlib import Path


# ── Paths ─────────────────────────────────────────────────────

PLUGIN_ROOT = Path(__file__).parent
CORRIDORKEY_ROOT = PLUGIN_ROOT  # Plugin repo root

# Resolve
def get_resolve_scripts_path():
    if sys.platform == "win32":
        base = os.environ.get("PROGRAMDATA", "C:/ProgramData")
        return Path(base) / "Blackmagic Design/DaVinci Resolve/Fusion/Scripts"
    elif sys.platform == "darwin":
        return Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts")
    else:
        return Path("/opt/resolve/Fusion/Scripts")

# Adobe CEP
def get_cep_extensions_path():
    if sys.platform == "win32":
        # User-level CEP path (no admin needed)
        return Path(os.environ.get("APPDATA", "")) / "Adobe/CEP/extensions"
    elif sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Adobe/CEP/extensions"
    else:
        return Path.home() / ".adobe/CEP/extensions"


# ── Detection ─────────────────────────────────────────────────

def detect_resolve():
    scripts = get_resolve_scripts_path()
    return scripts.parent.exists()

def detect_after_effects():
    if sys.platform == "win32":
        ae_base = Path("C:/Program Files/Adobe")
        return any(ae_base.glob("Adobe After Effects*"))
    elif sys.platform == "darwin":
        return Path("/Applications/Adobe After Effects 2025").exists() or \
               Path("/Applications/Adobe After Effects 2026").exists()
    return False

def detect_premiere():
    if sys.platform == "win32":
        pp_base = Path("C:/Program Files/Adobe")
        return any(pp_base.glob("Adobe Premiere Pro*"))
    elif sys.platform == "darwin":
        return Path("/Applications/Adobe Premiere Pro 2025").exists() or \
               Path("/Applications/Adobe Premiere Pro 2026").exists()
    return False

def detect_corridorkey():
    """Check if CorridorKey engine is installed (parent or sibling directory)."""
    # Check common locations
    candidates = [
        PLUGIN_ROOT.parent / "CorridorKey",
        PLUGIN_ROOT.parent,
        Path(os.environ.get("CORRIDORKEY_ROOT", "")),
    ]
    for p in candidates:
        if p.exists() and (p / "CorridorKeyModule").exists():
            return p
    return None


# ── Installers ────────────────────────────────────────────────

def check_write_permission(path):
    """Check if we can write to a directory."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        test_file = path / ".ck_install_test"
        test_file.write_text("test")
        test_file.unlink()
        return True
    except PermissionError:
        return False


def install_resolve(ck_engine_path):
    """Install CorridorKey plugin to DaVinci Resolve."""
    print("\n── DaVinci Resolve ──")

    scripts_path = get_resolve_scripts_path()
    utility_dir = scripts_path / "Utility"
    dest_dir = utility_dir / "CorridorKey"

    if not check_write_permission(utility_dir):
        print("  ERROR: No write permission. Try running as administrator.")
        return False

    # Warn before overwriting
    if dest_dir.exists():
        print(f"  Existing install found at: {dest_dir}")
        response = input("  Overwrite? [y/N]: ").strip().lower()
        if response != "y":
            print("  Skipped.")
            return False

    dest_dir.mkdir(parents=True, exist_ok=True)

    # Copy plugin files
    src = PLUGIN_ROOT / "resolve_plugin"
    items = ["core", "ui", "resolve_corridorkey.py"]

    for item in items:
        src_path = src / item
        dst_path = dest_dir / item
        if not src_path.exists():
            print(f"  Warning: {item} not found, skipping")
            continue
        if src_path.is_dir():
            if dst_path.exists():
                shutil.rmtree(dst_path)
            shutil.copytree(src_path, dst_path)
        else:
            shutil.copy2(src_path, dst_path)
        print(f"  Copied: {item}")

    # Write config pointing to CorridorKey engine
    config_path = dest_dir / "corridorkey_path.txt"
    config_path.write_text(str(ck_engine_path))
    print(f"  Config: {config_path}")

    # Write launcher script
    launcher = utility_dir / "CorridorKey.py"

    # Also generate and install the full standalone plugin
    write_plugin = PLUGIN_ROOT / "write_plugin.py"
    if write_plugin.exists():
        print("  Generating standalone plugin...")
        import subprocess
        result = subprocess.run(
            [sys.executable, str(write_plugin)],
            capture_output=True, text=True, cwd=str(PLUGIN_ROOT)
        )
        if result.returncode == 0:
            print("  Standalone plugin installed")
        else:
            # Fallback: write a simple launcher
            launcher.write_text(f'''"""CorridorKey Neural Green Screen"""
import sys, os
plugin_dir = os.path.join(os.path.dirname(__file__), "CorridorKey")
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)
config_path = os.path.join(plugin_dir, "corridorkey_path.txt")
if os.path.exists(config_path):
    with open(config_path) as f:
        ck_root = f.read().strip()
    if ck_root not in sys.path:
        sys.path.insert(0, ck_root)
from resolve_corridorkey import main
main()
''')
            print(f"  Launcher: {launcher.name}")

    print("  Resolve: INSTALLED")
    print("  Access via: Workspace > Scripts > CorridorKey")
    return True


def install_adobe(ck_engine_path):
    """Install CorridorKey CEP panel for After Effects and Premiere Pro."""
    print("\n── Adobe After Effects / Premiere Pro ──")

    cep_path = get_cep_extensions_path()
    dest_dir = cep_path / "com.corridorkey.panel"

    cep_path.mkdir(parents=True, exist_ok=True)

    if not check_write_permission(cep_path):
        print("  ERROR: No write permission.")
        return False

    # Warn before overwriting
    if dest_dir.exists():
        print(f"  Existing install found at: {dest_dir}")
        response = input("  Overwrite? [y/N]: ").strip().lower()
        if response != "y":
            print("  Skipped.")
            return False

    dest_dir.mkdir(parents=True, exist_ok=True)

    # Copy CEP panel files
    src = PLUGIN_ROOT / "ae_plugin" / "cep_panel"
    if src.exists():
        for item in src.iterdir():
            dst = dest_dir / item.name
            if item.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(item, dst)
            else:
                shutil.copy2(item, dst)
            print(f"  Copied: {item.name}")

    # Copy the processor script
    processor = PLUGIN_ROOT / "ae_plugin" / "ae_processor.py"
    if processor.exists():
        shutil.copy2(processor, dest_dir / "ae_processor.py")
        print("  Copied: ae_processor.py")

    # Write config file
    config_path = dest_dir / "corridorkey_path.txt"
    config_path.write_text(str(ck_engine_path))
    print(f"  Config: {config_path}")

    # Create icons directory if missing
    icons_dir = dest_dir / "icons"
    icons_dir.mkdir(exist_ok=True)

    # Enable unsigned extensions (required for development)
    enable_unsigned_extensions()

    print("  Adobe CEP: INSTALLED")
    print("  Access via: Window > Extensions > CorridorKey")
    return True


def enable_unsigned_extensions():
    """Enable loading of unsigned CEP extensions (required for dev/sideloaded panels)."""
    if sys.platform == "win32":
        try:
            import winreg
            # Set PlayerDebugMode for CSXS.11 (and a few versions)
            for version in ["11", "12", "10", "9"]:
                key_path = f"Software\\Adobe\\CSXS.{version}"
                try:
                    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
                    winreg.SetValueEx(key, "PlayerDebugMode", 0, winreg.REG_SZ, "1")
                    winreg.CloseKey(key)
                except Exception:
                    pass
            print("  Enabled unsigned extensions (registry)")
        except ImportError:
            print("  Warning: Could not set registry. You may need to enable unsigned extensions manually.")
    elif sys.platform == "darwin":
        for version in ["11", "12", "10", "9"]:
            os.system(f'defaults write com.adobe.CSXS.{version} PlayerDebugMode 1 2>/dev/null')
        print("  Enabled unsigned extensions (defaults)")


# ── Uninstaller ───────────────────────────────────────────────

def uninstall():
    """Remove CorridorKey from all apps."""
    print("\nUninstalling CorridorKey Plugin...")

    # Resolve
    resolve_dir = get_resolve_scripts_path() / "Utility" / "CorridorKey"
    resolve_launcher = get_resolve_scripts_path() / "Utility" / "CorridorKey.py"
    resolve_config = get_resolve_scripts_path() / "Utility" / "corridorkey_path.txt"

    for p in [resolve_dir, resolve_launcher, resolve_config]:
        if p.exists():
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            print(f"  Removed: {p}")

    # Adobe CEP
    cep_dir = get_cep_extensions_path() / "com.corridorkey.panel"
    if cep_dir.exists():
        shutil.rmtree(cep_dir)
        print(f"  Removed: {cep_dir}")

    print("  Uninstall complete!")


# ── Main ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CorridorKey Plugin Installer")
    parser.add_argument("--all", action="store_true", help="Install to all detected apps")
    parser.add_argument("--resolve", action="store_true", help="Install to DaVinci Resolve only")
    parser.add_argument("--adobe", action="store_true", help="Install to After Effects / Premiere only")
    parser.add_argument("--uninstall", action="store_true", help="Remove from all apps")
    args = parser.parse_args()

    print("=" * 50)
    print("  CorridorKey Plugin Installer")
    print("  AI Green Screen for Video Editors")
    print("=" * 50)

    if args.uninstall:
        uninstall()
        return

    # Detect CorridorKey engine
    ck_path = detect_corridorkey()
    if not ck_path:
        print("\nERROR: CorridorKey engine not found!")
        print("Install it first: https://github.com/nikopueringer/CorridorKey")
        print("Or set CORRIDORKEY_ROOT environment variable.")
        return
    print(f"\nCorridorKey engine: {ck_path}")

    # Detect apps
    has_resolve = detect_resolve()
    has_ae = detect_after_effects()
    has_ppro = detect_premiere()

    print(f"\nDetected apps:")
    print(f"  DaVinci Resolve: {'YES' if has_resolve else 'not found'}")
    print(f"  After Effects:   {'YES' if has_ae else 'not found'}")
    print(f"  Premiere Pro:    {'YES' if has_ppro else 'not found'}")

    if not any([has_resolve, has_ae, has_ppro]):
        print("\nNo supported apps found!")
        return

    # Determine what to install
    install_resolve_flag = False
    install_adobe_flag = False

    if args.all:
        install_resolve_flag = has_resolve
        install_adobe_flag = has_ae or has_ppro
    elif args.resolve:
        install_resolve_flag = has_resolve
    elif args.adobe:
        install_adobe_flag = has_ae or has_ppro
    else:
        # Interactive mode
        print("")
        if has_resolve:
            r = input("Install for DaVinci Resolve? [Y/n]: ").strip().lower()
            install_resolve_flag = r != "n"
        if has_ae or has_ppro:
            apps = []
            if has_ae: apps.append("After Effects")
            if has_ppro: apps.append("Premiere Pro")
            r = input(f"Install for {' + '.join(apps)}? [Y/n]: ").strip().lower()
            install_adobe_flag = r != "n"

    # Install
    results = []

    if install_resolve_flag:
        if install_resolve(ck_path):
            results.append("DaVinci Resolve")

    if install_adobe_flag:
        if install_adobe(ck_path):
            if has_ae: results.append("After Effects")
            if has_ppro: results.append("Premiere Pro")

    # Summary
    print("\n" + "=" * 50)
    if results:
        print("  INSTALLED for: " + ", ".join(results))
        print("")
        print("  Next steps:")
        print("  1. Restart your video editor")
        if "DaVinci Resolve" in results:
            print("  2. Resolve: Workspace > Scripts > CorridorKey")
            print("     (Enable scripting: Preferences > System > General)")
        if "After Effects" in results or "Premiere Pro" in results:
            print("  2. Adobe: Window > Extensions > CorridorKey")
    else:
        print("  Nothing installed.")
    print("=" * 50)


if __name__ == "__main__":
    main()
