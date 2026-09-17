"""Persistent relay settings and solar times; no hardware imports."""
from contextlib import closing
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from astral import LocationInfo
from astral.sun import sun

ZONE = ZoneInfo('America/New_York')


def daylight(now=None):
    now = now or datetime.now(ZONE)
    location = LocationInfo('McNair', 'Virginia', 'America/New_York', 38.93, -77.40)
    times = sun(location.observer, date=now.astimezone(ZONE).date(), tzinfo=ZONE)
    return {'date': now.astimezone(ZONE).date().isoformat(), 'timezone': str(ZONE),
            'location': 'McNair, Virginia', 'sunrise': times['sunrise'].timestamp(),
            'sunset': times['sunset'].timestamp(),
            'on_time': times['sunrise'].strftime('%H:%M'),
            'off_time': times['sunset'].strftime('%H:%M')}


def migrate(path):
    with closing(sqlite3.connect(path)) as db, db:
        columns = {r[1] for r in db.execute('PRAGMA table_info(relay_schedules)')}
        if 'name' not in columns:
            db.execute("ALTER TABLE relay_schedules ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        db.execute("UPDATE relay_schedules SET name = 'Relay ' || relay_id WHERE name = ''")
        # Legacy auto rows were all overwritten by the daily solar sync.
        db.execute("UPDATE relay_schedules SET mode = 'sun' WHERE mode = 'auto'")


def sync_solar(path, times=None):
    times = times or daylight()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""UPDATE relay_schedules SET on_time=?, off_time=?,
                   last_updated=CURRENT_TIMESTAMP WHERE mode='sun'
                   AND (on_time != ? OR off_time != ?)""",
                   (times['on_time'], times['off_time'], times['on_time'], times['off_time']))


def save(path, relay_id, payload):
    if relay_id not in ('1', '2', '3', '4'):
        raise ValueError('Unknown relay')
    mode = payload['mode']
    # The debug page's legacy auto payload represents explicitly entered times.
    if mode == 'auto':
        mode = 'custom'
    if mode not in ('sun', 'custom'):
        raise ValueError('Choose sun or custom schedule')
    name = payload.get('name')
    if name is not None:
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or any(ord(c) < 32 for c in name):
            raise ValueError('Name must contain 1–60 printable characters')
        name = name.strip()
    if mode == 'sun':
        times = daylight()
        on, off = times['on_time'], times['off_time']
    else:
        on, off = payload.get('on_time'), payload.get('off_time')
        if any(not isinstance(t, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', t) for t in (on, off)):
            raise ValueError('Times must use HH:MM')
        if on == off:
            raise ValueError('On and off times must differ')
    with closing(sqlite3.connect(path)) as db, db:
        db.row_factory = sqlite3.Row
        result = db.execute('''UPDATE relay_schedules SET name=COALESCE(?, name),
            on_time=?, off_time=?, mode=?, last_updated=CURRENT_TIMESTAMP WHERE relay_id=?''',
            (name, on, off, mode, relay_id))
        if result.rowcount != 1:
            raise ValueError('Relay schedule missing')
        return dict(db.execute('SELECT * FROM relay_schedules WHERE relay_id=?', (relay_id,)).fetchone())


def is_on(on, off, now):
    if on == off:
        return False
    return on <= now < off if on < off else now >= on or now < off
