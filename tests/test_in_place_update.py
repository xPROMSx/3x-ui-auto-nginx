"""In-place Update cutover and crash recovery, using real SQLite files."""

import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'assets/update/managed.py'
INSTALLER = SOURCE.parents[2] / 'x-ui-latest.sh'
spec = importlib.util.spec_from_file_location('project_update', SOURCE)
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)
database = update.database


class InPlaceUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'x-ui.db'
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in [
            ('DB', self.db),
            ('WORK', self.root),
            ('JOURNAL', self.root / 'journal.json'),
            ('MANIFEST', self.root / 'manifest.json'),
        ]:
            self.stack.enter_context(patch.object(update, name, value))
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript(
                'CREATE TABLE clients(id INTEGER PRIMARY KEY,sub_id TEXT);'
                'CREATE TABLE client_inbounds(client INTEGER,inbound INTEGER,flow TEXT);'
                "INSERT INTO clients VALUES(1,'first');"
                "INSERT INTO client_inbounds VALUES(1,7,'original flow');"
            )

    def test_ui_noop_reports_plan_and_health_without_backup_or_cutover(self):
        for lang, heading, result in (
            ('en', 'UPDATE NOT REQUIRED', 'Result: UP TO DATE'),
            ('ru', 'ОБНОВЛЕНИЕ НЕ ТРЕБУЕТСЯ', 'Результат: UP TO DATE'),
        ):
            with self.subTest(lang=lang):
                output = StringIO()
                state = {'source_ref': 'same'}

                def verify(actual):
                    self.assertIs(actual, state)
                    self.assertIn('[5/5]', output.getvalue())
                    self.assertNotIn(result, output.getvalue())

                with (
                    patch.dict('os.environ', {'UI_LANG': lang}),
                    patch.object(update, 'WORK', self.root / 'work'),
                    patch.object(update, 'metadata', return_value=state),
                    patch.object(update, 'versions', return_value=(False, None)),
                    patch.object(update, 'make_plan', return_value=([], [], [], {})),
                    patch.object(update, 'verified_health', side_effect=verify) as health,
                    patch.object(update, 'backup') as backup,
                    patch.object(update, 'service') as service,
                    redirect_stdout(output),
                ):
                    update.apply_update('same')
                health.assert_called_once()
                backup.assert_not_called()
                service.assert_not_called()
                rendered = output.getvalue()
                self.assertIn(heading, rendered)
                self.assertIn(result, rendered)
                for omitted in ('[3/5]', '[4/5]', 'SUCCESS', '\x1b['):
                    self.assertNotIn(omitted, rendered)

    def test_ui_success_reports_only_after_commit_and_includes_backup(self):
        output = StringIO()
        archive = '/var/backups/x-ui/update-fixture.tar.gz'
        journal = dict(target={'source_ref': 'old'}, sha='new', backup=archive, active={})

        def verify(*args):
            self.assertIn('[5/5] FINAL VERIFICATION', output.getvalue())
            self.assertNotIn('SUCCESS', output.getvalue())

        def clear(j):
            self.assertEqual(j['phase'], 'committed')
            self.assertNotIn('SUCCESS', output.getvalue())

        with (
            patch.dict('os.environ', {'UI_LANG': 'en'}),
            patch.object(update, 'restore_services'),
            patch.object(update, 'verified_health', side_effect=verify),
            patch.object(update, 'clear', side_effect=clear) as cleanup,
            redirect_stdout(output),
        ):
            update.commit(journal)
        cleanup.assert_called_once_with(journal)
        self.assertEqual(json.loads(update.MANIFEST.read_text())['source_ref'], 'new')
        rendered = output.getvalue()
        self.assertIn('UPDATE COMPLETE', rendered)
        self.assertIn(archive, rendered)
        self.assertIn('Result: SUCCESS', rendered)

    def test_ui_worker_refusal_is_localized_and_keeps_nonzero_status(self):
        for lang, result in (({}, 'Result: FAILED'), ({'UI_LANG': 'ru'}, 'Результат: FAILED')):
            with self.subTest(lang=lang):
                worker = subprocess.run(
                    [sys.executable, '-I', SOURCE, 'worker', 'invalid'],
                    env={**lang, 'TERM': 'dumb', 'NO_COLOR': ''},
                    capture_output=True, text=True,
                )
                self.assertNotEqual(worker.returncode, 0)
                output = worker.stdout + worker.stderr
                self.assertIn(result, output)
                self.assertIn('SHA', output)
                for omitted in ('ROLLBACK', 'SUCCESS', 'Traceback', '\x1b['):
                    self.assertNotIn(omitted, output)

    def test_ui_manager_prints_progress_before_pinning_source(self):
        manifest = self.root / 'compat.json'
        manifest.write_text(json.dumps(dict(format=1, layout='nginx-stream-v1', update_contract=1)))
        # Relocate only the root/manifest boundary. Stop before using any live path.
        action = INSTALLER.read_text().split('\nupdate_action() (\n', 1)[1].split('\n)\n', 1)[0]
        action = action.replace('(( EUID == 0 ))', ':', 1)
        action = action.replace('manifest=/etc/x-ui/auto-nginx-managed.json', 'manifest="$2"', 1)
        shell = '''
source "$1"
UI_LANG=ru
check_os() { echo checking-os; }
recover_domain_operation() { :; }
curl() { echo fetching-source >&3; return 22; }
PROJECT_REF=main
update_action() (
''' + action + '''
)
update_action "$@"
'''
        result = subprocess.run(
            ['bash', '-c', shell, 'bash', INSTALLER, manifest], input='y\n', text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=5,
        )
        self.assertEqual(result.returncode, 69)
        steps = ('[1/5] ПРОВЕРКА СОВМЕСТИМОСТИ', 'checking-os', 'ОС и формат установки поддерживаются',
                 '[2/5] ПОДГОТОВКА ОБНОВЛЕНИЯ', 'fetching-source', 'Результат: FAILED')
        positions = [result.stdout.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(result.stdout.count('FAILED'), 1)
        self.assertNotIn('[3/5]', result.stdout)

    def test_ui_failed_cutover_reports_actual_recovery_and_redacts_errors(self):
        for retained in (False, True):
            with self.subTest(retained=retained):
                output = StringIO()

                def fail(_):
                    update.save({'operation': 'update', 'old': {}, 'active': {}}, 'switching')
                    raise RuntimeError('private update data')

                def rollback(j):
                    if retained:
                        update.save(j, 'target_retained')
                        raise update.problem(
                            RuntimeError, 'Target DB and WAL retained', 'Целевая база и WAL сохранены',
                        )
                    update.save(j, 'old_ready')

                with (
                    patch.dict('os.environ', {'UI_LANG': 'ru'}),
                    patch.object(sys, 'argv', ['managed.py', 'worker', 'a' * 40]),
                    patch.object(update.os, 'umask'),
                    patch.object(update, 'apply_update', side_effect=fail),
                    patch.object(update, 'rollback', side_effect=rollback),
                    patch.object(update, 'run'),
                    patch.object(update, 'restore_services'),
                    patch.object(update, 'verified_health') as health,
                    patch.object(update, 'clear', side_effect=lambda j: update.JOURNAL.unlink()),
                    patch.object(update, 'SYSTEM', self.root),
                    redirect_stdout(output),
                ):
                    with self.assertRaises(RuntimeError):
                        update.main()
                rendered = output.getvalue()
                self.assertNotIn('private update data', rendered)
                self.assertNotIn('SUCCESS', rendered)
                self.assertIn('Результат: FAILED', rendered)
                if retained:
                    health.assert_not_called()
                    self.assertIn('Целевая база и WAL сохранены', rendered)
                    self.assertIn('Результат: ROLLBACK UNVERIFIED', rendered)
                    self.assertNotIn('ROLLBACK VERIFIED', rendered)
                    self.assertEqual(update.record()['phase'], 'target_retained')
                else:
                    health.assert_called_once_with({}, False)
                    self.assertIn('Результат: ROLLBACK VERIFIED', rendered)
                    self.assertNotIn('ROLLBACK UNVERIFIED', rendered)

    def test_killed_wal_writer_requires_checkpoint_before_fingerprint(self):
        script = """import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA journal_mode=WAL')
db.execute('PRAGMA wal_autocheckpoint=0')
db.execute("INSERT INTO clients VALUES(2,'last write')")
db.commit()
os._exit(0)
"""
        subprocess.run([sys.executable, '-c', script, self.db], check=True)
        wal = Path(str(self.db) + '-wal')
        self.assertGreater(wal.stat().st_size, 0)
        checks = []
        with database.stopped_database(
            self.db, lambda: checks.append('stopped'), lambda path: checks.append('handles')
        ) as db:
            self.assertEqual(len(checks), 4)
            self.assertEqual(db.execute('SELECT count(*) FROM clients').fetchone(), (2,))
            self.assertEqual(wal.stat().st_size, 0)
            stamp = database.digest(self.db)
        with database.stopped_database(self.db, lambda: None, lambda path: None):
            self.assertEqual(stamp, database.digest(self.db))

    def test_active_sqlite_writer_prevents_hash(self):
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            with self.assertRaises(sqlite3.OperationalError):
                with database.stopped_database(self.db, lambda: None, lambda path: None):
                    self.fail('Fingerprint allowed with a running writer')

    def test_live_holder_refuses_cutover_even_without_sqlite_write_lock(self):
        child = subprocess.Popen(
            [
                sys.executable,
                '-c',
                "import sys; f=open(sys.argv[1],'rb'); print('open',flush=True); sys.stdin.read()",
                self.db,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        try:
            self.assertEqual(child.stdout.readline(), b'open\n')
            original = Path.iterdir
            with patch.object(
                Path, 'iterdir', lambda p: iter([p / str(child.pid)]) if p == Path('/proc') else original(p)
            ):
                with self.assertRaisesRegex(RuntimeError, 'still has'):
                    database.assert_no_holders(self.db)
        finally:
            child.communicate(timeout=5)

    def test_new_link_after_panel_start_keeps_target_and_both_images(self):
        self.stack.enter_context(patch.object(update, 'stopped'))
        self.stack.enter_context(patch.object(update, 'service'))
        self.stack.enter_context(patch.object(update, 'restore_services'))
        real = database.stopped_database
        self.stack.enter_context(
            patch.object(database, 'stopped_database', lambda path, cb: real(path, cb, lambda path: None))
        )
        self.stack.enter_context(patch.object(database, 'assert_no_holders'))
        before = update.capture_db(self.root / 'before.db')
        j = dict(operation='update', db=True, before_sha=before, files={}, active={'x-ui': True})
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE target_schema(id INTEGER)')
        update.start_intent(j, {}, True)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO client_inbounds VALUES(1,22,'custom')")
        with self.assertRaisesRegex(RuntimeError, 'Both versions'):
            update.rollback(j)
        self.assertEqual(update.record()['phase'], 'target_retained')
        for path in (self.db, self.root / 'target.db'):
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM client_inbounds').fetchone(), (2,))
        self.assertEqual(database.digest(self.root / 'before.db'), before)
        update.recover(j, offline=True)
        self.assertFalse(update.MANIFEST.exists())

    def test_failed_actual_start_rolls_back_migrated_database(self):
        for obj, name in (
            (update, 'stopped'),
            (update, 'service'),
            (database, 'assert_no_holders'),
            (update, 'run'),
        ):
            if name != 'run':
                self.stack.enter_context(patch.object(obj, name))
        self.stack.enter_context(
            patch.object(
                update,
                'run',
                wraps=update.run,
                side_effect=lambda *args, **kw: subprocess.check_output(args) if args[0] != 'nginx' else b'',
            )
        )
        real = database.stopped_database
        self.stack.enter_context(
            patch.object(database, 'stopped_database', lambda path, cb: real(path, cb, lambda path: None))
        )
        before = update.capture_db(self.root / 'before.db')
        j = dict(operation='update', db=True, files={}, before_sha=before)
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('ALTER TABLE clients ADD COLUMN target_only')
        update.start_intent(j, {}, True)
        update.rollback(j)
        self.assertEqual(update.record()['phase'], 'old_ready')
        self.assertEqual(database.digest(self.db), before)
        self.assertFalse(update.MANIFEST.exists())

    def test_service_gate_and_failed_boot_recovery_preserve_evidence(self):
        j = {'phase': 'switching', 'db': True, 'before_sha': 'missing', 'files': {}, 'operation': 'update'}
        update.save(j, 'switching')
        self.stack.enter_context(patch.object(update, 'stopped'))
        self.stack.enter_context(patch.object(database, 'assert_no_holders'))
        with self.assertRaises(FileNotFoundError):
            update.recover(j, offline=True)
        j = update.record()
        for unit in ('x-ui.service', 'mtr-backend.service', 'certbot.service'):
            with self.assertRaisesRegex(RuntimeError, 'blocked'):
                update.gate(j, unit)
        with patch.object(update, 'prop', return_value='active'):
            with self.assertRaisesRegex(RuntimeError, 'blocked'):
                update.gate(j, 'nginx.service')
        with patch.object(update, 'prop', return_value='failed'):
            update.gate(j, 'nginx.service')
        self.assertTrue(update.JOURNAL.exists())

        system = self.root / 'system'
        (system / 'multi-user.target.wants').mkdir(parents=True)
        with patch.object(update, 'SYSTEM', system), patch.object(update, 'run'), patch.object(update, 'service'):
            update.create_units()
        nginx = (system / 'nginx.service.d/90-xui-update.conf').read_text()
        panel = (system / 'x-ui.service.d/90-xui-update.conf').read_text()
        self.assertIn(f'Wants={update.BOOT}\nAfter={update.BOOT}\n', nginx)
        self.assertNotIn(f'Requires={update.BOOT}', nginx)
        self.assertIn(f'Requires={update.BOOT}\nAfter={update.BOOT}\n', panel)

    def test_failed_health_does_not_publish_target_ref(self):
        update.MANIFEST.write_text('previous')
        with (
            patch.object(update, 'restore_services'),
            patch.object(update, 'verified_health', side_effect=RuntimeError('offline')),
        ):
            with self.assertRaisesRegex(RuntimeError, 'offline'):
                update.commit(dict(target={}, sha='new', active={}))
        self.assertEqual(update.MANIFEST.read_text(), 'previous')

    def test_audit_preserves_vless_settings_on_existing_roles(self):
        params = dict(domain='panel.example', reality_domain='cover.example', short_ids=[],
                      private_key='private', public_key='public', ws_port=12001, ws_route='/custom-ws',
                      trojan_port=12002, service_name='/custom/grpc|multi', xhttp_route='/xhttp',
                      certificate='cert', key_file='key', INSTALL_WS_GRPC='y', flag='')
        core = update.module('inbounds')
        vision = update.module('vision')
        target = {role: core.core_inbound(role, params) for role in ('reality', 'xhttp', 'ws')}
        target['vision'] = vision.inbound('vision.example', 12003, 12004, 'cert', 'key')
        target['xhttp_cdn'] = update.module('xhttp-cdn').inbound(
            12005, '/CDNFixture123/cookie', '', dict(encryption='pair', decryption='key'))
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('CREATE TABLE inbounds(id INTEGER PRIMARY KEY, settings TEXT)')
            for iid, (role, definition) in enumerate(target.items(), 1):
                saved = dict(encryption='pair', decryption='key', clients=[{'id': 'existing'}])
                row = dict(id=iid, settings=saved, stream_settings=definition['streamSettings'],
                           sniffing=definition['sniffing'], **{key: definition[key] for key in ('listen','port','protocol')})
                db.execute('INSERT INTO inbounds VALUES(?,?)', (iid, json.dumps(saved)))
                database.apply(db, database.changes({role: row}, {role: definition}))
                actual = json.loads(db.execute('SELECT settings FROM inbounds WHERE id=?', (iid,)).fetchone()[0])
                self.assertEqual(actual, saved, role)
        old_fallbacks = [{'dest': '127.0.0.1:12006', 'xver': 1}]
        self.assertEqual(database.merge_vless_settings(
            dict(encryption='none', decryption='none', fallbacks=old_fallbacks), target['vision']['settings']
        )['fallbacks'], old_fallbacks)

    def test_audit_shared_renderer_uses_custom_transport_paths(self):
        from test_personal_xhttp import SHARED, render

        for service, route in (('/custom/rpc|bulk', '/custom/rpc'), ('existing', '/existing/Tun')):
            with self.subTest(service=service):
                snippet = render(SHARED, ws_port='12001', ws_route='/custom-ws',
                                 trojan_port='12002', service_name=service)
                self.assertIn('location = /custom-ws {', snippet)
                self.assertIn('proxy_pass http://127.0.0.1:12001;', snippet)
                self.assertIn(f'location = {route} {{', snippet)
                self.assertIn('grpc_pass grpc://127.0.0.1:12002;', snippet)

    def test_audit_trial_rejects_host_change_before_planning_or_cutover(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript('CREATE TABLE hosts(id INTEGER PRIMARY KEY, mihomo_x25519 INTEGER);'
                             'INSERT INTO hosts VALUES(1,1);')
        work = self.root/'work'

        def native_migration(binary, folder, *_):
            with closing(sqlite3.connect(folder/self.db.name)) as db, db:
                db.execute('UPDATE hosts SET mihomo_x25519=0')

        with (
            patch.object(update, 'WORK', work),
            patch.object(update, 'metadata', return_value={}),
            patch.object(update, 'versions', return_value=(True, None)),
            patch.object(update, 'migrate_schema', side_effect=native_migration),
            patch.object(update, 'run'),
            patch.object(update, 'make_plan', side_effect=AssertionError('unexpected stack preparation')),
            patch.object(update, 'service') as service,
        ):
            with self.assertRaisesRegex(ValueError, 'existing record in hosts'):
                update.apply_update('revision')
            service.assert_not_called()
            self.assertFalse(update.JOURNAL.exists())

    def test_audit_recovery_restarts_changed_mtr_before_health(self):
        for files, active, restart in (({'/usr/local/lib/3x-ui-pro/mtr-backend.py': {}}, True, True),
                                       ({}, True, False),
                                       ({'/usr/local/lib/3x-ui-pro/mtr-backend.py': {}}, False, False)):
            with self.subTest(files=files, active=active):
                j = dict(active={'mtr-backend': active}, files=files, phase='start_intent', target={})
                calls = []
                with (
                    patch.object(update, 'restore_services'),
                    patch.object(update, 'service', side_effect=lambda *args: calls.append(args)),
                    patch.object(update, 'prop', return_value='active' if active else 'inactive'),
                    patch.object(update, 'verified_health', side_effect=lambda *args: calls.append(('health',))),
                    patch.object(update, 'save'),
                    patch.object(update, 'finish_commit'),
                ):
                    update.recover(j)
                self.assertEqual(calls, ([('restart', 'mtr-backend')] if restart else []) + [('health',)])

    def test_audit_mtr_exit_after_restart_prevents_verified_phase(self):
        j = dict(target={}, active={'mtr-backend': True}, files={'/etc/systemd/system/mtr-backend.service': {}})
        with (
            patch.object(update, 'restore_services'),
            patch.object(update, 'service'),
            patch.object(update, 'prop', return_value='failed'),
            patch.object(update, 'verified_health'),
            patch.object(update, 'save') as save,
            patch.object(update, 'finish_commit'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'MTR backend is not active'):
                update.commit(j)
            save.assert_not_called()
    def test_worker_cleans_owned_source_only_after_recovery_dependencies_are_gone(self):
        source = self.root/'source.1234abcd'
        source.mkdir()
        system = self.root/'system'
        system.mkdir()
        guard = system/update.BOOT
        guard.touch()
        args = ['managed.py', 'worker', 'a'*40]
        with (
            patch.object(update, 'ROOT', source),
            patch.object(update, 'BASE', self.root),
            patch.object(update, 'SYSTEM', system),
            patch.object(update, 'apply_update'),
            patch.object(update.os, 'umask'),
            patch.object(sys, 'argv', args),
        ):
            update.main()
            self.assertTrue(source.is_dir())
            guard.unlink()
            update.main()
            self.assertFalse(source.exists())

    def test_audit_health_probes_active_subscriptions_and_empty_install(self):
        from urllib.parse import urlsplit

        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript("""
                ALTER TABLE clients ADD COLUMN enable DEFAULT 1;
                ALTER TABLE clients ADD COLUMN expiry_time DEFAULT 0;
                ALTER TABLE clients ADD COLUMN total_gb DEFAULT 0;
                ALTER TABLE clients ADD COLUMN limit_hwid DEFAULT 0;
                ALTER TABLE clients ADD COLUMN email DEFAULT 'client';
                ALTER TABLE client_inbounds RENAME COLUMN client TO client_id;
                ALTER TABLE client_inbounds RENAME COLUMN inbound TO inbound_id;
                CREATE TABLE inbounds(id INTEGER, protocol TEXT, enable INTEGER, exclude_from_sub INTEGER);
                INSERT INTO inbounds VALUES(7,'vless',1,0);
                CREATE TABLE client_traffics(email TEXT, enable INTEGER, expiry_time INTEGER, total INTEGER, up INTEGER, down INTEGER);
                CREATE TABLE settings(key TEXT,value TEXT);
                INSERT INTO settings VALUES ('webBasePath','/panel/'),('subPath','/raw/'),('subJsonPath','/json/'),
                      ('subURI','https://panel.example/raw/'),('subJsonURI','https://panel.example/json/'),
                      ('subEnable','true'),('subJsonEnable','true'),('subPort','12345');
            """)
        state = dict(domains={'panel':'panel.example'}, features={}, diagnostics={'mtr_backend_port':12346})
        failures = {'RAW': (404, b''), 'JSON': (200, b'{}'), 'Clash provider': (200, b'not a subscription')}
        seen = []

        def request(_, panel, path, cert, headers=None):
            route = urlsplit(path)
            if route.path == '/panel/':
                return 200, b''
            if route.path.startswith('/raw/') and headers and headers.get('User-Agent') == 'mihomo' and not route.query:
                return 200, (b'proxy-providers:\n  sub:\n    type: http\n    url: https://panel.example'
                             + route.path.encode() + b'?provider=1\n')
            if not route.path.endswith('/first'):
                return 404, b''
            kind = 'Clash provider' if route.query else 'JSON' if route.path.startswith('/json/') else 'RAW'
            seen.append(kind)
            if kind == fault:
                return failures[kind]
            return (200, b'{"outbounds":[{"protocol":"vless"}]}') if kind == 'JSON' else (200, b'vless://id@host:443')

        with (
            patch.object(update, 'installation', return_value=({}, None, {}, [])),
            patch.object(update, 'workers', return_value={1}),
            patch.object(update, 'versions', return_value=(False, self.root/'xray')),
            patch.object(update.database, 'holds_database', return_value=True),
            patch.object(update.os.path, 'samefile', return_value=True),
            patch.object(update.Path, 'read_text', return_value='2'),
            patch.object(update, 'prop', side_effect=lambda u, p: {'MainPID':'1','ControlGroup':'/x-ui',
                                                                  'ActiveState':'active'}[p]),
            patch.object(update, 'run', side_effect=lambda *args, **kw:
                         b'127.0.0.1' if args[0] == 'curl' else b'LISTEN 0 0 127.0.0.1:12345 *:* pid=1,'),
            patch.object(update.module('domains').Operation, 'request', request),
        ):
            for fault in failures:
                with self.subTest(format=fault), self.assertRaisesRegex(ValueError, fault):
                    update.health(state)
            fault = None
            seen.clear()
            update.health(state)
            self.assertEqual(set(seen), set(failures))
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("UPDATE clients SET expiry_time=1")
            seen.clear()
            update.health(state)
            self.assertEqual(seen, [])
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute('UPDATE clients SET expiry_time=0')
                db.execute("INSERT INTO clients(id,sub_id,limit_hwid) VALUES (2,'first',1)")
            update.health(state)
            self.assertEqual(seen, [])

    def test_schema_migration_uses_normal_init_and_requires_cli_completion(self):
        binary = self.root / 'x-ui'
        with patch.object(update, 'run', return_value=b'current panel settings as follows:\n') as run:
            update.migrate_schema(binary, self.root)
            run.assert_called_once_with(
                'env', 'XUI_DB_TYPE=sqlite', 'XUI_DB_FOLDER=' + str(self.root),
                binary, 'setting', '-show', cwd=self.root, timeout=300,
            )
            run.return_value = b'Database initialization failed\n'
            with self.assertRaisesRegex(RuntimeError, 'did not complete SQLite schema initialization'):
                update.migrate_schema(binary, self.root)

    def test_migration_accepts_additions_but_checks_client_links(self):
        with (
            closing(sqlite3.connect(self.db)) as live,
            closing(sqlite3.connect(self.root / 'before.db')) as old,
        ):
            live.backup(old)
        with closing(sqlite3.connect(self.db.as_uri() + '?mode=rw', uri=True)) as live:
            live.execute('ALTER TABLE clients ADD COLUMN target_only')
            live.execute("INSERT INTO clients VALUES(2,'new',NULL)")
            update.verify_migration(live, self.root / 'before.db')
            live.commit()
            live.execute('DETACH previous')
            live.execute('DELETE FROM client_inbounds')
            with self.assertRaisesRegex(ValueError, 'client_inbounds'):
                update.verify_migration(live, self.root / 'before.db')

    def test_migration_accepts_reserialized_inbound_settings_but_rejects_key_change(self):
        before = self.root / 'before.db'
        with closing(sqlite3.connect(self.db)) as live, closing(sqlite3.connect(before)) as old:
            live.execute('CREATE TABLE inbounds(id INTEGER PRIMARY KEY, settings TEXT)')
            live.execute('INSERT INTO inbounds VALUES(1,?)', ('{"encryption": "original", "clients": []}',))
            live.commit()
            live.backup(old)
        with closing(update.connect(False)) as db:
            db.execute('UPDATE inbounds SET settings=?', ('{"clients":[],"encryption":"original"}',))
            update.verify_migration(db, before)
            db.commit()
            db.execute('DETACH previous')
            db.execute('UPDATE inbounds SET settings=?', ('{"clients":[],"encryption":"replacement"}',))
            with self.assertRaisesRegex(ValueError, 'inbounds'):
                update.verify_migration(db, before)

    def test_migration_accepts_empty_inbound_fields_but_rejects_changed_values(self):
        before = self.root / 'before.db'
        with closing(sqlite3.connect(self.db)) as live, closing(sqlite3.connect(before)) as old:
            live.execute('CREATE TABLE inbounds(id INTEGER PRIMARY KEY, share_addr TEXT, origin_node_guid TEXT)')
            live.executemany('INSERT INTO inbounds VALUES(?,?,?)',
                             [(1, None, None), (2, 'custom.example', 'original-node')])
            live.commit()
            live.backup(old)
        with closing(update.connect(False)) as db:
            db.execute("UPDATE inbounds SET share_addr='',origin_node_guid='' WHERE id=1")
            update.verify_migration(db, before)
            db.commit()
        for column in ('share_addr', 'origin_node_guid'):
            with self.subTest(column=column), closing(update.connect(False)) as db:
                db.execute(f"UPDATE inbounds SET {column}='' WHERE id=2")
                with self.assertRaisesRegex(ValueError, 'inbounds'):
                    update.verify_migration(db, before)

    def test_migration_preserves_host_certificate_name(self):
        before = self.root / 'before.db'
        with closing(sqlite3.connect(self.db)) as live, closing(sqlite3.connect(before)) as old:
            live.execute('CREATE TABLE hosts(id INTEGER PRIMARY KEY, verify_peer_cert_by_name TEXT)')
            live.executemany('INSERT INTO hosts VALUES(?,?)', [(1, None), (2, 'cert.example')])
            live.commit()
            live.backup(old)
        with closing(update.connect(False)) as db:
            db.execute("UPDATE hosts SET verify_peer_cert_by_name='' WHERE id=1")
            update.verify_migration(db, before)
        with closing(update.connect(False)) as db:
            db.execute("UPDATE hosts SET verify_peer_cert_by_name='' WHERE id=2")
            with self.assertRaisesRegex(ValueError, 'hosts'):
                update.verify_migration(db, before)

    def test_update_merges_owned_fields_without_replacing_client_data(self):
        clients = [{'id': 'original', 'flow': 'individual'}]
        row = dict(
            id=7,
            listen='127.0.0.1',
            port=10001,
            protocol='vless',
            enable=0,
            settings={'clients': clients, 'encryption': 'pair', 'fallbacks': [], 'extra': True},
            stream_settings={'xhttpSettings': {'path': '/private', 'extra': True}},
            sniffing={},
        )
        wanted = dict(
            listen=row['listen'],
            port=row['port'],
            protocol=row['protocol'],
            settings={'clients': [], 'encryption': 'pair'},
            sniffing={},
            streamSettings={'xhttpSettings': {'mode': 'packet-up'}},
        )
        edits = database.changes({'xhttp_cdn': row}, {'xhttp_cdn': wanted})
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(
                'CREATE TABLE inbounds(id INTEGER PRIMARY KEY, settings TEXT,stream_settings TEXT,enable INTEGER)'
            )
            db.execute(
                'INSERT INTO inbounds VALUES(7,?,?,0)',
                (json.dumps(row['settings']), json.dumps(row['stream_settings'])),
            )
            database.apply(db, edits)
            settings, stream, enabled = db.execute(
                'SELECT settings,stream_settings,enable FROM inbounds'
            ).fetchone()
            row.update(settings=json.loads(settings), stream_settings=json.loads(stream))
            self.assertEqual(row['settings'], {'clients': clients, 'encryption': 'pair', 'extra': True})
            self.assertTrue(row['stream_settings']['xhttpSettings']['extra'])
            self.assertEqual(enabled, 0)
            self.assertEqual(database.changes({'xhttp_cdn': row}, {'xhttp_cdn': wanted}), [])

    def test_release_definition_can_introduce_a_disabled_role_once(self):
        assets = update.module('inbounds')
        fresh = assets.core_inbound
        params = {
            'domain': 'panel.example',
            'reality_domain': 'cover.example',
            'xhttp_route': '/initial',
            'short_ids': ['original'],
            'flag': '',
            'certificate': 'cert',
            'key_file': 'key',
            'private_key': 'private',
            'public_key': 'public',
            'INSTALL_WS_GRPC': 'n',
        }

        def revised(role, inputs):
            if role == 'test_role':
                result = fresh('xhttp', inputs)
                result.update(port=12040, listen='127.0.0.1')
                return result
            return fresh(role, inputs)

        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript(
                'CREATE TABLE inbounds(id INTEGER PRIMARY KEY,user_id,up,down,total,remark,enable,'
                'expiry_time,listen,port,protocol,settings,stream_settings,tag UNIQUE,sniffing,node_id);'
                'CREATE TABLE hosts(id INTEGER PRIMARY KEY,inbound_id,sort_order,remark,address,port,'
                'security,fingerprint,alpn,group_id);'
            )
            state = dict(inbound_roles={}, host_roles={})
            for role in assets.core_roles(False):
                iid, hid = assets.insert_core_role(db, role, params, enabled=True, user_id=1)
                state['inbound_roles'][role] = iid
                state['host_roles'][role] = hid
            db.commit()
            roles = (*assets.core_roles(False), 'test_role')
            with (
                patch.object(assets, 'core_roles', return_value=roles),
                patch.object(assets, 'core_inbound', revised),
            ):
                for attempt in range(2):
                    rows = database.read_roles(db, state)
                    wanted = database.definitions(update.module, state, rows, params)
                    database.reconcile(
                        db, update.module, state, rows, params, wanted, database.changes(rows, wanted)
                    )
                    db.commit()
                    iid = state['inbound_roles']['test_role']
                    self.assertEqual(db.execute('SELECT count(*) FROM inbounds').fetchone()[0], len(roles))
                    enabled, settings = db.execute(
                        'SELECT enable,settings FROM inbounds WHERE id=?', (iid,)
                    ).fetchone()
                    self.assertEqual(enabled, 0)
                    self.assertEqual(json.loads(settings)['clients'], [])
                    self.assertEqual(
                        db.execute('SELECT count(*) FROM hosts WHERE inbound_id=?', (iid,)).fetchone()[0], 1
                    )
