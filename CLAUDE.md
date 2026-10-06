# 3x-ui Auto Nginx

Maintained deployment stack derived from [mozaroc/3x-ui-pro](https://github.com/mozaroc/3x-ui-pro), using the panel from [MHSanaei/3x-ui](https://github.com/MHSanaei/3x-ui). Development is independent; do not synchronize upstream automatically.

## Repository structure

- `x-ui-latest.sh`: fresh installer/rebuild; stops and removes the previous installation and panel database.
- `x-ui-patch.sh`: reads an existing database and regenerates managed nginx/web assets; no database changes. Back up first and validate generated configuration on a disposable host.
- `x-ui-adguard.sh`: optional AdGuard Home integration. Reapply its nginx snippet after installer/patch regeneration when needed.
- `assets/backup/x-ui-backup.sh`: Backup/Restore v3, same OS ID/version and architecture.
- `assets/clash/clash.yaml`: Clash/Mihomo subscription template.
- `assets/diagnostics/`: MTR backend, diagnostics page and vendored LibreSpeed files.
- `assets/fake-sites/`: cover pages.
- `tests/test_personal_xhttp.py`: complete current transport, nginx security, firewall and panel-version regression suite.
- `tests/test_personal_backup.py`: complete current Backup/Restore regression suite.
- `.github/workflows/stack-xhttp.yml` and `stack-backup.yml`: PR/push validation on `main`; manual dispatch available.
- `README.md` and `README_EN.md`: Russian/English user documentation.
- `CONTRIBUTING.md`: development, checks, protection and emergency recovery.

## Runtime sources and compatibility

Canonical asset base: `https://raw.githubusercontent.com/xPROMSx/3x-ui-auto-nginx/main`.
Installer and patch must use this same `GITHUB_RAW`; panel/CLI releases still come from MHSanaei/3x-ui. Keep upstream attribution and third-party release references.

Do not rename historical filesystem paths `/usr/local/lib/3x-ui-pro` or `/etc/sysctl.d/99-3x-ui-pro.conf` without a separate backup-compatible migration. Test filenames/classes and archived `personal-v*` releases are historical identifiers.

## Architecture

The installer creates REALITY/TCP, XHTTP stream-up over a Unix socket, Hysteria2/UDP, WebSocket and Trojan gRPC inbounds. nginx routes public TCP 443 by SNI to REALITY on 8443 or the panel TLS vhost on 7443. The REALITY target uses 9443. Hysteria2 uses UDP 443.

WS and Trojan gRPC routes must use known paths and fixed backend ports. Never restore variable `/<port>/...` proxying. Diagnostics use panel-session authentication and a hardened localhost backend. Active UFW policy is preserved; enabling inactive UFW requires successful SSH-port discovery.

## Validation and changes

Read actual code before making claims. Run both complete suites in the Ubuntu CI environment with nginx and SQLite installed. Never execute the destructive installer on the development machine. See CONTRIBUTING.md for exact commands.

Use feature branches and PRs to `main`. Preserve `personal`, historical branches, tags and releases. Their removal is outside routine maintenance. Add no new license covering inherited code.

## Companion project

[Telemt WEB Manager](https://github.com/xPROMSx/telemt-web-manager) complements the stack with Telemt WEB proxy installation and management. Keep its prominent, relevant README block; do not imply automatic installation or port-conflict-free coexistence.
