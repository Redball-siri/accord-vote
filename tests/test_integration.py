import unittest,tempfile,threading,json,sys,time,base64,hmac,hashlib,uuid,os
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server,integration

class HostIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'ACCORD_AUTH_MODE':'host','ACCORD_SSO_SECRET':'s'*40,'ACCORD_DIRECTORY_TOKEN':'d'*40,'ACCORD_HOST_ORIGIN':'https://company.example','ACCORD_SSO_ISSUER':'company-main','ACCORD_DIRECTORY_URL':''});self.env.start()
        self.temp=tempfile.TemporaryDirectory();self.old_db=server.DB;server.DB=Path(self.temp.name)/'host.sqlite3';server.init()
        self.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);threading.Thread(target=self.http.serve_forever,daemon=True).start();self.url='http://127.0.0.1:'+str(self.http.server_port)
        self.rows=[dict(id='upstream-admin',employeeNumber='HR001',name='測試主管',department='人資部',admin=True,active=True),dict(id='upstream-employee',employeeNumber='SA001',name='測試員工',department='業務部',admin=False,active=True)]
    def tearDown(self):self.http.shutdown();self.http.server_close();server.DB=self.old_db;self.temp.cleanup();self.env.stop()
    def call(self,path,data=None,token=None,extra=None):
        headers={'Content-Type':'application/json',**(extra or {})}
        if token:headers['Authorization']='Bearer '+token
        req=Request(self.url+'/api/'+path,data=json.dumps(data).encode() if data is not None else None,headers=headers)
        try:
            with urlopen(req) as r:return r.status,json.load(r)
        except HTTPError as e:return e.code,json.load(e)
    def sync(self,rows=None,revision=1):return self.call('integration/roster',dict(revision=revision,employees=self.rows if rows is None else rows),token='d'*40)
    def ticket(self,**kw):
        t=int(time.time());body=dict(iss='company-main',aud='accord',sub='upstream-employee',iat=t,exp=t+120,jti=str(uuid.uuid4()));body.update(kw)
        b64=lambda obj:base64.urlsafe_b64encode(json.dumps(obj,separators=(',',':')).encode()).decode().rstrip('=')
        signing=b64({'alg':'HS256','typ':'JWT'})+'.'+b64(body)
        return signing+'.'+base64.urlsafe_b64encode(hmac.new(('s'*40).encode(),signing.encode(),hashlib.sha256).digest()).decode().rstrip('=')
    def login(self,sub='upstream-employee'):
        self.sync();code,out=self.call('auth/exchange',dict(token=self.ticket(sub=sub)));self.assertEqual(code,200,out);return out['sessionToken']
    def test_demo_header_cannot_impersonate_admin(self):
        self.assertEqual(self.call('state',extra={'X-Demo-User':'A001'})[0],401)
        self.assertEqual(self.call('save',{},extra={'X-Demo-User':'A001'})[0],401)
    def test_host_login_and_role_from_directory(self):
        self.sync();code,out=self.call('auth/exchange',dict(token=self.ticket(admin=True,role='admin')));self.assertEqual(code,200)
        token=out['sessionToken'];state=self.call('state',token=token)[1]
        self.assertFalse(state['user']['admin']);self.assertEqual(state['user']['employeeNumber'],'SA001');self.assertEqual(len(state['people']),1)
        self.assertEqual(self.call('save',{},token=token)[0],403)
    def test_admin_directory_access(self):
        token=self.login('upstream-admin');state=self.call('state',token=token)[1];self.assertTrue(state['user']['admin']);self.assertEqual(len(state['people']),2)
    def test_invalid_expired_audience_and_replay(self):
        self.sync();t=int(time.time())
        for token in [self.ticket(exp=t-1),self.ticket(aud='other-app'),self.ticket(iss='someone-else'),self.ticket(iat=t,exp=t+1000),'invalid']:
            self.assertEqual(self.call('auth/exchange',dict(token=token))[0],401)
        token=self.ticket();self.assertEqual(self.call('auth/exchange',dict(token=token))[0],200);self.assertEqual(self.call('auth/exchange',dict(token=token))[0],401)
    def test_unknown_employee_no_demo_fallback(self):
        self.assertEqual(self.call('auth/exchange',dict(token=self.ticket(sub='E001')))[0],403)
    def test_disabled_employee_loses_existing_session(self):
        token=self.login();rows=[{**r,'active':False} if not r['admin'] else r for r in self.rows];self.assertEqual(self.sync(rows,2)[0],200);self.assertEqual(self.call('state',token=token)[0],401)
    def test_role_revocation_applies_to_existing_session(self):
        token=self.login('upstream-admin');rows=[{**r,'admin':False} for r in self.rows];self.sync(rows,2)
        self.assertFalse(self.call('state',token=token)[1]['user']['admin']);self.assertEqual(self.call('save',{},token=token)[0],403)
    def test_roster_service_auth_and_revision(self):
        self.assertEqual(self.call('integration/roster',dict(revision=1,employees=self.rows))[0],401)
        self.assertEqual(self.sync()[0],200);self.assertTrue(self.sync()[1]['unchanged']);self.assertEqual(self.sync([{**r,'name':'不同'} for r in self.rows])[0],409)
        self.assertEqual(self.sync(self.rows*2,2)[0],400)
    def test_session_logout_and_no_plaintext_storage(self):
        token=self.login()
        with server.connect() as db:self.assertNotEqual(db.execute('SELECT digest FROM auth_sessions').fetchone()[0],token)
        self.assertEqual(self.call('auth/logout',{},token=token)[0],200);self.assertEqual(self.call('state',token=token)[0],401)
    def test_public_config_contains_no_secrets(self):
        code,config=self.call('integration/config');self.assertEqual(code,200);self.assertEqual(config,dict(mode='host',hostOrigin='https://company.example'))
    def test_poll_eligibility_uses_host_roster(self):
        from datetime import timedelta
        admin=self.login('upstream-admin');employee=self.login()
        raw=dict(title='主網站票選',description='',mode='single',limit=1,anonymous=True,options=[dict(text='同意'),dict(text='不同意')],start=(server.now()-timedelta(minutes=1)).isoformat(),end=(server.now()+timedelta(hours=1)).isoformat(),audienceMode='all',publish=True)
        code,out=self.call('save',raw,token=admin);self.assertEqual(code,200,out)
        self.assertEqual(set(out['poll']['audience']),{'upstream-admin','upstream-employee'})
        choice=out['poll']['options'][0]['id'];vote=dict(id=out['id'],choices=[choice])
        self.assertEqual(self.call('vote',vote,token=admin)[0],200);self.assertEqual(self.call('vote',vote,token=employee)[0],200)
        self.assertEqual(self.call('state',token=admin)[1]['polls'][0]['total'],2)

    def test_invalid_host_configuration_fails_closed(self):
        with patch.dict(os.environ,{'ACCORD_SSO_SECRET':''}):
            with self.assertRaises(RuntimeError):integration.check_config()

if __name__=='__main__':unittest.main()
