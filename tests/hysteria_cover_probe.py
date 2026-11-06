"""Manual Hysteria cover experiment using the existing real integration fixture.

Run only in a disposable nonroot container with test-only python3-aioquic.
See docs/HYSTERIA_COVER.md. No installer or host services are executed.
Only public-response metadata and boolean checks are printed, never subscriptions,
credentials, response bodies, or private process logs. This is an investigation,
not a required assertion that any candidate is suitable for production.
"""
import asyncio, hashlib, http.client, json, os, socket, sqlite3, ssl, subprocess, time
from pathlib import Path
from aioquic.asyncio import connect, QuicConnectionProtocol
from aioquic.h3.connection import H3Connection, H3_ALPN
from aioquic.h3.events import HeadersReceived, DataReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import ConnectionTerminated
from test_real_integration import RealIntegration, stop
from test_cover_sites import helper

class H3(QuicConnectionProtocol):
    def __init__(self,*a,**k):
        super().__init__(*a,**k);self.http=H3Connection(self._quic);self.pending={}
    def quic_event_received(self,event):
        if isinstance(event,ConnectionTerminated):
            for entry in self.pending.values():
                if not entry[0].done():entry[0].set_exception(ConnectionError('QUIC connection closed'))
        for response in self.http.handle_event(event):
            if isinstance(response,(HeadersReceived,DataReceived)) and response.stream_id in self.pending:
                future,headers,body=self.pending[response.stream_id]
                if isinstance(response,HeadersReceived):headers.extend(response.headers)
                else:body.extend(response.data)
                if response.stream_ended and not future.done():future.set_result((headers,bytes(body)))
    async def request(self,path,method='GET',authority='deploy.example',extra=()):
        stream=self._quic.get_next_available_stream_id();future=asyncio.get_running_loop().create_future()
        self.pending[stream]=(future,[],bytearray())
        headers=[(b':method',method.encode()),(b':scheme',b'https'),(b':authority',authority.encode()),(b':path',path.encode()),*extra]
        self.http.send_headers(stream,headers,end_stream=True);self.transmit()
        return await asyncio.wait_for(future,10)

async def h3(f,path,method='GET',authority='deploy.example',sni='deploy.example',extra=()):
    config=QuicConfiguration(is_client=True,alpn_protocols=H3_ALPN,server_name=sni,idle_timeout=5)
    config.load_verify_locations(cafile=str(f.cert))
    async with connect('127.0.0.1',443,configuration=config,create_protocol=H3) as client:
        return await client.request(path,method,authority,extra)

def summary(headers,body):
    headers={k.decode().lower():v.decode() for k,v in headers}
    return dict(status=int(headers.get(':status',0)),mime=headers.get('content-type',''),
                location=headers.get('location',''),length=len(body),sha256=hashlib.sha256(body).hexdigest())

def main():
    if os.geteuid()==0:raise RuntimeError('Run in a disposable nonroot test container')
    RealIntegration.setUpClass();f=RealIntegration('runTest');report={}
    try:
        f.setUp();f.public=443;f.hysteria_port=443
        generator=helper().module('generate')
        site=f.root/'www/html/cover-panel';generator.generate('A1',site,[0,0,0,0])
        generator.generate('B1',f.root/'www/html/cover-reality',[0,0,0,0])
        before={str(p.relative_to(site)):hashlib.sha256(p.read_bytes()).hexdigest() for p in site.rglob('*') if p.is_file()}
        socket_path=str(f.root/'hysteria-cover.sock')
        # A static-only local backend is necessary to evaluate Unix proxy: the
        # existing TLS listener requires PROXY protocol, which Xray proxy lacks.
        f.http_site='server { listen unix:'+socket_path+'; server_name deploy.example; server_tokens off; absolute_redirect off; root '+str(site)+'; index index.html; if ($host !~* ^deploy\\.example$) { return 404; } location / { error_page 404 /404.html; try_files $uri $uri/ =404; } }'
        f.nginx_config()
        def tcp(path,method='GET',authority='deploy.example'):
            context=ssl.create_default_context(cafile=str(f.cert));connection=http.client.HTTPConnection('127.0.0.1',443,timeout=10)
            connection.sock=context.wrap_socket(socket.create_connection(('127.0.0.1',443),timeout=10),server_hostname='deploy.example')
            try:
                connection.request(method,path,headers={'Host':authority});response=connection.getresponse();body=response.read()
                return summary([(b':status',str(response.status).encode()),*[(k.encode(),v.encode()) for k,v in response.getheaders()]],body),body
            finally:connection.close()
        probes=[('/', 'GET','deploy.example'),('/', 'HEAD','deploy.example'),('/', 'POST','deploy.example'),
                ('/index.html','GET','deploy.example'),('/product','GET','deploy.example'),('/product/','GET','deploy.example'),
                ('/static/','GET','deploy.example'),('/missing','GET','deploy.example'),('/auth','GET','deploy.example'),
                ('/auth','POST','deploy.example'),('/','GET','attacker.example'),('/.env','GET','deploy.example'),
                ('/etc/x-ui/x-ui.db','GET','deploy.example'),('/root/cert/deploy.example/privkey.pem','GET','deploy.example'),
                ('/%2e%2e/%2e%2e/etc/passwd','GET','deploy.example'),
                ('/panel/panel/api/inbounds/list','GET','deploy.example'),('/diagnostics/','GET','deploy.example'),
                ('/diagnostics/api/st/ping','GET','deploy.example'),('/panel/diag','GET','deploy.example')]
        for file in sorted(site.rglob('*')):
            if file.is_file():probes.append(('/'+str(file.relative_to(site)),'GET','deploy.example'))
        for mode,masq in [('proxy-unix',{'type':'proxy','url':'unix:'+socket_path,'rewriteHost':False}),('none',None),('file',{'type':'file','dir':str(site)}),
                          ('proxy-loopback-tls',{'type':'proxy','url':f'https://127.0.0.1:{f.backends["tls"]}','rewriteHost':False}),
                          ('proxy-own-domain',{'type':'proxy','url':'https://deploy.example/','rewriteHost':False,'xForwarded':True})]:
            if mode not in os.environ.get('HY_EXPERIMENT_MODES','none,file,proxy-loopback-tls,proxy-unix').split(','):continue
            with sqlite3.connect(f.root/'db/x-ui.db') as db:
                row=db.execute("SELECT id,stream_settings FROM inbounds WHERE protocol='hysteria'").fetchone()
                stream=json.loads(row[1]);settings=stream['hysteriaSettings'];settings.pop('masquerade',None)
                if masq:settings['masquerade']=masq
                db.execute('UPDATE inbounds SET port=443,stream_settings=? WHERE id=?',(json.dumps(stream),row[0]))
            env={**os.environ,'XUI_DB_FOLDER':str(f.root/'db'),'XUI_BIN_FOLDER':str(f.root/'bin'),'XUI_LOG_FOLDER':str(f.root/'logs'),
                 'SSL_CERT_FILE':str(f.cert),'NO_PROXY':os.environ.get('NO_PROXY','')+',deploy.example,127.0.0.1'}
            panel=f.launch([f.xui],'h3-panel-'+mode,env);f.ready(panel,('127.0.0.1',f.backends['panel']))
            deadline=time.monotonic()+15
            while '127.0.0.1:443' not in subprocess.check_output(['ss','-H','-lunp','sport = :443'],text=True):
                if panel.poll() is not None or time.monotonic()>deadline:raise RuntimeError('Native Hysteria listener failed')
                time.sleep(.05)
            generated=json.loads((f.root/'bin/config.json').read_text())
            hy=next(i['streamSettings']['hysteriaSettings'] for i in generated['inbounds'] if i['protocol']=='hysteria')
            if hy.get('masquerade')!=masq:raise AssertionError('Native panel did not preserve masquerade')
            context=ssl.create_default_context(cafile=str(f.cert));c=http.client.HTTPConnection('127.0.0.1',443,timeout=10)
            c.sock=context.wrap_socket(socket.create_connection(('127.0.0.1',443),timeout=10),server_hostname='deploy.example')
            try:
                c.request('GET','/jsonsub/fixture',headers={'Host':'deploy.example'});response=c.getresponse();export=response.read()
                if response.status!=200:raise AssertionError('Native subscription export failed')
                json.loads(export)  # Verify this is the native JSON export, not an error page.
            finally:c.close()
            export_contains_server_path=b'hysteria-cover.sock' in export or str(site).encode() in export
            rows=[]
            for path,method,authority in probes:
                reference,_=tcp(path,method,authority)
                headers,body=asyncio.run(asyncio.wait_for(h3(f,path,method,authority),15))
                observed=summary(headers,body)
                observed['directory_listing']=b'<pre>' in body and b'<a href=' in body
                observed['diag_cookie_issued']=any(k.lower()==b'set-cookie' and b'diag_key=' in v for k,v in headers)
                rows.append(dict(path=path,method=method,host=authority,tcp=reference,h3=observed))
            headers,body=asyncio.run(h3(f,'/auth','POST','hysteria',extra=[(b'hysteria-auth',b'wrong-auth')]))
            wrong_auth=summary(headers,body)
            if wrong_auth['status']==233:raise AssertionError('Wrong Hysteria auth accepted')
            success_headers,_=asyncio.run(h3(f,'/auth','POST','hysteria',extra=[(b'hysteria-auth',f.auth.encode())]))
            if dict(success_headers).get(b':status')!=b'233':raise AssertionError('Native valid Hysteria auth failed')
            bad_sni=False
            try:asyncio.run(asyncio.wait_for(h3(f,'/',sni='wrong.example'),10))
            except (ConnectionError,TimeoutError):bad_sni=True
            if not bad_sni:raise AssertionError('Wrong SNI certificate was trusted')
            f.client('hysteria2',server_log='h3-panel-'+mode+'.log');f.client('hysteria2',negative=True,server_log='h3-panel-'+mode+'.log')
            report[mode]=dict(native_preserved=True,site_files_unchanged=True,export_contains_server_path=export_contains_server_path,results=rows,wrong_auth=wrong_auth,
                              wrong_sni_tls_rejected=bad_sni,client_positive_negative='PASS')
            stop(panel);f.release_subscription_socket()
        after={str(p.relative_to(site)):hashlib.sha256(p.read_bytes()).hexdigest() for p in site.rglob('*') if p.is_file()}
        if before!=after:raise AssertionError('Cover content changed')
        print('HYSTERIA_HTTP3_EXPERIMENT '+json.dumps(report,sort_keys=True),flush=True)
    finally:f.doCleanups();RealIntegration.doClassCleanups()

if __name__=='__main__':main()
