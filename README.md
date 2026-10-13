# sunlapse

One frame a day, a fixed number of minutes before sunset, stitched into a
video. Runs on a Raspberry Pi with a camera module pointed out of a window.

Standard library Python plus `ffmpeg` and `rpicam-still`. No API key, no
hosted service, no network at capture time.

## Why sunset-relative and not a clock time

At 45 degrees north, sunset moves from about 16:29 in early December to
about 21:02 in late June. A fixed clock time would put the sun in a
completely different place in the sky across the year, and the light and
framing would never settle.

Sunset time is a closed-form calculation from latitude, longitude and the
date. It is in `capture.py` as `sunset_utc`, it is about forty lines, and
it has no dependencies. Daylight saving changes become invisible: the clock
jumps an hour and the target time moves with it, so there is no seam in the
video.

## Setup

Edit the config block at the top of `capture.py`: latitude, longitude,
timezone, minutes before sunset, and the backup directory.

Check it before leaving it alone:

```bash
python3 capture.py --dry-run
```

Then two cron lines. Both matter.

```cron
# take today's frame if it is due and we do not already have one
*/15 * * * *  /usr/bin/python3 /home/USER/sunlapse/capture.py >> /home/USER/sunlapse/cron.log 2>&1

# weekly second copy onto a USB stick left plugged into the Pi
0 3 * * 0     rsync -a /home/USER/sunlapse/frames/ /media/backup/sunlapse/
```

Running the capture every fifteen minutes rather than once at the right
time is deliberate. The script works out whether today's frame is due and
does nothing if it already has one, so a reboot, a power cut or a missed
window costs you one wasted check rather than one day.

## Checking on it

```bash
python3 capture.py --status
```

```
sunlapse status                         2026-11-22 21:46 PST

frames        41  (2026-10-12 to 2026-11-22, 42 days spanned)
gaps          1 missing day(s) across 1 gap(s)
                1 between 2026-10-30 and 2026-11-01
today         captured   (sunset 16:41, target 16:21)
last record   2026-11-22  ok, 2 min after target
last 14       13 ok, 1 not ok
                2026-10-31  camera_failed  ERROR: *** no cameras available ***
disk          23.4 GB free
backup        39 of 41 frames, newest 2026-11-20, 2 day(s) behind
```

That is the whole check. Frames, gaps, whether today happened, what
recently went wrong, disk, and whether a second copy exists.

Every capture also appends a line to `record.jsonl` with the computed
sunset, the target time, when the frame was actually taken, how late it
was, bytes written and free disk. The record is the only thing that can
tell you a capture went wrong on a day you were not looking.

## Making the video

```bash
python3 make_video.py --list              # frames on disk, and any gaps
python3 make_video.py                     # dissolve, the default
python3 make_video.py --seconds 45        # aim for a length
python3 make_video.py --style cut         # hard cuts
python3 make_video.py --from 2026-10-12 --to 2026-11-30
```

**On the dissolve.** A frame a day played as hard cuts is a slideshow. Six
weeks at six frames a second is seven seconds long and it stutters. The
dissolve turns the same frames into drift and buys length without
inventing data: 42 frames become about twenty seconds instead of seven.

`--list` reports missing days before you publish something that looks
continuous and is not.

## Getting it off the Pi

```bash
scp pi@HOST:~/sunlapse/sunset.mp4 .
```

The frames are the originals and the video is derived from them. A
re-encode is always possible from the frames and nothing is recoverable
from the video, so delete originals last and only once the video exists
somewhere else.

---

## Things that will actually end this project

Running the capture is close to fire and forget. These are not.

### The SD card, which is the real one

A year of daily writes on a card that is also running an OS. Cards fail,
and month eight is exactly the sort of time it happens. If it dies you lose
every sunset since October and you cannot re-shoot them, because they
already happened.

This is why there are two cron lines. An eight dollar USB stick left
plugged into the Pi, rsynced weekly, gives you two independent failure
domains with no network, no SSH keys and no laptop that has to be awake.
`--status` tells you whether that copy is current, and says so loudly when
it is not.

### Storage is not the problem

At one frame a day, 3280 x 2464 at quality 92, a frame is roughly 3 MB:

| Span | Storage |
|---|---:|
| 6 weeks | about 0.12 GB |
| 1 year | about 1.1 GB |
| 2 years | about 2.1 GB |

A 32 GB card with the OS and Ollama models on it still has room for roughly
twenty years of frames. The free-space floor in `capture.py` is insurance
against runaway logs and apt caches, not against your own captures.

### The camera cannot move

Not in month three, not when someone cleans the window, not when the sill
gets used for something else. A lapse whose frame shifts partway through is
a lapse you throw away.

Mount it rigidly, mark the position, and photograph the mounting so you can
restore it if something knocks it.

### Smaller things, worth knowing rather than worrying about

**The Pi has no real-time clock.** After a power cut it gets the time from
the network at boot, so if wifi is slow to come up the first capture could
fire against a wrong clock. The record logs both the computed target and
the actual capture time, so this shows up rather than hiding.

**`unattended-upgrades` could change `rpicam-apps` under you.** Low risk,
and it would appear as a run of `camera_failed` lines in the record and in
`--status`.

**The camera ribbon connector does not love a year of thermal cycling.** If
frames stop suddenly, reseat it before assuming anything worse.

### The one habit worth keeping

Run `--status` once a month. It takes five seconds, and it is looking for
the two things that fail silently: a capture that has been broken for
weeks, and a backup that quietly stopped happening.
