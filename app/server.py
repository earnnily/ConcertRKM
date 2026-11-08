"""Rukkoon LINE booking server. Python 3.11+, standard library only."""
import base64, hashlib, hmac, json, logging, os, re, secrets, sqlite3, threading, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from http.cookies import SimpleCookie

ROOT = Path(__file__).resolve().parent
SERVERLESS = os.environ.get('SERVERLESS') == '1'
DATA = Path(os.environ.get('DATA_DIR', str(ROOT / 'data')))
if not SERVERLESS: DATA.mkdir(parents=True, exist_ok=True)
SLIPS = DATA / 'slips'
if not SERVERLESS: SLIPS.mkdir(exist_ok=True)
DB_PATH = DATA / 'bookings.sqlite3'
ZONES = {'vip': {'name':'VIP','price':2000,'capacity':42,'extra':2}, 'special': {'name':'พิเศษ','price':1600,'capacity':14,'extra':1}, 'lawn': {'name':'ลานนั่ง','price':180,'capacity':1000,'extra':0}}
LABELS = {'pending':'รอชำระ','review':'รอตรวจสลิป','confirmed':'ยืนยันแล้ว','expired':'หมดเวลา','rejected':'ไม่อนุมัติ','cancelled':'ยกเลิก'}
LOCK = threading.RLock()

def db():
    if os.environ.get('DATABASE_URL'):
        from cloud import Database
        return Database()
    if SERVERLESS: raise RuntimeError('DATABASE_URL is required on Vercel')
    c = sqlite3.connect(DB_PATH, timeout=15); c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON'); return c

def init():
    if os.environ.get('DATABASE_URL'):
        return # Cloud schema is applied explicitly, never during cold starts.
    with db() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS bookings(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,name TEXT NOT NULL,phone TEXT NOT NULL,zone TEXT NOT NULL,quantity INTEGER NOT NULL,extras INTEGER NOT NULL,total INTEGER NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,slip TEXT,reason TEXT,checked_in REAL);
        CREATE TABLE IF NOT EXISTS booking_tables(booking_id TEXT NOT NULL REFERENCES bookings(id),table_id TEXT NOT NULL,PRIMARY KEY(booking_id,table_id));
        CREATE INDEX IF NOT EXISTS booking_status ON bookings(status,expires);
        CREATE TABLE IF NOT EXISTS sessions(user_id TEXT PRIMARY KEY,body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,body TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'new',attempts INTEGER NOT NULL DEFAULT 0,error TEXT,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT NOT NULL,body TEXT NOT NULL,sent INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,next_at REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS admin_sessions(token_hash TEXT PRIMARY KEY,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,action TEXT NOT NULL,booking_id TEXT,at REAL NOT NULL);
        ''')
        # An interrupted worker can safely resume; events use booking/event idempotency.
        c.execute("UPDATE events SET status='new' WHERE status='processing'")

def settings(c=None):
    if c is None:
        with db() as c: return settings(c)
    return {r[0]:r[1] for r in c.execute('SELECT key,value FROM settings').fetchall()}

def expire(c, now=None):
    c.execute("UPDATE bookings SET status='expired' WHERE status='pending' AND expires<=?", (now or time.time(),))

TABLES = {z: [f'{r}{col}' for r in rows for col in range(1,8)] for z,rows in [('vip','ABCDEF'),('special','GH')]}

def selected_tables(bid, c=None):
    if c is None:
        with db() as c: return selected_tables(bid,c)
    return [r[0] for r in c.execute('SELECT table_id FROM booking_tables WHERE booking_id=? ORDER BY table_id',(bid,))]

def table_inventory(c=None):
    if c is None:
        with db() as c:
            expire(c); return table_inventory(c)
    busy={r['table_id']:dict(r) for r in c.execute("SELECT t.table_id,b.id,b.status FROM booking_tables t JOIN bookings b ON b.id=t.booking_id WHERE b.status IN ('pending','review','confirmed')")}
    return {t:busy.get(t,{'table_id':t,'status':'available'}) for zone in TABLES.values() for t in zone}

def availability(c=None):
    if c is None:
        with db() as c:
            expire(c); return availability(c)
    return {k: {**z,'remaining':z['capacity']-c.execute("SELECT COALESCE(SUM(quantity),0) FROM bookings WHERE zone=? AND status IN ('pending','review','confirmed')",(k,)).fetchone()[0]} for k,z in ZONES.items()}

def create_booking(uid, draft, booking_id=None):
    zone = draft.get('zone'); q=draft.get('quantity'); extra=draft.get('extras',0)
    if zone not in ZONES or type(q) is not int or not 1<=q<=10 or type(extra) is not int or not 0<=extra<=q*ZONES[zone]['extra']:
        raise ValueError('จำนวนหรือโซนไม่ถูกต้อง (สูงสุด 10 โต๊ะ/ใบต่อรายการ)')
    tables=draft.get('tables',[])
    if zone!='lawn' and (not isinstance(tables,list) or len(tables)!=q or len(set(tables))!=q or any(t not in TABLES[zone] for t in tables)):
        raise ValueError('กรุณาเลือกเลขโต๊ะให้ครบ และไม่ซ้ำกัน')
    if zone=='lawn' and tables: raise ValueError('ลานนั่งไม่มีการจัดสรรเลขโต๊ะ')
    # Lawn quantity is always stored as tickets, including table sales (4 tickets/table).
    if zone=='lawn' and draft.get('sales_unit','seat')=='table': q*=4
    name=str(draft.get('name','')).strip(); phone=str(draft.get('phone','')).strip()
    if not 1<=len(name)<=100 or not re.fullmatch(r'0[0-9]{8,9}',phone): raise ValueError('กรุณาระบุชื่อและเบอร์โทรให้ถูกต้อง')
    bid=booking_id or ('RK'+secrets.token_hex(5).upper())
    with LOCK, db() as c:
        c.execute('BEGIN IMMEDIATE'); expire(c)
        old=c.execute('SELECT * FROM bookings WHERE id=?',(bid,)).fetchone()
        if old: return dict(old)
        s=settings(c)
        if s.get('sales_open')!='1' or not all(s.get(k,'').strip() for k in ('bank_name','bank_account','bank_holder')):
            raise ValueError('ยังไม่เปิดรับจอง กรุณาติดต่อแอดมิน @rukkoon')
        if c.execute("SELECT 1 FROM bookings WHERE user_id=? AND status IN ('pending','review')",(uid,)).fetchone():
            raise ValueError('คุณมีรายการรอชำระหรือรอตรวจอยู่แล้ว กด ดูการจอง ก่อนทำรายการใหม่')
        if availability(c)[zone]['remaining']<q: raise ValueError('โควตาคงเหลือไม่พอ กรุณาเลือกจำนวนใหม่')
        if zone!='lawn':
            inventory=table_inventory(c)
            if any(inventory[t]['status']!='available' for t in tables): raise ValueError('โต๊ะที่เลือกถูกจองแล้ว กรุณาเลือกใหม่')
        now=time.time(); total=ZONES[zone]['price']*q+extra*200
        c.execute('INSERT INTO bookings(id,user_id,name,phone,zone,quantity,extras,total,status,created,expires) VALUES(?,?,?,?,?,?,?,?,?,?,?)', (bid,uid,name,phone,zone,q,extra,total,'pending',now,now+900))
        for t in tables: c.execute('INSERT INTO booking_tables VALUES(?,?)',(bid,t))
        return dict(c.execute('SELECT * FROM bookings WHERE id=?',(bid,)).fetchone())

def manual_lawn_sale(quantity, sales_unit, name=''):
    if type(quantity) is not int or quantity<1 or sales_unit not in ('seat','table'): raise ValueError('จำนวนหรือหน่วยขายไม่ถูกต้อง')
    tickets=quantity*(4 if sales_unit=='table' else 1)
    if tickets>1000: raise ValueError('จำนวนเกินความจุ')
    with LOCK, db() as c:
        c.execute('BEGIN IMMEDIATE'); expire(c)
        if availability(c)['lawn']['remaining']<tickets: raise ValueError('โควตาคงเหลือไม่พอ')
        bid='RK'+secrets.token_hex(5).upper(); now=time.time()
        c.execute('INSERT INTO bookings(id,user_id,name,phone,zone,quantity,extras,total,status,created,expires) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(bid,'admin-manual',str(name or 'ขายผ่านแอดมิน')[:100],'','lawn',tickets,0,tickets*180,'confirmed',now,now))
        c.execute('INSERT INTO audit(action,booking_id,at) VALUES(?,?,?)',('manual_lawn_sale',bid,now))
        return bid

def attach_slip(uid, data, mime):
    if len(data)>10*1024*1024 or not ((mime=='image/jpeg' and data.startswith(b'\xff\xd8\xff')) or (mime=='image/png' and data.startswith(b'\x89PNG\r\n\x1a\n'))):
        raise ValueError('รับเฉพาะภาพสลิป JPG/PNG ขนาดไม่เกิน 10 MB')
    with LOCK, db() as c:
        c.execute('BEGIN IMMEDIATE'); expire(c)
        b=c.execute("SELECT * FROM bookings WHERE user_id=? AND status='pending' ORDER BY created DESC LIMIT 1",(uid,)).fetchone()
        if not b: raise ValueError('ไม่มีรายการรอชำระ หรือหมดเวลาแล้ว กรุณากด ดูการจอง')
        filename=secrets.token_hex(20)+('.png' if mime=='image/png' else '.jpg')
        if os.environ.get('SUPABASE_URL'):
            from cloud import storage
            storage(filename, data, mime)
        elif SERVERLESS:
            raise RuntimeError('Supabase Storage is not configured')
        else:
            (SLIPS/filename).write_bytes(data)
        c.execute("UPDATE bookings SET status='review',slip=? WHERE id=? AND status='pending'",(filename,b['id']))
        return b['id']

def notify(c, uid, text):
    c.execute('INSERT INTO outbox(user_id,body) VALUES(?,?)',(uid,json.dumps([{'type':'text','text':text}],ensure_ascii=False)))

def decide(bid, action, reason=''):
    with LOCK, db() as c:
        c.execute('BEGIN IMMEDIATE'); expire(c)
        row=c.execute('SELECT * FROM bookings WHERE id=?',(bid,)).fetchone()
        if not row: raise ValueError('ไม่พบรายการ')
        b=dict(row)
        if action=='checkin':
            if b['status']!='confirmed' or b['checked_in']: raise ValueError('รายการยังไม่ยืนยัน หรือเช็กอินแล้ว')
            c.execute('UPDATE bookings SET checked_in=? WHERE id=?',(time.time(),bid))
        else:
            if b['status']!='review': raise ValueError('ดำเนินการได้เฉพาะรายการรอตรวจสลิป')
            if action not in ('approve','reject'): raise ValueError('คำสั่งไม่ถูกต้อง')
            if action=='reject' and not reason.strip(): raise ValueError('กรุณาระบุเหตุผลที่ไม่อนุมัติ')
            status='confirmed' if action=='approve' else 'rejected'
            c.execute('UPDATE bookings SET status=?,reason=? WHERE id=?',(status,reason[:500],bid))
            text=(f"ยืนยันการจอง {bid} แล้วค่ะ 🎉\nโต๊ะ {', '.join(selected_tables(bid,c)) if b['zone']!='lawn' else 'ลานนั่ง เลือกโต๊ะหน้างาน'}\nโซน {ZONES[b['zone']]['name']} จำนวน {b['quantity']} {'ใบ' if b['zone']=='lawn' else 'โต๊ะ'}\nที่นั่งเสริม {b['extras']} คน\nยอด {b['total']:,} บาท\n20 พ.ย. 2569 ประตูเปิด 17.00 น.\nแสดงรหัสนี้พร้อมชื่อและเบอร์โทรที่จุดเข้างาน" if action=='approve' else f'รายการ {bid} ไม่ผ่านการตรวจ: {reason}\nกรุณาติดต่อแอดมินก่อนจองหรือโอนซ้ำ')
            notify(c,b['user_id'],text)
        c.execute('INSERT INTO audit(action,booking_id,at) VALUES(?,?,?)',(action,bid,time.time()))

def line_request(path, body=None, method=None, retry_key=None):
    token=os.environ.get('LINE_CHANNEL_ACCESS_TOKEN','')
    if not token: raise RuntimeError('LINE token not configured')
    raw=json.dumps(body,ensure_ascii=False).encode() if body is not None else None
    host='https://api-data.line.me' if '/content' in path else 'https://api.line.me'
    headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}
    if retry_key: headers['X-Line-Retry-Key']=retry_key
    req=urllib.request.Request(host+path,data=raw,headers=headers,method=method)
    return urllib.request.urlopen(req,timeout=12)

def text_message(text, options=None):
    msg={'type':'text','text':text}
    if options: msg['quickReply']={'items':[{'type':'action','action':{'type':'postback','label':label[:20],'data':value,'displayText':label}} for label,value in options[:13]]}
    return msg

def session(uid):
    with db() as c:
        r=c.execute('SELECT body FROM sessions WHERE user_id=?',(uid,)).fetchone(); return json.loads(r[0]) if r else {}

def save_session(uid, body):
    with db() as c: c.execute('INSERT INTO sessions VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET body=excluded.body',(uid,json.dumps(body,ensure_ascii=False)))

def booking_summary(b):
    return ('โต๊ะ '+', '.join(selected_tables(b['id']))+'\n' if b['zone']!='lawn' else '') + f"รหัส {b['id']} • {LABELS[b['status']]}\nโซน {ZONES[b['zone']]['name']} {b['quantity']} {'ใบ' if b['zone']=='lawn' else 'โต๊ะ'} + เสริม {b['extras']} คน\nยอดรวม {b['total']:,} บาท"

def table_picker(st):
    row=st['row']; rows='ABCDEF' if st['zone']=='vip' else 'GH'; inv=table_inventory(); chosen=st['tables']
    options=[(('✓ ' if t in chosen else '')+t,'table:'+t) for t in TABLES[st['zone']] if t.startswith(row) and (inv[t]['status']=='available' or t in chosen)]
    idx=rows.index(row)
    options += [('แถวก่อนหน้า','row:'+rows[(idx-1)%len(rows)]),('แถวถัดไป','row:'+rows[(idx+1)%len(rows)]),('เลือกครบแล้ว','tables_done')]
    return text_message('เลือกเลขโต๊ะ แถว '+row+' (แตะซ้ำเพื่อเอาออก)\nเลือกแล้ว: '+(', '.join(chosen) or 'ยังไม่เลือก')+'\nกันโต๊ะเมื่อยืนยันรายการเท่านั้น',options)

def handle_event(e):
    uid=e.get('source',{}).get('userId')
    if not uid: return []
    if e.get('type')=='unfollow': return []
    message=e.get('message',{}); kind=message.get('type'); value=e.get('postback',{}).get('data') or message.get('text','').strip()
    st=session(uid); stage=st.get('stage')
    if kind=='image':
        try:
            mid=message.get('id','')
            if not re.fullmatch(r'[0-9]+',mid): raise ValueError('ไม่พบรหัสภาพ')
            with line_request('/v2/bot/message/'+mid+'/content') as response:
                data=response.read(10*1024*1024+1); mime=response.headers.get_content_type()
            bid=attach_slip(uid,data,mime)
            return [text_message(f'รับสลิปสำหรับ {bid} แล้วค่ะ กำลังรอแอดมินตรวจสอบ ยังไม่ถือว่ายืนยันการจอง',['dummy'] if False else [('ดูการจอง','my')])]
        except ValueError as ex: return [text_message(str(ex))]
    if value in ('my','ดูการจอง','การจองของฉัน'):
        with db() as c:
            expire(c); rows=c.execute('SELECT * FROM bookings WHERE user_id=? ORDER BY created DESC LIMIT 3',(uid,)).fetchall()
        return [text_message('\n\n'.join(booking_summary(dict(b)) for b in rows) or 'ยังไม่มีรายการจอง', [('จองบัตร','start'),('ติดต่อแอดมิน','help')])]
    if value in ('help','ติดต่อแอดมิน'):
        return [text_message('ติดต่อทีมตลาดรักคุณผ่านแชตนี้ได้เลยค่ะ พิมพ์คำถามไว้ให้แอดมินตอบได้เลย')]
    if value in ('cancel','ยกเลิกรายการ'):
        with db() as c:
            c.execute("UPDATE bookings SET status='cancelled' WHERE user_id=? AND status='pending'",(uid,))
        save_session(uid,{})
        return [text_message('ยกเลิกรายการรอชำระแล้ว หากโอนเงินแล้วกรุณาติดต่อแอดมิน',[('เริ่มจองใหม่','start')])]
    if value in ('start','จอง','จองบัตร','จองคอนเสิร์ต') or e.get('type')=='follow':
        save_session(uid,{'stage':'zone'})
        av=availability()
        return [text_message('🎤 ไหมไทย • ตลาดรักคุณ\n20 พ.ย. 2569 ประตูเปิด 17.00 น.\nเลือกโซนที่ต้องการจอง\nVIP 2,000 บาท/โต๊ะ (4 คน)\nพิเศษ 1,600 บาท/โต๊ะ (4 คน)\nลานนั่ง 180 บาท/ใบ\nเสริมคนละ 200 บาท',[(f"{z['name']} เหลือ {av[k]['remaining']}", 'zone:'+k) for k,z in ZONES.items()])]
    if value.startswith('zone:'):
        z=value.split(':')[1]
        if z not in ZONES: return []
        st={'stage':'tables' if z!='lawn' else 'lawnmode','zone':z,'tables':[],'row':'A' if z=='vip' else 'G'}
        save_session(uid,st)
        if z!='lawn': return [table_picker(st)]
        return [text_message('ลานนั่ง เลือกซื้อเป็นบัตรหรือโต๊ะ (1 โต๊ะ = 4 บัตร ใช้โควตาเดียวกัน)',[('รายบัตร 180 บาท','lawnmode:seat'),('รายโต๊ะ 720 บาท','lawnmode:table')])]
    if value.startswith('lawnmode:') and st.get('zone')=='lawn':
        mode=value.split(':')[1]
        if mode not in ('seat','table'): return []
        st.update(stage='quantity',sales_unit=mode); save_session(uid,st)
        return [text_message('เลือกจำนวน'+('โต๊ะ' if mode=='table' else 'บัตร'),[(str(i),'qty:'+str(i)) for i in range(1,11)])]
    if stage=='tables' and value.startswith('row:'):
        row=value.split(':')[1]
        if row not in ('ABCDEF' if st['zone']=='vip' else 'GH'): return []
        st['row']=row; save_session(uid,st); return [table_picker(st)]
    if stage=='tables' and value.startswith('table:'):
        t=value.split(':')[1]
        if t not in TABLES[st['zone']]: return []
        if t in st['tables']: st['tables'].remove(t)
        elif len(st['tables'])<10 and table_inventory()[t]['status']=='available': st['tables'].append(t)
        save_session(uid,st); return [table_picker(st)]
    if stage=='tables' and value=='tables_done':
        if not st['tables']: return [table_picker(st)]
        st['quantity']=len(st['tables']); st.update(extras=0,stage='extras'); save_session(uid,st)
        return [text_message('เลือกโต๊ะ '+', '.join(st['tables'])+'\nพิมพ์จำนวนที่นั่งเสริมรวม คนละ 200 บาท (สูงสุด '+str(st['quantity']*ZONES[st['zone']]['extra'])+' คน)',[(str(i),'extra:'+str(i)) for i in range(min(12,st['quantity']*ZONES[st['zone']]['extra'])+1)])]
    if value.startswith('qty:') and stage=='quantity':
        try: q=int(value.split(':')[1])
        except ValueError: return []
        if not 1<=q<=10: return []
        st['quantity']=q; st['extras']=0
        if st['zone']=='lawn': st['stage']='name'; out=text_message('พิมพ์ชื่อผู้จองค่ะ')
        else:
            st['stage']='extras'; out=text_message('ต้องการเสริมที่นั่งกี่คน? คนละ 200 บาท\nVIP สูงสุด 2 คน/โต๊ะ • พิเศษสูงสุด 1 คน/โต๊ะ\nพิมพ์จำนวนรวม หรือกดปุ่มด้านล่าง', [(str(i),'extra:'+str(i)) for i in range(min(12,q*ZONES[st['zone']]['extra'])+1)])
        save_session(uid,st); return [out]
    if stage=='extras':
        try: n=int(value.removeprefix('extra:'))
        except ValueError: return [text_message('กรุณาระบุจำนวนที่นั่งเสริมเป็นตัวเลข')]
        if not 0<=n<=st['quantity']*ZONES[st['zone']]['extra']: return [text_message('จำนวนที่นั่งเสริมเกินจำนวนที่กำหนดค่ะ')]
        st.update(extras=n,stage='name'); save_session(uid,st); return [text_message('พิมพ์ชื่อผู้จองค่ะ')]
    if stage=='name' and kind=='text':
        if not 1<=len(value)<=100: return [text_message('กรุณาพิมพ์ชื่อไม่เกิน 100 ตัวอักษร')]
        st.update(name=value,stage='phone'); save_session(uid,st); return [text_message('พิมพ์เบอร์โทรผู้จอง (ตัวเลข 9–10 หลัก ขึ้นต้นด้วย 0)')]
    if stage=='phone' and kind=='text':
        value=re.sub(r'[\s-]','',value)
        if not re.fullmatch(r'0[0-9]{8,9}',value): return [text_message('เบอร์โทรไม่ถูกต้อง กรุณาพิมพ์ใหม่ค่ะ')]
        st.update(phone=value,stage='confirm'); save_session(uid,st)
        total=ZONES[st['zone']]['price']*st['quantity']*(4 if st.get('sales_unit')=='table' and st['zone']=='lawn' else 1)+200*st['extras']
        return [text_message(f"ตรวจสอบรายการ โต๊ะ {', '.join(st.get('tables',[])) if st['zone']!='lawn' else ('ลานนั่ง '+('รายโต๊ะ (4 บัตร/โต๊ะ)' if st.get('sales_unit')=='table' else 'รายบัตร'))}\nชื่อ {st['name']} โทร {st['phone']}\nโซน {ZONES[st['zone']]['name']} จำนวน {st['quantity']}\nเสริม {st['extras']} คน\nยอด {total:,} บาท\nเมื่อยืนยัน ระบบกันโควตา 15 นาที\nส่งสลิปในแชตนี้ภายในเวลาที่กำหนด\nลานนั่งเลือกโต๊ะหน้างาน มาก่อนได้เลือกก่อน",[('ยืนยันการจอง','confirm'),('เริ่มใหม่','start')])]
    if value=='confirm' and stage=='confirm':
        try:
            event_id=e.get('webhookEventId') or secrets.token_hex(16)
            bid='RK'+hashlib.sha256(event_id.encode()).hexdigest()[:12].upper()
            b=create_booking(uid,st,bid); save_session(uid,{'stage':'payment'})
            s=settings(); deadline=time.strftime('%H:%M',time.gmtime(b['expires']+7*3600))
            return [text_message(booking_summary(b)+f"\nโอนและส่งสลิปก่อน {deadline} น. (เวลาไทย)\nธนาคาร {s.get('bank_name')}\nเลขบัญชี {s.get('bank_account')}\nชื่อบัญชี {s.get('bank_holder')}\nส่งรูปสลิปในแชตนี้ เมื่อส่งทันเวลาจะกันโควตาไว้รอแอดมินตรวจ",[('ดูการจอง','my'),('ยกเลิกรายการ','cancel')])]
        except ValueError as ex: return [text_message(str(ex),[('ดูการจอง','my'),('เริ่มใหม่','start')])]
    return [text_message('ยินดีต้อนรับสู่ตลาดรักคุณค่ะ เลือกเมนูด้านล่างได้เลย',[('จองบัตร','start'),('ดูการจอง','my'),('ติดต่อแอดมิน','help')])]

def signature_ok(raw, signature):
    secret=os.environ.get('LINE_CHANNEL_SECRET','')
    if not secret or not signature: return False
    expected=base64.b64encode(hmac.new(secret.encode(),raw,hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected,signature)

def worker_tick():
    with db() as c:
        expire(c)
        row=c.execute("SELECT * FROM events WHERE status='new' AND attempts<5 ORDER BY created LIMIT 1").fetchone()
        if row: c.execute("UPDATE events SET status='processing',attempts=attempts+1 WHERE id=?",(row['id'],))
    if row:
        try:
            e=json.loads(row['body']); messages=handle_event(e)
            if messages and e.get('replyToken'):
                with line_request('/v2/bot/message/reply',{'replyToken':e['replyToken'],'messages':messages}): pass
            with db() as c: c.execute("UPDATE events SET status='done',error=NULL WHERE id=?",(row['id'],))
        except Exception as ex:
            logging.warning('LINE event processing failed: %s',type(ex).__name__)
            with db() as c: c.execute("UPDATE events SET status=?,error=? WHERE id=?",('failed',type(ex).__name__,row['id']))
    with db() as c: item=c.execute('SELECT * FROM outbox WHERE sent=0 AND next_at<=? AND attempts<10 ORDER BY id LIMIT 1',(time.time(),)).fetchone()
    if item:
        try:
            with line_request('/v2/bot/message/push',{'to':item['user_id'],'messages':json.loads(item['body'])}): pass
            with db() as c: c.execute('UPDATE outbox SET sent=1 WHERE id=?',(item['id'],))
        except Exception:
            with db() as c: c.execute('UPDATE outbox SET attempts=attempts+1,next_at=? WHERE id=?',(time.time()+min(3600,30*2**item['attempts']),item['id']))

def cloud_tick():
    """Drain a bounded batch under a database lease shared by all instances.

    The lease exceeds Vercel's configured 60-second function limit. A later
    invocation can recover work interrupted by a terminated function.
    """
    import uuid
    token=secrets.token_hex(16); started=time.monotonic()
    with db() as c:
        claimed=c.execute('UPDATE worker_lock SET token=?,expires=? WHERE id=1 AND expires<? RETURNING id',
                          (token,time.time()+120,time.time())).fetchone()
        if not claimed: return False
        c.execute("UPDATE events SET status='new' WHERE status='processing'")
    try:
        for _ in range(10):
            if time.monotonic()-started>20: break
            with db() as c:
                expire(c)
                row=c.execute("SELECT * FROM events WHERE status='new' AND attempts<5 ORDER BY created LIMIT 1").fetchone()
                if row: c.execute("UPDATE events SET status='processing',attempts=attempts+1 WHERE id=?",(row['id'],))
            if not row: break
            try:
                e=json.loads(row['body']); messages=handle_event(e)
                if messages and e.get('replyToken'):
                    try:
                        with line_request('/v2/bot/message/reply',{'replyToken':e['replyToken'],'messages':messages}): pass
                    except Exception:
                        # Persist fallback delivery if a reply token expired or LINE is unavailable.
                        uid=e.get('source',{}).get('userId')
                        if uid:
                            with db() as c: c.execute('INSERT INTO outbox(user_id,body) VALUES(?,?)',(uid,json.dumps(messages,ensure_ascii=False)))
                with db() as c: c.execute("UPDATE events SET status='done',error=NULL WHERE id=?",(row['id'],))
            except Exception as ex:
                logging.warning('LINE event failed: %s',type(ex).__name__)
                with db() as c: c.execute("UPDATE events SET status='failed',error=? WHERE id=?",(type(ex).__name__,row['id']))
        for _ in range(2):
            if time.monotonic()-started>35: break
            with db() as c: item=c.execute('SELECT * FROM outbox WHERE sent=0 AND next_at<=? AND attempts<10 ORDER BY id LIMIT 1',(time.time(),)).fetchone()
            if not item: break
            # Stable key makes retries safe after a successful send followed by a crash.
            retry=str(uuid.uuid5(uuid.NAMESPACE_URL,os.environ.get('SUPABASE_URL','')+'/outbox/'+str(item['id'])))
            sent=False
            try:
                with line_request('/v2/bot/message/push',{'to':item['user_id'],'messages':json.loads(item['body'])},retry_key=retry): pass
                sent=True
            except urllib.error.HTTPError as ex:
                sent=ex.code==409 and bool(ex.headers.get('x-line-accepted-request-id'))
            except Exception: pass
            with db() as c:
                if sent: c.execute('UPDATE outbox SET sent=1 WHERE id=?',(item['id'],))
                else: c.execute('UPDATE outbox SET attempts=attempts+1,next_at=? WHERE id=?',(time.time()+min(3600,30*2**item['attempts']),item['id']))
        return True
    finally:
        with db() as c: c.execute('UPDATE worker_lock SET expires=0,token=NULL WHERE id=1 AND token=?',(token,))

def worker():
    while True:
        try: worker_tick()
        except Exception: logging.exception('Background worker failure')
        time.sleep(.5)

LOGIN_ATTEMPTS={}
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass # avoid personal details and credentials in access logs
    def reply(self,status,body,ctype='application/json; charset=utf-8',headers=None):
        if not isinstance(body,bytes): body=json.dumps(body,ensure_ascii=False).encode() if ctype.startswith('application/json') else body.encode()
        self.send_response(status)
        for k,v in {'Content-Type':ctype,'Content-Length':str(len(body)),'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'same-origin','X-Frame-Options':'DENY',**(headers or {})}.items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(body)
    def read_body(self):
        n=int(self.headers.get('Content-Length','0'))
        if n<0 or n>1024*1024: raise ValueError('Request too large')
        return self.rfile.read(n)
    def authorized(self):
        cookie=SimpleCookie(); cookie.load(self.headers.get('Cookie',''))
        token=cookie.get('rk_session'); value=token.value if token else ''
        if not value: return False
        with db() as c: return bool(c.execute('SELECT 1 FROM admin_sessions WHERE token_hash=? AND expires>?',(hashlib.sha256(value.encode()).hexdigest(),time.time())).fetchone())
    def do_GET(self):
        path=self.path.split('?')[0]
        if path=='/api/cron' and SERVERLESS:
            secret=os.environ.get('CRON_SECRET','')
            if not secret or not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+secret):
                return self.reply(401,{'error':'Unauthorized'})
            return self.reply(200,{'ok':True,'processed':cloud_tick()})
        if path=='/health': return self.reply(200,{'ok':True})
        if path in ('/','/admin'):
            return self.reply(200,(ROOT/'index.html').read_bytes(),'text/html; charset=utf-8')
        if path=='/api/status':
            s=settings(); return self.reply(200,{'zones':availability(),'sales_open':s.get('sales_open')=='1','bank_ready':all(s.get(k) for k in ('bank_name','bank_account','bank_holder')),'line_ready':bool(os.environ.get('LINE_CHANNEL_SECRET') and os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')),'tables':table_inventory(),'authenticated':self.authorized(),'mode':'server'})
        if not self.authorized(): return self.reply(401,{'error':'กรุณาเข้าสู่ระบบแอดมิน'})
        if path=='/api/admin':
            with db() as c:
                expire(c); bookings=[dict(r) for r in c.execute('SELECT * FROM bookings ORDER BY created DESC')]
                for b in bookings: b['tables']=selected_tables(b['id'],c)
                errors=c.execute("SELECT COUNT(*) FROM events WHERE status='failed'").fetchone()[0]
                unsent=c.execute('SELECT COUNT(*) FROM outbox WHERE sent=0').fetchone()[0]
            return self.reply(200,{'bookings':bookings,'settings':settings(),'zones':availability(),'tables':table_inventory(),'failed_events':errors,'unsent_messages':unsent})
        if path.startswith('/slips/'):
            filename=path.split('/')[-1]
            if not re.fullmatch(r'[a-f0-9]{40}\.(png|jpg)',filename): return self.reply(404,{'error':'Not found'})
            if os.environ.get('SUPABASE_URL'):
                from cloud import signed_slip
                return self.reply(302,b'',headers={'Location':signed_slip(filename)})
            f=SLIPS/filename
            if not f.exists(): return self.reply(404,{'error':'Not found'})
            return self.reply(200,f.read_bytes(),'image/png' if filename.endswith('.png') else 'image/jpeg')
        self.reply(404,{'error':'Not found'})
    def do_POST(self):
        try: self.post()
        except (ValueError,json.JSONDecodeError) as ex: self.reply(400,{'error':str(ex)})
        except Exception:
            logging.exception('Request failed'); self.reply(500,{'error':'ระบบขัดข้อง กรุณาลองใหม่หรือติดต่อแอดมิน'})
    def post(self):
        path=self.path.split('?')[0]; raw=self.read_body()
        if path=='/webhook/line':
            if not signature_ok(raw,self.headers.get('X-Line-Signature','')): return self.reply(401,{'error':'Invalid signature'})
            body=json.loads(raw)
            with db() as c:
                for e in body.get('events',[]):
                    eid=e.get('webhookEventId')
                    if eid: c.execute('INSERT OR IGNORE INTO events(id,body,created) VALUES(?,?,?)',(eid,json.dumps(e,ensure_ascii=False),time.time()))
            if SERVERLESS: cloud_tick()
            return self.reply(200,{'ok':True})
        # Browser mutations require same origin; JSON-only bodies block form CSRF.
        if not self.headers.get('Content-Type','').startswith('application/json'): return self.reply(415,{'error':'JSON required'})
        if self.headers.get('Sec-Fetch-Site')=='cross-site': return self.reply(403,{'error':'Cross-site request blocked'})
        body=json.loads(raw or b'{}')
        if path=='/api/login':
            key=self.client_address[0]; now=time.time()
            if SERVERLESS:
                # Vercel instances do not share memory. Persist throttling in Postgres.
                key=hashlib.sha256(self.headers.get('X-Vercel-Forwarded-For',key).encode()).hexdigest()
                with db() as c:
                    c.execute('BEGIN IMMEDIATE')
                    c.execute('DELETE FROM login_attempts WHERE at<?',(now-600,))
                    count=c.execute('SELECT COUNT(*) FROM login_attempts WHERE client_hash=?',(key,)).fetchone()[0]
                    if count>=10: return self.reply(429,{'error':'ลองใหม่ใน 10 นาที'})
                    c.execute('INSERT INTO login_attempts VALUES(?,?)',(key,now))
            with LOCK:
                attempts=[t for t in LOGIN_ATTEMPTS.get(key,[]) if t>now-600]; LOGIN_ATTEMPTS[key]=attempts
                if len(attempts)>=10: return self.reply(429,{'error':'ลองใหม่ใน 10 นาที'})
                expected=os.environ.get('ADMIN_PASSWORD','')
                if len(expected)<16: return self.reply(503,{'error':'ต้องตั้ง ADMIN_PASSWORD อย่างน้อย 16 ตัวอักษรก่อนใช้งาน'})
                if not hmac.compare_digest(str(body.get('password','')).encode(),expected.encode()):
                    attempts.append(now); return self.reply(401,{'error':'รหัสผ่านไม่ถูกต้อง'})
                LOGIN_ATTEMPTS.pop(key,None)
            token=secrets.token_urlsafe(32)
            with db() as c: c.execute('INSERT INTO admin_sessions VALUES(?,?)',(hashlib.sha256(token.encode()).hexdigest(),now+8*3600))
            secure='' if os.environ.get('ALLOW_INSECURE_LOCALHOST')=='1' else '; Secure'
            return self.reply(200,{'ok':True},headers={'Set-Cookie':f'rk_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800'+secure})
        if not self.authorized(): return self.reply(401,{'error':'กรุณาเข้าสู่ระบบแอดมิน'})
        if path=='/api/logout':
            cookie=SimpleCookie(); cookie.load(self.headers.get('Cookie','')); token=cookie.get('rk_session')
            if token:
                with db() as c: c.execute('DELETE FROM admin_sessions WHERE token_hash=?',(hashlib.sha256(token.value.encode()).hexdigest(),))
            return self.reply(200,{'ok':True},headers={'Set-Cookie':'rk_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'})
        if path=='/api/lawn-sale':
            bid=manual_lawn_sale(body.get('quantity'),body.get('sales_unit'),body.get('name',''))
            return self.reply(200,{'ok':True,'id':bid})
        if path=='/api/settings':
            allowed=('bank_name','bank_account','bank_holder','sales_open'); current=settings()
            for k in allowed:
                if k in body: current[k]=str(body[k]).strip()[:200]
            if current.get('sales_open')=='1' and (not all(current.get(k) for k in allowed[:3]) or not os.environ.get('LINE_CHANNEL_SECRET') or not os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')):
                raise ValueError('กรุณาตั้งข้อมูลธนาคารและเชื่อม LINE ให้ครบก่อนเปิดรับจอง')
            with db() as c:
                for k in allowed: c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(k,current.get(k,'')))
                c.execute('INSERT INTO audit(action,at) VALUES(?,?)',('settings_updated',time.time()))
            return self.reply(200,{'ok':True})
        if path=='/api/decision':
            decide(str(body.get('id','')),str(body.get('action','')),str(body.get('reason','')))
            if SERVERLESS: cloud_tick()
            return self.reply(200,{'ok':True})
        return self.reply(404,{'error':'Not found'})

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO); init()
    threading.Thread(target=worker,daemon=True).start()
    host=os.environ.get('BIND_HOST','127.0.0.1'); port=int(os.environ.get('PORT','8080'))
    logging.info('Rukkoon server listening on %s:%s',host,port)
    ThreadingHTTPServer((host,port),Handler).serve_forever()
