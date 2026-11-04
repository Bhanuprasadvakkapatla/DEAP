"""DEAP Connect hackathon demo server. Synthetic data only; NOT production-ready."""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, unquote
from pathlib import Path
from datetime import datetime, timezone, timedelta
import json, sqlite3, os, threading, uuid

ROOT=Path(__file__).resolve().parent
DB=ROOT/'deap_demo.sqlite3'
LOCK=threading.RLock()

def conn():
    c=sqlite3.connect(DB,timeout=10)
    c.row_factory=sqlite3.Row
    return c

def now():return datetime.now(timezone.utc).isoformat()

def initialize():
    with conn() as c:
        c.execute('CREATE TABLE IF NOT EXISTS patients (id TEXT PRIMARY KEY, state TEXT NOT NULL, care_plan TEXT NOT NULL DEFAULT \'\', updated_at TEXT NOT NULL)')
        if c.execute('SELECT COUNT(*) FROM patients').fetchone()[0]:return
        names=['Jordan Lee','Sam Rivera','Taylor Brooks','Avery Chen','Morgan Davis','Riley Patel','Casey Wilson','Alex Kim','Jamie Flores','Parker Reed','Skyler James','Quinn Carter']
        barriers=['forget','schedule','activities','swallow','side','taste','forget','schedule','activities','forget','side','schedule']
        for i,name in enumerate(names):
            first,last=name.split(' ')
            status=['green','green','yellow','red','green','yellow','green','red','green','yellow','green','green'][i]
            weekly=[{'date':(datetime.now(timezone.utc)-timedelta(days=3)).isoformat(),'status':status,'note':('I would like help with my routine.' if status!='green' else '')}]
            state={'patientName':first,'patientLastInitial':last[0],'role':'caregiver' if i%4==0 else 'self','caregiverName':('Robin' if i%4==0 else ''),'lang':'es' if i%5==0 else 'en','view':'today','step':4,'barriers':[barriers[i]],'primary':barriers[i],'strategies':['reminder'],'time':'07:00','shift':i%3==0,'channel':'SMS','tone':'friendly','routine':'After breakfast','plan':'Use a reminder linked to the morning routine','daily':[{'date':(datetime.now()-timedelta(days=j)).date().isoformat(),'status':'reported_taken'} for j in range(1,14) if (i+j)%4!=0],'weekly':weekly,'contactRequests':1 if i in (3,7) else 0,'silence':i==7}
            c.execute('INSERT INTO patients VALUES (?,?,?,?)',(f'demo-{i+1:02d}',json.dumps(state),'',now()))

def record(row):
    state=json.loads(row['state'])
    w=state.get('weekly') or []
    last=w[-1] if w else None
    status=('red' if (last and last.get('status')=='red') or state.get('contactRequests',0)>0 else 'yellow' if state.get('silence') or (last and last.get('status')=='yellow') else 'green' if last else 'blue')
    return {'id':row['id'],'patientName':state.get('patientName',''),'patientLastInitial':state.get('patientLastInitial',''),'caregiverName':state.get('caregiverName',''),'role':state.get('role','self'),'lang':state.get('lang','en'),'primary':state.get('primary',''),'barriers':state.get('barriers',[]),'strategies':state.get('strategies',[]),'weekly':w,'daily':state.get('daily',[]),'contactRequests':state.get('contactRequests',0),'silence':state.get('silence',False),'channel':state.get('channel','SMS'),'time':state.get('time',''),'plan':state.get('plan',''),'carePlan':row['care_plan'],'status':status,'updatedAt':row['updated_at']}

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*a,**kw):super().__init__(*a,directory=str(ROOT),**kw)
    def json_response(self,status,obj):
        raw=json.dumps(obj,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def read_json(self):
        n=int(self.headers.get('Content-Length','0'))
        if n>100000:self.json_response(413,{'error':'Payload too large'});return None
        try:return json.loads(self.rfile.read(n))
        except (ValueError,UnicodeDecodeError):self.json_response(400,{'error':'Invalid JSON'});return None
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/api/health':return self.json_response(200,{'ok':True,'mode':'synthetic-demo'})
        if path=='/api/patients':
            with LOCK,conn() as c:rows=c.execute('SELECT * FROM patients ORDER BY updated_at DESC').fetchall()
            return self.json_response(200,{'patients':[record(r) for r in rows]})
        if path.startswith('/api/patients/'):
            pid=unquote(path.split('/')[-1])
            with LOCK,conn() as c:row=c.execute('SELECT * FROM patients WHERE id=?',(pid,)).fetchone()
            return self.json_response(200,{'patient':record(row),'state':json.loads(row['state'])} if row else {'error':'Not found'}) if row else self.json_response(404,{'error':'Not found'})
        if path=='/':self.path='/clinician.html'
        return super().do_GET()
    def do_PUT(self):
        path=urlparse(self.path).path
        if not path.startswith('/api/patients/'):return self.json_response(404,{'error':'Not found'})
        pid=unquote(path.split('/')[-1]);data=self.read_json()
        if data is None:return
        state=data.get('state') if isinstance(data,dict) else None
        if not isinstance(state,dict) or not isinstance(state.get('patientName'),str) or len(json.dumps(state))>70000:return self.json_response(400,{'error':'Invalid patient state'})
        # Demo safety: never store contact details or other sensitive patient identifiers.
        state={k:v for k,v in state.items() if k not in ('phone','email','dob','address')}
        with LOCK,conn() as c:
            old=c.execute('SELECT care_plan FROM patients WHERE id=?',(pid,)).fetchone()
            c.execute('INSERT INTO patients(id,state,care_plan,updated_at) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at',(pid,json.dumps(state),old['care_plan'] if old else '',now()))
            row=c.execute('SELECT * FROM patients WHERE id=?',(pid,)).fetchone()
        return self.json_response(200,{'patient':record(row)})
    def do_POST(self):
        path=urlparse(self.path).path
        if not path.startswith('/api/patients/') or not path.endswith('/care-plan'):return self.json_response(404,{'error':'Not found'})
        pid=unquote(path.split('/')[-2]);data=self.read_json()
        if data is None:return
        plan=data.get('carePlan') if isinstance(data,dict) else None
        if not isinstance(plan,str) or len(plan)>1200:return self.json_response(400,{'error':'Care plan must be text under 1200 characters'})
        with LOCK,conn() as c:
            cur=c.execute('UPDATE patients SET care_plan=?,updated_at=? WHERE id=?',(plan,now(),pid))
            if not cur.rowcount:return self.json_response(404,{'error':'Not found'})
        return self.json_response(200,{'ok':True,'carePlan':plan})

if __name__=='__main__':
    initialize();port=int(os.environ.get('PORT','8000'))
    print(f'DEAP Connect demo: http://localhost:{port} (patient: /patient.html; clinician: /clinician.html)',flush=True)
    ThreadingHTTPServer(('127.0.0.1',port),Handler).serve_forever()
