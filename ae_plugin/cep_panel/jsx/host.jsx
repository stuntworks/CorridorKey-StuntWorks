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
        var sourceTime = comp.time - layer.startTime + layer.inPoint;
        var sourceFrame = Math.round(sourceTime * fps);
        if (sourceFrame < 0) sourceFrame = 0;

        return JSON.stringify({
            ok: true,
            sourceFile: layer.source.file.fsName,
            sourceFrame: sourceFrame,
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
        var sourceStartTime = startTime - layer.startTime + layer.inPoint;
        var sourceEndTime = endTime - layer.startTime + layer.inPoint;
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
function ae_importSequence(firstFramePath, fps, compStartTime) {
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

        var sourceTimeSec = playerPos.seconds - targetClip.start.seconds + targetClip.inPoint.seconds;
        var sourceFrame = Math.floor(sourceTimeSec * fps) - 1;
        if (sourceFrame < 0) sourceFrame = 0;

        return JSON.stringify({
            ok: true,
            sourceFile: filePath,
            sourceFrame: sourceFrame,
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

        var inPoint = seq.getInPointAsTime();
        var outPoint = seq.getOutPointAsTime();
        if (!inPoint || !outPoint || inPoint.seconds >= outPoint.seconds) {
            return JSON.stringify({ ok: false, error: "Set in/out points on timeline first" });
        }

        var startFrame = Math.round(inPoint.seconds * fps);
        var endFrame = Math.round(outPoint.seconds * fps);
        if (endFrame <= startFrame) return JSON.stringify({ ok: false, error: "Invalid in/out range" });

        var track = seq.videoTracks[0];
        if (track.clips.numItems < 1) return JSON.stringify({ ok: false, error: "No clips on Track 1" });
        var sourceClip = track.clips[0];
        var filePath = sourceClip.projectItem.getMediaPath();
        if (!filePath) return JSON.stringify({ ok: false, error: "Cannot get source file path" });

        return JSON.stringify({
            ok: true,
            sourceFile: filePath,
            startFrame: startFrame,
            endFrame: endFrame,
            fps: fps,
            inPointSeconds: inPoint.seconds
        });
    } catch (e) { return JSON.stringify({ ok: false, error: String(e) }); }
}

// ============================================================
// PREMIERE PRO — timeline mutators
// ============================================================

// WHAT IT DOES: Imports a single keyed PNG into the "CorridorKey" bin and overwrites it
//   onto V2 at the playhead, trimmed to one frame.
// DEPENDS-ON: outputPath exists; active sequence has ≥2 video tracks (creates if not).
// AFFECTS: Project panel (import), timeline V2 (overwriteClip).
// NOTE: Placement needs a +1 frame nudge to match the -1 offset used in ppro_getFrameInfo.
function ppro_importFrame(outputPath, playheadSeconds, fps) {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return JSON.stringify({ ok: false, error: "No active sequence" });

        var outputFile = new File(outputPath);
        if (!outputFile.exists) return JSON.stringify({ ok: false, error: "Output file not found: " + outputPath });

        // Find or create CorridorKey bin
        var root = app.project.rootItem;
        var ckBin = null;
        for (var i = 0; i < root.children.numItems; i++) {
            var item = root.children[i];
            if (item.name === "CorridorKey" && item.type === 2 /* BIN */) { ckBin = item; break; }
        }
        if (!ckBin) ckBin = root.createBin("CorridorKey");

        // Import into the bin
        var ok = app.project.importFiles([outputPath], true, ckBin, false);
        if (!ok) return JSON.stringify({ ok: false, error: "Import failed" });

        var imported = ckBin.children[ckBin.children.numItems - 1];
        if (!imported) return JSON.stringify({ ok: false, error: "Imported item not found" });

        // Make sure we have at least two video tracks
        if (seq.videoTracks.numTracks < 2) seq.videoTracks.addTracks(1);
        var v2 = seq.videoTracks[1];

        // Place one frame ahead to compensate for Premiere's boundary reporting
        var nudge = 1.0 / Number(fps);
        var placeSec = Number(playheadSeconds) + nudge;
        v2.overwriteClip(imported, placeSec);

        return JSON.stringify({ ok: true });
    } catch (e) { return JSON.stringify({ ok: false, error: String(e) }); }
}

// WHAT IT DOES: Imports a PNG sequence into the CorridorKey bin. Does NOT place on timeline —
//   user drags from bin (safer for batch jobs that could be long / partial).
// DEPENDS-ON: firstFramePath exists.
function ppro_importSequence(firstFramePath) {
    try {
        var root = app.project.rootItem;
        var ckBin = null;
        for (var i = 0; i < root.children.numItems; i++) {
            var item = root.children[i];
            if (item.name === "CorridorKey" && item.type === 2) { ckBin = item; break; }
        }
        if (!ckBin) ckBin = root.createBin("CorridorKey");

        var ok = app.project.importFiles([firstFramePath], true, ckBin, true);
        if (!ok) return JSON.stringify({ ok: false, error: "Sequence import failed" });
        return JSON.stringify({ ok: true });
    } catch (e) { return JSON.stringify({ ok: false, error: String(e) }); }
}
