#!/usr/bin/env python3
"""Generate the project logo: one vertical stripe per day, plus the real
annual sunset curve traced across them.

Two sources for the stripe colors:

``synthetic``
    A seasonal palette, invented. What the repository ships with before
    there is a year of captures to draw on.

``frames``
    The average color of the horizon band of each captured frame. This is
    the version worth regenerating once the lapse has run a while, because
    it turns the logo from a stylized picture into a data portrait: every
    stripe is genuinely that evening's sky.

The curve is real in both cases. It is the sunset time for the configured
latitude, computed by :func:`capture.sunset_utc`, which is the same
function that decides when to take the picture.

.. code-block:: console

    $ python3 tools/make_logo.py                     # synthetic, the default
    $ python3 tools/make_logo.py --source frames     # real sky colors
    $ python3 tools/make_logo.py --png               # also write a PNG

Requires ``ffmpeg`` for ``--source frames``, which the project already
needs for video. ``--png`` additionally needs ``cairosvg``; without it the
script prints the equivalent command and carries on.
"""

from __future__ import annotations

import argparse
import colorsys
import math
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from capture import LAT, LON, sunset_utc  # noqa: E402

HERE: Path = Path(__file__).resolve().parent.parent
FRAMES: Path = HERE / "frames"
IMG: Path = HERE / "img"

WIDTH: int = 1200
HEIGHT: int = 420
PAD_X: int = 40
PAD_TOP: int = 44
PAD_BOTTOM: int = 54
GAP: float = 2.0
STRIPES: int = 73

#: Horizon colour is sampled from the bottom sixth of the frame.
HORIZON_CROP: str = "crop=iw:ih/6:0:ih*5/6,scale=1:1:flags=area"

#: Stripe gradients run from this night colour at the top.
NIGHT: str = "#0F1320"
BACKDROP: str = "#0E1116"
CURVE: str = "#F2A63C"
DOT: str = "#FFE2B8"

Sky = tuple[str, str]
"""Horizon colour and mid-sky colour for one stripe."""


def _hex(hue: float, light: float, sat: float) -> str:
    """Convert HLS to a hex colour string.

    :param hue: Hue in degrees, 0 to 360.
    :param light: Lightness, 0 to 1.
    :param sat: Saturation, 0 to 1.
    :returns: Colour as ``#rrggbb``.
    :rtype: str
    """
    red, green, blue = colorsys.hls_to_rgb((hue % 360) / 360.0, light, sat)
    return f"#{int(red * 255):02x}{int(green * 255):02x}{int(blue * 255):02x}"


def synthetic_sky(day_of_year: int) -> Sky:
    """Invent a plausible sky for a day of the year.

    Hue swings from rose in midwinter to gold at midsummer. This is a
    palette, not a measurement, and the README says so.

    :param day_of_year: Day index, 0 to 364.
    :returns: Horizon and mid-sky colours.
    :rtype: Sky
    """
    season = 0.5 - 0.5 * math.cos(2 * math.pi * day_of_year / 365.0)
    hue = 352 + 52 * season
    return (_hex(hue, 0.56 + 0.06 * season, 0.86),
            _hex(hue + 300, 0.26, 0.42))


def frame_sky(path: Path) -> Sky | None:
    """Sample the horizon colour from a captured frame.

    Uses ``ffmpeg`` to average the bottom sixth of the image down to a
    single pixel, which avoids adding an image library to a project that
    is otherwise standard library only.

    :param path: Path to a JPEG frame.
    :returns: Horizon and mid-sky colours, or ``None`` if ffmpeg could not
        read the file.
    :rtype: Sky | None
    """
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", HORIZON_CROP,
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True)
    if result.returncode != 0 or len(result.stdout) < 3:
        return None
    red, green, blue = (channel / 255 for channel in result.stdout[:3])
    hue, light, sat = colorsys.rgb_to_hls(red, green, blue)
    hue *= 360
    # Lift the sampled colour a little so a dull evening still reads, and
    # derive the mid-sky from it rather than inventing a second colour.
    horizon = _hex(hue, min(0.68, max(0.42, light * 1.15)),
                   min(0.95, max(0.35, sat * 1.25)))
    mid = _hex(hue + 300, 0.26, 0.42)
    return horizon, mid


def collect(source: str, count: int) -> list[tuple[date, Sky]]:
    """Build the list of days and stripe colours to draw.

    :param source: Either ``"synthetic"`` or ``"frames"``.
    :param count: Maximum number of stripes.
    :returns: Date and colour pairs, in date order.
    :rtype: list[tuple[date, Sky]]
    :raises SystemExit: If ``frames`` was asked for and none exist.
    """
    if source == "synthetic":
        start = date(date.today().year, 1, 1)
        step = max(1, 365 // count)
        days = [start + timedelta(days=i * step) for i in range(count)]
        return [(day, synthetic_sky((day - start).days)) for day in days]

    paths: list[tuple[date, Path]] = []
    for path in sorted(FRAMES.glob("*.jpg")):
        try:
            paths.append((date.fromisoformat(path.stem), path))
        except ValueError:
            continue
    if not paths:
        raise SystemExit(f"No frames in {FRAMES}. Use --source synthetic "
                         f"until the lapse has run a while.")

    stride = max(1, len(paths) // count)
    picked = paths[::stride][:count]
    out: list[tuple[date, Sky]] = []
    for day, path in picked:
        sky = frame_sky(path)
        if sky is not None:
            out.append((day, sky))
    if not out:
        raise SystemExit("ffmpeg could not read any frames.")
    return out


def build_svg(items: list[tuple[date, Sky]]) -> str:
    """Render the logo as an SVG document.

    :param items: Date and colour pairs, in date order.
    :returns: Complete SVG markup.
    :rtype: str
    """
    minutes: list[float] = []
    for day, _ in items:
        sunset = sunset_utc(day, LAT, LON)
        minutes.append(0.0 if sunset is None
                       else sunset.hour * 60 + sunset.minute
                       + sunset.second / 60)
    low, high = min(minutes), max(minutes)
    spread = (high - low) or 1.0
    width = (WIDTH - 2 * PAD_X) / len(items)
    body = HEIGHT - PAD_TOP - PAD_BOTTOM

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} '
        f'{HEIGHT}" width="{WIDTH}" height="{HEIGHT}" role="img" '
        f'aria-label="One vertical stripe per captured day, warm at the '
        f'horizon, with the real annual sunset-time curve traced across '
        f'them">', "<defs>"]
    for index, (_, (horizon, mid)) in enumerate(items):
        parts.append(
            f'<linearGradient id="g{index}" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="{NIGHT}"/>'
            f'<stop offset="0.5" stop-color="{mid}"/>'
            f'<stop offset="1" stop-color="{horizon}"/></linearGradient>')
    parts.append("</defs>")
    parts.append(f'<rect width="{WIDTH}" height="{HEIGHT}" '
                 f'fill="{BACKDROP}"/>')
    for index in range(len(items)):
        x = PAD_X + index * width
        parts.append(f'<rect x="{x:.2f}" y="{PAD_TOP}" '
                     f'width="{width - GAP:.2f}" height="{body}" '
                     f'fill="url(#g{index})" rx="1.5"/>')

    points = [(PAD_X + i * width + (width - GAP) / 2,
               PAD_TOP + 26 + (body - 66) * (1 - (m - low) / spread))
              for i, m in enumerate(minutes)]
    path = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    parts.append(f'<path d="{path}" fill="none" stroke="{BACKDROP}" '
                 f'stroke-width="8" stroke-linecap="round" opacity="0.6"/>')
    parts.append(f'<path d="{path}" fill="none" stroke="{CURVE}" '
                 f'stroke-width="2.5" stroke-linecap="round"/>')
    for x, y in points:
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.6" '
                     f'fill="{DOT}"/>')
    parts.append("</svg>")
    return "\n".join(parts)


def write_png(svg_path: Path, png_path: Path, width: int) -> bool:
    """Rasterize the SVG if ``cairosvg`` is importable.

    :param svg_path: Source SVG.
    :param png_path: Destination PNG.
    :param width: Output width in pixels.
    :returns: ``True`` if a PNG was written.
    :rtype: bool
    """
    try:
        import cairosvg
    except ImportError:
        print("cairosvg not installed, skipping PNG. Either:")
        print("  pip install cairosvg")
        print(f"  rsvg-convert -w {width} {svg_path} -o {png_path}")
        return False
    cairosvg.svg2png(url=str(svg_path), write_to=str(png_path),
                     output_width=width)
    return True


def main(argv: list[str] | None = None) -> int:
    """Generate the logo.

    :param argv: Argument list, defaulting to :data:`sys.argv` minus the
        program name.
    :returns: Process exit code.
    :rtype: int
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", choices=["synthetic", "frames"],
                        default="synthetic",
                        help="where stripe colours come from")
    parser.add_argument("--stripes", type=int, default=STRIPES,
                        help="how many days to draw")
    parser.add_argument("--out", default=str(IMG / "logo.svg"))
    parser.add_argument("--png", action="store_true",
                        help="also write a PNG beside the SVG")
    parser.add_argument("--png-width", type=int, default=1200)
    args = parser.parse_args(argv)

    items = collect(args.source, args.stripes)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_svg(items), encoding="utf-8")
    print(f"wrote {out}  ({len(items)} stripes, source: {args.source})")
    if args.source == "frames":
        print(f"      {items[0][0]} to {items[-1][0]}, real horizon colours")

    if args.png and write_png(out, out.with_suffix(".png"), args.png_width):
        print(f"wrote {out.with_suffix('.png')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
