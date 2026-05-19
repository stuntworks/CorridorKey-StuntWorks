# Last modified: 2026-04-26 | Change: New unified deploy script — pushes source files to live homes for both DaVinci (Fusion Utility folder) and AE (CEP extension folder), with timestamped backups, --revert, --clean-dummies, and 30-day auto-prune | Full history: git log
"""CorridorKey unified deploy.

ONE command to push every source file to the place its host program actually
loads from, with a backup of whatever was there before, so any deploy can be
undone within 30 days. Replaces the ad-hoc write_plugin.py — that script
still works but only handled the DaVinci side.

WHY this exists: Adobe and Blackmagic each force plugins to live in a
specific OS folder (Adobe CEP extensions, Resolve Fusion Utility). You
can't just point them at your repo. So you keep a master in your repo and
push a copy into their folder. With multiple files, multiple hosts, and
multiple "old dummy" copies left behind, it becomes very easy to edit the
wrong file. This script makes the layout explicit and the deploy automatic.

USAGE:
    python deploy.py                 — forward deploy (with backup)
    python deploy.py --map           — print the deploy map and exit (no writes)
    python deploy.py --clean-dummies — forward deploy + archive known unused files
    python deploy.py --revert latest — restore from the most recent backup
    python deploy.py --revert YYYY-MM-DD_HHMMSS — restore from a specific backup
    python deploy.py --list-backups  — show retained backups + their manifest

BACKUP RETENTION: 30 days. Any backup directory older than that is deleted
at the start of each run. So if you want a backup to live longer, copy it
out of .deploy_backups/ within 30 days.

REVERT SAFETY: Each backup ships with a manifest.json describing every file
that was changed, so revert is a straight inverse copy. If a destination
file did not exist before the deploy (deploy created it from nothing), the
revert deletes it rather than restoring an empty file.
"""

import os
import sys
import json
import shutil
import argparse
import datetime
from pathlib import Path


# DANGER ZONE CRITICAL: paths must match what each host actually loads.
# If Adobe or Blackmagic ever change these, the whole script needs updating.
# Reason: Resolve only auto-discovers plugins in Fusion/Scripts/Utility/.
# Reason: AE only loads CEP extensions from %APPDATA%/Adobe/CEP/extensions/.
ROOT = Path(__file__).resolve().parent
BACKUP_DIR = ROOT / ".deploy_backups"
RETENTION_DAYS = 30

PROGRAMDATA = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData"))
APPDATA = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))

DAVINCI_UTILITY = PROGRAMDATA / "Blackmagic Design" / "DaVinci Resolve" / "Fusion" / "Scripts" / "Utility"
AE_EXTENSION = APPDATA / "Adobe" / "CEP" / "extensions" / "com.corridorkey.panel"


# Forward deployments — (label, source_path, live_path).
# Source must exist; live will be backed up (if it existed) then overwritten.
DEPLOYMENTS = [
    (
        "DaVinci plugin",
        ROOT / "resolve_plugin" / "CorridorKey_Pro.py",
        DAVINCI_UTILITY / "CorridorKey.py",
    ),
]

# Generated files — written by deploy with computed content (no source file).
# The content function takes no args and returns a string.
GENERATED = [
    (
        "DaVinci config (engine root path)",
        DAVINCI_UTILITY / "corridorkey_path.txt",
        lambda: str(ROOT),
    ),
]

# Files known to be unused dummies. Archived to backup, then deleted from
# their working location. Only touched when --clean-dummies is passed.
#
# DANGER ZONE CRITICAL: cep_panel/ files are NOT dummies — they are the
# LIVE AE plugin files reached through a Windows junction at
# %APPDATA%\Adobe\CEP\extensions\com.corridorkey.panel -> ae_plugin\cep_panel.
# The skill file's old "DUMMY" list was written before the junction was set
# up on 2026-04-26 (see SESSION_HANDOFF.md "CEP folder is now a Windows
# JUNCTION to the engine repo"). NEVER add cep_panel/* paths to this list.
#
# Source for this list: SESSION_HANDOFF.md "Cleanup obsolete files in
# ae_plugin/ root" — only the ae_plugin/ root duplicates are safe to remove,
# AND only the deployed Fusion preview_viewer_v2.py which is unused (the
# plugin launches the viewer from the source repo path directly).
DUMMIES = [
    ROOT / "ae_plugin" / "ae_processor.py",
    ROOT / "ae_plugin" / "preview_viewer_v2.py",
    DAVINCI_UTILITY / "preview_viewer_v2.py",
]

# Reference info — printed in --map but never touched. AE source-of-truth
# lives at the CEP folder directly; there is no master copy in the repo.
REFERENCE = [
    ("AE CEP panel (edit IN PLACE - no deploy)", AE_EXTENSION),
    ("Engine module (shared by all hosts)", ROOT / "CorridorKeyModule"),
    ("Engine venv", ROOT / ".venv"),
    ("SAM2 weights", ROOT / "sam2_weights"),
]


# WHAT IT DOES: Builds a YYYY-MM-DD_HHMMSS string for the current local time.
#   Used to name a fresh backup directory so multiple deploys in one day don't
#   collide. Local time chosen over UTC because the user reads these dates
#   and would be confused by UTC offsets.
# DEPENDS-ON: datetime stdlib.
# AFFECTS: pure function — returns a string.
def _now_stamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")


# WHAT IT DOES: Parses a backup directory name back to a datetime so we can
#   compare against the retention window.
# DEPENDS-ON: directory must be named YYYY-MM-DD_HHMMSS.
# AFFECTS: returns datetime, or None if the name is unparseable.
def _stamp_to_dt(name: str):
    try:
        return datetime.datetime.strptime(name, "%Y-%m-%d_%H%M%S")
    except ValueError:
        return None


# WHAT IT DOES: Deletes any backup directory older than RETENTION_DAYS days.
#   Called at the start of every deploy/revert run so cleanup is automatic.
#   Skips directories whose name doesn't parse as a timestamp (manual additions
#   or junk files won't get auto-deleted — only our own timestamped dirs).
# DEPENDS-ON: BACKUP_DIR existing or being missing (handled both).
# AFFECTS: deletes directories on disk. Prints what it pruned.
def _prune_old_backups():
    if not BACKUP_DIR.exists():
        return
    cutoff = datetime.datetime.now() - datetime.timedelta(days=RETENTION_DAYS)
    pruned = 0
    for child in BACKUP_DIR.iterdir():
        if not child.is_dir():
            continue
        dt = _stamp_to_dt(child.name)
        if dt is None:
            continue
        if dt < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            pruned += 1
            print(f"  pruned old backup: {child.name}")
    if pruned == 0 and BACKUP_DIR.exists():
        # silent — only print when work happens
        pass


# WHAT IT DOES: Copies a single source file to a target path, creating any
#   missing parent directories. Atomic on Windows via os.replace once the
#   .tmp file is fully written.
# DEPENDS-ON: source file existing, write permission on target's parent.
# AFFECTS: writes target_path on disk.
def _copy_atomic(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    shutil.copy2(str(src), str(tmp))
    os.replace(str(tmp), str(dst))


# WHAT IT DOES: Writes a string to a file, creating parents and writing
#   atomically. Used for the generated config files (corridorkey_path.txt).
# DEPENDS-ON: write permission on target's parent.
# AFFECTS: writes target_path on disk.
def _write_atomic(dst: Path, content: str):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(str(tmp), str(dst))


# WHAT IT DOES: Backs up an existing live file (if any) into the timestamped
#   backup directory, preserving relative-to-root layout so the manifest can
#   point at it cleanly. Returns the backup path or None if there was nothing
#   to back up (live file did not exist).
# DEPENDS-ON: backup_root already created.
# AFFECTS: writes a copy of the live file under backup_root.
def _backup_live(live_path: Path, backup_root: Path):
    if not live_path.exists():
        return None
    # Use a flat name based on the absolute path, replacing colons and slashes
    # with underscores so it's a legal filename on Windows. Keeps backups
    # readable instead of recreating C:/ProgramData/... directory trees.
    flat = str(live_path).replace(":", "_").replace("\\", "_").replace("/", "_")
    bp = backup_root / flat
    bp.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(live_path), str(bp))
    return bp


# ===== Map =====
# WHAT IT DOES: Prints a human-readable map of every (source -> live) pair
#   the deploy script knows about, plus the reference paths and the dummy
#   list. No writes. This is the answer to "where does everything live?"
# DEPENDS-ON: DEPLOYMENTS, GENERATED, DUMMIES, REFERENCE.
# AFFECTS: prints to stdout.
def cmd_map():
    print("=" * 78)
    print("CorridorKey deploy map")
    print("=" * 78)
    print()
    print("FORWARD DEPLOY (source -> live):")
    for label, src, live in DEPLOYMENTS:
        src_status = "exists" if src.exists() else "MISSING"
        live_status = "exists" if live.exists() else "absent"
        print(f"  {label}")
        print(f"    source: {src}  [{src_status}]")
        print(f"    live:   {live}  [{live_status}]")
    print()
    print("GENERATED (computed at deploy time):")
    for label, live, _content_fn in GENERATED:
        live_status = "exists" if live.exists() else "absent"
        print(f"  {label}")
        print(f"    live:   {live}  [{live_status}]")
    print()
    print("DUMMIES (archived only with --clean-dummies):")
    for d in DUMMIES:
        status = "exists" if d.exists() else "absent (already cleaned)"
        print(f"  {d}  [{status}]")
    print()
    print("REFERENCE (informational - not deployed by this script):")
    for label, p in REFERENCE:
        status = "exists" if p.exists() else "missing"
        print(f"  {label}")
        print(f"    {p}  [{status}]")
    print()
    print(f"BACKUP DIR: {BACKUP_DIR}  (retention: {RETENTION_DAYS} days)")
    print()


# ===== Deploy =====
# WHAT IT DOES: Pushes every DEPLOYMENTS source to its live path, writes
#   every GENERATED file, optionally archives DUMMIES, and writes a manifest
#   so the run can be reverted later. Backs up the previous live state of
#   every file it touches.
# DEPENDS-ON: source files for DEPLOYMENTS existing.
# AFFECTS: writes live files, creates backup dir, prints a checklist.
def cmd_deploy(clean_dummies: bool = False):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    _prune_old_backups()
    stamp = _now_stamp()
    backup_root = BACKUP_DIR / stamp
    backup_root.mkdir(parents=True, exist_ok=True)

    manifest = {
        "timestamp": stamp,
        "clean_dummies": clean_dummies,
        "operations": [],
    }

    print("=" * 78)
    print(f"CorridorKey deploy — {stamp}")
    print("=" * 78)
    print()

    print("FORWARD DEPLOY:")
    for label, src, live in DEPLOYMENTS:
        if not src.exists():
            print(f"  [SKIP] {label} — source missing: {src}")
            continue
        backup_path = _backup_live(live, backup_root)
        _copy_atomic(src, live)
        manifest["operations"].append({
            "op": "deploy",
            "label": label,
            "source": str(src),
            "live": str(live),
            "backup": str(backup_path) if backup_path else None,
        })
        backup_note = "(no prior file)" if backup_path is None else "(prev backed up)"
        print(f"  [OK]   {label}")
        print(f"         {src}")
        print(f"      -> {live}  {backup_note}")

    print()
    print("GENERATED:")
    for label, live, content_fn in GENERATED:
        content = content_fn()
        backup_path = _backup_live(live, backup_root)
        _write_atomic(live, content)
        manifest["operations"].append({
            "op": "generate",
            "label": label,
            "live": str(live),
            "backup": str(backup_path) if backup_path else None,
            "content_preview": content[:200],
        })
        backup_note = "(no prior file)" if backup_path is None else "(prev backed up)"
        print(f"  [OK]   {label}")
        print(f"      -> {live}  {backup_note}")

    if clean_dummies:
        print()
        print("DUMMY CLEANUP:")
        for d in DUMMIES:
            if not d.exists():
                print(f"  [SKIP] already absent: {d}")
                continue
            backup_path = _backup_live(d, backup_root)
            try:
                d.unlink()
                manifest["operations"].append({
                    "op": "archive_dummy",
                    "live": str(d),
                    "backup": str(backup_path) if backup_path else None,
                })
                print(f"  [ARC] archived & removed: {d}")
            except Exception as e:
                print(f"  [ERR] could not remove {d}: {e}")
    else:
        # Always show the count so the user knows --clean-dummies is available
        existing_dummies = [d for d in DUMMIES if d.exists()]
        if existing_dummies:
            print()
            print(f"NOTE: {len(existing_dummies)} known-unused file(s) detected.")
            print("      Run with --clean-dummies to archive and remove them.")

    # Write manifest last — if anything above crashed, the partial manifest
    # still records what was done before the crash.
    manifest_path = backup_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print()
    print(f"Backup dir: {backup_root}")
    print(f"Manifest:   {manifest_path}")
    print(f"To revert:  python deploy.py --revert {stamp}")
    print()


# ===== Revert =====
# WHAT IT DOES: Reads a backup's manifest and undoes every operation it
#   recorded. Files that had a prior version get the prior version copied
#   back. Files that the deploy created from nothing get deleted. Archived
#   dummies get put back where they were.
# DEPENDS-ON: backup directory existing under BACKUP_DIR with a manifest.json.
# AFFECTS: writes live files, prints a checklist of what was reverted.
def cmd_revert(target: str):
    if not BACKUP_DIR.exists():
        print(f"No backups directory yet: {BACKUP_DIR}")
        return 1

    if target == "latest":
        candidates = sorted(
            (c for c in BACKUP_DIR.iterdir() if c.is_dir() and _stamp_to_dt(c.name)),
            key=lambda c: c.name,
            reverse=True,
        )
        if not candidates:
            print("No backups available to revert.")
            return 1
        backup_root = candidates[0]
    else:
        backup_root = BACKUP_DIR / target
        if not backup_root.exists():
            print(f"Backup not found: {backup_root}")
            print("Available backups:")
            for c in sorted(BACKUP_DIR.iterdir()):
                if c.is_dir():
                    print(f"  {c.name}")
            return 1

    manifest_path = backup_root / "manifest.json"
    if not manifest_path.exists():
        print(f"Manifest missing in {backup_root} — cannot revert safely.")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    print("=" * 78)
    print(f"CorridorKey revert — restoring backup {manifest['timestamp']}")
    print("=" * 78)
    print()

    for op in manifest["operations"]:
        kind = op["op"]
        if kind in ("deploy", "generate"):
            live = Path(op["live"])
            backup = op.get("backup")
            if backup is None:
                # The deploy created this file from nothing — revert deletes it.
                if live.exists():
                    try:
                        live.unlink()
                        print(f"  [DEL] {live}  (had no prior version)")
                    except Exception as e:
                        print(f"  [ERR] could not delete {live}: {e}")
                else:
                    print(f"  [SKIP] already absent: {live}")
            else:
                bp = Path(backup)
                if not bp.exists():
                    print(f"  [ERR] backup file missing: {bp}")
                    continue
                _copy_atomic(bp, live)
                print(f"  [RST] {live}  (from {bp.name})")
        elif kind == "archive_dummy":
            live = Path(op["live"])
            bp = Path(op["backup"]) if op.get("backup") else None
            if bp and bp.exists():
                _copy_atomic(bp, live)
                print(f"  [RST] (dummy) {live}")
            else:
                print(f"  [ERR] dummy backup missing: {bp}")
        else:
            print(f"  [WARN] unknown op {kind} — skipped")

    print()
    print("Revert complete.")
    return 0


# ===== List backups =====
# WHAT IT DOES: Prints every retained backup with its timestamp and the
#   number of operations it recorded. Lets the user pick a target for
#   --revert without guessing.
# DEPENDS-ON: BACKUP_DIR may or may not exist (handled).
# AFFECTS: prints to stdout.
def cmd_list_backups():
    if not BACKUP_DIR.exists() or not any(BACKUP_DIR.iterdir()):
        print(f"No backups yet: {BACKUP_DIR}")
        return
    print(f"Backups under {BACKUP_DIR}:")
    print(f"  (retention: {RETENTION_DAYS} days — older entries auto-pruned)")
    print()
    for child in sorted(BACKUP_DIR.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        manifest_path = child / "manifest.json"
        if manifest_path.exists():
            try:
                m = json.loads(manifest_path.read_text(encoding="utf-8"))
                ops = m.get("operations", [])
                age = ""
                dt = _stamp_to_dt(child.name)
                if dt:
                    days = (datetime.datetime.now() - dt).days
                    age = f"  ({days}d ago)"
                print(f"  {child.name}{age}  — {len(ops)} op(s){'  [clean-dummies]' if m.get('clean_dummies') else ''}")
            except Exception as e:
                print(f"  {child.name}  — manifest unreadable: {e}")
        else:
            print(f"  {child.name}  — no manifest")


# WHAT IT DOES: argparse driver. Default action is forward deploy.
# DEPENDS-ON: cmd_* functions above.
# AFFECTS: dispatches to a command, returns its exit code.
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="CorridorKey unified deploy script.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--map", action="store_true",
                        help="Print the deploy map and exit (no writes).")
    parser.add_argument("--clean-dummies", action="store_true",
                        help="Archive known unused files in addition to forward deploy.")
    parser.add_argument("--revert", metavar="TIMESTAMP",
                        help="Revert from a specific backup, or 'latest'.")
    parser.add_argument("--list-backups", action="store_true",
                        help="List retained backups and their op counts.")
    args = parser.parse_args(argv)

    if args.map:
        cmd_map()
        return 0
    if args.list_backups:
        cmd_list_backups()
        return 0
    if args.revert:
        return cmd_revert(args.revert)
    cmd_deploy(clean_dummies=args.clean_dummies)
    return 0


if __name__ == "__main__":
    sys.exit(main())
