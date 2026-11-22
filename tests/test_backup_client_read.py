"""Offline reads of Backup v3 with the official panel's SQLite layout."""
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tarfile
import unittest
from unittest.mock import patch

from test_integrated_features import helper
from test_recovery_extract import RecoveryExtract, SCRIPT


DB_MEMBER = 'files/etc/x-ui/x-ui.db'
RAW_PATH = '/raw_1/'
JSON_PATH = '/json-2/'
# Synthetic values only. Keep the declared columns and defaults aligned with
# Setting, ClientRecord, ClientInbound and ClientTraffic in official 3x-ui.
SCHEMA = """
CREATE TABLE settings (id integer PRIMARY KEY AUTOINCREMENT, key text, value text);
CREATE INDEX idx_settings_key ON settings(key);
CREATE TABLE clients (
    id integer PRIMARY KEY AUTOINCREMENT, email text NOT NULL UNIQUE, sub_id text,
    uuid text, password text, auth text, flow text, security text, reverse text,
    client_host_rule_ids text, wg_private_key text, wg_public_key text, wg_allowed_ips text,
    wg_pre_shared_key text, wg_keep_alive integer DEFAULT 0,
    wg_keep_alive_range text DEFAULT '', wg_forwarded_ports text, secret text,
    ad_tag text DEFAULT '', limit_ip integer, limit_hwid integer DEFAULT 0,
    total_gb integer, expiry_time integer, enable numeric DEFAULT true, tg_id integer,
    group_name text DEFAULT '', comment text, reset integer DEFAULT 0,
    reset_day integer DEFAULT 0, reset_weekday integer DEFAULT 0, reset_max integer DEFAULT 0,
    traffic_reset text DEFAULT 'never', traffic_reset_day integer DEFAULT 1,
    created_at integer, updated_at integer, sync_orphaned_at integer DEFAULT 0
);
CREATE TABLE client_inbounds (
    client_id integer, inbound_id integer, flow_override text, created_at integer,
    PRIMARY KEY(client_id, inbound_id)
);
CREATE TABLE client_traffics (
    id integer PRIMARY KEY AUTOINCREMENT, inbound_id integer, enable numeric, email text UNIQUE,
    up integer, down integer, expiry_time integer, total integer, reset integer DEFAULT 0,
    reset_day integer DEFAULT 0, reset_weekday integer DEFAULT 0, reset_max integer DEFAULT 0,
    reset_count integer DEFAULT 0, last_online integer DEFAULT 0, last_sub_fetch integer DEFAULT 0
);
"""
CLIENTS = (
    {
        'id': 7, 'email': 'attached', 'sub_id': 'fixture-sub-a',
        'uuid': '00000000-0000-4000-8000-000000000001', 'password': 'fixture-password',
        'auth': 'fixture-auth', 'flow': 'xtls-rprx-vision', 'security': 'auto',
        'reverse': '{"tag":"fixture-reverse"}', 'client_host_rule_ids': '[42]',
        'wg_private_key': 'fictional-private-key', 'wg_public_key': 'fictional-public-key',
        'wg_pre_shared_key': 'fictional-psk', 'wg_allowed_ips': '10.0.0.2/32, fd00::2/128',
        'wg_keep_alive_range': '20-40', 'wg_forwarded_ports': '80,443',
        'secret': 'fictional-protocol-secret', 'ad_tag': 'fictional-ad-tag',
        'limit_ip': 3, 'limit_hwid': 2, 'total_gb': 21474836480, 'expiry_time': 1900000000000,
        'enable': 1, 'tg_id': 1234, 'group_name': 'fixture-group', 'comment': 'fixture-comment',
        'reset': 7, 'reset_day': 0, 'reset_weekday': 2, 'reset_max': 5,
        'traffic_reset': 'monthly', 'traffic_reset_day': 3,
        'created_at': 1700000000000, 'updated_at': 1700000000001, 'sync_orphaned_at': 123,
    },
    {'id': 9, 'email': 'disabled', 'sub_id': 'fixture-sub-b', 'enable': 0,
     'expiry_time': -86400000, 'wg_keep_alive_range': '0'},
    {'id': 10, 'email': 'unattached', 'sub_id': 'fixture-sub-c', 'enable': 1,
     'wg_keep_alive': 25},
)


class BackupClientRead(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        RecoveryExtract.setUpClass()
        cls.reader = helper('client')

    def setUp(self):
        self.fixture = RecoveryExtract('runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.sqlite = self.fixture.root / 'panel.sqlite'
        self.staging = self.fixture.root / 'tmp'
        self.staging.mkdir()
        env = {**self.fixture.env, 'PATH': self.fixture.env['PATH'] + ':/usr/bin:/bin'}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.assert_private_state_cleaned)
        self.connect = sqlite3.connect
        with closing(self.connect(self.sqlite)) as db, db:
            db.executescript(SCHEMA)
            db.executemany('INSERT INTO settings(key,value) VALUES (?,?)',
                           [('subPath', RAW_PATH), ('subJsonPath', JSON_PATH)])
            for client in CLIENTS:
                columns = ', '.join(client)
                placeholders = ', '.join('?' for _ in client)
                db.execute(f'INSERT INTO clients({columns}) VALUES ({placeholders})', tuple(client.values()))
            db.execute("INSERT INTO client_inbounds VALUES (7, 456, 'old-inbound-flow', 0)")
            db.execute("""INSERT INTO client_traffics(inbound_id,email,up,down,reset_count,last_online,last_sub_fetch)
                          VALUES (456,'attached',123,4567,2,1700000000010,1700000000011),
                                 (789,'deleted',900,901,0,0,0)""")

    def assert_private_state_cleaned(self):
        self.assertEqual(list(self.staging.iterdir()), [])
        self.assertFalse(self.fixture.calls.exists())

    def pack(self, **changes):
        self.fixture.pack(changes={DB_MEMBER: self.sqlite.read_bytes(), **changes})

    def read(self):
        before = self.fixture.archive.read_bytes()
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with patch.object(self.reader.tempfile, 'tempdir', str(self.staging)), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                return self.reader.read_backup_clients(self.fixture.archive, SCRIPT)
        finally:
            self.assertTrue(self.fixture.archive.read_bytes() == before, 'Source archive changed')
            self.assertEqual((stdout.getvalue(), stderr.getvalue()), ('', ''))
            self.assert_private_state_cleaned()

    def test_payload_matches_official_import_fields(self):
        self.pack()
        result = self.read()
        self.assertEqual((result['subPath'], result['subJsonPath']), (RAW_PATH, JSON_PATH))
        attached, disabled, unattached = result['clients']
        client = attached['client']
        self.assertEqual(client, {
            'email': 'attached', 'subId': 'fixture-sub-a', 'enable': True,
            'id': '00000000-0000-4000-8000-000000000001', 'password': 'fixture-password',
            'auth': 'fixture-auth', 'flow': 'xtls-rprx-vision', 'security': 'auto',
            'reverse': {'tag': 'fixture-reverse'}, 'privateKey': 'fictional-private-key',
            'publicKey': 'fictional-public-key', 'preSharedKey': 'fictional-psk',
            'allowedIPs': ['10.0.0.2/32', 'fd00::2/128'], 'keepAlive': '20-40',
            'forwardedPorts': '80,443', 'secret': 'fictional-protocol-secret', 'adTag': 'fictional-ad-tag',
            'limitIp': 3, 'limitHwid': 2, 'totalGB': 21474836480, 'expiryTime': 1900000000000,
            'tgId': 1234, 'group': 'fixture-group', 'comment': 'fixture-comment',
            'reset': 7, 'resetDay': 0, 'resetWeekday': 2, 'resetMax': 5,
            'trafficReset': 'monthly', 'trafficResetDay': 3,
            'created_at': 1700000000000, 'updated_at': 1700000000001,
        })
        self.assertEqual(attached['traffic'], {'up': 123, 'down': 4567, 'resetCount': 2,
                                              'lastOnline': 1700000000010, 'lastSubFetch': 1700000000011})
        self.assertEqual(set(attached), {'client', 'traffic'})
        self.assertIs(client['enable'], True)
        self.assertEqual(disabled['client']['email'], 'disabled')
        self.assertIs(disabled['client']['enable'], False)
        self.assertEqual(disabled['client']['expiryTime'], -86400000)
        self.assertEqual(disabled['client']['keepAlive'], 0)
        self.assertEqual(unattached['client']['email'], 'unattached')
        self.assertEqual(unattached['client']['keepAlive'], 25)
        self.assertEqual(set(unattached), {'client'})

    def test_wal_is_read_in_private_staging(self):
        with closing(self.connect(self.sqlite)) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=wal').fetchone(), ('wal',))
            db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute("UPDATE clients SET enable=0 WHERE email='unattached'")
            db.commit()
            extra = []
            for suffix in ('-wal', '-shm'):
                payload = Path(str(self.sqlite) + suffix).read_bytes()
                member = tarfile.TarInfo(DB_MEMBER + suffix)
                member.size = len(payload)
                extra.append((member, payload))
            self.fixture.pack(changes={DB_MEMBER: self.sqlite.read_bytes()}, extra=extra)

        def checked_connect(database_uri, **options):
            database = next(self.staging.rglob('x-ui.db'))
            directories = {p for p in self.staging.rglob('*') if p.is_dir()}
            files = set(self.staging.rglob('*')) - directories
            self.assertEqual(files, {database, database.with_name('x-ui.db-wal')})
            for path in directories:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            for path in files:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(database_uri, database.as_uri() + '?mode=ro')
            return self.connect(database_uri, **options)

        with patch.object(self.reader.sqlite3, 'connect', side_effect=checked_connect):
            self.assertIs(self.read()['clients'][2]['client']['enable'], False)

    def test_optional_columns_and_tables(self):
        with closing(self.connect(self.sqlite)) as db, db:
            db.executescript("""
                ALTER TABLE clients ADD COLUMN future_data blob;
                UPDATE clients SET future_data=x'ff00';
                ALTER TABLE clients DROP COLUMN traffic_reset_day;
                ALTER TABLE client_traffics DROP COLUMN last_sub_fetch;
                DROP TABLE client_inbounds;
            """)
        self.pack()
        item = self.read()['clients'][0]
        self.assertNotIn('future_data', item['client'])
        self.assertNotIn('trafficResetDay', item['client'])
        self.assertNotIn('lastSubFetch', item['traffic'])
        with closing(self.connect(self.sqlite)) as db, db:
            db.execute('DROP TABLE client_traffics')
        self.pack()
        self.assertTrue(all('traffic' not in item for item in self.read()['clients']))

    def test_corrupt_archive_and_missing_db(self):
        self.pack()
        original = self.fixture.archive.read_bytes()
        for invalid in (b'not gzip', original[:-8]):
            self.fixture.archive.write_bytes(invalid)
            with self.assertRaisesRegex(ValueError, 'Invalid or unsafe backup archive'):
                self.read()
        self.fixture.pack(omit=(DB_MEMBER,))
        with self.assertRaisesRegex(ValueError, 'missing required managed state'):
            self.read()

    def test_unsupported_backup_versions(self):
        metadata = json.loads(next(data for member, data in self.fixture.entries if member.name == 'meta.json'))
        for version in (2, 4, '3'):
            with self.subTest(version=version):
                self.pack(**{'meta.json': json.dumps({**metadata, 'format_version': version}).encode()})
                with self.assertRaisesRegex(ValueError, 'backup format'):
                    self.read()

    def test_corrupt_sqlite(self):
        self.pack(**{DB_MEMBER: b'not SQLite: fixture-password'})
        with self.assertRaisesRegex(ValueError, r'^Archived x-ui.db could not be read as SQLite\.$'):
            self.read()

    def test_wal_link_is_rejected(self):
        wal = tarfile.TarInfo(DB_MEMBER + '-wal')
        wal.type = tarfile.SYMTYPE
        wal.linkname = 'x-ui.db'
        self.fixture.pack(changes={DB_MEMBER: self.sqlite.read_bytes()}, extra=[(wal, None)])
        with self.assertRaisesRegex(ValueError, 'SQLite WAL must be a regular file'):
            self.read()

    def test_missing_required_client_columns(self):
        with closing(self.connect(self.sqlite)) as db, db:
            db.execute('ALTER TABLE clients DROP COLUMN sub_id')
        self.pack()
        with self.assertRaisesRegex(ValueError, r'clients\.sub_id'):
            self.read()

    def test_missing_required_client_value(self):
        with closing(self.connect(self.sqlite)) as db, db:
            db.execute("UPDATE clients SET enable=NULL WHERE email='disabled'")
        self.pack()
        with self.assertRaisesRegex(ValueError, r'clients\.enable'):
            self.read()

    def test_missing_or_invalid_paths(self):
        with closing(self.connect(self.sqlite)) as db, db:
            db.execute("DELETE FROM settings WHERE key='subJsonPath'")
        self.pack()
        with self.assertRaisesRegex(ValueError, 'subJsonPath'):
            self.read()
        with closing(self.connect(self.sqlite)) as db, db:
            db.execute("INSERT INTO settings(key,value) VALUES ('subJsonPath','/unsafe?query/')")
        self.pack()
        with self.assertRaisesRegex(ValueError, 'subJsonPath'):
            self.read()
        with closing(self.connect(self.sqlite)) as db, db:
            db.execute("INSERT INTO settings(key,value) VALUES ('subPath','/also-raw/')")
        self.pack()
        with self.assertRaisesRegex(ValueError, 'subPath'):
            self.read()
