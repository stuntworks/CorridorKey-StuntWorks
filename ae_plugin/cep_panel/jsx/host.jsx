/**
 * CorridorKey - Host Script for After Effects and Premiere Pro
 * Handles communication between CEP panel and host app.
 */

// ── JSON polyfill for ExtendScript (no native JSON) ──
if (typeof JSON === "undefined") {
    JSON = {
        parse: function(s) {
            return eval("(" + s + ")");
        },
        stringify: function(obj) {
            if (obj === null) return "null";
            if (typeof obj === "undefined") return undefined;
            if (typeof obj === "number" || typeof obj === "boolean") return String(obj);
            if (typeof obj === "string") return '"' + obj.replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
            if (obj instanceof Array) {
                var items = [];
                for (var i = 0; i < obj.length; i++) items.push(JSON.stringify(obj[i]));
                return "[" + items.join(",") + "]";
            }
            if (typeof obj === "object") {
                var pairs = [];
                for (var k in obj) {
                    if (obj.hasOwnProperty(k)) pairs.push('"' + k + '":' + JSON.stringify(obj[k]));
                }
                return "{" + pairs.join(",") + "}";
            }
            return String(obj);
        }
    };
}

// Locate CorridorKey root: check config file in known CEP locations
var CORRIDORKEY_ROOT = null;
var _cepPaths = [
    Folder(Folder.userData.fsName + "/Adobe/CEP/extensions/com.corridorkey.panel"),
    Folder(Folder.appData.fsName + "/Adobe/CEP/extensions/com.corridorkey.panel"),
    (new File($.fileName)).parent.parent
];
for (var _ci = 0; _ci < _cepPaths.length; _ci++) {
    var _cf = new File(_cepPaths[_ci].fsName + "/corridorkey_path.txt");
    if (_cf.exists) {
        _cf.open("r");
        CORRIDORKEY_ROOT = _cf.read().replace(/[\r\n]/g, "");
        _cf.close();
        break;
    }
}
if (!CORRIDORKEY_ROOT) {
    CORRIDORKEY_ROOT = "D:\\New AI Projects\\CorridorKey";
}
var PYTHON_EXE = CORRIDORKEY_ROOT + "\\.venv\\Scripts\\pythonw.exe";
var PROCESSOR_SCRIPT = CORRIDORKEY_ROOT + "\\ae_plugin\\ae_processor.py";
var TEMP_DIR = Folder.temp.fsName;

// Detect host application
var HOST_APP = "unknown";
if (typeof CompItem !== "undefined") {
    HOST_APP = "ae";
} else if (typeof ProjectItem !== "undefined" || (app && app.project && app.project.activeSequence !== undefined)) {
    HOST_APP = "ppro";
}

function getProjectOutputDir() {
    // Try AE method first
    try {
        var projFile = app.project.file;
        if (projFile) {
            var projDir = projFile.parent.fsName;
            var ckDir = new Folder(projDir + "/CorridorKey");
            if (!ckDir.exists) ckDir.create();
            return ckDir.fsName;
        }
    } catch (e) {}
    // Try Premiere method
    try {
        var projPath = app.project.path;
        if (projPath && projPath.length > 0) {
            var projFolder = new File(projPath).parent.fsName;
            var ckDir = new Folder(projFolder + "/CorridorKey");
            if (!ckDir.exists) ckDir.create();
            return ckDir.fsName;
        }
    } catch (e) {}
    // Fallback to Documents
    return Folder.myDocuments.fsName + "/CorridorKey";
}

function getHostApp() {
    return HOST_APP;
}

// ============================================================
// AFTER EFFECTS
// ============================================================

function ae_processCurrentFrame(settingsJson, previewOnly) {
    try {
        var settings = JSON.parse(settingsJson);
        var comp = app.project.activeItem;

        if (!(comp instanceof CompItem)) {
            return "Error: No composition selected";
        }

        var layer = comp.selectedLayers[0];
        if (!layer) {
            return "Error: No layer selected";
        }

        var pid = Math.floor(Math.random() * 100000);
        var inputPath = TEMP_DIR + "\\ck_ae_in_" + pid + ".png";
        var outputPath = TEMP_DIR + "\\ck_ae_out_" + pid + ".png";

        // Extract frame directly from source file via Python/OpenCV
        var sourceFile = null;
        try {
            sourceFile = layer.source.file.fsName;
        } catch (e) {}

        if (!sourceFile) {
            return "Error: Cannot get source file from selected layer";
        }

        // Calculate source frame number
        var fps = comp.frameRate;
        var layerTimeInComp = comp.time - layer.startTime;
        var sourceTime = layerTimeInComp + layer.inPoint;
        var frameNum = Math.floor(sourceTime * fps + 0.5) - 1;

        var extractCmd = '"' + PYTHON_EXE + '" -c "' +
            "import cv2; " +
            "cap = cv2.VideoCapture(r'" + sourceFile.replace(/\\/g, "\\\\") + "'); " +
            "cap.set(cv2.CAP_PROP_POS_FRAMES, " + frameNum + "); " +
            "ret, frame = cap.read(); " +
            "cap.release(); " +
            "cv2.imwrite(r'" + inputPath.replace(/\\/g, "\\\\") + "', frame) if ret else None" +
            '"';
        system.callSystem(extractCmd);

        var inputCheck = new File(inputPath);
        if (!inputCheck.exists) {
            return "Error: Could not extract frame " + frameNum + " from: " + sourceFile;
        }

        var cmd = buildCommand(inputPath, outputPath, settings);
        var result = system.callSystem(cmd);

        var outputFile = new File(outputPath);
        if (!outputFile.exists) {
            return "Error: Processing failed - no output";
        }

        // Import keyed frame to comp above selected layer
        app.beginUndoGroup("CorridorKey Process");
        try {
            var normalizedOutput = outputPath.replace(/\\/g, "/");
            var importFile = new File(normalizedOutput);
            var io = new ImportOptions(importFile);
            io.importAs = ImportAsType.FOOTAGE;
            io.sequence = false;

            var importedItem = app.project.importFile(io);
            if (importedItem) {
                var playheadTime = comp.time;
                var newLayer = comp.layers.add(importedItem);
                newLayer.enabled = true;
                newLayer.moveBefore(layer);
                newLayer.startTime = playheadTime;
                newLayer.outPoint = playheadTime + comp.frameDuration;
                comp.time = comp.time;
            }
        } catch (importErr) {
            return "Error importing: " + importErr.toString();
        } finally {
            app.endUndoGroup();
        }

        var inputFile = new File(inputPath);
        if (inputFile.exists) inputFile.remove();

        return "success:" + outputPath;

    } catch (e) {
        return "Error: " + e.toString();
    }
}

function ae_processWorkArea(settingsJson) {
    try {
        var settings = JSON.parse(settingsJson);
        var comp = app.project.activeItem;

        if (!(comp instanceof CompItem)) {
            return "Error: No composition selected";
        }

        var layer = comp.selectedLayers[0];
        if (!layer) {
            return "Error: No layer selected";
        }

        // Get source file for batch processing
        var sourceFile = null;
        try { sourceFile = layer.source.file.fsName; } catch (e) {}
        if (!sourceFile) return "Error: Cannot get source file";

        var fps = comp.frameRate;
        var startTime = comp.workAreaStart;
        var duration = comp.workAreaDuration;
        var endTime = startTime + duration;

        // Convert comp work area to source media frame numbers
        var sourceStartTime = startTime - layer.startTime + layer.inPoint;
        var sourceEndTime = endTime - layer.startTime + layer.inPoint;
        var startFrame = Math.floor(sourceStartTime * fps + 0.5) - 1;
        var endFrame = Math.floor(sourceEndTime * fps + 0.5) - 1;
        if (startFrame < 0) startFrame = 0;
        if (endFrame <= startFrame) return "Error: Invalid frame range";

        var frameCount = endFrame - startFrame;

        var pid = Math.floor(Math.random() * 100000);
        var outputFolder = new Folder(TEMP_DIR + "\\ck_seq_" + pid);
        if (!outputFolder.exists) outputFolder.create();

        // ONE Python call for entire batch — Python reads video directly
        var cmd = '"' + PYTHON_EXE + '" "' + PROCESSOR_SCRIPT + '" batch ';
        cmd += '"' + sourceFile + '" ';
        cmd += '"' + outputFolder.fsName + '" ';
        cmd += '--start-frame ' + startFrame + ' ';
        cmd += '--end-frame ' + endFrame + ' ';
        cmd += '--fps ' + fps + ' ';
        cmd += '--screen ' + ((settings.screenType === "blue") ? "blue" : "green") + ' ';
        cmd += '--despill ' + (parseFloat(settings.despill) || 0.5) + ' ';
        cmd += '--despeckle ' + (settings.despeckle ? '1' : '0') + ' ';
        cmd += '--despeckle-size ' + (parseInt(settings.despeckleSize, 10) || 400) + ' ';
        cmd += '--refiner ' + (parseFloat(settings.refiner) || 1.0);

        system.callSystem(cmd);

        // Read result summary
        var resultFile = new File(outputFolder.fsName + "\\batch_result.txt");
        var processedCount = 0;
        if (resultFile.exists) {
            resultFile.open("r");
            var resultText = resultFile.read();
            resultFile.close();
            var parts = resultText.split(",");
            processedCount = parseInt(parts[0], 10);
        }

        if (processedCount === 0) {
            return "Error: Batch processing failed - no frames produced";
        }

        // Import output sequence
        app.beginUndoGroup("CorridorKey Batch");

        var firstFrame = new File(outputFolder.fsName + "\\output_00000.png");
        if (firstFrame.exists) {
            var importOptions = new ImportOptions(firstFrame);
            importOptions.sequence = true;
            var importedSeq = app.project.importFile(importOptions);

            if (importedSeq) {
                importedSeq.mainSource.conformFrameRate = fps;
                var newLayer = comp.layers.add(importedSeq);
                newLayer.moveBefore(layer);
                newLayer.startTime = startTime;
            }
        }

        app.endUndoGroup();
        comp.time = comp.time;

        return "success: " + processedCount + "/" + frameCount + " frames processed";

    } catch (e) {
        return "Error: " + e.toString();
    }
}

// ============================================================
// PREMIERE PRO
// ============================================================

// Premiere: get clip info for panel-side Python execution (no system.callSystem in Premiere)
function ppro_getClipInfo() {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return JSON.stringify({error: "No active sequence"});

        var playerPos = seq.getPlayerPosition();

        var videoTracks = seq.videoTracks;
        if (videoTracks.numTracks < 1) return JSON.stringify({error: "No video tracks"});

        var track = videoTracks[0];
        var clips = track.clips;
        var targetClip = null;

        for (var i = 0; i < clips.numItems; i++) {
            var clip = clips[i];
            var pSec = playerPos.seconds;
            var cStart = clip.start.seconds;
            var cEnd = clip.end.seconds;
            if (pSec >= cStart && pSec < cEnd) {
                targetClip = clip;
                break;
            }
        }

        if (!targetClip) {
            var dbg = "No clip at playhead. Clips on V1=" + clips.numItems;
            for (var di = 0; di < clips.numItems && di < 3; di++) {
                dbg += " | Clip" + di + " start=" + clips[di].start.seconds + "s end=" + clips[di].end.seconds + "s";
            }
            dbg += " | Playhead=" + playerPos.seconds + "s";
            return JSON.stringify({error: dbg});
        }

        var filePath = targetClip.projectItem.getMediaPath();
        if (!filePath) return JSON.stringify({error: "Cannot get source file path"});

        // Get fps from sequence settings
        var fpsRaw = seq.getSettings().videoFrameRate;
        var fps = parseFloat(fpsRaw);
        if (isNaN(fps) || fps <= 0) fps = 24;

        // Use .seconds property (avoids ticks math entirely)
        var playheadSec = playerPos.seconds;
        var clipStartSec = targetClip.start.seconds;
        var clipInPointSec = targetClip.inPoint.seconds;

        // How far into the clip is the playhead
        var offsetInClip = playheadSec - clipStartSec;
        // Source media time = clip inPoint + offset
        var sourceTimeSec = clipInPointSec + offsetInClip;
        var sourceFrame = Math.floor(sourceTimeSec * fps) - 1;
        if (sourceFrame < 0) sourceFrame = 0;

        var clipDurationSec = targetClip.end.seconds - targetClip.start.seconds;
        var totalFrames = Math.floor(clipDurationSec * fps);

        return JSON.stringify({
            sourcePath: filePath,
            sourceFrame: sourceFrame,
            fps: fps,
            totalFrames: totalFrames,
            debug: "playhead=" + playheadSec.toFixed(2) + "s clipStart=" + clipStartSec.toFixed(2) + "s inPoint=" + clipInPointSec.toFixed(2) + "s offset=" + offsetInClip.toFixed(2) + "s srcTime=" + sourceTimeSec.toFixed(2) + "s"
        });
    } catch (e) {
        return JSON.stringify({error: e.toString()});
    }
}

function ppro_getWorkAreaInfo() {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return JSON.stringify({error: "No active sequence"});

        var inPoint = seq.getInPointAsTime();
        var outPoint = seq.getOutPointAsTime();

        if (!inPoint || !outPoint || inPoint.seconds >= outPoint.seconds) {
            return JSON.stringify({error: "Set in/out points on timeline first"});
        }

        var fpsRaw = seq.getSettings().videoFrameRate;
        var fps = parseFloat(fpsRaw);
        if (isNaN(fps) || fps <= 0) fps = 24;
        var startFrame = Math.floor(inPoint.seconds * fps);
        var endFrame = Math.floor(outPoint.seconds * fps);

        if (endFrame <= startFrame) return JSON.stringify({error: "Invalid in/out range"});

        var track = seq.videoTracks[0];
        var clips = track.clips;
        if (clips.numItems < 1) return JSON.stringify({error: "No clips on Track 1"});

        var filePath = clips[0].projectItem.getMediaPath();
        if (!filePath) return JSON.stringify({error: "Cannot get source file path"});

        return JSON.stringify({
            sourcePath: filePath,
            startFrame: startFrame,
            endFrame: endFrame,
            fps: fps
        });
    } catch (e) {
        return JSON.stringify({error: e.toString()});
    }
}

function ppro_importFile(filePath) {
    try {
        // Find or create CorridorKey bin
        var rootItem = app.project.rootItem;
        var ckBin = null;
        for (var i = 0; i < rootItem.children.numItems; i++) {
            if (rootItem.children[i].name === "CorridorKey" && rootItem.children[i].type === 2) {
                ckBin = rootItem.children[i];
                break;
            }
        }
        if (!ckBin) {
            ckBin = rootItem.createBin("CorridorKey");
        }

        // Import to CorridorKey bin
        var imported = app.project.importFiles([filePath], true, ckBin, false);

        // Find the imported item (last in the bin)
        var importedItem = null;
        for (var j = 0; j < ckBin.children.numItems; j++) {
            importedItem = ckBin.children[j]; // last one
        }

        if (!importedItem) return "success:bin_only";

        // Insert on timeline V2 at playhead
        var seq = app.project.activeSequence;
        if (seq) {
            var playerPos = seq.getPlayerPosition();
            var vTracks = seq.videoTracks;
            if (vTracks.numTracks >= 2) {
                // Use overwriteClip instead of insertClip — doesn't push other clips
                vTracks[1].overwriteClip(importedItem, playerPos.seconds);

                // Find the clip we just inserted and trim to 1 frame
                var fps = parseFloat(seq.getSettings().videoFrameRate) || 24;
                var frameDuration = 1.0 / fps;
                var track2 = vTracks[1];
                for (var ci = 0; ci < track2.clips.numItems; ci++) {
                    var c = track2.clips[ci];
                    if (Math.abs(c.start.seconds - playerPos.seconds) < 0.01) {
                        c.end = c.start.seconds + frameDuration;
                        break;
                    }
                }
                return "success";
            } else {
                return "success:bin_only";
            }
        }
        return "success:bin_only";
    } catch (e) {
        return "Error: " + e.toString();
    }
}

function ppro_importSequence(folderPath) {
    try {
        var firstOut = folderPath + "\\output_00000.png";
        if (new File(firstOut).exists) {
            app.project.importFiles([firstOut], true, app.project.rootItem, true);
            return "success";
        }
        return "Error: No output files found";
    } catch (e) {
        return "Error: " + e.toString();
    }
}

// Legacy wrappers — these are called by the router but Premiere now uses panel-side execution
function ppro_processCurrentFrame(settingsJson, previewOnly) {
    return "PPRO_PANEL_SIDE";
}

function ppro_processWorkArea(settingsJson) {
    return "PPRO_PANEL_SIDE";
}

// ============================================================
// ROUTER
// ============================================================

function processCurrentFrame(settingsJson, previewOnly) {
    if (HOST_APP === "ppro") {
        return ppro_processCurrentFrame(settingsJson, previewOnly);
    }
    return ae_processCurrentFrame(settingsJson, previewOnly);
}

function processWorkArea(settingsJson) {
    if (HOST_APP === "ppro") {
        return ppro_processWorkArea(settingsJson);
    }
    return ae_processWorkArea(settingsJson);
}

// ============================================================
// SHARED UTILITIES
// ============================================================

function buildCommand(inputPath, outputPath, settings) {
    var screenType = (settings.screenType === "blue") ? "blue" : "green";
    var despill = parseFloat(settings.despill);
    if (isNaN(despill) || despill < 0 || despill > 1) despill = 0.5;
    var refiner = parseFloat(settings.refiner);
    if (isNaN(refiner) || refiner < 0 || refiner > 1) refiner = 1.0;
    var despeckleSize = parseInt(settings.despeckleSize, 10);
    if (isNaN(despeckleSize) || despeckleSize < 50 || despeckleSize > 2000) despeckleSize = 400;

    var cmd = '"' + PYTHON_EXE + '" "' + PROCESSOR_SCRIPT + '" ';
    cmd += '"' + inputPath + '" "' + outputPath + '" ';
    cmd += '--screen ' + screenType + ' ';
    cmd += '--despill ' + despill + ' ';
    cmd += '--refiner ' + refiner + ' ';
    cmd += '--despeckle ' + (settings.despeckle ? '1' : '0') + ' ';
    cmd += '--despeckle-size ' + despeckleSize;
    return cmd;
}

function padNumber(num, length) {
    var str = num.toString();
    while (str.length < length) {
        str = '0' + str;
    }
    return str;
}
