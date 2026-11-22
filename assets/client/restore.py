# 3X-UI AUTO NGINX - https://github.com/xPROMSx/3x-ui-auto-nginx
# Copyright (C) 2026 xPROMSx contributors
# SPDX-License-Identifier: GPL-3.0-only
# Additional attribution terms (GPL-3.0 section 7) apply to material added by xPROMSx contributors: see docs/NOTICE.md

"""Restore client identities through the official panel API and switch local subscription paths."""

from collections import Counter
from contextlib import closing
import getpass
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlsplit


DB = Path('etc/x-ui/x-ui.db')
MANIFEST = DB.with_name('auto-nginx-managed.json')
INCLUDES = Path('etc/nginx/snippets/includes.conf')
CLASH = Path('var/www/subpage/clash.yaml.tpl')
SETTINGS = ('subPath', 'subURI', 'subJsonPath', 'subJsonURI')
MISSING = object()
MIHOMO = 'mihomo'
CREDENTIALS = {
    'vless': ('id', 'security'), 'trojan': ('password',), 'hysteria': ('auth',),
    'amneziawg': ('privateKey', 'publicKey', 'preSharedKey', 'keepAlive', 'forwardedPorts'),
}


def load_module(path):
    spec = importlib.util.spec_from_file_location(path.parent.name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, timeout=60).stdout


def connect(root, mode='ro'):
    return closing(sqlite3.connect((root / DB).as_uri() + '?mode=' + mode, uri=True, timeout=10))


def local_settings(root):
    with connect(root) as db:
        return dict(db.execute('SELECT key,value FROM settings ORDER BY id DESC'))


def stored_paths(settings):
    return {key: settings[key] for key in SETTINGS if key in settings}


def write_paths(root, settings):
    with connect(root, 'rw') as db, db:
        # settings.key is indexed but not unique in official 3x-ui.
        for key in SETTINGS:
            if key not in settings:
                db.execute('DELETE FROM settings WHERE key=?', (key,))
            elif not db.execute(
                'UPDATE settings SET value=? WHERE id=(SELECT MIN(id) FROM settings WHERE key=?)',
                (settings[key], key),
            ).rowcount:
                db.execute('INSERT INTO settings(key,value) VALUES (?,?)', (key, settings[key]))


def managed_origin(root, settings):
    manifest = json.loads((root / MANIFEST).read_text())
    domain = manifest['domains']['panel']
    if (manifest['format'] != 1 or manifest['layout'] != 'nginx-stream-v1'
            or not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+', domain)
            or settings['webCertFile'] != f'/root/cert/{domain}/fullchain.pem'
            or any(settings.get(key, '') not in ('', domain) for key in ('webDomain', 'subDomain'))):
        raise ValueError('Panel domain and managed installation disagree')
    return 'https://' + domain


def edited_file(path, original, origin, old_raw, old_json, raw, json_path):
    # Match all five generated headers in one pass (the two archived paths can
    # be swapped). Their bodies and every other nginx directive stay in place.
    if path == INCLUDES:
        before = (old_raw, old_raw[:-1], '^' + old_raw, old_json[:-1], old_json)
        after = (raw, raw[:-1], '^' + raw, json_path[:-1], json_path)
        forms = (
            (r'location[ \t]+', r'[ \t]*\{'),
            (r'location[ \t]+=[ \t]+', r'[ \t]*\{'),
            (r'location[ \t]+~[ \t]+', r'\(\?<clash_sub_id>\[\^/\]\+\)\$[ \t]*\{'),
            (r'location[ \t]+', r'[ \t]*\{'),
            (r'location[ \t]+', r'[ \t]*\{'),
        )
        spans = []
        for (prefix, suffix), source, target in zip(forms, before, after):
            pattern = r'^[ \t]*' + prefix + '(' + re.escape(source) + ')' + suffix
            matches = list(re.finditer(pattern, original, re.M))
            if len(matches) != 1:
                raise ValueError('Managed nginx subscription header is missing or ambiguous')
            spans.append((*matches[0].span(1), target))
        for start, end, target in sorted(spans, reverse=True):
            original = original[:start] + target + original[end:]
        return original
    pattern = r'(^[ \t]*url:[ \t]*' + re.escape(origin) + ')' + re.escape(old_raw) + r'(\$\{SUB_ID\}\?provider=1[ \t]*$)'
    result, count = re.subn(pattern, lambda match: match[1] + raw + match[2], original, flags=re.M)
    if count != 1:
        raise ValueError('Managed Mihomo provider URL is missing or ambiguous')
    return result


def atomic_write(target, data, mode, uid, gid):
    fd, temporary = tempfile.mkstemp(prefix='.xui-client-path-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            os.fchown(out.fileno(), uid, gid)
            os.fchmod(out.fileno(), mode)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def candidate_nginx(candidate, live):
    # Both live vhosts include the snippet by absolute path. An alternate
    # nginx.conf alone cannot substitute it through that tree and external includes.
    run('unshare', '--mount', '--propagation', 'private', '--',
        'sh', '-c', 'mount --bind -- "$1" "$2" && exec nginx -t', 'sh', candidate, live)


def service_active(name):
    state = run('systemctl', 'show', '--property=ActiveState', '--value', name).decode().strip()
    return state == 'active'


def panel_settings(panel):
    return panel.request('panel/api/setting/defaultSettings', {})['obj']


def started_panel_settings(panel):
    # systemd Type=simple can report active before the HTTPS listener opens.
    for attempt in range(10):
        try:
            return panel.request('panel/api/setting/defaultSettings', {}, timeout=2)['obj']
        except (ValueError, subprocess.SubprocessError):
            if attempt == 9:
                raise
            time.sleep(1)


def provider_url(origin, raw, sub_id):
    return origin + raw + sub_id + '?provider=1'


def has_provider(body, url):
    return re.search(rb'(?m)^[ \t]*url:[ \t]*' + re.escape(url.encode()) + rb'[ \t]*$', body) is not None


def check_routes(reader, panel, origin, paths, settings, existing):
    if not settings['subEnable']:
        return  # The master switch also stops the JSON subscription listener.
    reserved = {entry['subId'] for entry in existing}
    sub_id = reader.random_id()
    while sub_id in reserved:
        sub_id = reader.random_id()
    raw = origin + paths['subPath'] + sub_id
    if reader.fetch(panel, raw, with_status=True)[0] != 404:
        raise ValueError('RAW route')
    status, body = reader.fetch(panel, raw, user_agent=MIHOMO, with_status=True)
    provider = provider_url(origin, paths['subPath'], sub_id)
    if status != 200 or not has_provider(body, provider):
        raise ValueError('Mihomo route')
    if reader.fetch(panel, provider, user_agent=MIHOMO, with_status=True)[0] != 404:
        raise ValueError('Mihomo provider route')
    if settings['subJsonEnable'] and reader.fetch(
            panel, origin + paths['subJsonPath'] + sub_id, with_status=True)[0] != 404:
        raise ValueError('JSON route')


def apply_paths(root, reader, panel, origin, archived, current, settings, existing):
    """Switch the four settings and two project files before any import."""
    old = stored_paths(current)
    target = {key: archived[key] for key in ('subPath', 'subJsonPath')}
    target.update(subURI=origin + target['subPath'], subJsonURI=origin + target['subJsonPath'])
    old_raw = old.get('subPath', '/sub/')
    old_json = old.get('subJsonPath', '/json/')
    files = {}
    for path in (INCLUDES, CLASH):
        info = (root / path).lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Managed subscription file is not a regular file')
        data = (root / path).read_bytes()
        edited = edited_file(path, data.decode(), origin, old_raw, old_json,
                             target['subPath'], target['subJsonPath']).encode()
        files[path] = data, edited, (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)
    if old == target and all(data == edited for data, edited, _ in files.values()):
        check_routes(reader, panel, origin, target, settings, existing)
        return 'already applied', [], None

    if not service_active('x-ui') or not service_active('nginx'):
        raise ValueError('x-ui and nginx must be active')

    snapshot = Path(tempfile.mkdtemp(prefix='xui-client-paths-', dir=root / 'var/lib'))
    retain = False
    try:
        previous = {'settings': old, 'services': {'x-ui': 'active', 'nginx': 'active'}, 'files': {}}
        for path, (data, _, attributes) in files.items():
            (snapshot / path.name).write_bytes(data)
            previous['files'][str(path)] = dict(zip(('mode', 'uid', 'gid'), attributes))
        (snapshot / 'previous.json').write_text(json.dumps(previous))
        candidate = snapshot / 'candidate'
        candidate.write_bytes(files[INCLUDES][1])
        try:
            candidate_nginx(candidate, root / INCLUDES)
        except (OSError, subprocess.SubprocessError):
            return 'candidate nginx -t failed', [], None
        finally:
            candidate.unlink()

        stage = 'stop x-ui'
        changed_db = False
        replaced = []
        reloaded = False
        try:
            run('systemctl', 'stop', 'x-ui')
            stage = 'write SQLite settings'
            changed_db = True
            write_paths(root, target)
            for path, (data, edited, attributes) in files.items():
                if data != edited:
                    stage = 'replace ' + path.name
                    replaced.append(path)
                    atomic_write(root / path, edited, *attributes)
            stage = 'start x-ui and verify settings'
            run('systemctl', 'start', 'x-ui')
            actual = started_panel_settings(panel)
            if (not service_active('x-ui') or stored_paths(local_settings(root)) != target
                    or any(actual[key] != target[key] for key in ('subURI', 'subJsonURI'))):
                raise ValueError(stage)
            if INCLUDES in replaced:
                stage = 'reload nginx'
                reloaded = True
                run('systemctl', 'reload', 'nginx')
            stage = 'verify HTTPS routes'
            if not service_active('nginx'):
                raise ValueError(stage)
            attempts = 6 if reloaded else 1
            for attempt in range(attempts):
                try:
                    check_routes(reader, panel, origin, target, settings, existing)
                    break
                except ValueError:
                    # ExecReload signals nginx before the new workers accept HTTPS.
                    if attempt == attempts - 1:
                        raise
                    time.sleep(1)
        except Exception:
            failed = rollback_paths(root, panel, old, files, changed_db, replaced, reloaded)
            retain = bool(failed)
            return stage, failed, snapshot if retain else None
        return 'applied', [], None
    finally:
        if not retain:
            shutil.rmtree(snapshot)


def rollback_paths(root, panel, old, files, changed_db, replaced, reloaded):
    failed = []
    if changed_db:
        try:
            if service_active('x-ui'):
                run('systemctl', 'stop', 'x-ui')
            write_paths(root, old)
        except Exception:
            pass
    for path in reversed(replaced):
        data, _, attributes = files[path]
        try:
            atomic_write(root / path, data, *attributes)
        except Exception:
            pass
    try:
        if stored_paths(local_settings(root)) != old:
            failed.append('SQLite settings')
    except Exception:
        failed.append('SQLite settings')
    for path, (data, _, attributes) in files.items():
        try:
            info = (root / path).stat()
            if ((root / path).read_bytes(), (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)) != (data, attributes):
                failed.append(path.name)
        except OSError:
            failed.append(path.name)

    for name in ('x-ui', 'nginx'):
        try:
            if not service_active(name):
                run('systemctl', 'start', name)
            elif name == 'nginx' and reloaded:
                run('systemctl', 'reload', name)
            if not service_active(name):
                failed.append(name)
        except Exception:
            failed.append(name)
    if 'x-ui' not in failed:
        try:
            restored = started_panel_settings(panel)
            if any(restored[key] != old[key] for key in ('subURI', 'subJsonURI') if old.get(key)):
                failed.append('x-ui')
        except Exception:
            failed.append('x-ui')
    return failed


def plan_import(archived, existing, rows):
    emails = {item['email'].strip().casefold() for item in existing}
    sub_ids = {item['subId'] for item in existing}
    counts = Counter(item['client']['subId'] for item in archived)
    inbound_ids = sorted(row['id'] for row in rows)
    pending, skipped = [], []
    for item in archived:
        client = dict(item['client'])
        email, sub_id = client['email'].strip(), client['subId']
        if not sub_id:
            reason = 'empty subId'
        elif counts[sub_id] > 1:
            reason = 'shared archived subId'
        elif email.casefold() in emails:
            reason = 'email already present'
        elif sub_id in sub_ids:
            reason = 'subId already present'
        else:
            emails.add(email.casefold())
            client['email'] = email
            for field in ('allowedIPs', 'allowedIPsByInbound', 'clientHostRuleIds', 'updated_at'):
                client.pop(field, None)
            pending.append({**item, 'client': client, 'inboundIds': inbound_ids})
            continue
        skipped.append((email, reason))
    return pending, skipped


def inbound_clients(reader, row):
    return reader.object_value(row['settings']).get('clients') or []


def effective_flow(reader, row, client):
    stream = reader.object_value(row.get('streamSettings') or {})
    settings = reader.object_value(row['settings'])
    eligible = (stream.get('network') in ('tcp', 'raw') and stream.get('security') in ('tls', 'reality')
                or any(settings.get(key) not in (None, '', 'none') for key in ('encryption', 'decryption')))
    if row['protocol'] == 'vless' and not row.get('disableFlow') and stream.get('network') != 'xdrive' and eligible:
        return client.get('flow', '')
    return ''


def awg_addresses(reader, row, attached):
    server = reader.object_value(row['settings'])['server']
    networks = [ipaddress.ip_network(f"{server['subnetIp']}/{server.get('subnetCidr') or 24}", strict=False)]
    if server.get('ipv6Enabled') and server.get('ipv6Subnet'):
        networks.append(ipaddress.ip_network(server['ipv6Subnet'], strict=False))
    ips = [ipaddress.ip_interface(value).ip for value in attached.get('allowedIPs', [])]
    others = [ipaddress.ip_interface(value).ip for entry in inbound_clients(reader, row)
              if entry['email'] != attached['email'] for value in entry.get('allowedIPs', [])]
    return (len(ips) == len(networks) and all(sum(ip in network for ip in ips) == 1 for network in networks)
            and not set(ips).intersection(others))


def matches_record(reader, item, record, attached):
    client = item['client']
    if set(record.get('inboundIds') or []) != set(item['inboundIds']) or set(attached) != set(item['inboundIds']):
        return False
    credentials = {key for row, _ in attached.values() for key in CREDENTIALS[row['protocol']]}
    optional = {key for keys in CREDENTIALS.values() for key in keys}
    for key, value in client.items():
        if key == 'flow' or key in optional and (key not in credentials or value == ''):
            continue  # The panel can generate credentials absent from the archive.
        actual = record.get({'id': 'uuid', 'created_at': 'createdAt'}.get(key, key), MISSING)
        if key == 'reverse':
            actual = json.loads(actual or 'null')
        if key == 'keepAlive':
            actual, value = str(actual or '0'), str(value)
        if key == 'created_at' and value == 0:
            continue  # The official import assigns a timestamp for zero.
        if key == 'trafficReset' and value == '':
            value = 'never'
        if actual != value:
            return False
    traffic = record.get('traffic') or {}
    if any(traffic.get(key) != value for key, value in item.get('traffic', {}).items()):
        return False
    for row, matches in attached.values():
        if len(matches) != 1:
            return False
        entry = matches[0]
        if entry.get('enable') is not client['enable'] or entry.get('subId') != client['subId']:
            return False
        if entry.get('flow', '') != effective_flow(reader, row, client):
            return False
        for key in CREDENTIALS[row['protocol']]:
            if key == 'keepAlive':
                if key in client and str(entry.get(key) or 0) != str(client[key]):
                    return False
            elif client.get(key) and entry.get(key) != client[key]:
                return False
        if row['protocol'] == 'amneziawg' and not awg_addresses(reader, row, entry):
            return False
    return True


def import_clients(reader, rejected, panel, pending, skipped):
    attempts = []
    for item in pending:
        email = item['client']['email']
        try:
            result = panel.request('panel/api/clients/import', {'data': json.dumps([item])})['obj']
            reply = 'accepted' if result['created'] == 1 and not result.get('skipped') else 'API rejected import'
        except rejected:
            reply = 'API rejected import'
        except Exception:
            attempts.append((item, 'import response lost or invalid'))
            skipped.extend((later['client']['email'], 'import stopped after lost response')
                           for later in pending[len(attempts):])
            break
        attempts.append((item, reply))

    full, incomplete, issues = [], [], []
    changed = any(reply == 'accepted' for _, reply in attempts)
    try:
        records = {record['email']: record for record in panel.request('panel/api/clients/list')['obj']}
        rows = panel.request('panel/api/inbounds/list')['obj']
        attached = {}
        attempted = {item['client']['email'] for item, _ in attempts}
        for row in rows:
            for client in inbound_clients(reader, row):
                email = client['email']
                if email in attempted:
                    _, matches = attached.setdefault(email, {}).setdefault(row['id'], (row, []))
                    matches.append(client)
    except Exception:
        issues.append('panel readback failed')
        incomplete.extend((item['client']['email'], 'could not verify panel state') for item, _ in attempts)
    else:
        for item, reply in attempts:
            email = item['client']['email']
            record, bindings = records.get(email), attached.get(email, {})
            changed |= bool(record or bindings)
            if reply == 'import response lost or invalid':
                incomplete.append((email, reply))
            elif not record and not bindings:
                skipped.append((email, reply if reply != 'accepted' else 'client missing after import'))
            elif reply != 'accepted':
                incomplete.append((email, reply))
            else:
                try:
                    verified = record and matches_record(reader, item, record, bindings)
                except (ValueError, KeyError, TypeError):
                    verified = False
                if verified:
                    full.append((record, bindings))
                else:
                    incomplete.append((email, 'client, attachments or traffic differ'))
    if changed:
        try:
            panel.request('panel/api/server/restartXrayService', {})
        except Exception:
            issues.append('Xray restart failed')
    return full, incomplete, issues


def usable(record):
    traffic = record.get('traffic') or {}
    expiry = record['expiryTime']
    limit = record['totalGB']
    return (record['enable'] and traffic.get('enable', True) and (expiry <= 0 or expiry > time.time() * 1000)
            and (limit <= 0 or traffic.get('up', 0) + traffic.get('down', 0) < limit))


def json_endpoints(configs):
    return {(protocol, server['address'], server['port'])
            for config in configs for outgoing in config['outbounds']
            if (protocol := outgoing['protocol']) in ('vless', 'trojan', 'hysteria')
            for server in (outgoing['settings'].get('vnext') or outgoing['settings'].get('servers')
                           or [outgoing['settings']])}


def verify_subscriptions(reader, panel, origin, paths, settings, full):
    if not settings['subEnable']:
        return 'subscription service disabled'
    candidates = [(record, bindings) for record, bindings in full if usable(record)]
    if not candidates:
        return 'no usable imported client'
    candidate = next(((record, bindings) for record, bindings in candidates if not record.get('limitHwid')), None)
    if candidate is None:
        return 'HTTP probe would occupy a device slot'
    record, bindings = candidate
    sub_id = quote(record['subId'], safe='')
    url = origin + paths['subPath'] + sub_id
    official = panel.request('panel/api/clients/links/' + quote(record['email'], safe=''))['obj']
    protocols = Counter(row['protocol'] for row, _ in bindings.values())
    schemes = {'vless': 'vless', 'trojan': 'trojan', 'hy2': 'hysteria', 'hysteria2': 'hysteria', 'vpn': 'amneziawg'}
    links = [urlsplit(link) for link in official]
    coverage = Counter(schemes[link.scheme] for link in links)
    if any(coverage[protocol] < count for protocol, count in protocols.items()):
        return 'official links miss an active inbound'
    expected = Counter(map(reader.link_identity, official))
    endpoints = {(schemes[link.scheme], link.hostname, link.port) for link in links if link.scheme != 'vpn'}
    for _ in range(2):
        actual = Counter(map(reader.link_identity, reader.decode_subscription(reader.fetch(panel, url))))
        if actual != expected:
            return 'RAW endpoints or credentials differ'
        if endpoints:
            provider = provider_url(origin, paths['subPath'], sub_id)
            if not has_provider(reader.fetch(panel, url, user_agent=MIHOMO), provider):
                return 'Mihomo YAML provider URL differs'
            actual = Counter(map(reader.link_identity, reader.decode_subscription(
                reader.fetch(panel, provider, user_agent=MIHOMO))))
            if actual != expected:
                return 'Mihomo provider endpoints or credentials differ'
        if settings['subJsonEnable'] and endpoints:
            configs = json.loads(reader.fetch(panel, origin + paths['subJsonPath'] + sub_id))
            if json_endpoints(configs) != endpoints:
                return 'JSON endpoints differ'
    return 'verified'


MESSAGES = {
    'empty subId': 'пустой идентификатор подписки',
    'shared archived subId': 'один идентификатор подписки у нескольких клиентов архива',
    'email already present': 'email уже занят',
    'subId already present': 'идентификатор подписки уже занят',
    'API rejected import': 'панель отклонила импорт',
    'import response lost or invalid': 'ответ на импорт потерян или повреждён',
    'import stopped after lost response': 'импорт остановлен после потери ответа',
    'client missing after import': 'после импорта запись отсутствует',
    'could not verify panel state': 'состояние записи не подтверждено',
    'client, attachments or traffic differ': 'запись, привязки или трафик отличаются',
    'panel readback failed': 'не удалось перечитать клиентов и подключения',
    'Xray restart failed': 'не удалось перезапустить Xray',
    'archive read': 'чтение Backup v3',
    'managed state': 'сверка домена и настроек панели',
    'panel login': 'вход в панель',
    'import plan': 'получение клиентов, активных локальных подключений и плана',
    'subscription paths or candidate nginx test': 'подготовка путей или проверка кандидата nginx -t',
    'candidate nginx -t failed': 'nginx -t отклонил конфигурацию кандидата',
    'stop x-ui': 'остановка x-ui',
    'write SQLite settings': 'запись настроек SQLite',
    'replace includes.conf': 'замена includes.conf',
    'replace clash.yaml.tpl': 'замена clash.yaml.tpl',
    'start x-ui and verify settings': 'запуск x-ui и проверка настроек',
    'reload nginx': 'перечитывание конфигурации nginx',
    'verify HTTPS routes': 'проверка маршрутов HTTPS',
    'not applied': 'не применены',
    'rolled back': 'предыдущее состояние восстановлено',
    'rollback unverified': 'откат не подтверждён',
    'already applied': 'уже действовали',
    'applied': 'применены',
    'not checked': 'не выполнена',
    'subscription service disabled': 'служба подписок выключена',
    'no usable imported client': 'нет пригодного импортированного клиента для HTTP-проверки',
    'HTTP probe would occupy a device slot': 'для HTTP-проверки пришлось бы занять слот устройства',
    'official links miss an active inbound': 'в официальных ссылках отсутствует активное подключение',
    'RAW endpoints or credentials differ': 'RAW содержит другие адреса подключений или учётные данные',
    'Mihomo YAML provider URL differs': 'в YAML Mihomo указан другой адрес провайдера',
    'Mihomo provider endpoints or credentials differ': 'провайдер Mihomo вернул другие адреса подключений или учётные данные',
    'JSON endpoints differ': 'JSON содержит другие адреса подключений',
    'subscription check failed': 'проверка загруженных подписок завершилась ошибкой',
    'verified': 'подтверждена для включённых форматов и поддерживаемых протоколов',
    'SQLite settings': 'настройки SQLite',
}


def report(ru, status, paths, subscription='not checked', *, full=(), skipped=(),
           incomplete=(), issues=(), recovery=None):
    def tr(en, translated):
        return translated if ru else en

    def reason(key):
        return MESSAGES.get(key, key) if ru else key

    print('  ' + status)
    print(tr('  Subscription paths — ', '  Пути подписок — ') + reason(paths))
    print(tr('  Fully restored — ', '  Полностью восстановлено — ') + str(len(full)))
    print(tr('  Skipped — ', '  Пропущено — ') + str(len(skipped)))
    for email, why in skipped:
        print('    ' + ascii(email) + ' — ' + reason(why))
    print(tr('  Incomplete or uncertain — ', '  Неполных или неподтверждённых записей — ') + str(len(incomplete)))
    for email, why in incomplete:
        print('    ' + ascii(email) + ' — ' + reason(why))
    print(tr('  Subscriptions and connections — ', '  Подписки и подключения — ') + reason(subscription))
    for issue in issues:
        print('    ' + reason(issue))
    if recovery:
        print(tr('  Private recovery files — ', '  Приватные файлы для восстановления — ') + str(recovery))
    if status == 'PARTIAL':
        print(tr('  Links previously issued by this server with different paths may stop working.',
                 '  Ссылки, ранее выданные этим сервером с другими путями, могут перестать работать.'))
    if status in ('SUCCESS', 'PARTIAL'):
        print(tr('  Check clients, limits, expiry and connections in 3x-ui. Refresh subscriptions in your apps.',
                 '  Проверьте клиентов, лимиты, сроки и подключения в 3x-ui. Обновите подписки в приложениях.'))
        print(tr('  Point the previous domain to this server if needed.',
                 '  При необходимости направьте прежний домен на этот сервер.'))
    return 0 if status == 'SUCCESS' else 1


def restore(root, reader, session, archive, backup, directory, ru):
    stage = 'archive read'
    try:
        archived = reader.read_backup_clients(archive, backup)
        stage = 'managed state'
        current = local_settings(root)
        origin = managed_origin(root, current)
        web_base = current['webBasePath']
        if not re.fullmatch(r'/(?:[A-Za-z0-9_-]+/)+', web_base):
            raise ValueError(stage)
        stage = 'panel login'
        # Opening the terminal first avoids getpass falling back to visible stdin.
        with open('/dev/tty', 'w') as tty:
            login = getpass.getpass('  Логин панели: ' if ru else '  Panel login: ', tty)
            password = getpass.getpass('  Пароль панели: ' if ru else '  Panel password: ', tty)
            panel = session.Panel(origin + web_base, login, password, directory,
                                  ['--resolve', f'{urlsplit(origin).hostname}:443:127.0.0.1'],
                                  two_factor=lambda: getpass.getpass('  Код 2FA: ' if ru else '  2FA code: ', tty))
        stage = 'managed state'
        settings = panel_settings(panel)
        if any(settings.get(key) not in ('', urlsplit(origin).hostname) for key in ('webDomain', 'subDomain')):
            raise ValueError(stage)
        stage = 'import plan'
        rows = reader.active_inbounds(panel)
        existing = panel.request('panel/api/clients/list')['obj']
        pending, skipped = plan_import(archived['clients'], existing, rows)
        stage = 'subscription paths or candidate nginx test'
        paths, rollback, recovery = apply_paths(root, reader, panel, origin, archived, current, settings, existing)
    except EOFError:
        return 2
    except Exception:
        return report(ru, 'FAILED', 'not applied', issues=(stage,))

    if paths not in ('applied', 'already applied'):
        if paths == 'candidate nginx -t failed':
            return report(ru, 'FAILED', 'not applied', issues=(paths,))
        if rollback:
            return report(ru, 'STATE_UNVERIFIED', 'rollback unverified',
                          issues=(paths, *rollback), recovery=recovery)
        return report(ru, 'FAILED', 'rolled back', issues=(paths,))

    full, incomplete, issues = import_clients(reader, session.PanelRejected, panel, pending, skipped)
    try:
        checked = verify_subscriptions(reader, panel, origin, archived, settings, full)
    except Exception:
        checked = 'subscription check failed'
    status = 'PARTIAL' if skipped or incomplete or issues or checked != 'verified' else 'SUCCESS'
    return report(ru, status, paths, checked, full=full, skipped=skipped, incomplete=incomplete, issues=issues)


def interrupted(signum, frame):
    raise InterruptedError


def main():
    archive, language = sys.argv[1:]
    stage = Path(__file__).resolve().parent
    reader = load_module(stage / 'client/managed.py')
    session = load_module(stage / 'amneziawg/managed.py')
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    os.umask(0o077)
    return restore(Path('/'), reader, session, archive, stage / 'x-ui-backup.sh', stage, language == 'ru')


if __name__ == '__main__':
    sys.exit(main())
