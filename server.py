#!/usr/bin/env python3
"""公司投票第一版：僅供本機示範，正式登入需串接主網站。"""
import json, sqlite3, os, uuid, re, mimetypes, hashlib
from pathlib import Path
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import integration

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get('VOTE_DB', ROOT / 'data' / 'votes.sqlite3'))
TZ = timezone(timedelta(hours=8))
PEOPLE = [dict(id='A001',name='示範管理員',department='行政部',admin=True),dict(id='E001',name='示範同仁一',department='行政部',admin=False),dict(id='E002',name='示範同仁二',department='業務部',admin=False),dict(id='E003',name='示範同仁三',department='資訊部',admin=False),dict(id='E004',name='示範同仁四',department='業務部',admin=False),dict(id='E005',name='示範同仁五',department='人資部',admin=False),dict(id='E006',name='示範同仁六',department='財務部',admin=False)]
class Problem(Exception):
    def __init__(self,message,status=400): self.message,self.status=message,status

def connect():
    db=sqlite3.connect(DB,timeout=10); db.row_factory=sqlite3.Row; db.execute('PRAGMA foreign_keys=ON'); return db

def get_people(include_inactive=False):
    if not integration.enabled(): return PEOPLE
    with connect() as db: return integration.people(db,include_inactive)

def now(): return datetime.now(TZ)
def stamp(): return now().isoformat()
def status(p):
    if p['state'] in ('draft','cancelled'): return p['state']
    t=now()
    if t < datetime.fromisoformat(p['start']): return 'scheduled'
    return 'ended' if t >= datetime.fromisoformat(p['end']) else 'active'

def init():
    integration.check_config()
    DB.parent.mkdir(parents=True,exist_ok=True)
    with connect() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS polls(id TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS receipts(poll_id TEXT NOT NULL REFERENCES polls(id), person_id TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(poll_id,person_id));
        CREATE TABLE IF NOT EXISTS counts(poll_id TEXT NOT NULL REFERENCES polls(id), option_id TEXT NOT NULL, votes INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(poll_id,option_id));
        CREATE TABLE IF NOT EXISTS named_ballots(poll_id TEXT NOT NULL REFERENCES polls(id), person_id TEXT NOT NULL, choices TEXT NOT NULL, PRIMARY KEY(poll_id,person_id));
        CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY, poll_id TEXT, actor TEXT NOT NULL, action TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS requests(actor TEXT NOT NULL, request_key TEXT NOT NULL, digest TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(actor,request_key));''')
        integration.init(db)

def validate(raw, partial=False):
    title=str(raw.get('title','')).strip(); desc=str(raw.get('description','')).strip()
    if (not partial and not title) or len(title)>100: raise Problem('請填寫投票名稱（最多 100 字）')
    if len(desc)>3000: raise Problem('說明最多 3000 字')
    mode=raw.get('mode'); options=raw.get('options',[])
    if mode not in ('single','multiple'): raise Problem('請選擇投票方式')
    if not isinstance(options,list) or not (0 if partial else 2)<=len(options)<=20: raise Problem('請建立 2 至 20 個選項')
    clean=[]
    for o in options:
        if not isinstance(o,dict): raise Problem('選項格式錯誤')
        text=str(o.get('text','')).strip(); image=str(o.get('image',''))
        if not partial and not text and not image: raise Problem('每個選項需要文字或圖片')
        if len(text)>300: raise Problem('選項文字最多 300 字')
        if image and (len(image)>2800000 or not re.fullmatch(r'data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+',image)): raise Problem('圖片需為 2 MB 以下的 PNG、JPG 或 WebP')
        scope=o.get('audienceMode','all'); depts=o.get('departments',[])
        if scope not in ('all','departments') or not isinstance(depts,list) or not all(isinstance(d,str) for d in depts): raise Problem('選項開放對象設定錯誤')
        if scope=='departments' and ((not partial and not depts) or not set(depts)<={x['department'] for x in get_people()}): raise Problem('請為限定部門的選項勾選有效部門')
        clean.append(dict(id=str(uuid.uuid4()),text=text,image=image,audienceMode=scope,departments=list(dict.fromkeys(depts)) if scope=='departments' else []))
    limit=raw.get('limit',1) if mode=='multiple' else 1
    if type(limit) is not int: raise Problem('複選上限只能填寫整數')
    if not 1<=limit<=max(1,len(clean)): raise Problem('複選上限不可超過選項數量，且至少為 1')
    dates={}
    for key in ('start','end'):
        value=raw.get(key,'')
        if partial and not value:
            dates[key]=''; continue
        try:
            parsed=datetime.fromisoformat(value)
            if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=TZ)
            dates[key]=parsed.isoformat()
        except (ValueError,TypeError): raise Problem('請設定正確的開始與結束時間')
    if not partial and datetime.fromisoformat(dates['end'])<=datetime.fromisoformat(dates['start']): raise Problem('結束時間必須晚於開始時間')
    audience_mode=raw.get('audienceMode','all'); departments=raw.get('departments',[])
    if audience_mode=='all': audience=[x['id'] for x in get_people()]
    elif audience_mode=='departments': audience=[x['id'] for x in get_people() if x['department'] in departments]
    elif audience_mode=='people': audience=list(dict.fromkeys(raw.get('audience',[])))
    else: raise Problem('請選擇投票對象')
    if (not partial and not audience) or not set(audience)<=set(x['id'] for x in get_people()): raise Problem('請指定至少一位有效的投票對象')
    for o in clean:
        o['audience']=[x['id'] for x in get_people() if x['id'] in audience and (o['audienceMode']=='all' or x['department'] in o['departments'])]
        if not partial and not o['audience']: raise Problem('選項「'+(o['text'] or '圖片選項')+'」沒有符合活動對象的可投票人員')
    if not partial and any(not any(uid in o['audience'] for o in clean) for uid in audience): raise Problem('部分活動參與者沒有任何可投選項，請調整選項開放對象')
    if type(raw.get('anonymous')) is not bool: raise Problem('請設定是否匿名')
    return dict(title=title,description=desc,mode=mode,limit=limit,options=clean,start=dates['start'],end=dates['end'],anonymous=raw['anonymous'],audience=audience,audienceMode=audience_mode,departments=departments,wizardStep=max(0,min(3,raw.get('wizardStep',0))) if type(raw.get('wizardStep',0)) is int else 0)

def get_poll(db,id):
    row=db.execute('SELECT body FROM polls WHERE id=?',(id,)).fetchone()
    if not row: raise Problem('找不到投票活動',404)
    return json.loads(row['body'])

def option_audience(p,o):
    return o.get('audience',p['audience'])

def public_poll(db,p,user):
    admin=user['admin']; eligible=user['id'] in p['audience']; voted=bool(db.execute('SELECT 1 FROM receipts WHERE poll_id=? AND person_id=?',(p['id'],user['id'])).fetchone()); s=status(p)
    if not admin and (not eligible or s=='draft'): return None
    result={**p,'revision':p.get('revision',1),'status':s,'eligible':eligible,'voted':voted,'eligibleCount':len(p['audience'])}
    result['options']=[{**o,'canVote':user['id'] in option_audience(p,o),'eligibleCount':len(option_audience(p,o))} for o in p['options'] if admin or user['id'] in option_audience(p,o)]
    for o in result['options']: o.pop('audience',None)
    result.pop('audience',None)
    result['canViewResults']=admin or voted
    if result['canViewResults']:
        result['total']=db.execute('SELECT COUNT(*) FROM receipts WHERE poll_id=?',(p['id'],)).fetchone()[0]
        visible={o['id'] for o in result['options']}
        result['counts']={r['option_id']:r['votes'] for r in db.execute('SELECT * FROM counts WHERE poll_id=?',(p['id'],)) if r['option_id'] in visible}
        voted_people={r['person_id'] for r in db.execute('SELECT person_id FROM receipts WHERE poll_id=?',(p['id'],))}
        result['optionTotals']={o['id']:len(set(option_audience(p,o)) & voted_people) for o in p['options'] if o['id'] in visible}
    if admin:
        result['audience']=p['audience']
        receipts={r['person_id'] for r in db.execute('SELECT person_id FROM receipts WHERE poll_id=?',(p['id'],))}
        result['participants']=[{**x,'voted':x['id'] in receipts} for x in get_people(True) if x['id'] in p['audience']]
        if not p['anonymous']: result['ballots']={r['person_id']:json.loads(r['choices']) for r in db.execute('SELECT * FROM named_ballots WHERE poll_id=?',(p['id'],))}
        result['history']=[dict(r) for r in db.execute('SELECT actor,action,created FROM history WHERE poll_id=? ORDER BY id DESC',(p['id'],))]
    return result

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send_json(self,data,code=200):
        out=json.dumps(data,ensure_ascii=False).encode(); self.send_response(code); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(out)
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/api/integration/config':
            return self.send_json(dict(mode='host' if integration.enabled() else 'demo',hostOrigin=integration.setting('HOST_ORIGIN') if integration.enabled() else ''))
        if path=='/api/state':
            try:
                user=self.user()
                with connect() as db:
                    polls=[public_poll(db,json.loads(r['body']),user) for r in db.execute('SELECT body FROM polls ORDER BY rowid DESC')]
                return self.send_json(dict(user=user,people=get_people() if user['admin'] else [user],polls=[p for p in polls if p],serverTime=stamp(),authMode='host' if integration.enabled() else 'demo'))
            except (Problem,integration.IntegrationError) as e: return self.send_json({'error':e.message},e.status)
        file=ROOT/'public'/('index.html' if path=='/' else path.lstrip('/'))
        if ROOT/'public' not in file.resolve().parents or not file.is_file(): return self.send_json({'error':'找不到頁面'},404)
        self.send_response(200); self.send_header('Content-Type',mimetypes.guess_type(file)[0] or 'application/octet-stream'); self.end_headers(); self.wfile.write(file.read_bytes())
    def user(self):
        if integration.enabled():
            with connect() as db: return integration.resolve_user(db,self.headers)
        uid=self.headers.get('X-Demo-User','E001'); user=next((x for x in get_people() if x['id']==uid),None)
        if not user: raise Problem('示範身分不存在',401)
        return user
    def do_POST(self):
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length>25000000: raise Problem('上傳內容過大',413)
            try: raw=json.loads(self.rfile.read(length))
            except (ValueError,UnicodeDecodeError): raise Problem('資料格式錯誤')
            if not isinstance(raw,dict): raise Problem('資料格式錯誤')
            path=urlparse(self.path).path
            if path.startswith('/api/integration/') or path.startswith('/api/auth/'):
                if not integration.enabled(): raise Problem('主網站串接模式尚未啟用',404)
                with connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if path=='/api/auth/exchange': result=integration.exchange(db,raw)
                    elif path=='/api/auth/logout': result=integration.logout(db,self.headers)
                    elif path in ('/api/integration/roster','/api/integration/sync'):
                        integration.require_service(self.headers)
                        result=integration.sync_roster(db,raw) if path.endswith('/roster') else integration.pull_roster(db)
                    else: raise Problem('不存在的串接操作',404)
                return self.send_json(result)
            user=self.user()
            with connect() as db:
                db.execute('BEGIN IMMEDIATE')
                key=raw.get('requestKey')
                hash_input={k:v for k,v in raw.items() if path!='/api/vote' or k!='choices'}
                digest=hashlib.sha256((path+json.dumps(hash_input,sort_keys=True,ensure_ascii=False)).encode()).hexdigest()
                if key:
                    if not isinstance(key,str) or len(key)>100: raise Problem('操作識別格式錯誤')
                    prior=db.execute('SELECT digest,response FROM requests WHERE actor=? AND request_key=?',(user['id'],key)).fetchone()
                    if prior:
                        if prior['digest']!=digest: raise Problem('此操作已送出，請先確認結果再進行新的修改',409)
                        replayed=json.loads(prior['response'])
                        if path=='/api/vote': replayed['poll']=public_poll(db,get_poll(db,replayed['id']),user)
                        return self.send_json(replayed)
                if path=='/api/save':
                    if not user['admin']: raise Problem('只有管理員可以建立投票',403)
                    p=validate(raw,partial=not raw.get('publish')); old=get_poll(db,raw['id']) if raw.get('id') else None
                    if old and old['state']!='draft': raise Problem('活動發布後不可修改',409)
                    if old and raw.get('revision')!=old.get('revision',1): raise Problem('這份草稿已在其他視窗更新，請先重新載入最新草稿；目前修改尚未覆蓋原資料',409)
                    p.update(revision=old.get('revision',1)+1 if old else 1,id=old['id'] if old else str(uuid.uuid4()),state='published' if raw.get('publish') else 'draft',created=old['created'] if old else stamp(),creator=user['id'])
                    if p['state']=='published' and datetime.fromisoformat(p['end'])<=now(): raise Problem('結束時間已過，請先調整再發布')
                    db.execute('INSERT OR REPLACE INTO polls VALUES(?,?)',(p['id'],json.dumps(p,ensure_ascii=False)))
                    db.execute('DELETE FROM counts WHERE poll_id=?',(p['id'],))
                    db.executemany('INSERT INTO counts VALUES(?,?,0)',[(p['id'],o['id']) for o in p['options']])
                    action='發布活動' if p['state']=='published' else '儲存草稿'
                elif path=='/api/cancel':
                    if not user['admin']: raise Problem('只有管理員可以取消活動',403)
                    p=get_poll(db,raw.get('id'))
                    if p['state']!='published' or status(p)=='ended': raise Problem('只能取消尚未結束的活動',409)
                    p['state']='cancelled'; db.execute('UPDATE polls SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id'])); action='取消活動'
                elif path=='/api/vote':
                    p=get_poll(db,raw.get('id')); choices=raw.get('choices',[])
                    if user['id'] not in p['audience']: raise Problem('你不在此活動的投票對象內',403)
                    if status(p)!='active': raise Problem('目前不在投票期間',409)
                    if not isinstance(choices,list) or not all(isinstance(x,str) for x in choices) or not 1<=len(choices)<=p['limit'] or len(set(choices))!=len(choices) or not set(choices)<={o['id'] for o in p['options'] if user['id'] in option_audience(p,o)}: raise Problem('請依投票規則選擇有效選項')
                    try: db.execute('INSERT INTO receipts VALUES(?,?,?)',(p['id'],user['id'],stamp()))
                    except sqlite3.IntegrityError: raise Problem('你已完成投票，無法重複送出',409)
                    for choice in choices: db.execute('UPDATE counts SET votes=votes+1 WHERE poll_id=? AND option_id=?',(p['id'],choice))
                    if not p['anonymous']: db.execute('INSERT INTO named_ballots VALUES(?,?,?)',(p['id'],user['id'],json.dumps(choices)))
                    # 匿名投票只保留參與憑據與累計數，不建立人員和選項的對應。
                    action='完成投票'
                else: raise Problem('不存在的操作',404)
                db.execute('INSERT INTO history(poll_id,actor,action,created) VALUES(?,?,?,?)',(p['id'],user['id'],action,stamp()))
                response={'ok':True,'id':p['id'],'revision':p.get('revision',1),'poll':public_poll(db,p,user)}
                if key:
                    # 匿名投票重試只保存收件確認，不保存個人選擇指紋或當時各項票數。
                    stored={k:v for k,v in response.items() if path!='/api/vote' or k!='poll'}
                    db.execute('INSERT INTO requests VALUES(?,?,?,?)',(user['id'],key,digest,json.dumps(stored,ensure_ascii=False)))
            self.send_json(response)
        except (Problem,integration.IntegrationError) as e: self.send_json({'error':e.message},e.status)
        except Exception:
            self.send_json({'error':'操作未完成，請稍後再試'},500)

if __name__=='__main__':
    init(); port=int(os.environ.get('PORT','8876')); print(f'Local: http://127.0.0.1:{port}',flush=True); ThreadingHTTPServer(('127.0.0.1',port),Handler).serve_forever()
