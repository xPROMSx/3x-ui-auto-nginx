# 3X-UI AUTO NGINX — Copyright (C) 2026 xPROMSx contributors
# SPDX-License-Identifier: GPL-3.0-only
# Additional attribution terms: see docs/NOTICE.md
"""Build a private tree of project-owned leaves using the Installation renderers."""

import filecmp
import functools
import importlib.util
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / 'assets/update/driver.sh'
DB = Path('/etc/x-ui/x-ui.db')
MANIFEST = DB.with_name('auto-nginx-managed.json')
NGINX = Path('/etc/nginx')
PANEL = Path('/usr/local/x-ui')
HOOK = Path('/etc/letsencrypt/renewal-hooks/deploy')


@functools.lru_cache(None)
def module(name, filename='managed.py'):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'assets' / name / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(*args, data=None, timeout=120, lock=False, cwd=None):
    cmd = list(map(str, args))
    result = subprocess.run(
        cmd, input=data, capture_output=True, timeout=timeout, cwd=cwd, pass_fds=(9,) if lock else ()
    )
    if result.returncode:
        # Upstream stderr and arguments can contain credentials.
        raise RuntimeError(
            f'{Path(cmd[0]).name} {Path(cmd[1]).name if len(cmd) > 1 else ""} exited {result.returncode}'
        )
    return result.stdout


def metadata():
    info = MANIFEST.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('Unsafe auto-nginx-managed.json')
    state = json.loads(MANIFEST.read_text())
    if (
        state.get('format') != 1
        or state.get('layout') != 'nginx-stream-v1'
        or type(state.get('update_contract')) is not int
        or state['update_contract'] != 1
    ):
        raise ValueError('Update requires update_contract 1')
    for value in (*state['domains'].values(), *state.get('retained_domains', {})):
        if value and not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+', value):
            raise ValueError('Invalid managed domain name')
    return state


def version(binary, flag):
    output = run(binary, flag)
    match = re.search(rb'\bv?(\d+)\.(\d+)\.(\d+)\b', output)
    if not match:
        raise ValueError(f'Cannot establish version of {binary.name}')
    return tuple(map(int, match.groups()))


def versions():
    baseline = json.loads(run('bash', DRIVER, 'baseline'))
    arch = run('bash', DRIVER, 'arch').decode().strip()
    xray = (
        PANEL / 'bin' / ('xray-linux-arm32' if arch in ('armv5', 'armv6', 'armv7') else 'xray-linux-' + arch)
    )
    upgrade = False
    for path, flag, key in ((PANEL / 'x-ui', '-v', 'version'), (xray, 'version', 'xray')):
        current = version(path, flag)
        target = tuple(map(int, baseline[key].lstrip('v').split('.')))
        if current > target:
            raise ValueError(
                f'{path.name} {current} is newer than verified {target}; automatic downgrade refused'
            )
        upgrade |= current < target
    return upgrade, xray


def ensure_parent(path):
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError(f'Cannot publish through symlinked directory {parent}')


def file_info(path):
    if path.is_symlink():
        return ('link', os.readlink(path))
    if not path.exists():
        return ('absent',)
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode):
        raise ValueError(f'Managed file path is occupied: {path}')
    return ('file', stat.S_IMODE(s.st_mode), s.st_uid, s.st_gid)


def same(a, b):
    meta = file_info(a)
    return meta == file_info(b) and (meta[0] != 'file' or filecmp.cmp(a, b, shallow=False))


def fsync(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_tree(directory):
    for parent, _, files in os.walk(directory, topdown=False):
        for name in files:
            path = Path(parent) / name
            if not path.is_symlink():
                fsync(path)
        fsync(parent)


def render(directory, state, rows, params, release=None):
    dest = directory / 'files'
    assets = ROOT / 'assets'

    def at(path):
        return dest / str(path).lstrip('/')

    def copy(source, target, mode):
        p = at(target)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, p)
        p.chmod(mode)
        return p

    # The original IP is a display value; the installer uses the local route source.
    route = subprocess.run(['ip', '-4', 'route', 'get', '8.8.8.8'], capture_output=True).stdout
    local_ip = re.search(rb'\bsrc\s+([0-9.]+)', route)
    values = dict(
        params,
        ip=local_ip[1].decode() if local_ip else '',
        adguard=int(state['features'].get('adguard', False)),
        vision=state['domains'].get('vision') or '',
    )
    fields = b''.join(
        (str(k) + '\0' + str(v) + '\0').encode()
        for k, v in values.items()
        if k not in ('private_key', 'public_key', 'short_ids')
    )
    run('bash', DRIVER, 'render', dest, data=fields)
    ng = at(NGINX)
    retained = list(
        dict.fromkeys([params['domain'], params['reality_domain'], *state.get('retained_domains', {})])
    )
    if len(retained) > 2:
        http = ng / 'sites-available/80.conf'
        original = [params['domain'], params['reality_domain']]
        content = http.read_text().replace(
            'server_name ' + ' '.join(original) + ';', 'server_name ' + ' '.join(retained) + ';'
        )
        pattern = lambda names: '|'.join(n.replace('.', r'\.') for n in names)
        http.write_text(content.replace('^(' + pattern(original) + ')$', '^(' + pattern(retained) + ')$'))
    main = NGINX / 'nginx.conf'
    content = main.read_bytes()
    # Only the project's one-line stream include belongs to Update. Preserve
    # the live DoT decision and every other byte in the mixed-ownership file.
    block = re.compile(
        rb'^stream \{ include /etc/nginx/stream-enabled/\*\.conf;(?: include /etc/nginx/stream-dot\.d/\*\.conf;)? \}$',
        re.M,
    )
    match = block.search(content)
    if match:
        dot = 'y' if b'include /etc/nginx/stream-dot.d/*.conf;' in match[0] else 'n'
        desired = run('bash', DRIVER, 'stream-block', dot).rstrip(b'\n')
        if match[0] != desired:
            copy(main, main, stat.S_IMODE(main.stat().st_mode)).write_bytes(
                content[: match.start()] + desired + content[match.end() :]
            )
    if values['vision']:
        row = rows['vision']
        port = int(row['settings']['fallbacks'][0]['dest'].rsplit(':', 1)[-1])
        vision = module('vision')
        (ng / 'sites-available/vision.conf').write_text(
            vision.fallback_config(values['vision'], port, root='/var/www/html/cover-vision', rate_limit=True)
        )
        (ng / 'sites-available/vision-acme.conf').write_text(vision.acme_config(values['vision']))
        stream = ng / 'stream-enabled/stream.conf'
        stream.write_text(vision.stream_config(stream.read_text(), values['vision'], row['port']))
    if 'xhttp_cdn' in rows:
        row = rows['xhttp_cdn']
        cdn = ng / 'snippets/x-ui-auto-optional/xhttp-cdn.conf'
        cdn.parent.mkdir(parents=True, exist_ok=True)
        cdn.write_text(
            module('xhttp-cdn').nginx(row['port'], row['stream_settings']['xhttpSettings']['path'])
        )
    for site in (ng / 'sites-available').iterdir():
        link = ng / 'sites-enabled' / site.name
        link.parent.mkdir(exist_ok=True)
        link.symlink_to(NGINX / 'sites-available' / site.name)
    for leaf in ng.rglob('*'):
        if leaf.is_file() and not leaf.is_symlink() and leaf != ng / 'nginx.conf':
            private = leaf.name in (params['domain'], 'hysteria-cover.conf', 'adguard.conf', 'xhttp-cdn.conf')
            leaf.chmod(0o600 if private else 0o644)
    for p in at(HOOK).iterdir():
        p.chmod(0o755)
    at('/etc/systemd/system/mtr-backend.service').chmod(0o644)
    account = pwd.getpwnam('www-data')
    for p in at('/var/www').rglob('*'):
        if p.is_file():
            os.chown(p, account.pw_uid, account.pw_gid)
            p.chmod(0o644)
    copy(assets / 'diagnostics/mtr-backend.py', '/usr/local/lib/3x-ui-pro/mtr-backend.py', 0o755)
    copy(assets / 'backup/x-ui-backup.sh', '/usr/local/bin/x-ui-backup', 0o755)
    if values['adguard']:
        copy(assets / 'adguard/managed.sh', '/usr/local/lib/3x-ui-pro/managed-adguard.sh', 0o600)
    covers = directory / 'covers'
    roles = ['panel', 'reality'] + (['vision'] if values['vision'] else [])
    module('cover-sites').prepare(covers, DB.with_name('cover-sites.json'), roles, from_saved=True)
    for role in roles:
        for p in (covers / ('cover-' + role)).rglob('*'):
            if p.is_file():
                copy(p, Path('/var/www/html') / p.relative_to(covers), 0o644)
    distribution = release if release else PANEL
    run('bash', DRIVER, 'unit', distribution, at('/etc/systemd/system/x-ui.service'))
    at('/etc/systemd/system/x-ui.service').chmod(0o644)
    copy(distribution / 'x-ui.sh', '/usr/bin/x-ui', 0o755)
    if release:
        for p in release.rglob('*'):
            if not p.is_file():
                continue
            target = PANEL / p.relative_to(release)
            # Geo data may already have been independently updated by the panel.
            if target.name == 'config.json' or (p.suffix == '.dat' and target.exists()):
                continue
            copy(p, target, stat.S_IMODE(p.stat().st_mode))
    conntrack = run('bash', DRIVER, 'conntrack').strip()
    if conntrack:
        tuning = json.loads(conntrack)
        for name, content in tuning.pop('files').items():
            leaf = at(name)
            leaf.parent.mkdir(parents=True, exist_ok=True)
            leaf.write_text(content)
            leaf.chmod(0o644)
        if tuning['before'] != tuning['after']:
            (directory / 'sysctl.json').write_text(json.dumps(tuning))
    changes = []
    for leaf in sorted(dest.rglob('*')):
        if leaf.is_symlink() or leaf.is_file():
            live = Path('/') / leaf.relative_to(dest)
            ensure_parent(live)
            if live.is_symlink() and (not leaf.is_symlink() or live.readlink() != leaf.readlink()):
                raise ValueError(f'Unexpected link at managed destination {live}')
            if not same(live, leaf):
                changes.append(str(live))
    return changes


def candidate_nginx(directory, changes):
    tree = directory / 'nginx'
    run('cp', '-a', '--reflink=auto', '--', NGINX, tree)
    for name in changes:
        live = Path(name)
        if live.is_relative_to(NGINX):
            target = tree / live.relative_to(NGINX)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.unlink(missing_ok=True)
            shutil.copy2(directory / 'files' / name.lstrip('/'), target, follow_symlinks=False)
    module('client', 'restore.py').candidate_nginx(tree, NGINX)
