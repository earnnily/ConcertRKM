"""Cloud queue behavior with a local transactional database, no external calls.

Live Postgres pooling, locking, Storage, and Vercel routing still need integration QA.
"""
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_booking

sys.path.insert(0, str(Path(__file__).parents[1] / 'app'))
from cloud import Row, Database

s = test_booking.s


class CloudTests(unittest.TestCase):
    def setUp(self):
        test_booking.BookingTests.setUp(self)
        with s.db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS worker_lock(id INTEGER PRIMARY KEY,token TEXT,expires REAL NOT NULL DEFAULT 0)')
            c.execute('DELETE FROM worker_lock')
            c.execute('INSERT INTO worker_lock(id) VALUES(1)')

    def add_event(self):
        event={'webhookEventId':'CLOUD1','source':{'userId':'U'},'type':'follow','replyToken':'reply'}
        with s.db() as c:
            c.execute('INSERT INTO events(id,body,created) VALUES(?,?,?)',('CLOUD1',json.dumps(event),time.time()))

    def test_lease_prevents_overlapping_workers(self):
        self.add_event()
        with s.db() as c: c.execute('UPDATE worker_lock SET expires=?',(time.time()+120,))
        with patch.object(s,'handle_event') as handle:
            self.assertFalse(s.cloud_tick())
            handle.assert_not_called()

    def test_reply_failure_queues_push_and_does_not_reprocess(self):
        self.add_event()
        with patch.object(s,'handle_event',return_value=[s.text_message('test')]) as handle, patch.object(s,'line_request',side_effect=TimeoutError):
            self.assertTrue(s.cloud_tick())
            self.assertTrue(s.cloud_tick())
            self.assertEqual(handle.call_count,1)
        with s.db() as c:
            self.assertEqual(c.execute('SELECT status FROM events').fetchone()[0],'done')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],1)
            self.assertGreater(c.execute('SELECT next_at FROM outbox').fetchone()[0],time.time())
            self.assertEqual(c.execute('SELECT expires FROM worker_lock').fetchone()[0],0)

    def test_interrupted_event_is_recovered_after_lease_expires(self):
        self.add_event()
        with s.db() as c: c.execute("UPDATE events SET status='processing'")
        with patch.object(s,'handle_event',return_value=[]) as handle:
            s.cloud_tick()
            self.assertEqual(handle.call_count,1)

    def test_postgres_rows_preserve_named_and_positional_access(self):
        row=Row({'key':'bank_name','value':'TEST'})
        self.assertEqual(row[0],'bank_name')
        self.assertEqual(row['value'],'TEST')
        self.assertEqual(dict(row),{'key':'bank_name','value':'TEST'})
        with patch.object(s,'db') as db:
            db.return_value.__enter__.return_value.execute.return_value.fetchall.return_value=[row]
            self.assertEqual(s.settings(),{'bank_name':'TEST'})

    def test_inventory_lock_is_database_scoped(self):
        from unittest.mock import MagicMock
        database=Database(); database.connection=MagicMock()
        database.execute('BEGIN IMMEDIATE')
        database.connection.execute.assert_called_once_with('SELECT pg_advisory_xact_lock(74002026)')


if __name__=='__main__': unittest.main()
