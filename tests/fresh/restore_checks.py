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


def database_state(db):
    tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result={}
    for name in ('settings','users','inbounds','hosts','client_traffics'):
        if name not in tables:continue
        columns=[row[1] for row in db.execute('PRAGMA table_info('+name+')')]
        rows=[dict(zip(columns,row)) for row in db.execute('SELECT * FROM '+name+' ORDER BY rowid')]
        if name=='inbounds':
            result['inbound_traffic']=[{key:row[key] for key in ('id','up','down','all_time') if key in row} for row in rows]
            rows=[{key:value for key,value in row.items() if key not in ('up','down','all_time')} for row in rows]
        result[name]=rows
    return result


def snapshot():
    from contextlib import closing
    with closing(sqlite3.connect('file:/etc/x-ui/x-ui.db?mode=ro',uri=True)) as db:result=database_state(db)
    result['files']={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in
        [*Path('/etc/letsencrypt/live').glob('*/*.pem'),Path('/etc/x-ui/initial-client.json'),
         Path('/etc/x-ui/auto-nginx-managed.json'),Path('/var/www/html/restore-custom.txt')]}
    return result


def archived_database_state(archive):
    import tarfile,tempfile
    from contextlib import closing
    with tarfile.open(archive) as tar,tempfile.TemporaryDirectory() as temporary:
        path=Path(temporary)/'x-ui.db'
        for suffix in ('','-wal','-shm','-journal'):
            try:member=tar.getmember('files/etc/x-ui/x-ui.db'+suffix)
            except KeyError:
                assert suffix,'Archived SQLite is missing'
                continue
            assert member.isfile()
            path.with_name(path.name+suffix).write_bytes(tar.extractfile(member).read())
        with closing(sqlite3.connect(path)) as db:return database_state(db)


def source_backup():
    Path('/var/www/html/restore-custom.txt').write_bytes(os.urandom(64))
    deadline=time.monotonic()+30
    while True:
        state=snapshot()
        if sum(row['up']+row['down'] for row in state['client_traffics'])>0:break
        assert time.monotonic()<deadline,'Real source traffic was not persisted before Backup'
        time.sleep(.1)
    with sqlite3.connect('/etc/x-ui/x-ui.db') as db:
        if not db.execute("UPDATE settings SET value='Clean Restore private fixture' WHERE key='subTitle'").rowcount:
            db.execute("INSERT INTO settings(key,value) VALUES('subTitle','Clean Restore private fixture')")
    db.close()
    checks.manager_backup()
    archive=max(Path('/var/backups/x-ui').glob('*.tar.gz'),key=lambda p:p.stat().st_mtime_ns)
    (ROOT/'source-path').write_text(str(archive))
    expected=snapshot();expected.update(archived_database_state(archive))
    (ROOT/'source-state.private.json').write_text(json.dumps(expected))
    (ROOT/'source-sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest())
    return {'real_menu_backup':True,'source_state_recorded':True}


def preserved():
    expected=json.loads((ROOT/'source-state.private.json').read_text())
    actual=json.loads(json.dumps(snapshot()))
    for name in ('settings','users','inbounds','hosts','files'):
        assert actual[name]==expected[name],name+' changed during Restore'
    for table in ('client_traffics','inbound_traffic'):
        if table not in expected:continue
        rows={row['id']:row for row in actual[table]}
        for row in expected[table]:
            assert row['id'] in rows
            if table=='client_traffics':
                for key,value in row.items():
                    if key not in ('up','down','all_time','last_online','last_sub_fetch'):
                        assert rows[row['id']][key]==value,'Client quota/identity changed'
            for key in ('up','down','all_time','last_online','last_sub_fetch'):
                if key in row:assert rows[row['id']][key]>=row[key]
    assert hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()==(ROOT/'source-sha256').read_text()
    assert checks.adguard_state()==json.loads((ROOT/'backup-adguard.private.json').read_text())
    return {'credentials_users_settings_inbounds_hosts_and_files_preserved':True,
            'traffic_not_reset':True,'archive_bytes_unchanged':True,'AGH_rewrites_upstreams_preserved':True}


def test_ssh_socket_ports():
    # Disposable target only: retain the control port and exercise a nonstandard
    # socket-activated port. Never called by the installer/Restore production code.
    assert checks.run('systemctl','show','ssh.service','-p','KillMode','--value').strip()=='process'
    Path('/etc/ssh/sshd_config.d/50-restore-ports.conf').write_text('Port 22\nPort 2222\n')
    directory=Path('/etc/systemd/system/ssh.socket.d');directory.mkdir(parents=True,exist_ok=True)
    (directory/'90-restore-ports.conf').write_text('[Socket]\nListenStream=\nListenStream=0.0.0.0:22\nListenStream=0.0.0.0:2222\n')
    checks.run('/usr/sbin/sshd','-t')
    checks.run('systemctl','stop','ssh.service','ssh.socket')
    checks.run('systemctl','daemon-reload');checks.run('systemctl','enable','--now','ssh.socket')
    assert checks.run('systemctl','is-active','ssh.socket').strip()=='active'
    # Activate ssh.service through its real socket before Security checks sshd.
    with socket.create_connection(('127.0.0.1',2222),timeout=5) as client:
        assert client.recv(200).startswith(b'SSH-2.0-')
    checks.run('/usr/sbin/sshd','-t')


def restore_outcome(output, exit_code):
    # Only the final owned summary is public diagnostics, never raw TTY output.
    summary=output.rsplit('RESTORE RESULT',1)[1] if 'RESTORE RESULT' in output else ''
    states=dict(re.findall(r'^(core|dot|apt|fail2ban|ufw): ([a-z_]+)\r?$',summary,re.M))
    allowed=('configured','preserved','not_requested','requested','not_enabled',
             'failed_rolled_back','failed_unverified','unknown')
    components={name:states.get(name,'unknown') if states.get(name,'unknown') in allowed else 'unknown'
                for name in ('dot','apt','fail2ban','ufw')}
    core=states.get('core','unknown')
    if core not in ('success','not_started','failed_rolled_back','failed_unverified'):core='unknown'
    overall=re.findall(r'^Overall: (SUCCESS|PARTIAL|FAILED)\r?$',summary,re.M)
    return {'exit':exit_code,'core':core,'overall':overall[-1] if overall else 'UNKNOWN','components':components}


def restore(scenario, ref):
    assert re.fullmatch('[0-9a-f]{40}',ref)
    assert Path('/proc/1/comm').read_text().strip()=='systemd'
    clean=not Path('/etc/x-ui/x-ui.db').exists()
    if clean:
        assert not Path('/usr/local/x-ui').exists() and not Path('/opt/AdGuardHome').exists()
        assert not Path('/etc/nginx/nginx.conf').exists()
    env={**os.environ,**json.loads((ROOT/'environment.json').read_text()),'XUI_AUTO_REF':ref,
         'CURL_CA_BUNDLE':'/etc/ssl/certs/ca-certificates.crt','TERM':'dumb','NO_COLOR':'1'}
    os.environ.update(env)
    before_dns=(os.readlink('/etc/resolv.conf'),Path('/etc/resolv.conf').read_bytes())
    security_files=('/etc/systemd/resolved.conf.d/90-3x-ui-auto-nginx.conf','/etc/apt/apt.conf.d/99zz-3x-ui-auto-nginx',
                    '/etc/fail2ban/jail.d/90-3x-ui-auto-nginx.local',
                    '/etc/systemd/system/apt-daily.timer.d/90-3x-ui-auto-nginx.conf',
                    '/etc/systemd/system/apt-daily-upgrade.timer.d/90-3x-ui-auto-nginx.conf')
    def security_state():return {name:(Path(name).read_bytes(),Path(name).stat().st_mode) if Path(name).exists() else None for name in security_files}
    original_security=security_state()
    before_ufw=checks.run('ufw','status').startswith('Status: active') if Path('/usr/sbin/ufw').exists() else False
    if scenario=='security':test_ssh_socket_ports()
    if scenario=='ufw-unknown':env['SSH_CONNECTION']='unconfirmed'

    selected=scenario in ('security','fail2ban','dot-outage')
    previous_marker=Path('/etc/x-ui/native-previous-target-marker') if scenario in ('fail2ban','dot-outage') else None
    if previous_marker is not None:
        assert not clean
        previous_marker.write_bytes(b'previous target '+os.urandom(16))
        assert previous_marker.is_file()
    fault=None
    if scenario=='fail2ban':
        fault=Path('/etc/apt/preferences.d/restore-fail2ban-negative')
        fault.write_text('Package: fail2ban\nPin: version *\nPin-Priority: -1\n')
    blocker=None
    core_marker=None
    if scenario=='core-failure':
        # Existing target, deliberately stopped before Restore: recovery must
        # return that exact service state and marker. No repeated APT bootstrap.
        if not clean:
            checks.run('systemctl','stop','x-ui')
            core_marker=Path('/etc/x-ui/native-core-target-marker')
            core_marker.write_bytes(b'previous target '+os.urandom(16))
        settings={row['key']:row['value'] for row in archived_database_state(ARCHIVE)['settings']}
        port=int(settings['webPort'])
        blocker=socket.socket();blocker.bind(('127.0.0.1',port));blocker.listen()
    choices=b'y\ny\ny\ny\n' if selected else b'n\nn\nn\nn\n'
    if scenario=='ufw-unknown':choices=b'n\nn\nn\ny\n'
    answers=b'3\n3\n'+str(ARCHIVE).encode()+b'\n'
    master,slave=pty.openpty()
    process=subprocess.Popen(['bash',str(ROOT/'x-ui-latest.sh')],stdin=slave,stdout=slave,stderr=slave,env=env)
    os.close(slave);output=bytearray();deadline=time.monotonic()+600
    prepared=False;confirmed=False;exiting=False
    os.write(master,answers)
    try:
        while time.monotonic()<deadline:
            if select.select([master],[],[],.2)[0]:
                try:part=os.read(master,65536)
                except OSError:break
                if not part:break
                output.extend(part)
                # Native APT may consume terminal input during package setup.
                # Answer the actual Restore prompt, then the returning menu.
                if not prepared and b'Type YES to prepare Restore: ' in output:
                    os.write(master,b'YES\n');prepared=True
                if not confirmed and b'Type YES to restore: ' in output:
                    os.write(master,b'YES\n'+choices);confirmed=True
                if not exiting and b'Overall: SUCCESS' in output and output.count(b'Select an option: ')>=3:
                    os.write(master,b'0\n');exiting=True
            if process.poll() is not None:break
        process.wait(timeout=2)
    finally:
        if process.poll() is None:process.kill();process.wait()
        os.close(master)
        if blocker is not None:blocker.close()
        if fault is not None:fault.unlink()
        (ROOT/('restore-'+scenario+'.private.log')).write_bytes(output)
        (ROOT/'last-restore-outcome.json').write_text(json.dumps(restore_outcome(output.decode(errors='replace'),process.returncode)))
        os.sync()  # Preserve failure evidence before the host stops its disposable VM.
    text=output.decode(errors='replace')
    assert prepared and confirmed,'Restore confirmations were not reached; private log retained'
    if scenario=='core-failure':
        if Path('/etc/x-ui/x-ui.db').exists():
            with sqlite3.connect('file:/etc/x-ui/x-ui.db?mode=ro',uri=True) as db:
                assert int(db.execute("SELECT value FROM settings WHERE key='webPort'").fetchone()[0])==port,'Fault targeted another port'
        assert process.returncode!=0 and 'core: failed_rolled_back' in text
        assert 'Overall: FAILED' in text
        if clean:assert not Path('/etc/x-ui/x-ui.db').exists()
        else:assert core_marker.is_file() and Path('/etc/x-ui/x-ui.db').is_file()
        assert subprocess.run(['systemctl','is-active','--quiet','x-ui']).returncode!=0
        assert '--configure' not in text
        return {'actual_runtime_failure_and_previous_target_recovery':True}
    expected=0 if scenario in ('preserve','security') else 1
    assert (process.returncode==0)==(expected==0),'Unexpected Restore result; private log retained'
    assert 'core: success' in text and ('Overall: SUCCESS' if expected==0 else 'Overall: PARTIAL') in text
    for name in ('dot','apt','fail2ban','ufw'):
        status='not_enabled' if scenario=='ufw-unknown' and name=='ufw' else 'not_requested' if not selected else 'failed_rolled_back' if (name=='fail2ban' and scenario=='fail2ban') or (name=='dot' and scenario=='dot-outage') else 'configured'
        assert name+': '+status in text,name+' outcome differs'
    if not selected:
        assert (os.readlink('/etc/resolv.conf'),Path('/etc/resolv.conf').read_bytes())==before_dns
        assert checks.run('ufw','status').startswith('Status: active')==before_ufw
        assert security_state()==original_security,'Unrequested Security was changed'
    else:
        assert 'Status: active' in checks.run('ufw','status')
        assert 'Automatic-Reboot-Time "04:38";' in checks.run('apt-config','dump')
        for unit in ('apt-daily.timer','apt-daily-upgrade.timer'):checks.run('systemctl','is-active','--quiet',unit)
        if scenario!='dot-outage':
            assert checks.run('busctl','get-property','org.freedesktop.resolve1','/org/freedesktop/resolve1','org.freedesktop.resolve1.Manager','DNSOverTLS').strip()=='s \"yes\"'
            checks.run('resolvectl','query','--cache=no','cloudflare.com')
        checks.run('getent','ahosts','debian.org')
        if scenario!='fail2ban':checks.run('fail2ban-client','status','sshd')
    if scenario=='dot-outage':assert (os.readlink('/etc/resolv.conf'),Path('/etc/resolv.conf').read_bytes())==before_dns
    preserved()
    if previous_marker is not None:assert not previous_marker.exists(),'Old target returned after core commit'
    return {'actual_TTY_manager_restore':True,'core_committed':True,'independent_security_results':True,
            'clean_target' if clean else 'existing_managed_target':True, 'state_and_archive_preserved':True}


def mihomo(transport='REALITY'):
    """Official HTTPS provider and unchanged credentials -> actual origin bytes."""
    assert transport in ('REALITY','Vision')
    tag=transport.lower()
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
            subprocess.run([str(ROOT/'mihomo'),'-t','-d',str(root/'state'),'-f',str(config)],
                           check=True,capture_output=True,timeout=30)
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
                    marker='reality' if transport=='REALITY' else 'xtls'
                    names=[p['name'] for p in proxies if marker in p['name'].casefold()]
                    assert len(names)==1,'Expected one installed '+transport+' provider node'
                    name=names[0]
                    api('/proxies/'+quote('🌍 VPN',safe=''),{'name':name})
                    path='/restored-mihomo/'+os.urandom(8).hex();expected=('FRESH-INSTALL-'+path).encode()
                    response=subprocess.run(['curl','-q','-fsS','--proxy',f'socks5h://127.0.0.1:{socks}','--noproxy','',
                        '--connect-timeout','5','--max-time','30','http://8.8.8.9:18081'+path],capture_output=True,timeout=40)
                    (ROOT/('mihomo-'+tag+'-response.private.bin')).write_bytes(response.stdout)
                    (ROOT/('mihomo-'+tag+'-curl.private.log')).write_bytes(response.stderr)
                    (ROOT/('mihomo-'+tag+'-status.json')).write_text(json.dumps({'exit':response.returncode,'expected_length':len(expected),'received_length':len(response.stdout)}))
                    assert response.returncode==0 and response.stdout==expected,'Mihomo restored provider byte-exact response failed'
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:process.wait(timeout=5)
                        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
                    (ROOT/('mihomo-'+tag+'.private.log')).write_bytes((root/'mihomo.private.log').read_bytes())
    finally:
        rule[3]='-D';subprocess.run(rule,check=True,timeout=10)
    return {'Mihomo_'+transport+'_client_config_validated':True,
            'Mihomo_'+transport+'_HTTPS_provider_and_byte_exact_payload':True}


def reality_observation():
    """Record the real failure, never turn it into a transport PASS."""
    status=ROOT/'mihomo-reality-status.json';log=ROOT/'mihomo-reality.private.log'
    status.unlink(missing_ok=True);log.unlink(missing_ok=True)
    result={'status':'PASS','reason':''}
    try:mihomo('REALITY')
    except AssertionError:
        metrics=json.loads(status.read_text())
        if (metrics['exit']!=52 or metrics['received_length']!=0 or
                'REALITY authentication failed' not in log.read_text()):raise
        result={'status':'FAIL','reason':'REALITY authentication failed','curl_exit':52}
    (ROOT/'mihomo-reality-observation.json').write_text(json.dumps(result))
    return {'actual_Mihomo_REALITY_outcome_recorded':True}
