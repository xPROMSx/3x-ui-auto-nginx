"""Manual Hysteria cover experiment using the existing real integration fixture.

Run only in a disposable nonroot container with test-only python3-aioquic.
See docs/HYSTERIA_COVER.md. No installer or host services are executed.
Only public-response metadata and boolean checks are printed, never subscriptions,
credentials, response bodies, or private process logs. This is an investigation,
not a required assertion that any candidate is suitable for production.
"""
import asyncio, hashlib, http.client, json, os, socket, sqlite3, ssl, subprocess, time
from pathlib import Path
from unittest.mock import patch
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
    async def request(self,path,method='GET',authority='deploy.example',extra=(),body=b''):
        stream=self._quic.get_next_available_stream_id();future=asyncio.get_running_loop().create_future()
        self.pending[stream]=(future,[],bytearray())
        headers=[(b':method',method.encode()),(b':scheme',b'https'),(b':authority',authority.encode()),(b':path',path.encode()),*extra]
        self.http.send_headers(stream,headers,end_stream=not body)
        if body:self.http.send_data(stream,body,end_stream=True)
        self.transmit()
        return await asyncio.wait_for(future,10)

# aioquic 0.9 exposes local_port but not a source-address option. Bind its real
# UDP socket to a distinct loopback client, without substituting network I/O.
class SourceSocket(socket.socket):
    def bind(self,address):
        if self.family==socket.AF_INET6 and address[0]=='::':address=('::ffff:127.0.0.2',*address[1:])
        return super().bind(address)

async def h3(f,path,method='GET',authority='deploy.example',sni='deploy.example',extra=(),body=b'',verify=True):
    config=QuicConfiguration(is_client=True,alpn_protocols=H3_ALPN,server_name=sni,idle_timeout=5)
    config.load_verify_locations(cafile=str(getattr(f,'cover_ca',f.cert)))
    if not verify:config.verify_mode=ssl.CERT_NONE  # Adversarial test client only.
    with patch('aioquic.asyncio.client.socket.socket',SourceSocket):
        async with connect('127.0.0.1',443,configuration=config,create_protocol=H3) as client:
            return await client.request(path,method,authority,extra,body)

def summary(headers,body):
    items=headers
    headers={k.decode().lower():v.decode() for k,v in items}
    public={key:headers[key] for key in ('content-type','content-length','cache-control','etag','last-modified','location','server','vary','allow','accept-ranges','alt-svc') if key in headers}
    cookies=[value.split(b'=',1)[0].decode() for key,value in items if key.lower()==b'set-cookie']
    return dict(headers=public,cookie_names=cookies,other_header_names=sorted(set(headers)-set(public)-{':status','date','connection','transfer-encoding','set-cookie'}),status=int(headers.get(':status',0)),mime=headers.get('content-type',''),
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
            connection.sock=context.wrap_socket(socket.create_connection(('127.0.0.1',443),timeout=10,source_address=('127.0.0.2',0)),server_hostname='deploy.example')
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
            c.sock=context.wrap_socket(socket.create_connection(('127.0.0.1',443),timeout=10,source_address=('127.0.0.2',0)),server_hostname='deploy.example')
            try:
                c.request('GET','/jsonsub/fixture',headers={'Host':'deploy.example'});response=c.getresponse();export=response.read()
                if response.status!=200:raise AssertionError('Native subscription export failed')
                json.loads(export)  # Verify this is the native JSON export, not an error page.
            finally:c.close()
            export_contains_server_path=b'hysteria-cover.sock' in export or str(site).encode() in export
            source_ip_result=None
            if mode=='proxy-own-domain':
                from test_integrated_features import helper as feature_helper
                from http.cookies import SimpleCookie
                directory=f.root/'source-ip-api';directory.mkdir()
                api=feature_helper('amneziawg').Panel('https://deploy.example/panel/','integration',f.auth,directory,
                    ['--cacert',str(f.cert),'--resolve','deploy.example:443:127.0.0.1','--interface','127.0.0.2'])
                try:api.request('login',{'username':'tcp-source-fixture','password':'wrong-fixture'})
                except ValueError:pass
                else:raise AssertionError('Wrong TCP login accepted')
                headers,body=asyncio.run(h3(f,'/panel/csrf-token'))
                token=json.loads(body)['obj'];cookies=SimpleCookie()
                for key,value in headers:
                    if key==b'set-cookie':cookies.load(value.decode())
                cookie='; '.join(key+'='+value.value for key,value in cookies.items())
                headers,body=asyncio.run(h3(f,'/panel/login','POST',extra=[
                    (b'content-type',b'application/json'),(b'x-csrf-token',token.encode()),(b'cookie',cookie.encode())],
                    body=json.dumps({'username':'h3-source-fixture','password':'wrong-fixture'}).encode()))
                if json.loads(body).get('success') is not False:raise AssertionError('Wrong HTTP3 login accepted')
                import re
                log=(f.root/'logs/3xui.log').read_text()
                tcp_ips=re.findall(r'failed login: username="tcp-source-fixture", IP="([^"]+)"',log)
                h3_ips=re.findall(r'failed login: username="h3-source-fixture", IP="([^"]+)"',log)
                source_ip_result=({'tcp_source_ip_preserved':tcp_ips==['127.0.0.2'],
                    'h3_source_ip_preserved':h3_ips==['127.0.0.2'],'tcp_login_logged':bool(tcp_ips),
                    'h3_login_logged':bool(h3_ips),'h3_recorded_loopback':h3_ips==['127.0.0.1']})
                if not source_ip_result['tcp_source_ip_preserved'] or not h3_ips:raise AssertionError('Native login IP control failed')
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
            report[mode]=dict(native_login_ip=source_ip_result,native_preserved=True,site_files_unchanged=True,export_contains_server_path=export_contains_server_path,results=rows,wrong_auth=wrong_auth,
                              wrong_sni_tls_rejected=bad_sni,client_positive_negative='PASS')
            stop(panel);f.release_subscription_socket()
        after={str(p.relative_to(site)):hashlib.sha256(p.read_bytes()).hexdigest() for p in site.rglob('*') if p.is_file()}
        if before!=after:raise AssertionError('Cover content changed')
        print('HYSTERIA_HTTP3_EXPERIMENT '+json.dumps(report,sort_keys=True),flush=True)
    finally:f.doCleanups();RealIntegration.doClassCleanups()

def unix_clients(f, exported, raw, domain, socket_path, udp_port):
    """Import the new native JSON, preserving masquerade; force test traffic via HY."""
    import copy
    from test_real_integration import port
    configs=exported if isinstance(exported,list) else [exported]
    original=next(c for c in configs if any(o.get('protocol')=='hysteria' for o in c['outbounds']))
    serialized=json.dumps(original)
    if any(value in serialized for value in (f.private,'PRIVATE KEY',str(f.key),str(getattr(f,'cover_key',f.key)),'/root/cert/','/etc/x-ui/')):
        raise AssertionError('Native Hysteria JSON contains server private state or key paths')
    f.validate(original,'native-hysteria-import')
    config=copy.deepcopy(original)
    outbound=next(o for o in config['outbounds'] if o.get('protocol')=='hysteria')
    stream=outbound['streamSettings'];masquerade=copy.deepcopy(stream['hysteriaSettings']['masquerade'])
    if outbound['settings']['address']!=domain or stream['tlsSettings']['serverName']!=domain:
        raise AssertionError('Native public Host/SNI mismatch')
    if set(masquerade)!={'type','url','rewriteHost'}:raise AssertionError('Unexpected masquerade fields in export')
    # Only endpoint/trust/listener/routing are relocated, as in RealIntegration.client.
    # The exported server masquerade remains untouched, including its missing UDS.
    outbound['settings'].update(address='127.0.0.1',port=443)
    stream['tlsSettings'].update(allowInsecure=False,certificates=[{'certificateFile':str(getattr(f,'cover_ca',f.cert)),'usage':'verify'}])
    socks=port(f.allocated_ports);outbound['tag']='hy-probe'
    config['inbounds']=[dict(listen='127.0.0.1',port=socks,protocol='socks',tag='probe',settings=dict(auth='noauth',udp=True))]
    config['outbounds']=[outbound];config.pop('dns',None)
    config['routing']=dict(rules=[dict(type='field',inboundTag=['probe'],outboundTag='hy-probe')])
    saved=socket_path.with_suffix('.hidden');socket_path.rename(saved)
    try:
        process=f.launch([f.xray,'run','-c',f.validate(config,'native-json-runtime')],'native-json-client')
        f.ready(process,('127.0.0.1',socks))
        before=len(f.origin_requests);durations=[]
        for _ in range(3):
            started=time.monotonic()
            r=subprocess.run(['curl','-fsS','--noproxy','','--max-time','12','--socks5-hostname',f'127.0.0.1:{socks}',
                              f'http://127.0.0.1:{f.origin.server_port}/hysteria-native-json'],capture_output=True,timeout=18)
            if r.returncode or r.stdout!=f.marker.encode():raise AssertionError('Native imported Hysteria TCP failed')
            durations.append(round(time.monotonic()-started,3))
        if len(f.origin_requests)!=before+3:raise AssertionError('TCP origin was bypassed')
        with socket.create_connection(('127.0.0.1',socks),timeout=5) as control:
            control.sendall(b'\x05\x01\x00')
            if control.recv(2)!=b'\x05\x00':raise AssertionError('SOCKS handshake')
            control.sendall(b'\x05\x03\x00\x01'+socket.inet_aton('127.0.0.1')+b'\x00\x00')
            response=control.recv(10)
            if response[:4]!=b'\x05\x00\x00\x01':raise AssertionError('UDP associate')
            relay=(socket.inet_ntoa(response[4:8]),int.from_bytes(response[8:10],'big'))
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as tunnel:
                tunnel.settimeout(10)
                prefix=b'\x00\x00\x00\x01'+socket.inet_aton('127.0.0.1')+udp_port.to_bytes(2,'big')
                for size in (32,512,1200):
                    payload=os.urandom(size);tunnel.sendto(prefix+payload,relay)
                    data,_=tunnel.recvfrom(65536)
                    if data!=prefix+payload:raise AssertionError('Native imported Hysteria UDP payload mismatch')
        stop(process)
    finally:saved.rename(socket_path)
    stream['hysteriaSettings']['auth']='wrong-auth'
    bad=f.launch([f.xray,'run','-c',f.validate(config,'native-json-negative')],'native-json-negative')
    f.ready(bad,('127.0.0.1',socks));before=len(f.origin_requests)
    r=subprocess.run(['curl','-fsS','--noproxy','','--max-time','5','--socks5-hostname',f'127.0.0.1:{socks}',
                      f'http://127.0.0.1:{f.origin.server_port}/wrong-auth'],capture_output=True,timeout=8)
    if not r.returncode or len(f.origin_requests)!=before:raise AssertionError('Wrong imported Hysteria password accepted')
    stop(bad)
    import base64
    from urllib.parse import urlsplit,parse_qs
    links=base64.b64decode(raw,validate=True).decode().splitlines()
    link=next(urlsplit(value) for value in links if urlsplit(value).scheme in ('hy2','hysteria2'))
    query=parse_qs(link.query)
    if link.hostname!=domain or link.port!=443 or query.get('sni',[domain])!=[domain]:raise AssertionError('Native RAW HY identity mismatch')
    if query.get('insecure',['0'])!=['0']:raise AssertionError('Native RAW disabled certificate validation')
    directory=f.root/('mihomo-'+str(port(f.allocated_ports)));directory.mkdir(mode=0o700)
    provider=directory/'native-raw';provider.touch(mode=0o600);provider.write_bytes(raw)
    socks=port(f.allocated_ports);controller=port(f.allocated_ports)
    runtime={'mixed-port':socks,'bind-address':'127.0.0.1','allow-lan':False,'external-controller':f'127.0.0.1:{controller}',
        'log-level':'warning','hosts':{domain:'127.0.0.1'},'tls':{'custom-certifactes':[getattr(f,'cover_ca',f.cert).read_text()]},
        'proxy-providers':{'native':{'type':'file','path':str(provider),'override':{'udp':True}}},
        'proxy-groups':[{'name':'PROBE','type':'select','use':['native'],'filter':'hysteria2'}],'rules':['MATCH,PROBE']}
    path=directory/'runtime.json';path.touch(mode=0o600);path.write_text(json.dumps(runtime))
    mihomo=f.launch([f.mihomo,'-d',directory,'-f',path],'native-raw-mihomo');f.ready(mihomo,('127.0.0.1',controller))
    c=http.client.HTTPConnection('127.0.0.1',controller,timeout=5)
    try:
        c.request('GET','/proxies');r=c.getresponse();proxies=json.loads(r.read())['proxies']
        names=proxies['PROBE']['all']
        c.request('GET','/providers/proxies/native');r=c.getresponse();nodes=json.loads(r.read())['proxies']
        if len(names)!=1 or not any(node['name']==names[0] and node['type']=='Hysteria2' for node in nodes):raise AssertionError('RAW client did not select native Hysteria2')
    finally:c.close()
    r=subprocess.run(['curl','-fsS','--noproxy','','--max-time','12','--socks5-hostname',f'127.0.0.1:{socks}',
                      f'http://127.0.0.1:{f.origin.server_port}/native-raw-mihomo'],capture_output=True,timeout=18)
    if r.returncode or r.stdout!=f.marker.encode():raise AssertionError('Mihomo native RAW data transfer failed')
    stop(mihomo)
    return dict(no_server_private_state=True,native_raw_mihomo=True,raw_tls_verified=True,native_json_import=True,masquerade_unchanged=True,client_socket_absent=True,tcp_transfers=3,
                udp_sizes=[32,512,1200],wrong_password_rejected=True,tcp_seconds=durations,
                masquerade_contains_only_fixed_socket=True)


def unix_comparison():
    """Compare both identities before making a production identity decision."""
    import re, threading
    from urllib.parse import urljoin,urlsplit
    from html.parser import HTMLParser
    from test_real_integration import port
    class Links(HTMLParser):
        def __init__(self):super().__init__();self.paths=set()
        def handle_starttag(self,tag,attrs):
            self.paths.update(value for key,value in attrs if key in ('href','src') and value)
    RealIntegration.setUpClass();report={}
    try:
        for identity,code,domain in (('panel','A1','deploy.example'),('reality','B1','cover.example')):
            f=RealIntegration('runTest')
            try:
                f.setUp();f.public=f.hysteria_port=443
                generator=helper().module('generate')
                for role,template in (('panel','A1'),('reality','B1')):
                    generator.generate(template,f.root/'www/html'/('cover-'+role),[0,0,0,0])
                site=f.root/'www/html'/('cover-'+identity)
                f.cover_cert=f.root/'cover-cert.pem';f.cover_key=f.root/'cover-key.pem';f.cover_ca=f.root/'cover-ca.pem'
                subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                    '-subj','/CN='+domain,'-addext','subjectAltName=DNS:'+domain,
                    '-keyout',str(f.cover_key),'-out',str(f.cover_cert)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20)
                f.cover_ca.write_bytes(f.cert.read_bytes()+f.cover_cert.read_bytes())
                before={str(p.relative_to(f.root/'www')):hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in (f.root/'www').rglob('*') if p.is_file() and
                        any(f.root/'www/html'/('cover-'+role) in p.parents for role in ('panel','reality'))}
                udp=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);udp.bind(('127.0.0.1',0));udp.settimeout(.2)
                f.addCleanup(udp.close);udp_port=udp.getsockname()[1];closing=threading.Event();f.addCleanup(closing.set)
                def echo():
                    while not closing.is_set():
                        try:data,addr=udp.recvfrom(65536);udp.sendto(data,addr)
                        except socket.timeout:pass
                        except OSError:return
                threading.Thread(target=echo,daemon=True).start()
                socket_path=f.root/'hysteria-cover.sock'
                escaped=domain.replace('.','\\.')
                f.http_site=f'''server {{ listen unix:{socket_path}; server_name {domain}; server_tokens off;
absolute_redirect off; root {site}; index index.html;
if ($host !~* ^{escaped}$) {{ return 404; }}
location / {{ error_page 404 /404.html; try_files $uri $uri/ =404; }} }}'''
                # Relocate only the selected vhost's certificate, using the real
                # installer heredoc, exactly like the existing fixture relocation.
                from test_real_integration import render as original_render,MAIN
                selected=MAIN if identity=='panel' else 'cat > "/etc/nginx/sites-available/${reality_domain}"'
                def certificate_render(target,*args,**kwargs):
                    text=original_render(target,*args,**kwargs)
                    if target==selected:
                        text=re.sub(r'/etc/letsencrypt/live/[^/]+/fullchain.pem',str(f.cover_cert),text)
                        text=re.sub(r'/etc/letsencrypt/live/[^/]+/privkey.pem',str(f.cover_key),text)
                    return text
                def nginx_start():
                    with patch('test_real_integration.render',side_effect=certificate_render):return f.nginx_config()
                nginx=nginx_start()
                # Real REALITY fallback must be traversed, not a standalone TLS vhost.
                with sqlite3.connect(f.root/'db/x-ui.db') as db:
                    row=db.execute("SELECT id,stream_settings FROM inbounds WHERE protocol='hysteria'").fetchone()
                    stream=json.loads(row[1]);stream['tlsSettings']['serverName']=domain
                    stream['tlsSettings']['certificates'][0].update(certificateFile=str(f.cover_cert),keyFile=str(f.cover_key))
                    stream['hysteriaSettings']['masquerade']=dict(type='proxy',url='unix:'+str(socket_path),rewriteHost=False)
                    db.execute('UPDATE inbounds SET port=443,stream_settings=? WHERE id=?',(json.dumps(stream),row[0]))
                    db.execute('UPDATE hosts SET address=? WHERE inbound_id=?',(domain,row[0]))
                    key='xrayTemplateConfig';template=json.loads(db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()[0])
                    template['outbounds'][0]['settings']['finalRules'][0]['port']+=','+str(udp_port)
                    db.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(template),key))
                env={**os.environ,'XUI_DB_FOLDER':str(f.root/'db'),'XUI_BIN_FOLDER':str(f.root/'bin'),'XUI_LOG_FOLDER':str(f.root/'logs')}
                panel=f.launch([f.xui],identity+'-panel',env);f.ready(panel,('127.0.0.1',f.backends['panel']))
                deadline=time.monotonic()+15
                while '127.0.0.1:443' not in subprocess.check_output(['ss','-H','-lunp','sport = :443'],text=True):
                    if panel.poll() is not None or time.monotonic()>deadline:raise AssertionError('Native UDP listener failed')
                    time.sleep(.05)
                def tcp(path,method='GET',host=domain,sni=domain):
                    c=http.client.HTTPConnection('127.0.0.1',443,timeout=10)
                    c.sock=ssl.create_default_context(cafile=str(f.cover_ca)).wrap_socket(
                        socket.create_connection(('127.0.0.1',443),timeout=10),server_hostname=sni)
                    try:
                        c.request(method,path,headers={'Host':host});r=c.getresponse();body=r.read()
                        return summary([(b':status',str(r.status).encode()),*[(k.encode(),v.encode()) for k,v in r.getheaders()]],body),body
                    finally:c.close()
                with ssl.create_default_context(cafile=str(f.cover_ca)).wrap_socket(
                        socket.create_connection(('127.0.0.1',443),timeout=10),server_hostname=domain) as verified:
                    expected=ssl.PEM_cert_to_DER_cert(f.cover_cert.read_text())
                    if verified.getpeercert(binary_form=True)!=expected:raise AssertionError('TCP did not present the selected domain certificate')
                # Subscriptions remain on Panel, even when the HY endpoint uses REALITY.
                c=http.client.HTTPConnection('127.0.0.1',443,timeout=10)
                c.sock=ssl.create_default_context(cafile=str(getattr(f,'cover_ca',f.cert))).wrap_socket(socket.create_connection(('127.0.0.1',443),timeout=10),server_hostname='deploy.example')
                try:
                    c.request('GET','/jsonsub/fixture',headers={'Host':'deploy.example'});r=c.getresponse();exported=json.loads(r.read())
                    if r.status!=200:raise AssertionError('Native export failed')
                    c.request('GET','/subscription/fixture',headers={'Host':'deploy.example'});r=c.getresponse();raw=r.read()
                    if r.status!=200:raise AssertionError('Native RAW export failed')
                finally:c.close()
                cases={(path,method,domain) for path in ('/','/index.html','/missing','/auth','/assets','/assets/',
                       '/.env','/etc/x-ui/x-ui.db','/root/cert/'+domain+'/privkey.pem','/hysteria-cover.sock',
                       '/%2e%2e/%2e%2e/etc/passwd','/panel/','/panel/panel/api/inbounds/list','/diagnostics/',
                       '/diagnostics/api/st/ping','/panel/diag','/subscription/fixture','/jsonsub/fixture') for method in ('GET','HEAD')}
                cases.update((('/', 'POST',domain),('/auth','POST',domain),('/','GET','attacker.example'),
                              ('/','GET',domain.upper()),('/','GET',domain+':443')))
                links=set()
                for file in sorted(site.rglob('*')):
                    path='/'+str(file.relative_to(site))
                    if file.is_file():
                        cases.update((path,m,domain) for m in ('GET','HEAD'))
                        if file.suffix=='.html':
                            parser=Links();parser.feed(file.read_text())
                            for value in parser.paths:
                                url=urlsplit(urljoin(path,value))
                                if not url.scheme and not url.netloc:links.add(url.path)
                    elif file.is_dir():
                        cases.update((p,m,domain) for p in (path,path+'/') for m in ('GET','HEAD'))
                cases.update((path,m,domain) for path in links for m in ('GET','HEAD'))
                rows=[]
                for path,method,authority in sorted(cases):
                    reference,_=tcp(path,method,authority)
                    headers,body=asyncio.run(h3(f,path,method,authority,sni=domain))
                    observed=summary(headers,body)
                    if b'<pre>' in body and b'<a href=' in body:raise AssertionError('Directory listing')
                    if any(k.lower()==b'set-cookie' and b'diag_key=' in v for k,v in headers):raise AssertionError('Anonymous diagnostic cookie')
                    if method=='HEAD' and body:raise AssertionError('HEAD returned a body')
                    if any(value in body for value in (b'-----BEGIN PRIVATE KEY-----',b'SQLite format 3',f.auth.encode(),f.private.encode())):raise AssertionError('Private state exposed by cover backend')
                    rows.append(dict(path=path,method=method,host=authority,tcp=reference,h3=observed))
                h2=[]
                for path,method in (('/','GET'),('/','HEAD'),('/','POST'),('/static/site.css','GET'),('/static/','GET'),('/missing','GET')):
                    header_file=f.root/'h2-headers';body_file=f.root/'h2-body'
                    for file in (header_file,body_file):file.touch(mode=0o600)
                    command=['curl','-q','-sS','--http2','--noproxy','*','--cacert',str(f.cover_ca),
                        '--resolve',domain+':443:127.0.0.1','--max-time','10','-D',str(header_file),'-o',str(body_file),'-w','%{http_version}']
                    if method=='HEAD':command+=['--head']
                    elif method=='POST':command+=['-X','POST']
                    result=subprocess.run(command+['https://'+domain+path],capture_output=True,timeout=15)
                    if result.returncode or result.stdout!=b'2':raise AssertionError('Real HTTP2 request failed')
                    lines=header_file.read_bytes().splitlines();status=int(lines[0].split()[1]);headers=[(b':status',str(status).encode())]
                    headers.extend(tuple(line.split(b': ',1)) for line in lines[1:] if b': ' in line)
                    body=b'' if method=='HEAD' else body_file.read_bytes()
                    h2.append(dict(path=path,method=method,tcp=summary(headers,body)))
                managed=('/assets','/assets/','/panel/','/panel/panel/api/inbounds/list','/diagnostics/',
                         '/diagnostics/api/st/ping','/panel/diag','/subscription/fixture','/jsonsub/fixture')
                for entry in rows:
                    if entry['host']!=domain:continue
                    if entry['path'] in managed:
                        if entry['h3']['status']!=404:raise AssertionError('Static backend exposed an application route')
                    elif '%' not in entry['path']:
                        for key in ('status','sha256','headers','cookie_names'):
                            if entry['tcp'][key]!=entry['h3'][key]:raise AssertionError('Public HTTP3 content/header mismatch')
                    if any('hysteria' in key.lower() for key in entry['h3']['other_header_names']):raise AssertionError('Hysteria response fingerprint')
                for entry in h2:
                    match=next(r for r in rows if (r['path'],r['method'],r['host'])==(entry['path'],entry['method'],domain))
                    for key in ('status','sha256','headers','cookie_names'):
                        if entry['tcp'][key]!=match['h3'][key]:raise AssertionError('HTTP2/HTTP3 public response mismatch')
                for path in links:
                    match=next(r for r in rows if (r['path'],r['method'],r['host'])==(path,'GET',domain))
                    if match['h3']['status']!=200:raise AssertionError('Broken real internal link')
                wrong_headers,_=asyncio.run(h3(f,'/auth','POST','hysteria',sni=domain,extra=[(b'hysteria-auth',b'wrong-auth')]))
                if dict(wrong_headers).get(b':status')==b'233':raise AssertionError('Wrong auth accepted')
                # Deliberately adversarial CLIENT probe only: distinguish server SNI
                # refusal from certificate verification. No production TLS is weakened.
                headers,body=asyncio.run(h3(f,'/','GET',domain,sni='wrong.example',verify=False))
                wrong_sni=summary(headers,body)
                stop(panel);f.release_subscription_socket()
                with sqlite3.connect(f.root/'db/x-ui.db') as db:
                    stream['tlsSettings']['rejectUnknownSni']=True
                    db.execute('UPDATE inbounds SET stream_settings=? WHERE id=?',(json.dumps(stream),row[0]))
                panel=f.launch([f.xui],identity+'-strict-sni-panel',env);f.ready(panel,('127.0.0.1',f.backends['panel']))
                deadline=time.monotonic()+15
                while '127.0.0.1:443' not in subprocess.check_output(['ss','-H','-lunp','sport = :443'],text=True):
                    if panel.poll() is not None or time.monotonic()>deadline:raise AssertionError('Strict SNI UDP listener failed')
                    time.sleep(.05)
                try:
                    await_result=asyncio.run(h3(f,'/','GET',domain,sni='wrong.example',verify=False))
                except (ConnectionError,TimeoutError):strict_rejected=True
                else:strict_rejected=False
                if not strict_rejected:raise AssertionError('Server accepted unknown SNI with rejectUnknownSni')
                other='cover.example' if identity=='panel' else 'deploy.example'
                try:asyncio.run(h3(f,'/','GET',domain,sni=other,verify=False))
                except (ConnectionError,TimeoutError):other_rejected=True
                else:raise AssertionError('Server accepted the other domain with its single-name certificate')
                headers,_=asyncio.run(h3(f,'/','GET',domain,sni=domain))
                if dict(headers).get(b':status')!=b'200':raise AssertionError('Strict SNI broke normal HTTP3')
                c=http.client.HTTPConnection('127.0.0.1',443,timeout=10)
                c.sock=ssl.create_default_context(cafile=str(getattr(f,'cover_ca',f.cert))).wrap_socket(socket.create_connection(('127.0.0.1',443),timeout=10),server_hostname='deploy.example')
                try:
                    c.request('GET','/jsonsub/fixture',headers={'Host':'deploy.example'});r=c.getresponse();exported=json.loads(r.read())
                    if r.status!=200:raise AssertionError('Strict SNI native export failed')
                    c.request('GET','/subscription/fixture',headers={'Host':'deploy.example'});r=c.getresponse();raw=r.read()
                    if r.status!=200:raise AssertionError('Native RAW export failed')
                finally:c.close()
                auth_headers,_=asyncio.run(h3(f,'/auth','POST','hysteria',sni=domain,extra=[(b'hysteria-auth',f.auth.encode())]))
                if dict(auth_headers).get(b':status')!=b'233':raise AssertionError('Valid Hysteria auth failed')
                client_result=unix_clients(f,exported,raw,domain,socket_path,udp_port)
                stop(nginx)
                nginx=nginx_start()
                headers,_=asyncio.run(h3(f,'/','GET',domain,sni=domain))
                if dict(headers).get(b':status')!=b'200' or not socket_path.is_socket():raise AssertionError('Nginx did not recreate working UDS')
                restarted_client=unix_clients(f,exported,raw,domain,socket_path,udp_port)
                # Nginx restart must recreate the UDS; native HY client restart was
                # already exercised above, with the exported masquerade untouched.
                after={str(p.relative_to(f.root/'www')):hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in (f.root/'www').rglob('*') if p.is_file() and
                        any(f.root/'www/html'/('cover-'+role) in p.parents for role in ('panel','reality'))}
                if before!=after:raise AssertionError('Existing cover files changed')
                report[identity]=dict(results=rows,http2=h2,internal_links=len(links),wrong_sni_server=wrong_sni,strict_sni_server_rejected=strict_rejected,other_domain_sni_rejected=other_rejected,same_domain_certificate=True,
                                      wrong_auth=summary(wrong_headers,b''),native_client=client_result,sites_unchanged=True,
                                      socket_mode=oct(socket_path.stat().st_mode&0o777),socket_recreated=True,post_restart_client=restarted_client)
                stop(panel)
            finally:f.doCleanups()
        print('HYSTERIA_UNIX_COMPARISON '+json.dumps(report,sort_keys=True),flush=True)
    finally:RealIntegration.doClassCleanups()

if __name__=='__main__':
    if os.geteuid()==0:raise RuntimeError('Run in a disposable nonroot test container')
    if os.environ.get('HY_EXPERIMENT_MODES')=='unix-comparison':unix_comparison()
    else:main()
