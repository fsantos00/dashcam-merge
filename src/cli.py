"""``dashcam-merge [INPUT_DIR] [options]``: find the clips, render each minute, join them, add the GPS."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import logging
import math
import shutil
import struct
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import mp4
import render
from cameras import ROLES, parse_clip_name
from mp4 import Error

__version__ = "0.1.0"
log = logging.getLogger("dashcam_merge")

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_PARTIAL, EXIT_INTERRUPTED = 0, 1, 2, 3, 130
CACHE_DIR = ".dashcam-merge-cache"
PREVIEW_OUTPUT = "trip-preview.mp4"
WORKERS = 2  # minutes rendered at once; GPU encoders rarely gain from more
DISK_FACTOR = 1.5  # an unrendered minute needs its render plus its part of the joined file, ~0.7x each
MOOV_PER_MINUTE = 200_000  # moov index bytes reserved per joined minute (measured ~70 KB with x265 + AAC)
MINUTE_ERRORS = (Error, OSError, struct.error)  # these skip one minute instead of stopping the run


class UsageError(Error):
    """Bad arguments: exit code 2, like argparse's own usage errors."""


CameraClips = dict[str, Path]  # role (front, rear, interior) -> clip
Segment = tuple[Path, float, list[bytes], bytes]  # rendered minute, duration, GPS blocks, udta/user box


def cam_names(cams: CameraClips) -> str:
    return "+".join(r for r in ROLES if r in cams)


def discover(root: Path, exclude: Path) -> dict[str, CameraClips]:
    """Every minute under ``root`` (searched recursively: SD cards keep cameras in subfolders)."""
    skip = exclude.resolve()
    minutes: dict[str, CameraClips] = {}
    starts: dict[str, dt.datetime] = {}
    for path in sorted(root.rglob("*")):
        clip = parse_clip_name(path.name)
        if clip is None or not path.is_file() or path.resolve().is_relative_to(skip):
            continue
        cams = minutes.setdefault(clip.minute, {})
        starts[clip.minute] = clip.start
        if clip.role in cams:
            log.warning("Duplicate clip %s, keeping %s", path, cams[clip.role])
        else:
            cams[clip.role] = path
    return dict(sorted(minutes.items(), key=lambda kv: (starts[kv[0]], kv[0])))


def plan_trips(input_dir: Path, output_dir: Path, clips: Sequence[str]) -> dict[Path, dict[str, CameraClips]]:
    """Output file -> its minutes: one trip-YYYY-MM-DD.mp4 per day, or trip-preview.mp4 for ``--clip``."""
    minutes = discover(input_dir, exclude=output_dir / CACHE_DIR)
    if clips:
        parsed = {c: parse_clip_name(Path(c).name) for c in clips}
        if unknown := [c for c, p in parsed.items() if p is None]:
            raise UsageError(f"not a supported clip name: {', '.join(unknown)}")
        wanted = {p.minute for p in parsed.values() if p}
        minutes = {s: c for s, c in minutes.items() if s in wanted}
    if no_front := [s for s, c in minutes.items() if "front" not in c]:
        log.warning("Leaving out %d minute(s) with no front clip: %s", len(no_front), ", ".join(no_front))
    minutes = {s: c for s, c in minutes.items() if "front" in c}
    if not minutes:
        raise Error(f"no clips with a supported name in {input_dir} (see README: Supported cameras)")
    if clips:
        return {output_dir / PREVIEW_OUTPUT: minutes}
    trips: dict[Path, dict[str, CameraClips]] = {}
    for minute, cams in minutes.items():
        if front := parse_clip_name(cams["front"].name):  # always: discover only keeps parsed names
            trips.setdefault(output_dir / f"trip-{front.start:%Y-%m-%d}.mp4", {})[minute] = cams
    return trips


def cache_key(encoder: str, mph: bool) -> str:
    """Changes whenever the rendered pixels could: version, encoder, units or the render code itself."""
    ident = repr((__version__, encoder, mph)).encode() + Path(render.__file__).read_bytes()
    return hashlib.sha256(ident).hexdigest()[:10]


def prepare_minute(cams: CameraClips, out: Path, encoder: str, mph: bool) -> Segment:
    """GPS from the front clip, render the minute unless it is cached, duration from the render."""
    blocks, user_box = mp4.read_gps(cams["front"])
    if not out.exists():
        duration = mp4.movie_duration(cams["front"])
        speeds = [mp4.speed_knots(b) for b in blocks]
        speeds += speeds[-1:] * math.ceil(duration)  # ass_script cuts it to the duration
        render.render_minute(cams, out, duration, speeds, encoder=encoder, mph=mph)
    return out, mp4.movie_duration(out), blocks, user_box  # render's own length keeps GPS in step


def verify(path: Path, gps_points: int, expect_duration: float) -> None:
    """Structural self-check of a finished trip: box layout, GPS index and duration."""
    with path.open("rb") as f:
        if [b.end for b in mp4.iter_boxes(f)][-1:] != [path.stat().st_size]:
            raise Error("self-check: top-level boxes do not cover the file exactly")
        entries = mp4.gps_index(f)
        if len(entries) != gps_points:
            raise Error(f"self-check: gps index has {len(entries)} entries, expected {gps_points}")
        for offset, _ in entries:
            f.seek(offset + 4)
            if f.read(8) != mp4.BLOCK_MAGIC:
                raise Error(f"self-check: gps entry at offset {offset} does not point at a GPS block")
    duration = mp4.movie_duration(path)
    if abs(duration - expect_duration) >= 2 + expect_duration * 0.001:
        raise Error(f"self-check: duration is {duration:.1f}s, expected {expect_duration:.1f}s")


def render_minutes(
    minutes: dict[str, CameraClips], work: Path, encoder: str, mph: bool
) -> tuple[list[Segment], list[str]]:
    """Render every minute (in parallel), in order. Failed minutes are logged and skipped."""
    key = cache_key(encoder, mph)
    outs = {m: work / f"{m}.{''.join(r[0] for r in ROLES if r in c)}.{key}.mp4" for m, c in minutes.items()}
    need = sum(
        o.stat().st_size if o.exists() else DISK_FACTOR * sum(p.stat().st_size for p in minutes[s].values())
        for s, o in outs.items()
    )
    if shutil.disk_usage(work).free < need:
        raise Error(f"not enough free space in {work}: need about {need / 1e9:.1f} GB")

    segments: list[Segment] = []
    skipped: list[str] = []
    with ThreadPoolExecutor(WORKERS) as pool:
        jobs = {s: pool.submit(prepare_minute, c, outs[s], encoder, mph) for s, c in minutes.items()}
        try:
            for i, (minute, job) in enumerate(jobs.items(), 1):
                try:
                    segments.append(job.result())
                except MINUTE_ERRORS as e:
                    log.warning("[%d/%d] %s SKIPPED: %s", i, len(jobs), minute, e)
                    skipped.append(minute)
                else:
                    names = cam_names(minutes[minute])
                    log.info("[%d/%d] %s %s %.0fs", i, len(jobs), minute, names, segments[-1][1])
        except BaseException:
            pool.shutdown(cancel_futures=True)  # Ctrl-C: do not start the queued minutes
            raise
    return segments, skipped


def join_minutes(segments: Sequence[Segment], output: Path, work: Path) -> tuple[float, int]:
    """Join the rendered minutes without re-encoding and add their GPS. Returns (duration, GPS points)."""
    paths, durations, tracks, users = zip(*segments, strict=True)
    # GPS only for minutes that made it into the video, one block per second, so both stay in step.
    blocks = mp4.join_tracks(tracks, durations)
    user_box = next((u for u in users if u), b"")
    total = sum(durations)

    lst, joined = work / f"{output.stem}.concat.txt", work / f"{output.stem}.joined.mp4"
    quoted = (str(p.resolve()).replace("'", "'\\''") for p in paths)  # concat-script quoting
    lst.write_text("".join(f"file '{q}'\n" for q in quoted), encoding="utf-8")
    gps_bytes = sum(map(len, blocks)) + 8 * len(blocks) + len(user_box)
    reserve = gps_bytes + (1 << 20) + MOOV_PER_MINUTE * len(paths)
    log.info("Joining %d minutes and adding %d GPS points...", len(paths), len(blocks))
    try:
        render.run_ffmpeg([*render.FFMPEG, "-f", "concat", "-safe", "0", "-i", str(lst), "-map", "0",
                           "-c", "copy", "-moov_size", str(reserve), str(joined)])  # fmt: skip
        mp4.insert_gps(joined, blocks, user_box)
        verify(joined, len(blocks), total)
        joined.replace(output)
    finally:
        joined.unlink(missing_ok=True)
        lst.unlink(missing_ok=True)
    log.info("Self-check OK: %d GPS points, %.1f min.", len(blocks), total / 60)
    return total, len(blocks)


def build_trip(
    minutes: dict[str, CameraClips], output: Path, encoder: str, mph: bool = False, keep_work: bool = False
) -> tuple[float, int, list[str]]:
    """Render, join and GPS-tag ``minutes`` (all with a front clip) into ``output``.

    Returns (duration, GPS points, skipped minutes). The cache sits next to ``output``.
    """
    work = output.parent / CACHE_DIR
    work.mkdir(parents=True, exist_ok=True)
    segments, skipped = render_minutes(minutes, work, encoder, mph)
    if not segments:
        raise Error("every minute failed; see the messages above")
    total, points = join_minutes(segments, output, work)
    if not keep_work:
        for f in work.iterdir():
            if f.name.split(".", 1)[0] in minutes:  # this trip's segments, any cache key, .tmp and .ass
                f.unlink()
        if not any(work.iterdir()):
            work.rmdir()
    return total, points, skipped


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="dashcam-merge",
        description="Merge a day of Novatek-based dashcam clips (front, rear, interior) into one video "
        "per day, with the GPS track kept so Telemetry Overlay / RaceRender / ExifTool can read it.",
        epilog="Exit codes: 0 done, 1 error, 2 bad usage, 3 done but some minutes were skipped.",
    )
    p.add_argument("input_dir", nargs="?", type=Path, default=Path(),
                   help="folder with the clips, searched recursively (default: current folder)")  # fmt: skip
    p.add_argument("-o", "--output-dir", type=Path, help="folder for the videos (default: INPUT_DIR)")
    p.add_argument("--clip", nargs="+", metavar="FILE", default=[],
                   help=f"render only these minutes (any camera's file name) into {PREVIEW_OUTPUT}; "
                   "the rendered minutes are kept and reused by the full run. "
                   "Put INPUT_DIR before --clip")  # fmt: skip
    p.add_argument("--units", choices=("kmh", "mph"), default="kmh", help="speed units (default: kmh)")
    p.add_argument("--encoder", default="auto", choices=("auto", *render.ENCODERS),
                   help="video encoder (default: auto = the first one listed that works)")  # fmt: skip
    p.add_argument("--dry-run", action="store_true", help="show what would be done and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="more detail")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    if not args.input_dir.is_dir():
        raise UsageError(f"not a folder: {args.input_dir}")
    output_dir = args.output_dir or args.input_dir
    trips = plan_trips(args.input_dir, output_dir, args.clip)
    render.require_ffmpeg()
    encoder, mph = render.choose_encoder(args.encoder), args.units == "mph"
    if args.dry_run:
        print(f"Encoder: {encoder}   cache: {output_dir / CACHE_DIR}")
        for out, minutes in trips.items():
            print(f"{out}: {len(minutes)} minute(s)")
            print("".join(f"  {minute} {cam_names(cams)}\n" for minute, cams in minutes.items()), end="")
        minute, cams = next(iter(next(iter(trips.values())).items()))
        graph = render.filtergraph([r for r in ROLES if r in cams], "speed.ass")
        print(f"Filtergraph ({minute}):\n  " + graph.replace(";", ";\n  "))
        return EXIT_OK

    log.info("Encoder: %s", encoder)
    skipped: list[str] = []
    for out, minutes in trips.items():
        try:
            total, points, skip = build_trip(minutes, out, encoder, mph, keep_work=bool(args.clip))
        except Error as e:  # one bad day must not cost the other days their video
            log.error("%s not made: %s", out, e)
            skipped += minutes
            continue
        skipped += skip
        log.info("Done: %s (%.1f min, %d GPS points)", out, total / 60, points)
    if skipped:
        log.warning("Skipped %d minute(s): %s", len(skipped), ", ".join(skipped))
        return EXIT_PARTIAL
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s" if args.verbose else "%(message)s"))
    log.handlers[:], log.propagate = [handler], False
    log.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    try:
        return run(args)
    except MINUTE_ERRORS as e:
        log.error("Error: %s", e)  # no traceback: it would only confuse users
        return EXIT_USAGE if isinstance(e, UsageError) else EXIT_ERROR
    except KeyboardInterrupt:
        log.error("Interrupted. Finished minutes are kept in the cache and reused next time.")
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
