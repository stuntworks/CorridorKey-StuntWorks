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
    // Last resort fallback
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
    // Returns a CorridorKey subfolder next to the AE project file
    try {
        var projFile = app.project.file;
        if (projFile) {
            var projDir = projFile.parent.fsName;
            var ckDir = new Folder(projDir + "/CorridorKey");
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

        // Extract frame directly from source file via Python/OpenCV — bypasses render queue entirely
        var sourceFile = null;
        try {
            sourceFile = layer.source.file.fsName;
        } catch (e) {}

        if (!sourceFile) {
            return "Error: Cannot get source file from selected layer";
        }

        // Calculate source frame number — account for layer in-point and start time
        var fps = comp.frameRate;
        var layerTimeInComp = comp.time - layer.startTime;
        var sourceTime = layerTimeInComp + layer.inPoint;
        var frameNum = Math.floor(sourceTime * fps + 0.5) - 1;

        // Use Python to extract frame
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

        // Debug: check Python exists
        var pyFile = new File(PYTHON_EXE);
        if (!pyFile.exists) {
            return "Error: Python not found at: " + PYTHON_EXE;
        }
        var procFile = new File(PROCESSOR_SCRIPT);
        if (!procFile.exists) {
            return "Error: Processor script not found at: " + PROCESSOR_SCRIPT;
        }

        var result = system.callSystem(cmd);

        var outputFile = new File(outputPath);
        if (!outputFile.exists) {
            return "Error: Processing failed - no output. CMD: " + cmd + " | Result: " + result;
        }

        // Import keyed frame to comp above selected layer
        app.beginUndoGroup("CorridorKey Process");
        try {
            var normalizedOutput = outputPath.replace(/\\/g, "/");
            var importFile = new File(normalizedOutput);
            var io = new ImportOptions(importFile);
            io.importAs = ImportAsType.FOOTAGE;
            io.sequence = false;  // CRITICAL: filename ends in digits, AE thinks it's a sequence

            var importedItem = app.project.importFile(io);
            if (importedItem) {
                var playheadTime = comp.time;
                var newLayer = comp.layers.add(importedItem);
                newLayer.enabled = true;
                newLayer.moveBefore(layer);
                // Position at playhead — set startTime AFTER moveBefore
                newLayer.startTime = playheadTime;
                newLayer.outPoint = playheadTime + comp.frameDuration;
                comp.time = comp.time;  // Force UI refresh
            }
        } catch (importErr) {
            return "Error importing: " + importErr.toString();
        } finally {
            app.endUndoGroup();
        }

        // Cleanup input
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

        var startTime = comp.workAreaStart;
        var duration = comp.workAreaDuration;
        var endTime = startTime + duration;
        var frameDuration = comp.frameDuration;
        var frameCount = Math.floor(duration / frameDuration);
        var processedCount = 0;

        var pid = Math.floor(Math.random() * 100000);
        var outputFolder = new Folder(TEMP_DIR + "\\ck_seq_" + pid);
        if (!outputFolder.exists) outputFolder.create();

        for (var t = startTime; t < endTime; t += frameDuration) {
            var frameNum = Math.floor((t - startTime) / frameDuration);
            var inputPath = outputFolder.fsName + "\\input_" + padNumber(frameNum, 5) + ".png";
            var outputPath = outputFolder.fsName + "\\output_" + padNumber(frameNum, 5) + ".png";

            saveFrameToFile(comp, t, inputPath);

            var cmd = buildCommand(inputPath, outputPath, settings);
            system.callSystem(cmd);

            var inputFile = new File(inputPath);
            if (inputFile.exists) inputFile.remove();

            processedCount++;
        }

        // Import sequence
        var firstFrame = new File(outputFolder.fsName + "\\output_00000.png");
        if (firstFrame.exists) {
            var importOptions = new ImportOptions(firstFrame);
            importOptions.sequence = true;
            var importedSeq = app.project.importFile(importOptions);

            if (importedSeq) {
                var newLayer = comp.layers.add(importedSeq);
                newLayer.moveBefore(layer);
                newLayer.startTime = startTime;
            }
        }

        return "success: " + processedCount + " frames processed";

    } catch (e) {
        return "Error: " + e.toString();
    }
}

function saveFrameToFile(comp, time, filePath) {
    var targetFile = new File(filePath);
    if (targetFile.exists) targetFile.remove();

    // Method 1: saveFrameToPng (may exist in some AE builds)
    try {
        comp.saveFrameToPng(time, targetFile);
        if (targetFile.exists) return true;
        // Check sequence-numbered variant
        var seqFile1 = new File(filePath.replace(".png", "_00000.png"));
        if (seqFile1.exists) { seqFile1.rename(targetFile.name); return true; }
    } catch (e) { /* not available in this build */ }

    // Method 2: Render queue with FourCC format (bypasses locale/template issues)
    try {
        var originalTime = comp.time;

        // Flush stale render queue items
        var rq = app.project.renderQueue;
        while (rq.numItems > 0) { rq.item(1).remove(); }

        // Add comp and set single-frame range
        var rqItem = rq.items.add(comp);
        rqItem.timeSpanStart = time;
        rqItem.timeSpanDuration = comp.frameDuration;

        // Configure output module — NO applyTemplate, use FourCC directly
        var om = rqItem.outputModules[1];
        try { om["format"] = 1797552720; } catch (e2) { /* FourCC not supported, try template */ }
        // Set file AFTER format (critical ordering)
        om.file = new File(filePath);

        // Render
        rq.render();

        // Hunt for the actual written file — AE appends sequence numbers
        var found = findWrittenFile(filePath);

        // Clean up
        try { rqItem.remove(); } catch (e3) {}
        comp.time = originalTime;

        if (found) return true;
    } catch (e) { /* render queue failed */ }

    // Method 3: Render queue with template search
    try {
        var rq2 = app.project.renderQueue;
        while (rq2.numItems > 0) { rq2.item(1).remove(); }

        var rqItem2 = rq2.items.add(comp);
        rqItem2.timeSpanStart = time;
        rqItem2.timeSpanDuration = comp.frameDuration;

        var om2 = rqItem2.outputModules[1];
        // Find any PNG-related template
        var templates = om2.templates;
        for (var i = 1; i <= templates.length; i++) {
            if (templates[i].match(/PNG/i)) {
                om2.applyTemplate(templates[i]);
                break;
            }
        }
        om2.file = new File(filePath);
        rq2.render();

        var found2 = findWrittenFile(filePath);
        try { rqItem2.remove(); } catch (e4) {}

        if (found2) return true;
    } catch (e) {}

    return false;
}

function findWrittenFile(filePath) {
    // Check exact path first
    if (new File(filePath).exists) return true;

    // AE sequence numbering variants
    var targetFile = new File(filePath);
    var dir = targetFile.parent;
    var baseName = targetFile.displayName.replace(/\.png$/i, "");
    var candidates = [
        baseName + "_00000.png",
        baseName + "00000.png",
        baseName + "[00000].png",
        baseName + "_00001.png",
        baseName + "00001.png"
    ];
    for (var i = 0; i < candidates.length; i++) {
        var f = new File(dir.fsName + "/" + candidates[i]);
        if (f.exists) {
            f.rename(targetFile.name);
            return true;
        }
    }

    // Glob: any file starting with baseName in the temp dir
    var matches = dir.getFiles(baseName + "*");
    if (matches.length > 0) {
        matches[0].rename(targetFile.name);
        return true;
    }

    return false;
}

// ============================================================
// PREMIERE PRO
// ============================================================

function ppro_processCurrentFrame(settingsJson, previewOnly) {
    try {
        var settings = JSON.parse(settingsJson);
        var seq = app.project.activeSequence;

        if (!seq) {
            return "Error: No active sequence";
        }

        // Get playhead position in ticks
        var playerPos = seq.getPlayerPosition();
        var ticksPerSecond = 254016000000;

        // Find clip at playhead on track 1 (video track index 0)
        var videoTracks = seq.videoTracks;
        if (videoTracks.numTracks < 1) {
            return "Error: No video tracks";
        }

        var track = videoTracks[0];
        var clips = track.clips;
        var targetClip = null;

        for (var i = 0; i < clips.numItems; i++) {
            var clip = clips[i];
            if (playerPos.ticks >= clip.start.ticks && playerPos.ticks < clip.end.ticks) {
                targetClip = clip;
                break;
            }
        }

        if (!targetClip) {
            return "Error: No clip at playhead on Track 1";
        }

        // Get source file path
        var projectItem = targetClip.projectItem;
        var filePath = projectItem.getMediaPath();

        if (!filePath) {
            return "Error: Cannot get source file path";
        }

        // Calculate source frame number
        var fps = seq.getSettings().videoFrameRate;
        var clipOffsetTicks = playerPos.ticks - targetClip.start.ticks + targetClip.inPoint.ticks;
        var sourceTimeSec = clipOffsetTicks / ticksPerSecond;
        var sourceFrame = Math.floor(sourceTimeSec * fps);

        // Export frame using QE (Premiere's scripting DOM)
        var pid = Math.floor(Math.random() * 100000);
        var inputPath = TEMP_DIR + "\\ck_ppro_in_" + pid + ".png";
        var outputPath = TEMP_DIR + "\\ck_ppro_out_" + pid + ".png";

        // Use system command to extract frame with ffmpeg or Python/OpenCV
        var extractCmd = '"' + PYTHON_EXE + '" -c "' +
            "import cv2; " +
            "cap = cv2.VideoCapture(r'" + filePath.replace(/\\/g, "\\\\") + "'); " +
            "cap.set(cv2.CAP_PROP_POS_FRAMES, " + sourceFrame + "); " +
            "ret, frame = cap.read(); " +
            "cap.release(); " +
            "cv2.imwrite(r'" + inputPath.replace(/\\/g, "\\\\") + "', frame) if ret else None" +
            '"';

        system.callSystem(extractCmd);

        var inputFile = new File(inputPath);
        if (!inputFile.exists) {
            return "Error: Could not extract frame from source";
        }

        // Process through CorridorKey
        var cmd = buildCommand(inputPath, outputPath, settings);
        system.callSystem(cmd);

        var outputFile = new File(outputPath);
        if (!outputFile.exists) {
            // Cleanup input
            if (inputFile.exists) inputFile.remove();
            return "Error: Processing failed - no output";
        }

        if (!previewOnly) {
            // Import to project
            var success = app.project.importFiles([outputPath], true, app.project.rootItem, false);

            if (success) {
                // Find the imported item (last item in root bin)
                var rootItems = app.project.rootItem.children;
                var importedItem = rootItems[rootItems.numItems - 1];

                // Insert on track above (video track index 1)
                if (videoTracks.numTracks < 2) {
                    // Need at least 2 tracks
                    seq.videoTracks.numTracks = 2;
                }
                var insertTime = playerPos;
                // Overwrite edit onto track 2
                var track2 = videoTracks[1];
                targetClip.projectItem; // refresh
                seq.insertClip(importedItem, insertTime, 1); // trackIndex is 0-based in some versions
            }
        }

        // Cleanup
        if (inputFile.exists) inputFile.remove();
        if (!previewOnly && outputFile.exists) outputFile.remove();

        return "success";

    } catch (e) {
        return "Error: " + e.toString();
    }
}

function ppro_processWorkArea(settingsJson) {
    try {
        var settings = JSON.parse(settingsJson);
        var seq = app.project.activeSequence;

        if (!seq) {
            return "Error: No active sequence";
        }

        // Get in/out points for work area
        var inPoint = seq.getInPointAsTime();
        var outPoint = seq.getOutPointAsTime();

        if (!inPoint || !outPoint || inPoint.seconds >= outPoint.seconds) {
            return "Error: Set in/out points on timeline first";
        }

        var fps = seq.getSettings().videoFrameRate;
        var startFrame = Math.floor(inPoint.seconds * fps);
        var endFrame = Math.floor(outPoint.seconds * fps);
        var totalFrames = endFrame - startFrame;

        if (totalFrames <= 0) {
            return "Error: Invalid in/out range";
        }

        // Find source clip on track 1
        var track = seq.videoTracks[0];
        var clips = track.clips;
        if (clips.numItems < 1) {
            return "Error: No clips on Track 1";
        }

        var sourceClip = clips[0];
        var filePath = sourceClip.projectItem.getMediaPath();

        var pid = Math.floor(Math.random() * 100000);
        var outputFolder = new Folder(TEMP_DIR + "\\ck_ppro_seq_" + pid);
        if (!outputFolder.exists) outputFolder.create();

        var processedCount = 0;

        for (var f = startFrame; f < endFrame; f++) {
            var frameNum = f - startFrame;
            var inputPath = outputFolder.fsName + "\\input_" + padNumber(frameNum, 5) + ".png";
            var outputPath = outputFolder.fsName + "\\output_" + padNumber(frameNum, 5) + ".png";

            // Extract frame
            var extractCmd = '"' + PYTHON_EXE + '" -c "' +
                "import cv2; " +
                "cap = cv2.VideoCapture(r'" + filePath.replace(/\\/g, "\\\\") + "'); " +
                "cap.set(cv2.CAP_PROP_POS_FRAMES, " + f + "); " +
                "ret, frame = cap.read(); " +
                "cap.release(); " +
                "cv2.imwrite(r'" + inputPath.replace(/\\/g, "\\\\") + "', frame) if ret else None" +
                '"';

            system.callSystem(extractCmd);

            // Process
            var cmd = buildCommand(inputPath, outputPath, settings);
            system.callSystem(cmd);

            // Cleanup input
            var inputFile = new File(inputPath);
            if (inputFile.exists) inputFile.remove();

            processedCount++;
        }

        // Import the output sequence
        var firstOut = outputFolder.fsName + "\\output_00000.png";
        if (new File(firstOut).exists) {
            app.project.importFiles([firstOut], true, app.project.rootItem, true);
        }

        return "success: " + processedCount + " frames processed";

    } catch (e) {
        return "Error: " + e.toString();
    }
}

// ============================================================
// ROUTER — called from panel, dispatches to correct host
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
