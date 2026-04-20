# Last modified: 2026-04-13 | Change: HRCS retrofit (documentation only, no logic changes) | Full history: git log
"""
DaVinci Resolve API Bridge for CorridorKey.

WHAT IT DOES:
    Wraps the DaVinci Resolve scripting API (DaVinciResolveScript) to provide
    timeline inspection, frame export, media pool import, and UI access for the
    CorridorKey plugin.

DEPENDS-ON:
    - DaVinciResolveScript module (ships with DaVinci Resolve, path varies by OS)
    - A running DaVinci Resolve instance with external scripting enabled
    - fusionscript module (optional, for UI dispatcher)

AFFECTS:
    - resolve_plugin/core/* — any module that talks to Resolve goes through this bridge
    - resolve_plugin/ui/* — UI panels obtain UIManager/dispatcher from here
    - Render queue — export_clip_frames creates and deletes render jobs
"""
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Dict, Any

# WHAT IT DOES: Detects OS and adds the correct Resolve scripting module path to sys.path
# ISOLATED: no external dependencies beyond sys/os
# Add Resolve scripting module path
def _add_resolve_paths():
    """Add DaVinci Resolve scripting paths to sys.path."""
    if sys.platform == "win32":
        resolve_script_path = os.path.join(
            os.environ.get("PROGRAMDATA", "C:/ProgramData"),
            "Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting/Modules"
        )
    elif sys.platform == "darwin":
        resolve_script_path = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"
    else:
        resolve_script_path = "/opt/resolve/Developer/Scripting/Modules"

    if resolve_script_path not in sys.path:
        sys.path.insert(0, resolve_script_path)

_add_resolve_paths()

try:
    import DaVinciResolveScript as dvr_script
except ImportError:
    dvr_script = None


# WHAT IT DOES: Custom exception raised when Resolve is not running or scripting module is missing
# AFFECTS: Any caller that catches connection failures (UI layer, CLI entry points)
class ResolveConnectionError(Exception):
    """Could not connect to DaVinci Resolve."""
    pass


# WHAT IT DOES: Central bridge object — holds live Resolve/Fusion handles and exposes all API operations
# DEPENDS-ON: DaVinciResolveScript module, running Resolve instance
# AFFECTS: Every feature that reads/writes timeline data, exports frames, or imports media
class ResolveBridge:
    """Wrapper for DaVinci Resolve scripting API."""

    def __init__(self):
        """Initialize connection to running Resolve instance."""
        self.resolve = None
        self.fusion = None
        self.project = None
        self.timeline = None
        self.media_pool = None
        self._connect()

    # WHAT IT DOES: Establishes live connection to Resolve, caches project/timeline/media_pool handles
    # DEPENDS-ON: dvr_script module (imported at module level), Resolve running with scripting enabled
    # AFFECTS: All other methods — if this fails, nothing else works
    # DANGER ZONE FRAGILE/HIGH/CRITICAL: Resolve must be running BEFORE instantiation / breaks: entire bridge / depends on: external process state
    def _connect(self):
        """Connect to running DaVinci Resolve instance."""
        if dvr_script is None:
            raise ResolveConnectionError(
                "DaVinciResolveScript module not found. "
                "Make sure DaVinci Resolve is installed."
            )

        self.resolve = dvr_script.scriptapp("Resolve")
        if self.resolve is None:
            raise ResolveConnectionError(
                "Could not connect to DaVinci Resolve. "
                "Make sure Resolve is running and scripting is enabled "
                "(Preferences > System > General > External scripting using)."
            )

        # Get Fusion for UIManager access
        self.fusion = self.resolve.Fusion()

        # Get current project and timeline
        pm = self.resolve.GetProjectManager()
        self.project = pm.GetCurrentProject()
        if self.project:
            self.timeline = self.project.GetCurrentTimeline()
            self.media_pool = self.project.GetMediaPool()

    # WHAT IT DOES: Returns all video clips on a single timeline track
    # DEPENDS-ON: self.timeline (set by _connect)
    def get_timeline_video_items(self, track_index: int = 1) -> List[Any]:
        """Get all video items from a timeline track.

        Args:
            track_index: Video track number (1-based)

        Returns:
            List of TimelineItem objects
        """
        if not self.timeline:
            return []
        return self.timeline.GetItemListInTrack("video", track_index) or []

    # WHAT IT DOES: Iterates every video track and collects all clips into a flat list
    # DEPENDS-ON: self.timeline (set by _connect)
    def get_all_video_items(self) -> List[Any]:
        """Get all video items from all tracks."""
        if not self.timeline:
            return []

        items = []
        track_count = self.timeline.GetTrackCount("video")
        for i in range(1, track_count + 1):
            track_items = self.timeline.GetItemListInTrack("video", i)
            if track_items:
                items.extend(track_items)
        return items

    # WHAT IT DOES: Extracts name, frame range, file path, and FPS from a timeline item
    # DEPENDS-ON: self.timeline, timeline_item.GetMediaPoolItem()
    def get_clip_info(self, timeline_item) -> Dict[str, Any]:
        """Get information about a timeline item.

        Returns dict with:
            - name: Clip name
            - start: Start frame
            - end: End frame
            - duration: Duration in frames
            - file_path: Source file path
            - track: Video track number
        """
        mpi = timeline_item.GetMediaPoolItem()
        props = mpi.GetClipProperty() if mpi else {}

        return {
            "name": timeline_item.GetName(),
            "start": timeline_item.GetStart(),
            "end": timeline_item.GetEnd(),
            "duration": timeline_item.GetDuration(),
            "file_path": props.get("File Path", ""),
            "fps": self.timeline.GetSetting("timelineFrameRate") if self.timeline else "24",
        }

    # WHAT IT DOES: Exports frames from a clip via Resolve's render queue (creates job, polls, deletes job)
    # DEPENDS-ON: self.project, self.timeline, get_clip_info(), _frame_to_timecode()
    # AFFECTS: Resolve render queue — adds then removes a render job; writes image files to output_dir
    # DANGER ZONE FRAGILE/HIGH/CRITICAL: Mutates Resolve render queue / breaks: other pending renders / depends on: no concurrent render jobs
    def export_clip_frames(
        self,
        timeline_item,
        output_dir: str,
        format: str = "PNG",
        progress_callback=None
    ) -> Dict[str, Any]:
        """Export frames from a timeline item.

        Uses Resolve's render queue to export frames.

        Args:
            timeline_item: TimelineItem to export
            output_dir: Directory for output frames
            format: "PNG", "EXR", or "TIFF"
            progress_callback: Optional callback(current, total, message)

        Returns:
            Dict with output_dir, frame_count, format
        """
        os.makedirs(output_dir, exist_ok=True)

        clip_info = self.get_clip_info(timeline_item)
        start_frame = timeline_item.GetStart()
        end_frame = timeline_item.GetEnd()
        duration = timeline_item.GetDuration()

        # Set timeline in/out to clip bounds
        self.timeline.SetCurrentTimecode(
            self._frame_to_timecode(start_frame, clip_info["fps"])
        )

        # Configure render settings
        render_settings = {
            "TargetDir": output_dir,
            "CustomName": "frame_",
            "UniqueFilenameStyle": 1,  # Use frame numbers
            "MarkIn": start_frame,
            "MarkOut": end_frame - 1,
        }

        # Set format
        if format.upper() == "EXR":
            self.project.SetCurrentRenderFormatAndCodec("OpenEXR", "RGBHalf")
        elif format.upper() == "TIFF":
            self.project.SetCurrentRenderFormatAndCodec("TIFF", "RGB16")
        else:
            self.project.SetCurrentRenderFormatAndCodec("PNG", "PNG")

        self.project.SetRenderSettings(render_settings)

        # Add render job
        job_id = self.project.AddRenderJob()
        if not job_id:
            raise RuntimeError("Failed to create render job")

        # Start rendering
        self.project.StartRendering(job_id)

        # DANGER ZONE FRAGILE/HIGH: try/finally ensures DeleteRenderJob always runs.
        # An orphaned job blocks all future renders in the Resolve session.
        try:
            while self.project.IsRenderingInProgress():
                if progress_callback:
                    status = self.project.GetRenderJobStatus(job_id)
                    percent = status.get("CompletionPercentage", 0)
                    progress_callback(
                        int(percent * duration / 100),
                        duration,
                        f"Exporting: {percent}%"
                    )
                time.sleep(0.5)
        finally:
            self.project.DeleteRenderJob(job_id)

        return {
            "output_dir": output_dir,
            "frame_count": duration,
            "format": format,
            "start_frame": start_frame,
            "end_frame": end_frame,
        }

    # WHAT IT DOES: Imports an image sequence folder into a named MediaPool bin, optionally generates proxy
    # DEPENDS-ON: self.media_pool, generate_proxy_for_clip()
    # AFFECTS: MediaPool structure — creates bin if missing, adds media items
    def import_sequence_to_mediapool(
        self,
        folder_path: str,
        bin_name: str = "CorridorKey Output",
        generate_proxy: bool = True
    ) -> Any:
        """Import image sequence to MediaPool.

        Args:
            folder_path: Directory containing image sequence
            bin_name: MediaPool bin name
            generate_proxy: If True, generate proxy using project settings

        Returns:
            MediaPoolItem for imported sequence
        """
        # Find or create bin
        root_folder = self.media_pool.GetRootFolder()
        target_bin = None

        for subfolder in root_folder.GetSubFolderList():
            if subfolder.GetName() == bin_name:
                target_bin = subfolder
                break

        if not target_bin:
            target_bin = self.media_pool.AddSubFolder(root_folder, bin_name)

        self.media_pool.SetCurrentFolder(target_bin)

        # Find first frame to determine sequence
        folder = Path(folder_path)
        frames = sorted(folder.glob("*.exr")) or sorted(folder.glob("*.png"))

        if not frames:
            raise RuntimeError(f"No frames found in {folder_path}")

        # Import as sequence
        items = self.media_pool.ImportMedia([str(frames[0])])

        if items:
            media_pool_item = items[0]

            # Generate proxy if requested
            if generate_proxy:
                self.generate_proxy_for_clip(media_pool_item)

            return media_pool_item
        return None

    # WHAT IT DOES: Kicks off proxy generation for a single MediaPoolItem using project proxy settings
    # DEPENDS-ON: media_pool_item.GenerateProxy() — may not exist in older Resolve versions
    # DANGER ZONE FRAGILE/MEDIUM/CRITICAL: API availability varies by Resolve version / breaks: silently fails / depends on: Resolve version
    def generate_proxy_for_clip(self, media_pool_item) -> bool:
        """Generate proxy media for a MediaPoolItem.

        Uses the project's proxy settings (configured in Project Settings > Master Settings).
        Proxies are stored in the project's cache location.

        Args:
            media_pool_item: MediaPoolItem to generate proxy for

        Returns:
            True if proxy generation started successfully
        """
        try:
            # GenerateProxy() uses project proxy settings
            # Returns True if generation started
            result = media_pool_item.GenerateProxy()
            return result
        except Exception as e:
            # Some versions may not support this API
            print(f"Proxy generation not available: {e}")
            return False

    # WHAT IT DOES: Reads current proxy mode, resolution, and format from project settings
    # DEPENDS-ON: self.project
    def get_proxy_settings(self) -> Dict[str, Any]:
        """Get current project proxy settings.

        Returns dict with proxy configuration.
        """
        if not self.project:
            return {}

        return {
            "proxy_mode": self.project.GetSetting("proxyMode"),
            "proxy_resolution": self.project.GetSetting("proxyResolution"),
            "proxy_format": self.project.GetSetting("proxyFormat"),
        }

    # WHAT IT DOES: Places a MediaPoolItem onto the timeline at a specific track and frame position
    # DEPENDS-ON: self.media_pool
    # AFFECTS: Timeline — inserts new video item which shifts downstream clips
    def add_clip_to_timeline(
        self,
        media_pool_item,
        track_index: int,
        start_frame: int
    ) -> Any:
        """Add MediaPoolItem to timeline at specified position.

        Args:
            media_pool_item: MediaPoolItem to add
            track_index: Video track (1-based)
            start_frame: Start frame position

        Returns:
            New TimelineItem
        """
        # Append to timeline
        result = self.media_pool.AppendToTimeline([{
            "mediaPoolItem": media_pool_item,
            "startFrame": 0,
            "recordFrame": start_frame,
            "trackIndex": track_index,
            "mediaType": 1,  # Video
        }])

        return result[0] if result else None

    # WHAT IT DOES: Converts an absolute frame number to HH:MM:SS:FF timecode string
    # ISOLATED: pure math, no external dependencies
    def _frame_to_timecode(self, frame: int, fps: str) -> str:
        """Convert frame number to timecode string."""
        fps_num = float(fps) if fps else 24.0
        total_seconds = frame / fps_num
        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        seconds = int(total_seconds % 60)
        frames = int((total_seconds % 1) * fps_num)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"

    # WHAT IT DOES: Returns Fusion UIManager handle used to build custom UI panels inside Resolve
    # DEPENDS-ON: self.fusion (set by _connect)
    def get_ui_manager(self):
        """Get Fusion UIManager for creating panels."""
        if self.fusion:
            return self.fusion.UIManager
        return None

    # WHAT IT DOES: Creates a UIDispatcher for handling UI events (buttons, inputs) in custom panels
    # DEPENDS-ON: fusionscript module (optional import), get_ui_manager()
    # DANGER ZONE FRAGILE/MEDIUM/CRITICAL: fusionscript import may fail depending on Resolve edition / breaks: UI event loop / depends on: Resolve Studio vs Free
    def get_ui_dispatcher(self):
        """Get UI dispatcher for event handling."""
        try:
            import fusionscript
            ui = self.get_ui_manager()
            if ui:
                return fusionscript.UIDispatcher(ui)
        except ImportError:
            pass
        return None
