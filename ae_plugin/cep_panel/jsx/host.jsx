/**
 * CorridorKey - After Effects Host Script
 * Handles communication between panel and AE
 */

// Auto-detect CorridorKey root: this script is in ae_plugin/cep_panel/jsx/
var CORRIDORKEY_ROOT = (new File($.fileName)).parent.parent.parent.parent.fsName;
var PYTHON_EXE = CORRIDORKEY_ROOT + "\\.venv\\Scripts\\python.exe";
var PROCESSOR_SCRIPT = CORRIDORKEY_ROOT + "\\ae_plugin\\ae_processor.py";
var TEMP_DIR = Folder.temp.fsName;

function processCurrentFrame(settingsJson, previewOnly) {
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

        // Get current time
        var currentTime = comp.time;

        // Export current frame
        var inputPath = TEMP_DIR + "\\ck_ae_input.png";
        var outputPath = TEMP_DIR + "\\ck_ae_output.png";

        // Save frame using render queue
        var savedFrame = saveFrameToFile(comp, currentTime, inputPath);
        if (!savedFrame) {
            return "Error: Could not export frame";
        }

        // Build command
        var cmd = buildCommand(inputPath, outputPath, settings);

        // Execute Python
        var result = system.callSystem(cmd);

        // Check if output exists
        var outputFile = new File(outputPath);
        if (!outputFile.exists) {
            return "Error: Processing failed - no output";
        }

        if (!previewOnly) {
            // Import result back to AE
            var importedFile = app.project.importFile(new ImportOptions(outputFile));
            if (importedFile) {
                // Add to comp above current layer
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

function processWorkArea(settingsJson) {
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

        // Get work area
        var startTime = comp.workAreaStart;
        var duration = comp.workAreaDuration;
        var endTime = startTime + duration;
        var frameDuration = comp.frameDuration;

        var frameCount = Math.floor(duration / frameDuration);
        var processedCount = 0;

        // Create output folder
        var outputFolder = new Folder(TEMP_DIR + "\\ck_sequence");
        if (!outputFolder.exists) outputFolder.create();

        // Process each frame
        for (var t = startTime; t < endTime; t += frameDuration) {
            var frameNum = Math.floor((t - startTime) / frameDuration);
            var inputPath = outputFolder.fsName + "\\input_" + padNumber(frameNum, 5) + ".png";
            var outputPath = outputFolder.fsName + "\\output_" + padNumber(frameNum, 5) + ".png";

            // Export frame
            saveFrameToFile(comp, t, inputPath);

            // Process
            var cmd = buildCommand(inputPath, outputPath, settings);
            system.callSystem(cmd);

            // Cleanup input
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
    // Use saveFrameToPng if available (AE 2020+)
    try {
        comp.saveFrameToPng(time, new File(filePath));
        return true;
    } catch (e) {
        // Fallback: render via render queue
        return renderFrameViaQueue(comp, time, filePath);
    }
}

function renderFrameViaQueue(comp, time, filePath) {
    try {
        // Save original time
        var originalTime = comp.time;
        comp.time = time;

        // Add to render queue
        var rqItem = app.project.renderQueue.items.add(comp);
        var om = rqItem.outputModules[1];

        // Set output
        om.file = new File(filePath);
        om.applyTemplate("PNG Sequence");

        // Set time span to single frame
        rqItem.timeSpanStart = time;
        rqItem.timeSpanDuration = comp.frameDuration;

        // Render
        app.project.renderQueue.render();

        // Cleanup render queue item
        rqItem.remove();

        // Restore time
        comp.time = originalTime;

        return true;
    } catch (e) {
        return false;
    }
}

function buildCommand(inputPath, outputPath, settings) {
    // Validate inputs to prevent command injection
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
