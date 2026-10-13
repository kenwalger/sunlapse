#!/usr/bin/env python3
"""Stitch the daily frames into a video.

.. code-block:: console

    $ python3 make_video.py                      # dissolve, the default
    $ python3 make_video.py --style cut          # hard cuts between days
    $ python3 make_video.py --seconds 45         # aim for a length
    $ python3 make_video.py --from 2026-10-12 --to 2026-11-30
    $ python3 make_video.py --list               # what is on disk, and gaps

Requires ``ffmpeg``. Nothing else.

On style: a day-per-frame lapse played as hard cuts is a slideshow. At six
frames a second, six weeks of captures runs seven seconds and stutters. The
dissolve turns the same frames into drift and buys length without inventing
data, taking 42 frames from seven seconds to about twenty.

The dissolve is produced with ``minterpolate`` rather than a chain of
``xfade`` filters because it takes a single input, so it still works at 365
frames instead of needing a 364-step filter graph.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

HERE: Path = Path(__file__).resolve().parent
FRAMES: Path = HERE / "frames"

#: Dates spanned by a gap, and how many days are missing inside it.
Gap = tuple[date, date, int]


def frames_in_range(lo: date | None, hi: date | None) -> list[tuple[date, Path]]:
    """Collect frames whose filename date falls inside a range.

    :param lo: Earliest date to include, or ``None`` for no lower bound.
    :param hi: Latest date to include, or ``None`` for no upper bound.
    :returns: Date and path pairs, sorted by date.
    :rtype: list[tuple[date, Path]]
    """
    found: list[tuple[date, Path]] = []
    for path in sorted(FRAMES.glob("*.jpg")):
        try:
            when = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if (lo is None or when >= lo) and (hi is None or when <= hi):
            found.append((when, path))
    return found


def report_gaps(items: list[tuple[date, Path]]) -> list[Gap]:
    """Find missing days between consecutive frames.

    A lapse with a hole in it looks continuous once encoded, so the hole
    has to be reported before the video is published rather than after.

    :param items: Date and path pairs, sorted by date.
    :returns: One entry per gap, as ``(before, after, missing_days)``.
    :rtype: list[Gap]
    """
    gaps: list[Gap] = []
    for (first, _), (second, _) in zip(items, items[1:]):
        missing = (second - first).days - 1
        if missing > 0:
            gaps.append((first, second, missing))
    return gaps


def build_command(listfile: Path, out: Path, style: str,
                  input_fps: float, output_fps: int) -> list[str]:
    """Assemble the ``ffmpeg`` invocation.

    :param listfile: Concat demuxer list of frame paths.
    :param out: Destination video path.
    :param style: Either ``"dissolve"`` or ``"cut"``.
    :param input_fps: Frames per second before interpolation.
    :param output_fps: Frames per second of the encoded video.
    :returns: Argument list suitable for :func:`subprocess.run`.
    :rtype: list[str]
    """
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "concat", "-safe", "0",
           "-r", f"{input_fps}", "-i", str(listfile)]
    if style == "dissolve":
        cmd += ["-vf", f"minterpolate=fps={output_fps}:mi_mode=blend,"
                       f"format=yuv420p"]
    else:
        cmd += ["-vf", f"fps={output_fps},format=yuv420p"]
    return cmd + ["-c:v", "libx264", "-crf", "18", "-preset", "medium",
                  "-movflags", "+faststart", str(out)]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments.

    :param argv: Argument list, defaulting to :data:`sys.argv` minus the
        program name.
    :returns: Parsed arguments.
    :rtype: argparse.Namespace
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-o", "--out", default="sunset.mp4",
                        help="output video path")
    parser.add_argument("--style", choices=["dissolve", "cut"],
                        default="dissolve", help="transition between days")
    parser.add_argument("--seconds", type=float,
                        help="target length; overrides --input-fps")
    parser.add_argument("--input-fps", type=float, default=2.0,
                        help="frames per second before interpolation")
    parser.add_argument("--output-fps", type=int, default=30,
                        help="frames per second of the encoded video")
    parser.add_argument("--from", dest="lo", help="earliest date, YYYY-MM-DD")
    parser.add_argument("--to", dest="hi", help="latest date, YYYY-MM-DD")
    parser.add_argument("--list", action="store_true",
                        help="report frames and gaps, then stop")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Report on the frames and, unless ``--list`` was given, encode them.

    :param argv: Argument list, defaulting to :data:`sys.argv` minus the
        program name.
    :returns: Process exit code. ``0`` on success, ``1`` when there are no
        frames, otherwise whatever ``ffmpeg`` returned.
    :rtype: int
    """
    args = parse_args(argv)
    lo = date.fromisoformat(args.lo) if args.lo else None
    hi = date.fromisoformat(args.hi) if args.hi else None
    items = frames_in_range(lo, hi)

    if not items:
        print(f"No frames in {FRAMES}", file=sys.stderr)
        return 1

    span = (items[-1][0] - items[0][0]).days + 1
    print(f"{len(items)} frames, {items[0][0]} to {items[-1][0]} "
          f"({span} days spanned)")

    gaps = report_gaps(items)
    if gaps:
        print(f"!! {sum(g[2] for g in gaps)} missing day(s) across "
              f"{len(gaps)} gap(s):")
        for first, second, missing in gaps:
            print(f"     {missing:3d} missing between {first} and {second}")
        print("   The video will cut straight over them. Say so in the post,")
        print("   or note it on screen, rather than letting it look "
              "continuous.")
    if args.list:
        return 0

    input_fps = (len(items) / args.seconds) if args.seconds else args.input_fps
    out = Path(args.out)

    listfile = HERE / ".frames.txt"
    listfile.write_text(
        "".join(f"file '{path.as_posix()}'\n" for _, path in items),
        encoding="utf-8")

    print(f"style {args.style}, {input_fps:.2f} fps in, "
          f"{args.output_fps} fps out")
    print(f"expected length about {len(items) / input_fps:.1f} s")

    try:
        result = subprocess.run(build_command(listfile, out, args.style,
                                              input_fps, args.output_fps))
    finally:
        listfile.unlink(missing_ok=True)

    if result.returncode != 0:
        return result.returncode

    print(f"wrote {out}")
    print("\nTo pull it over and clear the card:")
    print(f"  scp pi@HOST:~/sunlapse/{out.name} .")
    print("  # check it plays, THEN:")
    print("  # rm ~/sunlapse/frames/*.jpg")
    print("The frames are the originals and the video is derived from them.")
    print("Delete the originals only once the video is somewhere else.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
