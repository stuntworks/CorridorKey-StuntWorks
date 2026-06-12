# Last modified: 2026-06-12 | Change: gauntlet runner v1 -- 6 clips, OLD vs NEW, contact sheet + scorecard
#
# WHAT IT DOES:
#   End-to-end gauntlet for fusion_v2 validation.  For each of 6 benchmark clips:
#     - Picks 3 test frames at 25%/50%/75% of duration.
#     - Extracts each frame via ae_processor.py extract (handles HEVC via PyAV).
#     - Down-halves any frame wider than 4096 px (truck-hit is 6K).
#     - Runs ae_processor.py cache (NN alpha, writes fg.png + alpha.png + plate.png).
#     - Auto-derives SAM positive dots from the NN alpha blob.
#     - Runs ae_processor.py sam-apply (SAM2 gate into sam2_gate_raw.png).
#     - Runs ae_processor.py postproc twice:
#         matte_NEW  -- fusion_v2:true, --background matte
#         matte_OLD  -- fusion_v2:false (standard pipeline), --background matte
#   Outputs:
#     D:\CLAUDE_JUNK\ck-gauntlet\run1\contact_sheet_run1.png
#     D:\CLAUDE_JUNK\ck-gauntlet\run1\scorecard.md
#     D:\CLAUDE_JUNK\ck-gauntlet\run1\<clip-id>\f{1,2,3}\ (session + matte PNGs)
#
# DEPENDS ON: numpy, cv2, subprocess, fusion_v2/gauntlet_clips.json, ae_processor.py
# AFFECTS: writes to D:\CLAUDE_JUNK\ck-gauntlet\run1\ only
# ISOLATED: yes — all writes go to the run1 output dir, no source modification

import sys
import os
import json
import time
import traceback
import subprocess
import shutil
from pathlib import Path

import numpy as np
import cv2

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CK_ROOT   = Path(__file__).resolve().parent.parent
VENV_PY   = CK_ROOT / ".venv" / "Scripts" / "python.exe"
AE_PROC   = CK_ROOT / "ae_plugin" / "cep_panel" / "ae_processor.py"
AE_DIR    = AE_PROC.parent
CLIPS_JSON = Path(__file__).resolve().parent / "gauntlet_clips.json"
OUT_ROOT  = Path(r"D:\CLAUDE_JUNK\ck-gauntlet\run1")  # overridden by --out at runtime

# ---------------------------------------------------------------------------
# Base settings (shared across all clips; clip-specific overrides added inline)
# ---------------------------------------------------------------------------

_BASE = {
    "screenType":        "green",
    "refiner":           1.0,
    "despill":           0,
    "despeckle":         False,
    "despeckleSize":     400,
    "choke":             0,
    "sam2_margin":       0,
    "sam_sidecar_margin": 10,
    "sam2_soften":       0,
    "fill_holes":        0,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _write_params(params, path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(params), encoding="utf-8")
    return str(p)


def _run(cmd, desc="", timeout=600):
    """Run ae_processor.py subcommand in AE_DIR. Returns (ok, combined_log)."""
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(AE_DIR)
        )
        log = (res.stdout or "") + (res.stderr or "")
        ok = (res.returncode == 0)
        if not ok:
            _log(f"  FAIL ({desc}) rc={res.returncode}: {log[-400:]}")
        return ok, log
    except subprocess.TimeoutExpired:
        _log(f"  TIMEOUT ({desc}) after {timeout}s")
        return False, f"TIMEOUT after {timeout}s"
    except Exception as exc:
        _log(f"  ERROR ({desc}): {exc}")
        return False, str(exc)


def _probe_video(path):
    """Return (total_frames, fps, width, height) or None."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return None
    fps    = cap.get(cv2.CAP_PROP_FPS) or 24.0
    total  = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w      = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h      = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    if total < 3 or fps <= 0:
        return None
    return total, fps, w, h


def _derive_sam_dots(sess_dir):
    """
    Derive SAM positive dots from cached alpha.png.
    Returns (pos_points, neg_points, note_or_None).
    Points are [[x,y], ...] in pixel coords of the cached frame.
    Strategy: find the largest fg blob, return 4 dots:
      centroid, topmost pixel (head), two bottom extremes (feet).
    If blob covers >60% of frame (junk-heavy) -> centroid only.
    """
    alpha_path = Path(sess_dir) / "alpha.png"
    alpha_raw = cv2.imread(str(alpha_path), cv2.IMREAD_UNCHANGED)
    if alpha_raw is None:
        return None, [], "alpha.png unreadable"

    alpha = alpha_raw.astype(np.float32)
    alpha /= (65535.0 if alpha_raw.dtype == np.uint16 else 255.0)
    if alpha.ndim == 3:
        alpha = alpha[:, :, 0]

    H, W = alpha.shape
    frame_px = H * W

    binary = (alpha > 0.5).astype(np.uint8)
    n_lbl, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, connectivity=8)

    if n_lbl < 2:
        return None, [], "no FG blob found in NN alpha"

    # Largest blob by area (skip label 0 = background)
    areas     = stats[1:, cv2.CC_STAT_AREA]
    best_idx  = int(np.argmax(areas)) + 1
    best_area = int(areas[best_idx - 1])

    cx = int(round(centroids[best_idx][0]))
    cy = int(round(centroids[best_idx][1]))

    if best_area > frame_px * 0.60:
        return [[cx, cy]], [], f"junk-heavy ({best_area}/{frame_px}={best_area/frame_px:.1%}) — centroid only"

    blob_mask = (labels == best_idx)

    # Topmost row: pick the column midpoint of that row
    rows_with_fg = np.where(blob_mask.any(axis=1))[0]
    if len(rows_with_fg) == 0:
        return [[cx, cy]], [], "blob mask empty after component"

    top_row  = int(rows_with_fg[0])
    top_cols = np.where(blob_mask[top_row, :])[0]
    top_x    = int(top_cols[len(top_cols) // 2])
    head_pt  = [top_x, top_row]

    # Bottom row extremes (feet)
    bot_row  = int(rows_with_fg[-1])
    bot_cols = np.where(blob_mask[bot_row, :])[0]
    foot_l   = [int(bot_cols[0]),  bot_row]
    foot_r   = [int(bot_cols[-1]), bot_row]

    return [[cx, cy], head_pt, foot_l, foot_r], [], None


# ---------------------------------------------------------------------------
# Metrics helpers
# ---------------------------------------------------------------------------

def _bh_from_gate(gate_path):
    """Approximate bbox height from SAM gate."""
    g = cv2.imread(str(gate_path), cv2.IMREAD_UNCHANGED)
    if g is None:
        return 200
    gf = g.astype(np.float32) / (65535.0 if g.dtype == np.uint16 else 255.0)
    if gf.ndim == 3:
        gf = gf[:, :, 0]
    rows = np.where(gf.any(axis=1))[0]
    return max(50, int(rows[-1] - rows[0] + 1) if len(rows) >= 2 else gf.shape[0])


def _junk_pixels(matte_path, sam_gate_path, bh):
    """Alpha>0.7 pixels with SAM-distance > 5% bh (float(-1) on error)."""
    try:
        m = cv2.imread(str(matte_path), cv2.IMREAD_GRAYSCALE)
        if m is None:
            return -1
        alpha = m.astype(np.float32) / 255.0

        g = cv2.imread(str(sam_gate_path), cv2.IMREAD_UNCHANGED)
        if g is None:
            return -1
        sam = g.astype(np.float32) / (65535.0 if g.dtype == np.uint16 else 255.0)
        if sam.ndim == 3:
            sam = sam[:, :, 0]
        if sam.shape != alpha.shape:
            sam = cv2.resize(sam, (alpha.shape[1], alpha.shape[0]), interpolation=cv2.INTER_LINEAR)

        thresh_px = max(1, int(bh * 0.05))
        sam_bin   = (sam > 0.5).astype(np.uint8)
        dist      = cv2.distanceTransform(1 - sam_bin, cv2.DIST_L2, 5)
        return int(np.sum((alpha > 0.7) & (dist > thresh_px)))
    except Exception:
        return -1


def _edge_energy(matte_path):
    """Sum of |Sobel| gradient on matte (higher = more edge detail)."""
    try:
        m = cv2.imread(str(matte_path), cv2.IMREAD_GRAYSCALE)
        if m is None:
            return -1.0
        sx = cv2.Sobel(m, cv2.CV_32F, 1, 0, ksize=3)
        sy = cv2.Sobel(m, cv2.CV_32F, 0, 1, ksize=3)
        return float(np.sqrt(sx ** 2 + sy ** 2).sum())
    except Exception:
        return -1.0


# ---------------------------------------------------------------------------
# Contact sheet builder
# ---------------------------------------------------------------------------

def _build_contact_sheet(rows_data, out_path):
    """
    rows_data: list of (clip_id, [
        (label, img_path_or_None),  # 6 tiles per clip: f1old, f1new, f2old, f2new, f3old, f3new
        ...
    ])
    """
    TILE_W   = 420
    LABEL_H  = 26
    FONT     = cv2.FONT_HERSHEY_SIMPLEX
    FSCALE   = 0.38
    FTHICK   = 1
    CLR_NEW  = (0, 200, 80)
    CLR_OLD  = (160, 160, 160)
    CLR_ERR  = (0, 80, 200)

    sheet_rows = []
    for clip_id, tiles in rows_data:
        row_tiles = []
        for label, img_path in tiles:
            # Load tile
            if img_path and Path(img_path).exists():
                img = cv2.imread(str(img_path))
                if img is None:
                    img = np.zeros((240, TILE_W, 3), np.uint8)
                    cv2.putText(img, "READ ERR", (6, 120), FONT, FSCALE, CLR_ERR, FTHICK, cv2.LINE_AA)
            else:
                img = np.zeros((240, TILE_W, 3), np.uint8)
                cv2.putText(img, "SKIPPED", (6, 120), FONT, FSCALE, (100, 100, 0), FTHICK, cv2.LINE_AA)

            # Scale to TILE_W preserving aspect
            h_orig, w_orig = img.shape[:2]
            th = int(round(h_orig * TILE_W / w_orig)) if w_orig > 0 else 240
            img = cv2.resize(img, (TILE_W, th), interpolation=cv2.INTER_AREA)

            # Label bar (colored by OLD/NEW)
            bar = np.zeros((LABEL_H, TILE_W, 3), np.uint8)
            if "NEW" in label:
                bar[:, :] = (0, 30, 0)
                tc = CLR_NEW
            elif "OLD" in label:
                bar[:, :] = (20, 20, 20)
                tc = CLR_OLD
            else:
                bar[:, :] = (30, 0, 30)
                tc = (180, 80, 180)
            cv2.putText(bar, label[:52], (4, 18), FONT, FSCALE, tc, FTHICK, cv2.LINE_AA)

            row_tiles.append(np.vstack([bar, img]))

        # Pad tiles in this row to the same height
        max_h = max(t.shape[0] for t in row_tiles)
        padded = []
        for t in row_tiles:
            dh = max_h - t.shape[0]
            if dh > 0:
                pad = np.zeros((dh, t.shape[1], 3), np.uint8)
                t = np.vstack([t, pad])
            padded.append(t)
        sheet_rows.append(np.hstack(padded))

    # Pad rows to same width
    max_w = max(r.shape[1] for r in sheet_rows)
    final_rows = []
    for r in sheet_rows:
        dw = max_w - r.shape[1]
        if dw > 0:
            r = np.hstack([r, np.zeros((r.shape[0], dw, 3), np.uint8)])
        final_rows.append(r)

    sheet = np.vstack(final_rows)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ok = cv2.imwrite(str(out_path), sheet)
    return ok


# ---------------------------------------------------------------------------
# Per-frame pipeline
# ---------------------------------------------------------------------------

def _run_frame(clip_id, clip_path, time_sec, frame_label, sess_dir, need_downscale):
    """
    Extract + cache + SAM + 2x postproc for one frame.
    Returns dict with paths to matte_new, matte_old, gate, and notes.
    On any fatal step, returns partial dict with error key.
    """
    sess_dir = Path(sess_dir)
    sess_dir.mkdir(parents=True, exist_ok=True)

    result = {
        "clip_id":    clip_id,
        "label":      frame_label,
        "time_sec":   time_sec,
        "matte_new":  None,
        "matte_old":  None,
        "gate":       None,
        "error":      None,
        "sam_note":   None,
    }

    # ---- 1. Extract frame ----
    frame_png = sess_dir / "frame.png"
    params_extract = {}  # extract takes no --params
    ok, _ = _run([
        str(VENV_PY), str(AE_PROC),
        "extract", str(clip_path), str(frame_png),
        "--time", str(time_sec),
    ], f"extract {clip_id}/{frame_label}", timeout=120)
    if not ok or not frame_png.exists():
        result["error"] = "extract failed"
        return result
    _log(f"  extract OK  ({time_sec:.1f}s)  -> {frame_png.name}")

    # ---- 1b. Downscale if 6K ----
    if need_downscale:
        img_raw = cv2.imread(str(frame_png), cv2.IMREAD_UNCHANGED)
        if img_raw is not None and img_raw.shape[1] > 4096:
            new_w = img_raw.shape[1] // 2
            new_h = img_raw.shape[0] // 2
            img_half = cv2.resize(img_raw, (new_w, new_h), interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(frame_png), img_half)
            _log(f"  halved to {new_w}x{new_h}")

    # ---- 2. Cache (NN alpha) ----
    cache_params = dict(_BASE)
    params_cache = _write_params(cache_params, sess_dir / "params_cache.json")
    ok, _ = _run([
        str(VENV_PY), str(AE_PROC),
        "cache", str(frame_png), str(sess_dir),
        "--params", params_cache,
    ], f"cache {clip_id}/{frame_label}", timeout=300)
    if not ok or not (sess_dir / "alpha.png").exists():
        result["error"] = "cache failed"
        return result
    _log(f"  cache  OK")

    # ---- 3. Auto-derive SAM dots ----
    pos_pts, neg_pts, sam_note = _derive_sam_dots(sess_dir)
    result["sam_note"] = sam_note
    if pos_pts is None:
        result["error"] = f"SAM dot derivation failed: {sam_note}"
        # Try cache-only postproc without SAM (no fusion_v2 path available)
        _log(f"  SAM dot derive FAILED: {sam_note} -- skipping SAM apply")
    else:
        _log(f"  SAM dots: {len(pos_pts)} positive ({sam_note or 'ok'})")

        # ---- 4. SAM apply ----
        sam_params = dict(_BASE)
        sam_params["sam_positive"] = pos_pts
        sam_params["sam_negative"] = neg_pts or []
        params_sam = _write_params(sam_params, sess_dir / "params_sam.json")
        ok, _ = _run([
            str(VENV_PY), str(AE_PROC),
            "sam-apply", str(sess_dir),
            "--params", params_sam,
        ], f"sam-apply {clip_id}/{frame_label}", timeout=300)
        if not ok or not (sess_dir / "sam2_gate_raw.png").exists():
            _log(f"  SAM apply FAILED — postproc will run standard path only")
        else:
            _log(f"  SAM    OK  -> sam2_gate_raw.png")
            result["gate"] = str(sess_dir / "sam2_gate_raw.png")

    # ---- 5a. Postproc NEW (fusion_v2) ----
    matte_new = sess_dir / "matte_new.png"
    pp_new_params = dict(_BASE)
    pp_new_params["fusion_v2"]     = True
    pp_new_params["fusion_expand"] = 6
    if pos_pts is not None and result["gate"]:
        pp_new_params["sam_positive"] = pos_pts
        pp_new_params["sam_negative"] = neg_pts or []
    params_new = _write_params(pp_new_params, sess_dir / "params_new.json")
    ok, _ = _run([
        str(VENV_PY), str(AE_PROC),
        "postproc", str(sess_dir), str(matte_new),
        "--params", params_new,
        "--background", "matte",
    ], f"postproc-new {clip_id}/{frame_label}", timeout=180)
    if ok and matte_new.exists():
        result["matte_new"] = str(matte_new)
        _log(f"  postproc NEW OK")
    else:
        _log(f"  postproc NEW FAILED")

    # ---- 5b. Postproc OLD (standard) ----
    matte_old = sess_dir / "matte_old.png"
    pp_old_params = dict(_BASE)
    pp_old_params["fusion_v2"] = False
    if pos_pts is not None and result["gate"]:
        pp_old_params["sam_positive"] = pos_pts
        pp_old_params["sam_negative"] = neg_pts or []
    params_old = _write_params(pp_old_params, sess_dir / "params_old.json")
    ok, _ = _run([
        str(VENV_PY), str(AE_PROC),
        "postproc", str(sess_dir), str(matte_old),
        "--params", params_old,
        "--background", "matte",
    ], f"postproc-old {clip_id}/{frame_label}", timeout=180)
    if ok and matte_old.exists():
        result["matte_old"] = str(matte_old)
        _log(f"  postproc OLD OK")
    else:
        _log(f"  postproc OLD FAILED")

    return result


# ---------------------------------------------------------------------------
# Main gauntlet loop
# ---------------------------------------------------------------------------

def run_gauntlet():
    t_start = time.time()
    clips = json.loads(CLIPS_JSON.read_text(encoding="utf-8"))["clips"]

    _log("=" * 60)
    _log(f"GAUNTLET RUN 1  --  {len(clips)} clips, 3 frames each")
    _log(f"Output: {OUT_ROOT}")
    _log("=" * 60)

    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    all_results  = []   # per-clip list of frame result dicts
    sheet_rows   = []   # for contact sheet
    score_rows   = []   # for scorecard

    for clip in clips:
        clip_id   = clip["id"]
        clip_path = Path(clip["path"])
        clip_why  = clip.get("why", "")

        _log("")
        _log(f"CLIP: {clip_id}")
        _log(f"  {clip_path}")

        if not clip_path.exists():
            _log(f"  MISSING — skipping")
            all_results.append({"clip_id": clip_id, "frames": [], "error": "file not found"})
            # Add blank row to sheet
            blank_tiles = [(f"{clip_id} f{i+1} {v}", None)
                           for i in range(3) for v in ("OLD", "NEW")]
            sheet_rows.append((clip_id, blank_tiles))
            score_rows.append({"clip_id": clip_id, "error": "file not found", "frames": []})
            continue

        # Probe video
        probe = _probe_video(clip_path)
        if probe is None:
            _log(f"  UNREADABLE — skipping")
            all_results.append({"clip_id": clip_id, "frames": [], "error": "probe failed"})
            blank_tiles = [(f"{clip_id} f{i+1} {v}", None)
                           for i in range(3) for v in ("OLD", "NEW")]
            sheet_rows.append((clip_id, blank_tiles))
            score_rows.append({"clip_id": clip_id, "error": "probe failed", "frames": []})
            continue

        total_frames, fps, W_vid, H_vid = probe
        duration_sec = total_frames / fps
        need_downscale = (W_vid > 4096)
        _log(f"  {W_vid}x{H_vid}  fps={fps:.2f}  frames={total_frames}  dur={duration_sec:.1f}s"
             + ("  [WILL HALVE]" if need_downscale else ""))

        # Pick test frames: 25%, 50%, 75% of duration
        test_times = [duration_sec * frac for frac in (0.25, 0.50, 0.75)]

        clip_results = []
        clip_tiles   = []  # 6 entries: f1old, f1new, f2old, f2new, f3old, f3new

        for fi, t_sec in enumerate(test_times, 1):
            label = f"f{fi}"
            sess  = OUT_ROOT / clip_id / label
            _log(f"  [{clip_id}] {label} @ {t_sec:.1f}s ...")

            try:
                r = _run_frame(clip_id, clip_path, t_sec, label, sess, need_downscale)
            except Exception as exc:
                _log(f"  EXCEPTION in {clip_id}/{label}: {exc}")
                traceback.print_exc()
                r = {"clip_id": clip_id, "label": label, "time_sec": t_sec,
                     "matte_new": None, "matte_old": None, "gate": None,
                     "error": str(exc), "sam_note": None}

            clip_results.append(r)

            # Add to sheet tiles (OLD then NEW per frame)
            old_label = f"{clip_id} {label} OLD"
            new_label = f"{clip_id} {label} NEW"
            clip_tiles.append((old_label, r.get("matte_old")))
            clip_tiles.append((new_label, r.get("matte_new")))

        all_results.append({"clip_id": clip_id, "frames": clip_results})
        sheet_rows.append((clip_id, clip_tiles))

        # Per-clip score row
        frame_scores = []
        for r in clip_results:
            gate  = r.get("gate")
            m_new = r.get("matte_new")
            m_old = r.get("matte_old")
            bh    = _bh_from_gate(gate) if gate else 200

            junk_new  = _junk_pixels(m_new, gate, bh) if (m_new and gate) else -1
            junk_old  = _junk_pixels(m_old, gate, bh) if (m_old and gate) else -1
            ee_new    = _edge_energy(m_new) if m_new else -1.0
            ee_old    = _edge_energy(m_old) if m_old else -1.0
            frame_scores.append({
                "label":    r["label"],
                "junk_new": junk_new,
                "junk_old": junk_old,
                "ee_new":   ee_new,
                "ee_old":   ee_old,
                "error":    r.get("error"),
                "sam_note": r.get("sam_note"),
            })

        score_rows.append({
            "clip_id": clip_id,
            "why":     clip_why,
            "frames":  frame_scores,
            "error":   None,
        })

    # ---- Build contact sheet ----
    _log("")
    _log("Building contact sheet...")
    run_name = OUT_ROOT.name
    sheet_path = OUT_ROOT / f"contact_sheet_{run_name}.png"
    ok = _build_contact_sheet(sheet_rows, sheet_path)
    _log(f"  {'OK' if ok else 'FAILED'}: {sheet_path}")

    # ---- Write scorecard.md ----
    sc_path = OUT_ROOT / "scorecard.md"
    _write_scorecard(score_rows, sc_path)
    _log(f"  scorecard: {sc_path}")

    elapsed = time.time() - t_start
    _log("")
    _log("=" * 60)
    _log(f"DONE in {elapsed:.0f}s  ({elapsed/60:.1f} min)")
    _log(f"SHEET  : {sheet_path}")
    _log(f"SCORES : {sc_path}")
    _log("=" * 60)

    # Print per-clip status table
    _print_status(score_rows, elapsed)

    return all_results, sheet_path, sc_path


# ---------------------------------------------------------------------------
# Scorecard writer
# ---------------------------------------------------------------------------

def _write_scorecard(score_rows, out_path):
    lines = [
        "# Gauntlet Run 1 — Scorecard",
        "",
        "> **NOTE**: No reference mattes exist. Junk-px and edge-energy are proxies only.",
        "> Junk-px = alpha>0.7 outside SAM boundary (>5% bh). Lower = cleaner.",
        "> Edge energy = sum |Sobel| on matte. Higher NEW/OLD ratio = more detail preserved.",
        "> **Berto's eye is the authoritative verdict.** Compare the contact_sheet PNG in this run folder.",
        "",
        "---",
        "",
    ]
    for sr in score_rows:
        cid = sr["clip_id"]
        lines.append(f"## {cid}")
        lines.append("")
        if sr.get("error"):
            lines.append(f"**ERROR**: {sr['error']}")
            lines.append("")
            continue
        if sr.get("why"):
            lines.append(f"*{sr['why']}*")
            lines.append("")
        lines.append("| Frame | Junk-px OLD | Junk-px NEW | EE OLD | EE NEW | EE NEW/OLD | Notes |")
        lines.append("|-------|------------|------------|--------|--------|-----------|-------|")
        for fs in sr.get("frames", []):
            jold = fs["junk_old"]
            jnew = fs["junk_new"]
            eold = fs["ee_old"]
            enew = fs["ee_new"]
            ratio = f"{enew/eold:.3f}" if (eold > 0 and enew >= 0) else "n/a"
            notes = ""
            if fs.get("error"):
                notes = f"ERR: {fs['error']}"
            elif fs.get("sam_note"):
                notes = fs["sam_note"]
            lines.append(
                f"| {fs['label']} "
                f"| {jold if jold >= 0 else 'n/a'} "
                f"| {jnew if jnew >= 0 else 'n/a'} "
                f"| {eold:.0f} "
                f"| {enew:.0f} "
                f"| {ratio} "
                f"| {notes} |"
            )
        lines.append("")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Status table
# ---------------------------------------------------------------------------

def _print_status(score_rows, elapsed):
    print()
    print(f"{'CLIP':<20} {'F1':>4} {'F2':>4} {'F3':>4}  {'J_NEW-OLD':>10}  NOTES")
    print("-" * 70)
    for sr in score_rows:
        cid = sr["clip_id"]
        if sr.get("error"):
            print(f"{cid:<20}  {'ERR':>4}  {'ERR':>4}  {'ERR':>4}  {sr['error']}")
            continue
        statuses = []
        junk_diffs = []
        notes = []
        for fs in sr.get("frames", []):
            if fs.get("error"):
                statuses.append("ERR")
                notes.append(fs["error"][:20])
            else:
                statuses.append("OK ")
                if fs["junk_old"] >= 0 and fs["junk_new"] >= 0:
                    junk_diffs.append(fs["junk_new"] - fs["junk_old"])
                if fs.get("sam_note"):
                    notes.append(fs["sam_note"][:20])
        while len(statuses) < 3:
            statuses.append("---")
        jd_str = f"{sum(junk_diffs):+d}" if junk_diffs else "n/a"
        note_str = "; ".join(set(notes))[:30]
        print(f"{cid:<20}  {statuses[0]:>4}  {statuses[1]:>4}  {statuses[2]:>4}  {jd_str:>10}  {note_str}")
    print("-" * 70)
    print(f"Total runtime: {elapsed:.0f}s ({elapsed/60:.1f} min)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse as _ap
    _p = _ap.ArgumentParser(description="Gauntlet runner")
    _p.add_argument("--out", default=None,
                    help="Output root dir (default: run1 path)")
    _args, _ = _p.parse_known_args()
    if _args.out:
        OUT_ROOT = Path(_args.out)
    run_gauntlet()
