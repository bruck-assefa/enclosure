from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch
import relay_settings as settings


class RelaySettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'test.db')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE relay_schedules (relay_id TEXT PRIMARY KEY, on_time TEXT, off_time TEXT, mode TEXT, last_updated TEXT)')
            for i in range(1, 5):
                db.execute('INSERT INTO relay_schedules VALUES (?, ?, ?, ?, ?)', (str(i), '07:00', '19:00', 'auto', 'old'))
        settings.migrate(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def rows(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM relay_schedules ORDER BY relay_id')]

    def test_migration_is_idempotent_and_preserves_custom(self):
        settings.save(self.path, '1', {'name': 'UV bulb', 'mode': 'custom', 'on_time': '08:00', 'off_time': '18:00'})
        settings.migrate(self.path)
        self.assertEqual(self.rows()[0]['mode'], 'custom')
        self.assertEqual(self.rows()[0]['name'], 'UV bulb')
        self.assertEqual(self.rows()[1]['name'], 'Relay 2')

    def test_solar_sync_never_overwrites_custom(self):
        settings.save(self.path, '1', {'mode': 'custom', 'on_time': '20:00', 'off_time': '06:00'})
        settings.sync_solar(self.path, {'on_time': '06:52', 'off_time': '19:15'})
        self.assertEqual(self.rows()[0]['on_time'], '20:00')
        self.assertEqual(self.rows()[1]['on_time'], '06:52')

    def test_sun_ignores_client_times_and_preserves_name(self):
        with patch.object(settings, 'daylight', return_value={'on_time': '06:52', 'off_time': '19:15'}):
            row = settings.save(self.path, '2', {'mode': 'sun', 'on_time': '00:00'})
        self.assertEqual(row['on_time'], '06:52')
        self.assertEqual(row['name'], 'Relay 2')

    def test_bad_payloads_do_not_write(self):
        before = self.rows()
        for payload in ({'mode': 'bad'}, {'mode': 'custom', 'on_time': '12:00', 'off_time': '12:00'},
                        {'mode': 'custom', 'on_time': '25:00', 'off_time': '06:00'}, {'mode': 'sun', 'name': ' '}):
            with self.assertRaises(ValueError): settings.save(self.path, '1', payload)
        self.assertEqual(before, self.rows())

    def test_overnight_and_exact_boundaries(self):
        self.assertTrue(settings.is_on('20:00', '06:00', '23:00'))
        self.assertTrue(settings.is_on('20:00', '06:00', '05:59'))
        self.assertFalse(settings.is_on('20:00', '06:00', '06:00'))
        self.assertFalse(settings.is_on('07:00', '07:00', '12:00'))

    def test_daylight_uses_enclosure_date(self):
        sun = settings.daylight(datetime(2026, 9, 16, 12, tzinfo=settings.ZONE))
        self.assertEqual(sun['date'], '2026-09-16')
        self.assertLess(sun['sunrise'], sun['sunset'])

if __name__ == '__main__': unittest.main()
