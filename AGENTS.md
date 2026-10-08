# Codex repository instructions

This file is the canonical repository guidance for Codex.

## Development model

- `main` is the protected production branch. Work through feature/fix/docs/chore branches and pull requests.
- Do not force-push or delete protected/history branches as part of routine work.
- The repository remains a fork of `mozaroc/3x-ui-pro`, but upstream is reference-only; do not auto-sync it.
- Preserve attribution to `mozaroc/3x-ui-pro` and `MHSanaei/3x-ui`.
- Do not add a new repository-wide license for inherited code without a separate maintainer decision.

## Critical compatibility

- Keep canonical runtime assets under `https://raw.githubusercontent.com/xPROMSx/3x-ui-auto-nginx/main`.
- Preserve historical runtime paths `/usr/local/lib/3x-ui-pro` and `/etc/sysctl.d/99-3x-ui-pro.conf` unless a dedicated Backup/Restore-compatible migration is designed.
- `x-ui-latest.sh` is the main installer for fresh install / full rebuild only. It is destructive; never run it on the development machine.
- `assets/adguard/managed.sh` is the active managed AdGuard Home component used by the integrated installer and Backup/Restore.
- Never restore generic request-controlled localhost-port proxying. WS and Trojan gRPC routes must use managed fixed backends; XHTTP uses its Unix socket.

## Required validation

Before proposing a merge, run the current repository checks described in `CONTRIBUTING.md`, including:

- Bash syntax and ShellCheck error-only for all tracked shell scripts.
- Canonical completeness and all suites through `tests/run_ci.py`, preserving their root/non-root privilege contexts.
- nginx syntax/runtime security checks where available.
- `git diff --check`.

Required GitHub check contexts on `main` are:

- `Stack XHTTP and security`
- `Stack Backup and restore`
- `Real services (ubuntu-24.04)`
- `Real services (ubuntu-26.04)`

Do not rename those job contexts casually because branch protection depends on them.

## Documentation

Keep `README.md` and `README_EN.md` semantically aligned. README should stay user-first: simple installation and product value first, technical internals below.

`CLAUDE.md` is retained for compatibility with other tooling, but Codex should treat this `AGENTS.md` plus `CONTRIBUTING.md` as the primary working instructions.
