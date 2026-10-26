"""Actual manager Restore in the disposable guest; secret results stay on disk."""
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import socket
import sqlite3
import subprocess
import time

import checks

ROOT=checks.ROOT
ARCHIVE=ROOT/'source-backup.tar.gz'


def snapshot():
    with sqlite3.connect('file:/etc/x-ui/x-ui.db?mode=ro',uri=True) as db:
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result={name:db.execute('SELECT * FROM '+name+' ORDER BY rowid').fetchall()
                for name in ('settings','users','inbounds','hosts') if name in tables}
        # Runtime traffic can grow under real consumers, but must never reset.
        for name in ('client_traffics','inbound_client_ips'):
            if name in tables and name=='client_traffics':
                columns=[row[1] for row in db.execute('PRAGMA table_info('+name+')')]
                result[name]=[dict(zip(columns,row)) for row in db.execute('SELECT * FROM '+name)]
    result['files']={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in
        [*Path('/etc/letsencrypt/live').glob('*/*.pem'),Path('/etc/x-ui/initial-client.json'),
         Path('/etc/x-ui/auto-nginx-managed.json'),Path('/var/www/html/restore-custom.txt')]}
    return result


def source_backup():
    Path('/var/www/html/restore-custom.txt').write_bytes(os.urandom(64))
    with sqlite3.connect('/etc/x-ui/x-ui.db') as db:
        db.execute("UPDATE settings SET value='Clean Restore private fixture' WHERE key='webRemark'")
    checks.manager_backup()
    archive=max(Path('/var/backups/x-ui').glob('*.tar.gz'),key=lambda p:p.stat().st_mtime_ns)
    (ROOT/'source-path').write_text(str(archive))
    # Read the actual archived SQLite, not a later state modified by consumers.
    import tarfile,tempfile
    with tarfile.open(archive) as tar, tempfile.TemporaryDirectory() as temporary:
        data=tar.extractfile('files/etc/x-ui/x-ui.db').read()
        path=Path(temporary)/'db';path.write_bytes(data)
        with sqlite3.connect(path) as db:
            expected=snapshot()
            for name in ('settings','users','inbounds','hosts'):
                expected[name]=db.execute('SELECT * FROM '+name+' ORDER BY rowid').fetchall()
    (ROOT/'source-state.private.json').write_text(json.dumps(expected))
    (ROOT/'source-sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest())
    return {'real_menu_backup':True,'source_state_recorded':True}


def preserved():
    expected=json.loads((ROOT/'source-state.private.json').read_text())
    actual=json.loads(json.dumps(snapshot()))
    for name in ('settings','users','inbounds','hosts','files'):
        assert actual[name]==expected[name],name+' changed during Restore'
    if 'client_traffics' in expected:
        rows={row['id']:row for row in actual['client_traffics']}
        for row in expected['client_traffics']:
            assert row['id'] in rows
            for key in ('up','down'):assert rows[row['id']][key]>=row[key]
    assert hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()==(ROOT/'source-sha256').read_text()
    assert checks.adguard_state()==json.loads((ROOT/'backup-adguard.private.json').read_text())
    return {'credentials_users_settings_inbounds_hosts_and_files_preserved':True,
            'traffic_not_reset':True,'archive_bytes_unchanged':True,'AGH_rewrites_upstreams_preserved':True}


def restore(scenario, ref):
    assert re.fullmatch('[0-9a-f]{40}',ref)
    assert Path('/proc/1/comm').read_text().strip()=='systemd'
    clean=not Path('/etc/x-ui/x-ui.db').exists()
    if clean:
        assert not Path('/usr/local/x-ui').exists() and not Path('/opt/AdGuardHome').exists()
        assert not Path('/etc/nginx/nginx.conf').exists()
    subprocess.run(['update-ca-certificates'],check=True,capture_output=True,timeout=90)
    env={**os.environ,**json.loads((ROOT/'environment.json').read_text()),'XUI_AUTO_REF':ref,
         'CURL_CA_BUNDLE':'/etc/ssl/certs/ca-certificates.crt','TERM':'dumb','NO_COLOR':'1'}
    os.environ.update(env)
    before_dns=(os.readlink('/etc/resolv.conf'),Path('/etc/resolv.conf').read_bytes())
    selected=scenario in ('security','fail2ban','dot-outage')
    fault=None
    if scenario=='fail2ban':
        fault=Path('/etc/apt/preferences.d/restore-fail2ban-negative')
        fault.write_text('Package: fail2ban\nPin: version *\nPin-Priority: -1\n')
    blocker=None
    if scenario=='core-failure':
        import tarfile,tempfile
        with tarfile.open(ARCHIVE) as tar,tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'db';path.write_bytes(tar.extractfile('files/etc/x-ui/x-ui.db').read())
            with sqlite3.connect(path) as db:port=int(db.execute("SELECT value FROM settings WHERE key='webPort'").fetchone()[0])
        blocker=socket.socket();blocker.bind(('127.0.0.1',port));blocker.listen()
    answers=b'3\n3\n'+str(ARCHIVE).encode()+b'\nYES\n'+(b'y\ny\ny\ny\n' if selected else b'n\nn\nn\nn\n')+b'0\n'
    master,slave=pty.openpty()
    process=subprocess.Popen(['bash',str(ROOT/'x-ui-latest.sh')],stdin=slave,stdout=slave,stderr=slave,env=env)
    os.close(slave);output=bytearray();deadline=time.monotonic()+600
    os.write(master,answers)
    try:
        while time.monotonic()<deadline:
            if select.select([master],[],[],.2)[0]:
                try:part=os.read(master,65536)
                except OSError:break
                if not part:break
                output.extend(part)
            if process.poll() is not None:break
        process.wait(timeout=2)
    finally:
        if process.poll() is None:process.kill();process.wait()
        os.close(master)
        if blocker is not None:blocker.close()
        if fault is not None:fault.unlink()
        (ROOT/('restore-'+scenario+'.private.log')).write_bytes(output)
    text=output.decode(errors='replace')
    if scenario=='core-failure':
        assert process.returncode!=0 and 'core: failed_rolled_back' in text
        assert 'Overall: FAILED' in text and not Path('/etc/x-ui/x-ui.db').exists()
        assert '--configure' not in text
        return {'actual_runtime_failure_and_clean_target_recovery':True}
    expected=0 if scenario in ('preserve','security') else 1
    assert (process.returncode==0)==(expected==0),'Unexpected Restore result; private log retained'
    assert 'core: success' in text and ('Overall: SUCCESS' if expected==0 else 'Overall: PARTIAL') in text
    for name in ('dot','apt','fail2ban','ufw'):
        status='not_requested' if not selected else 'failed_rolled_back' if (name=='fail2ban' and scenario=='fail2ban') or (name=='dot' and scenario=='dot-outage') else 'configured'
        assert name+': '+status in text,name+' outcome differs'
    if not selected:
        assert (os.readlink('/etc/resolv.conf'),Path('/etc/resolv.conf').read_bytes())==before_dns
        assert 'Status: inactive' in checks.run('ufw','status')
        for p in ('/etc/systemd/resolved.conf.d/90-3x-ui-auto-nginx.conf','/etc/apt/apt.conf.d/99zz-3x-ui-auto-nginx',
                  '/etc/fail2ban/jail.d/90-3x-ui-auto-nginx.local'):
            assert not Path(p).exists(),'Unrequested Security was changed'
    else:
        assert 'Status: active' in checks.run('ufw','status')
        assert 'Automatic-Reboot-Time "04:38";' in checks.run('apt-config','dump')
        if scenario!='fail2ban':checks.run('fail2ban-client','status','sshd')
    preserved()
    return {'actual_TTY_manager_restore':True,'core_committed':True,'independent_security_results':True,
            'clean_target' if clean else 'existing_managed_target':True, 'state_and_archive_preserved':True}


def mihomo():
    """Real restored HTTPS template/provider -> official Mihomo -> origin payload."""
    import tempfile
    import urllib.request
    from urllib.parse import quote
    log=re.sub(r'\x1b\[[0-9;]*m','',(ROOT/'installer.raw.log').read_text())
    username=re.search(r'^ Username: (\S+)$',log,re.M)[1];password=re.search(r'^ Password: (\S+)$',log,re.M)[1]
    with sqlite3.connect('file:/etc/x-ui/x-ui.db?mode=ro',uri=True) as db:settings=dict(db.execute('SELECT key,value FROM settings'))
    identity=json.loads(Path('/etc/x-ui/initial-client.json').read_text())['identity']
    rule=['iptables','-t','nat','-A','OUTPUT','-d','8.8.8.9','-p','tcp','--dport','18081','-j','DNAT','--to-destination','10.0.2.2:18081']
    subprocess.run(rule,check=True,timeout=10)
    try:
        with tempfile.TemporaryDirectory(prefix='restore-mihomo-') as directory:
            root=Path(directory)
            panel=checks.load('session').Panel('https://panel.example.test'+settings['webBasePath'],username,password,root,
                ['--resolve','panel.example.test:443:127.0.0.1','-A','mihomo'])
            url=panel.request('panel/api/setting/defaultSettings',{})['obj']['subURI']+identity['subId']
            runtime=checks.load('client').fetch(panel,url).decode()
            assert 'proxy-providers:' in runtime and '?provider=1' in runtime and '${' not in runtime
            for section in ('dns','rule-providers','rules'):
                runtime=re.sub(r'^'+section+r':.*?(?=^[A-Za-z][^\n]*:|\Z)','',runtime,flags=re.M|re.S)
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));socks=sock.getsockname()[1]
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));controller=sock.getsockname()[1]
            runtime=runtime.replace('mixed-port: 10000',f'mixed-port: {socks}').replace('allow-lan: true','allow-lan: false')
            runtime=runtime.replace('      enable: true','      enable: false').replace('    type: url-test','    type: select')
            runtime+=f'\nexternal-controller: 127.0.0.1:{controller}\nbind-address: 127.0.0.1\nrules:\n  - MATCH,🌍 VPN\n'
            runtime+='hosts:\n  panel.example.test: 127.0.0.1\n  reality.example.test: 127.0.0.1\n  vision.example.test: 127.0.0.1\n'
            runtime+='tls:\n  custom-certifactes:\n    - '+json.dumps(Path('/usr/local/share/ca-certificates/pilot-pebble.crt').read_text())+'\n'
            config=root/'mihomo.yaml';config.write_text(runtime)
            with (root/'mihomo.private.log').open('wb') as output:
                process=subprocess.Popen([str(ROOT/'mihomo'),'-d',str(root/'state'),'-f',str(config)],stdout=output,stderr=subprocess.STDOUT)
                try:
                    def api(path,body=None):
                        request=urllib.request.Request(f'http://127.0.0.1:{controller}'+path,
                            data=json.dumps(body).encode() if body is not None else None,
                            method='PUT' if body is not None else 'GET',headers={'Content-Type':'application/json'})
                        with urllib.request.urlopen(request,timeout=3) as response:return response.read()
                    deadline=time.monotonic()+20
                    while True:
                        assert process.poll() is None,'Mihomo exited during provider preparation'
                        try:proxies=json.loads(api('/providers/proxies/sub'))['proxies']
                        except OSError:proxies=[]
                        if proxies:break
                        assert time.monotonic()<deadline,'Mihomo provider readiness deadline'
                        time.sleep(.05)
                    name=next(p['name'] for p in proxies if 'REALITY' in p['name'])
                    api('/proxies/'+quote('🌍 VPN',safe=''),{'name':name})
                    path='/restored-mihomo/'+os.urandom(8).hex();expected=('FRESH-INSTALL-'+path).encode()
                    response=subprocess.run(['curl','-q','-fsS','--proxy',f'socks5h://127.0.0.1:{socks}','--noproxy','',
                        '--connect-timeout','5','--max-time','30','http://8.8.8.9:18081'+path],capture_output=True,timeout=40)
                    assert response.returncode==0 and response.stdout==expected,'Mihomo restored provider byte-exact response failed'
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:process.wait(timeout=5)
                        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
    finally:
        rule[3]='-D';subprocess.run(rule,check=True,timeout=10)
    return {'real_Mihomo_restored_HTTPS_template_provider_and_payload':True}
