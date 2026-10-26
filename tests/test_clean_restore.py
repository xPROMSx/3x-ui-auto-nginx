"""Clean Restore contracts reuse the private v3 fixture and real filesystem tools."""
import datetime
import hashlib
import json
import sqlite3
import unittest


class CleanRestore(unittest.TestCase):
    def setUp(self):
        from test_personal_backup import PersonalBackup
        self.f=PersonalBackup('runTest');self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.write('/etc/systemd/system/x-ui.service','[Service]\nExecStart='+str(self.f.path('/usr/local/x-ui/x-ui'))+'\n')

    def test_expired_certificate_refuses_before_replacement(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes,serialization
        path=self.f.path('/etc/letsencrypt/live/example.com')
        cert=x509.load_pem_x509_certificate((path/'fullchain.pem').read_bytes())
        key=serialization.load_pem_private_key((path/'privkey.pem').read_bytes(),password=None)
        now=datetime.datetime.now(datetime.timezone.utc)
        expired=(x509.CertificateBuilder().subject_name(cert.subject).issuer_name(cert.issuer)
                 .public_key(key.public_key()).serial_number(2)
                 .not_valid_before(now-datetime.timedelta(days=2)).not_valid_after(now-datetime.timedelta(days=1))
                 .add_extension(cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value,False)
                 .sign(key,hashes.SHA256()))
        (path/'fullchain.pem').write_bytes(expired.public_bytes(serialization.Encoding.PEM))
        archive=self.f.backup();sha=hashlib.sha256(archive.read_bytes()).digest()
        self.f.write('/etc/x-ui/target-marker','unchanged target');(self.f.root/'commands').write_text('')
        result=self.f.run_tool('restore',archive)
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('expired',result.stderr.lower())
        self.assertEqual(self.f.path('/etc/x-ui/target-marker').read_text(),'unchanged target')
        self.assertFalse(any(c[:2]==['systemctl','stop'] for c in self.f.commands()))
        self.assertEqual(hashlib.sha256(archive.read_bytes()).digest(),sha)

    def test_native_awg_empty_stream_survives_restore(self):
        self.f.seed_amneziawg()
        dbpath=self.f.path('/etc/x-ui/x-ui.db')
        with sqlite3.connect(dbpath) as db:
            db.execute("UPDATE inbounds SET stream_settings='' WHERE protocol='amneziawg'")
            original=db.execute("SELECT id,settings FROM inbounds WHERE protocol='amneziawg'").fetchone()
        archive=self.f.backup()
        result=self.f.run_tool('restore',archive)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        with sqlite3.connect(dbpath) as db:
            row=db.execute("SELECT id,settings,stream_settings FROM inbounds WHERE protocol='amneziawg'").fetchone()
        self.assertEqual(row,(*original,''))

    def test_managed_mode_capability_is_read_only(self):
        before=self.f.path('/etc/x-ui/x-ui.db').read_bytes()
        result=self.f.run_tool('restore-plan','a'*40)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),{'mode':'managed-restore','version':1,'source_ref':'a'*40})
        self.assertEqual(self.f.path('/etc/x-ui/x-ui.db').read_bytes(),before)
        self.assertFalse(any(c[0] in ('apt-get','systemctl','ufw') for c in self.f.commands()))

    def managed(self, archive, answers='YES\nn\nn\nn\nn\n', **flags):
        import shlex
        from test_manager import tty_run
        entry=self.f.root/'managed-entry.sh'
        entry.write_text('source '+shlex.quote(str(self.f.script))+'\n'+
            ('rm() { if [[ ${1:-} == -rf && ${*: -1} == */.rollback-* ]]; then return 1; fi; command rm "$@"; };\n' if flags.pop('LATE_CLEANUP',False) else '')+
            'main managed-restore '+shlex.quote(str(archive))+' '+('a'*40)+'\n')
        return tty_run(entry,answers.encode(),timeout=90,env={**self.f.env,**flags})

    def security_fixture(self):
        # Only the remote Security process is fault-injected. cmd_restore, owner
        # guards, snapshots/replacement, health and EXIT cleanup are the real code.
        fake=self.f.root/'security-fixture.py'
        fake.write_text('''import json,os,sys,subprocess
from pathlib import Path
root=Path(os.environ['FIXTURE_ROOT'])
if sys.argv[1:] == ['--capabilities']:
 print(json.dumps({'status':'compatible','interface':'managed-restore-security:1'}));sys.exit(0)
# This independent open description must contend even after core commit.
probe=subprocess.run(['bash',str(root.parent/'backup.sh'),'lock-check'],capture_output=True)
assert probe.returncode==75,'Operation lock released during Security/recovery'
with (root/'security-calls').open('a') as f:f.write(' '.join(sys.argv[1:3])+'\\n')
if sys.argv[1]=='--prepare-dns':
 Path(sys.argv[2]).mkdir(mode=0o700);status=os.environ.get('DOT_RESULT','configured');name='dot'
elif sys.argv[1]=='--rollback-dns':status=os.environ.get('DNS_ROLLBACK','failed_rolled_back');name='dot'
else:name=sys.argv[2];status=os.environ.get(name.upper()+'_RESULT','configured')
print(json.dumps({'status':status,name:status,'warnings':[]}))
sys.exit(0 if status in ('configured','preserved') or sys.argv[1]=='--rollback-dns' and status=='failed_rolled_back' else 1)
''')
        curl=self.f.bin/'curl';original=curl.read_text()
        curl.write_text(original.replace("elif name == 'curl':", """elif name == 'curl' and any('/assets/security/managed.py' in arg for arg in args):
    import shutil
    shutil.copyfile(root/'security-fixture.py',args[args.index('-o')+1])
elif name == 'curl':"""))

    def distinguishable_archive(self):
        self.f.write('/etc/x-ui/source-marker','incoming source')
        archive=self.f.backup()
        self.f.path('/etc/x-ui/source-marker').unlink()
        self.f.write('/etc/x-ui/target-marker','previous target')
        return archive

    def test_preserve_default_and_one_archive_pass(self):
        archive=self.distinguishable_archive();digest=hashlib.sha256(archive.read_bytes()).digest()
        (self.f.root/'commands').write_text('')
        code,text=self.managed(archive,answers='YES\n\n\n\n\n')
        self.assertEqual(code,0,text);self.assertIn('Overall: SUCCESS',text)
        for name in ('dot','apt','fail2ban','ufw'):self.assertIn(name+': not_requested',text)
        self.assertIn('existing managed',text)
        self.assertFalse(self.f.path('/etc/x-ui/target-marker').exists())
        self.assertEqual(self.f.path('/etc/x-ui/source-marker').read_text(),'incoming source')
        self.assertEqual(hashlib.sha256(archive.read_bytes()).digest(),digest)
        self.assertFalse(any(c[0]=='curl' and '/assets/security/' in ' '.join(c) for c in self.f.commands()))
        self.assertEqual(sum(c[0]=='tar' and '-xzf' in c for c in self.f.commands()),1)
        self.assertEqual(sum(c[0]=='python3' and len(c)>3 and c[1]=='-' and c[3]=='inspect' for c in self.f.commands()),1)

    def test_optional_failure_and_late_cleanup_cannot_recover_old_core(self):
        self.security_fixture();archive=self.distinguishable_archive()
        for fault in ('apt','fail2ban','ufw','cleanup'):
            with self.subTest(fault=fault):
                self.f.write('/etc/x-ui/target-marker','previous target')
                self.f.path('/etc/x-ui/source-marker').unlink(missing_ok=True)
                code,text=self.managed(archive,answers='YES\ny\ny\ny\ny\n',
                    LATE_CLEANUP=fault=='cleanup',**({fault.upper()+'_RESULT':'failed_unverified'} if fault!='cleanup' else {}))
                self.assertNotEqual(code,0,text);self.assertIn('core: success',text);self.assertIn('Overall: PARTIAL',text)
                self.assertNotIn('Previous managed state',text)
                self.assertFalse(self.f.path('/etc/x-ui/target-marker').exists())
                self.assertEqual(self.f.path('/etc/x-ui/source-marker').read_text(),'incoming source')
                for name in ('dot','apt','fail2ban','ufw'):
                    self.assertIn(name+': '+('failed_unverified' if name==fault else 'configured'),text)
                self.assertTrue(list(self.f.path('/var/backups/x-ui').glob('.restore-*')))

    def test_dot_verified_failure_continues_but_unverified_stops_before_core(self):
        self.security_fixture();archive=self.distinguishable_archive()
        code,text=self.managed(archive,answers='YES\ny\ny\ny\nn\n',DOT_RESULT='failed_unverified')
        self.assertNotEqual(code,0,text);self.assertIn('core: not_started',text)
        self.assertTrue(self.f.path('/etc/x-ui/target-marker').exists())
        self.assertNotIn('--configure',(self.f.root/'security-calls').read_text())
        code,text=self.managed(archive,answers='YES\ny\ny\ny\nn\n',DOT_RESULT='failed_rolled_back')
        self.assertNotEqual(code,0,text);self.assertIn('core: success',text);self.assertIn('Overall: PARTIAL',text)
        self.assertIn('apt: configured',text);self.assertIn('fail2ban: configured',text)
        self.assertFalse(self.f.path('/etc/x-ui/target-marker').exists())

    def test_core_failure_recovers_previous_target_and_dns_without_post_security(self):
        self.security_fixture();archive=self.distinguishable_archive()
        for fault in ('verified','unverified'):
            with self.subTest(fault=fault):
                (self.f.root/'fault-used').unlink(missing_ok=True)
                (self.f.root/'security-calls').unlink(missing_ok=True)
                code,text=self.managed(archive,answers='YES\ny\ny\ny\ny\n',
                    FAIL_HEALTH_SERVICE='mtr-backend',FAIL_ONCE='1' if fault=='verified' else '',
                    DNS_ROLLBACK='failed_rolled_back' if fault=='verified' else 'failed_unverified')
                self.assertNotEqual(code,0,text)
                self.assertIn('core: failed_'+('rolled_back' if fault=='verified' else 'unverified'),text)
                self.assertIn('--rollback-dns',(self.f.root/'security-calls').read_text())
                self.assertNotIn('--configure',(self.f.root/'security-calls').read_text())
                self.assertEqual(self.f.path('/etc/x-ui/target-marker').read_text(),'previous target')

    def test_exact_confirmation_and_partial_target_refusal(self):
        archive=self.distinguishable_archive()
        for answer in ('\n','yes\n','no\n'):
            code,text=self.managed(archive,answers=answer)
            self.assertEqual(code,0,text);self.assertIn('Cancelled.',text)
            self.assertTrue(self.f.path('/etc/x-ui/target-marker').exists())
        self.f.path('/etc/x-ui/x-ui.db').unlink()
        code,text=self.managed(archive)
        self.assertNotEqual(code,0,text);self.assertIn('Partial or foreign target',text)
        self.assertTrue(self.f.path('/etc/x-ui/target-marker').exists())

    def test_ipv6_text_is_not_mistaken_for_an_active_nginx_listener(self):
        import subprocess
        config=self.f.path('/etc/nginx/nginx.conf')
        config.write_text(config.read_text().replace('http {',
            'http { server { listen 127.0.0.1:10081; location /literal { return 200 "listen [::]:10082;"; } }',1))
        archive=self.distinguishable_archive()
        code='source "$1"; restore_ipv6_available() { return 1; }; main restore "$2"'
        result=subprocess.run(['bash','-c',code,'fixture',str(self.f.script),str(archive)],
                              capture_output=True,text=True,env=self.f.env,timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertTrue(self.f.path('/etc/x-ui/source-marker').exists())

    def test_ipv4_bootstrap_preserves_preexisting_unowned_default_site(self):
        import subprocess
        target=self.f.path('/etc/nginx/sites-available/default')
        target.parent.mkdir(parents=True,exist_ok=True);target.write_text('administrator site\n')
        link=self.f.path('/etc/nginx/sites-enabled/default')
        link.parent.mkdir(parents=True,exist_ok=True);link.symlink_to(target)
        result=subprocess.run(['bash','-c','source "$1"; restore_ipv6_available() { return 1; }; install_restore_packages',
            'fixture',str(self.f.script)],env={**self.f.env,'MISSING_PACKAGES':'nginx-full nginx-common'},capture_output=True,text=True,timeout=30)
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertTrue(link.is_symlink());self.assertEqual(target.read_text(),'administrator site\n')
        self.assertFalse(any(c[:2]==['apt-get','install'] for c in self.f.commands()))

    def test_security_service_directories_readable_but_checkpoint_and_logs_private(self):
        self.security_fixture();archive=self.distinguishable_archive()
        helper=self.f.root/'security-fixture.py'
        helper.write_text(helper.read_text().replace("# This independent open description", "(root/'service-readable').mkdir(exist_ok=True)\n# This independent open description"))
        code,text=self.managed(archive,answers='YES\ny\ny\nn\nn\n',APT_RESULT='failed_unverified')
        self.assertNotEqual(code,0,text);self.assertIn('core: success',text)
        self.assertEqual((self.f.root/'service-readable').stat().st_mode&0o777,0o755)
        staging=next(self.f.path('/var/backups/x-ui').glob('.restore-*'))
        for path in (staging,staging/'dns-checkpoint',staging/'security-state'):
            self.assertEqual(path.stat().st_mode&0o777,0o700)
        for path in (staging/'managed-security.py',staging/'security-result.json',staging/'security-error.log'):
            self.assertEqual(path.stat().st_mode&0o777,0o600)


def nginx_ipv6_staged_preflight():
    """Native disabled-family proof, called by the existing Ubuntu 26.04 guest."""
    import shutil
    import socket
    from test_personal_backup import PersonalBackup
    try:
        with socket.socket(socket.AF_INET6,socket.SOCK_STREAM):pass
    except OSError:pass
    else:raise AssertionError('Requires the real ipv6.disable=1 guest')
    fixture=PersonalBackup('runTest');fixture.setUp()
    try:
        config=fixture.path('/etc/nginx/nginx.conf')
        config.write_text(config.read_text().replace('http {','http { server { listen [::]:10081; }',1))
        archive=fixture.backup();fixture.write('/etc/x-ui/target-marker','original target')
        (fixture.root/'commands').write_text('')
        result=fixture.run_tool('restore',archive,NGINX_BIN=shutil.which('nginx'))
        assert result.returncode!=0 and 'Address family not supported' in result.stderr
        assert fixture.path('/etc/x-ui/target-marker').read_text()=='original target'
        assert not any(c[:2]==['systemctl','stop'] for c in fixture.commands())
    finally:fixture.doCleanups()
    print('NATIVE staged nginx -t rejects real unavailable AF_INET6 before replacement: PASS',flush=True)
