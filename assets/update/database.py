# 3X-UI AUTO NGINX — Copyright (C) 2026 xPROMSx contributors
# SPDX-License-Identifier: GPL-3.0-only
# Additional attribution terms: see docs/NOTICE.md
"""SQLite operations for a stopped local 3x-ui service."""

import hashlib
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


def digest(path):
    with path.open('rb') as f:
        result = hashlib.sha256()
        for block in iter(lambda: f.read(1024 * 1024), b''):
            result.update(block)
        return result.hexdigest()


def holds_database(process, path):
    for fd in (process / 'fd').iterdir():
        try:
            if fd.samefile(path):
                return True
        except FileNotFoundError:
            continue
    return False


def assert_no_holders(path):
    """A foreign database handle can outlive systemctl stop (including WAL readers)."""
    files = set()
    for suffix in ('', '-wal', '-shm', '-journal'):
        try:
            st = Path(str(path) + suffix).stat()
            files.add((st.st_dev, st.st_ino))
        except FileNotFoundError:
            pass
    for proc in Path('/proc').iterdir():
        if not proc.name.isdecimal() or int(proc.name) == os.getpid():
            continue
        try:
            for fd in (proc / 'fd').iterdir():
                try:
                    st = fd.stat()
                except FileNotFoundError:
                    continue
                if (st.st_dev, st.st_ino) in files:
                    raise RuntimeError(f'Process {proc.name} still has {path.name} open')
        except (FileNotFoundError, ProcessLookupError):
            continue


@contextmanager
def stopped_database(path, confirm_stopped, holders=assert_no_holders):
    """Hold an exclusive SQLite lock across checkpoint, hash and before-image copy."""
    confirm_stopped()
    holders(path)
    with closing(
        sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=0, isolation_level=None)
    ) as db:
        db.execute('PRAGMA locking_mode=EXCLUSIVE')
        db.execute('BEGIN EXCLUSIVE')
        db.execute('COMMIT')
        mode = db.execute('PRAGMA journal_mode').fetchone()[0]
        result = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        expected = (0, 0, 0) if mode == 'wal' else (0, -1, -1)
        wal = Path(str(path) + '-wal')
        if result != expected or (wal.exists() and wal.stat().st_size):
            raise RuntimeError(f'Uncheckpointed WAL for {path.name}: {result}')
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise RuntimeError(f'SQLite quick_check failed for {path.name}')
        confirm_stopped()
        holders(path)
        yield db


def merge_fields(stored, desired):
    """Overlay defined leaves, retaining extra object keys and certificate entries."""
    if isinstance(desired, dict) and isinstance(stored, dict):
        merged = dict(stored)
        for key, val in desired.items():
            if key != 'clients':
                merged[key] = merge_fields(stored.get(key), val)
        return merged
    if isinstance(desired, list) and desired and all(isinstance(v, dict) for v in desired):
        if not isinstance(stored, list):
            return desired
        return [
            merge_fields(stored[i] if i < len(stored) else None, val) for i, val in enumerate(desired)
        ] + stored[len(desired) :]
    return desired


def read_roles(db, state):
    db.row_factory = sqlite3.Row
    rows = {}
    for role, iid in state['inbound_roles'].items():
        row = db.execute('SELECT * FROM inbounds WHERE id=?', (iid,)).fetchone()
        if row is None or row['node_id'] not in (None, 0):
            raise ValueError(f'Managed local inbound {role} ({iid}) is missing')
        rows[role] = dict(row)
        for key in ('settings', 'stream_settings', 'sniffing'):
            rows[role][key] = json.loads(row[key] or '{}')
        if role == 'amneziawg':
            continue
        hid = state['xhttp_cdn']['host']['id'] if role == 'xhttp_cdn' else state['host_roles'][role]
        if not db.execute('SELECT 1 FROM hosts WHERE id=? AND inbound_id=?', (hid, iid)).fetchone():
            raise ValueError(f'Managed Host {hid} no longer belongs to {role} ({iid})')
    return rows


def parameters(state, settings, rows):
    real = rows['reality']['stream_settings']['realitySettings']
    tls = rows['hysteria2']['stream_settings']['tlsSettings']['certificates'][0]
    p = dict(
        domain=state['domains']['panel'],
        reality_domain=state['domains']['reality'],
        private_key=real['privateKey'],
        public_key=real['settings']['publicKey'],
        short_ids=real['shortIds'],
        certificate=tls['certificateFile'],
        key_file=tls['keyFile'],
        xhttp_route=rows['xhttp']['stream_settings']['xhttpSettings']['path'],
        panel_port=settings['webPort'],
        sub_port=settings['subPort'],
        sub_path=settings['subPath'].strip('/'),
        panel_path=settings['webBasePath'].strip('/'),
        json_path=settings['subJsonPath'].strip('/'),
        **state['diagnostics'],
    )
    p.update(xhttp_path=p['xhttp_route'].lstrip('/'), INSTALL_WS_GRPC='n', flag='🌐')
    if 'ws' in rows:
        ws, grpc = rows['ws'], rows['grpc']
        ws_path = ws['stream_settings']['wsSettings']['path']
        service = grpc['stream_settings']['grpcSettings']['serviceName']
        p.update(
            INSTALL_WS_GRPC='y',
            ws_port=ws['port'],
            ws_route=ws_path,
            ws_path=ws_path.removeprefix(f"/{ws['port']}/"),
            trojan_port=grpc['port'],
            service_name=service,
            trojan_path=service.split('|')[0].removeprefix(f"/{grpc['port']}/"),
        )
    return p


def definitions(assets, state, rows, p):
    wanted = {
        role: assets('inbounds').core_inbound(role, p)
        for role in assets('inbounds').core_roles(p['INSTALL_WS_GRPC'] == 'y')
    }
    if 'vision' in rows:
        row = rows['vision']
        tls = row['stream_settings']['tlsSettings']
        cert = tls['certificates'][0]
        port = int(row['settings']['fallbacks'][0]['dest'].rsplit(':', 1)[-1])
        wanted['vision'] = assets('vision').inbound(
            state['domains']['vision'], row['port'], port, cert['certificateFile'], cert['keyFile']
        )
    if 'xhttp_cdn' in rows:
        row = rows['xhttp_cdn']
        wanted['xhttp_cdn'] = assets('xhttp-cdn').inbound(
            row['port'], row['stream_settings']['xhttpSettings']['path'], p['flag'], row['settings']
        )
    if 'amneziawg' in rows:
        row = rows['amneziawg']
        wanted['amneziawg'] = assets('amneziawg').inbound_definition(
            p['domain'], row['port'], settings=row['settings']
        )
    return wanted


def changes(rows, wanted):
    edits = []
    for role, definition in wanted.items():
        if role not in rows:
            continue
        row = rows[role]
        for column, field in (
            ('listen', 'listen'),
            ('port', 'port'),
            ('protocol', 'protocol'),
            ('settings', 'settings'),
            ('stream_settings', 'streamSettings'),
            ('sniffing', 'sniffing'),
        ):
            old = row[column]
            new = merge_fields(old, definition[field])
            if role == 'xhttp_cdn' and column == 'settings':
                new.pop('fallbacks', None)
            if old != new:
                edits.append((row['id'], column, old, new))
    return edits


def apply(db, edits):
    for iid, column, old, new in edits:
        if column not in ('settings', 'stream_settings', 'sniffing', 'listen', 'port', 'protocol'):
            raise ValueError('Unknown managed inbound column')
        current = db.execute(f'SELECT {column} FROM inbounds WHERE id=?', (iid,)).fetchone()
        encoded = isinstance(new, dict)
        if not current or (json.loads(current[0] or '{}') if encoded else current[0]) != old:
            raise ValueError(f'Managed inbound {iid} changed in {column}')
        db.execute(f'UPDATE inbounds SET {column}=? WHERE id=?', (json.dumps(new) if encoded else new, iid))


def reconcile(db, assets, state, rows, params, wanted, edits):
    db.execute('BEGIN IMMEDIATE')
    apply(db, edits)
    for role in wanted.keys() - rows.keys():
        iid, hid = assets('inbounds').insert_core_role(
            db, role, params, enabled=False, user_id=rows['reality']['user_id']
        )
        state['inbound_roles'][role] = iid
        state['host_roles'][role] = hid
