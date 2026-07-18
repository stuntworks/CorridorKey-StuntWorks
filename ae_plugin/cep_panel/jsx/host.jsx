/**
 * CorridorKey — Host Script (ExtendScript)
 * Last modified: 2026-05-29 | Change: ppro_getFrameInfo returns sourceFrame for the SAM2
 *   anchor (was seconds-only, so mid-range Premiere clicks lost backward propagation).
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
// SAFE JSON STRINGIFY + PARSE (ExtendScript has NO native JSON in any AE version)
// ============================================================
// WHAT IT DOES: Minimal JSON.stringify + JSON.parse for ExtendScript.
// HISTORY: this block originally shipped stringify ONLY ("we never eval inbound strings"),
//   then the LAYER LOCK feature (2026-06-04) called JSON.parse — which didn't exist — so
//   the lock handle silently died on every render ("lock step: no-handle", root-caused
//   2026-06-05 by 3-way review). The parse below is a strict recursive-descent parser,
//   NOT eval-based, so the original security stance (no eval of inbound strings) holds.
// DEPENDS-ON: nothing.
// AFFECTS: Defines JSON.stringify + JSON.parse globally.
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
    if (typeof JSON.parse === "undefined") {
        // Strict recursive-descent JSON parser (ES3, no eval). Throws on malformed input.
        JSON.parse = function (text) {
            var at = 0, len = String(text).length, src = String(text);
            function err(msg) { throw new Error("JSON.parse: " + msg + " at " + at); }
            function ws() { while (at < len && " \t\n\r".indexOf(src.charAt(at)) >= 0) at++; }
            function value() {
                ws();
                var c = src.charAt(at);
                if (c === "{") return obj();
                if (c === "[") return arr();
                if (c === '"') return str();
                if (c === "-" || (c >= "0" && c <= "9")) return num();
                if (src.substr(at, 4) === "true") { at += 4; return true; }
                if (src.substr(at, 5) === "false") { at += 5; return false; }
                if (src.substr(at, 4) === "null") { at += 4; return null; }
                err("unexpected '" + c + "'");
            }
            function obj() {
                var o = {}; at++; ws();
                if (src.charAt(at) === "}") { at++; return o; }
                while (at < len) {
                    ws();
                    if (src.charAt(at) !== '"') err("expected key string");
                    var k = str(); ws();
                    if (src.charAt(at) !== ":") err("expected ':'");
                    at++;
                    o[k] = value(); ws();
                    if (src.charAt(at) === ",") { at++; continue; }
                    if (src.charAt(at) === "}") { at++; return o; }
                    err("expected ',' or '}'");
                }
                err("unterminated object");
            }
            function arr() {
                var a = []; at++; ws();
                if (src.charAt(at) === "]") { at++; return a; }
                while (at < len) {
                    a.push(value()); ws();
                    if (src.charAt(at) === ",") { at++; continue; }
                    if (src.charAt(at) === "]") { at++; return a; }
                    err("expected ',' or ']'");
                }
                err("unterminated array");
            }
            function str() {
                var out = ""; at++;                         // skip opening quote
                while (at < len) {
                    var ch = src.charAt(at);
                    if (ch === '"') { at++; return out; }
                    if (ch === "\\") {
                        at++;
                        var e = src.charAt(at);
                        if (e === '"') out += '"';
                        else if (e === "\\") out += "\\";
                        else if (e === "/") out += "/";
                        else if (e === "n") out += "\n";
                        else if (e === "r") out += "\r";
                        else if (e === "t") out += "\t";
                        else if (e === "b") out += "\b";
                        else if (e === "f") out += "\f";
                        else if (e === "u") {
                            out += String.fromCharCode(parseInt(src.substr(at + 1, 4), 16));
                            at += 4;
                        } else err("bad escape '\\" + e + "'");
                        at++;
                    } else { out += ch; at++; }
                }
                err("unterminated string");
            }
            function num() {
                var s = at;
                if (src.charAt(at) === "-") at++;
                while (at < len && src.charAt(at) >= "0" && src.charAt(at) <= "9") at++;
                if (src.charAt(at) === ".") { at++; while (at < len && src.charAt(at) >= "0" && src.charAt(at) <= "9") at++; }
                if (src.charAt(at) === "e" || src.charAt(at) === "E") {
                    at++;
                    if (src.charAt(at) === "+" || src.charAt(at) === "-") at++;
                    while (at < len && src.charAt(at) >= "0" && src.charAt(at) <= "9") at++;
                }
                return parseFloat(src.substring(s, at));
            }
            var result = value(); ws();
            if (at < len) err("trailing characters");
            return result;
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
        if (!(comp instanceof CompItem)) return JSON.stringify({ ok: false, error: "No composition open" });
        var layer = comp.selectedLayers[0];
        // Fall back to the TOPMOST footage layer when nothing is selected (or the
        // selection isn't footage) so the Source Monitor just shows the clip — no
        // manual layer-select needed. comp.layer(1) is the top of the stack.
        if (!layer || !layer.source || !layer.source.file) {
            layer = null;
            for (var i = 1; i <= comp.numLayers; i++) {
                var L = comp.layer(i);
                if (L.source && L.source.file) { layer = L; break; }
            }
        }
        if (!layer) return JSON.stringify({ ok: false, error: "No footage layer in this comp" });

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
        if (!layer || !layer.source || !layer.source.file) {
            // Mirror ae_getFrameInfo: scan for footage layers. Auto-select only when
            // exactly one exists — ambiguous multi-layer comps keep the explicit error.
            var footageLayers = [];
            for (var i = 1; i <= comp.numLayers; i++) {
                var L = comp.layer(i);
                if (L.source && L.source.file) footageLayers.push(L);
            }
            if (footageLayers.length === 1) {
                layer = footageLayers[0];
            } else {
                return JSON.stringify({ ok: false, error: !layer ? "No layer selected" : "Selected layer has no source file" });
            }
        }

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
            compStartTime: startTime,
            // LAYER LOCK: durable handle to comp+layer captured NOW (render start) so the
            // import at render END never depends on what is selected then. comp.id is a
            // stable per-project integer; layer.index can shift if the user edits during the
            // long render, so layerName + sourceFsName are carried as re-find fallbacks.
            lock: {
                compId: comp.id,
                layerIndex: layer.index,
                layerName: layer.name,
                sourceFsName: layer.source.file.fsName
            }
        });
    } catch (e) { return JSON.stringify({ ok: false, error: String(e) }); }
}

// ============================================================
// AFTER EFFECTS — timeline mutators
// ============================================================
//
// v1.0 layer placement note: AE's comp.layers.add() inserts the new layer at
// position 1 (topmost), then newLayer.moveBefore(sourceLayer) moves it to
// one position above the source — never overwriting any existing layer
// above the source. The relative shift produces "stack up, no overwrite"
// behavior automatically. For SAM matte sidecar (v1.1 item 2) the SAM
// layer will moveBefore the keyed layer so the order is:
//   ... above-source layers untouched ...
//   SAM matte (NEW)
//   keyed clip (NEW)
//   source layer
// No code change needed for item 1 in AE.

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
// LAYER LOCK helpers (v1.1): re-find the original target comp+layer from a durable
// handle so an 11-minute render's import never depends on the live selection.
function ck_findCompById(id) {
    for (var i = 1; i <= app.project.numItems; i++) {
        var it = app.project.item(i);
        if ((it instanceof CompItem) && it.id === id) return it;
    }
    return null;
}
// Resolution order: (a) handle comp-by-id + layer-by-index, validated against
// name+source; (b) re-find within that comp by source path (+name bonus); (c) caller
// falls back to the current selection. Returns { comp, layer, via } (via = which step won).
function ck_resolveLockedLayer(activeComp, lockHandleJson) {
    var lock = null, parseErr = null;
    try { if (lockHandleJson) lock = JSON.parse(lockHandleJson); }
    catch (e) { lock = null; parseErr = String(e); }
    // Distinguish "handle never arrived" from "handle arrived but unparseable" — the
    // swallowed-parse-error ambiguity is exactly how the missing-JSON.parse bug hid.
    if (!lock) return { comp: activeComp, layer: null,
                        via: parseErr ? ("bad-lock-json: " + parseErr) : "no-handle" };
    var comp = (typeof lock.compId === "number") ? ck_findCompById(lock.compId) : null;
    if (!(comp instanceof CompItem)) return { comp: activeComp, layer: null, via: "comp-not-found" };
    // (a) stored index, validated against name + source so a shifted index is rejected
    if (lock.layerIndex >= 1 && lock.layerIndex <= comp.numLayers) {
        var Li = comp.layer(lock.layerIndex);
        var nameOk = (lock.layerName == null) || (Li.name === lock.layerName);
        var srcOk = (lock.sourceFsName == null) || (Li.source && Li.source.file && Li.source.file.fsName === lock.sourceFsName);
        if (nameOk && srcOk) return { comp: comp, layer: Li, via: "index" };
    }
    // (b) re-find inside the locked comp by source path (durable); name match preferred
    var bySource = null;
    for (var i = 1; i <= comp.numLayers; i++) {
        var L = comp.layer(i);
        if (!(L.source && L.source.file)) continue;
        if (lock.sourceFsName != null && L.source.file.fsName === lock.sourceFsName) {
            if (lock.layerName != null && L.name === lock.layerName) return { comp: comp, layer: L, via: "name+source" };
            if (!bySource) bySource = L;
        }
    }
    if (bySource) return { comp: comp, layer: bySource, via: "source" };
    return { comp: comp, layer: null, via: "no-match" };
}

function ae_importSequence(firstFramePath, fps, compStartTime, hideSource, lockHandleJson, ckMatteFirstFramePath, sourceFsName, samJunkFirstFramePath) {
    try {
        // GENERATION TRIPWIRE (2026-06-05): host.jsx loads ONCE per AE session while the
        // panel reloads per open — a stale engine silently drops trailing args (the lock
        // bug). Surface the arg count so version skew shows up as data, never silence.
        var _argc = arguments.length;
        if (_argc < 8) return JSON.stringify({ ok: false,
            error: "HOST SCRIPT STALE: ae_importSequence got " + _argc + "/8 args — restart After Effects fully (host.jsx engine is from an older session)." });
        // LAYER LOCK resolution: (a) handle index, (b) handle re-find by source, (c) selection,
        // (d) project-wide scan by source path — the render KNOWS which file it keyed, so even
        // with no lock handle and nothing selected/focused the import still finds its clip
        // (fix for "No composition (lock step: no-handle)" when AE focus moved mid-render).
        var activeComp = app.project.activeItem;
        var res = ck_resolveLockedLayer((activeComp instanceof CompItem) ? activeComp : null, lockHandleJson);
        var comp = res.comp, layer = res.layer, via = res.via;
        if (!layer) {                                  // (c) fall back to the live selection
            if (!(comp instanceof CompItem)) comp = activeComp;
            if (comp instanceof CompItem) { layer = comp.selectedLayers[0] || null; if (layer) via = "selected-fallback"; }
        }
        if (!layer && sourceFsName) {                  // (d) project-wide source-path scan
            for (var ci = 1; ci <= app.project.numItems; ci++) {
                var cIt = app.project.item(ci);
                if (!(cIt instanceof CompItem)) continue;
                for (var li = 1; li <= cIt.numLayers; li++) {
                    var cL = cIt.layer(li);
                    if (cL.source && cL.source.file && cL.source.file.fsName === sourceFsName) {
                        comp = cIt; layer = cL; via = "project-scan";
                        break;
                    }
                }
                if (layer) break;
            }
        }
        if (!(comp instanceof CompItem)) return JSON.stringify({ ok: false, error: "No composition (lock step: " + via + ")" });
        if (!layer) return JSON.stringify({ ok: false, error: "Could not resolve target layer (lock step: " + via + "; locked layer gone and no live selection)" });
        var firstFrame = new File(firstFramePath);
        if (!firstFrame.exists) return JSON.stringify({ ok: false, error: "First frame not found: " + firstFramePath });

        app.beginUndoGroup("CorridorKey Batch");

        // Pre-import all sequences before creating the precomp.
        var importOptions = new ImportOptions(firstFrame);
        importOptions.sequence = true;
        var importedSeq = app.project.importFile(importOptions);

        var ckMatteImported = null;
        if (ckMatteFirstFramePath) {
            try {
                var ckmFirst = new File(String(ckMatteFirstFramePath));
                if (ckmFirst.exists) { var ckmOpts = new ImportOptions(ckmFirst); ckmOpts.sequence = true; ckMatteImported = app.project.importFile(ckmOpts); }
            } catch (eCkm) {}
        }
        var samJunkImported = null;
        if (samJunkFirstFramePath) {
            try {
                var sjFirst = new File(String(samJunkFirstFramePath));
                if (sjFirst.exists) { var sjOpts = new ImportOptions(sjFirst); sjOpts.sequence = true; samJunkImported = app.project.importFile(sjOpts); }
            } catch (eSj) {}
        }
        // Conform frame rates.
        if (importedSeq) importedSeq.mainSource.conformFrameRate = Number(fps);
        if (ckMatteImported) ckMatteImported.mainSource.conformFrameRate = Number(fps);
        if (samJunkImported) samJunkImported.mainSource.conformFrameRate = Number(fps);

        // Create a dedicated precomp for all CK layers so the main comp gets ONE
        // clean layer instead of 4-5 loose layers. Double-click the precomp to paint.
        var srcName = (layer.source && layer.source.name) ? layer.source.name.replace(/\.[^.]+$/, '') : layer.name;
        var ckCompDuration = (comp.workAreaDuration > 0) ? comp.workAreaDuration : comp.duration;
        var ckComp = app.project.items.addComp("CK " + srcName, comp.width, comp.height, comp.pixelAspect, ckCompDuration, comp.frameRate);

        // Add layers in reverse stack order — each layers.add() inserts at pos 1.
        // Final stack top→bottom: SAM JUNK MASK | CK MATTE | keyed clip.
        var _warnings = [];
        var ckLayer = null;
        if (importedSeq) {
            ckLayer = ckComp.layers.add(importedSeq);
            ckLayer.name = "CK KEY";
            ckLayer.startTime = 0;
        }
        if (ckMatteImported) {
            try {
                var ckmLayer = ckComp.layers.add(ckMatteImported);
                ckmLayer.name = "CK MATTE";
                ckmLayer.startTime = 0;
                ckmLayer.enabled = false;  // Berto: matte is reference only — once it lands, only CK KEY shows
            } catch (eCkm) { _warnings.push("CK matte layer: " + String(eCkm)); }
        }
        if (samJunkImported) {
            try {
                var sjLayer = ckComp.layers.add(samJunkImported);
                sjLayer.name = "SAM JUNK MASK";
                sjLayer.startTime = 0;
                sjLayer.enabled = false;  // utility matte — off by default
                // Simple Choker: negative choke spreads the junk mask inward by ~20px at 1080p.
                try {
                    var sjChoker = sjLayer.property("ADBE Effect Parade").addProperty("ADBE Simple Choker");
                    sjChoker.property("Choke Matte").setValue(0);
                } catch (eChoker) { _warnings.push("SAM JUNK choker: " + String(eChoker)); }
            } catch (eSj) { _warnings.push("SAM JUNK MASK layer: " + String(eSj)); }
        }
        if (samJunkImported) {
            try {
                var sjGuide = ckComp.layers.addText("JUNK MASK — adjust Simple Choker \"Choke Matte\" to taste (negative = spread)");
                sjGuide.name = "JUNK MASK — adjust Simple Choker choke to taste";
                sjGuide.locked = true;
                sjGuide.guideLayer = true;
                sjGuide.startTime = 0;
                try {
                    var sjTxt = sjGuide.property("ADBE Text Properties").property("ADBE Text Document").value;
                    sjTxt.fontSize = 18;
                    sjTxt.fillColor = [0.0, 0.78, 0.90];
                    sjGuide.property("ADBE Text Properties").property("ADBE Text Document").setValue(sjTxt);
                } catch (_) {}
            } catch (eSjG) { _warnings.push("SAM JUNK guide text: " + String(eSjG)); }
        }

        // Drop the precomp as a single layer in the main comp above the source clip.
        var ckPrecompLayer = comp.layers.add(ckComp);
        ckPrecompLayer.moveBefore(layer);
        ckPrecompLayer.startTime = Number(compStartTime);

        if (String(hideSource) === 'true') {
            layer.enabled = false;
        }
        app.endUndoGroup();
        comp.time = comp.time;
        return JSON.stringify({ ok: true, ckMatteImported: !!ckMatteImported, samJunkImported: !!samJunkImported, via: via, warnings: _warnings.length ? _warnings : undefined });
    } catch (e) { return JSON.stringify({ ok: false, error: String(e) }); }
}

// ============================================================
// ADD MASK — drop a rectangle mask on CK KEY or CK SAM layer
// ============================================================
// WHAT IT DOES: Finds the CK precomp in the active comp, locates the named
//   layer inside it ("CK KEY" or "CK SAM"), and adds a rectangle mask covering
//   the full frame. User then resizes/moves/keyframes the mask natively in AE.
// WHY: Enables clean CK+SAM compositing — CK mask cuts garbage, SAM mask
//   isolates feet/limbs that extend off the green screen.
// AFFECTS: Adds one mask property to one layer inside the precomp. Fully undoable.
function ck_addLayerMask(layerName, maskModeStr) {
    try {
        var comp = app.project.activeItem;
        if (!(comp instanceof CompItem)) return JSON.stringify({ ok: false, error: "No active comp" });

        // Case A: active comp IS the CK precomp (user double-clicked into it)
        // Case B: active comp is the parent — find the CK precomp layer inside it
        var innerComp = null;
        if (comp.name.indexOf("CK ") === 0) {
            innerComp = comp;
        } else {
            for (var i = 1; i <= comp.layers.length; i++) {
                var L = comp.layers[i];
                try {
                    if ((L.source instanceof CompItem) && L.source.name.indexOf("CK ") === 0) {
                        innerComp = L.source;
                        break;
                    }
                } catch (_) {}
            }
        }
        // Case C: scan entire project for any comp named "CK *"
        if (!innerComp) {
            for (var k = 1; k <= app.project.numItems; k++) {
                var item = app.project.items[k];
                if ((item instanceof CompItem) && item.name.indexOf("CK ") === 0) {
                    innerComp = item;
                    break;
                }
            }
        }
        if (!innerComp) return JSON.stringify({ ok: false, error: "No CK precomp found. Run COMMIT first." });
        var target = null;
        for (var j = 1; j <= innerComp.layers.length; j++) {
            if (innerComp.layers[j].name === layerName) { target = innerComp.layers[j]; break; }
        }
        if (!target) return JSON.stringify({ ok: false, error: "Layer \"" + layerName + "\" not found in precomp." });

        // Resolve mask mode
        var mode = MaskMode.ADD;
        if (maskModeStr === "subtract") mode = MaskMode.SUBTRACT;
        else if (maskModeStr === "intersect") mode = MaskMode.INTERSECT;

        app.beginUndoGroup("CK Add Mask: " + layerName);
        var masks = target.property("ADBE Mask Parade");
        var newMask = masks.addProperty("ADBE Mask Atom");
        newMask.maskMode = mode;

        // Default rectangle = full frame
        var w = innerComp.width;
        var h = innerComp.height;
        var s = new Shape();
        s.vertices = [[0, 0], [w, 0], [w, h], [0, h]];
        s.closed = true;
        s.inTangents  = [[0,0],[0,0],[0,0],[0,0]];
        s.outTangents = [[0,0],[0,0],[0,0],[0,0]];
        newMask.property("ADBE Mask Shape").setValue(s);
        app.endUndoGroup();

        return JSON.stringify({ ok: true, layer: layerName, mode: maskModeStr });
    } catch (e) { return JSON.stringify({ ok: false, error: String(e) }); }
}

// ============================================================
// SAM PRECOMP — create 3-layer precomp from CK+SAM render
// ============================================================
// WHAT IT DOES: Imports the merged CK+SAM result + matte sequences + the CK FULL clip
//   and wraps them in a precomp "CK Comp [clip name]" placed above the source layer.
//   Layer stack top→bottom: CK FULL (hair rescue) | SAM MATTE | CK MERGED | CK MATTE.
//   SAM MATTE drives CK FULL and CK MERGED as luma-inverted garbage mattes.
// WHEN TO CALL: after a SAM batch render finishes (doSAMCommit in index.html).
// ARGS (paths corrected 2026-06-14 — old comments said mattes/ + sam_mattes/, wrong):
//   mergedFirstFramePath  — path to output_00000.png (CK+SAM merged BGRA)
//   ckMatteFirstFramePath — path to CK_ALPHA/CK_ALPHA_00000.png (raw CK alpha B&W)
//   samMatteFirstFramePath— path to GARBAGE_MATTE/ (green-aware) or SAM_JUNK/ (fallback), white=junk
//   fps                   — clip frame rate
//   compStartTime         — start time in main comp (seconds)
//   sourceFsName          — source file fsName for layer-lock fallback
//   lockHandleJson        — layer lock handle JSON from ae_lockLayer
//   advanced              — "true" = show matte layers; "false" = hide them
//   ckOnlyFirstFramePath  — path to CK_ONLY/CK_ONLY_00000.png (full-hair CK clip)
// WHAT IT DOES: Adds a single duration/span layer marker (start=0, spans the given
//   duration) carrying `text` as the marker comment, so the instruction reads directly
//   on the layer's timeline bar without the hidden Comment column enabled.
// WHY (Berto 2026-07-14): comments only show if that column is manually turned on;
//   markers render on the bar / on hover / on double-click by default.
// AFFECTS: mutates `layer`'s Marker property stream. Never throws — caller wraps each
//   call in its own try/catch so one failed marker never aborts the comp build.
function _addLayerMarker(layer, text, duration) {
    var mv = new MarkerValue(String(text));
    mv.duration = Number(duration) || 0;
    layer.property("Marker").setValueAtTime(0, mv);
}

function ae_createSAMPrecomp(mergedFirstFramePath, ckMatteFirstFramePath, samMatteFirstFramePath,
                              fps, compStartTime, sourceFsName, lockHandleJson, advanced, ckOnlyFirstFramePath,
                              tightSamFirstFramePath) {
    try {
        var isAdvanced = (String(advanced) === "true");
        var _warnings = [];
        // Version tripwire (audit 2026-06-14): this function gained a 9th arg
        // (ckOnlyFirstFramePath). If AE is running a STALE host.jsx (panel reloaded
        // but AE not fully restarted), the 9th arg arrives undefined and CK FULL goes
        // missing with no error. Surface it so the operator knows to restart AE.
        if (typeof ckOnlyFirstFramePath === "undefined") {
            _warnings.push("Stale host.jsx (no CK FULL arg) — fully quit + relaunch AE to load the current import code.");
        }
        if (typeof tightSamFirstFramePath === "undefined") {
            _warnings.push("Stale host.jsx (no TIGHT SAM arg) — fully quit + relaunch AE to load the current import code.");
        }
        var activeComp = app.project.activeItem;
        var res = ck_resolveLockedLayer((activeComp instanceof CompItem) ? activeComp : null, lockHandleJson);
        var comp = res.comp, layer = res.layer, via = res.via;
        if (!layer) {
            if (!(comp instanceof CompItem)) comp = activeComp;
            if (comp instanceof CompItem) { layer = comp.selectedLayers[0] || null; if (layer) via = "selected-fallback"; }
        }
        if (!layer && sourceFsName) {
            for (var ci = 1; ci <= app.project.numItems; ci++) {
                var cIt = app.project.item(ci);
                if (!(cIt instanceof CompItem)) continue;
                for (var li = 1; li <= cIt.numLayers; li++) {
                    var cL = cIt.layer(li);
                    if (cL.source && cL.source.file && cL.source.file.fsName === sourceFsName) {
                        comp = cIt; layer = cL; via = "project-scan"; break;
                    }
                }
                if (layer) break;
            }
        }
        if (!(comp instanceof CompItem)) return JSON.stringify({ ok: false, error: "No composition (lock: " + via + ")" });
        if (!layer) return JSON.stringify({ ok: false, error: "Could not resolve source layer (lock: " + via + ")" });

        var mergedFirst = new File(mergedFirstFramePath);
        if (!mergedFirst.exists) return JSON.stringify({ ok: false, error: "Merged sequence not found: " + mergedFirstFramePath });

        app.beginUndoGroup("CorridorKey SAM Precomp");

        // Import sequences
        var mergedOpts = new ImportOptions(mergedFirst);
        mergedOpts.sequence = true;
        var mergedSeq = app.project.importFile(mergedOpts);
        mergedSeq.mainSource.conformFrameRate = Number(fps);

        var ckMatteSeq = null;
        if (ckMatteFirstFramePath) {
            try {
                var ckmFile = new File(String(ckMatteFirstFramePath));
                if (ckmFile.exists) {
                    var ckmOpts = new ImportOptions(ckmFile); ckmOpts.sequence = true;
                    ckMatteSeq = app.project.importFile(ckmOpts);
                    ckMatteSeq.mainSource.conformFrameRate = Number(fps);
                }
            } catch (eCkm) {}
        }

        var samMatteSeq = null;
        if (samMatteFirstFramePath) {
            try {
                var samFile = new File(String(samMatteFirstFramePath));
                if (samFile.exists) {
                    var samOpts = new ImportOptions(samFile); samOpts.sequence = true;
                    samMatteSeq = app.project.importFile(samOpts);
                    samMatteSeq.mainSource.conformFrameRate = Number(fps);
                }
            } catch (eSam) {}
        }

        // Tight SAM matte (SAM_JUNK): raw inverted-SAM, no green-aware dilation.
        // Secondary garbage matte for off-green shots. Null if same as wide (fusion render
        // where GARBAGE_MATTE was absent and wide already fell back to SAM_JUNK).
        var tightSamSeq = null;
        var _effectiveTightPath = (tightSamFirstFramePath && tightSamFirstFramePath !== samMatteFirstFramePath)
            ? tightSamFirstFramePath : null;
        if (_effectiveTightPath) {
            try {
                var tsmFile = new File(String(_effectiveTightPath));
                if (tsmFile.exists) {
                    var tsmOpts = new ImportOptions(tsmFile); tsmOpts.sequence = true;
                    tightSamSeq = app.project.importFile(tsmOpts);
                    tightSamSeq.mainSource.conformFrameRate = Number(fps);
                }
            } catch (eTsm) {}
        }

        // CK_ONLY (Berto 2026-06-14): the CK key alone (full hair + junk, no SAM clip),
        // RGBA. For the hair-rescue workflow — matte-box the head, lay over CK MERGED.
        var ckOnlySeq = null;
        if (ckOnlyFirstFramePath) {
            try {
                var ckoFile = new File(String(ckOnlyFirstFramePath));
                if (ckoFile.exists) {
                    var ckoOpts = new ImportOptions(ckoFile); ckoOpts.sequence = true;
                    ckOnlySeq = app.project.importFile(ckoOpts);
                    ckOnlySeq.mainSource.conformFrameRate = Number(fps);
                }
            } catch (eCko) {}
        }

        // Create precomp
        var srcName = (layer.source && layer.source.name) ? layer.source.name.replace(/\.[^.]+$/, '') : layer.name;
        var ckCompDuration = (comp.workAreaDuration > 0) ? comp.workAreaDuration : comp.duration;
        var ckComp = app.project.items.addComp("CK Comp " + srcName, comp.width, comp.height, comp.pixelAspect, ckCompDuration, comp.frameRate);

        // Build the compositing stack. Final top→bottom:
        //   guide text (no render) | GARBAGE MASK (OFF, unwired) | CK + SAM AI OUTPUT (ON, no track matte) | CK MASTER (OFF, raw)
        // Add in reverse: each layers.add() inserts at index 1 (top), so first-added ends at bottom.
        // DEFAULT 2026-07-12 (Berto, after the butt/shape-kill fixes cleaned the auto result):
        // "keep CK and SAM selected as the main one, keep CK there but not selected... take off
        // the mask, we don't need it anymore, but leave the mask on for CorridorKey [= keep the
        // GARBAGE MASK layer present, off, available]." So CK + SAM AI OUTPUT is the visible
        // default with NO GARBAGE MASK track matte wired (the SAM garbage cut is already baked
        // into the merged sequence upstream in corridorkey_sam_merge.py — the AE track matte was
        // a redundant second cut, no longer needed now the merge is clean). GARBAGE MASK layer
        // still ships in the stack, OFF and unwired, so any shot that regresses can re-enable it.
        // CK MASTER stays OFF and RAW (no matte) — untouched full-hair CK key for hand-mask work.
        // Supersedes the 2026-07-10 CK-MASTER-default flip and the 2026-07-03 matte wiring (git a23409a).
        // CK MASTER — raw CK clip, NO matte (Berto 2026-07-12), user draws masks here
        var ckoLayer = null;
        if (ckOnlySeq) {
            try {
                ckoLayer = ckComp.layers.add(ckOnlySeq);        // added 1st → bottom
                ckoLayer.name = "CK MASTER";
                ckoLayer.comment = "Raw CK key, full hair — for hand-mask rescue. Same junk fix works here: turn on GARBAGE MASK, set it as this layer's Track Matte, LUMA INVERTED.";
                ckoLayer.startTime = 0;
                ckoLayer.enabled = false;   // raw backup since 2026-07-12 (see stack note above)
                try {
                    var ckoChoke = ckoLayer.property("ADBE Effect Parade").addProperty("ADBE Simple Choker");
                    ckoChoke.property("Choke Matte").setValue(0);
                } catch (eChoke) {}
                try {
                    _addLayerMarker(ckoLayer, "Raw CK, full hair — hand-mask rescue. Junk fix works here too: GARBAGE MASK as Track Matte, LUMA INVERTED.", ckCompDuration);
                } catch (eMarkCko) { _warnings.push("CK MASTER marker: " + String(eMarkCko)); }
            } catch (ecko2) {}
        }
        // CK + SAM AI OUTPUT — merged CK with SAM garbage matte; the visible default
        // again since 2026-07-12 (see stack note above).
        var mergedLayer = ckComp.layers.add(mergedSeq);        // added 2nd → above CK MASTER
        mergedLayer.name = "CK + SAM AI OUTPUT";
        mergedLayer.comment = "This is your finished key — most shots, you're done. Leftover junk on a shot? Turn on GARBAGE MASK, set it as this layer's Track Matte, LUMA INVERTED.";
        mergedLayer.startTime = 0;
        mergedLayer.enabled = true;   // visible default (Berto 2026-07-12 re-flip)
        try {
            var mergedChoke = mergedLayer.property("ADBE Effect Parade").addProperty("ADBE Simple Choker");
            mergedChoke.property("Choke Matte").setValue(0);   // neutral; tune per shot
        } catch (eMergedChoke) {}
        try {
            _addLayerMarker(mergedLayer, "YOUR FINISHED KEY — most shots you're done. Junk on a shot? Turn on GARBAGE MASK, set it as this layer's Track Matte, LUMA INVERTED.", ckCompDuration);
        } catch (eMarkMerged) { _warnings.push("CK + SAM AI OUTPUT marker: " + String(eMarkMerged)); }
        // GARBAGE MASK — SAM junk-mask source. Ships OFF and UNWIRED (Berto 2026-07-12:
        // "take off the mask... but leave the mask on for CorridorKey"). The merged CK + SAM
        // default already has this cut baked in upstream, so it needs no track matte here;
        // this layer stays available for a shot that regresses.
        var tightSamLayer = null;
        if (tightSamSeq) {
            try {
                tightSamLayer = ckComp.layers.add(tightSamSeq); // added 3rd → above CK + SAM AI OUTPUT
                tightSamLayer.name = "GARBAGE MASK";
                tightSamLayer.comment = "Junk cleaner, OFF by default. Need it? Turn me on, then set me as the Track Matte (LUMA INVERTED) on CK + SAM or CK MASTER. Trim to just the bad frames.";
                tightSamLayer.startTime = 0;
                tightSamLayer.enabled = false;
                try {
                    _addLayerMarker(tightSamLayer, "Junk cleaner, OFF by default. Turn me on, set me as the Track Matte (LUMA INVERTED) on CK + SAM or CK MASTER.", ckCompDuration);
                } catch (eMarkTsl) { _warnings.push("GARBAGE MASK marker: " + String(eMarkTsl)); }
            } catch (eTsl) {}
        }
        // Per-layer instructions live in each layer's .comment (Comment column), set above.
        // No on-screen guide layer (Berto 2026-06-21: notes belong ON the layer, not a text layer).

        // NO track matte wired by default (Berto 2026-07-12): the merged CK + SAM sequence is
        // already clean, so nothing needs the GARBAGE MASK cut on top. CK + SAM is the visible
        // default; CK MASTER is OFF and raw. Both stay untouched here.

        // Drop precomp in main comp above source, hide source
        var precompLayer = comp.layers.add(ckComp);
        precompLayer.moveBefore(layer);
        precompLayer.startTime = Number(compStartTime);
        layer.enabled = false;

        app.endUndoGroup();
        comp.time = comp.time;
        return JSON.stringify({ ok: true, compName: ckComp.name, advanced: isAdvanced,
            ckMatte: !!ckMatteSeq, samMatte: !!samMatteSeq, tightSamMatte: !!tightSamSeq, ckFull: !!ckoLayer, via: via,
            warnings: _warnings.length ? _warnings : undefined });
    } catch (e) { return JSON.stringify({ ok: false, error: String(e) }); }
}

// ============================================================
// PREMIERE PRO — track placement helper (v1.0 two-mask)
// ============================================================

// WHAT IT DOES: Returns the highest video-track number (1-based, user-visible Vn)
//   in `seq` that currently contains at least one clip. Returns 0 if every track
//   is empty.
// WHY: v1.0 placement must never overwrite an existing clip on a higher track.
//   Source on V1 + leftover output on V3 → next CK render lands on V4, not V2.
// AFFECTS: pure read; returns int.
function ppro_highest_used_video_track(seq) {
    var n = 0;
    try { n = Number(seq.videoTracks.numTracks) || 0; } catch (e) { n = 0; }
    var highest = 0;
    for (var i = 0; i < n; i++) {
        try {
            var tr = seq.videoTracks[i];
            if (tr && tr.clips && tr.clips.numItems > 0) {
                highest = i + 1; // user-visible Vn (1-based)
            }
        } catch (e) {
            // Skip unreadable tracks rather than aborting placement entirely.
        }
    }
    return highest;
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

        // Source-media TIME in seconds. NO -1 frame adjustment: getPlayerPosition()
        // reports the START boundary of the frame under the CTI (verified 2026-07-17,
        // Berto's tile compare on A001_02091949_C020.braw — the displayed frame
        // matched the un-shifted decode). A -1 lived here until then on the belief
        // that playerPos reports the NEXT boundary; it shifted every key-frame
        // extract and SAM anchor one frame back, pushing the anchor OUTSIDE the
        // render range so the dots re-anchored onto the wrong frame's image
        // (chewed mattes on fast motion). Python seeks by CAP_PROP_POS_MSEC
        // (accurate across long-GOP codecs + fps mismatches), not by frame number.
        var sourceTimeSec = playerPos.seconds - targetClip.start.seconds + targetClip.inPoint.seconds;
        if (sourceTimeSec < 0) sourceTimeSec = 0;

        // sourceFrame = the absolute source-media frame under the playhead. Used ONLY as
        // the SAM2 anchor (which frame the click points attach to). Without it the panel
        // sends no anchor and a mid-range click silently degrades to forward-only
        // propagation from frame 0 (ae_processor.py cmd_batch). Mirrors ae_getFrameInfo.
        // DANGER ZONE FRAGILE: do NOT use sourceFrame for keying math — keying uses
        //   sourceTimeSeconds + the source's own fps in Python. Frames here use the
        //   SEQUENCE fps and would reintroduce drift (ALIGNMENT.md Behavior 2). SAM
        //   tolerates a frame of anchor slop because it propagates both directions.
        var sourceFrame = Math.round(sourceTimeSec * fps);

        return JSON.stringify({
            ok: true,
            sourceFile: filePath,
            sourceTimeSeconds: sourceTimeSec,
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

        // v1.0 placement — find highest-used video track so we never overwrite
        // a previous-run output. CK keyed clip lands on max(source, used) + 1.
        // Source assumed on V1 (the input clip the panel keys); fall back to
        // (highest_used + 1) when source detection fails.
        var highestUsed = ppro_highest_used_video_track(seq);
        var ckTrackV = (highestUsed >= 1) ? (highestUsed + 1) : 2;
        var ckTrackIdx = ckTrackV - 1; // JSX videoTracks is 0-indexed
        // Make sure that track exists; addTracks tops up by one as needed.
        while (seq.videoTracks.numTracks <= ckTrackIdx) {
            try { seq.videoTracks.addTracks(1); } catch (_) { break; }
        }
        var v2 = seq.videoTracks[ckTrackIdx];
        if (!v2) return JSON.stringify({ ok: true, placed: false, note: "Imported but V" + ckTrackV + " unavailable" });

        var placeSec = Number(playheadSeconds);
        if (isNaN(placeSec) || placeSec < 0) placeSec = 0;
        try {
            v2.overwriteClip(imported, placeSec);
            return JSON.stringify({ ok: true, placed: true, trackV: ckTrackV });
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
function ppro_importSequence(firstFramePath, startSeconds, fps, sourceFrameRate, samFirstFramePath, ckOnlyFirstFramePath, durationSeconds) {
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

        // v1.0 two-mask: optional SAM matte sidecar sequence. Imported the same
        // way as the CK sequence but tracked separately so we can place it on
        // the track above CK. Snapshot before the second import so we diff
        // against the post-CK state, not the pre-CK state.
        var samImported = null;
        if (samFirstFramePath) {
            try {
                var samFile = new File(String(samFirstFramePath));
                if (samFile.exists) {
                    var beforeIdsSam = {};
                    for (var iSam = 0; iSam < root.children.numItems; iSam++) {
                        var chSam = root.children[iSam];
                        beforeIdsSam[chSam.nodeId || String(iSam) + "-" + chSam.name] = true;
                    }
                    var okSam = app.project.importFiles([String(samFirstFramePath)], true, root, true);
                    if (okSam) {
                        for (var jSam = 0; jSam < root.children.numItems; jSam++) {
                            var cSam = root.children[jSam];
                            var idSam = cSam.nodeId || String(jSam) + "-" + cSam.name;
                            if (!beforeIdsSam[idSam]) { samImported = cSam; break; }
                        }
                    }
                }
            } catch (eSamImport) {
                // Non-fatal — CK still goes through.
            }
        }

        // CK_ONLY sidecar (CK MASTER layer) — same snapshot-diff import pattern.
        var ckOnlyImported = null;
        if (ckOnlyFirstFramePath) {
            try {
                var ckoFile = new File(String(ckOnlyFirstFramePath));
                if (ckoFile.exists) {
                    var beforeIdsCko = {};
                    for (var iCko = 0; iCko < root.children.numItems; iCko++) {
                        var chCko = root.children[iCko];
                        beforeIdsCko[chCko.nodeId || String(iCko) + "-" + chCko.name] = true;
                    }
                    var okCko = app.project.importFiles([String(ckOnlyFirstFramePath)], true, root, true);
                    if (okCko) {
                        for (var jCko = 0; jCko < root.children.numItems; jCko++) {
                            var cCko = root.children[jCko];
                            var idCko = cCko.nodeId || String(jCko) + "-" + cCko.name;
                            if (!beforeIdsCko[idCko]) { ckOnlyImported = cCko; break; }
                        }
                    }
                }
            } catch (eCkoImport) {
                // Non-fatal — CK + garbage still go through.
            }
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

        // Layer names — Premiere parity with the AE precomp stack.
        try { imported.name = "CK + SAM AI OUTPUT"; } catch (_) {}
        try { if (samImported) samImported.name = "GARBAGE MASK"; } catch (_) {}
        try { if (ckOnlyImported) ckOnlyImported.name = "CK MASTER"; } catch (_) {}

        // Move the new item into the CorridorKey bin (create if missing). If the move
        // fails we leave it at the root — still visible to the user.
        var ckBin = null;
        for (var k = 0; k < root.children.numItems; k++) {
            var kc = root.children[k];
            if (kc.name === "CorridorKey" && kc.type === 2) { ckBin = kc; break; }
        }
        if (!ckBin) { try { ckBin = root.createBin("CorridorKey"); } catch (_) {} }
        if (ckBin) { try { imported.moveBin(ckBin); } catch (_) {} }

        // Nested-sequence placement — AE-precomp parity. Build one nested sequence
        // containing CK + SAM + CK MASTER stacked (aux disabled), then place that
        // single nest on the user's main timeline. Falls back to the old flat
        // placement below if nesting isn't available in this Premiere build.
        var mainSeq = seq;
        var placeSec = Number(startSeconds);
        if (isNaN(placeSec) || placeSec < 0) placeSec = 0;
        // NO nudge — place exactly at startSeconds. A +1-frame nudge lived here
        // until 2026-07-16 to compensate for "Premiere dropping the first frame of
        // a numbered-stills import". That drop was a misdiagnosis of our own dirty
        // frame 0 (pre-FIX-C CAP_PROP_POS_MSEC decode); output_00000.png is the
        // REAL first frame (pixel-verified against the source), Premiere keeps it,
        // and the nudge landed every render one frame late on the timeline —
        // Berto's long-standing "one frame off". Do not re-add.

        var nestSeq = null;
        var nestErr = "";
        var _nestName = "CK " + (imported.name || "Render");
        try {
            if (typeof app.project.createNewSequenceFromClips === "function") {
                var seqCountBefore = app.project.sequences.numSequences;
                var newSeqObj = app.project.createNewSequenceFromClips(_nestName, [imported], ckBin || app.project.rootItem);
                // Some versions return the Sequence, some return undefined — resolve by diff.
                if (newSeqObj && newSeqObj.sequenceID) { nestSeq = newSeqObj; }
                else if (app.project.sequences.numSequences > seqCountBefore) {
                    nestSeq = app.project.sequences[app.project.sequences.numSequences - 1];
                }
            } else {
                nestErr = "createNewSequenceFromClips unavailable";
            }
        } catch (eNest) { nestErr = String(eNest); }

        var samPlaced = false;
        var ckOnlyPlaced = false;
        var trackV = 0;
        var samTrackV = 0;
        var mode = "flat";
        var nestProjectItem = null;

        if (nestSeq) {
            // createNewSequenceFromClips already placed `imported` on V1 at 0 — do
            // not place it again. Ensure V2 (SAM) + V3 (CK MASTER) exist.
            // GOTCHA (2026-07-18, root cause of the missing GARBAGE MASK/CK MASTER):
            // TrackCollection.addTracks() does NOT exist in Premiere's plain
            // scripting DOM — it's a QE-DOM method. The call threw into a swallowing
            // catch, the fresh nest kept its single video track, videoTracks[1]/[2]
            // came back undefined, and both aux placements were silently skipped.
            // Use the QE sequence (the nest IS the active sequence right after
            // createNewSequenceFromClips) to add real tracks, and record failure in
            // nestErr instead of swallowing it.
            try { app.enableQE(); } catch (_) {}
            try {
                var _trackGuard = 0;
                while (nestSeq.videoTracks.numTracks < 3 && _trackGuard < 6) {
                    _trackGuard++;
                    var _beforeTracks = nestSeq.videoTracks.numTracks;
                    try { nestSeq.videoTracks.addTracks(1); } catch (_) {}
                    if (nestSeq.videoTracks.numTracks === _beforeTracks) {
                        try { qe.project.getActiveSequence().addTracks(1); } catch (_) {}
                    }
                    if (nestSeq.videoTracks.numTracks === _beforeTracks) {
                        nestErr = nestErr || ("could not add video tracks to nest (stuck at " +
                                              nestSeq.videoTracks.numTracks + ")");
                        break;
                    }
                }
            } catch (eTrk) { nestErr = nestErr || String(eTrk); }

            if (samImported) {
                try {
                    if (typeof samImported.setOverrideFrameRate === "function") {
                        samImported.setOverrideFrameRate(targetRate);
                    } else {
                        var fiSamN = samImported.getFootageInterpretation();
                        if (fiSamN) { fiSamN.frameRate = targetRate; samImported.setFootageInterpretation(fiSamN); }
                    }
                } catch (_) {}
                try { if (ckBin) samImported.moveBin(ckBin); } catch (_) {}
                try {
                    var vSamN = nestSeq.videoTracks[1];
                    if (vSamN) {
                        vSamN.overwriteClip(samImported, 0);
                        samPlaced = true;
                        // AE-parity: auxiliary layers ship OFF inside the nest.
                        try {
                            for (var qiSamN = 0; qiSamN < vSamN.clips.numItems; qiSamN++) {
                                var qcSamN = vSamN.clips[qiSamN];
                                if (Math.abs(qcSamN.start.seconds) < 0.5 / Number(fps || 24)) { qcSamN.disabled = true; break; }
                            }
                        } catch (_) {}
                    }
                } catch (_) {}
            }

            if (ckOnlyImported) {
                try {
                    if (typeof ckOnlyImported.setOverrideFrameRate === "function") {
                        ckOnlyImported.setOverrideFrameRate(targetRate);
                    } else {
                        var fiCkoN = ckOnlyImported.getFootageInterpretation();
                        if (fiCkoN) { fiCkoN.frameRate = targetRate; ckOnlyImported.setFootageInterpretation(fiCkoN); }
                    }
                } catch (_) {}
                try { if (ckBin) ckOnlyImported.moveBin(ckBin); } catch (_) {}
                try {
                    var vCkoN = nestSeq.videoTracks[2];
                    if (vCkoN) {
                        vCkoN.overwriteClip(ckOnlyImported, 0);
                        ckOnlyPlaced = true;
                        // AE-parity: CK MASTER ships OFF inside the nest.
                        try {
                            for (var qiCkoN = 0; qiCkoN < vCkoN.clips.numItems; qiCkoN++) {
                                var qcCkoN = vCkoN.clips[qiCkoN];
                                if (Math.abs(qcCkoN.start.seconds) < 0.5 / Number(fps || 24)) { qcCkoN.disabled = true; break; }
                            }
                        } catch (_) {}
                    }
                } catch (_) {}
            }

            // Locate the nest's own project item so it can be placed on the main
            // timeline, same as any other clip.
            try {
                var _searchRoots = ckBin ? [app.project.rootItem, ckBin] : [app.project.rootItem];
                for (var rIdx = 0; rIdx < _searchRoots.length && !nestProjectItem; rIdx++) {
                    var _kids = _searchRoots[rIdx].children;
                    for (var nIdx = 0; nIdx < _kids.numItems; nIdx++) {
                        var _kid = _kids[nIdx];
                        if (_kid.name === _nestName && _kid.type !== 2) { nestProjectItem = _kid; break; }
                    }
                }
            } catch (eFind) { if (!nestErr) nestErr = String(eFind); }
            if (!nestProjectItem && !nestErr) { nestErr = "nest projectItem not found"; }

            if (nestProjectItem) {
                try {
                    var highestUsedMain = ppro_highest_used_video_track(mainSeq);
                    var nestTrackV = (highestUsedMain >= 1) ? (highestUsedMain + 1) : 2;
                    var nestTrackIdx = nestTrackV - 1;
                    // NEVER-OVERWRITE GUARD (2026-07-18): overwriteClip TRUNCATES
                    // whatever occupies the target range — a wrong track pick here
                    // can chop the user's SOURCE clip to a stub (prime suspect for
                    // Berto's frozen one-frame render: the next render measured a
                    // 1-frame braw remnant). Only place on a track that is EMPTY
                    // across the nest's whole duration; climb until one is found.
                    // Plain-DOM addTracks() does not exist (QE-only), so search
                    // EXISTING tracks first and use QE growth as a last resort —
                    // and REFUSE loudly rather than eat a clip.
                    var _durGuard = (typeof durationSeconds !== "undefined" && Number(durationSeconds) > 0)
                        ? Number(durationSeconds) : (1.0 / Number(fps || 24));
                    var _placeEnd = placeSec + _durGuard;
                    var _trackFreeInRange = function (trk) {
                        try {
                            for (var fi2 = 0; fi2 < trk.clips.numItems; fi2++) {
                                var fc = trk.clips[fi2];
                                if (fc.start.seconds < _placeEnd && fc.end.seconds > placeSec) return false;
                            }
                            return true;
                        } catch (_) { return false; }
                    };
                    var _chosenIdx = -1;
                    for (var ti2 = nestTrackIdx; ti2 < mainSeq.videoTracks.numTracks; ti2++) {
                        if (_trackFreeInRange(mainSeq.videoTracks[ti2])) { _chosenIdx = ti2; break; }
                    }
                    if (_chosenIdx < 0) {
                        // Grow the MAIN sequence via QE (needs to be the active sequence).
                        try {
                            if (typeof app.project.openSequence === "function" && mainSeq.sequenceID) {
                                app.project.openSequence(mainSeq.sequenceID);
                            }
                            app.enableQE();
                            qe.project.getActiveSequence().addTracks(1);
                            var _newIdx = mainSeq.videoTracks.numTracks - 1;
                            if (_trackFreeInRange(mainSeq.videoTracks[_newIdx])) _chosenIdx = _newIdx;
                        } catch (_) {}
                    }
                    if (_chosenIdx < 0) {
                        throw new Error("no empty video track for the nested clip — refusing to overwrite existing clips");
                    }
                    var vNest = mainSeq.videoTracks[_chosenIdx];
                    vNest.overwriteClip(nestProjectItem, placeSec);
                    trackV = _chosenIdx + 1;
                    mode = "nested";
                } catch (eMainPlace) {
                    if (!nestErr) nestErr = String(eMainPlace);
                }
            }
        }

        // Restore the user's own sequence as active — createNewSequenceFromClips
        // may have switched focus to the nest.
        try {
            if (mainSeq && typeof app.project.openSequence === "function" && mainSeq.sequenceID) {
                app.project.openSequence(mainSeq.sequenceID);
            }
        } catch (_) {}

        // FALLBACK — old flat placement (pre-nesting behavior). Used when
        // createNewSequenceFromClips is unavailable, the nest's project item
        // couldn't be located, or placing the nest on the main timeline failed.
        if (mode !== "nested") {
            samPlaced = false;
            ckOnlyPlaced = false;

            // v1.0 placement — find highest-used video track so we never overwrite
            // previous-run output. CK keyed sequence lands on max(source, used)+1.
            var highestUsedSeq = ppro_highest_used_video_track(mainSeq);
            var ckTrackVSeq = (highestUsedSeq >= 1) ? (highestUsedSeq + 1) : 2;
            var ckTrackIdxSeq = ckTrackVSeq - 1;
            // SAM sidecar (when present) lives on the track immediately above CK.
            var samTrackVSeq = samImported ? (ckTrackVSeq + 1) : 0;
            var samTrackIdxSeq = samImported ? (samTrackVSeq - 1) : -1;
            // CK MASTER (CK_ONLY) lives on the track immediately above SAM (or above
            // CK if no SAM matte is present).
            var ckOnlyTrackVSeq = ckOnlyImported ? ((samImported ? samTrackVSeq : ckTrackVSeq) + 1) : 0;
            var ckOnlyTrackIdxSeq = ckOnlyImported ? (ckOnlyTrackVSeq - 1) : -1;
            var topNeededIdx = ckOnlyImported ? ckOnlyTrackIdxSeq : (samImported ? samTrackIdxSeq : ckTrackIdxSeq);
            while (mainSeq.videoTracks.numTracks <= topNeededIdx) {
                try { mainSeq.videoTracks.addTracks(1); } catch (_) { break; }
            }
            var v2 = mainSeq.videoTracks[ckTrackIdxSeq];
            if (!v2) {
                return JSON.stringify({ ok: true, placed: false, note: "Imported but V" + ckTrackVSeq + " unavailable",
                    diag: { mainName: imported.name,
                        mainPath: (function () { try { return imported.getMediaPath(); } catch (_) { return ""; } })(),
                        mode: "flat", nestErr: nestErr, nestFallback: nestErr } });
            }

            try {
                v2.overwriteClip(imported, placeSec);
            } catch (e) {
                return JSON.stringify({ ok: true, placed: false, binName: imported.name,
                    note: "Imported into bin but overwriteClip failed: " + String(e),
                    diag: { mainName: imported.name,
                        mainPath: (function () { try { return imported.getMediaPath(); } catch (_) { return ""; } })(),
                        mode: "flat", nestErr: nestErr, nestFallback: nestErr } });
            }

            // SAM matte sidecar — conform rate, move into bin, place above CK.
            // Failures here don't roll back CK placement; the user keeps the keyed
            // sequence either way.
            if (samImported) {
                try {
                    if (typeof samImported.setOverrideFrameRate === "function") {
                        samImported.setOverrideFrameRate(targetRate);
                    } else {
                        var fiSam = samImported.getFootageInterpretation();
                        if (fiSam) {
                            fiSam.frameRate = targetRate;
                            samImported.setFootageInterpretation(fiSam);
                        }
                    }
                } catch (_) {}
                try { if (ckBin) samImported.moveBin(ckBin); } catch (_) {}
                try {
                    var vSam = mainSeq.videoTracks[samTrackIdxSeq];
                    if (vSam) {
                        vSam.overwriteClip(samImported, placeSec);
                        samPlaced = true;
                        // AE-parity: auxiliary layers ship OFF (GARBAGE MASK / CK MASTER are rescue
                        // material, not part of the default composite). trackItem.disabled exists on
                        // Premiere 14+; older versions just leave the clip visible (harmless).
                        try {
                            var vTrkSam = mainSeq.videoTracks[samTrackIdxSeq];
                            for (var qiSam = 0; qiSam < vTrkSam.clips.numItems; qiSam++) {
                                var qcSam = vTrkSam.clips[qiSam];
                                if (Math.abs(qcSam.start.seconds - placeSec) < 0.5 / Number(fps || 24)) { qcSam.disabled = true; break; }
                            }
                        } catch (_) {}
                    }
                } catch (_) {}
            }

            // CK MASTER (CK_ONLY) — conform rate, move into bin, place above the SAM
            // matte (or above CK if no SAM matte). Failures here don't roll back
            // CK/SAM placement; the user keeps those either way.
            if (ckOnlyImported) {
                try {
                    if (typeof ckOnlyImported.setOverrideFrameRate === "function") {
                        ckOnlyImported.setOverrideFrameRate(targetRate);
                    } else {
                        var fiCko = ckOnlyImported.getFootageInterpretation();
                        if (fiCko) {
                            fiCko.frameRate = targetRate;
                            ckOnlyImported.setFootageInterpretation(fiCko);
                        }
                    }
                } catch (_) {}
                try { if (ckBin) ckOnlyImported.moveBin(ckBin); } catch (_) {}
                try {
                    var vCko = mainSeq.videoTracks[ckOnlyTrackIdxSeq];
                    if (vCko) {
                        vCko.overwriteClip(ckOnlyImported, placeSec);
                        ckOnlyPlaced = true;
                        // AE-parity: CK MASTER ships OFF (rescue material, not part of the
                        // default composite).
                        try {
                            var vTrkCko = mainSeq.videoTracks[ckOnlyTrackIdxSeq];
                            for (var qiCko = 0; qiCko < vTrkCko.clips.numItems; qiCko++) {
                                var qcCko = vTrkCko.clips[qiCko];
                                if (Math.abs(qcCko.start.seconds - placeSec) < 0.5 / Number(fps || 24)) { qcCko.disabled = true; break; }
                            }
                        } catch (_) {}
                    }
                } catch (_) {}
            }

            trackV = ckTrackVSeq;
            samTrackV = samImported ? samTrackVSeq : 0;
        } else {
            samTrackV = samImported ? 2 : 0;
        }

        // Read-back diagnostics — so the next "aux clip shows nothing" failure is
        // diagnosable without re-running the whole export. getMediaPath is guarded;
        // sequences/some footage types don't implement it.
        var diag = {
            mainName: imported.name,
            mainPath: (function () { try { return imported.getMediaPath(); } catch (_) { return ""; } })(),
            mode: mode,
            nestErr: nestErr,
            // Placement truth — the import can succeed while a placement silently
            // fails (2026-07-18: nest had 1 video track, aux clips skipped).
            samPlaced: samPlaced,
            ckOnlyPlaced: ckOnlyPlaced,
            nestTracks: (function () { try { return nestSeq ? nestSeq.videoTracks.numTracks : -1; } catch (_) { return -1; } })()
        };
        if (mode !== "nested") { diag.nestFallback = nestErr; }
        if (samImported) {
            diag.samName = samImported.name;
            diag.samPath = (function () { try { return samImported.getMediaPath(); } catch (_) { return ""; } })();
        }
        if (ckOnlyImported) {
            diag.ckoName = ckOnlyImported.name;
            diag.ckoPath = (function () { try { return ckOnlyImported.getMediaPath(); } catch (_) { return ""; } })();
        }

        return JSON.stringify({
            ok: true, placed: true, binName: imported.name, appliedRate: appliedRate,
            trackV: trackV, samPlaced: samPlaced,
            samTrackV: samTrackV,
            ckOnlyPlaced: ckOnlyPlaced,
            diag: diag
        });
    } catch (e) { return JSON.stringify({ ok: false, error: String(e) }); }
}
