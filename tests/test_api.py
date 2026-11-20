import unittest, tempfile, threading, json, sys
from pathlib import Path
from datetime import timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server

class VotingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();server.DB=Path(cls.temp.name)/'test.sqlite3';server.init()
        cls.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        cls.url='http://127.0.0.1:'+str(cls.http.server_port)
        threading.Thread(target=cls.http.serve_forever,daemon=True).start()
    @classmethod
    def tearDownClass(cls):cls.http.shutdown();cls.http.server_close();cls.temp.cleanup()
    def call(self,path,data=None,user='A001'):
        req=Request(self.url+'/api/'+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json','X-Demo-User':user})
        try:
            with urlopen(req) as r:return r.status,json.load(r)
        except HTTPError as e:return e.code,json.load(e)
    def payload(self,**kw):
        p=dict(title='測試表決',description='測試',mode='single',limit=1,anonymous=True,options=[dict(text='甲'),dict(text='乙'),dict(text='丙')],start=(server.now()-timedelta(hours=1)).isoformat(),end=(server.now()+timedelta(hours=1)).isoformat(),audienceMode='all',publish=True)
        p.update(kw);return p
    def make(self,**kw):
        code,out=self.call('save',self.payload(**kw));self.assertEqual(code,200,out);return self.poll(out['id'])
    def poll(self,id,user='A001'):
        return next(p for p in self.call('state',user=user)[1]['polls'] if p['id']==id)
    def vote(self,p,choices=None,user='E001'):
        return self.call('vote',dict(id=p['id'],choices=choices if choices is not None else [p['options'][0]['id']]),user)
    def test_only_admin_creates(self):self.assertEqual(self.call('save',self.payload(),user='E001')[0],403)
    def test_poll_hidden_from_uninvited(self):
        p=self.make(audienceMode='people',audience=['E001']);self.assertNotIn(p['id'],[x['id'] for x in self.call('state',user='E002')[1]['polls']]);self.assertEqual(self.vote(p,user='E002')[0],403)
    def test_department_scope(self):
        p=self.make(audienceMode='departments',departments=['業務部']);self.assertEqual(p['audience'],['E002','E004'])
    def test_results_hidden_until_vote(self):
        p=self.make();self.assertNotIn('counts',self.poll(p['id'],'E001'));self.assertEqual(self.vote(p)[0],200);self.assertEqual(self.poll(p['id'],'E001')['total'],1)
    def test_anonymous_not_linked(self):
        p=self.make();self.vote(p);self.assertNotIn('ballots',self.poll(p['id']))
        with server.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM named_ballots WHERE poll_id=?',(p['id'],)).fetchone()[0],0)
    def test_named_ballot(self):
        p=self.make(anonymous=False);self.vote(p);self.assertEqual(self.poll(p['id'])['ballots']['E001'],[p['options'][0]['id']]);self.assertNotIn('ballots',self.poll(p['id'],'E001'))
    def test_duplicate_and_concurrent_submissions(self):
        p=self.make()
        with ThreadPoolExecutor(max_workers=4) as pool:codes=list(pool.map(lambda _:self.vote(p)[0],range(4)))
        self.assertEqual(codes.count(200),1);self.assertEqual(codes.count(409),3);self.assertEqual(self.poll(p['id'])['total'],1);self.assertEqual(sum(self.poll(p['id'])['counts'].values()),1)
    def test_selection_validation(self):
        p=self.make(mode='multiple',limit=2);ids=[o['id'] for o in p['options']]
        for x in [[],ids,[ids[0],ids[0]],['invalid'],[{}]]:self.assertEqual(self.vote(p,x)[0],400)
        self.assertEqual(self.vote(p,ids[:2])[0],200);self.assertEqual(sum(self.poll(p['id'])['counts'].values()),2)
    def test_schedule_enforced(self):
        p=self.make(start=(server.now()+timedelta(minutes=5)).isoformat());self.assertEqual(p['status'],'scheduled');self.assertEqual(self.vote(p)[0],409)
    def test_deadline_enforced(self):
        p=self.make()
        with server.connect() as db:
            raw=server.get_poll(db,p['id']);raw['end']=(server.now()-timedelta(seconds=1)).isoformat();db.execute('UPDATE polls SET body=? WHERE id=?',(json.dumps(raw),p['id']))
        self.assertEqual(self.vote(p)[0],409);self.assertEqual(self.poll(p['id'])['status'],'ended')
    def test_publish_locks_and_cancel_keeps_votes(self):
        p=self.make();self.vote(p);self.assertEqual(self.call('save',self.payload(id=p['id']))[0],409);self.assertEqual(self.call('cancel',dict(id=p['id']))[0],200);q=self.poll(p['id']);self.assertEqual(q['total'],1);self.assertEqual(q['status'],'cancelled');self.assertEqual(self.vote(p,user='E002')[0],409)
    def test_draft_edit_and_publish(self):
        p=self.make(publish=False);self.assertEqual(p['status'],'draft');self.assertNotIn(p['id'],[x['id'] for x in self.call('state',user='E001')[1]['polls']]);code,_=self.call('save',self.payload(id=p['id'],revision=p['revision'],title='更新題目'));self.assertEqual(code,200);self.assertEqual(self.poll(p['id'])['title'],'更新題目')
    def test_bad_configuration(self):
        for kw in [dict(options=[dict(text='一')]),dict(mode='multiple',limit=9),dict(audienceMode='people',audience=[]),dict(end='invalid'),dict(anonymous='yes'),dict(options=[dict(text=''),dict(text='二')])]:self.assertEqual(self.call('save',self.payload(**kw))[0],400)
    def test_restart_preserves_records(self):
        p=self.make();self.vote(p);server.init();self.assertEqual(self.poll(p['id'])['total'],1)
    def test_option_department_visibility_and_server_enforcement(self):
        p=self.make(options=[dict(text='全部'),dict(text='業務限定',audienceMode='departments',departments=['業務部'])])
        q=self.poll(p['id'],'E001');self.assertEqual([o['text'] for o in q['options']],['全部'])
        self.assertEqual(self.vote(p,[p['options'][1]['id']])[0],400)
        self.assertEqual(len(self.poll(p['id'],'E002')['options']),2)
        self.assertEqual(self.vote(p,[p['options'][1]['id']],user='E002')[0],200)
        self.assertEqual(self.vote(p,[p['options'][0]['id']],user='E001')[0],200)
        result=self.poll(p['id']);self.assertEqual(result['optionTotals'][p['options'][1]['id']],1)
        self.assertEqual(result['optionTotals'][p['options'][0]['id']],2)
        self.assertNotIn(p['options'][1]['id'],self.poll(p['id'],'E001')['counts'])
    def test_option_scope_must_overlap_activity(self):
        payload=self.payload(audienceMode='departments',departments=['行政部'],options=[dict(text='全部'),dict(text='業務限定',audienceMode='departments',departments=['業務部'])])
        self.assertEqual(self.call('save',payload)[0],400)
    def test_every_participant_has_available_choice(self):
        options=[dict(text=x,audienceMode='departments',departments=['業務部']) for x in ['甲','乙']]
        self.assertEqual(self.call('save',self.payload(options=options))[0],400)
    def test_empty_or_unknown_option_departments_rejected(self):
        for departments in [[],['不存在部門']]:
            self.assertEqual(self.call('save',self.payload(options=[dict(text='全部'),dict(text='限定',audienceMode='departments',departments=departments)]))[0],400)

    def test_incomplete_draft_and_resume_step(self):
        p=self.make(publish=False,title='',description='',start='',end='',audienceMode='people',audience=[],options=[dict(text=''),dict(text='')],wizardStep=1)
        self.assertEqual(p['title'],'');self.assertEqual(p['wizardStep'],1);self.assertEqual(p['revision'],1)
        code,result=self.call('save',self.payload(id=p['id'],revision=1,publish=False,title='只有標題',options=[dict(text=''),dict(text='')]))
        self.assertEqual(code,200);self.assertEqual(result['revision'],2)
        self.assertEqual(self.call('save',self.payload(id=p['id'],revision=2,title='完整發布'))[0],200)
    def test_stale_draft_cannot_overwrite(self):
        p=self.make(publish=False)
        self.assertEqual(self.call('save',self.payload(id=p['id'],revision=1,publish=False,title='新內容'))[0],200)
        self.assertEqual(self.call('save',self.payload(id=p['id'],revision=1,publish=False,title='舊內容'))[0],409)
        self.assertEqual(self.poll(p['id'])['title'],'新內容')
    def test_integer_limits(self):
        for value in [1.9,1.0,'2',True,None,0,4]:
            self.assertEqual(self.call('save',self.payload(mode='multiple',limit=value))[0],400)
        self.assertEqual(self.call('save',self.payload(mode='multiple',limit=3))[0],200)
    def test_create_retry_idempotency(self):
        raw=self.payload(requestKey='create-retry-key')
        code,first=self.call('save',raw);code2,second=self.call('save',raw)
        self.assertEqual((code,code2),(200,200));self.assertEqual(first['id'],second['id']);self.assertEqual(first['revision'],second['revision'])
        self.assertEqual(self.call('save',{**raw,'title':'修改內容'})[0],409)
        with server.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM history WHERE poll_id=?',(first['id'],)).fetchone()[0],1)
    def test_draft_save_retry_does_not_conflict(self):
        p=self.make(publish=False)
        raw=self.payload(id=p['id'],revision=1,publish=False,requestKey='draft-save-retry')
        a=self.call('save',raw);b=self.call('save',raw)
        self.assertEqual(a,b);self.assertEqual(a[1]['revision'],2)
    def test_vote_retry_does_not_double_count(self):
        p=self.make();raw=dict(id=p['id'],choices=[p['options'][0]['id']],requestKey='vote-retry')
        a=self.call('vote',raw,user='E001');b=self.call('vote',raw,user='E001')
        self.assertEqual(a,b);self.assertEqual(a[0],200);self.assertEqual(self.poll(p['id'])['total'],1)
        self.assertEqual(self.vote(p,user='E001')[0],409)
        with server.connect() as db:
            stored=json.loads(db.execute('SELECT response FROM requests WHERE actor=? AND request_key=?',('E001','vote-retry')).fetchone()[0])
            self.assertNotIn('poll',stored);self.assertNotIn('choices',stored)
    def test_concurrent_create_retry(self):
        raw=self.payload(requestKey='parallel-create')
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:self.call('save',raw),range(4)))
        self.assertEqual([r[0] for r in results],[200]*4);self.assertEqual(len({r[1]['id'] for r in results}),1)

    def test_image_only_options(self):
        image='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6dAAAAABJRU5ErkJggg=='
        p=self.make(options=[dict(text='',image=image),dict(text='文字')]);self.assertEqual(p['options'][0]['image'],image)

if __name__=='__main__':unittest.main()
