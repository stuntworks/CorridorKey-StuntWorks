/**
 * CorridorKey — Host Script (ExtendScript)
 * Last modified: 2026-04-14 | Change: Remove all shell exec + all eval(). Python now
 *   runs from the Node.js panel side (index.html). ExtendScript is pure timeline code.
 *
 * WHAT IT DOES: Reads timeline state from After Effects / Premiere Pro and returns it
 *   to the CEP panel as a JSON string. Imports the PNG(s) Python produced back onto the
 *   timeline. Does NOT spawn Python, does NOT run shell commands, does NOT eval() any
 *   inbound string. All untrusted inputs arrive as separate function arguments.
 *
 * DEPENDS-ON: AE CompItem / Premiere Sequence scripting APIs.
 * AFFECTS: Timeline (adds layers / clips), Project Panel (imports files).
 */

// ============================================================
// SAFE JSON STRINGIFY (no parse — we never eval inbound strings)
// ============================================================
// WHAT IT DOES: Minimal JSON.stringify implementation for ExtendScript, which has no native
//   JSON object. We deliberately do NOT ship a JSON.parse — all inbound strings from the
//   panel arrive as function arguments, not as JSON payloads to be parsed.
// DEPENDS-ON: nothing.
// AFFECTS: Defines JSON.stringify globally.
(function() {
    if (typeof JSON === "undefined") JSON = {};
    if (typeof JSON.stringify === "undefined") {
        JSON.stringify = function(obj) {
            if (obj === null) return "null";
            if (typeof obj === "undefined") return undefined;
            if (typeof obj === "number") return isFinite(obj) ? String(obj) : "null";
            if (typeof obj === "boolean") return String(obj);
            if (typeof obj === "string") {
                return '"' + obj
                    .replace(/\\/g, "\\\\").replace(/"/g, '\\"')
                    .replace(/\n/g, "\\n").replace(/\r/g, "\\r").replace(/\t/g, "\\t") + '"';
            }
            if (obj instanceof Array) {
                var arr = [];
                for (var i = 0; i < obj.length; i++) {
                    var v = JSON.stringify(obj[i]);
                    arr.push(v === undefined ? "null" : v);
                }
                return "[" + arr.join(",") + "]";
            }
            if (typeof obj === "object") {
                var pairs = [];
                for (var k in obj) {
                    if (obj.hasOwnProperty(k)) {
                        var vv = JSON.stringify(obj[k]);
                        if (vv !== undefined) pairs.push(JSON.stringify(String(k)) + ":" + vv);
                    }
                }
                return "{" + pairs.join(",") + "}";
            }
            return undefined;
        };
    }
})();

// ============================================================
// HOST DETECTION
// ============================================================
// WHAT IT DOES: Returns "ae" / "ppro" / "unknown" so the panel routes to the right code path.
function getHostApp() {
    if (typeof CompItem !== "undefined") return "ae";
    if (typeof app !== "undefined" && app.project && app.project.activeSequence !== undefined) return "ppro";
    return "unknown";
}

// ============================================================
// AFTER EFFECTS — read-only introspection
// ============================================================

// WHAT IT DOES: Returns the state the panel needs to process the current frame: source file
//   path, source-media frame number, fps, comp time. All data is emitted as a JSON string.
// DEPENDS-ON: A CompItem is the activeItem; one layer with a file source is selected.
// AFFECTS: Read-only.
function ae_getFrameInfo() {
    try {
        var comp = app.project.activeItem;
        if (!(comp instanceof CompItem)) return JSON.stringify({ ok: false, error: "No composition selected" });
        var layer = comp.selectedLayers[0];
        if (!layer) return JSON.stringify({ ok: false, error: "No layer selected" });
        if (!layer.source || !layer.source.file) return JSON.stringify({ ok: false, error: "Selected layer has no source file" });

        var fps = 1.0 / comp.frameDuration;
        // AE reports comp.time at the END of the current frame — subtract one frame
        // to match what the user is looking at (same fix applied to ppro_getFrameInfo).
        var sourceTime = comp.time - layer.startTime - comp.frameDuration;
        if (sourceTime < 0) sourceTime = 0;
        var sourceFrame = Math.round(sourceTime * fps);
        if (sourceFrame < 0) sourceFrame = 0;

        return JSON.stringify({
            ok: true,
            sourceFile: layer.source.file.fsName,
            sourceFrame: sourceFrame,
            sourceTimeSeconds: sourceTime,
            fps: fps,
            compTime: comp.time,
            frameDuration: comp.frameDuration
        });
    } catch (e) { return JSON.stringify({ ok: false, error: String(e) }); }
}

// WHAT IT DOES: Returns the work-area range mapped to source-media frame numbers.
// DEPENDS-ON: A CompItem with a selected file-backed layer and a work area set.
// AFFECTS: Read-only.
function ae_getWorkAreaInfo() {
    try {
        var comp = app.project.activeItem;
        if (!(comp instanceof CompItem)) return JSON.stringify({ ok: false, error: "No composition selected" });
        var layer = comp.selectedLayers[0];
        if (!layer) return JSON.stringify({ ok: false, error: "No layer selected" });
        if (!layer.source || !layer.source.file) return JSON.stringify({ ok: false, error: "Selected layer has no source file" });

        var fps = 1.0 / comp.frameDuration;
        var startTime = comp.workAreaStart;
        var duration = comp.workAreaDuration;
        var endTime = startTime + duration;
        var sourceStartTime = startTime - layer.startTime;
        var sourceEndTime = endTime - layer.startTime;
        var startFrame = Math.round(sourceStartTime * fps);
        var endFrame = Math.round(sourceEndTime * fps);
        if (startFrame < 0) startFrame = 0;
        if (endFrame <= startFrame) return JSON.stringify({ ok: false, error: "Invalid frame range" });

        return JSON.stringify({
            ok: true,
            sourceFile: layer.source.file.fsName,
            startFrame: startFrame,
            endFrame: endFrame,
            fps: fps,
            compStartTime: startTime
        });
    } catch (e) { return JSON.stringify({ ok: false, error: String(e) }); }
}

// ============================================================
// AFTER EFFECTS — timeline mutators
// ============================================================

// WHAT IT DOES: Imports a single PNG produced by Python above the currently selected layer,
//   trimmed to one frame at the current comp time.
// DEPENDS-ON: outputPath exists on disk (panel pre-verifies), comp still active.
// AFFECTS: Adds ImportItem + Layer.
function ae_importFrame(outputPath) {
    try {
        var comp = app.project.activeItem;
        if (!(comp instanceof CompItem)) return JSON.stringify({ ok: false, error: "No composition selected" });
        var layer = comp.selectedLayers[0];
        if (!layer) return JSON.stringify({ ok: false, error: "No layer selected" });
        var outputFile = new File(outputPath);
        if (!outputFile.exists) return JSON.stringify({ ok: false, error: "Output file not found: " + outputPath });

        app.beginUndoGroup("CorridorKey Frame");
        var importedFile = app.project.importFile(new ImportOptions(outputFile));
        if (importedFile) {
            var newLayer = comp.layers.add(importedFile);
            newLayer.moveBefore(layer);
            newLayer.startTime = comp.time;
            newLayer.outPoint = comp.time + comp.frameDuration;
        }
        app.endUndoGroup();
        comp.time = comp.time; // force UI refresh
        return JSON.stringify({ ok: true });
    } catch (e) { return JSON.stringify({ ok: false, error: String(e) }); }
}

// WHAT IT DOES: Imports a PNG sequence produced by batch Python and lays it on the track
//   above the source layer at the work-area start time.
// DEPENDS-ON: firstFramePath exists and is the first PNG of a numbered sequence.
// AFFECTS: Adds ImportItem + Layer.
// WHAT IT DOES: Imports the keyed PNG sequence above the source layer, then optionally
//   hides the source layer so the keyed result is immediately visible.
// DEPENDS-ON: firstFramePath exists; comp has a selected layer (the original source clip).
// AFFECTS: Adds a new layer to the comp; optionally sets source layer.enabled = false.
function ae_importSequence(firstFramePath, fps, compStartTime, hideSource) {
    try {
        var comp = app.project.activeItem;
        if (!(comp instanceof CompItem)) return JSON.stringify({ ok: false, error: "No composition selected" });
        var layer = comp.selectedLayers[0];
        if (!layer) return JSON.stringify({ ok: false, error: "No layer selected" });
        var firstFrame = new File(firstFramePath);
        if (!firstFrame.exists) return JSON.stringify({ ok: false, error: "First frame not found: " + firstFramePath });

        app.beginUndoGroup("CorridorKey Batch");
        var importOptions = new ImportOptions(firstFrame);
        importOptions.sequence = true;
        var importedSeq = app.project.importFile(importOptions);
        if (importedSeq) {
            importedSeq.mainSource.conformFrameRate = Number(fps);
            var newLayer = comp.layers.add(importedSeq);
            newLayer.moveBefore(layer);
            newLayer.startTime = Number(compStartTime);
        }
        if (String(hideSource) === 'true') {
            layer.enabled = false;
        }
        app.endUndoGroup();
        comp.time = comp.time;
        return JSON.stringify({ ok: true });
    } catch (e) { return JSON.stringify({ ok: false, error: String(e) }); }
}

// ============================================================
// PREMIERE PRO — read-only introspection
// ============================================================

// WHAT IT DOES: Returns clip-at-playhead info: file path, source-media frame number, fps.
// DEPENDS-ON: An active sequence with a clip under the playhead on track 1.
// AFFECTS: Read-only.
// NOTE: Premiere reports the NEXT frame boundary for playerPos — we offset by -1 to match.
function ppro_getFrameInfo() {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return JSON.stringify({ ok: false, error: "No active sequence" });

        // Premiere's videoFrameRate can return a Time object, a string, or a number
        // depending on version. parseFloat handles most; if it still fails, fall back
        // to 24 rather than erroring — a wrong-by-a-bit fps beats a dead button.
        var fps = parseFloat(seq.getSettings().videoFrameRate);
        if (isNaN(fps) || fps <= 0) fps = 24;

        var playerPos = seq.getPlayerPosition();
        var videoTracks = seq.videoTracks;
        if (videoTracks.numTracks < 1) return JSON.stringify({ ok: false, error: "No video tracks" });

        var track = videoTracks[0];
        var clips = track.clips;
        var targetClip = null;
        for (var i = 0; i < clips.numItems; i++) {
            var c = clips[i];
            if (playerPos.ticks >= c.start.ticks && playerPos.ticks < c.end.ticks) {
                targetClip = c; break;
            }
        }
        if (!targetClip) return JSON.stringify({ ok: false, error: "No clip at playhead on Track 1" });

        var filePath = targetClip.projectItem.getMediaPath();
        if (!filePath) return JSON.stringify({ ok: false, error: "Cannot get source file path" });

        // Source-media TIME in seconds. Subtract one sequence-frame's worth because
        // Premiere's playerPos reports the NEXT frame boundary. Python seeks by
        // CAP_PROP_POS_MSEC (accurate across long-GOP codecs + fps mismatches),
        // not by frame number.
        var sourceTimeSec = playerPos.seconds - targetClip.start.seconds + targetClip.inPoint.seconds;
        sourceTimeSec = sourceTimeSec - (1.0 / fps);
        if (sourceTimeSec < 0) sourceTimeSec = 0;

        return JSON.stringify({
            ok: true,
            sourceFile: filePath,
            sourceTimeSeconds: sourceTimeSec,
            fps: fps,
            playheadSeconds: playerPos.seconds
        });
    } catch (e) { return JSON.stringify({ ok: false, error: String(e) }); }
}

// WHAT IT DOES: Returns the in/out range of the sequence mapped to source-media frames.
// DEPENDS-ON: In/out points set on an active sequence with clips on track 1.
function ppro_getInOutInfo() {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return JSON.stringify({ ok: false, error: "No active sequence" });

        // Premiere's videoFrameRate can return a Time object, a string, or a number
        // depending on version. parseFloat handles most; if it still fails, fall back
        // to 24 rather than erroring — a wrong-by-a-bit fps beats a dead button.
        var fps = parseFloat(seq.getSettings().videoFrameRate);
        if (isNaN(fps) || fps <= 0) fps = 24;

        // Preferred: sequence in/out markers.
        // Fallback: the clip the playhead sits on (or the first clip on V1) — we use
        // its own in/out trim to decide what source-media frame range to key. This
        // lets the user click PROCESS IN/OUT RANGE without first setting timeline
        // markers, which was the behavior before the rewrite and the one Berto
        // expects.
        var track = seq.videoTracks[0];
        if (track.clips.numItems < 1) return JSON.stringify({ ok: false, error: "No clips on Track 1" });

        var inPoint = seq.getInPointAsTime();
        var outPoint = seq.getOutPointAsTime();
        var haveSeqRange = inPoint && outPoint && inPoint.seconds < outPoint.seconds;

        var sourceClip = null;
        if (haveSeqRange) {
            // Find a clip overlapping the marker range (prefer the one at inPoint).
            for (var i = 0; i < track.clips.numItems; i++) {
                var c = track.clips[i];
                if (inPoint.seconds >= c.start.seconds && inPoint.seconds < c.end.seconds) {
                    sourceClip = c; break;
                }
            }
            if (!sourceClip) sourceClip = track.clips[0];
        } else {
            // No markers — use the clip under the playhead, or the first clip.
            var playheadSec = seq.getPlayerPosition().seconds;
            for (var j = 0; j < track.clips.numItems; j++) {
                var cc = track.clips[j];
                if (playheadSec >= cc.start.seconds && playheadSec < cc.end.seconds) {
                    sourceClip = cc; break;
                }
            }
            if (!sourceClip) sourceClip = track.clips[0];
        }

        var filePath = sourceClip.projectItem.getMediaPath();
        if (!filePath) return JSON.stringify({ ok: false, error: "Cannot get source file path" });

        // Capture V1's footage-interpretation frame rate. We will apply this exact rate
        // to the imported PNG sequence so Premiere conforms V1 and V2 identically —
        // without this, V2 drifts because Premiere defaults numbered-stills imports to
        // the project fps, which may not match V1's native rate.
        var sourceFrameRate = 0;
        try {
            var fi = sourceClip.projectItem.getFootageInterpretation();
            if (fi && fi.frameRate) sourceFrameRate = Number(fi.frameRate);
        } catch (_) {}
        if (!sourceFrameRate || sourceFrameRate <= 0) sourceFrameRate = fps;

        // Compute source-media TIME range in SECONDS. We do not convert to frames here
        // because the sequence fps can differ from the source clip's native fps —
        // converting on the JSX side with the wrong fps causes drift across the batch.
        // Python opens the video, reads its native fps via cv2.CAP_PROP_FPS, and seeks
        // with cv2.CAP_PROP_POS_MSEC. No drift because no mismatched conversion.
        var rangeStartSec, rangeEndSec, timelineInSec;
        if (haveSeqRange) {
            rangeStartSec = inPoint.seconds - sourceClip.start.seconds + sourceClip.inPoint.seconds;
            rangeEndSec   = outPoint.seconds - sourceClip.start.seconds + sourceClip.inPoint.seconds;
            timelineInSec = inPoint.seconds;
        } else {
            rangeStartSec = sourceClip.inPoint.seconds;
            rangeEndSec   = sourceClip.outPoint.seconds;
            timelineInSec = sourceClip.start.seconds;
        }
        if (rangeEndSec <= rangeStartSec) return JSON.stringify({ ok: false, error: "Invalid range" });

        return JSON.stringify({
            ok: true,
            sourceFile: filePath,
            startSeconds: rangeStartSec,
            endSeconds: rangeEndSec,
            fps: fps,
            sourceFrameRate: sourceFrameRate,
            inPointSeconds: timelineInSec,
            usedSeqMarkers: haveSeqRange
        });
    } catch (e) { return JSON.stringify({ ok: false, error: String(e) }); }
}

// ============================================================
// PREMIERE PRO — timeline mutators
// ============================================================

// WHAT IT DOES: Imports a single keyed PNG into the "CorridorKey" bin, TRIMS the project
//   item's in/out to exactly one frame so overwriteClip doesn't drop Premiere's default
//   5-second still duration, then places it on V2 at the playhead.
// DEPENDS-ON: outputPath exists; active sequence has >=2 video tracks (created if not).
// AFFECTS: Project panel (import + in/out trim), timeline V2 (overwriteClip).
// NOTE: Placement is AT playhead (no +1 nudge). The -1 frame offset in ppro_getFrameInfo
//   already compensated for Premiere's next-frame-boundary reporting — nudging placement
//   too would stack a second offset and land one frame off.
function ppro_importFrame(outputPath, playheadSeconds, fps) {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return JSON.stringify({ ok: false, error: "No active sequence" });

        var outputFile = new File(outputPath);
        if (!outputFile.exists) return JSON.stringify({ ok: false, error: "Output file not found: " + outputPath });

        // Diff root children before/after so we find the new item even when Premiere
        // ignores the targetBin argument for still imports.
        var root = app.project.rootItem;
        var beforeIds = {};
        for (var i = 0; i < root.children.numItems; i++) {
            var ch = root.children[i];
            beforeIds[ch.nodeId || String(i) + "-" + ch.name] = true;
        }

        var ok = app.project.importFiles([outputPath], true, root, false);
        if (!ok) return JSON.stringify({ ok: false, error: "Import failed" });

        var imported = null;
        for (var j = 0; j < root.children.numItems; j++) {
            var cj = root.children[j];
            var id = cj.nodeId || String(j) + "-" + cj.name;
            if (!beforeIds[id]) { imported = cj; break; }
        }
        if (!imported) return JSON.stringify({ ok: false, error: "Imported item not found after diff" });

        // Trim the still to exactly one frame of video. Premiere's default still
        // duration is ~5 seconds which is what made the placed clip span the timeline.
        // Media type 4 = VIDEO per Premiere's ProjectItem API.
        try {
            var oneFrameSec = 1.0 / Number(fps || 24);
            var tIn = new Time(); tIn.seconds = 0;
            var tOut = new Time(); tOut.seconds = oneFrameSec;
            imported.setInPoint(tIn, 4);
            imported.setOutPoint(tOut, 4);
        } catch (trimErr) {
            // If trim fails on this Premiere version, continue anyway — the clip
            // lands but will be longer than one frame and user can trim manually.
        }

        // Move into the CorridorKey bin now that we found it.
        var ckBin = null;
        for (var k = 0; k < root.children.numItems; k++) {
            var kc = root.children[k];
            if (kc.name === "CorridorKey" && kc.type === 2) { ckBin = kc; break; }
        }
        if (!ckBin) { try { ckBin = root.createBin("CorridorKey"); } catch (_) {} }
        if (ckBin) { try { imported.moveBin(ckBin); } catch (_) {} }

        // Place on V2 at exact playhead time.
        if (seq.videoTracks.numTracks < 2) { try { seq.videoTracks.addTracks(1); } catch (_) {} }
        var v2 = seq.videoTracks[1];
        if (!v2) return JSON.stringify({ ok: true, placed: false, note: "Imported but V2 unavailable" });

        var placeSec = Number(playheadSeconds);
        if (isNaN(placeSec) || placeSec < 0) placeSec = 0;
        try {
            v2.overwriteClip(imported, placeSec);
            return JSON.stringify({ ok: true, placed: true });
        } catch (e) {
            return JSON.stringify({ ok: true, placed: false, note: "Imported but overwriteClip failed: " + String(e) });
        }
    } catch (e) { return JSON.stringify({ ok: false, error: String(e) }); }
}

// WHAT IT DOES: Imports a PNG sequence into the ROOT project bin (Premiere's importFiles
//   ignores the targetBin argument for numbered-stills imports in many versions, so we
//   import to root, locate the new item by diffing root.children before/after, then move
//   it into the CorridorKey bin and overwrite onto V2.
// DEPENDS-ON: firstFramePath exists; its folder contains a clean output_NNNNN.png pattern
//   with no other PNG series (mattes live in a subfolder).
// AFFECTS: Project panel (bin + imported item), timeline V2 (overwriteClip).
function ppro_importSequence(firstFramePath, startSeconds, fps, sourceFrameRate) {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return JSON.stringify({ ok: false, error: "No active sequence" });

        var root = app.project.rootItem;

        // Snapshot existing root item IDs so we can find what the import just added.
        var beforeIds = {};
        for (var i = 0; i < root.children.numItems; i++) {
            var child = root.children[i];
            beforeIds[child.nodeId || String(i) + "-" + child.name] = true;
        }

        // Import to ROOT (targetBin arg is flaky for numbered-stills). suppressUI=true,
        // importAsNumberedStills=true so Premiere detects the output_NNNNN.png sequence.
        var ok = app.project.importFiles([firstFramePath], true, root, true);
        if (!ok) return JSON.stringify({ ok: false, error: "importFiles returned false" });

        // Find the newly-added item by diffing against the snapshot.
        var imported = null;
        for (var j = 0; j < root.children.numItems; j++) {
            var cj = root.children[j];
            var id = cj.nodeId || String(j) + "-" + cj.name;
            if (!beforeIds[id]) { imported = cj; break; }
        }
        if (!imported) {
            return JSON.stringify({
                ok: false,
                error: "Import ran but no new project item appeared. Folder: " +
                       (new File(firstFramePath)).parent.fsName
            });
        }

        // Force the imported PNG sequence's footage frame rate to match V1's. Without
        // this, Premiere applies its default (usually the project fps) and V2 drifts
        // relative to V1 whenever the source's native fps differs. Try both APIs —
        // setOverrideFrameRate exists on newer Premieres, getFootageInterpretation +
        // setFootageInterpretation on older ones.
        var appliedRate = 0;
        var targetRate = Number(sourceFrameRate);
        if (!targetRate || isNaN(targetRate) || targetRate <= 0) targetRate = Number(fps || 24);
        try {
            if (typeof imported.setOverrideFrameRate === "function") {
                imported.setOverrideFrameRate(targetRate);
                appliedRate = targetRate;
            } else {
                var fi2 = imported.getFootageInterpretation();
                if (fi2) {
                    fi2.frameRate = targetRate;
                    imported.setFootageInterpretation(fi2);
                    appliedRate = targetRate;
                }
            }
        } catch (_) {
            try {
                var fi3 = imported.getFootageInterpretation();
                if (fi3) {
                    fi3.frameRate = targetRate;
                    imported.setFootageInterpretation(fi3);
                    appliedRate = targetRate;
                }
            } catch (_) {}
        }

        // Move the new item into the CorridorKey bin (create if missing). If the move
        // fails we leave it at the root — still visible to the user.
        var ckBin = null;
        for (var k = 0; k < root.children.numItems; k++) {
            var kc = root.children[k];
            if (kc.name === "CorridorKey" && kc.type === 2) { ckBin = kc; break; }
        }
        if (!ckBin) { try { ckBin = root.createBin("CorridorKey"); } catch (_) {} }
        if (ckBin) { try { imported.moveBin(ckBin); } catch (_) {} }

        // Ensure V2 exists, then overwrite onto it. +1 frame nudge matches the -1 in
        // ppro_getFrameInfo for playhead-boundary compensation.
        if (seq.videoTracks.numTracks < 2) { try { seq.videoTracks.addTracks(1); } catch (_) {} }
        var v2 = seq.videoTracks[1];
        if (!v2) return JSON.stringify({ ok: true, placed: false, note: "Imported but V2 unavailable" });

        var placeSec = Number(startSeconds);
        if (isNaN(placeSec) || placeSec < 0) placeSec = 0;
        // Nudge by one frame at the SOURCE'S rate (since that's what the imported
        // sequence is now conformed to) to compensate for Premiere dropping the first
        // frame of a numbered-stills import. The dummy output_00000.png takes that hit.
        var rateForNudge = appliedRate || targetRate;
        var nudge = 1.0 / Number(rateForNudge || 24);
        try {
            v2.overwriteClip(imported, placeSec + nudge);
            return JSON.stringify({ ok: true, placed: true, binName: imported.name, appliedRate: appliedRate });
        } catch (e) {
            return JSON.stringify({ ok: true, placed: false, binName: imported.name,
                note: "Imported into bin but overwriteClip failed: " + String(e) });
        }
    } catch (e) { return JSON.stringify({ ok: false, error: String(e) }); }
}
