"""Which camera recorded a clip, and when: the only file a new camera model needs to touch.

Add a camera by adding one :class:`Naming` entry to :data:`NAMING`, with a real file name as its
example, plus cases in ``tests/test_cameras.py``. Its GPS must be in the Novatek format (docs/novatek-gps.md).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import NamedTuple

ROLES = ("front", "rear", "interior")  # also the ffmpeg input order: the front is always input 0


class Clip(NamedTuple):
    minute: str  # what the cameras of one minute share, e.g. "20261122_141923_20958_N"
    start: dt.datetime
    role: str  # one of ROLES


class Naming(NamedTuple):
    camera: str
    example: str  # a real file name from this camera; the tests check it parses
    # Groups: minute (no dots: cache files are "<minute>.*"), date and time (their digits read as
    # YYYYMMDD and HHMMSS, separators ignored) and cam, the key into ``roles``.
    pattern: re.Pattern[str]
    roles: dict[str, str]


NAMING = [
    Naming(
        "Vantrue N4S",
        "20261122_141923_20958_N_A.MP4",  # date, time, counter, N = normal loop recording, camera
        re.compile(r"^(?P<minute>(?P<date>\d{8})_(?P<time>\d{6})_\d+_N)_(?P<cam>[ABC])\.mp4$", re.IGNORECASE),
        {"A": "front", "B": "interior", "C": "rear"},
    ),
]


def parse_clip_name(name: str) -> Clip | None:
    """The clip a file name describes, or None if no supported camera names files like that."""
    for _, _, pattern, roles in NAMING:
        if m := pattern.match(name):
            try:  # rejects month 13 and the like
                start = dt.datetime.strptime(re.sub(r"\D", "", m["date"] + m["time"]), "%Y%m%d%H%M%S")
            except ValueError:
                return None
            return Clip(m["minute"], start, roles[m["cam"].upper()])
    return None
