"""Synthetic test clips (real footage would contain GPS positions).

``build_mp4`` writes a minimal camera-like MP4 from raw bytes for the parser tests. ``camera_clip``
makes a playable clip with ffmpeg and adds Novatek GPS to it for the end-to-end tests.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from mp4 import GPS_INDEX_VERSION, insert_gps

BLOCK_SIZE = 0x4000

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def box(typ: bytes, body: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(body), typ) + body


USER_BOX = box(b"user", box(b"\xa9fmt", b"\x00\x07\x00\x00NOVATEK"))


def mvhd(duration: float, timescale: int = 1000, version: int = 0) -> bytes:
    if version == 1:
        body = bytes([1, 0, 0, 0]) + bytes(16) + struct.pack(">IQ", timescale, round(duration * timescale))
    else:
        body = bytes(4) + bytes(8) + struct.pack(">II", timescale, round(duration * timescale))
    return box(b"mvhd", body + bytes(80))


def gps_block(knots: float | None = 10.0, second: int = 0) -> bytes:
    """A fake 16 KB Novatek GPS block. ``knots=None`` means "no fix"."""
    b = bytearray(BLOCK_SIZE)
    struct.pack_into(">I4s4s", b, 0, BLOCK_SIZE, b"free", b"GPS ")
    struct.pack_into("<I3I", b, 12, 232, 12, second // 60, second % 60)
    b[28:29] = b"V" if knots is None else b"A"
    struct.pack_into("<d", b, 64, knots or 0.0)
    return bytes(b)


def build_mp4(
    path: Path,
    duration: float = 3.0,
    blocks: Sequence[bytes] = (),
    user_box: bytes = USER_BOX,
    extra_index: Sequence[tuple[int, int]] = (),
) -> Path:
    """A camera-like file: ftyp, moov(mvhd, udta/user, gps index), mdat holding the GPS blocks."""
    ftyp = box(b"ftyp", b"isom" + bytes(4))
    n = len(blocks) + len(extra_index)
    udta = box(b"udta", user_box) if user_box else b""
    moov_len = 8 + len(mvhd(duration)) + len(udta) + 16 + 8 * n
    pos = len(ftyp) + moov_len + 8  # first byte of mdat's body
    entries = []
    for b in blocks:
        entries.append(struct.pack(">II", pos, len(b)))
        pos += len(b)
    entries += [struct.pack(">II", o, s) for o, s in extra_index]
    gps = box(b"gps ", struct.pack(">II", GPS_INDEX_VERSION, n) + b"".join(entries))
    moov = box(b"moov", mvhd(duration) + udta + gps)
    assert len(moov) == moov_len
    path.write_bytes(ftyp + moov + box(b"mdat", b"".join(blocks)))
    return path


def build_joined(path: Path, duration: float = 3.0, reserve: int = 1 << 20) -> Path:
    """What ffmpeg writes with -moov_size: ftyp, moov, free (reserved space), mdat."""
    moov = box(b"moov", mvhd(duration) + box(b"trak", bytes(32)) + box(b"udta", box(b"meta", bytes(12))))
    path.write_bytes(
        box(b"ftyp", b"isom" + bytes(4)) + moov + box(b"free", bytes(reserve)) + box(b"mdat", bytes(64))
    )
    return path


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args], check=True)


ClipFactory = Callable[..., Path]


@pytest.fixture
def camera_clip(tmp_path: Path) -> ClipFactory:
    """Make a small playable clip named like the camera does, with fake GPS in it."""
    if not HAVE_FFMPEG:
        if os.environ.get("DASHCAM_MERGE_REQUIRE_FFMPEG"):
            pytest.fail("ffmpeg is required (DASHCAM_MERGE_REQUIRE_FFMPEG is set) but not on PATH")
        pytest.skip("ffmpeg not installed")

    def make(
        name: str,
        seconds: int = 3,
        speeds: Sequence[float | None] | None = None,
        audio: bool = True,
        folder: Path | None = None,
    ) -> Path:
        folder = folder or tmp_path / "sd_card"
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / name
        tmp = folder / (name + ".tmp.mp4")
        inputs = ["-f", "lavfi", "-i", f"testsrc2=s=320x180:r=30:d={seconds}"]
        if audio:
            inputs += ["-f", "lavfi", "-i", f"sine=d={seconds}"]
        codecs = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
        if audio:
            codecs += ["-c:a", "aac"]
        ffmpeg(*inputs, *codecs, "-moov_size", str(1 << 20), "-f", "mp4", str(tmp))
        speeds = [10.0] * seconds if speeds is None else speeds
        insert_gps(tmp, [gps_block(k, i) for i, k in enumerate(speeds)], USER_BOX)
        tmp.replace(out)
        return out

    return make
