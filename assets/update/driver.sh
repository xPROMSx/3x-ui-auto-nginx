#!/usr/bin/env bash
# 3X-UI AUTO NGINX — Copyright (C) 2026 xPROMSx contributors
# SPDX-License-Identifier: GPL-3.0-only
# Additional attribution terms: see docs/NOTICE.md
# Executed only from the pinned, root-private source tree.
set -eo pipefail
src=$(cd -- "$(dirname -- "$0")/../.." && pwd)
action=$1
shift
source "$src/x-ui-latest.sh"
case "$action" in
    worker|recover|boot)
        if [[ $action == boot ]] && /usr/bin/python3 -I "$src/assets/update/managed.py" boot-ready; then exit 0; fi
        acquire_stack_lock || exit $?
        exec /usr/bin/python3 -I "$src/assets/update/managed.py" "$action" "$@"
        ;;
    backup)
        source "$src/assets/backup/x-ui-backup.sh"
        acquire_stack_lock || exit $?
        run_backup_command backup --json
        ;;
    release)
        unset PANEL_VERSION
        preflight_panel_release staged-update
        prepare_panel_binaries "$PANEL_ARCHIVE" "$1" >/dev/null
        ;;
    stream-block) managed_stream_block "$1" ;;
    conntrack) source "$src/assets/backup/x-ui-backup.sh"; tune_conntrack plan ;;
    baseline) verified_panel_release ;;
    arch) _arch ;;
    render)
        dest=$1
        declare -A p=()
        while IFS= read -r -d '' k && IFS= read -r -d '' v; do p[$k]=$v; done
        p[ipv6]=n
        if ipv6_available; then p[ipv6]=y; fi
        p[nginx_version]=$(nginx -v 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')
        render_acme_nginx "$dest/etc/nginx" p
        render_core_nginx "$dest/etc/nginx" p
        hooks="$dest/etc/letsencrypt/renewal-hooks/deploy"
        mkdir -p "$hooks" "$dest/etc/systemd/system" "$dest/var/www/subpage" "$dest/var/www/diagnostics"
        render_certificate_hook "${p[domain]}" "${p[reality_domain]}" > "$hooks/3x-ui-auto-nginx"
        if [[ -n ${p[vision]:-} ]]; then render_vision_hook "${p[vision]}" > "$hooks/3x-ui-auto-nginx-vision"; fi
        cp "$src/assets/clash/clash.yaml" "$dest/var/www/subpage/clash.yaml.tpl"
        render_clash_template "$dest/var/www/subpage/clash.yaml.tpl" "${p[domain]}" "${p[sub_path]}"
        cp "$src/assets/diagnostics/index.html" "$dest/var/www/diagnostics/index.html"
        render_diagnostics_page "$dest/var/www/diagnostics/index.html" "${p[diag_path]}" "${p[domain]}" "${p[ip]}"
        cp "$src"/assets/diagnostics/librespeed/*.js "$dest/var/www/diagnostics/"
        render_mtr_unit /usr/local/lib/3x-ui-pro/mtr-backend.py "${p[mtr_backend_port]}" > "$dest/etc/systemd/system/mtr-backend.service"
        if [[ ${p[adguard]} == 1 ]]; then
            source "$src/assets/adguard/managed.sh"
            agh_config
            mkdir -p "$dest/etc/nginx/snippets/x-ui-auto-optional"
            agh_snippet > "$dest$AGH_SNIPPET"
            if [[ -f /etc/nginx/stream-dot.d/adguard-dot.conf ]] &&
               grep -Fxq "$(managed_stream_block y)" /etc/nginx/nginx.conf; then
                mkdir -p "$dest/etc/nginx/stream-dot.d"
                render_adguard_dot "${p[domain]}" "$AGH_DNS_PORT" "${p[ipv6]}" > "$dest/etc/nginx/stream-dot.d/adguard-dot.conf"
            fi
        fi
        ;;
    unit) install_panel_unit "$1" "$2" ;;
    agh-health) source "$src/assets/adguard/managed.sh"; agh_health ;;
    *) exit 2 ;;
esac
