#!/usr/bin/env python3
"""
Stitch the daily frames into a video.

    python3 make_video.py                      # dissolve, the default
    python3 make_video.py --style cut          # hard cuts between days
    python3 make_video.py --seconds 45         # aim for a length
    python3 make_video.py --from 2026-10-12 --to 2026-11-30
    python3 make_video.py --list               # what is on disk, and gaps

Needs ffmpeg. Nothing else.

On style: a day-per-frame lapse played as hard cuts is a slideshow. At six
frames a second, six weeks of captures is seven seconds long and it
stutters. The dissolve is what turns the same frames into something that
reads as drift, and it buys length without inventing data: 42 frames become
twenty seconds instead of seven.
"""

import argparse
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRAMES = HERE / "frames"


def frames_in_range(lo, hi):
    out = []
    for p in sorted(FRAMES.glob("*.jpg")):
        try:
            d = date.fromisoformat(p.stem)
        except ValueError:
            continue
        if (lo is None or d >= lo) and (hi is None or d <= hi):
            out.append((d, p))
    return out


def report_gaps(items):
    """A lapse with a hole in it should say so before you publish it."""
    if len(items) < 2:
        return []
    gaps = []
    for (d1, _), (d2, _) in zip(items, items[1:]):
        missing = (d2 - d1).days - 1
        if missing > 0:
            gaps.append((d1, d2, missing))
    return gaps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="sunset.mp4")
    ap.add_argument("--style", choices=["dissolve", "cut"], default="dissolve")
    ap.add_argument("--seconds", type=float,
                    help="target length; overrides --input-fps")
    ap.add_argument("--input-fps", type=float, default=2.0,
                    help="frames per second BEFORE interpolation (default 2)")
    ap.add_argument("--output-fps", type=int, default=30)
    ap.add_argument("--from", dest="lo")
    ap.add_argument("--to", dest="hi")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    lo = date.fromisoformat(args.lo) if args.lo else None
    hi = date.fromisoformat(args.hi) if args.hi else None
    items = frames_in_range(lo, hi)

    if not items:
        print(f"No frames in {FRAMES}", file=sys.stderr)
        return 1

    gaps = report_gaps(items)
    span = (items[-1][0] - items[0][0]).days + 1

    print(f"{len(items)} frames, {items[0][0]} to {items[-1][0]} "
          f"({span} days spanned)")
    if gaps:
        total = sum(g[2] for g in gaps)
        print(f"!! {total} missing day(s) across {len(gaps)} gap(s):")
        for d1, d2, n in gaps:
            print(f"     {n:3d} missing between {d1} and {d2}")
        print("   The video will cut straight over them. Say so in the post,")
        print("   or note it on screen, rather than letting it look continuous.")
    if args.list:
        return 0

    in_fps = args.input_fps
    if args.seconds:
        in_fps = len(items) / args.seconds

    listfile = HERE / ".frames.txt"
    listfile.write_text("".join(f"file '{p.as_posix()}'\n" for _, p in items),
                        encoding="utf-8")

    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-r", f"{in_fps}", "-i", str(listfile)]
    if args.style == "dissolve":
        cmd += ["-vf", f"minterpolate=fps={args.output_fps}:mi_mode=blend,"
                       f"format=yuv420p"]
    else:
        cmd += ["-vf", f"fps={args.output_fps},format=yuv420p"]
    cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "medium",
            "-movflags", "+faststart", args.out]

    print(f"style {args.style}, {in_fps:.2f} fps in, {args.output_fps} fps out")
    print(f"expected length about {len(items)/in_fps:.1f} s")
    res = subprocess.run(cmd)
    listfile.unlink(missing_ok=True)
    if res.returncode != 0:
        return res.returncode

    print(f"wrote {args.out}")
    print("\nTo pull it over and clear the card:")
    print(f"  scp pi@HOST:~/sunlapse/{Path(args.out).name} .")
    print("  # check it plays, THEN:")
    print("  # rm ~/sunlapse/frames/*.jpg")
    print("The frames are the originals and the video is derived from them.")
    print("Delete the originals only once the video is somewhere else.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
