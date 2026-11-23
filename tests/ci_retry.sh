#!/bin/sh
# Shared by hosted steps, test scripts and image builds.
ci_retry() {
    ci_attempt=1
    while :; do
        if "$@"; then
            return 0
        else
            ci_status=$?
        fi
        printf '%s attempt %s/3 failed (exit %s)\n' "$1" "$ci_attempt" "$ci_status" >&2
        if [ "$ci_attempt" -eq 3 ]; then
            return "$ci_status"
        fi
        sleep "$((ci_attempt * 10))"
        ci_attempt=$((ci_attempt + 1))
    done
}

# $1 is the apt command (possibly a wrapper that supplies options and timeout).
ci_apt_install() {
    ci_apt_command=$1
    shift
    "$ci_apt_command" update &&
        "$ci_apt_command" install -y --no-install-recommends "$@"
}

ci_host_apt() {
    timeout 8m sudo apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 "$@"
}

# An Ubuntu image build step can share its selected mirror across downloads.
# Restore the base image's sources before exporting it for guest acceptance.
ci_image_apt() (
    ci_ubuntu_sources=${CI_IMAGE_APT_SOURCES:-/etc/apt/sources.list.d/ubuntu.sources}
    ci_primary_sources=
    trap 'if [ -n "$ci_primary_sources" ]; then
        cat "$ci_primary_sources" > "$ci_ubuntu_sources" && rm -f "$ci_primary_sources"
    fi' EXIT
    "$@"
)

ci_image_apt_attempt() {
    ci_budget=$1
    shift
    timeout -k 5s "$ci_budget" sh -ec '
        refresh=$1
        shift
        apt() {
            apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 "$@"
        }
        if [ "$refresh" = yes ]; then apt -o APT::Update::Error-Mode=any update; fi
        apt "$@"
    ' sh "$@"
}

ci_image_apt_fetch() {
    ci_refresh=$1
    shift
    if [ ! -f "$ci_ubuntu_sources" ]; then
        apt-get "$@"
        return
    fi

    ci_mirror=https://mirrors.edge.kernel.org/ubuntu/
    if [ -z "$ci_primary_sources" ]; then
        if ci_image_apt_attempt 2m "$ci_refresh" "$@"; then return 0; else ci_status=$?; fi
        printf 'Ubuntu APT %s from primary failed (exit %s); switching to %s\n' "$1" "$ci_status" "$ci_mirror" >&2
        ci_saved_sources=$(mktemp) || return
        cp "$ci_ubuntu_sources" "$ci_saved_sources" || { rm -f "$ci_saved_sources"; return 1; }
        ci_primary_sources=$ci_saved_sources
        sed -E "s@^URIs: https?://(archive|security)\.ubuntu\.com/ubuntu/?\$@URIs: $ci_mirror@" \
            "$ci_primary_sources" > "$ci_ubuntu_sources" || return
        ci_refresh=yes
    fi
    if ci_image_apt_attempt 3m "$ci_refresh" "$@"; then return 0; else ci_status=$?; fi
    printf 'Ubuntu APT %s from %s fallback failed (exit %s)\n' "$1" "$ci_mirror" "$ci_status" >&2
    return "$ci_status"
}

ci_image_apt_install() {
    ci_install_refresh=$1
    shift
    if [ -f "$ci_ubuntu_sources" ]; then
        # Time-limit the transfer before dpkg can make changes.
        ci_image_apt_fetch "$ci_install_refresh" install -y --no-install-recommends --download-only "$@" &&
            apt-get --no-download install -y --no-install-recommends "$@"
    elif [ "$ci_install_refresh" = yes ]; then
        ci_apt_install apt-get "$@"
    else
        apt-get install -y --no-install-recommends "$@"
    fi
}
