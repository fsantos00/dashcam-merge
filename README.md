# dashcam-merge

Merges a day of dashcam clips from a Vantrue N4S (and probably other Novatek-based cameras, see
[supported cameras](#supported-cameras)) into one video per day. All three cameras go into one frame
with a speed readout, and the GPS track is kept, so Telemetry Overlay, RaceRender or anything else
that reads GPS through ExifTool can still add a map and telemetry afterwards.

```
┌────────────────────────────────────────────────────┐
│ (free for a map)   ┌──────────────────┐            │
│                    │ rear, mirrored   │            │
│                    └──────────────────┘            │
│                                                    │
│                 front camera, full frame           │
│                                                    │
│                                        ┌─────────┐ │
│ 87 km/h                                │interior │ │
│                                        └─────────┘ │
└────────────────────────────────────────────────────┘
```

The camera stores GPS in its own undocumented format, which a plain ffmpeg join drops. I reverse
engineered it from the clips' MP4 metadata ([notes](docs/novatek-gps.md)) and the tool writes it back
into the merged file, one GPS point per second of video.

Other details:

- Uses the GPU's H.265 encoder (AMD, NVIDIA, Intel, Apple) if there is one, otherwise x265 or x264.
  Two minutes render at a time.
- Rendered minutes are cached, so an interrupted run continues where it stopped.
- A damaged minute is skipped along with its GPS, so the rest stays in sync.
- Needs only Python 3.11+ and ffmpeg. The original clips are never modified.

## Install

1. Install **ffmpeg**:
   * Windows: `winget install Gyan.FFmpeg`, then open a new terminal
   * macOS: `brew install ffmpeg`
   * Linux: `sudo apt install ffmpeg` (or your distribution's package)
2. Install dashcam-merge with [pipx](https://pipx.pypa.io), which gives it an environment of its
   own (its modules are named plainly `cli`, `cameras`, `mp4` and `render`, so they shouldn't share one):
   ```sh
   pipx install git+https://github.com/fsantos00/dashcam-merge
   ```

## Use

Copy the clips off the SD card into a folder (subfolders are fine), then point dashcam-merge at it:

```sh
dashcam-merge ~/Videos/dashcam                    # macOS / Linux
dashcam-merge "C:\Users\me\Videos\dashcam"        # Windows
dashcam-merge ~/Videos/dashcam -o ~/Videos/trips  # write the videos elsewhere
```

```
dashcam/
├── …_N_A.MP4  …_N_B.MP4  …_N_C.MP4   clips from the SD card, in any subfolders
├── trip-2025-06-14.mp4               one video per day found
└── .dashcam-merge-cache/           rendered minutes; removed after a full run
```

You get one `trip-YYYY-MM-DD.mp4` per day found. Useful options:

| Option | What it does |
| --- | --- |
| `-o DIR` | write the videos somewhere else |
| `--clip FILE [FILE ...]` | quick test: render only those minutes into `trip-preview.mp4` (they are reused by the full run). Put the folder first: `dashcam-merge DIR --clip FILE` |
| `--units mph` | speed in mph |
| `--encoder NAME` | force an encoder instead of the first one that works (`--help` lists them) |
| `--dry-run` | show the clips found, the encoder and the ffmpeg filtergraph, then stop |

`dashcam-merge --help` lists everything. Exit code 3 means the video was made but some minutes were
skipped (they are listed at the end).

### Getting the GPS into Telemetry Overlay / RaceRender

Load the `trip-YYYY-MM-DD.mp4` itself as the data file. These tools read the camera's GPS with ExifTool,
and dashcam-merge writes it where ExifTool looks. To check by hand:

```sh
exiftool -ee -G3 -GPSLatitude -GPSLongitude -GPSSpeed trip-2025-06-14.mp4
```

## Supported cameras

| Camera | Status |
| --- | --- |
| **Vantrue N4S** (front 2560×1440, interior, rear) | Tested |
| Other cameras that meet both requirements below | Should work, untested. Please report back either way |
| Cameras with other file names (most non-Vantrue brands) | Not yet. Supporting one is a small change ([adding a camera](CONTRIBUTING.md#adding-a-camera)), or [open an issue](CONTRIBUTING.md#reporting-an-unsupported-camera) |

A camera works if:

1. **It stores GPS the Novatek way**: a `gps ` index in `moov` pointing at `freeGPS` blocks
   ([format](docs/novatek-gps.md)). If `exiftool -ee -GPSSpeed CLIP.MP4` prints speeds, it does.
2. **Its file names match a pattern in [`src/cameras.py`](src/cameras.py).** Today that is
   Vantrue's `YYYYMMDD_HHMMSS_NNNNN_N_<A|B|C>.MP4` (A = front, B = interior, C = rear). Check with
   `dashcam-merge DIR --dry-run`: it lists every minute it recognised, and which cameras it found.

Front-only and front + rear cameras work too, and a minute can be missing its rear or interior clip. The layout
is designed for a 16:9 front camera, and the front is scaled to 2560×1440.

Tried it on another camera? An issue with the model and the result, good or bad, gets it added to
this table.

Minutes without a front clip are left out, because the duration, audio and GPS come from the front
camera.

## How it works

1. Clips are grouped by minute. Each minute is rendered on its own with one ffmpeg filtergraph
   (scale, mirror and crop the rear, crop the interior's date stamp, overlay, burn in the speed
   subtitle) into a cache folder.
2. The minutes are joined without re-encoding. ffmpeg is told to reserve room in front of the video
   data.
3. The GPS blocks from every front clip are written into that room, together with a new `gps `
   index. The video data never moves, so nothing else in the file has to change.
4. The finished file is checked: box structure, every GPS entry points at a GPS block, duration.

Neither Vantrue nor Novatek documents the GPS format. I worked it out by pulling apart the camera's
MP4 boxes (the `gps ` index in `moov` and the `freeGPS` blocks it points at) and comparing against
ExifTool's output. The details are in [docs/novatek-gps.md](docs/novatek-gps.md).

## FAQ

**The speed shows `--`.** The camera had no GPS fix for that second (common in the first minutes).

**I changed an option. Do I have to delete the cache?** No. Cached minutes are keyed on the cameras,
encoder, units, layout and version, so a change re-renders automatically.

**A drive went past midnight and I got two videos.** That's by design: there is one video per date,
taken from the clips' file names.

**Can I stop and resume?** Yes: press Ctrl+C and run the same command again.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Bug reports with a camera model and a box dump help
a lot.

## License

[MIT](LICENSE). Not affiliated with Vantrue or Novatek.
