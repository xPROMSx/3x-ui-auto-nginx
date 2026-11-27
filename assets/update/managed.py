# 3X-UI AUTO NGINX — Copyright (C) 2026 xPROMSx contributors
# SPDX-License-Identifier: GPL-3.0-only
# Additional attribution terms: see docs/NOTICE.md
"""In-place Update. The journal is write-ahead and lives outside Restore trees."""

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import database
from resources import (
    DB,
    DRIVER,
    MANIFEST,
    NGINX,
    PANEL,
    ROOT,
    candidate_nginx,
    ensure_parent,
    file_info,
    fsync,
    metadata,
    module,
    render,
    run,
    sync_tree,
    versions,
)

BASE = Path('/var/lib/3x-ui-auto-nginx')
WORK = BASE / 'update'
JOURNAL = BASE / 'change-domains/journal.json'
SYSTEM = Path('/etc/systemd/system')
COHERENT_PHASES = ('prepared', 'old_ready', 'verified', 'committed', 'target_retained')
BOOT = 'xui-update-files.service'
FINISH = 'xui-update-recover.service'
GUARDS = ('x-ui.service', 'nginx.service', 'mtr-backend.service', 'certbot.service', 'certbot.timer')


def write(path, data, mode=0o600):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        try:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            os.replace(tmp, path)
            fsync(path.parent)
        finally:
            tmp.unlink(missing_ok=True)


def save(j, phase):
    j['phase'] = phase
    write(JOURNAL, json.dumps(j, separators=(',', ':')).encode())


def record():
    if JOURNAL.exists():
        j = json.loads(JOURNAL.read_text())
        if j.get('operation') == 'update':
            return j
    return None


def prop(unit, key):
    return run('systemctl', 'show', unit, '-p', key, '--value').decode().strip()


def service(action, unit):
    run('systemctl', action, unit, timeout=150)


def stopped():
    if prop('x-ui', 'ActiveState') not in ('inactive', 'failed') or prop('x-ui', 'MainPID') != '0':
        raise RuntimeError('x-ui.service has not stopped')
    group = prop('x-ui', 'ControlGroup')
    if group:
        root = Path('/sys/fs/cgroup') / group.lstrip('/')
        if any(p.read_text().strip() for p in root.rglob('cgroup.procs')):
            raise RuntimeError('x-ui.service still has running processes')


def connect(ro=True):
    return sqlite3.connect(DB.as_uri() + ('?mode=ro' if ro else '?mode=rw'), uri=True)


def installation(state):
    with closing(connect()) as db:
        rows = database.read_roles(db, state)
        params = database.parameters(state, dict(db.execute('SELECT key,value FROM settings')), rows)
        wanted = database.definitions(module, state, rows, params)
        return rows, params, wanted, database.changes(rows, wanted)


def publish(source, target):
    ensure_parent(target)
    missing = []
    d = target.parent
    while not d.exists():
        missing.append(d)
        d = d.parent
    for d in reversed(missing):
        d.mkdir(mode=0o755)
        d.chmod(0o755)
        fsync(d.parent)
    with tempfile.TemporaryDirectory(prefix='.xui-publish-', dir=target.parent) as tmp:
        p = Path(tmp) / 'leaf'
        run('cp', '-a', '--', source, p)
        if not p.is_symlink():
            fsync(p)
        os.replace(p, target)
    fsync(target.parent)


def snapshot_files(paths, archive):
    # Backup already contains the large executables. Reuse just the leaves
    # that are still identical at cutover, with a durable link to the archive.
    candidates = {str(PANEL / 'x-ui'), *(str(p) for p in (PANEL / 'bin').glob('xray-linux-*'))} & set(paths)
    archived = {}
    if candidates:
        with tarfile.open(archive, 'r|gz') as source:
            for member in source:
                path = member.name.removeprefix('files')
                if (
                    path in candidates
                    and member.isfile()
                    and file_info(Path(path)) == ('file', member.mode, member.uid, member.gid)
                ):
                    with source.extractfile(member) as f:
                        stamp = hashlib.file_digest(f, 'sha256').hexdigest()
                    if stamp == database.digest(Path(path)):
                        archived[path] = {'kind': 'file', 'member': member.name, 'digest': stamp}
        if archived:
            try:
                os.link(archive, WORK / 'backup.tar.gz')
            except OSError:
                archived.clear()  # Separate /var/backups filesystem: use local leaf copies.
    files = dict(archived)
    for index, name in enumerate(paths):
        if name in archived:
            continue
        p = Path(name)
        info = file_info(p)
        files[name] = {'kind': info[0]}
        if info[0] != 'absent':
            stored = WORK / 'before' / str(index)
            run('cp', '-a', '--', p, stored)
            files[name]['copy'] = stored.name
    fsync(WORK)
    sync_tree(WORK / 'before')
    return files


def restore_archived_files(files):
    selected = {v['member']: (Path(k), v['digest']) for k, v in files.items() if 'member' in v}
    if not selected:
        return
    with tarfile.open(WORK / 'backup.tar.gz', 'r|gz') as archive:
        for member in archive:
            if member.name not in selected:
                continue
            target, expected = selected.pop(member.name)
            leaf = WORK / 'before/archived-leaf'
            with archive.extractfile(member) as src, leaf.open('wb') as dest:
                shutil.copyfileobj(src, dest)
            if database.digest(leaf) != expected:
                raise RuntimeError(f'Archived binary checksum mismatch: {target}')
            os.chown(leaf, member.uid, member.gid)
            leaf.chmod(member.mode)
            publish(leaf, target)
    if selected:
        raise RuntimeError('Missing Update binary in retained Backup v3')


def tune_runtime(tuning, forward):
    key = tuning['key']
    current = int(run('sysctl', '-n', key))
    desired = tuning['after' if forward else 'before']
    if current == desired:
        return
    if current != tuning['before' if forward else 'after']:
        raise RuntimeError(f'{key} changed independently')
    run('sysctl', '-q', '-w', f'{key}={desired}')
    if int(run('sysctl', '-n', key)) != desired:
        raise RuntimeError(f'{key} was not applied')


def capture_db(target):
    with database.stopped_database(DB, stopped):
        publish(DB, target)
        return database.digest(DB)


def backup():
    data = run('bash', DRIVER, 'backup', lock=True, timeout=3600)
    result = json.loads(data)
    if result['status'] != 'success':
        raise RuntimeError('Backup v3 did not return a verified archive')
    archive = Path(result['archive'])
    fsync(archive)
    fsync(archive.parent)
    return str(archive)


def create_units():
    # Installed before the journal or any temporary service stop. Source and
    # recovery tools stay on persistent storage until after journal removal.
    shell = f'/bin/bash {DRIVER}'
    check = f'/usr/bin/python3 -I {Path(__file__).resolve()}'
    write(
        SYSTEM / BOOT,
        f'''[Unit]
Description=Recover coherent x-ui Update files before starting services
After=local-fs.target
DefaultDependencies=no
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={shell} boot
'''.encode(),
        0o644,
    )
    write(
        SYSTEM / FINISH,
        f'''[Unit]
Description=Finish or roll back x-ui Update
After={BOOT} x-ui.service nginx.service mtr-backend.service
[Service]
Type=oneshot
ExecStart={shell} recover
TimeoutStartSec=20min
'''.encode(),
        0o644,
    )
    for unit in GUARDS:
        folder = SYSTEM / (unit + '.d')
        folder.mkdir(mode=0o755, exist_ok=True)
        # A failed SQLite recovery must not take independent nginx sites down.
        dependency = 'Wants' if unit == 'nginx.service' else 'Requires'
        data = f'[Unit]\n{dependency}={BOOT}\nAfter={BOOT}\n'
        if unit.endswith('.service'):
            data += f'[Service]\nExecStartPre=+{check} gate {unit}\n'
        write(folder / '90-xui-update.conf', data.encode(), 0o644)
    link = SYSTEM / 'multi-user.target.wants' / FINISH
    if not link.exists():
        link.symlink_to(SYSTEM / FINISH)
        fsync(link.parent)
    run('systemctl', 'daemon-reload')
    service('start', BOOT)


def gate(j, unit):
    if not j:
        return
    phase = j['phase']
    if phase in COHERENT_PHASES:
        return
    if phase == 'start_intent' and unit != 'certbot.service':
        return
    if unit == 'nginx.service' and prop(BOOT, 'ActiveState') == 'failed':
        # The distro unit's own ExecStartPre validates the live nginx config.
        return
    raise RuntimeError(f'{unit} start blocked during Update phase {phase}')


def clear(j):
    JOURNAL.unlink()
    fsync(JOURNAL.parent)
    for unit in GUARDS:
        folder = SYSTEM / (unit + '.d')
        (folder / '90-xui-update.conf').unlink(missing_ok=True)
        if folder.exists():
            fsync(folder)
    (SYSTEM / 'multi-user.target.wants' / FINISH).unlink(missing_ok=True)
    (SYSTEM / BOOT).unlink(missing_ok=True)
    (SYSTEM / FINISH).unlink(missing_ok=True)
    fsync(SYSTEM / 'multi-user.target.wants')
    fsync(SYSTEM)
    run('systemctl', 'daemon-reload')
    shutil.rmtree(WORK)
    fsync(BASE)


def restore_services(j):
    for unit, active in j['active'].items():
        if unit == 'certbot.timer' and j.get('phase') == 'start_intent':
            continue
        current = prop(unit, 'ActiveState')
        if active and current != 'active':
            service('start', unit)
        elif not active and current in ('active', 'activating'):
            service('stop', unit)
        if (prop(unit, 'ActiveState') == 'active') != active:
            raise RuntimeError(f'Cannot restore original state of {unit}')


def workers():
    pid = int(prop('nginx', 'MainPID'))
    if not pid:
        raise RuntimeError('nginx master is inactive')
    children = Path(f'/proc/{pid}/task/{pid}/children').read_text().split()
    result = set()
    for child in children:
        try:
            if Path(f'/proc/{child}/cmdline').read_bytes().startswith(b'nginx: worker process\0'):
                result.add(child)
        except FileNotFoundError:
            pass
    return result


def reload_nginx():
    run('nginx', '-t')
    previous = workers()
    service('reload', 'nginx')
    until = time.monotonic() + 8
    while time.monotonic() < until:
        if workers() - previous:
            return
        time.sleep(0.1)
    raise RuntimeError('nginx master did not start a new worker generation')


def health(state, target=True):
    run('nginx', '-t')
    if target:
        rows, _, wanted, edits = installation(state)
    else:
        with closing(connect()) as db:
            rows = database.read_roles(db, state)
    if target and (edits or wanted.keys() - rows.keys()):
        raise ValueError('Managed inbound definitions did not persist')
    with closing(connect()) as db:
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise RuntimeError('Working SQLite quick_check failed')
    if not workers():
        raise RuntimeError('nginx has no active workers')
    outdated, xray = versions()
    if target and outdated:
        raise ValueError('Working panel or Xray differs from the verified release')
    pid = prop('x-ui', 'MainPID')
    if not os.path.samefile(f'/proc/{pid}/exe', PANEL / 'x-ui'):
        raise ValueError('Unexpected x-ui executable')
    if not database.holds_database(Path('/proc') / pid, DB):
        raise ValueError('Running panel does not have /etc/x-ui/x-ui.db open')
    cgroup = Path('/sys/fs/cgroup') / prop('x-ui', 'ControlGroup').lstrip('/')
    pids = (cgroup / 'cgroup.procs').read_text().split()
    xray_pids = []
    for child in pids:
        try:
            if os.path.samefile(f'/proc/{child}/exe', xray):
                xray_pids.append(child)
        except FileNotFoundError:
            continue
    if not xray_pids:
        raise RuntimeError('Bundled Xray is not running in x-ui.service')
    run(xray, 'run', '-test', '-c', xray.with_name('config.json'), cwd=xray.parent)
    tcp = run('ss', '-H', '-lntp').decode().splitlines()
    udp = run('ss', '-H', '-lnup').decode().splitlines()
    unix = run('ss', '-H', '-lxnp').decode().splitlines()
    for role, row in rows.items():
        if not row['enable']:
            continue
        if row['listen'].startswith('/'):
            address = row['listen'].split(',')[0]
            listing = [line for line in unix if address in line.split()]
        else:
            lines = udp if role in ('hysteria2', 'amneziawg') else tcp
            listing = [line for line in lines if line.split()[3].rsplit(':', 1)[-1] == str(row['port'])]
        owners = [pid, *xray_pids] if role == 'amneziawg' else xray_pids
        if not any(f'pid={p},' in line for line in listing for p in owners):
            raise RuntimeError(f'Managed {role} listener unavailable')
    op = module('domains').Operation('', '')
    panel = state['domains']['panel']
    cert = '/etc/letsencrypt/live/' + panel + '/fullchain.pem'
    with closing(connect()) as db:
        settings = dict(db.execute('SELECT key,value FROM settings'))
        token = secrets.token_hex(20)
        while db.execute('SELECT 1 FROM clients WHERE sub_id=?', (token,)).fetchone():
            token = secrets.token_hex(20)
    request = op.request
    if request(panel, settings['webBasePath'], cert)[0] not in (200, 302):
        raise RuntimeError('Panel HTTPS unavailable')
    if settings.get('subEnable', 'true') == 'true':
        # Probe configured local routes without reading customer profiles or
        # assuming a particular subscription format, public origin, or valid client.
        raw = settings['subPath'].rstrip('/') + '/' + token
        if request(panel, raw, cert)[0] not in (400, 404):
            raise RuntimeError('RAW subscription route did not reject an unknown client')
        code, body = request(panel, raw, cert, headers={'User-Agent': 'mihomo'})
        if code != 200 or b'proxy-providers:' not in body:
            raise RuntimeError('Managed Clash subscription unavailable')
        if settings.get('subJsonEnable', 'true') == 'true':
            if request(panel, settings['subJsonPath'].rstrip('/') + '/' + token, cert)[0] not in (400, 404):
                raise RuntimeError('JSON subscription route did not reject an unknown client')
    if state['features'].get('adguard') and prop('AdGuardHome', 'ActiveState') == 'active':
        run('bash', DRIVER, 'agh-health')


def verified_health(state, target=True):
    # systemd Type=simple announces active before the panel and Xray listen.
    until = time.monotonic() + 15
    while True:
        try:
            health(state, target)
            return
        except (OSError, ValueError, RuntimeError) as error:
            if time.monotonic() >= until:
                raise RuntimeError(f'Working stack health failed ({type(error).__name__}): {error}') from None
            time.sleep(0.5)


def make_plan(state, release):
    directory = WORK / 'stage'
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir()
    rows, params, wanted, edits = installation(state)
    files = render(directory, state, rows, params, release)
    candidate_nginx(directory, files)
    return files, edits, list(wanted.keys() - rows.keys()), params


def verify_migration(db, before):
    def normalize_inbound_settings(value):
        if not isinstance(value, str):
            return value
        try:
            return json.dumps(json.loads(value), sort_keys=True)
        except ValueError:
            return value

    # The upstream migrator can reserialize the inbound settings object.
    db.create_function('normalize_inbound_settings', 1, normalize_inbound_settings)
    # Both upstream models interpret NULL as an unset string in these fields.
    unset_strings = {'inbounds': ('share_addr', 'origin_node_guid'),
                     'hosts': ('verify_peer_cert_by_name',)}
    db.execute('ATTACH DATABASE ? AS previous', (before.as_uri() + '?mode=ro',))
    for (name,) in db.execute(
        "SELECT name FROM previous.sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        table = '"' + name.replace('"', '""') + '"'
        fields = []
        for row in db.execute('PRAGMA previous.table_info(' + table + ')'):
            quoted = '"' + row[1].replace('"', '""') + '"'
            if name == 'inbounds' and row[1] == 'settings':
                quoted = f'normalize_inbound_settings({quoted})'
            elif row[1] in unset_strings.get(name, ()):
                quoted = f"COALESCE({quoted}, '')"
            fields.append(quoted)
        columns = ','.join(fields)
        if db.execute(
            f'SELECT {columns} FROM previous.{table} EXCEPT SELECT {columns} FROM main.{table} LIMIT 1'
        ).fetchone():
            raise ValueError(f'Upstream migration changed an existing record in {name}')


def start_intent(j, state, change_db):
    if change_db:
        with database.stopped_database(DB, stopped):
            j['stamp'] = database.digest(DB)
    j['target'] = state
    save(j, 'start_intent')


def commit(j):
    restore_services(j)
    verified_health(j['target'])
    save(j, 'verified')
    finish_commit(j)


def finish_commit(j):
    state = dict(j['target'], source_ref=j['sha'])
    write(MANIFEST, (json.dumps(state) + '\n').encode())
    save(j, 'committed')
    restore_services(j)
    archive = j['backup']
    clear(j)
    print(f'Update verified. Backup: {archive}', flush=True)


def apply_update(sha):
    if JOURNAL.exists():
        raise RuntimeError('Recover pending operation first')
    state = metadata()
    upgrade, _ = versions()
    if WORK.exists():
        # No journal: a prior preparation stopped before its first stack mutation.
        shutil.rmtree(WORK)
    WORK.mkdir(mode=0o700)
    release = None
    if upgrade:
        run('bash', DRIVER, 'release', WORK, timeout=600)
        release = WORK / 'x-ui'
        # Exercise the native migration on a consistent private SQLite backup
        # while the old service is still available. Cutover takes a fresh image.
        trial = WORK / 'trial'
        trial.mkdir()
        with closing(connect()) as live, closing(sqlite3.connect(trial / DB.name)) as db:
            live.backup(db)
        run(
            'env',
            'XUI_DB_TYPE=sqlite',
            'XUI_DB_FOLDER=' + str(trial),
            'XUI_BIN_FOLDER=' + str(release / 'bin'),
            'XUI_LOG_FOLDER=' + str(trial),
            release / 'x-ui',
            'migrate',
            cwd=release,
            timeout=300,
        )
        with closing(sqlite3.connect(trial / DB.name)) as db:
            if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise RuntimeError('Trial SQLite migration failed quick_check')
        shutil.rmtree(trial)
    files, edits, missing, params = make_plan(state, release)
    if not (
        files
        or edits
        or missing
        or upgrade
        or (WORK / 'stage/sysctl.json').exists()
        or state['source_ref'] != sha
    ):
        verified_health(state)
        shutil.rmtree(WORK)
        print('Already up to date.', flush=True)
        return
    (WORK / 'before').mkdir()
    (WORK / 'source').symlink_to(ROOT)
    sync_tree(ROOT)
    fsync(WORK)
    fsync(BASE)
    JOURNAL.parent.mkdir(mode=0o700, exist_ok=True)
    fsync(BASE)
    units = ['x-ui', 'nginx', 'mtr-backend', 'certbot.timer']
    if state['features'].get('adguard'):
        units.append('AdGuardHome')
    j = dict(
        operation='update',
        format=1,
        sha=sha,
        active={u: prop(u, 'ActiveState') == 'active' for u in units},
        old=state,
    )
    if not database.holds_database(Path('/proc') / prop('x-ui', 'MainPID'), DB):
        raise RuntimeError('Working panel is not using the installed SQLite database')
    if not j['active']['nginx'] or not j['active']['x-ui']:
        raise RuntimeError('Update requires the installed panel and nginx to be running')
    create_units()
    save(j, 'prepared')
    j['backup'] = backup()
    print(f'Backup verified: {j["backup"]}', flush=True)
    save(j, 'draining')
    if j['active']['certbot.timer']:
        service('stop', 'certbot.timer')
    if prop('certbot.service', 'ActiveState') not in ('inactive', 'failed'):
        raise RuntimeError('Certificate renewal in progress')
    # Plan again after Backup, which restarted the panel and admitted writes.
    state = metadata()
    rows, _, wanted, edits = installation(state)
    unit = '/etc/systemd/system/x-ui.service'
    change_db = bool(upgrade or edits or wanted.keys() - rows.keys() or unit in files)
    if change_db:
        service('stop', 'x-ui')
        j['before_sha'] = capture_db(WORK / 'before.db')
    state = metadata()
    files, edits, missing, params = make_plan(state, release)
    restart_panel = bool(change_db or edits or missing or unit in files)
    if restart_panel and not change_db:
        service('stop', 'x-ui')
        j['before_sha'] = capture_db(WORK / 'before.db')
        change_db = True
        state = metadata()
        files, edits, missing, params = make_plan(state, release)
    j.update(db=change_db, upgrade=upgrade, files=snapshot_files(files + [str(MANIFEST)], j['backup']))
    tuning = WORK / 'stage/sysctl.json'
    if tuning.exists():
        j['sysctl'] = json.loads(tuning.read_text())
    save(j, 'switching')
    if 'sysctl' in j:
        tune_runtime(j['sysctl'], True)
    for name in files:
        publish(WORK / 'stage/files' / name.lstrip('/'), Path(name))
    if any(name.startswith('/etc/systemd/system/') for name in files):
        run('systemctl', 'daemon-reload')
    if upgrade:
        run(
            'env',
            'XUI_DB_TYPE=sqlite',
            'XUI_DB_FOLDER=' + str(DB.parent),
            PANEL / 'x-ui',
            'migrate',
            cwd=PANEL,
            timeout=300,
        )
        with closing(connect()) as db:
            verify_migration(db, WORK / 'before.db')
    if change_db:
        rows, params, wanted, edits = installation(state)
        with closing(connect(False)) as db, db:
            database.reconcile(db, module, state, rows, params, wanted, edits)
    if 'xhttp_cdn' in state:
        cdn = state['xhttp_cdn']
        cdn['snippet'] = (NGINX / 'snippets/x-ui-auto-optional/xhttp-cdn.conf').read_text()
        with closing(connect()) as db:
            db.row_factory = sqlite3.Row
            hid = cdn['host']['id']
            cdn['host'] = dict(db.execute('SELECT * FROM hosts WHERE id=?', (hid,)).fetchone())
            row = database.read_roles(db, state)['xhttp_cdn']
            cdn.update(port=row['port'], path=row['stream_settings']['xhttpSettings']['path'])
    if any(Path(n).is_relative_to(NGINX) for n in files):
        reload_nginx()
    start_intent(j, state, change_db)
    if restart_panel:
        service('start', 'x-ui')
    if j['active']['mtr-backend'] and any('mtr-backend' in f for f in files):
        service('restart', 'mtr-backend')
    commit(j)


def rollback(j, offline=False):
    phase = j['phase']
    if phase in ('prepared', 'draining'):
        save(j, 'old_ready')
        return
    if phase in ('committed', 'old_ready'):
        return
    if phase == 'target_retained':
        if not offline:
            restore_services(j)
            raise RuntimeError(f'Target retained. Inspect {WORK} and {JOURNAL} before retrying.')
        return
    if j['db']:
        if phase in ('start_intent', 'compare_target'):
            # Block systemd restarts before stopping the last possible public writer.
            save(j, 'compare_target')
            if not offline:
                service('stop', 'x-ui')
            try:
                with database.stopped_database(DB, stopped):
                    if database.digest(DB) != j['stamp']:
                        publish(DB, WORK / 'target.db')
                        save(j, 'target_retained')
                    else:
                        save(j, 'rollback')
            except (RuntimeError, OSError, sqlite3.Error):
                # The live DB and all its sidecars still belong to the target
                # binaries. Keep them in place if we cannot prove a safe copy.
                with closing(connect()) as db:
                    if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                        raise RuntimeError(f'Target SQLite integrity unverified. Recovery retained in {WORK}')
                save(j, 'target_retained')
                if not offline:
                    restore_services(j)
                    raise RuntimeError(
                        f'Checkpoint or snapshot unverified. Target DB and WAL retained at {DB}'
                    )
                return
            if j['phase'] == 'target_retained':
                if offline:
                    return
                restore_services(j)
                raise RuntimeError(f'Working SQLite changed after startup. Both versions retained in {WORK}.')
        save(j, 'rollback')
        if not offline:
            service('stop', 'x-ui')
        stopped()
        database.assert_no_holders(DB)
        if database.digest(WORK / 'before.db') != j['before_sha']:
            raise RuntimeError('Damaged SQLite before-image. Recovery files retained.')
        # No service has permission to reopen this DB. Rollback may have been
        # interrupted anywhere below, including during a failed upstream migration.
        for suffix in ('-journal', '-wal', '-shm'):
            Path(str(DB) + suffix).unlink(missing_ok=True)
        publish(WORK / 'before.db', DB)
    else:
        save(j, 'rollback')
    restore_archived_files(j['files'])
    for name, entry in reversed(j['files'].items()):
        if 'member' in entry:
            continue
        p = Path(name)
        if entry['kind'] == 'absent':
            p.unlink(missing_ok=True)
            if p.parent.exists():
                fsync(p.parent)
        else:
            publish(WORK / 'before' / entry['copy'], p)
    if 'sysctl' in j:
        tune_runtime(j['sysctl'], False)
    run('nginx', '-t')
    if any(n.startswith('/etc/systemd/system/') for n in j['files']):
        run('systemctl', 'daemon-reload')
    save(j, 'old_ready')


def finish_old(j):
    run('systemctl', 'daemon-reload')
    restore_services(j)
    if any(Path(n).is_relative_to(NGINX) for n in j.get('files', {})):
        reload_nginx()
    if j['active']['mtr-backend'] and any('mtr-backend' in f for f in j.get('files', {})):
        service('restart', 'mtr-backend')
    verified_health(j['old'], False)
    clear(j)
    print('ROLLBACK_VERIFIED: previous installation restored.', flush=True)


def recover(j, offline=False):
    if j['phase'] == 'verified':
        if not offline:
            restore_services(j)
            verified_health(j['target'])
            finish_commit(j)
        return
    if j['phase'] == 'committed':
        if not offline:
            restore_services(j)
            verified_health(j['target'])
            clear(j)
        return
    if j['phase'] == 'start_intent':
        if offline:
            # All target files and the SQLite transaction were durable before start_intent.
            return
        try:
            commit(j)
            return
        except Exception:
            if j['phase'] != 'start_intent':
                raise
    rollback(j, offline)
    if not offline and j['phase'] == 'old_ready':
        finish_old(j)


def main():
    os.umask(0o077)
    action = sys.argv[1]
    j = record()
    if action == 'boot-ready':
        # A manual recovery may already own fd9 and start a service whose
        # previous boot dependency failed. The file gate can pass without
        # taking the lock once the on-disk pair is coherent.
        sys.exit(0 if j is None or j['phase'] in (*COHERENT_PHASES, 'start_intent') else 1)
    if action == 'gate':
        gate(j, sys.argv[2])
        return
    if action == 'worker':
        if not re.fullmatch(r'[a-f0-9]{40}', sys.argv[2]):
            raise ValueError('Expected pinned Update SHA')
        if j:
            raise RuntimeError('Recover the pending Update before starting another')
        try:
            apply_update(sys.argv[2])
        except Exception:
            j = record()
            if j and j['phase'] not in ('verified', 'committed'):
                rollback(j)
                finish_old(j)
            raise
    elif j:
        recover(j, offline=action == 'boot')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Keep private values from JSON, subprocesses and SQLite exceptions out of logs.
        print(
            f'Update failed: {error}'
            if isinstance(error, (RuntimeError, ValueError))
            and type(error) not in (json.JSONDecodeError, UnicodeDecodeError)
            else f'Update failed at {sys.exc_info()[2].tb_lineno}: {type(error).__name__}',
            file=sys.stderr,
        )
        sys.exit(1)
