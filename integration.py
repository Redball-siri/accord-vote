"""主網站串接：伺服器名冊同步、短效簽章登入票券、可撤銷的獨立工作階段。"""
import os, json, base64, hashlib, hmac, secrets, time
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.parse import urlparse

class IntegrationError(Exception):
    def __init__(self,message,status=400): self.message,self.status=message,status

def enabled(): return os.environ.get('ACCORD_AUTH_MODE','demo')=='host'
def setting(key,default=''): return os.environ.get('ACCORD_'+key,default)
def check_config():
    mode=setting('AUTH_MODE','demo')
    if mode not in ('demo','host'): raise RuntimeError('ACCORD_AUTH_MODE 必須為 demo 或 host')
    if not enabled(): return
    for name in ('SSO_SECRET','DIRECTORY_TOKEN'):
        if len(setting(name))<32: raise RuntimeError('ACCORD_'+name+' 需設定至少 32 字元的獨立密鑰')
    if setting('SSO_SECRET')==setting('DIRECTORY_TOKEN'): raise RuntimeError('登入密鑰與名冊密鑰不可相同')
    for name in ('HOST_ORIGIN','SSO_ISSUER'):
        if not setting(name): raise RuntimeError('缺少 ACCORD_'+name)
    origin=urlparse(setting('HOST_ORIGIN'))
    if origin.scheme not in ('http','https') or not origin.netloc or origin.path not in ('','/'): raise RuntimeError('HOST_ORIGIN 必須是完整主網站來源，不含路徑')
    if origin.scheme!='https' and origin.hostname not in ('127.0.0.1','localhost'): raise RuntimeError('主網站必須使用 HTTPS')

def init(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS directory(id TEXT PRIMARY KEY, body TEXT NOT NULL, active INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS directory_state(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, digest TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS auth_tickets(jti TEXT PRIMARY KEY, expires INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS auth_sessions(digest TEXT PRIMARY KEY, person_id TEXT NOT NULL, expires INTEGER NOT NULL);''')

def people(db,include_inactive=False):
    return [json.loads(r['body']) for r in db.execute('SELECT body FROM directory'+('' if include_inactive else ' WHERE active=1')+' ORDER BY id')]

def require_service(headers):
    expected=setting('DIRECTORY_TOKEN')
    if not expected or not hmac.compare_digest(headers.get('Authorization',''),'Bearer '+expected): raise IntegrationError('名冊同步驗證失敗',401)

def sync_roster(db,raw):
    rows=raw.get('employees');revision=raw.get('revision')
    if type(revision) is not int or revision<1 or not isinstance(rows,list) or len(rows)>20000: raise IntegrationError('名冊需提供正整數 revision 與 employees 陣列（最多 20000 人）')
    clean=[];ids=set();numbers=set()
    for item in rows:
        if not isinstance(item,dict): raise IntegrationError('員工資料格式錯誤')
        row={k:item.get(k) for k in ('id','employeeNumber','name','department','admin','active')}
        for key in ('id','employeeNumber','name','department'):
            if not isinstance(row[key],str) or not row[key].strip() or len(row[key])>200: raise IntegrationError('員工 '+key+' 為必填文字，最多 200 字')
            row[key]=row[key].strip()
        if type(row['admin']) is not bool or type(row['active']) is not bool: raise IntegrationError('admin 與 active 必須是布林值')
        if row['id'] in ids or row['employeeNumber'] in numbers: raise IntegrationError('員工 ID 或工號不可重複')
        ids.add(row['id']);numbers.add(row['employeeNumber']);clean.append(row)
    digest=hashlib.sha256(json.dumps(sorted(clean,key=lambda x:x['id']),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    old=db.execute('SELECT revision,digest FROM directory_state WHERE id=1').fetchone()
    if old and (revision<old['revision'] or (revision==old['revision'] and digest!=old['digest'])): raise IntegrationError('名冊版本過舊，請使用較新的 revision',409)
    if old and revision==old['revision']: return dict(ok=True,revision=revision,count=sum(x['active'] for x in clean),unchanged=True)
    # 缺席人員停用但保留舊名冊，避免破壞既有歷史紀錄。
    for existing in people(db,True):
        existing['active']=False
        db.execute('UPDATE directory SET body=?,active=0 WHERE id=?',(json.dumps(existing,ensure_ascii=False),existing['id']))
    for row in clean: db.execute('INSERT OR REPLACE INTO directory VALUES(?,?,?)',(row['id'],json.dumps(row,ensure_ascii=False),int(row['active'])))
    db.execute('INSERT OR REPLACE INTO directory_state VALUES(1,?,?)',(revision,digest))
    db.execute('DELETE FROM auth_sessions WHERE person_id NOT IN (SELECT id FROM directory WHERE active=1)')
    return dict(ok=True,revision=revision,count=sum(x['active'] for x in clean))

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl): return None

def pull_roster(db):
    url=setting('DIRECTORY_URL');parsed=urlparse(url)
    if parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in ('127.0.0.1','localhost')): raise IntegrationError('名冊來源需使用 HTTPS',503)
    try:
        req=Request(url,headers={'Authorization':'Bearer '+setting('DIRECTORY_TOKEN'),'Accept':'application/json'})
        with build_opener(NoRedirect()).open(req,timeout=10) as response:
            if response.geturl()!=url: raise ValueError('redirect not supported')
            content=response.read(10000001)
            if len(content)>10000000: raise ValueError('response too large')
            raw=json.loads(content)
        if not isinstance(raw,dict): raise ValueError('invalid roster')
    except Exception: raise IntegrationError('無法取得主網站名冊，請稍後再試',503)
    return sync_roster(db,raw)

def decode64(value):
    if not isinstance(value,str) or len(value)>16000: raise ValueError()
    return base64.urlsafe_b64decode(value+'='*((-len(value))%4))

def exchange(db,raw):
    token=raw.get('token','')
    try:
        if not isinstance(token,str) or len(token)>16000: raise ValueError()
        header64,body64,signature=token.split('.')
        header=json.loads(decode64(header64));body=json.loads(decode64(body64))
        if header!={'alg':'HS256','typ':'JWT'}: raise ValueError()
        expected=hmac.new(setting('SSO_SECRET').encode(),(header64+'.'+body64).encode(),hashlib.sha256).digest()
        if not hmac.compare_digest(expected,decode64(signature)): raise ValueError()
        t=int(time.time());iat=body.get('iat');exp=body.get('exp')
        if type(iat) is not int or type(exp) is not int or not iat-30<=t<exp or not 0<exp-iat<=300: raise ValueError()
        if body.get('iss')!=setting('SSO_ISSUER') or body.get('aud')!=setting('SSO_AUDIENCE','accord'): raise ValueError()
        if not isinstance(body.get('sub'),str) or not isinstance(body.get('jti'),str) or not 16<=len(body['jti'])<=200: raise ValueError()
    except Exception: raise IntegrationError('主網站登入票券無效或已過期',401)
    if db.execute('SELECT 1 FROM auth_tickets WHERE jti=?',(body['jti'],)).fetchone(): raise IntegrationError('登入票券已使用，請從主網站重新開啟',401)
    if setting('DIRECTORY_URL'): pull_roster(db)
    person=next((p for p in people(db) if p['id']==body['sub']),None)
    if not person: raise IntegrationError('主網站帳號不存在或已停用',403)
    db.execute('INSERT INTO auth_tickets VALUES(?,?)',(body['jti'],exp))
    session=secrets.token_urlsafe(32);expires=t+3600
    db.execute('INSERT INTO auth_sessions VALUES(?,?,?)',(hashlib.sha256(session.encode()).hexdigest(),person['id'],expires))
    db.execute('DELETE FROM auth_sessions WHERE expires<=?',(t,));db.execute('DELETE FROM auth_tickets WHERE expires<?',(t-30,))
    return dict(ok=True,sessionToken=session,expiresAt=expires,user=person)

def resolve_user(db,headers):
    value=headers.get('Authorization','')
    if not value.startswith('Bearer '): raise IntegrationError('請從主網站登入後進入投票系統',401)
    digest=hashlib.sha256(value[7:].encode()).hexdigest()
    session=db.execute('SELECT person_id FROM auth_sessions WHERE digest=? AND expires>?',(digest,int(time.time()))).fetchone()
    if not session: raise IntegrationError('登入已過期，請從主網站重新進入',401)
    user=next((p for p in people(db) if p['id']==session['person_id']),None)
    if not user: raise IntegrationError('此員工帳號已停用',403)
    return user

def logout(db,headers):
    value=headers.get('Authorization','')
    if value.startswith('Bearer '): db.execute('DELETE FROM auth_sessions WHERE digest=?',(hashlib.sha256(value[7:].encode()).hexdigest(),))
    return {'ok':True}
