from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import cli
import render
from cli import (
    CACHE_DIR,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_USAGE,
    build_trip,
    discover,
    main,
    verify,
)
from conftest import USER_BOX, ClipFactory, build_joined, build_mp4, gps_block
from mp4 import Error, has_audio, insert_gps, movie_duration, read_gps, speed_knots


@pytest.fixture
def fake_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend ffmpeg and libx265 exist (for tests that never actually run ffmpeg)."""
    monkeypatch.setattr(render, "require_ffmpeg", lambda: None)
    monkeypatch.setattr(render, "encoder_works", lambda e: e == "libx265")


def touch(folder: Path, *names: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / n).write_bytes(b"")


def make_sd_card(tmp_path: Path) -> Path:
    sd_card = tmp_path / "sd_card"
    sd_card.mkdir()
    for stem in ("20261122_100000_1_N", "20261123_090000_2_N"):
        build_mp4(sd_card / f"{stem}_A.MP4", blocks=[gps_block(1.0)])
    build_mp4(sd_card / "20261122_100000_1_N_C.MP4")
    build_mp4(sd_card / "20261122_100100_3_N_B.MP4")  # interior only: left out
    return sd_card


def test_discover_is_recursive_skips_cache_and_keeps_first_duplicate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    touch(tmp_path / "Normal" / "Front", "20261122_100000_0_N_A.MP4", "notes.txt")
    touch(tmp_path / "Normal" / "Back", "20261122_100000_0_N_C.MP4")
    # sorts after "Normal" whether or not the OS compares paths case-sensitively
    touch(tmp_path / "older_copy", "20261122_100000_0_N_A.MP4")
    touch(tmp_path / "cache", "20261122_110000_0_N_A.MP4")
    (tmp_path / "folder_N_A.MP4").mkdir()  # a directory with a clip-like name is ignored
    minutes = discover(tmp_path, exclude=tmp_path / "cache")
    assert minutes == {
        "20261122_100000_0_N": {
            "front": tmp_path / "Normal" / "Front" / "20261122_100000_0_N_A.MP4",
            "rear": tmp_path / "Normal" / "Back" / "20261122_100000_0_N_C.MP4",
        }
    }
    assert "Duplicate" in caplog.text


def test_errors_are_one_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(tmp_path / "missing")]) == EXIT_USAGE
    assert main([str(tmp_path)]) == EXIT_ERROR
    assert main([str(tmp_path), "--clip", "not_a_clip.mp4"]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "not a folder" in err
    assert "no clips with a supported name" in err
    assert "not a supported clip name" in err


@pytest.mark.usefixtures("fake_ffmpeg")
def test_os_error_is_reported_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def output_in_use(*_a: object, **_k: object) -> None:
        raise PermissionError("trip-2026-11-22.mp4 is open in another program")

    monkeypatch.setattr(cli, "build_trip", output_in_use)
    assert main([str(make_sd_card(tmp_path))]) == EXIT_ERROR
    assert "open in another program" in capsys.readouterr().err


@pytest.mark.usefixtures("fake_ffmpeg")
def test_dry_run_plans_one_video_per_day(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sd_card = make_sd_card(tmp_path)
    assert main([str(sd_card), "--dry-run"]) == EXIT_OK
    out, err = capsys.readouterr()
    assert "Encoder: libx265" in out
    assert "trip-2026-11-22.mp4: 1 minute(s)" in out
    assert "trip-2026-11-23.mp4: 1 minute(s)" in out
    assert "20261122_100000_1_N front+rear" in out
    assert "hflip" in out
    assert "no front clip: 20261122_100100_3_N" in err
    assert main([str(sd_card), "--dry-run", "--clip", "some/dir/20261123_090000_2_N_C.MP4"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "trip-preview.mp4: 1 minute(s)" in out
    assert "trip-2026-11-22" not in out
    assert not list(sd_card.glob("trip-*"))


def test_moov_reserve_grows_with_the_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def capture_reserve(cmd: Sequence[str], cwd: Path | None = None) -> None:  # noqa: ARG001
        reserve.append(int(cmd[cmd.index("-moov_size") + 1]))
        raise Error("ffmpeg not run")

    reserve: list[int] = []
    monkeypatch.setattr(render, "run_ffmpeg", capture_reserve)
    ten_hours: list[cli.Segment] = [(tmp_path / f"{i}.mp4", 60.0, [], b"") for i in range(600)]
    with pytest.raises(Error, match="ffmpeg not run"):
        cli.join_minutes(ten_hours, tmp_path / "Trip.mp4", tmp_path)
    assert reserve[0] > 600 * 70_000  # measured moov size per minute with x265 + AAC


def test_verify(tmp_path: Path) -> None:
    p = build_joined(tmp_path / "j.mp4", duration=120.0)
    insert_gps(p, [gps_block(1.0)] * 3, USER_BOX)
    verify(p, 3, 120.0)
    verify(p, 3, 121.5)  # within tolerance
    with pytest.raises(Error, match="entries"):
        verify(p, 4, 120.0)
    with pytest.raises(Error, match="duration"):
        verify(p, 3, 130.0)
    with p.open("ab") as f:
        f.write(b"trailing bytes")
    with pytest.raises(Error, match="cover"):
        verify(p, 3, 120.0)


def test_every_minute_failing_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "20261122_100000_1_N_A.MP4").write_bytes(b"not an mp4 file")
    with pytest.raises(Error, match="every minute failed"):
        build_trip(discover(tmp_path, tmp_path / CACHE_DIR), tmp_path / "Trip.mp4", "libx264")


def test_full_trip(camera_clip: ClipFactory, tmp_path: Path) -> None:
    sd_card = tmp_path / "sd_card"
    # minute 1: all cameras, GPS from the 2nd second; minute 2: front only, no audio, no GPS;
    # minute 3: front + interior, 2 s of video but only 1 GPS block
    camera_clip("20261122_100000_1_N_A.MP4", speeds=[None, 10.0, 20.0])
    camera_clip("20261122_100000_1_N_B.MP4")
    camera_clip("20261122_100000_1_N_C.MP4")
    camera_clip("20261122_100100_2_N_A.MP4", seconds=2, speeds=[], audio=False)
    camera_clip("20261122_100200_3_N_A.MP4", seconds=2, speeds=[30.0])
    camera_clip("20261122_100200_3_N_B.MP4", seconds=2)
    out = tmp_path / "Trip.mp4"

    total, points, skipped = build_trip(discover(sd_card, tmp_path / CACHE_DIR), out, "libx264")

    assert skipped == []
    assert total == pytest.approx(7, abs=0.1)
    assert points == 7  # 3 + 2 (filled) + 2 (padded)
    blocks, user = read_gps(out)
    assert user == USER_BOX
    assert has_audio(out)  # minute 2 is silent but still has an audio track
    assert movie_duration(out) == pytest.approx(total, abs=0.05)  # GPS total = the video's own length
    # the no-GPS minute is marked "no fix", not a stale position
    assert [speed_knots(b) for b in blocks] == [None, 10.0, 20.0, None, None, 30.0, 30.0]
    assert not (tmp_path / CACHE_DIR).exists()  # cache cleaned up after a full run


def test_silent_first_minute_keeps_the_day_audio(camera_clip: ClipFactory, tmp_path: Path) -> None:
    # concat takes its stream layout from the first file: a silent first minute must not drop all audio
    camera_clip("20261122_100000_1_N_A.MP4", seconds=2, audio=False)
    camera_clip("20261122_100100_2_N_A.MP4", seconds=2)
    out = tmp_path / "Trip.mp4"
    build_trip(discover(tmp_path / "sd_card", tmp_path / CACHE_DIR), out, "libx264")
    assert has_audio(out)


def test_failed_day_does_not_stop_the_next_day(camera_clip: ClipFactory, tmp_path: Path) -> None:
    sd_card = tmp_path / "sd_card"
    camera_clip("20261123_090000_2_N_A.MP4", seconds=2)
    (sd_card / "20261122_100000_1_N_A.MP4").write_bytes(b"not an mp4 file")  # day 1's only minute
    assert main([str(sd_card), "--encoder", "libx264"]) == EXIT_PARTIAL
    assert not (sd_card / "trip-2026-11-22.mp4").exists()
    assert (sd_card / "trip-2026-11-23.mp4").exists()


def test_clip_run_keeps_cache_for_the_full_run(
    camera_clip: ClipFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sd_card = tmp_path / "sd_card"
    good = camera_clip("20261122_100000_1_N_A.MP4", seconds=2)
    broken = sd_card / "20261122_100100_2_N_A.MP4"
    broken.write_bytes(b"\x00\x00\x00\x08free not a video")
    cache = sd_card / CACHE_DIR

    clip_run = [str(sd_card), "--encoder", "libx264", "--clip", good.name, broken.name]
    assert main(clip_run) == EXIT_PARTIAL
    assert len(read_gps(sd_card / "trip-preview.mp4")[0]) == 2
    (segment,) = cache.glob("*.mp4")
    mtime = segment.stat().st_mtime_ns
    assert main(clip_run) == EXIT_PARTIAL
    assert segment.stat().st_mtime_ns == mtime  # reused, not rendered again

    assert ".f." in segment.name
    camera_clip("20261122_100000_1_N_B.MP4", seconds=2)  # a camera the preview did not have
    stale = cache / "20261122_100000_1_N.f.0123456789.mp4"  # from an older version of the tool
    stale.write_bytes(b"")
    calls: list[str] = []
    real = render.render_minute

    def spy(cams: dict[str, Path], out: Path, *a: Any, **k: Any) -> None:
        calls.append(out.name)
        real(cams, out, *a, **k)

    monkeypatch.setattr(render, "render_minute", spy)

    assert main([str(sd_card), "--encoder", "libx264"]) == EXIT_PARTIAL
    assert len(read_gps(sd_card / "trip-2026-11-22.mp4")[0]) == 2
    assert [n.split(".")[1] for n in calls] == ["fi"]  # re-rendered with the interior, not reused
    assert not cache.exists()  # a full run cleans up, stale files included
