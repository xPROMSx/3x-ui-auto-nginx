# 3X-UI AUTO NGINX - https://github.com/xPROMSx/3x-ui-auto-nginx
# Copyright (C) 2026 xPROMSx contributors
# SPDX-License-Identifier: GPL-3.0-only
# Additional attribution terms: see docs/NOTICE.md

"""Core transport definitions and the SQLite adapter for a new installation."""
import json
from pathlib import Path
import secrets
import sqlite3
import string
import sys


def core_inbound(role, params):
    domain = params['domain']
    reality = params['reality_domain']
    listen = '127.0.0.1'
    protocol = 'vless'
    settings = dict(decryption='none', encryption='none', fallbacks=[])
    if role == 'reality':
        port = 8443
        stream = dict(network='tcp', security='reality', realitySettings=dict(
            show=False, xver=1, target='127.0.0.1:9443', serverNames=[reality],
            privateKey=params['private_key'], minClient='', maxClient='', maxTimediff=0,
            shortIds=params['short_ids'], settings=dict(publicKey=params['public_key'],
                fingerprint='firefox', serverName='', spiderX='/')),
            tcpSettings=dict(acceptProxyProtocol=True, header=dict(type='none')))
    elif role == 'ws':
        port = int(params['ws_port'])
        stream = dict(network='ws', security='none', wsSettings=dict(
            acceptProxyProtocol=False, path=params['ws_route'], host=domain, headers={}))
    elif role == 'xhttp':
        listen, port = '/dev/shm/uds2023.sock,0666', 0
        stream = dict(network='xhttp', security='none',
                      xhttpSettings=dict(path=params['xhttp_route'], mode='stream-up'),
                      sockopt=dict(trustedXForwardedFor=['X-Forwarded-For']))
    elif role == 'grpc':
        protocol, port = 'trojan', int(params['trojan_port'])
        settings = dict(fallbacks=[])
        stream = dict(network='grpc', security='none', grpcSettings=dict(
            serviceName=params['service_name'], authority=domain, multiMode=False))
    elif role == 'hysteria2':
        protocol, listen, port = 'hysteria', '', 443
        settings = dict(version=2)
        stream = dict(network='hysteria', security='tls', hysteriaSettings=dict(
            version=2, udpIdleTimeout=60, masquerade=dict(type='proxy',
                url='unix:/run/3x-ui-hysteria-cover.sock', rewriteHost=False)),
            tlsSettings=dict(serverName=reality, rejectUnknownSni=True, alpn=['h3'],
                certificates=[dict(certificateFile=params['certificate'], keyFile=params['key_file'],
                    ocspStapling=0, oneTimeLoading=False, usage='encipherment', buildChain=False)]))
    else:
        raise ValueError('Unknown core inbound role')
    return dict(protocol=protocol, listen=listen, port=port, settings=settings, streamSettings=stream,
                sniffing=dict(enabled=True, destOverride=['http','tls','quic','fakedns'],
                              metadataOnly=False, routeOnly=False))


def core_host(role, params):
    tls = role in ('ws', 'xhttp', 'grpc')
    alpn = ['http/1.1'] if role == 'ws' else ['h2','http/1.1'] if tls else []
    return dict(remark='trojan' if role == 'grpc' else role,
                address=params['reality_domain'] if role == 'hysteria2' else params['domain'],
                port=443, security='tls' if tls else 'same', fingerprint='firefox' if tls else '',
                alpn=alpn)


def new_group_id():
    return ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(16))


def insert_initial_inbounds(db, params):
    """Create empty roles and their Hosts in one transaction, returning the assigned IDs."""
    grouped = any(row[1] == 'group_id' for row in db.execute('PRAGMA table_info(hosts)'))
    created = {}
    with db:
        for role in ('reality', 'ws', 'xhttp', 'grpc', 'hysteria2'):
            if role in ('ws', 'grpc') and not params['ws_grpc']:
                continue
            inbound = core_inbound(role, params)
            name = 'trojan-grpc' if role == 'grpc' else role
            port = inbound['port']
            tag = {'hysteria2':'inbound-443-udp', 'xhttp':'inbound-/dev/shm/uds2023.sock,0666:0|'}.get(role, f'inbound-{port}')
            iid = db.execute('''INSERT INTO inbounds
                (user_id, up, down, total, remark, enable, expiry_time, listen, port, protocol,
                 settings, stream_settings, tag, sniffing) VALUES (1,0,0,0,?,1,0,?,?,?,?,?,?,?)''',
                (params['flag']+' '+name, inbound['listen'], port, inbound['protocol'],
                 json.dumps(dict(clients=[], **inbound['settings'])), json.dumps(inbound['streamSettings']),
                 tag, json.dumps(inbound['sniffing']))).lastrowid
            host = core_host(role, params)
            host.update(inbound_id=iid, sort_order=0, alpn=json.dumps(host['alpn'], separators=(',', ':')))
            if grouped:
                host['group_id'] = new_group_id()
            columns = ','.join(host)
            placeholders = ','.join('?' for _ in host)
            hid = db.execute(f'INSERT INTO hosts ({columns}) VALUES ({placeholders})', tuple(host.values())).lastrowid
            created[role] = [iid, hid]
    return created


def main():
    # Bash builtins deliver all per-installation values over a private pipe.
    fields = ('domain', 'reality_domain', 'flag', 'private_key', 'public_key', 'ws_grpc', 'ws_port',
              'ws_route', 'trojan_port', 'service_name', 'xhttp_route', 'certificate', 'key_file')
    *values, end = sys.stdin.buffer.read().decode().split('\0')
    if end or len(values) != len(fields) + 8:
        raise ValueError('Incomplete installation parameters')
    params = dict(zip(fields, values))
    params.update(short_ids=values[len(fields):], ws_grpc=params['ws_grpc'] == 'y')
    with sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=rw', uri=True) as db:
        created = insert_initial_inbounds(db, params)
    print(json.dumps(created))


if __name__ == '__main__':
    main()
