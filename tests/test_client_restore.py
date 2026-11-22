"""Partial client restore with real installer templates and isolated failure fixtures."""

from contextlib import closing
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_integrated_features import helper
from test_personal_xhttp import FIXTURE, ROOT, SHARED, render

spec = spec_from_file_location('restore_clients', ROOT / 'assets/client/restore.py')
restore = module_from_spec(spec)
spec.loader.exec_module(restore)
reader = helper('client')
session = helper('amneziawg')

ORIGIN = 'https://deploy.example'
SETTINGS = {'subPath': '/subscription/', 'subJsonPath': '/jsonsub/', 'subURI': ORIGIN + '/subscription/'}
ARCHIVED = {'subPath': '/old/raw/', 'subJsonPath': '/old/json/'}
FORMATS = {'subEnable': True, 'subJsonEnable': True}
TARGET = {**ARCHIVED, 'subURI': ORIGIN + ARCHIVED['subPath'], 'subJsonURI': ORIGIN + ARCHIVED['subJsonPath']}


def archive_client(email, sub_id, **fields):
    return {'client': {'email': email, 'subId': sub_id, 'enable': True, **fields}}


class ClientRestore(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(prefix='client-restore-fixture-')
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        for folder in ('var/lib', 'etc/x-ui', restore.INCLUDES.parent, restore.CLASH.parent):
            (self.root / folder).mkdir(parents=True)
        (self.root / restore.INCLUDES).write_text(render(SHARED) + '\n# foreign rule retained\n')
        template = (ROOT / 'assets/clash/clash.yaml').read_text()
        (self.root / restore.CLASH).write_text(template.replace('${DOMAIN}', FIXTURE['domain'])
                                               .replace('${SUB_PATH}', 'subscription'))
        (self.root / restore.CLASH).chmod(0o640)
        self.before = {path: (self.root / path).read_bytes() for path in (restore.INCLUDES, restore.CLASH)}
        with closing(sqlite3.connect(self.root / restore.DB)) as db, db:
            db.execute('CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT, value TEXT)')
            db.executemany('INSERT INTO settings(key, value) VALUES (?, ?)',
                           [*SETTINGS.items(), ('subClashPath', '/keep/'), ('webPort', '12345')])
        self.config_before = restore.local_settings(self.root)
        self.panel = Mock()
        self.panel.request.side_effect = lambda *a, **kw: {'obj': {
            **FORMATS, **restore.local_settings(self.root)}}
        self.services = {'x-ui': True, 'nginx': True}
        self.calls = []

    def service_call(self, *args):
        _, action, name = args
        self.calls.append((action, name))
        if action in ('start', 'stop'):
            self.services[name] = action == 'start'

    def candidate(self, candidate, live):
        text = candidate.read_text()
        self.assertIn('location ~ ^/old/raw/(?<clash_sub_id>[^/]+)$', text)
        self.assertIn('location = /old/raw {', text)
        self.assertIn('location /old/raw/ {', text)
        self.assertIn('location /old/json {', text)
        self.assertIn('location /old/json/ {', text)
        self.assertTrue(text.endswith('# foreign rule retained\n'))
        self.assertEqual(live.read_bytes(), self.before[restore.INCLUDES])
        self.assertEqual(restore.local_settings(self.root), self.config_before)
        self.assertEqual(self.calls, [])

    def apply(self, archive=ARCHIVED, *, test=None, command=None, routes=None):
        check = Mock(side_effect=routes)
        with patch.object(restore, 'run', side_effect=command or self.service_call), \
                patch.object(restore, 'service_active', side_effect=self.services.__getitem__), \
                patch.object(restore, 'candidate_nginx', side_effect=test or self.candidate), \
                patch.object(restore, 'check_routes', check):
            result = restore.apply_paths(self.root, reader, self.panel, ORIGIN, archive,
                                         restore.local_settings(self.root), FORMATS, [])
        return result, check

    def assert_original(self):
        self.assertEqual(restore.local_settings(self.root), self.config_before)
        for path, data in self.before.items():
            self.assertEqual((self.root / path).read_bytes(), data)
        self.assertEqual((self.root / restore.CLASH).stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.services, {'x-ui': True, 'nginx': True})

    def test_candidate_failure_does_not_touch_settings_files_or_services(self):
        def reject(*args):
            self.candidate(*args)
            raise subprocess.CalledProcessError(1, ['nginx', '-t'])
        result, routes = self.apply(test=reject)
        self.assertEqual(result, ('candidate nginx -t failed', [], None))
        self.assert_original()
        self.assertEqual(self.calls, [])
        self.assertEqual(list((self.root / 'var/lib').iterdir()), [])
        routes.assert_not_called()

    def test_switches_five_headers_and_provider_then_noops_on_repeat(self):
        result, routes = self.apply()
        self.assertEqual(result, ('applied', [], None))
        self.assertEqual(self.calls, [('stop', 'x-ui'), ('start', 'x-ui'), ('reload', 'nginx')])
        self.assertEqual(restore.local_settings(self.root), {**self.config_before, **TARGET})
        self.assertEqual((self.root / restore.CLASH).read_bytes(), self.before[restore.CLASH].replace(
            b'/subscription/${SUB_ID}?provider=1', b'/old/raw/${SUB_ID}?provider=1'))
        self.assertEqual((self.root / restore.CLASH).stat().st_mode & 0o777, 0o640)
        routes.assert_called_once_with(reader, self.panel, ORIGIN, TARGET, FORMATS, [])
        self.calls.clear()
        result, _ = self.apply()
        self.assertEqual(result, ('already applied', [], None))
        self.assertEqual(self.calls, [])

    def test_swapped_paths_are_edited_once(self):
        text = restore.edited_file(restore.INCLUDES, self.before[restore.INCLUDES].decode(), ORIGIN,
                                   '/subscription/', '/jsonsub/', '/jsonsub/', '/subscription/')
        self.assertIn('location ~ ^/jsonsub/(?<clash_sub_id>[^/]+)$', text)
        self.assertIn('location = /jsonsub {', text)
        self.assertIn('location /subscription {', text)

    def test_failed_start_reload_or_route_restores_absent_keys_and_files(self):
        for target in (('start', 'x-ui'), ('reload', 'nginx'), 'route'):
            with self.subTest(target=target):
                self.calls.clear()
                failure = [target]
                def command(*args):
                    if failure and tuple(args[1:]) == failure[0]:
                        failure.pop()
                        raise RuntimeError
                    self.service_call(*args)
                if target == 'route':
                    command = self.service_call
                result, _ = self.apply(command=command, routes=ValueError if target == 'route' else None)
                self.assertIn(result[0], ('start x-ui and verify settings', 'reload nginx', 'verify HTTPS routes'))
                self.assertEqual(result[1:], ([], None))
                self.assert_original()
                self.assertEqual(list((self.root / 'var/lib').iterdir()), [])

    def test_unverified_rollback_keeps_only_private_previous_paths(self):
        def command(*args):
            if args[1:] == ('start', 'x-ui'):
                raise RuntimeError
            self.service_call(*args)
        stage, components, recovery = self.apply(command=command)[0]
        self.assertEqual(stage, 'start x-ui and verify settings')
        self.assertEqual(components, ['x-ui'])
        self.assertEqual(recovery.stat().st_mode & 0o777, 0o700)
        self.assertEqual({p.name for p in recovery.iterdir()}, {'includes.conf', 'clash.yaml.tpl', 'previous.json'})
        previous = json.loads((recovery / 'previous.json').read_text())
        self.assertEqual(previous['settings'], SETTINGS)
        self.assertEqual((recovery / 'includes.conf').read_bytes(), self.before[restore.INCLUDES])

    def test_conflicts_include_orphans_case_exact_subid_and_entire_shared_group(self):
        archive = [
            archive_client(' Existing ', 'a'), archive_client('different', 'current-id'),
            archive_client('AlSo', 'other-id'), archive_client('one', 'shared'),
            archive_client('two', 'shared'), archive_client('empty', ''),
            archive_client(' imported ', 'CURRENT-ID', enable=False, limitHwid=2,
                           allowedIPs=['10.0.0.2/32'], allowedIPsByInbound={'3': ['10.0.1.2/32']}),
        ]
        existing = [{'email': 'existing', 'subId': 'current-id', 'inboundIds': None},
                    {'email': 'also', 'subId': 'other-id', 'inboundIds': None}]
        pending, skipped = restore.plan_import(archive, existing, [{'id': 17}, {'id': 3}])
        self.assertEqual(pending, [{**archive_client('imported', 'CURRENT-ID', enable=False, limitHwid=2),
                                   'inboundIds': [3, 17]}])
        self.assertEqual(len(skipped), 6)
        self.assertEqual([email for email, why in skipped if why == 'shared archived subId'], ['one', 'two'])
        self.assertFalse(restore.plan_import(archive, [*existing, pending[0]['client']], [{'id': 17}])[0])

    def test_active_inbounds_excludes_disabled_remote_and_unsupported(self):
        self.panel.request.side_effect = None
        self.panel.request.return_value = {'obj': [
            {'id': 1, 'enable': True, 'nodeId': None, 'protocol': 'vless'},
            {'id': 2, 'enable': False, 'protocol': 'vless'},
            {'id': 3, 'enable': True, 'nodeId': 42, 'protocol': 'trojan'},
            {'id': 4, 'enable': True, 'protocol': 'socks'},
        ]}
        self.assertEqual([r['id'] for r in reader.active_inbounds(self.panel)], [1])

    def test_explicit_failure_can_leave_partial_client_and_loss_stops_posts(self):
        pending, skipped = restore.plan_import(
            [archive_client('first', 'one'), archive_client('second', 'two'), archive_client('third', 'three')],
            [], [{'id': 1}, {'id': 2}])
        sent = []
        def request(path, body=None):
            if path.endswith('/import'):
                item, = json.loads(body['data'])
                self.assertEqual(item['inboundIds'], [1, 2])
                sent.append(item['client']['email'])
                if len(sent) == 1:
                    raise session.PanelRejected
                raise subprocess.TimeoutExpired('curl', 20)
            if path == 'panel/api/clients/list':
                return {'obj': [{'email': 'first', 'subId': 'one', 'inboundIds': [1]}]}
            return {'obj': []}
        self.panel.request.side_effect = request
        full, incomplete, issues = restore.import_clients(reader, session.PanelRejected, self.panel, pending, skipped)
        self.assertEqual(sent, ['first', 'second'])
        self.assertEqual(full, [])
        self.assertEqual(issues, [])
        self.assertEqual(incomplete, [('first', 'API rejected import'), ('second', 'import response lost or invalid')])
        self.assertEqual(skipped, [('third', 'import stopped after lost response')])
        self.panel.request.assert_called_with('panel/api/server/restartXrayService', {})

    def test_absent_probe_ignores_existing_subid_and_formats_disabled(self):
        settings = {'subEnable': True, 'subJsonEnable': False}
        with patch.object(reader, 'random_id', side_effect=['retained', 'missing']), \
                patch.object(reader, 'fetch', side_effect=[
                    (404, b''), (200, b'url: https://deploy.example/old/raw/missing?provider=1\n'), (404, b''),
                ]) as fetch:
            restore.check_routes(reader, self.panel, ORIGIN, TARGET, settings, [{'subId': 'retained'}])
        self.assertEqual(fetch.call_count, 3)
        self.assertTrue(all('/old/raw/missing' in call.args[1] for call in fetch.call_args_list))

    def test_menu_selects_number_or_path_and_cancels_before_lock_or_download(self):
        shell = """
source "$1"
ui_header() { :; }
ui_wrap() { :; }
backup_list() { BACKUP_CHOICES=(/test/first.tar.gz /test/second.tar.gz); }
acquire_stack_lock() { printf 'locked:%s\\n' "$archive"; return 1; }
prepare_backup_helper() { echo 'unexpected helper replacement'; }
pin_project_source() { echo 'unexpected helper download'; }
UI_LANG=ru
restore_clients_action
"""
        for text, selected, code in (
            ('', None, 0), ('\n', None, 0), ('2\n', None, 0), ('2\nn\n', None, 0),
            ('2\nN\n', None, 0), ('2\ny\n', '/test/second.tar.gz', 1),
            ('/tmp/copy.tar.gz\nY\n', '/tmp/copy.tar.gz', 1), ('2\n\n', '/test/second.tar.gz', 1),
        ):
            with self.subTest(text=text):
                result = subprocess.run(['bash', '-c', shell, 'fixture', str(ROOT / 'x-ui-latest.sh')],
                                        input=text, text=True, capture_output=True)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertNotIn('unexpected', result.stdout)
                if selected:
                    self.assertIn('locked:' + selected, result.stdout)
                    self.assertEqual(result.stdout.count('Продолжить? [Y/n]'), 1)
                else:
                    self.assertNotIn('locked:', result.stdout)


class ClientRestoreNginx(unittest.TestCase):
    def test_candidate_uses_live_include_graph_and_leaves_the_original_snippet(self):
        import os
        import shlex
        from shutil import which

        nginx = os.environ.get('NGINX_BIN') or which('nginx')
        self.assertIsNotNone(nginx, 'Real nginx is required')
        with tempfile.TemporaryDirectory(prefix='client-restore-nginx-') as directory:
            root = Path(directory)
            config, site, external = (root / name for name in ('nginx.conf', 'site.conf', 'external.conf'))
            live, candidate = (root / name for name in ('original.conf', 'candidate.conf'))
            live.write_text('location / { return 204; }\n')
            original = live.read_bytes()
            site.write_text(f'server {{ listen 127.0.0.1:17777; include {live}; }}\n')
            external.write_text('map $http_user_agent $external_response { default valid; }\n')
            config.write_text(f'pid {root}/pid; error_log {root}/error.log; events {{}}\n'
                              f'http {{ include {external}; include {site}; }}\n')
            launcher = root / 'nginx'
            launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(nginx)} "$@" -p {shlex.quote(directory)}/ '
                                f'-c {shlex.quote(str(config))}\n')
            launcher.chmod(0o755)
            with patch.dict(os.environ, PATH=f'{directory}:{os.environ["PATH"]}'):
                candidate.write_text('location / { return 200 $external_response; }\n')
                restore.candidate_nginx(candidate, live)
                self.assertEqual(live.read_bytes(), original)
                candidate.write_text('location / { return 204; } location / { return 204; }\n')
                with self.assertRaises(subprocess.CalledProcessError):
                    restore.candidate_nginx(candidate, live)
                self.assertEqual(live.read_bytes(), original)
