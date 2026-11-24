from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

import render
from mp4 import Error
from render import ass_script, ass_time, choose_encoder, filtergraph, render_minute


def test_layout_defaults() -> None:
    assert (render.W, render.H, render.FPS) == (2560, 1440, 30)
    assert (render.REAR_W, render.REAR_H, render.INTERIOR_W) == (852, 240, 640)
    assert (render.REAR_BAND, render.INTERIOR_KEEP) == (0.6, 0.94)
    assert (render.MARGIN_X, render.MARGIN_Y) == (51, 53)
    assert (render.SPEED_SIZE, render.SPEED_MARGIN_L, render.SPEED_MARGIN_V) == (72, 76, 129)


def test_ass_script_one_event_per_second_and_clipped_to_duration() -> None:
    script = ass_script([10.0, None, 20.0, 30.0], 2.5)
    events = [line for line in script.splitlines() if line.startswith("Dialogue")]
    assert events == [
        "Dialogue: 0,0:00:00.00,0:00:01.00,S,19 km/h",
        "Dialogue: 0,0:00:01.00,0:00:02.00,S,-- km/h",
        "Dialogue: 0,0:00:02.00,0:00:02.50,S,37 km/h",
    ]
    assert "PlayResX: 2560" in script
    assert "Style: S,Arial,72," in script
    assert "12 mph" in ass_script([10.0], 1, mph=True)
    assert ass_time(3725.25) == "1:02:05.25"


def test_filtergraph_all_cameras() -> None:
    assert filtergraph(["front", "rear", "interior"], "x.ass").split(";") == [
        "[0:v]scale=2560:1440,fps=30,setsar=1[v0]",
        "[1:v]hflip,scale=852:-2,crop=852:240:0:(ih-oh)*0.6,setsar=1[r]",
        "[v0][r]overlay=(W-w)/2:53:eof_action=pass[vr]",
        "[2:v]crop=iw:ih*0.94:0:0,scale=640:-2,setsar=1[i]",
        "[vr][i]overlay=W-w-51:H-h-53:eof_action=pass[vi]",
        "[vi]subtitles=filename=x.ass[vout]",
    ]


def test_filtergraph_without_rear() -> None:
    interior_only = filtergraph(["front", "interior"], "x.ass")
    assert "[1:v]crop" in interior_only
    assert "[v0][i]overlay" in interior_only
    assert "hflip" not in interior_only


@pytest.mark.parametrize("front_audio", [True, False])
def test_render_minute_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, front_audio: bool) -> None:
    calls: list[Sequence[str]] = []
    monkeypatch.setattr(render, "has_audio", lambda _p: front_audio)

    def fake_run(cmd: Sequence[str], cwd: Path | None = None) -> None:
        calls.append(cmd)
        assert cwd == tmp_path
        assert (tmp_path / "m.ass").exists()
        raise Error("ffmpeg failed")

    monkeypatch.setattr(render, "run_ffmpeg", fake_run)
    cams = {r: tmp_path / f"x_{r}.MP4" for r in ("interior", "rear", "front")}
    with pytest.raises(Error, match="ffmpeg failed"):
        render_minute(cams, tmp_path / "m.mp4", 59.9876, [1.0], encoder="libx265", mph=False)
    (cmd,) = calls
    inputs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
    cameras = [
        str((tmp_path / f"x_{r}.MP4").resolve()) for r in ("front", "rear", "interior")
    ]  # front is input 0
    assert inputs == cameras + ([] if front_audio else ["anullsrc=r=48000:cl=stereo"])
    assert cmd.count("-hwaccel") == 3
    # front audio only; silence when it has none, so every minute has the same streams
    assert cmd[cmd.index("[vout]") + 2] == ("0:a:0" if front_audio else "3:a")
    assert cmd[cmd.index("-t") + 1] == "59.988"
    assert cmd[cmd.index("-c:v") + 1] == "libx265"
    assert cmd[-1] == "m.tmp.mp4"
    assert list(tmp_path.iterdir()) == []  # subtitle and half-written file removed on failure


def test_choose_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    working = {"libx265", "hevc_nvenc"}
    monkeypatch.setattr(render, "encoder_works", lambda e: e in working)
    assert choose_encoder() == "hevc_nvenc"  # hardware first
    assert choose_encoder("libx265") == "libx265"
    with pytest.raises(Error, match="not working"):
        choose_encoder("hevc_qsv")
    working.clear()
    with pytest.raises(Error, match="no working"):
        choose_encoder()
