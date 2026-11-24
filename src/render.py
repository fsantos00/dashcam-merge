"""Rendering one minute: the three cameras composited into one frame, plus the speed readout.

Layout: front full frame, mirrored rear strip top center, interior
picture-in-picture bottom right, speed bottom left. The top left stays empty for a map added later
in Telemetry Overlay / RaceRender. All sizes are proportions of the front's native frame.
"""

from __future__ import annotations

import logging
import math
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from cameras import ROLES
from mp4 import Error, has_audio

log = logging.getLogger("dashcam_merge.render")  # child of the CLI logger, so -v shows it

W, H, FPS = 2560, 1440, 30  # the N4S front camera's native frame
REAR_W, REAR_H = W // 3 // 2 * 2, H // 6 // 2 * 2  # even sizes: yuv420p needs them
REAR_BAND = 0.6  # which band of the rear view to keep: 0 = top .. 1 = bottom
INTERIOR_W = W // 4 // 2 * 2
INTERIOR_KEEP = 0.94  # keep the top 94% of the interior view (drops its date stamp)
MARGIN_X, MARGIN_Y = W * 2 // 100, H * 37 // 1000
# Speed: 5% of H tall, 3% of W from the left, 9% of H from the bottom to clear the front's date stamp.
SPEED_SIZE, SPEED_MARGIN_L, SPEED_MARGIN_V = H * 5 // 100, W * 3 // 100, H * 9 // 100
KNOTS_TO_KMH, KNOTS_TO_MPH = 1.852, 1.150779

FFMPEG = ("ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y")
AUDIO = ("-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2")
HWDEC = "auto"  # a named device (d3d11va, videotoolbox) fails outright on machines without one

# Tried in this order by "auto". Quality is roughly CQP 20/22 for all; hvc1 so Apple players accept HEVC.
ENCODERS: dict[str, tuple[str, ...]] = {
    "hevc_amf": ("-quality", "quality", "-rc", "cqp", "-qp_i", "20", "-qp_p", "22", "-pix_fmt", "yuv420p",
                 "-tag:v", "hvc1"),
    "hevc_nvenc": ("-preset", "p5", "-tune", "hq", "-rc", "constqp", "-qp", "22", "-pix_fmt", "yuv420p",
                   "-tag:v", "hvc1"),
    "hevc_qsv": ("-preset", "medium", "-global_quality", "22", "-pix_fmt", "nv12", "-tag:v", "hvc1"),
    "hevc_videotoolbox": ("-q:v", "65", "-pix_fmt", "yuv420p", "-tag:v", "hvc1"),
    "libx265": ("-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p", "-x265-params", "log-level=error",
                "-tag:v", "hvc1"),
    "libx264": ("-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-tag:v", "avc1"),
}  # fmt: skip


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        hint = {
            "win32": "winget install Gyan.FFmpeg (then open a new terminal)",
            "darwin": "brew install ffmpeg",
        }.get(sys.platform, "install it with your package manager, e.g. sudo apt install ffmpeg")
        raise Error(f"ffmpeg was not found on PATH: {hint}")


def video_args(encoder: str) -> list[str]:
    return ["-c:v", encoder, *ENCODERS[encoder], "-g", str(2 * FPS), "-r", str(FPS)]


def encoder_works(encoder: str) -> bool:
    """Encode one tiny frame to see if the encoder and its options are usable here."""
    cmd = [*FFMPEG, "-f", "lavfi", "-i", "color=s=256x256", "-frames:v", "1", *video_args(encoder),
           "-f", "null", "-"]  # fmt: skip
    try:
        return subprocess.run(cmd, capture_output=True, timeout=60, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def choose_encoder(name: str = "auto") -> str:
    """The named encoder (checked to work), or the first working one in :data:`ENCODERS`."""
    for candidate in ENCODERS if name == "auto" else [name]:
        if encoder_works(candidate):
            return candidate
    if name == "auto":
        raise Error("no working H.265/H.264 encoder found in this ffmpeg build")
    raise Error(f"encoder {name} is not working on this machine (driver or ffmpeg build)")


def ass_time(t: float) -> str:
    cs = round(t * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02}:{cs // 100 % 60:02}.{cs % 100:02}"


def ass_script(speeds: Sequence[float | None], duration: float, mph: bool = False) -> str:
    """An ASS subtitle script that shows one speed value per second (``speeds`` in knots)."""
    unit, factor = ("mph", KNOTS_TO_MPH) if mph else ("km/h", KNOTS_TO_KMH)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV",
        f"Style: S,Arial,{SPEED_SIZE},&H00FFFFFF,&H00000000,&H80000000,1,1,4,2,1,"
        f"{SPEED_MARGIN_L},0,{SPEED_MARGIN_V}",
        "",
        "[Events]", "Format: Layer, Start, End, Style, Text",
    ]  # fmt: skip
    for i, knots in enumerate(speeds[: math.ceil(duration)]):
        text = "--" if knots is None else f"{knots * factor:.0f}"
        lines.append(f"Dialogue: 0,{ass_time(i)},{ass_time(min(i + 1, duration))},S,{text} {unit}")
    return "\n".join(lines) + "\n"


def filtergraph(cams: Sequence[str], ass_name: str) -> str:
    """The filter_complex for one minute; ``cams`` in input order, front first. Output is ``[vout]``.

    ``ass_name`` is a plain file name in ffmpeg's cwd (stem + hex key), so it needs no escaping.
    """
    graph, last = [f"[0:v]scale={W}:{H},fps={FPS},setsar=1[v0]"], "v0"
    if "rear" in cams:  # mirrored like a rear-view mirror, wide strip, top center
        graph += [
            f"[{cams.index('rear')}:v]hflip,scale={REAR_W}:-2,crop={REAR_W}:{REAR_H}:0:(ih-oh)*{REAR_BAND},setsar=1[r]",
            f"[{last}][r]overlay=(W-w)/2:{MARGIN_Y}:eof_action=pass[vr]",
        ]
        last = "vr"
    if "interior" in cams:  # own aspect ratio, bottom right, its date stamp cropped off
        graph += [
            f"[{cams.index('interior')}:v]crop=iw:ih*{INTERIOR_KEEP}:0:0,scale={INTERIOR_W}:-2,setsar=1[i]",
            f"[{last}][i]overlay=W-w-{MARGIN_X}:H-h-{MARGIN_Y}:eof_action=pass[vi]",
        ]
        last = "vi"
    graph.append(f"[{last}]subtitles=filename={ass_name}[vout]")
    return ";".join(graph)


def run_ffmpeg(cmd: Sequence[str], cwd: Path | None = None) -> None:
    log.debug("Running in %s: %s", cwd or ".", shlex.join(cmd))
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors="replace", check=False)
    if proc.returncode:
        tail = "\n".join(proc.stderr.strip().splitlines()[-5:]) or f"exit code {proc.returncode}"
        raise Error(f"ffmpeg failed: {tail}")


def render_minute(
    cams: Mapping[str, Path], out: Path, duration: float, speeds: Sequence[float | None],
    *, encoder: str, mph: bool,
) -> None:  # fmt: skip
    """Render one minute to ``out`` atomically: a half-written file never takes its place."""
    tmp, ass = out.with_name(out.stem + ".tmp.mp4"), out.with_suffix(".ass")
    order = [r for r in ROLES if r in cams]  # the front is always input 0
    inputs = [arg for c in order for arg in ("-hwaccel", HWDEC, "-i", str(cams[c].resolve()))]
    audio = "0:a:0"  # front camera audio only
    if not has_audio(cams["front"]):  # silence instead: concat needs the same streams in every minute
        inputs += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        audio = f"{len(order)}:a"
    try:
        ass.write_text(ass_script(speeds, duration, mph), encoding="utf-8")
        run_ffmpeg([
            *FFMPEG, *inputs, "-filter_complex", filtergraph(order, ass.name),
            "-map", "[vout]", "-map", audio,
            "-t", f"{duration:.3f}", *video_args(encoder), *AUDIO, tmp.name,
        ], cwd=out.parent)  # fmt: skip
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
        ass.unlink(missing_ok=True)
