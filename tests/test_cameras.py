from __future__ import annotations

import datetime as dt

import pytest

from cameras import NAMING, ROLES, Clip, Naming, parse_clip_name

START = dt.datetime(2026, 11, 22, 14, 19, 23)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # Vantrue N4S
        ("20261122_141923_20958_N_A.MP4", Clip("20261122_141923_20958_N", START, "front")),
        ("20261122_141923_20958_N_b.mp4", Clip("20261122_141923_20958_N", START, "interior")),
        ("20261122_141923_20958_N_C.MP4", Clip("20261122_141923_20958_N", START, "rear")),
        ("20261122_141923_20958_E_A.MP4", None),  # event clip, not loop recording
        ("20261322_141923_20958_N_A.MP4", None),  # month 13
        ("20261122_141923_20958_N_A.MP4.tmp", None),
    ],
)
def test_parse_clip_name(name: str, expected: Clip | None) -> None:
    assert parse_clip_name(name) == expected


@pytest.mark.parametrize("naming", NAMING, ids=lambda n: n.camera)
def test_naming_entries_follow_the_contract(naming: Naming) -> None:
    assert {"minute", "date", "time", "cam"} <= set(naming.pattern.groupindex)
    assert set(naming.roles.values()) <= set(ROLES)
    assert "front" in naming.roles.values()  # duration, audio and GPS come from the front camera
    clip = parse_clip_name(naming.example)
    assert clip is not None
    assert "." not in clip.minute  # the cache cleans up "<minute>.*"
