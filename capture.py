#!/usr/bin/env python3
"""Capture one frame per day a fixed number of minutes before sunset.

Designed to be run often from cron rather than once at the right time. The
script works out whether today's frame is due, takes it if so, and does
nothing if it already has one. A reboot, a power cut or a missed quarter
hour therefore costs one wasted check rather than one day, which matters
for something meant to run unattended for a year.

.. code-block:: cron

    */15 * * * * /usr/bin/python3 /home/USER/sunlapse/capture.py >> /home/USER/sunlapse/cron.log 2>&1

Standard library only. Sunset is computed from latitude, longitude and the
date using the NOAA solar position algorithm, so there is no network call,
no API key and no hosted service to outlive the project.

:var LAT: Observer latitude in decimal degrees, north positive.
:var LON: Observer longitude in decimal degrees, west negative.
:var MINUTES_BEFORE_SUNSET: How far ahead of sunset to take the frame.
:var BACKUP_DIR: Second copy location, checked by :func:`status`.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------- config

LAT: float = 45.0018                    # Silverton, Oregon
LON: float = -122.7834                  # west is negative
TZ: ZoneInfo = ZoneInfo("America/Los_Angeles")

MINUTES_BEFORE_SUNSET: int = 20

HERE: Path = Path(__file__).resolve().parent
FRAMES: Path = HERE / "frames"
RECORD: Path = HERE / "record.jsonl"

MIN_FREE_GB: float = 2.0                # refuse, loudly, below this
LATE_TOLERANCE_MIN: int = 90            # past this, record it as late

#: Where a second copy of the frames lives. A USB stick left plugged into
#: the Pi is enough. Set to ``None`` to turn the backup check off, but the
#: SD card is the single component most likely to end this project and a
#: year of sunsets cannot be re-shot.
BACKUP_DIR: Path | None = Path("/media/backup/sunlapse")
BACKUP_STALE_DAYS: int = 10             # warn if the copy falls this far behind

#: ``rpicam-still`` on current Raspberry Pi OS, ``libcamera-still`` on older.
CAMERA_ARGS: list[str] = [
    "--nopreview", "--timeout", "2000",
    "--width", "3280", "--height", "2464", "--quality", "92",
]

# ------------------------------------------------------------------- sun


def _julian_day(day: date) -> float:
    """Convert a calendar date to a Julian day number at 00:00 UTC.

    :param day: Gregorian calendar date.
    :returns: Julian day number.
    :rtype: float
    """
    year, month = day.year, day.month
    if month <= 2:
        year -= 1
        month += 12
    century = year // 100
    gregorian = 2 - century + century // 4
    return (math.floor(365.25 * (year + 4716))
            + math.floor(30.6001 * (month + 1))
            + day.day + gregorian - 1524.5)


def sunset_utc(day: date, lat: float, lon: float) -> datetime | None:
    """Compute sunset for a date and position using the NOAA algorithm.

    The zenith used is 90.833 degrees rather than 90. The extra half degree
    is atmospheric refraction plus the sun's apparent radius: the sun is
    already below the true horizon when you watch it touch the hills.

    :param day: Date to compute sunset for, in the observer's local sense.
    :param lat: Latitude in decimal degrees, north positive.
    :param lon: Longitude in decimal degrees, west negative.
    :returns: Timezone-aware UTC datetime of sunset, or ``None`` if the sun
        does not set that day at that latitude.
    :rtype: datetime | None
    """
    t = (_julian_day(day) - 2451545.0) / 36525.0
    mean_long = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    mean_anom = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    eccentricity = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    anom_rad = math.radians(mean_anom)
    center = (math.sin(anom_rad) * (1.914602 - t * (0.004817 + 0.000014 * t))
              + math.sin(2 * anom_rad) * (0.019993 - 0.000101 * t)
              + math.sin(3 * anom_rad) * 0.000289)
    omega = 125.04 - 1934.136 * t
    app_long = (mean_long + center - 0.00569
                - 0.00478 * math.sin(math.radians(omega)))
    secs = 21.448 - t * (46.8150 + t * (0.00059 - t * 0.001813))
    mean_obliq = 23.0 + (26.0 + secs / 60.0) / 60.0
    obliq = mean_obliq + 0.00256 * math.cos(math.radians(omega))
    decl = math.degrees(math.asin(math.sin(math.radians(obliq))
                                  * math.sin(math.radians(app_long))))
    var_y = math.tan(math.radians(obliq / 2)) ** 2
    eq_time = 4 * math.degrees(
        var_y * math.sin(2 * math.radians(mean_long))
        - 2 * eccentricity * math.sin(anom_rad)
        + 4 * eccentricity * var_y * math.sin(anom_rad)
        * math.cos(2 * math.radians(mean_long))
        - 0.5 * var_y * var_y * math.sin(4 * math.radians(mean_long))
        - 1.25 * eccentricity * eccentricity * math.sin(2 * anom_rad))

    cos_hour_angle = (
        (math.cos(math.radians(90.833))
         / (math.cos(math.radians(lat)) * math.cos(math.radians(decl))))
        - math.tan(math.radians(lat)) * math.tan(math.radians(decl)))
    if not -1.0 <= cos_hour_angle <= 1.0:
        return None
    hour_angle = math.degrees(math.acos(cos_hour_angle))
    minutes = 720 + 4 * (hour_angle - lon) - eq_time
    return (datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            + timedelta(minutes=minutes))

# ----------------------------------------------------------------- record


def log(**fields: Any) -> None:
    """Append one JSON object to the run record.

    :param fields: Arbitrary key/value pairs describing what happened. A
        ``logged_at`` local timestamp is added automatically.
    :returns: Nothing.
    :rtype: None
    """
    fields["logged_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    with RECORD.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(fields) + "\n")


def free_gb(path: Path) -> float:
    """Report free space on the filesystem holding a path.

    :param path: Any path on the filesystem of interest.
    :returns: Free space in gibibytes.
    :rtype: float
    """
    return shutil.disk_usage(path).free / 1024 ** 3


def camera_binary() -> str | None:
    """Find the still-capture binary provided by the OS.

    :returns: ``"rpicam-still"``, ``"libcamera-still"``, or ``None`` if
        neither is on ``PATH``.
    :rtype: str | None
    """
    for name in ("rpicam-still", "libcamera-still"):
        if shutil.which(name):
            return name
    return None


def frame_dates(directory: Path) -> list[date]:
    """List the dates of captured frames in a directory, in order.

    Frames are named ``YYYY-MM-DD.jpg``. Anything that does not parse as a
    date is ignored rather than raising, so stray files do not break a
    status check.

    :param directory: Directory containing frame images.
    :returns: Sorted list of dates for which a frame exists.
    :rtype: list[date]
    """
    found: list[date] = []
    for path in sorted(directory.glob("*.jpg")):
        try:
            found.append(date.fromisoformat(path.stem))
        except ValueError:
            continue
    return found

# ----------------------------------------------------------------- status


def status() -> int:
    """Print a one-screen health report and return an exit code.

    Answers the two questions that fail silently over a long run: has a
    capture been broken for weeks, and does a second copy of the frames
    still exist.

    :returns: Process exit code, always ``0``.
    :rtype: int
    """
    now = datetime.now(TZ)
    print(f"sunlapse status{' ' * 25}{now:%Y-%m-%d %H:%M %Z}\n")

    dates = frame_dates(FRAMES) if FRAMES.exists() else []
    if not dates:
        print("frames        none yet")
    else:
        span = (dates[-1] - dates[0]).days + 1
        print(f"frames        {len(dates)}  ({dates[0]} to {dates[-1]}, "
              f"{span} days spanned)")
        gaps = [(a, b, (b - a).days - 1)
                for a, b in zip(dates, dates[1:]) if (b - a).days > 1]
        if gaps:
            missing = sum(gap[2] for gap in gaps)
            print(f"gaps          {missing} missing day(s) across "
                  f"{len(gaps)} gap(s)")
            for first, second, count in gaps[-3:]:
                print(f"                {count} between {first} and {second}")
        else:
            print("gaps          none")

    today = now.date()
    sunset = sunset_utc(today, LAT, LON)
    if sunset is not None:
        target = (sunset.astimezone(TZ)
                  - timedelta(minutes=MINUTES_BEFORE_SUNSET))
        have_today = (FRAMES / f"{today.isoformat()}.jpg").exists()
        when = "captured" if have_today else (
            "due now" if now >= target else f"due at {target:%H:%M}")
        print(f"today         {when}   "
              f"(sunset {sunset.astimezone(TZ):%H:%M}, "
              f"target {target:%H:%M})")

    rows: list[dict[str, Any]] = []
    if RECORD.exists():
        for line in RECORD.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if rows:
        last = rows[-1]
        late = last.get("late_minutes")
        suffix = f", {late} min after target" if late is not None else ""
        print(f"last record   {last.get('date', '?')}  "
              f"{last.get('status', '?')}{suffix}")
        recent = rows[-14:]
        bad = [row for row in recent if row.get("status") != "ok"]
        print(f"last 14       {len(recent) - len(bad)} ok, {len(bad)} not ok")
        for row in bad[-3:]:
            print(f"                {row.get('date')}  {row.get('status')}  "
                  f"{(row.get('stderr') or '')[:60]}")
    else:
        print("last record   nothing logged yet")

    free = free_gb(FRAMES if FRAMES.exists() else HERE)
    floor_flag = "  <- BELOW FLOOR" if free < MIN_FREE_GB else ""
    print(f"disk          {free:.1f} GB free{floor_flag}")

    only_copy = "  <- the SD card is your only copy"
    if BACKUP_DIR is None:
        print(f"backup        not configured{only_copy}")
    elif not BACKUP_DIR.exists():
        print(f"backup        {BACKUP_DIR} NOT PRESENT{only_copy}")
    else:
        backed_up = frame_dates(BACKUP_DIR)
        if not backed_up:
            print(f"backup        {BACKUP_DIR} is empty{only_copy}")
        else:
            behind = (dates[-1] - backed_up[-1]).days if dates else 0
            stale = "  <- STALE" if behind > BACKUP_STALE_DAYS else ""
            print(f"backup        {len(backed_up)} of {len(dates)} frames, "
                  f"newest {backed_up[-1]}, {behind} day(s) behind{stale}")
    return 0

# ------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    """Run a status check, a dry run, or today's capture.

    :param argv: Argument list, defaulting to :data:`sys.argv`. Recognizes
        ``--status`` and ``--dry-run``.
    :returns: Process exit code. ``0`` on success or when there is nothing
        to do, ``1`` on a refusal or a capture failure.
    :rtype: int
    """
    args = sys.argv if argv is None else argv
    if "--status" in args:
        return status()

    dry_run = "--dry-run" in args
    FRAMES.mkdir(parents=True, exist_ok=True)

    today = datetime.now(TZ).date()
    frame = FRAMES / f"{today.isoformat()}.jpg"
    if frame.exists():
        return 0

    sunset = sunset_utc(today, LAT, LON)
    if sunset is None:
        log(date=today.isoformat(), status="no_sunset")
        return 0

    target = sunset.astimezone(TZ) - timedelta(minutes=MINUTES_BEFORE_SUNSET)
    now = datetime.now(TZ)
    if now < target:
        return 0

    late = round((now - target).total_seconds() / 60)
    free = free_gb(FRAMES)
    camera = camera_binary()

    if dry_run:
        print(f"date      {today}")
        print(f"sunset    {sunset.astimezone(TZ):%H:%M %Z}")
        print(f"target    {target:%H:%M %Z}  "
              f"({MINUTES_BEFORE_SUNSET} min before sunset)")
        print(f"now       {now:%H:%M %Z}  ({late} min past target)")
        print(f"free      {free:.1f} GB")
        print(f"camera    "
              f"{camera or 'NOT FOUND (rpicam-still / libcamera-still)'}")
        if camera:
            print("would run "
                  + " ".join([camera, "-o", str(frame)] + CAMERA_ARGS))
        return 0

    # A full card is a silent failure. The script keeps running, the writes
    # fail, and you find out months later that February does not exist.
    if free < MIN_FREE_GB:
        print(f"REFUSING TO CAPTURE: {free:.2f} GB free, below the "
              f"{MIN_FREE_GB} GB floor.", file=sys.stderr)
        log(date=today.isoformat(), status="disk_full",
            free_gb=round(free, 2),
            sunset=sunset.astimezone(TZ).isoformat(timespec="seconds"))
        return 1

    if camera is None:
        print("No rpicam-still or libcamera-still on PATH.", file=sys.stderr)
        log(date=today.isoformat(), status="no_camera")
        return 1

    result = subprocess.run([camera, "-o", str(frame)] + CAMERA_ARGS,
                            capture_output=True, text=True)
    ok = (result.returncode == 0 and frame.exists()
          and frame.stat().st_size > 0)

    log(date=today.isoformat(),
        status="ok" if ok else "camera_failed",
        sunset=sunset.astimezone(TZ).isoformat(timespec="seconds"),
        target=target.isoformat(timespec="seconds"),
        captured_at=now.isoformat(timespec="seconds"),
        late_minutes=late,
        very_late=late > LATE_TOLERANCE_MIN,
        bytes=frame.stat().st_size if frame.exists() else 0,
        free_gb=round(free, 2),
        frames_total=len(list(FRAMES.glob("*.jpg"))),
        stderr="" if ok else (result.stderr or "").strip()[:300])

    if not ok:
        print(f"Capture failed: {(result.stderr or '').strip()[:300]}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
