#!/usr/bin/env python3
"""
One frame per day, a fixed number of minutes before sunset.

Run it often from cron. It works out whether today's frame is due, takes it
if so, and does nothing if it already has one. That means a reboot, a late
start or a missed quarter hour costs you nothing, which matters for
something meant to run for a year unattended.

    */15 * * * * /usr/bin/python3 /home/USER/sunlapse/capture.py >> /home/USER/sunlapse/cron.log 2>&1

Standard library only. Sunset is computed from latitude, longitude and the
date with the NOAA solar position algorithm. No network, no API key, no
service to outlive the project.
"""

import json
import math
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------- config

LAT = 45.0018                       # Silverton, Oregon
LON = -122.7834                     # west is negative
TZ = ZoneInfo("America/Los_Angeles")

MINUTES_BEFORE_SUNSET = 20

HERE = Path(__file__).resolve().parent
FRAMES = HERE / "frames"
RECORD = HERE / "record.jsonl"

MIN_FREE_GB = 2.0                   # refuse, loudly, below this
LATE_TOLERANCE_MIN = 90             # past this, record it as late

# Where a second copy of the frames lives. A USB stick left plugged into
# the Pi is enough. Set to None to turn the backup check off, but read the
# README first: the SD card is the one component most likely to end this
# project, and a year of sunsets cannot be re-shot.
BACKUP_DIR = Path("/media/backup/sunlapse")
BACKUP_STALE_DAYS = 10              # warn if the copy falls this far behind

# rpicam-still on current Raspberry Pi OS, libcamera-still on older
CAMERA_ARGS = ["--nopreview", "--timeout", "2000", "--width", "3280",
               "--height", "2464", "--quality", "92"]

# ------------------------------------------------------------------- sun

def _julian_day(d):
    y, m = d.year, d.month
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return (math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1))
            + d.day + b - 1524.5)


def sunset_utc(d, lat, lon):
    """Sunset for a date as an aware UTC datetime, or None above the arctic
    circle on a day with no sunset."""
    t = (_julian_day(d) - 2451545.0) / 36525.0
    L0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    M = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    Mr = math.radians(M)
    C = (math.sin(Mr) * (1.914602 - t * (0.004817 + 0.000014 * t))
         + math.sin(2 * Mr) * (0.019993 - 0.000101 * t)
         + math.sin(3 * Mr) * 0.000289)
    omega = 125.04 - 1934.136 * t
    app_long = L0 + C - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    secs = 21.448 - t * (46.8150 + t * (0.00059 - t * 0.001813))
    e0 = 23.0 + (26.0 + secs / 60.0) / 60.0
    ec = e0 + 0.00256 * math.cos(math.radians(omega))
    decl = math.degrees(math.asin(math.sin(math.radians(ec))
                                  * math.sin(math.radians(app_long))))
    y = math.tan(math.radians(ec / 2)) ** 2
    eot = 4 * math.degrees(
        y * math.sin(2 * math.radians(L0))
        - 2 * e * math.sin(Mr)
        + 4 * e * y * math.sin(Mr) * math.cos(2 * math.radians(L0))
        - 0.5 * y * y * math.sin(4 * math.radians(L0))
        - 1.25 * e * e * math.sin(2 * Mr))

    # 90.833 degrees, not 90: the extra half degree is atmospheric
    # refraction plus the sun's apparent radius. The sun is already below
    # the true horizon when you watch it touch the hills.
    cos_ha = ((math.cos(math.radians(90.833))
               / (math.cos(math.radians(lat)) * math.cos(math.radians(decl))))
              - math.tan(math.radians(lat)) * math.tan(math.radians(decl)))
    if not -1 <= cos_ha <= 1:
        return None
    ha = math.degrees(math.acos(cos_ha))
    minutes = 720 + 4 * (ha - lon) - eot
    return (datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
            + timedelta(minutes=minutes))

# ----------------------------------------------------------------- record

def log(**fields):
    fields["logged_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    with RECORD.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(fields) + "\n")


def free_gb(path):
    return shutil.disk_usage(path).free / 1024 ** 3


def camera_binary():
    for name in ("rpicam-still", "libcamera-still"):
        if shutil.which(name):
            return name
    return None

# ----------------------------------------------------------------- status

def frame_dates(d):
    out = []
    for p in sorted(d.glob("*.jpg")):
        try:
            out.append(date.fromisoformat(p.stem))
        except ValueError:
            pass
    return out


def status():
    """One screen that answers: is this still working, and is it backed up."""
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
            missing = sum(g[2] for g in gaps)
            print(f"gaps          {missing} missing day(s) across "
                  f"{len(gaps)} gap(s)")
            for a, b, n in gaps[-3:]:
                print(f"                {n} between {a} and {b}")
        else:
            print("gaps          none")

    today = now.date()
    ss = sunset_utc(today, LAT, LON)
    if ss:
        target = ss.astimezone(TZ) - timedelta(minutes=MINUTES_BEFORE_SUNSET)
        got = (FRAMES / f"{today.isoformat()}.jpg").exists()
        when = "captured" if got else (
            "due now" if now >= target else f"due at {target:%H:%M}")
        print(f"today         {when}   "
              f"(sunset {ss.astimezone(TZ):%H:%M}, "
              f"target {target:%H:%M})")

    # The record is the only thing that can tell you a capture went wrong
    # on a day you were not looking.
    rows = []
    if RECORD.exists():
        for line in RECORD.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if rows:
        last = rows[-1]
        print(f"last record   {last.get('date', '?')}  "
              f"{last.get('status', '?')}"
              + (f", {last['late_minutes']} min after target"
                 if last.get("late_minutes") is not None else ""))
        recent = rows[-14:]
        bad = [r for r in recent if r.get("status") != "ok"]
        print(f"last 14       {len(recent) - len(bad)} ok, {len(bad)} not ok")
        for r in bad[-3:]:
            print(f"                {r.get('date')}  {r.get('status')}  "
                  f"{(r.get('stderr') or '')[:60]}")
    else:
        print("last record   nothing logged yet")

    free = free_gb(FRAMES if FRAMES.exists() else HERE)
    flag = "  <- BELOW FLOOR" if free < MIN_FREE_GB else ""
    print(f"disk          {free:.1f} GB free{flag}")

    if BACKUP_DIR is None:
        print("backup        not configured  <- the SD card is your only copy")
    elif not BACKUP_DIR.exists():
        print(f"backup        {BACKUP_DIR} NOT PRESENT  "
              f"<- the SD card is your only copy")
    else:
        bdates = frame_dates(BACKUP_DIR)
        if not bdates:
            print(f"backup        {BACKUP_DIR} is empty  "
                  f"<- the SD card is your only copy")
        else:
            behind = (dates[-1] - bdates[-1]).days if dates else 0
            flag = ("  <- STALE" if behind > BACKUP_STALE_DAYS else "")
            print(f"backup        {len(bdates)} of {len(dates)} frames, "
                  f"newest {bdates[-1]}, {behind} day(s) behind{flag}")
    return 0

# ------------------------------------------------------------------- main

def main():
    if "--status" in sys.argv:
        return status()

    dry = "--dry-run" in sys.argv
    FRAMES.mkdir(parents=True, exist_ok=True)

    today = datetime.now(TZ).date()
    frame = FRAMES / f"{today.isoformat()}.jpg"
    if frame.exists():
        return 0                                   # already have today

    ss = sunset_utc(today, LAT, LON)
    if ss is None:
        log(date=today.isoformat(), status="no_sunset")
        return 0
    target = ss.astimezone(TZ) - timedelta(minutes=MINUTES_BEFORE_SUNSET)
    now = datetime.now(TZ)
    if now < target:
        return 0                                   # not yet

    late = round((now - target).total_seconds() / 60)

    # A full card is a silent failure. The script keeps running, the writes
    # fail, and you find out months later that February does not exist.
    free = free_gb(FRAMES)
    if free < MIN_FREE_GB:
        msg = (f"REFUSING TO CAPTURE: {free:.2f} GB free, "
               f"below the {MIN_FREE_GB} GB floor. Nothing has been "
               f"written since this started.")
        print(msg, file=sys.stderr)
        log(date=today.isoformat(), status="disk_full",
            free_gb=round(free, 2), sunset=ss.astimezone(TZ).isoformat())
        return 1

    cam = camera_binary()

    if dry:
        print(f"date      {today}")
        print(f"sunset    {ss.astimezone(TZ):%H:%M %Z}")
        print(f"target    {target:%H:%M %Z}  "
              f"({MINUTES_BEFORE_SUNSET} min before sunset)")
        print(f"now       {now:%H:%M %Z}  ({late} min past target)")
        print(f"free      {free:.1f} GB")
        print(f"camera    {cam or 'NOT FOUND (rpicam-still / libcamera-still)'}")
        if cam:
            print("would run " + " ".join([cam, "-o", str(frame)] + CAMERA_ARGS))
        return 0

    if cam is None:
        print("No rpicam-still or libcamera-still on PATH.", file=sys.stderr)
        log(date=today.isoformat(), status="no_camera")
        return 1

    cmd = [cam, "-o", str(frame)] + CAMERA_ARGS

    res = subprocess.run(cmd, capture_output=True, text=True)
    ok = res.returncode == 0 and frame.exists() and frame.stat().st_size > 0

    log(date=today.isoformat(),
        status="ok" if ok else "camera_failed",
        sunset=ss.astimezone(TZ).isoformat(timespec="seconds"),
        target=target.isoformat(timespec="seconds"),
        captured_at=now.isoformat(timespec="seconds"),
        late_minutes=late,
        very_late=late > LATE_TOLERANCE_MIN,
        bytes=frame.stat().st_size if frame.exists() else 0,
        free_gb=round(free, 2),
        frames_total=len(list(FRAMES.glob("*.jpg"))),
        stderr=(res.stderr or "").strip()[:300] if not ok else "")

    if not ok:
        print(f"Capture failed: {res.stderr.strip()[:300]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
