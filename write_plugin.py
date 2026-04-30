#!/usr/bin/env python3
# Last modified: 2026-04-26 | Change: Read plugin source from file instead of embedded string
"""Write the CorridorKey plugin file to DaVinci Resolve's scripts directory.

Source of truth: resolve_plugin/CorridorKey_Pro.py
Running this script deploys whatever is currently in that file.
"""

import pathlib as _pathlib

# WHAT IT DOES: Reads the plugin source from disk so the embed can never fall behind.
# DANGER ZONE CRITICAL: CorridorKey_Pro.py is the source of truth. Edit THAT file, then run this.
_source = _pathlib.Path(__file__).parent / "resolve_plugin" / "CorridorKey_Pro.py"
if not _source.exists():
    raise FileNotFoundError(f"Plugin source not found: {_source}")
content = _source.read_text(encoding="utf-8")

# Show first line so operator can verify the right version is being deployed
_first_line = content.split("\n")[0]
print(f"Deploying: {_first_line[:120]}")

import sys as _sys

# Auto-detect Resolve scripts path
if _sys.platform == "win32":
    import os as _os
    _scripts_base = _os.path.join(_os.environ.get("PROGRAMDATA", "C:/ProgramData"),
        "Blackmagic Design", "DaVinci Resolve", "Fusion", "Scripts", "Utility")
elif _sys.platform == "darwin":
    _scripts_base = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
else:
    _scripts_base = "/opt/resolve/Fusion/Scripts/Utility"

output_path = _os.path.join(_scripts_base, "CorridorKey.py")

# Write the plugin script
_os.makedirs(_os.path.dirname(output_path), exist_ok=True)
with open(output_path, 'w', encoding='utf-8') as f:
    f.write(content)
print(f"Written to {output_path}")

# Write config file pointing to CorridorKey root
ck_root = _os.path.dirname(_os.path.abspath(__file__))
config_path = _os.path.join(_scripts_base, "corridorkey_path.txt")
with open(config_path, 'w') as f:
    f.write(ck_root)
print(f"Config written to {config_path}")
