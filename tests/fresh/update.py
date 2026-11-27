"""Faults run only in the existing disposable Full Fresh guest."""

import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
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
COVER = Path('/var/www/html/cover-panel/index.html')
BACKEND = Path('/usr/local/lib/3x-ui-pro/mtr-backend.py')
FOREIGN = COVER.with_name('customer.txt')
ENV = json.loads((ROOT / 'environment.json').read_text())
SHA = ENV['XUI_AUTO_REF']


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


def worker(fault):
    sys.path.insert(0, str(SOURCE / 'assets/update'))
    import managed as worker

    sys.argv = [str(worker.__file__), 'worker', SHA]
    original = worker.publish
    touched = ROOT / ('hit-' + fault)

    def publish(source, dest):
        original(source, dest)
        if dest != LIMIT or touched.exists():
            return
        if fault == 'cold':
            (WORK / 'before.db').rename(WORK / 'test-held-before.db')
            touched.touch()
            os.kill(os.getpid(), signal.SIGSTOP)
        touched.touch()
        if fault == 'kill':
            os.kill(os.getpid(), signal.SIGKILL)
        if fault == 'nginx':
            # Candidate test has already passed. Actual nginx -t must now fail.
            LIMIT.write_text('invalid_directive;\n')

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

    candidate = worker.candidate_nginx

    def verify_candidate(directory, files):
        if fault == 'candidate':
            (directory / 'files' / str(LIMIT).lstrip('/')).write_text('invalid_candidate_directive;\n')
            touched.touch()
        candidate(directory, files)

    with (
        patch.object(worker, 'publish', publish),
        patch.object(worker, 'verified_health', verify),
        patch.object(worker, 'candidate_nginx', verify_candidate),
    ):
        worker.main()


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
    for fault in ('candidate', 'nginx', 'start', 'kill'):
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
    return dict(nginx_failure=True, actual_panel_start_failure=True, SIGKILL_recovered=True)


def interrupt_switch():
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


def failed_boot():
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
    (WORK / 'test-held-before.db').rename(WORK / 'before.db')
    return dict(unsafe_panel_blocked=True, journal_retained=True)


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
            "INSERT INTO hosts(inbound_id,address,port,remark,group_id,is_disabled) VALUES(?, 'custom.example',443,'extra','customer-group',1)",
            (iid,),
        )
        clients = {}
        for name in ('users', 'clients', 'client_inbounds', 'hosts'):
            columns = [r[1] for r in db.execute('PRAGMA table_info(' + name + ')')]
            clients[name] = (columns, db.execute('SELECT * FROM ' + name + ' ORDER BY rowid').fetchall())
    checks.run('systemctl', 'restart', 'x-ui')
    checks.manager_tty(b'1\n7\nn\n0\n', ROOT / 'cancel.private.log', 90)
    assert json.loads(STATE.read_bytes()) == before
    output = checks.manager_tty(b'1\n7\ny\n0\n', ROOT / 'update.private.log', 600)
    assert b'Update verified' in output
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
    return dict(pinned_update=True, user_data=True, individual_cover=True, no_op=True, migration=True)


def remove_customer_fixture():
    # Change Domains has its own, stricter contract. Return the shared guest
    # to its original topology for the existing AGH/Restore scenarios.
    checks.run('systemctl', 'stop', 'x-ui')
    with closing(sqlite3.connect(DB)) as db, db:
        db.execute("DELETE FROM hosts WHERE group_id='customer-group'")
        db.execute("DELETE FROM inbounds WHERE tag='customer-ip'")
        db.execute("DELETE FROM settings WHERE key IN ('xrayTemplateConfig','updateFixture')")
        original = json.loads((ROOT / 'pre-fixture-template').read_text())
        if original:
            db.execute("INSERT INTO settings(key,value) VALUES('xrayTemplateConfig',?)", original)
    FOREIGN.unlink()
    checks.run('systemctl', 'start', 'x-ui')
    return dict(customer_fixture_removed=True)


if __name__ == '__main__':
    worker(sys.argv[1])
