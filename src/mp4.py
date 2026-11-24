"""Just enough MP4 box parsing to read and rewrite the Novatek GPS track (see docs/novatek-gps.md).

In short: ``moov/gps `` is an index of (offset, size) pairs pointing at 16 KB ``free`` boxes that
start with ``GPS ``, one per second of video.
"""

from __future__ import annotations

import os
import re
import struct
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import BinaryIO, NamedTuple

BLOCK_MAGIC = b"freeGPS "  # bytes 4..12 of every GPS block
GPS_INDEX_VERSION = 0x101  # first u32 of the ``gps `` box body as written by the camera
FIX_OFFSET = 28  # b"A" = valid fix, b"V" = no fix
SPEED_OFFSET = 64  # little-endian float64, knots


class Error(Exception):
    """Every error this package raises on purpose; the CLI prints it without a traceback."""


class Box(NamedTuple):
    type: bytes
    start: int  # offset of the size field
    header: int  # 8, or 16 for a 64-bit size
    end: int  # offset just past the box

    @property
    def body(self) -> int:
        return self.start + self.header


def iter_boxes(f: BinaryIO, start: int = 0, end: int | None = None) -> Iterator[Box]:
    """Yield the boxes in ``[start, end)``. A box running past ``end`` is still yielded
    (clips cut off by a power loss look like that)."""
    end = os.fstat(f.fileno()).st_size if end is None else end
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        raw = f.read(8)
        if len(raw) < 8:
            raise Error(f"truncated box header at offset {pos}")
        size, typ = struct.unpack(">I4s", raw)
        header = 8
        if size == 1:
            raw = f.read(8)
            if len(raw) < 8:
                raise Error(f"truncated 64-bit box size at offset {pos}")
            size, header = struct.unpack(">Q", raw)[0], 16
        elif size == 0:  # "extends to the end of the enclosing space"
            size = end - pos
        if size < header:
            raise Error(f"corrupt {typ!r} box at offset {pos} (size {size})")
        yield Box(typ, pos, header, pos + size)
        pos += size


def find(f: BinaryIO, *path: bytes, start: int = 0, end: int | None = None) -> Box | None:
    """The first box matching ``path`` (e.g. ``b"moov", b"udta"``), or None."""
    for box in iter_boxes(f, start, end):
        if box.type == path[0]:
            return box if len(path) == 1 else find(f, *path[1:], start=box.body, end=box.end)
    return None


def movie_duration(path: Path) -> float:
    """Duration in seconds from ``moov/mvhd``."""
    with path.open("rb") as f:
        mvhd = find(f, b"moov", b"mvhd")
        if mvhd is None:
            raise Error("no moov/mvhd box (is this an MP4 file?)")
        f.seek(mvhd.body)
        data = f.read(min(mvhd.end - mvhd.body, 32))
    v1 = data[:1] == b"\x01"  # version 1: 64-bit times and duration
    if len(data) < (32 if v1 else 20):
        raise Error("mvhd box is too short")
    timescale, duration = struct.unpack_from(">IQ" if v1 else ">II", data, 20 if v1 else 12)
    if timescale == 0:
        raise Error("mvhd timescale is 0")
    return float(duration / timescale)


def has_audio(path: Path) -> bool:
    """True if any ``moov/trak`` has a sound handler (``mdia/hdlr`` type ``soun``)."""
    with path.open("rb") as f:
        if (moov := find(f, b"moov")) is None:
            return False
        f.seek(moov.body)
        data = f.read(moov.end - moov.body)
    return re.search(rb"hdlr.{8}soun", data, re.DOTALL) is not None  # version/flags, pre_defined, type


def gps_index(f: BinaryIO) -> list[tuple[int, int]]:
    """(offset, size) pairs from the ``moov/gps `` box, or [] if there is none."""
    box = find(f, b"moov", b"gps ")
    if box is None:
        return []
    f.seek(box.body)
    head = f.read(8)
    if len(head) < 8:
        raise Error("gps index box is too short")
    count = struct.unpack(">II", head)[1]
    if box.body + 8 + 8 * count > box.end:
        raise Error(f"gps index claims {count} entries but the box only has room for fewer")
    return [struct.unpack(">II", f.read(8)) for _ in range(count)]


def read_gps(path: Path) -> tuple[list[bytes], bytes]:
    """The clip's GPS blocks, one per index entry, plus its raw ``moov/udta/user`` box (b"" if absent).

    An entry that is not a GPS block becomes a no-fix copy of a neighbour, so entry k stays second k.
    """
    with path.open("rb") as f:
        found: list[bytes | None] = []
        for offset, size in gps_index(f):
            f.seek(offset)
            block = bytearray(f.read(size))
            if len(block) == size and block[4:12] == BLOCK_MAGIC:
                struct.pack_into(">I", block, 0, size)  # the box size must cover the whole block
                found.append(bytes(block))
            else:
                found.append(None)
        good = [b for b in found if b]
        blocks: list[bytes] = []
        for b in found if good else ():
            blocks.append(b or no_fix(blocks[-1] if blocks else good[0]))
        user = find(f, b"moov", b"udta", b"user")
        if user is None:
            return blocks, b""
        f.seek(user.start)
        return blocks, f.read(user.end - user.start)


def speed_knots(block: bytes) -> float | None:
    """Ground speed from a GPS block, or None without a valid fix."""
    if len(block) < SPEED_OFFSET + 8 or block[FIX_OFFSET : FIX_OFFSET + 1] != b"A":
        return None
    return float(struct.unpack_from("<d", block, SPEED_OFFSET)[0])


def no_fix(block: bytes) -> bytes:
    """A copy of ``block`` marked "no fix", for seconds that have no GPS of their own."""
    return block[:FIX_OFFSET] + b"V" + block[FIX_OFFSET + 1 :]


def join_tracks(tracks: Sequence[Sequence[bytes]], durations: Sequence[float]) -> list[bytes]:
    """One block per second of the joined video, so GPS entry k is always second k.

    Each minute gets ``round(end) - round(start)`` blocks on the running total, so rounding never
    adds up. Short tracks repeat their last block; a minute with no GPS gets no-fix copies of the
    nearest block from another minute. Returns [] if no minute has GPS.
    """
    if not any(tracks):
        return []
    out: list[bytes] = []
    elapsed = 0.0
    for i, (track, duration) in enumerate(zip(tracks, durations, strict=True)):
        n = round(elapsed + duration) - round(elapsed)
        elapsed += duration
        if track:
            out += ([*track] + [track[-1]] * n)[:n]
            continue
        before = next((t[-1] for t in reversed(tracks[:i]) if t), b"")
        out += [no_fix(before or next(t[0] for t in tracks[i + 1 :] if t))] * n
    return out


def insert_gps(path: Path, blocks: Sequence[bytes], user_box: bytes) -> None:
    """Write a ``gps `` index, the GPS blocks and ``udta/user`` into the space ffmpeg reserved
    after moov with ``-moov_size``. mdat never moves, so no chunk offset changes."""
    if not blocks and not user_box:
        return
    with path.open("r+b") as f:
        top = {box.type: box for box in iter_boxes(f)}
        if b"moov" not in top or b"mdat" not in top:
            raise Error("joined file has no moov or mdat box")
        moov, mdat_start = top[b"moov"], top[b"mdat"].start
        if moov.header != 8 or moov.start > mdat_start:
            raise Error("unexpected joined file layout: moov is not reserved before mdat")
        f.seek(moov.start)
        mv = bytearray(f.read(moov.end - moov.start))

        udta = struct.pack(">I4s", 8 + len(user_box), b"udta") + user_box if user_box else b""
        index = struct.pack(">I4sII", 16 + 8 * len(blocks), b"gps ", GPS_INDEX_VERSION, len(blocks))
        pos = moov.start + len(mv) + len(index) + 8 * len(blocks) + len(udta)
        for block in blocks:
            index += struct.pack(">II", pos, len(block))
            pos += len(block)

        # Right after mvhd: ExifTool only reads 'gps ' before any handler box (ffmpeg writes udta/meta).
        if mv[12:16] != b"mvhd":
            raise Error("mvhd is not the first box in moov")
        mvhd_end = 8 + struct.unpack_from(">I", mv, 8)[0]
        mv[mvhd_end:mvhd_end] = index + udta
        struct.pack_into(">I", mv, 0, len(mv))

        gap = mdat_start - pos
        if gap < 0 or 0 < gap < 8:
            raise Error(f"reserved space too small by {max(-gap, 8 - gap)} bytes")
        f.seek(moov.start)
        f.write(mv)
        f.writelines(blocks)
        if gap:
            f.write(struct.pack(">I4s", gap, b"free") + bytes(gap - 8))
