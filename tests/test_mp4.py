from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import BinaryIO

import pytest

from conftest import USER_BOX, box, build_joined, build_mp4, gps_block, mvhd
from mp4 import (
    BLOCK_MAGIC,
    Error,
    find,
    gps_index,
    has_audio,
    insert_gps,
    iter_boxes,
    join_tracks,
    movie_duration,
    no_fix,
    read_gps,
    speed_knots,
)


def write(tmp_path: Path, data: bytes) -> Path:
    p = tmp_path / "f.mp4"
    p.write_bytes(data)
    return p


def walks_to_end(f: BinaryIO) -> bool:
    return [b.end for b in iter_boxes(f)][-1:] == [os.fstat(f.fileno()).st_size]


def test_iter_boxes(tmp_path: Path) -> None:
    big = struct.pack(">I4sQ", 1, b"mdat", 16 + 4) + b"abcd"  # 64-bit size
    p = write(tmp_path, box(b"ftyp", b"isom") + big + struct.pack(">I4s", 0, b"free") + bytes(100))
    with p.open("rb") as f:
        assert [(b.type, b.start, b.header, b.end) for b in iter_boxes(f)] == [
            (b"ftyp", 0, 8, 12),
            (b"mdat", 12, 16, 32),
            (b"free", 32, 8, 140),  # size 0 runs to the end
        ]


@pytest.mark.parametrize(
    "data",
    [
        struct.pack(">I4s", 4, b"bad!") + bytes(8),  # size smaller than its header
        struct.pack(">I4s", 1, b"mdat") + b"\x00\x00",  # truncated 64-bit size
    ],
)
def test_corrupt_boxes_raise(tmp_path: Path, data: bytes) -> None:
    with write(tmp_path, data).open("rb") as f, pytest.raises(Error):
        list(iter_boxes(f))


def test_nested_header_beyond_file_raises(tmp_path: Path) -> None:
    p = write(tmp_path, struct.pack(">I4s", 100, b"moov") + b"\x00\x00\x00")  # cut off by a power loss
    with p.open("rb") as f, pytest.raises(Error):
        find(f, b"moov", b"mvhd")


@pytest.mark.parametrize("version", [0, 1])
def test_movie_duration(tmp_path: Path, version: int) -> None:
    p = write(tmp_path, box(b"moov", mvhd(59.94, timescale=92400, version=version)))
    assert movie_duration(p) == pytest.approx(59.94)


@pytest.mark.parametrize(
    "data",
    [
        box(b"ftyp"),  # no moov at all
        box(b"moov", box(b"mvhd", bytes(10))),  # too short
        box(b"moov", box(b"mvhd", bytes([1]) + bytes(23))),  # version 1, too short
        box(b"moov", mvhd(1.0, timescale=0)),
    ],
)
def test_movie_duration_errors(tmp_path: Path, data: bytes) -> None:
    with pytest.raises(Error):
        movie_duration(write(tmp_path, data))


def test_read_gps(tmp_path: Path) -> None:
    blocks = [gps_block(k, i) for i, k in enumerate([1.0, 2.0, None])]
    assert read_gps(build_mp4(tmp_path / "a.mp4", blocks=blocks)) == (blocks, USER_BOX)
    # entries that do not point at a GPS block become no-fix copies, so later seconds do not shift
    p = build_mp4(tmp_path / "b.mp4", blocks=blocks[:1], extra_index=[(0, 16), (10**9, 0x4000)])
    assert read_gps(p)[0] == [blocks[0], no_fix(blocks[0]), no_fix(blocks[0])]
    p = build_mp4(tmp_path / "c.mp4", blocks=[], extra_index=[(0, 16)])
    assert read_gps(p)[0] == []


@pytest.mark.parametrize("bad", [0, 1])
def test_read_gps_bad_entry_keeps_its_second(tmp_path: Path, bad: int) -> None:
    blocks = [gps_block(k, i) for i, k in enumerate([1.0, 2.0, 3.0])]
    p = build_mp4(tmp_path / "a.mp4", blocks=blocks)
    with p.open("r+b") as f:
        f.seek(gps_index(f)[bad][0] + 4)
        f.write(b"junk")
    got = read_gps(p)[0]
    assert [speed_knots(b) for b in got] == [None if i == bad else k for i, k in enumerate([1.0, 2.0, 3.0])]
    assert got[bad] == no_fix(blocks[1] if bad == 0 else blocks[0])  # neighbour's block, marked no fix


def test_has_audio(tmp_path: Path) -> None:
    def trak(handler: bytes) -> bytes:
        return box(b"trak", box(b"mdia", box(b"hdlr", bytes(8) + handler + bytes(12))))

    assert has_audio(write(tmp_path, box(b"moov", mvhd(1.0) + trak(b"vide") + trak(b"soun"))))
    assert not has_audio(write(tmp_path, box(b"moov", mvhd(1.0) + trak(b"vide"))))
    assert not has_audio(write(tmp_path, box(b"ftyp", b"isom")))


def test_read_gps_fixes_block_size_field(tmp_path: Path) -> None:
    bad = bytearray(gps_block(5.0))
    struct.pack_into(">I", bad, 0, 8)  # some firmware writes a short box size
    (got,), _ = read_gps(build_mp4(tmp_path / "c.mp4", blocks=[bytes(bad)]))
    assert struct.unpack_from(">I", got)[0] == len(got)


def test_gps_index_count_larger_than_box(tmp_path: Path) -> None:
    p = write(tmp_path, box(b"moov", mvhd(1.0) + box(b"gps ", struct.pack(">II", 0x101, 50))))
    with p.open("rb") as f, pytest.raises(Error):
        gps_index(f)


def test_join_tracks_one_block_per_second() -> None:
    a, b = gps_block(1.0), gps_block(2.0)
    # padded; gap filled from the minute before, marked no-fix; cut to length
    assert join_tracks([[a], [], [b, b, b]], [2, 3, 2]) == [a, a, *[no_fix(a)] * 3, b, b]
    assert join_tracks([[], [b]], [2, 1]) == [no_fix(b), no_fix(b), b]  # leading gap: from the minute after
    assert join_tracks([[], []], [60, 60]) == []


def test_join_tracks_rounding_does_not_add_up() -> None:
    a = gps_block(1.0)
    # per-minute rounding would give 23 + 23 = 46 blocks for 46.8 s of video
    assert len(join_tracks([[a], [a]], [23.4, 23.4])) == 47


def test_insert_gps_round_trip(tmp_path: Path) -> None:
    p = build_joined(tmp_path / "j.mp4")
    blocks = [gps_block(float(i), i) for i in range(5)]
    insert_gps(p, blocks, USER_BOX)
    with p.open("rb") as f:
        assert walks_to_end(f)
        moov = find(f, b"moov")
        assert moov is not None
        children = [b.type for b in iter_boxes(f, moov.body, moov.end)]
        assert children[:3] == [b"mvhd", b"gps ", b"udta"]  # gps right after mvhd, for ExifTool
        assert [b.type for b in iter_boxes(f)][-1] == b"mdat"  # mdat did not move
    assert read_gps(p) == (blocks, USER_BOX)


# moov grows by the gps index (16 + 8 per block) and the udta wrapper (8 + user box); the reserved
# space is the free box's body plus its 8-byte header.
EXACT_FIT = 16 + 8 + 8 + len(USER_BOX) + 0x4000 - 8


def test_insert_gps_exact_fit(tmp_path: Path) -> None:
    p = build_joined(tmp_path / "j.mp4", reserve=EXACT_FIT)
    insert_gps(p, [gps_block(1.0)], USER_BOX)
    with p.open("rb") as f:
        assert walks_to_end(f)
        f.seek([b.start for b in iter_boxes(f)][2] + 4)
        assert f.read(8) == BLOCK_MAGIC  # no padding: the only free box left is the GPS block


@pytest.mark.parametrize("reserve", [100, EXACT_FIT + 3])  # last: leftover too small for a free box
def test_insert_gps_reserve_too_small(tmp_path: Path, reserve: int) -> None:
    p = build_joined(tmp_path / "j.mp4", reserve=reserve)
    before = p.read_bytes()
    with pytest.raises(Error, match="too small"):
        insert_gps(p, [gps_block(1.0)], USER_BOX)
    assert p.read_bytes() == before  # nothing written on failure


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (box(b"ftyp") + box(b"mdat", bytes(8)) + box(b"moov", mvhd(1.0)), "before mdat"),
        (box(b"moov", box(b"trak") + mvhd(1.0)) + box(b"free", bytes(1 << 16)) + box(b"mdat"), "mvhd"),
        (box(b"ftyp"), "no moov"),
    ],
)
def test_insert_gps_unexpected_layout(tmp_path: Path, data: bytes, match: str) -> None:
    with pytest.raises(Error, match=match):
        insert_gps(write(tmp_path, data), [gps_block(1.0)], b"")
