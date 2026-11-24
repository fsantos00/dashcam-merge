# The Novatek GPS track in Vantrue MP4 files

Vantrue cameras (Novatek chipset) do not store GPS as a normal MP4 track. They write one
**16 KB `free` box per second** into the middle of the video data and point to those boxes from a
custom `gps ` box in `moov`. Players skip `free` boxes, so the video still plays normally.
ExifTool (`-ee`) knows the format, and so do the telemetry tools that use it.

There is no public spec for this. Everything below was worked out by reading the box structure of
clips from a Vantrue N4S and comparing against what ExifTool extracts. Offsets are in bytes and
fields are little-endian unless noted.

## File layout (as written by the camera)

```
ftyp
skip
moov
  mvhd
  trak (video)
  trak (audio)
  udta
    user          "NOVATEK" maker tag and model string
  gps             index of the GPS blocks (below)
skip
mdat              video and audio samples, with a GPS block between samples about once a second
```

## `moov/gps ` index box (big-endian)

| Offset in body | Size | Field |
| --- | --- | --- |
| 0 | 4 | version/flags, `0x00000101` |
| 4 | 4 | number of entries *n* |
| 8 | 8·*n* | *n* × (u32 absolute file offset, u32 size) |

Each entry points at one GPS block. Size is `0x4000` (16384).

## GPS block

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 4 | box size, big-endian (`0x00004000`) |
| 4 | 4 | `free` |
| 8 | 4 | `GPS ` |
| 12 | 4 | unknown (`232` on the N4S) |
| 16 / 20 / 24 | 4 each | hour, minute, second (UTC) |
| 28 | 1 | fix status: `A` = valid, `V` = no fix |
| 32 | 8 | latitude, float64, NMEA `ddmm.mmmm` |
| 40 | 1 | `N` / `S` |
| 48 | 8 | longitude, float64, NMEA `dddmm.mmmm` |
| 56 | 1 | `E` / `W` |
| 64 | 8 | ground speed, float64, **knots** |
| 72 | 8 | course over ground, float64, degrees |
| 80 / 84 / 88 | 4 each | year − 2000, month, day |
| 105 | text | the raw `$GNRMC` NMEA sentence |

dashcam-merge itself only reads the fix status (28) and speed (64). It copies blocks as they are.

## What dashcam-merge writes

ffmpeg's concat join drops all of this. After joining (with `-moov_size` so the `moov` box sits in
front of `mdat` with room to spare), dashcam-merge:

1. puts a new `gps ` index **directly after `mvhd`**, followed by a `udta` that holds the camera's
   `user` box. ExifTool only reads a `gps ` box that comes before any handler box. ffmpeg writes
   `udta/meta/hdlr`, so the camera's original position at the end of `moov` would be ignored.
   ffmpeg's own `udta` stays further down, so the file has two `udta` boxes;
2. writes all GPS blocks right after `moov`, inside the reserved room;
3. fills what is left with a `free` box.

`mdat` never moves, so the `stco`/`co64` chunk offsets stay valid, and the GPS offsets stay far
below 4 GB.

Each minute's GPS is padded (by repeating its last block) or trimmed to `round(end) - round(start)`
blocks, where *start* and *end* are the minute's place in the joined video, measured with the
rendered minute's own length. Rounding the running total, not each minute, keeps the error below
half a second however long the day. A minute with no GPS at all gets copies of a neighbouring block
marked `V` (no fix), and so does an index entry that does not point at a GPS block. That way second
*k* of the video always matches GPS entry *k*.

## Reference

ExifTool's `QuickTimeStream.pl` (`ProcessFreeGPS`) parses this and many other dashcam variants.
