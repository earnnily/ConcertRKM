-- Run once in the Supabase SQL editor. Fresh databases only.
-- Private schema is deliberately not exposed through the Supabase Data API.
CREATE SCHEMA IF NOT EXISTS booking;
REVOKE ALL ON SCHEMA booking FROM PUBLIC, anon, authenticated;
SET search_path = booking, public;
CREATE TABLE IF NOT EXISTS bookings(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,name TEXT NOT NULL,phone TEXT NOT NULL,zone TEXT NOT NULL,quantity INTEGER NOT NULL,extras INTEGER NOT NULL,total INTEGER NOT NULL,status TEXT NOT NULL,created DOUBLE PRECISION NOT NULL,expires DOUBLE PRECISION NOT NULL,slip TEXT,reason TEXT,checked_in DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS booking_tables(booking_id TEXT NOT NULL REFERENCES bookings(id),table_id TEXT NOT NULL,PRIMARY KEY(booking_id,table_id));
CREATE INDEX IF NOT EXISTS booking_status ON bookings(status,expires);
CREATE TABLE IF NOT EXISTS sessions(user_id TEXT PRIMARY KEY,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,body TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'new',attempts INTEGER NOT NULL DEFAULT 0,error TEXT,created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(id BIGSERIAL PRIMARY KEY,user_id TEXT NOT NULL,body TEXT NOT NULL,sent INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,next_at DOUBLE PRECISION NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS admin_sessions(token_hash TEXT PRIMARY KEY,expires DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id BIGSERIAL PRIMARY KEY,action TEXT NOT NULL,booking_id TEXT,at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts(client_hash TEXT NOT NULL,at DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS login_attempts_client ON login_attempts(client_hash,at);
CREATE TABLE IF NOT EXISTS worker_lock(id INTEGER PRIMARY KEY CHECK(id=1),token TEXT,expires DOUBLE PRECISION NOT NULL DEFAULT 0);
INSERT INTO worker_lock(id) VALUES(1) ON CONFLICT DO NOTHING;
REVOKE ALL ON ALL TABLES IN SCHEMA booking FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA booking REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated;
-- Storage remains private; only server-side credentials can read slips.
INSERT INTO storage.buckets(id,name,public,file_size_limit,allowed_mime_types)
VALUES('booking-slips','booking-slips',false,10485760,ARRAY['image/jpeg','image/png'])
ON CONFLICT(id) DO NOTHING;
