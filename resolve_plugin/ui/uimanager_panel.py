# Last modified: 2026-04-13 | Change: HRCS retrofit (documentation only, no logic changes) | Full history: git log
"""
CorridorKey Fusion UIManager Panel
Native DaVinci Resolve Studio UI panel.

WHAT IT DOES:
    Builds the CorridorKey neural keyer UI panel inside DaVinci Resolve using
    Fusion's UIManager API. Provides controls for screen type, despill, refiner,
    despeckle, gamma, output placement, and proxy generation. Runs processing
    in background threads to keep the UI responsive.

DEPENDS-ON:
    - DaVinci Resolve's Fusion UIManager (fusion object passed at creation)
    - fusionscript module from Resolve's Developer/Scripting/Modules directory
    - on_process_callback (Callable) provided by the caller to do actual keying work

AFFECTS:
    - Any caller that imports create_corridorkey_panel (the sole public API of this module)
    - Changing widget IDs breaks event wiring at the bottom of create_corridorkey_panel
    - Changing the settings dict keys in get_settings breaks downstream process callbacks
"""
import threading
from typing import Callable, Optional, Any


# WHAT IT DOES: Builds the entire CorridorKey UI panel, wires events, and returns (window, dispatcher).
# DEPENDS-ON: fusion (Resolve Fusion obj), on_process_callback (keying logic), fusionscript module on disk
# AFFECTS: Everything — this is the only public function. Changing widget IDs, combo items, or settings keys ripples into all callers and the process callback contract.
# DANGER ZONE FRAGILE/HIGH: Widget IDs are string-matched to event handlers at the bottom of this function. Renaming any ID silently breaks that handler.
def create_corridorkey_panel(fusion, on_process_callback: Callable):
    """Create CorridorKey panel using Fusion UIManager.

    Args:
        fusion: Fusion object from Resolve
        on_process_callback: Callback when Process button clicked

    Returns:
        (window, dispatcher) tuple
    """
    ui = fusion.UIManager

    # Import dispatcher
    # DANGER ZONE FRAGILE/HIGH: Hardcoded path to Resolve's scripting modules. Breaks if Resolve is installed to a non-default location or PROGRAMDATA env var is missing.
    import sys
    import os
    resolve_modules = os.path.join(
        os.environ.get("PROGRAMDATA", "C:/ProgramData"),
        "Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting/Modules"
    )
    if resolve_modules not in sys.path:
        sys.path.insert(0, resolve_modules)

    import fusionscript
    disp = fusionscript.UIDispatcher(ui)

    # Panel state
    state = {
        "processing": False,
        "cancelled": False,
    }

    # Create window
    win = disp.AddWindow({
        "ID": "CorridorKeyPanel",
        "WindowTitle": "CorridorKey Neural Keyer",
        "Geometry": [100, 100, 380, 520],
        "Spacing": 10,
        "Margin": 10,
    }, [
        ui.VGroup({"Spacing": 8}, [
            # Header
            ui.Label({
                "ID": "Header",
                "Text": "CorridorKey Neural Green Screen",
                "Alignment": {"AlignHCenter": True},
                "Font": ui.Font({"PixelSize": 14, "Bold": True}),
                "StyleSheet": "color: #4CAF50;",
            }),

            ui.Label({
                "Text": "AI-powered green screen keying",
                "Alignment": {"AlignHCenter": True},
                "StyleSheet": "color: #888;",
            }),

            # Separator
            ui.Label({"Text": "", "MinimumSize": [0, 5]}),

            # Screen Type
            ui.HGroup({"Spacing": 10}, [
                ui.Label({"Text": "Screen Type:", "MinimumSize": [100, 0]}),
                ui.ComboBox({
                    "ID": "ScreenType",
                    "CurrentIndex": 0,
                }),
            ]),

            # Despill Strength
            ui.HGroup({"Spacing": 10}, [
                ui.Label({"Text": "Despill:", "MinimumSize": [100, 0]}),
                ui.Slider({
                    "ID": "DespillSlider",
                    "Minimum": 0,
                    "Maximum": 100,
                    "Value": 50,
                    "Orientation": "Horizontal",
                }),
                ui.Label({"ID": "DespillValue", "Text": "0.50", "MinimumSize": [40, 0]}),
            ]),

            # Refiner Strength
            ui.HGroup({"Spacing": 10}, [
                ui.Label({"Text": "Refiner:", "MinimumSize": [100, 0]}),
                ui.Slider({
                    "ID": "RefinerSlider",
                    "Minimum": 0,
                    "Maximum": 100,
                    "Value": 100,
                    "Orientation": "Horizontal",
                }),
                ui.Label({"ID": "RefinerValue", "Text": "1.00", "MinimumSize": [40, 0]}),
            ]),

            # Despeckle
            ui.HGroup({"Spacing": 10}, [
                ui.CheckBox({
                    "ID": "DespeckleCheck",
                    "Text": "Auto Despeckle",
                    "Checked": True,
                }),
                ui.SpinBox({
                    "ID": "DespeckleSize",
                    "Minimum": 50,
                    "Maximum": 1000,
                    "Value": 400,
                    "SingleStep": 50,
                }),
                ui.Label({"Text": "px min"}),
            ]),

            # Input Type
            ui.HGroup({"Spacing": 10}, [
                ui.Label({"Text": "Input Gamma:", "MinimumSize": [100, 0]}),
                ui.ComboBox({
                    "ID": "InputGamma",
                    "CurrentIndex": 0,
                }),
            ]),

            # Output Placement
            ui.HGroup({"Spacing": 10}, [
                ui.Label({"Text": "Place Result:", "MinimumSize": [100, 0]}),
                ui.ComboBox({
                    "ID": "OutputMode",
                    "CurrentIndex": 0,
                }),
            ]),

            # Proxy Generation
            ui.HGroup({"Spacing": 10}, [
                ui.CheckBox({
                    "ID": "GenerateProxy",
                    "Text": "Generate Proxy (uses project settings)",
                    "Checked": True,
                }),
            ]),

            # Separator
            ui.Label({"Text": "", "MinimumSize": [0, 10]}),

            # Status and Progress
            ui.VGroup({"Spacing": 5}, [
                ui.Label({
                    "ID": "StatusLabel",
                    "Text": "Ready - Select clips on timeline",
                    "Alignment": {"AlignHCenter": True},
                }),
                ui.Label({
                    "ID": "ProgressLabel",
                    "Text": "",
                    "Alignment": {"AlignHCenter": True},
                    "StyleSheet": "color: #4CAF50; font-weight: bold;",
                }),
            ]),

            # Separator
            ui.Label({"Text": "", "MinimumSize": [0, 5]}),

            # Preview Button - process single frame at playhead
            ui.HGroup({"Spacing": 10}, [
                ui.Button({
                    "ID": "PreviewBtn",
                    "Text": "Preview Current Frame",
                    "MinimumSize": [0, 35],
                    "StyleSheet": "background-color: #2196F3; color: white; font-weight: bold;",
                }),
            ]),

            ui.Label({
                "ID": "PreviewHint",
                "Text": "Move playhead to frame you want to preview",
                "Alignment": {"AlignHCenter": True},
                "StyleSheet": "color: #666; font-size: 10px;",
            }),

            # Separator
            ui.Label({"Text": "", "MinimumSize": [0, 5]}),

            # Action Buttons
            ui.HGroup({"Spacing": 10}, [
                ui.Button({
                    "ID": "ProcessBtn",
                    "Text": "Process Selected Clips",
                    "MinimumSize": [0, 35],
                    "StyleSheet": "background-color: #4CAF50; color: white; font-weight: bold;",
                }),
            ]),

            ui.HGroup({"Spacing": 10}, [
                ui.Button({
                    "ID": "ProcessAllBtn",
                    "Text": "Process All on Track 1",
                    "MinimumSize": [0, 30],
                }),
                ui.Button({
                    "ID": "CancelBtn",
                    "Text": "Cancel",
                    "MinimumSize": [0, 30],
                    "Enabled": False,
                }),
            ]),

            # Spacer
            ui.Label({"Text": "", "Weight": 1}),

            # Log output
            ui.TextEdit({
                "ID": "LogOutput",
                "ReadOnly": True,
                "PlainText": True,
                "MinimumSize": [0, 80],
                "StyleSheet": "background-color: #1a1a1a; color: #aaa; font-family: monospace;",
            }),
        ]),
    ])

    # Populate combo boxes
    items = win.GetItems()
    items["ScreenType"].AddItem("Green Screen")
    items["ScreenType"].AddItem("Blue Screen")

    items["InputGamma"].AddItem("sRGB (Video/PNG)")
    items["InputGamma"].AddItem("Linear (EXR)")

    items["OutputMode"].AddItem("Add to Track Above")
    items["OutputMode"].AddItem("MediaPool Only")
    items["OutputMode"].AddItem("Replace Original")

    # Helper functions

    # WHAT IT DOES: Appends a message to the in-panel log widget and prints to console.
    # ISOLATED: No external dependencies beyond the UI items dict.
    def log(message: str):
        """Add message to log output."""
        print(f"LOG: {message}")  # Also print to console
        try:
            current = items["LogOutput"].PlainText or ""
            items["LogOutput"].PlainText = current + message + "\n"
        except Exception as e:
            print(f"Log error: {e}")

    # WHAT IT DOES: Updates the progress percentage and status label in the UI.
    # ISOLATED: No external dependencies beyond the UI items dict.
    def update_progress(current: int, total: int, message: str):
        """Update progress display and status."""
        if total > 0:
            percent = int((current / total) * 100)
            items["ProgressLabel"].Text = f"{percent}%"
        else:
            items["ProgressLabel"].Text = ""
        items["StatusLabel"].Text = message

    # WHAT IT DOES: Toggles all input widgets enabled/disabled during processing to prevent double-runs.
    # DEPENDS-ON: Every widget ID in items dict — adding a new control means adding it here too.
    # DANGER ZONE FRAGILE/HIGH: Missing a widget here lets users change settings mid-process / breaks: race conditions in callback
    def set_processing(processing: bool):
        """Enable/disable UI during processing."""
        state["processing"] = processing
        items["PreviewBtn"].Enabled = not processing
        items["ProcessBtn"].Enabled = not processing
        items["ProcessAllBtn"].Enabled = not processing
        items["CancelBtn"].Enabled = processing
        items["ScreenType"].Enabled = not processing
        items["DespillSlider"].Enabled = not processing
        items["RefinerSlider"].Enabled = not processing
        items["DespeckleCheck"].Enabled = not processing
        items["DespeckleSize"].Enabled = not processing
        items["InputGamma"].Enabled = not processing
        items["OutputMode"].Enabled = not processing
        items["GenerateProxy"].Enabled = not processing

    # WHAT IT DOES: Reads all UI widget values and returns a normalized settings dict for the process callback.
    # DEPENDS-ON: Widget IDs (ScreenType, DespillSlider, RefinerSlider, DespeckleCheck, DespeckleSize, InputGamma, OutputMode, GenerateProxy)
    # AFFECTS: Downstream process callback expects these exact keys — changing a key name breaks processing.
    # DANGER ZONE FRAGILE/HIGH: Dict keys are the contract with on_process_callback / breaks: all keying logic if renamed
    def get_settings() -> dict:
        """Get current settings from UI."""
        return {
            "screen_type": "green" if items["ScreenType"].CurrentIndex == 0 else "blue",
            "despill_strength": items["DespillSlider"].Value / 100.0,
            "refiner_strength": items["RefinerSlider"].Value / 100.0,
            "despeckle_enabled": items["DespeckleCheck"].Checked,
            "despeckle_size": items["DespeckleSize"].Value,
            "input_is_srgb": items["InputGamma"].CurrentIndex == 0,
            "output_mode": ["above", "mediapool", "replace"][items["OutputMode"].CurrentIndex],
            "generate_proxy": items["GenerateProxy"].Checked,
        }

    # Event handlers

    # WHAT IT DOES: Syncs the despill label text when the slider moves.
    # ISOLATED
    def on_despill_changed(ev):
        value = items["DespillSlider"].Value / 100.0
        items["DespillValue"].Text = f"{value:.2f}"

    # WHAT IT DOES: Syncs the refiner label text when the slider moves.
    # ISOLATED
    def on_refiner_changed(ev):
        value = items["RefinerSlider"].Value / 100.0
        items["RefinerValue"].Text = f"{value:.2f}"

    # WHAT IT DOES: Runs a single-frame preview at the playhead in a background thread.
    # DEPENDS-ON: get_settings(), set_processing(), on_process_callback, state dict
    # AFFECTS: Spawns a thread — if callback throws, error lands in log widget only.
    def on_preview_clicked(ev):
        """Preview single frame at playhead."""
        # Immediate visual feedback
        items["StatusLabel"].Text = "CLICKED! Starting preview..."
        items["LogOutput"].PlainText = "Preview button clicked!\nPlease wait...\n"
        log("Initializing preview...")

        if state["processing"]:
            log("Already processing, please wait...")
            return

        state["cancelled"] = False
        set_processing(True)

        settings = get_settings()

        def run_preview():
            try:
                log("Running CorridorKey preview...")
                on_process_callback(
                    settings,
                    "preview",
                    update_progress,
                    log,
                    lambda: state["cancelled"]
                )
                log("Preview complete!")
            except Exception as e:
                log(f"Error: {e}")
                import traceback
                log(traceback.format_exc())
            finally:
                set_processing(False)
                items["ProgressLabel"].Text = ""
                items["StatusLabel"].Text = "Ready"

        thread = threading.Thread(target=run_preview)
        thread.start()

    # WHAT IT DOES: Processes selected timeline clips in a background thread.
    # DEPENDS-ON: get_settings(), set_processing(), on_process_callback, state dict
    def on_process_clicked(ev):
        if state["processing"]:
            return

        state["cancelled"] = False
        set_processing(True)
        log("Starting processing...")

        settings = get_settings()

        def run_process():
            try:
                on_process_callback(
                    settings,
                    "selected",
                    update_progress,
                    log,
                    lambda: state["cancelled"]
                )
            except Exception as e:
                log(f"Error: {e}")
            finally:
                set_processing(False)
                items["ProgressLabel"].Text = ""

        # Run in background thread
        thread = threading.Thread(target=run_process)
        thread.start()

    # WHAT IT DOES: Processes all clips on track 1 in a background thread.
    # DEPENDS-ON: get_settings(), set_processing(), on_process_callback, state dict
    def on_process_all_clicked(ev):
        if state["processing"]:
            return

        state["cancelled"] = False
        set_processing(True)
        log("Processing all clips on track 1...")

        settings = get_settings()

        def run_process():
            try:
                on_process_callback(
                    settings,
                    "all",
                    update_progress,
                    log,
                    lambda: state["cancelled"]
                )
            except Exception as e:
                log(f"Error: {e}")
            finally:
                set_processing(False)
                items["ProgressLabel"].Text = ""

        thread = threading.Thread(target=run_process)
        thread.start()

    # WHAT IT DOES: Sets the cancelled flag so background threads can check and abort gracefully.
    # ISOLATED
    def on_cancel_clicked(ev):
        state["cancelled"] = True
        log("Cancelling...")

    # WHAT IT DOES: Cancels any running work and exits the UIManager event loop (closes the panel).
    # DEPENDS-ON: state dict, disp (dispatcher)
    def on_close(ev):
        state["cancelled"] = True
        disp.ExitLoop()

    # Connect events
    # DANGER ZONE FRAGILE/CRITICAL: Widget IDs here must match the IDs in the UI layout above exactly. Renaming a widget ID without updating this block silently disconnects the handler.
    win.On.DespillSlider.SliderMoved = on_despill_changed
    win.On.RefinerSlider.SliderMoved = on_refiner_changed
    win.On.PreviewBtn.Clicked = on_preview_clicked
    win.On.ProcessBtn.Clicked = on_process_clicked
    win.On.ProcessAllBtn.Clicked = on_process_all_clicked
    win.On.CancelBtn.Clicked = on_cancel_clicked
    win.On.CorridorKeyPanel.Close = on_close

    return win, disp
