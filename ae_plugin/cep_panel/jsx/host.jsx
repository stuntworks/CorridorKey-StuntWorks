/**
 * CorridorKey - Host Script for After Effects and Premiere Pro
 * Handles communication between CEP panel and host app.
 */

// Auto-detect CorridorKey root: this script is in ae_plugin/cep_panel/jsx/
var CORRIDORKEY_ROOT = (new File($.fileName)).parent.parent.parent.parent.fsName;
var PYTHON_EXE = CORRIDORKEY_ROOT + "\\.venv\\Scripts\\python.exe";
var PROCESSOR_SCRIPT = CORRIDORKEY_ROOT + "\\ae_plugin\\ae_processor.py";
var TEMP_DIR = Folder.temp.fsName;

// Detect host application
var HOST_APP = "unknown";
if (typeof CompItem !== "undefined") {
    HOST_APP = "ae";
} else if (typeof ProjectItem !== "undefined" || (app && app.project && app.project.activeSequence !== undefined)) {
    HOST_APP = "ppro";
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

        var currentTime = comp.time;
        var pid = Math.floor(Math.random() * 100000);
        var inputPath = TEMP_DIR + "\\ck_ae_in_" + pid + ".png";
        var outputPath = TEMP_DIR + "\\ck_ae_out_" + pid + ".png";

        var savedFrame = saveFrameToFile(comp, currentTime, inputPath);
        if (!savedFrame) {
            return "Error: Could not export frame";
        }

        var cmd = buildCommand(inputPath, outputPath, settings);
        var result = system.callSystem(cmd);

        var outputFile = new File(outputPath);
        if (!outputFile.exists) {
            return "Error: Processing failed - no output";
        }

        if (!previewOnly) {
            var importedFile = app.project.importFile(new ImportOptions(outputFile));
            if (importedFile) {
                var newLayer = comp.layers.add(importedFile);
                newLayer.moveBefore(layer);
                newLayer.startTime = currentTime;
                newLayer.outPoint = currentTime + comp.frameDuration;
            }
        }

        // Cleanup
        var inputFile = new File(inputPath);
        if (inputFile.exists) inputFile.remove();
        if (!previewOnly && outputFile.exists) outputFile.remove();

        return "success";

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
    try {
        comp.saveFrameToPng(time, new File(filePath));
        return true;
    } catch (e) {
        return renderFrameViaQueue(comp, time, filePath);
    }
}

function renderFrameViaQueue(comp, time, filePath) {
    try {
        var originalTime = comp.time;
        comp.time = time;

        var rqItem = app.project.renderQueue.items.add(comp);
        var om = rqItem.outputModules[1];

        om.file = new File(filePath);
        om.applyTemplate("PNG Sequence");

        rqItem.timeSpanStart = time;
        rqItem.timeSpanDuration = comp.frameDuration;

        app.project.renderQueue.render();
        rqItem.remove();
        comp.time = originalTime;

        return true;
    } catch (e) {
        return false;
    }
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
