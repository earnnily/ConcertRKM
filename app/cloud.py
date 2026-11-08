"""Postgres and private Storage support for Vercel deployments."""
import os
import json
import urllib.request
import urllib.error
from urllib.parse import quote


class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class Result:
    def __init__(self, cursor):
        self.cursor = cursor

    def fetchone(self):
        row = self.cursor.fetchone()
        return Row(row) if row is not None else None

    def fetchall(self):
        return [Row(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class Database:
    """Small adapter retaining the existing SQL call sites and row access.

    BEGIN IMMEDIATE becomes a transaction-scoped database lock. Unlike the
    process-local lock, this serializes inventory writes across Vercel instances.
    """
    def __enter__(self):
        import psycopg
        from psycopg.rows import dict_row
        self.connection = psycopg.connect(
            os.environ['DATABASE_URL'], row_factory=dict_row,
            prepare_threshold=None, connect_timeout=10,
        )
        # Transaction-scoped settings are safe with Supavisor transaction pooling.
        self.connection.execute('SET LOCAL search_path TO booking, public')
        self.connection.execute("SET LOCAL statement_timeout = '20s'")
        return self

    def execute(self, sql, params=()):
        if sql == 'BEGIN IMMEDIATE':
            return Result(self.connection.execute('SELECT pg_advisory_xact_lock(74002026)'))
        if sql.startswith('INSERT OR IGNORE INTO '):
            sql = sql.replace('INSERT OR IGNORE INTO ', 'INSERT INTO ', 1) + ' ON CONFLICT DO NOTHING'
        return Result(self.connection.execute(sql.replace('?', '%s'), params))

    def __exit__(self, kind, value, traceback):
        try:
            if kind is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()


def storage(filename, data=None, mime=None):
    base = os.environ['SUPABASE_URL'].rstrip('/')
    key = os.environ['SUPABASE_SERVICE_ROLE_KEY']
    bucket = os.environ.get('SUPABASE_SLIPS_BUCKET', 'booking-slips')
    url = base + '/storage/v1/object/' + quote(bucket, safe='') + '/' + quote(filename, safe='')
    headers = {'Authorization': 'Bearer ' + key, 'apikey': key}
    if data is not None:
        headers['Content-Type'] = mime
    request = urllib.request.Request(url, data=data, headers=headers, method='POST' if data is not None else 'GET')
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def signed_slip(filename):
    """Short-lived redirect avoids Vercel's response-size limit for large slips."""
    base = os.environ['SUPABASE_URL'].rstrip('/')
    key = os.environ['SUPABASE_SERVICE_ROLE_KEY']
    bucket = os.environ.get('SUPABASE_SLIPS_BUCKET', 'booking-slips')
    path = '/storage/v1/object/sign/' + quote(bucket, safe='') + '/' + quote(filename, safe='')
    request = urllib.request.Request(base + path, data=json.dumps({'expiresIn':60}).encode(),
        headers={'Authorization':'Bearer '+key,'apikey':key,'Content-Type':'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=10) as response:
        signed = json.load(response)['signedURL']
    return base + '/storage/v1' + signed
