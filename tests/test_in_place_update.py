"""In-place Update cutover and crash recovery, using real SQLite files."""

import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'assets/update/managed.py'
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
                update.commit(dict(target={}, sha='new'))
        self.assertEqual(update.MANIFEST.read_text(), 'previous')

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
