"""Faults run only in the existing disposable Full Fresh guest."""

import importlib.util
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import checks

ROOT = checks.ROOT
SOURCE = ROOT / 'update-source'
DB = Path('/etc/x-ui/x-ui.db')
STATE = DB.with_name('auto-nginx-managed.json')
WORK = Path('/var/lib/3x-ui-auto-nginx/update')
JOURNAL = WORK.parent / 'change-domains/journal.json'
LIMIT = Path('/etc/nginx/snippets/xui-cover-limit.conf')
NGX_RECOVERY_SITE = Path('/etc/nginx/conf.d/pilot-recovery.conf')
NGX_RECOVERY_SOCKET = Path('/run/pilot-recovery-nginx.sock')
COVER = Path('/var/www/html/cover-panel/index.html')
BACKEND = Path('/usr/local/lib/3x-ui-pro/mtr-backend.py')
ROUTES = Path('/etc/nginx/snippets/includes.conf')
FOREIGN = COVER.with_name('customer.txt')
ENV = json.loads((ROOT / 'environment.json').read_text())
SHA = ENV['XUI_AUTO_REF']


def cdn_module():
    spec = importlib.util.spec_from_file_location('cdn', SOURCE / 'assets/xhttp-cdn/managed.py')
    cdn = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cdn)
    return cdn


def prepare_base_cdn():
    """Populate the CDN profile for the migration without overriding the installer preflight."""
    old_running()
    manifest = json.loads(STATE.read_bytes())
    assert 'xhttp_cdn' not in manifest['inbound_roles']
    cdn = cdn_module()
    cred = credentials()
    domain = manifest['domains']['panel']
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    path = '/UpdateBaseFixture/cookie'
    snippet = Path('/etc/nginx/snippets/x-ui-auto-optional/xhttp-cdn.conf')
    with tempfile.TemporaryDirectory() as directory:
        panel = checks.load('session').Panel(
            cred['url'], cred['username'], cred['password'], Path(directory),
            ['--resolve', domain + ':443:127.0.0.1'],
        )
        assert cdn.configure(panel, domain, port, path, '🌐', snippet) == 'configured'
    with closing(sqlite3.connect(DB)) as db:
        db.row_factory = sqlite3.Row
        inbound = db.execute('SELECT id,enable FROM inbounds WHERE tag=?', (f'inbound-{port}',)).fetchone()
        assert not inbound['enable']
        iid = inbound['id']
        host = db.execute('SELECT * FROM hosts WHERE inbound_id=?', (iid,)).fetchone()
    manifest['inbound_roles']['xhttp_cdn'] = iid
    manifest['features']['xhttp_cdn'] = True
    manifest['xhttp_cdn'] = dict(port=port, path=path, host=dict(host), snippet=snippet.read_text())
    STATE.write_text(json.dumps(manifest))
    return dict(cdn_fixture_disabled=True)


def old_running():
    assert json.loads(STATE.read_bytes())['source_ref'] == (ROOT / 'update-base-ref').read_text().strip()
    assert '3.10.2' in checks.run('/usr/local/x-ui/x-ui', '-v')
    assert not JOURNAL.exists()
    for unit in ('x-ui', 'nginx', 'mtr-backend', 'certbot.timer', 'ssh'):
        checks.run('systemctl', 'is-active', '--quiet', unit)


def invoke_fault(fault, *, background=False):
    # No simulated services. Faults are injected by the guest's test process
    # at the exact systemd/file operation, never through a production option.
    shell = 'source /root/pilot/update-source/x-ui-latest.sh; acquire_stack_lock || exit $?; exec python3 /root/pilot/update.py "$1"'
    cmd = [
        'systemd-run',
        '--unit=xui-update-fixture',
        '--collect',
        '--property=OnFailure=xui-update-recover.service',
    ]
    if not background:
        cmd.append('--wait')
    for key in ('HTTPS_PROXY', 'HTTP_PROXY', 'https_proxy', 'http_proxy'):
        if key in ENV:
            cmd.append('--setenv=' + key)
    return subprocess.run(
        [*cmd, '/bin/bash', '-c', shell, 'bash', fault],
        env={**os.environ, **ENV},
        timeout=480,
        capture_output=True,
    ).returncode


def migration_differences(db, before):
    """Describe every changed field by its public SQLite schema position."""
    def quoted(name):
        return '"' + name.replace('"', '""') + '"'

    db.execute('ATTACH DATABASE ? AS audit_before', (before.as_uri() + '?mode=ro',))
    try:
        tables = db.execute(
            "SELECT name FROM audit_before.sqlite_schema "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for table_index, (name,) in enumerate(tables):
            table = quoted(name)
            fields = db.execute(f'PRAGMA audit_before.table_info({table})').fetchall()
            old = f'audit_before.{table}'
            new = f'main.{table}'
            keys = [quoted(field[1]) for field in fields if field[5]] or ['rowid']
            join = ' AND '.join(f'b.{key} IS a.{key}' for key in keys)
            projection = ','.join(quoted(field[1]) for field in fields)
            try:
                if db.execute(f'SELECT {projection} FROM {old} EXCEPT SELECT {projection} FROM {new} LIMIT 1').fetchone():
                    if db.execute(f'SELECT 1 FROM {old} AS a WHERE NOT EXISTS '
                                  f'(SELECT 1 FROM {new} AS b WHERE {join}) LIMIT 1').fetchone():
                        yield f'{table_index}:row'
                    for index, name, *_ in fields:
                        column = quoted(name)
                        if db.execute(f'SELECT 1 FROM {old} AS a JOIN {new} AS b ON {join} '
                                      f'WHERE a.{column} IS NOT b.{column} LIMIT 1').fetchone():
                            yield f'{table_index}:{index}'
            except sqlite3.OperationalError:
                yield f'{table_index}:schema'
    finally:
        db.execute('DETACH DATABASE audit_before')


def inject_nginx_fault(worker, touched):
    journal = worker.record()
    if journal and journal['phase'] == 'switching' and not touched.exists():
        # The native SQLite migration has passed. Fail the live nginx -t.
        LIMIT.write_text('invalid_directive;\n')
        touched.touch()


def worker(fault):
    sys.path.insert(0, str(SOURCE / 'assets/update'))
    import managed as worker

    sys.argv = [str(worker.__file__), 'worker', SHA]
    original = worker.publish
    touched = ROOT / ('hit-' + fault)

    def publish(source, dest):
        original(source, dest)
        if fault == 'nginx' or dest != LIMIT or touched.exists():
            return
        if fault == 'cold':
            (WORK / 'before.db').rename(WORK / 'test-held-before.db')
            # The host kills QEMU without flushing the guest filesystem.
            worker.fsync(WORK)
            touched.touch()
            os.kill(os.getpid(), signal.SIGSTOP)
        touched.touch()
        if fault == 'kill':
            os.kill(os.getpid(), signal.SIGKILL)

    reload = worker.reload_nginx

    def reload_nginx():
        if fault == 'nginx':
            inject_nginx_fault(worker, touched)
        reload()

    migration = worker.verify_migration

    def verify_migration(db, before):
        try:
            migration(db, before)
        except (ValueError, sqlite3.Error):
            with closing(sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True)) as audit:
                positions = ','.join(migration_differences(audit, before))
                (ROOT / ('update-migration-' + fault)).write_text(positions)
            raise

    check = worker.verified_health

    def verify(state, target=True):
        check(state, target)
        if fault == 'external' and target:
            touched.touch()
            until = time.monotonic() + 120
            while not (ROOT / 'write-completed').exists():
                if time.monotonic() > until:
                    raise RuntimeError('Timed out waiting for external panel client')
                time.sleep(0.2)
            raise RuntimeError('Injected failure after an external write')

    refresh_mtr = worker.refresh_mtr

    def restart_mtr(j):
        if fault == 'mtr':
            assert j['phase'] == 'start_intent'
            touched.touch()
            os.kill(os.getpid(), signal.SIGKILL)
        refresh_mtr(j)

    candidate = worker.candidate_nginx

    def verify_candidate(directory, files):
        if fault == 'candidate':
            (directory / 'files' / str(LIMIT).lstrip('/')).write_text('invalid_candidate_directive;\n')
            touched.touch()
        candidate(directory, files)

    with (
        patch.object(worker, 'publish', publish),
        patch.object(worker, 'reload_nginx', reload_nginx),
        patch.object(worker, 'verify_migration', verify_migration),
        patch.object(worker, 'verified_health', verify),
        patch.object(worker, 'refresh_mtr', restart_mtr),
        patch.object(worker, 'candidate_nginx', verify_candidate),
    ):
        try:
            worker.main()
        except Exception as error:
            # Retain source coordinates for the final error and its context.
            # The exception text and arguments may contain private data.
            assets = SOURCE / 'assets'
            public = {assets / f'update/{name}.py' for name in ('managed', 'database', 'resources')}
            frames = []
            for cause in (error, error.__cause__ or error.__context__):
                if cause is None:
                    continue
                frames.extend(
                    f'{Path(frame.filename).name}:{frame.lineno}'
                    for frame in traceback.extract_tb(cause.__traceback__)
                    if Path(frame.filename) in public
                )
            (ROOT / ('update-trace-' + fault)).write_text(','.join(frames[-6:]))
            raise


def rollback_cases():
    old_running()
    WORK.parent.mkdir(mode=0o700, exist_ok=True)
    original = STATE.read_bytes()
    backups = set(Path('/var/backups/x-ui').glob('*.tar.gz'))
    try:
        legacy = json.loads(original)
        legacy.pop('update_contract')
        STATE.write_text(json.dumps(legacy))
        log = ROOT / 'legacy.private.log'
        try:
            checks.manager_tty(b'1\n7\n', log, 45)
        except AssertionError:
            assert b'update_contract 1' in log.read_bytes()
        else:
            raise AssertionError('Legacy manager operation accepted')
        assert set(Path('/var/backups/x-ui').glob('*.tar.gz')) == backups
    finally:
        STATE.write_bytes(original)
    LIMIT.write_bytes(LIMIT.read_bytes().replace(b'burst=80 ', b'burst=79 '))
    old = LIMIT.read_bytes()
    faults = ('candidate', 'nginx', 'start', 'kill')
    for fault in faults:
        try:
            rollback_case(fault, old)
        except Exception:
            print(f'CI Update recovery {fault}: FAIL', flush=True)
            if JOURNAL.exists():
                try:
                    journal_phase = json.loads(JOURNAL.read_text())['phase']
                except (OSError, ValueError, KeyError, TypeError):
                    journal_phase = 'unknown'
                if journal_phase in ('prepared', 'draining', 'switching', 'start_intent', 'compare_target',
                                     'rollback', 'target_retained', 'old_ready', 'verified', 'committed'):
                    print(f'CI Update journal {fault}: {journal_phase}', flush=True)
            trace = ROOT / ('update-trace-' + fault)
            if trace.exists():
                print(f'CI Update trace {fault}: {trace.read_text()}', flush=True)
            migration = ROOT / ('update-migration-' + fault)
            if migration.exists():
                print(f'CI Update migration {fault}: {migration.read_text()}', flush=True)
            raise
        print(f'CI Update recovery {fault}: PASS', flush=True)
    return dict.fromkeys(faults, True)


def rollback_case(fault, old):
    drop = Path('/etc/systemd/system/x-ui.service.d/95-update-test.conf')
    if fault == 'start':
        drop.parent.mkdir(exist_ok=True)
        fail = ROOT / 'fail-target-once'
        fail.write_text(
            '#!/usr/bin/python3\nimport json,sys\nfrom pathlib import Path\n'
            f'j=Path({str(JOURNAL)!r})\n'
            'if j.exists() and json.loads(j.read_bytes()).get("phase")=="start_intent":\n'
            '    Path("/root/pilot/failed-actual-start").touch()\n    sys.exit(1)\n'
        )
        fail.chmod(0o700)
        drop.write_text('[Service]\nExecStartPre=+' + str(fail) + '\n')
        checks.run('systemctl', 'daemon-reload')
    try:
        assert invoke_fault(fault) != 0
        deadline = time.monotonic() + 180
        while JOURNAL.exists() and time.monotonic() < deadline:
            time.sleep(0.5)
        assert (ROOT / ('hit-' + fault)).exists()
        if fault == 'start':
            assert (ROOT / 'failed-actual-start').exists()
        old_running()
        assert LIMIT.read_bytes() == old
    finally:
        if fault == 'start':
            drop.unlink()
            checks.run('systemctl', 'daemon-reload')


def interrupt_switch():
    # This independent vhost must boot even when x-ui remains blocked.
    listener = f'listen unix:{NGX_RECOVERY_SOCKET};'
    NGX_RECOVERY_SITE.write_text(
        'server { '+listener+' location / { return 200 "foreign-site"; } }\n'
    )
    checks.run('sync', str(NGX_RECOVERY_SITE), str(NGX_RECOVERY_SITE.parent))
    assert listener in checks.run('nginx', '-T')
    assert invoke_fault('cold', background=True) == 0
    deadline = time.monotonic() + 300
    while not (ROOT / 'hit-cold').exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert (ROOT / 'hit-cold').exists() and json.loads(JOURNAL.read_bytes())['phase'] == 'switching'
    return dict(partially_published=True)


def verify_cold_recovery():
    deadline = time.monotonic() + 180
    while JOURNAL.exists() and time.monotonic() < deadline:
        time.sleep(0.5)
    old_running()
    assert b'burst=79 ' in LIMIT.read_bytes()
    return dict(coherent_old_stack=True)


def verify_recovery_site():
    assert NGX_RECOVERY_SOCKET.is_socket()
    assert checks.run('curl', '-qfsS', '--noproxy', '*', '--unix-socket', str(NGX_RECOVERY_SOCKET),
                      'http://localhost/') == 'foreign-site'


def failed_boot():
    assert (WORK / 'test-held-before.db').is_file() and not (WORK / 'before.db').exists()
    assert NGX_RECOVERY_SITE.is_file()
    until = time.monotonic() + 90
    while (
        checks.run('systemctl', 'show', 'xui-update-files', '-p', 'ActiveState', '--value').strip()
        != 'failed'
    ):
        if time.monotonic() > until:
            raise RuntimeError('Damaged before-image did not stop boot recovery')
        time.sleep(0.2)
    assert (
        JOURNAL.exists()
        and checks.run('systemctl', 'show', 'x-ui', '-p', 'MainPID', '--value').strip() == '0'
    )
    while checks.run('systemctl', 'show', 'nginx', '-p', 'ActiveState', '--value').strip() != 'active':
        if time.monotonic() > until:
            raise RuntimeError('SQLite recovery failure prevented independent nginx startup')
        time.sleep(0.2)
    verify_recovery_site()
    assert checks.run('systemctl', 'show', 'x-ui', '-p', 'MainPID', '-p',
                      'ExecMainStartTimestampMonotonic', '--value').split() == ['0', '0']
    NGX_RECOVERY_SITE.unlink()
    (WORK / 'test-held-before.db').rename(WORK / 'before.db')
    return dict(unsafe_panel_blocked=True, journal_retained=True, independent_nginx_site=True)


def credentials():
    log = re.sub(r'\x1b\[[0-9;]*m', '', (ROOT / 'installer.raw.log').read_text())
    with closing(sqlite3.connect(DB)) as db:
        path = db.execute("SELECT value FROM settings WHERE key='webBasePath'").fetchone()[0]
    return dict(
        url='https://panel.example.test' + path,
        username=re.search(r'^ +Username: +(\S+)$', log, re.M)[1],
        password=re.search(r'^ +Password: +(\S+)$', log, re.M)[1],
    )


def await_external():
    before = json.loads(STATE.read_bytes())
    before['source_ref'] = (ROOT / 'update-base-ref').read_text().strip()
    STATE.write_text(json.dumps(before))
    with closing(sqlite3.connect(DB)) as db, db:
        db.execute(
            "UPDATE inbounds SET stream_settings=json_set(stream_settings,'$.hysteriaSettings.udpIdleTimeout',59) WHERE id=?",
            (before['inbound_roles']['hysteria2'],),
        )
    assert invoke_fault('external', background=True) == 0
    until = time.monotonic() + 300
    while not (ROOT / 'hit-external').exists():
        if time.monotonic() > until:
            raise RuntimeError('Target did not reach its working health checks')
        time.sleep(0.2)
    assert json.loads(JOURNAL.read_bytes())['phase'] == 'start_intent'
    return dict(actual_panel_serving=True)


def retained():
    (ROOT / 'write-completed').touch()
    until = time.monotonic() + 90
    while json.loads(JOURNAL.read_bytes())['phase'] != 'target_retained':
        if time.monotonic() > until:
            raise RuntimeError('Target was not retained')
        time.sleep(0.2)
    for path, count in ((DB, 1), (WORK / 'target.db', 1), (WORK / 'before.db', 0)):
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            assert db.execute(
                "SELECT count(*) FROM hosts WHERE remark='external-update-case'"
            ).fetchone() == (count,)
    assert json.loads(STATE.read_bytes())['source_ref'] == (ROOT / 'update-base-ref').read_text().strip()
    return dict(external_write_preserved=True, both_databases_retained=True)


def change_transport_fixture(db, roles, keys):
    replacements = []
    originals = []
    flows = db.execute('SELECT flow_override,client_id,inbound_id FROM client_inbounds WHERE inbound_id=?',
                       (roles['ws'],)).fetchall()
    for role, group, field, value in (
        ('ws', 'wsSettings', 'path', '/custom-ws'),
        ('grpc', 'grpcSettings', 'serviceName', '/audit/custom-rpc|custom-rpc-multi'),
    ):
        iid = roles[role]
        settings, stream = db.execute('SELECT settings,stream_settings FROM inbounds WHERE id=?', (iid,)).fetchone()
        originals.append((settings, stream, iid))
        options, transport = json.loads(settings), json.loads(stream)
        previous = transport[group][field]
        transport[group][field] = value
        replacements.append((previous.split('|')[0], value.split('|')[0]))
        if role == 'ws':
            options.update(keys)
            options.pop('fallbacks', None)
            for client in options['clients']:
                client['flow'] = 'xtls-rprx-vision'
            db.execute("UPDATE client_inbounds SET flow_override='xtls-rprx-vision' WHERE inbound_id=?", (iid,))
        db.execute('UPDATE inbounds SET settings=?,stream_settings=? WHERE id=?',
                   (json.dumps(options), json.dumps(transport), iid))
    original = ROUTES.read_text()
    changed = original
    for before, after in replacements:
        assert changed.count(f'location = {before} {{') == 1
        changed = changed.replace(f'location = {before} {{', f'location = {after} {{')
    (ROOT / 'pre-fixture-inbounds').write_text(json.dumps({'inbounds': originals, 'flows': flows}))
    ROUTES.write_text(changed)


def save_transport_profiles(keys):
    sys.path.insert(0, str(SOURCE / 'assets/update'))
    from resources import module
    from urllib.parse import quote, urlsplit

    with closing(sqlite3.connect(DB)) as db:
        settings = dict(db.execute("SELECT key,value FROM settings WHERE key IN ('subJsonURI','subPort')"))
    until = time.monotonic() + 15
    while True:
        try:
            with socket.create_connection(('127.0.0.1', int(settings['subPort'])), timeout=1):
                break
        except OSError:
            if time.monotonic() >= until:
                raise RuntimeError('Historical subscription listener did not start') from None
            time.sleep(0.1)
    sub_id = json.loads(DB.with_name('initial-client.json').read_text())['identity']['subId']
    url = urlsplit(settings['subJsonURI'])
    cert = '/etc/letsencrypt/live/' + url.hostname + '/fullchain.pem'
    op = module('domains').Operation('', '')
    code, body = op.request(url.hostname, url.path + quote(sub_id, safe=''), cert)
    print(f'CI Update saved profile HTTP: {code}', flush=True)
    assert code == 200
    profiles = {}
    for config in json.loads(body):
        out = next((out for out in config['outbounds'] if out.get('protocol') in ('vless', 'trojan')), {})
        network = out.get('streamSettings', {}).get('network')
        if network in ('ws', 'grpc'):
            name = 'Encrypted-WebSocket' if network == 'ws' else 'Custom-gRPC'
            if network == 'ws':
                assert keys['encryption'] in json.dumps(out)
            profiles[name] = config
    assert set(profiles) == {'Encrypted-WebSocket', 'Custom-gRPC'}
    (ROOT / 'pre-update-edge-profiles.json').write_text(json.dumps(profiles))


def verify_mtr_recovery():
    invocation = lambda: checks.run('systemctl', 'show', 'mtr-backend', '-p', 'InvocationID', '--value')
    before = BACKEND.read_bytes()
    BACKEND.write_bytes(before + b'\n# Test previous MTR process\n')
    checks.run('systemctl', 'restart', 'mtr-backend')
    previous = invocation()
    assert invoke_fault('mtr', background=True) == 0
    until = time.monotonic() + 180
    while not (ROOT / 'hit-mtr').exists() or JOURNAL.exists():
        if time.monotonic() > until:
            raise RuntimeError('MTR recovery did not complete')
        time.sleep(0.2)
    assert invocation() != previous and BACKEND.read_bytes() == before
    checks.run('systemctl', 'is-active', '--quiet', 'mtr-backend')
    return dict(mtr_restarted_after_interrupt=True)


def project_update():
    old_running()
    before = json.loads(STATE.read_bytes())
    covers = DB.with_name('cover-sites.json').read_bytes()
    public = COVER.read_bytes()
    old_backend = BACKEND.read_bytes()
    FOREIGN.write_text('private customer content')
    COVER.write_bytes(b'previous template\n')
    BACKEND.write_bytes(old_backend + b'\n# previous integration\n')
    with closing(sqlite3.connect(DB)) as db, db:
        original = db.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig'").fetchone()
        (ROOT / 'pre-fixture-template').write_text(json.dumps(original))
        iid = before['inbound_roles']['hysteria2']
        cred = credentials()
        with tempfile.TemporaryDirectory() as tmp:
            panel = checks.load('session').Panel(
                cred['url'],
                cred['username'],
                cred['password'],
                Path(tmp),
                ['--resolve', 'panel.example.test:443:127.0.0.1'],
            )
            reply = panel.request('panel/api/xray/', method='POST')['obj']
            if isinstance(reply, str):
                reply = json.loads(reply)
            old_template = reply['xraySetting']
            if isinstance(old_template, str):
                old_template = json.loads(old_template)
            keys = cdn_module().new_vless_keys(panel)
        change_transport_fixture(db, before['inbound_roles'], keys)
        db.execute(
            "UPDATE inbounds SET stream_settings=json_set(stream_settings,'$.hysteriaSettings.udpIdleTimeout',59) WHERE id=?",
            (iid,),
        )
        old_template['outbounds'].append(dict(tag='fixture-cascade', protocol='freedom', settings={}))
        old_template['routing']['rules'].append(
            dict(type='field', domain=['domain:fixture.example'], outboundTag='fixture-cascade')
        )
        db.execute("INSERT INTO settings(key,value) VALUES('updateFixture','preserve')")
        db.execute("DELETE FROM settings WHERE key='xrayTemplateConfig'")
        db.execute(
            "INSERT INTO settings(key,value) VALUES('xrayTemplateConfig',?)", (json.dumps(old_template),)
        )
        address = json.loads(checks.run('ip', '-j', '-4', 'route', 'get', '10.0.2.2'))[0]['prefsrc']
        db.execute(
            """INSERT INTO inbounds(user_id,enable,tag,remark,protocol,listen,port,settings,stream_settings,sniffing)
                      VALUES(1,1,'customer-ip','customer','mixed',?,23988,'{"auth":"noauth"}','{"network":"tcp"}','{}')""",
            (address,),
        )
        db.execute(
            """INSERT INTO hosts(inbound_id,address,port,remark,group_id,is_disabled,verify_peer_cert_by_name)
               VALUES(?, 'custom.example',443,'extra','customer-group',1,'cert.custom.example')""",
            (iid,),
        )
        clients = {}
        for name in ('users', 'clients', 'client_inbounds', 'hosts'):
            columns = [r[1] for r in db.execute('PRAGMA table_info(' + name + ')')]
            clients[name] = (columns, db.execute('SELECT * FROM ' + name + ' ORDER BY rowid').fetchall())
        cdn_id = before['inbound_roles']['xhttp_cdn']
        cdn_before = db.execute('SELECT enable,settings FROM inbounds WHERE id=?', (cdn_id,)).fetchone()
        assert cdn_before[0] == 0
    checks.run('systemctl', 'restart', 'x-ui')
    checks.run('nginx', '-t')
    checks.run('systemctl', 'reload', 'nginx')
    checks.manager_tty(b'1\n7\nn\n0\n', ROOT / 'cancel.private.log', 90)
    assert json.loads(STATE.read_bytes()) == before
    save_transport_profiles(keys)
    stages = set(WORK.parent.glob('source.*'))
    output = checks.manager_tty(b'1\n7\ny\n0\n', ROOT / 'update.private.log', 600)
    assert b'Update verified' in output
    assert set(WORK.parent.glob('source.*')) == stages
    after = json.loads(STATE.read_bytes())
    for key in before.keys() - {'source_ref', 'xhttp_cdn'}:
        assert after[key] == before[key]
    assert (
        after['source_ref'] == SHA and after['xhttp_cdn']['host']['id'] == before['xhttp_cdn']['host']['id']
    )
    assert DB.with_name('cover-sites.json').read_bytes() == covers and COVER.read_bytes() == public
    assert FOREIGN.read_text() == 'private customer content' and BACKEND.read_bytes() == old_backend
    assert b'burst=80 ' in LIMIT.read_bytes()
    assert address + ':23988' in checks.run('ss', '-H', '-lntp')
    with closing(sqlite3.connect(DB)) as db:
        transport = json.loads(db.execute('SELECT settings FROM inbounds WHERE id=?',
                                         (before['inbound_roles']['ws'],)).fetchone()[0])
        assert all(transport[key] == value for key, value in keys.items()) and 'fallbacks' not in transport
        cdn_after = db.execute('SELECT enable,settings FROM inbounds WHERE id=?', (cdn_id,)).fetchone()
        assert cdn_after[0] == 0 and json.loads(cdn_after[1]) == json.loads(cdn_before[1])
        for name, (columns, values) in clients.items():
            assert (
                db.execute('SELECT ' + ','.join(columns) + ' FROM ' + name + ' ORDER BY rowid').fetchall()
                == values
            )
        assert db.execute(
            "SELECT json_extract(stream_settings,'$.hysteriaSettings.udpIdleTimeout') FROM inbounds WHERE id=?",
            (iid,),
        ).fetchone() == (60,)
        assert (
            json.loads(db.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig'").fetchone()[0])
            == old_template
        )
        assert db.execute("SELECT value FROM settings WHERE key='updateFixture'").fetchone() == ('preserve',)

    def starts():
        return [
            checks.run('systemctl', 'show', u, '-p', 'InvocationID', '--value')
            for u in ('x-ui', 'nginx', 'mtr-backend')
        ]

    previous = starts()
    output = checks.manager_tty(b'1\n7\ny\n0\n', ROOT / 'repeat.private.log', 300)
    assert b'Already up to date' in output and starts() == previous
    return dict(pinned_update=True, user_data=True, individual_cover=True, no_op=True, migration=True,
                vless_encryption_preserved=True, source_stage_cleaned=True)


def remove_customer_fixture():
    # Change Domains has its own, stricter contract. Return the shared guest
    # to its original topology for the existing AGH/Restore scenarios.
    checks.run('systemctl', 'stop', 'x-ui')
    with closing(sqlite3.connect(DB)) as db, db:
        recorded = json.loads((ROOT / 'pre-fixture-inbounds').read_text())
        db.executemany('UPDATE inbounds SET settings=?,stream_settings=? WHERE id=?',
                       recorded['inbounds'])
        db.executemany('UPDATE client_inbounds SET flow_override=? WHERE client_id=? AND inbound_id=?',
                       recorded['flows'])
        db.execute("DELETE FROM hosts WHERE group_id='customer-group'")
        db.execute("DELETE FROM inbounds WHERE tag='customer-ip'")
        db.execute("DELETE FROM settings WHERE key IN ('xrayTemplateConfig','updateFixture')")
        original = json.loads((ROOT / 'pre-fixture-template').read_text())
        if original:
            db.execute("INSERT INTO settings(key,value) VALUES('xrayTemplateConfig',?)", original)
    FOREIGN.unlink()
    checks.run('systemctl', 'start', 'x-ui')
    # Reconcile the original public paths before resuming shared AGH tests.
    output = checks.manager_tty(b'1\n7\ny\n0\n', ROOT / 'restore-transport.private.log', 300)
    assert b'Update verified' in output
    return dict(customer_fixture_removed=True)


if __name__ == '__main__':
    worker(sys.argv[1])
