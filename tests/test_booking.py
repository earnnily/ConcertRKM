import base64, concurrent.futures, hashlib, hmac, importlib.util, json, os, tempfile, threading, time, unittest, urllib.request, urllib.error
from pathlib import Path
TMP=tempfile.TemporaryDirectory();os.environ['DATA_DIR']=TMP.name
os.environ['ADMIN_PASSWORD']='test-password-123456789';os.environ['ALLOW_INSECURE_LOCALHOST']='1'
spec=importlib.util.spec_from_file_location('server',Path(__file__).parents[1]/'app/server.py'); s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)
class BookingTests(unittest.TestCase):
    def setUp(self):
        s.init()
        with s.db() as c:
            for t in ['booking_tables','bookings','sessions','settings','events','outbox','admin_sessions','audit']: c.execute('DELETE FROM '+t)
            for k,v in {'sales_open':'1','bank_name':'TEST','bank_account':'000','bank_holder':'TEST'}.items(): c.execute('INSERT INTO settings VALUES(?,?)',(k,v))
    def draft(self,z='vip',q=1,e=0): return dict(zone=z,quantity=q,extras=e,name='ผู้ทดสอบ',phone='0800000000',tables=s.TABLES[z][:q] if z!='lawn' and type(q) is int and q>0 else [])
    def test_prices_and_extras(self):
        self.assertEqual(s.create_booking('a',self.draft('vip',2,4))['total'],4800)
        self.assertEqual(s.create_booking('b',self.draft('special',2,2))['total'],3600)
        self.assertEqual(s.create_booking('c',self.draft('lawn',10))['total'],1800)
        for d in [self.draft('special',1,2),self.draft('lawn',1,1),self.draft('vip',0),self.draft('vip',1,-1),self.draft('vip',True)]:
            with self.assertRaises(ValueError):s.create_booking('x',d)
    def test_concurrent_no_overselling(self):
        def reserve(i):
            try:
                draft=self.draft();draft['tables']=[s.TABLES['vip'][i%42]];s.create_booking(str(i),draft);return 1
            except ValueError:return 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool: successes=sum(pool.map(reserve,range(70)))
        self.assertEqual(successes,42);self.assertEqual(s.availability()['vip']['remaining'],0)
    def test_expiry_returns_stock(self):
        b=s.create_booking('a',self.draft())
        with s.db() as c:c.execute('UPDATE bookings SET expires=? WHERE id=?',(time.time()-1,b['id']))
        self.assertEqual(s.availability()['vip']['remaining'],42)
        with self.assertRaises(ValueError):s.attach_slip('a',b'\xff\xd8\xffTEST','image/jpeg')
    def test_review_keeps_stock_and_approval_idempotent(self):
        b=s.create_booking('a',self.draft());s.attach_slip('a',b'\xff\xd8\xffTEST','image/jpeg')
        with s.db() as c:c.execute('UPDATE bookings SET expires=?',(time.time()-1,))
        self.assertEqual(s.availability()['vip']['remaining'],41)
        s.decide(b['id'],'approve')
        with self.assertRaises(ValueError):s.decide(b['id'],'approve')
        with s.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],1)
        s.decide(b['id'],'checkin')
        with self.assertRaises(ValueError):s.decide(b['id'],'checkin')
    def test_reject_returns_stock(self):
        b=s.create_booking('a',self.draft());s.attach_slip('a',b'\xff\xd8\xffTEST','image/jpeg')
        with self.assertRaises(ValueError):s.decide(b['id'],'reject','')
        s.decide(b['id'],'reject','ยอดไม่ตรง');self.assertEqual(s.availability()['vip']['remaining'],42)
    def test_sale_gate_duplicates_and_idempotency(self):
        with s.db() as c:c.execute("UPDATE settings SET value='0' WHERE key='sales_open'")
        with self.assertRaises(ValueError):s.create_booking('a',self.draft())
        with s.db() as c:c.execute("UPDATE settings SET value='1' WHERE key='sales_open'")
        b=s.create_booking('a',self.draft(),'RKTEST');self.assertEqual(s.create_booking('a',self.draft(),'RKTEST')['id'],b['id'])
        with self.assertRaises(ValueError):s.create_booking('a',self.draft())
    def test_same_table_race_and_release(self):
        def reserve(uid):
            try:s.create_booking(uid,self.draft());return 1
            except ValueError:return 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sum(pool.map(reserve,['a','b'])),1)
        self.assertEqual(s.table_inventory()['A1']['status'],'pending')
        with s.db() as c:c.execute('UPDATE bookings SET expires=?',(time.time()-1,))
        self.assertEqual(s.table_inventory()['A1']['status'],'available')
        s.create_booking('c',self.draft())
    def test_lawn_seat_and_table_shared_inventory(self):
        s.manual_lawn_sale(3,'table');s.manual_lawn_sale(5,'seat')
        self.assertEqual(s.availability()['lawn']['remaining'],983)
        d=self.draft('lawn',2);d['sales_unit']='table';b=s.create_booking('a',d)
        self.assertEqual(b['quantity'],8);self.assertEqual(b['total'],1440)
        self.assertEqual(s.availability()['lawn']['remaining'],975)
        with self.assertRaises(ValueError):s.manual_lawn_sale(244,'table')
        self.assertEqual(len(s.TABLES['special']),14)
        self.assertEqual(s.table_inventory()['A4']['status'],'available')

    def test_signature(self):
        os.environ['LINE_CHANNEL_SECRET']='test-secret';raw=b'{"events":[]}'
        sig=base64.b64encode(hmac.new(b'test-secret',raw,hashlib.sha256).digest()).decode()
        self.assertTrue(s.signature_ok(raw,sig));self.assertFalse(s.signature_ok(raw+b' ',sig));self.assertFalse(s.signature_ok(raw,''))
    def test_bot_flow(self):
        def e(value,post=True):
            return s.handle_event({'webhookEventId':'FLOW1','source':{'userId':'U1'},'type':'postback' if post else 'message',**({'postback':{'data':value}} if post else {'message':{'type':'text','text':value}})})
        e('start');e('zone:vip');e('table:A1');e('table:A2');e('tables_done');e('extra:3');e('ทดสอบ',False);e('0800000000',False)
        out=e('confirm');self.assertIn('4,600',out[0]['text'])
        with s.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM bookings').fetchone()[0],1)
    def test_http_auth_webhook_and_dedup(self):
        server=s.ThreadingHTTPServer(('127.0.0.1',0),s.Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
        base='http://127.0.0.1:'+str(server.server_port)
        def req(path,data=None,headers=None):
            r=urllib.request.Request(base+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json',**(headers or {})})
            return urllib.request.urlopen(r)
        try:
            with self.assertRaises(urllib.error.HTTPError) as e:req('/api/admin')
            self.assertEqual(e.exception.code,401)
            with self.assertRaises(urllib.error.HTTPError):req('/api/login',{'password':'wrong'})
            with req('/api/login',{'password':os.environ['ADMIN_PASSWORD']}) as r:cookie=r.headers['Set-Cookie'].split(';')[0]
            with req('/api/admin',headers={'Cookie':cookie}) as r:self.assertIn('bookings',json.load(r))
            os.environ['LINE_CHANNEL_SECRET']='webhook-secret';payload={'events':[{'webhookEventId':'E1','type':'follow','source':{'userId':'U'}}]}
            raw=json.dumps(payload).encode();sig=base64.b64encode(hmac.new(b'webhook-secret',raw,hashlib.sha256).digest()).decode()
            for _ in range(2):
                with req('/webhook/line',payload,{'X-Line-Signature':sig}) as r:self.assertEqual(r.status,200)
            with s.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)
            with self.assertRaises(urllib.error.HTTPError):req('/webhook/line',payload,{'X-Line-Signature':'bad'})
            with req('/api/logout',{},headers={'Cookie':cookie}):pass
            with self.assertRaises(urllib.error.HTTPError):req('/api/admin',headers={'Cookie':cookie})
        finally:server.shutdown();server.server_close()
if __name__=='__main__':unittest.main()
