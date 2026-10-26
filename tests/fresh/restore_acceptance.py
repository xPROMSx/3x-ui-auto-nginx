"""Separate source/clean-target disks; reuse the existing QEMU/network boundaries."""
import ast
import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import threading
import time
import traceback

import acceptance as host

ROOT=host.ROOT
REF=os.environ['XUI_AUTO_REF']
report=host.report
phase='prepare'
os.umask(0o077)


def invoke(expression, name, timeout=180):
    global phase
    phase=name
    started=time.monotonic()
    code=('import sys,json;from pathlib import Path;sys.path.insert(0,"/root/pilot");'
          'import checks,restore_checks;result='+expression+';'
          'Path("/root/pilot/restore-result.json").write_text(json.dumps(result))')
    result=host.ssh('python3 -c '+shlex.quote(code),timeout)
    (ROOT/(name+'.private.log')).write_bytes(result.stdout+result.stderr)
    if result.returncode:
        report[name+'_guest_exit']=result.returncode
        if expression.startswith('restore_checks.restore('):
            saved=host.ssh('cat /root/pilot/last-restore-outcome.json',15)
            if saved.returncode==0:
                outcome=json.loads(saved.stdout)
                assert set(outcome)=={'exit','core','overall'} and type(outcome['exit']) is int
                assert outcome['core'] in ('success','not_started','failed_rolled_back','failed_unverified','unknown')
                assert outcome['overall'] in ('SUCCESS','PARTIAL','FAILED','UNKNOWN')
                report[name+'_outcome']=outcome
        import re
        text=re.sub(r'\x1b\[[0-9;]*m','',(result.stdout+result.stderr).decode(errors='replace'))
        frames=re.findall(r'File "/root/pilot/(checks|restore_checks)\.py", line ([0-9]+)',text)
        if frames:report['failure_location']=frames[-1][0]+'.py:'+frames[-1][1]
        raise AssertionError('Guest phase failed; private output not published')
    result=host.ssh('cat /root/pilot/restore-result.json',15)
    values=json.loads(result.stdout)
    assert values and isinstance(values,dict) and all(v is True for v in values.values())
    report[name]='PASS';report[name+'_seconds']=round(time.monotonic()-started,2)
    print(name+': PASS',flush=True)


def observe_reality(name, source=None):
    invoke('restore_checks.reality_observation()',name+'-outcome-captured',90)
    value=host.ssh('cat /root/pilot/mihomo-reality-observation.json',15)
    assert value.returncode==0
    outcome=json.loads(value.stdout)
    assert outcome in ({'status':'PASS','reason':''},
        {'status':'FAIL','reason':'REALITY authentication failed','curl_exit':52})
    report[name]=outcome
    print(name+': '+outcome['status']+(' ('+outcome['reason']+')' if outcome['reason'] else ''),flush=True)
    if source is not None and outcome['status']=='FAIL':
        assert source==outcome,'New Mihomo REALITY failure after Restore'
    return outcome


def copy_to_guest(local, remote):
    with Path(local).open('rb') as source:
        result=subprocess.run(['ssh','-i','/pilot/ssh-key','-o','UserKnownHostsFile=/pilot/known_hosts',
            '-o','StrictHostKeyChecking=accept-new','-p','22220','root@127.0.0.1',
            'umask 077; cat > '+shlex.quote(remote)],stdin=source,capture_output=True,timeout=120)
    assert result.returncode==0,'Private transfer failed'


def start_disk(name, backing='/pilot/clean.raw', backing_format='raw'):
    global phase
    phase=name;host.phase=name
    disk=ROOT/(name+'.qcow2')
    subprocess.run(['qemu-img','create','-q','-f','qcow2','-F',backing_format,'-b',str(backing),str(disk)],check=True)
    host.boot(str(disk),'qcow2')
    copy_to_guest(host.REPO/'tests/fresh/restore_checks.py','/root/pilot/restore_checks.py')
    return disk


def poweroff():
    # Only the new COW Restore disks advertise discard. Reclaim deleted private
    # staging/cache blocks before retaining a disk or using it as a snapshot base.
    assert host.ssh('fstrim /',60).returncode==0
    host.poweroff()


def prepare_target():
    started=time.monotonic()
    # The shared clean OS carries an ACME endpoint fixture for source issuance.
    # A Restore target gets cli.ini from its archive, after native Certbot APT;
    # precreating that conffile would cause an unrelated dpkg question in the TTY.
    code=("from pathlib import Path;p=Path('/etc/letsencrypt/cli.ini');"
          "assert not Path('/etc/x-ui/x-ui.db').exists();"
          "assert not p.is_symlink() and p.read_bytes()==b'server = https://10.0.2.2:14000/dir\\n';p.unlink()")
    assert host.ssh('python3 -c '+shlex.quote(code),15).returncode==0
    copy_to_guest(host.REPO/'tests/fresh/restore_checks.py','/root/pilot/restore_checks.py')
    copy_to_guest(host.REPO/'x-ui-latest.sh','/root/pilot/x-ui-latest.sh')
    copy_to_guest(ROOT/'mihomo','/root/pilot/mihomo')
    assert host.ssh('chmod 0700 /root/pilot/mihomo',15).returncode==0
    for name in ('source-backup.tar.gz','source-state.private.json','source-sha256','installer.raw.log','backup-adguard.private.json'):
        copy_to_guest(ROOT/name,'/root/pilot/'+name)
    copy_to_guest(ROOT/'pebble-root.crt','/usr/local/share/ca-certificates/pilot-pebble.crt')
    # Updating the normal OS trust store is test CA preparation, not Restore logic.
    assert host.ssh('update-ca-certificates >/dev/null',90).returncode==0
    report[phase+'_target_prepare_seconds']=round(time.monotonic()-started,2)


if __name__=='__main__':
    sampling=threading.Event()
    def sample():
        while not sampling.wait(.5):
            try:
                if host.vm is not None and host.vm.poll() is None:
                    data=Path(f'/proc/{host.vm.pid}/status').read_text()
                    rss=int(next(l.split()[1] for l in data.splitlines() if l.startswith('VmHWM:')))*1024
                    report['qemu_peak_rss_bytes']=max(report.get('qemu_peak_rss_bytes',0),rss)
                report['disk_allocated_bytes']=max(report.get('disk_allocated_bytes',0),sum(p.stat().st_blocks*512 for p in ROOT.glob('*.qcow2')))
                size=sum(p.stat().st_blocks*512 for p in ROOT.rglob('*') if p.is_file())
                report['workspace_peak_allocated_bytes']=max(report.get('workspace_peak_allocated_bytes',0),size)
            except (OSError,StopIteration):pass
    sampler=threading.Thread(target=sample,daemon=True);sampler.start()
    try:
        report['source_ref']=REF
        report['image_prepare_seconds']=int((ROOT/'images.ready').read_text())-int((ROOT/'started').read_text())
        report['disk_prepare_seconds']=int((ROOT/'disk.ready').read_text())-int((ROOT/'images.ready').read_text())
        report['prepared_disk_reused']=not (ROOT/'rootfs.tar').exists()
        # The prepared clean disk already contains this invocation's exported OS.
        (ROOT/'rootfs.tar').unlink(missing_ok=True)
        if (ROOT/'root').exists():shutil.rmtree(ROOT/'root')
        host.fixtures()
        pins={}
        for node in ast.parse((host.REPO/'tests/test_real_integration.py').read_text()).body:
            if isinstance(node,ast.Assign) and isinstance(node.targets[0],ast.Name) and node.targets[0].id in ('MIHOMO_VERSION','MIHOMO_SHA256'):
                pins[node.targets[0].id]=ast.literal_eval(node.value)
        version=pins['MIHOMO_VERSION'];path=ROOT/'mihomo.gz'
        subprocess.run(['curl','-q','-fsSL','--connect-timeout','15','--max-time','180',
            f'https://github.com/MetaCubeX/mihomo/releases/download/{version}/mihomo-linux-amd64-compatible-{version}.gz',
            '-o',str(path)],check=True,timeout=190,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        assert hashlib.sha256(path.read_bytes()).hexdigest()==pins['MIHOMO_SHA256']
        (ROOT/'mihomo').write_bytes(gzip.decompress(path.read_bytes()))
        source_disk=start_disk('source-install')
        os_release=host.ssh('cat /etc/os-release',15).stdout.decode()
        host.install('positive',REF)
        invoke('checks.installed()','source-installed')
        invoke('checks.transports()','source-transport-consumers',300)
        invoke('checks.amneziawg()','source-AWG',120)
        copy_to_guest(ROOT/'mihomo','/root/pilot/mihomo')
        assert host.ssh('chmod 0700 /root/pilot/mihomo',15).returncode==0
        invoke('restore_checks.mihomo("Vision")','source-Mihomo-Vision',90)
        source_reality=observe_reality('source-Mihomo-REALITY')
        invoke('restore_checks.source_backup()','source-backup',180)
        for name,remote in [('source-backup.tar.gz',None),('source-state.private.json','/root/pilot/source-state.private.json'),
                            ('source-sha256','/root/pilot/source-sha256'),('installer.raw.log','/root/pilot/installer.raw.log'),
                            ('backup-adguard.private.json','/root/pilot/backup-adguard.private.json'),
                            ('pebble-root.crt','/usr/local/share/ca-certificates/pilot-pebble.crt')]:
            if remote is None:
                path=host.ssh('cat /root/pilot/source-path',15)
                assert path.returncode==0
                remote=path.stdout.decode().strip()
            result=host.ssh('cat '+shlex.quote(remote),120)
            assert result.returncode==0
            (ROOT/name).write_bytes(result.stdout)
        digest=(ROOT/'source-sha256').read_text()
        assert hashlib.sha256((ROOT/'source-backup.tar.gz').read_bytes()).hexdigest()==digest
        poweroff()
        # Source data is now the verified private archive; no scenario reuses its disk.
        source_disk.unlink()
        # Prove a positive clean recovery before the native fault matrix.
        target=start_disk('preserve');prepare_target()
        invoke('restore_checks.restore("preserve",'+repr(REF)+')','clean-preserve-restore',660)
        invoke('checks.installed(security=False)','restored-services-and-subscriptions')
        invoke('checks.transports()','restored-transport-consumers',300)
        invoke('checks.amneziawg()','restored-AWG',120)
        invoke('restore_checks.mihomo("Vision")','restored-Mihomo-Vision',90)
        observe_reality('restored-Mihomo-REALITY',source_reality)
        invoke('restore_checks.preserved()','restored-identity-and-archive')
        assert host.ssh("tr -d '-' < /proc/sys/kernel/random/boot_id > /root/pilot/first-boot-id",15).returncode==0
        poweroff()
        host.phase='restored-cold-boot';host.boot(str(target),'qcow2')
        invoke('checks.installed(cold=True,security=False)','restored-cold-services')
        invoke('checks.transports()','restored-cold-consumers',300)
        invoke('checks.amneziawg()','restored-cold-AWG',120)
        invoke('restore_checks.mihomo("Vision")','restored-cold-Mihomo-Vision',90)
        observe_reality('restored-cold-Mihomo-REALITY',source_reality)
        invoke('restore_checks.preserved()','restored-cold-identity')
        invoke('restore_checks.restore("ufw-unknown",'+repr(REF)+')','native-unknown-SSH-no-UFW-enable',660)
        poweroff()
        # Keep this powered-off restored No-Security disk as the negative-case base.
        preserved_target=target
        # Ubuntu 24.04 has one full clean recovery/cold boot. The main 26.04
        # acceptance additionally exercises real optional failures from clean disks.
        scenarios=('security','fail2ban','dot-outage') if '26.04' in os_release else ('security',)
        for scenario in scenarios:
            if scenario=='dot-outage':(ROOT/'dot-outage').touch()
            if scenario=='security':
                target=start_disk('optional-'+scenario);prepare_target()
            else:
                # These are explicitly EXISTING managed targets, not fake clean VPSes.
                target=start_disk('optional-'+scenario,preserved_target,'qcow2')
                assert host.ssh("sqlite3 /etc/x-ui/x-ui.db \"UPDATE settings SET value='previous target' WHERE key='subTitle';\"",15).returncode==0
            invoke('restore_checks.restore('+repr(scenario)+','+repr(REF)+')','optional-'+scenario,660)
            # A new SSH connection after UFW enable must still reach the real server.
            assert host.ssh('/usr/sbin/sshd -t && systemctl is-active --quiet ssh',15).returncode==0
            report['optional-'+scenario+'-new-SSH']='PASS'
            if scenario=='security':
                invoke('checks.installed()','full-security-services')
                assert host.ssh("tr -d '-' < /proc/sys/kernel/random/boot_id > /root/pilot/first-boot-id",15).returncode==0
                poweroff();host.phase='security-cold-boot';host.boot(str(target),'qcow2')
                invoke('checks.installed(cold=True)','security-cold-services')
                invoke('checks.transports()','security-cold-consumers',300)
                invoke('checks.amneziawg()','security-cold-AWG',120)
                invoke('restore_checks.preserved()','security-cold-identity')
                invoke('restore_checks.mihomo("Vision")','security-cold-Mihomo-Vision',90)
                observe_reality('security-cold-Mihomo-REALITY',source_reality)
                assert host.ssh('/usr/sbin/sshd -t',15,port=22221).returncode==0
                report['native_nonstandard_socket_SSH_after_UFW']='PASS'
                invoke('restore_checks.restore("preserve",'+repr(REF)+')','existing-preserve-security',660)
                invoke('checks.installed()','existing-preserved-services')
                invoke('checks.renewal()','restored-real-renewal-and-hooks',300)
            else:
                invoke('checks.transports()','partial-'+scenario+'-healthy-core',300)
            poweroff();target.unlink()
            (ROOT/'dot-outage').unlink(missing_ok=True)
        start_disk('core-failure');prepare_target()
        invoke('restore_checks.restore("core-failure",'+repr(REF)+')','real-core-rollback',660)
        poweroff();(ROOT/'core-failure.qcow2').unlink()
        assert hashlib.sha256((ROOT/'source-backup.tar.gz').read_bytes()).hexdigest()==digest
        preserved_target.unlink()
        report['result']='PASS' if source_reality['status']=='PASS' else 'PASS for Restore acceptance; existing Mihomo REALITY failure recorded'
    except Exception as error:
        (ROOT/'failure.private.log').write_text(traceback.format_exc())
        report['result']='FAIL';report['error_class']=type(error).__name__;report['phase']=phase
        print(phase+': FAIL; private logs/images are never uploaded',flush=True)
        raise SystemExit(1)
    finally:
        sampling.set();sampler.join(timeout=2)
        for process in reversed(host.processes):
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        report['total_seconds']=round(time.time()-float((ROOT/'started').read_text()),2)
        peak=Path('/sys/fs/cgroup/memory.peak')
        if peak.exists():report['container_peak_bytes']=int(peak.read_text())
        (ROOT/'restore-summary.json').write_text(json.dumps(report))
        print(json.dumps(report,sort_keys=True),flush=True)
