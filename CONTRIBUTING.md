# Contributing

Bug reports, camera reports and pull requests are welcome.

## Development setup

```sh
git clone https://github.com/fsantos00/dashcam-merge && cd dashcam-merge
python -m venv .venv && . .venv/bin/activate      # Windows: py -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"
```

On Windows use `py`, not `python`: `python` can be the Microsoft Store stub.

You also need `ffmpeg` on PATH for the end-to-end tests. Without it, they are skipped. CI sets
`DASHCAM_MERGE_REQUIRE_FFMPEG=1` so they fail instead.

## Checks (CI runs the same)

```sh
ruff check . && ruff format --check .
mypy
pytest
```

Tests never use real camera footage. Real clips contain GPS positions and people's faces.
`tests/conftest.py` builds tiny synthetic clips with ffmpeg (or plain bytes for the parser tests)
and puts made-up GPS blocks into them.

## Guidelines

* **No runtime dependencies.** The code uses only the standard library plus the `ffmpeg` binary.
* **Never write to the input clips.** They are opened read-only. Keep it that way.
* Library code raises `mp4.Error`. Only `cli.py` prints errors and picks
  exit codes.
* Every bug fix comes with a test that fails without it.
* Changes to the default layout need a good reason and a before/after screenshot in the PR.

## Adding a camera

Camera models differ only in how they name their files, as long as their GPS is in the Novatek
format ([docs/novatek-gps.md](docs/novatek-gps.md)). Everything camera-specific lives in
[`src/cameras.py`](src/cameras.py):

1. Add a `Naming` entry to `NAMING`: the camera model, a real file name as the example, a regular
   expression with the groups `minute`, `date`, `time` and `cam`, and which `cam` value is the
   front, rear and interior camera. `tests/test_cameras.py` checks every entry against this contract.
2. Add a few file names from that camera to `test_parse_clip_name`, including ones it must reject
   (event clips, photos).
3. Add the camera to the table in the README's "Supported cameras" once someone has run a real day
   through it.

## Reporting an unsupported camera

Please open an issue with:

1. the camera model and firmware version,
2. the clip file names (one minute is enough),
3. the output of this command, run from a clone of this repo on one front clip. It prints only the
   box structure (top level and inside `moov`), no GPS positions:

   ```sh
   python -c "import sys; sys.path.insert(0, 'src'); from mp4 import iter_boxes; f = open(sys.argv[1], 'rb'); [print(b.type, b.start, b.end - b.start) for t in iter_boxes(f) for b in [t, *(list(iter_boxes(f, t.body, t.end)) if t.type == b'moov' else [])]]" CLIP.MP4
   ```

Please don't attach clips publicly unless you're happy to share where and when they were recorded.
