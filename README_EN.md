<div align="center">

[🇷🇺 Русский](README.md) · 🇬🇧 **English** · [🇪🇬 العربية](README_AR.md) · [🇮🇷 فارسی](README_FA.md) · [🇨🇳 简体中文](README_ZH_CN.md) · [🇪🇸 Español](README_ES.md) · [🇹🇷 Türkçe](README_TR.md)

<h1 align="center"><img src="assets/branding/logo.png" alt="3X-UI AUTO NGINX" width="560"></h1>

### Automated deployment of 3X-UI / XRAY-CORE on your own VPS

**REALITY · XHTTP · HYSTERIA2 · CASCADE · ADGUARD HOME · UPDATE · BACKUP / RESTORE**

Two domains, a clean VPS, and a few minutes to install.

[![Releases](https://img.shields.io/github/v/release/xPROMSx/3x-ui-auto-nginx)](https://github.com/xPROMSx/3x-ui-auto-nginx/releases)
[![Downloads](https://img.shields.io/github/downloads/xPROMSx/3x-ui-auto-nginx/total?label=Downloads)](https://github.com/xPROMSx/3x-ui-auto-nginx/releases)
[![Ubuntu](https://img.shields.io/badge/Ubuntu-24.04%20%7C%2026.04-E95420?logo=ubuntu&logoColor=white)](#technical-details)
[![Debian](https://img.shields.io/badge/Debian-13-A81D33?logo=debian&logoColor=white)](#technical-details)
[![Support](https://img.shields.io/badge/Support-Donate-EA4AAA)](docs/SUPPORT.en.md)

[📦 Releases](https://github.com/xPROMSx/3x-ui-auto-nginx/releases) · [🔗 Cascade](#cascade) · [🌐 Cover websites](#cover-sites) · [✈️ Telegram Web Proxy Manager](#telemt-web-manager) · [❤️ Support](#support-development) · [💬 Feedback](https://t.me/promsdev) · [🔄 Update](#installation-vs-update)

[📚 Documentation website (in Russian)](https://xpromsx.github.io/3x-ui-auto-nginx/)

</div>

**3X-UI AUTO NGINX** is a free installer for [3x-ui](https://github.com/MHSanaei/3x-ui) and Xray on your own VPS. Enter two domains and run a single command. The installer configures Nginx, HTTPS certificates, REALITY, XHTTP, Hysteria2, a subscription link, and a cover website. The menu lets you change domains, manage backups, set up a two-VPS Cascade and run your own DNS server. Compatible installations can now be updated in place, without reinstalling the server.

**More than a basic placeholder:** fresh installations get multi-page cover websites with navigation, articles and images. At least 2,592 variations are available. Unauthenticated Hysteria2 HTTP/3 requests receive the same website. Everything runs on Nginx and Xray, without Docker or extra web servers.

## ✨ Why this project?

| | |
| --- | --- |
| **Ready in minutes**<br>Two domains, a clean VPS and one command. You receive the panel URL and subscription link with REALITY, XHTTP and Hysteria2 configured. | **Looks like a regular website**<br>Multi-page cover sites with navigation, articles and illustrations. At least 2,592 variations, including Hysteria2 HTTP/3 cover. |
| **Two-server Cascade**<br>Connect two VPS servers so devices use the entry server and access the internet through the exit server, for example in another country. Configure it through the menu. | **Private DNS with ad blocking**<br>AdGuard Home blocks ads and tracking. Connect devices using DNS-over-HTTPS (DoH) or optional DNS-over-TLS (DoT). |
| **Updates and backups**<br>Update compatible v3.0.0+ installations without reinstalling. Backup v3 restores the full managed stack; clients and subscriptions can also be transferred to a fresh installation. | **Security without extra setup**<br>The panel and subscriptions use HTTPS, internal service ports stay local, and certificates renew automatically. Optional SSH protection and VPS security updates are available. |

**Only the essentials by default.** The three primary connections are installed automatically. WebSocket, Trojan gRPC, XTLS Vision, AmneziaWG and CDN-ready XHTTP are installed only if you select them. No Docker or additional web server is needed.

<a id="installation"></a>

## 🚀 Quick start

Use a clean VPS with Ubuntu 24.04, Ubuntu 26.04 or Debian 13, and connect by SSH as **root**. Point two domains to the VPS IP address, one for the panel and one for REALITY. Allow inbound TCP **80** and **443** and UDP **443**. If you enable DoT, you also need TCP **853**. [Technical details](#technical-details).

Already running this project? Read [Updating the project](#installation-vs-update) before selecting Installation.

```bash
curl -fSL https://github.com/xPROMSx/3x-ui-auto-nginx/releases/latest/download/x-ui-latest.sh -o x-ui-latest.sh && bash x-ui-latest.sh
```

After launching, choose **1 — English** or **2 — Русский**. Then the main menu opens:

<p align="center">
  <img src="docs-site/assets/readme/menu-en.png" alt="3X-UI AUTO NGINX English main menu" width="512">
</p>

- **1 — Installation:** set up a new server or perform a full reinstall.
- **2 — Change Domains:** change the panel domain, REALITY domain, or both without reinstalling.
- **3 — Backups:** Create, list and restore backups, or restore clients and subscriptions only.
- **4 — Telegram Web Proxy Manager:** launch or install the separate Telegram proxy manager.
- **5 — Cascade:** connect two servers, check status, or disconnect.
- **6 - My server details:** display panel, subscription and AdGuard Home addresses and usernames. Does not change settings or reveal passwords.
- **7 — Update:** Update a compatible installation in place, preserving clients and settings.
- **0 — Exit.**

The menu requires an interactive SSH terminal.

<details>
<summary><strong>Fallback download — if GitHub Releases is unavailable</strong></summary>

**The `main` branch contains an alpha version of the project.** Installing it is not recommended. Use the stable release from the main command above.

```bash
curl -fSL https://raw.githubusercontent.com/xPROMSx/3x-ui-auto-nginx/main/x-ui-latest.sh -o x-ui-latest.sh && bash x-ui-latest.sh
```

</details>

<a id="installation-vs-update"></a>

### Updating the project

**Compatible installations from v3.0.0 onwards:** download the current stable manager using the command above, select **7 — Update** and confirm. The manager checks compatibility, creates and verifies a Backup v3 before making changes, updates the managed configuration and, if needed, 3x-ui and Xray to the verified versions. Clients, subscriptions, keys, panel settings, Cascade and routing rules are preserved.

`SUCCESS` means the update completed; `UP TO DATE` means no update was needed. Afterwards, check the panel and a client connection, and keep the new backup off the VPS if one was created. Connections may briefly be interrupted, managed cover sites may be regenerated, and manual changes to managed Nginx or transport settings may be replaced. Refresh client profiles if needed.

Update requires a compatible installation (`update_contract=1`) and does not add installation options you did not select. If 3x-ui or Xray is newer than the version verified for the project release, Update refuses to downgrade it. On failure, check the error and rollback result. With `ROLLBACK UNVERIFIED`, stop and preserve the backup and recovery files; do not reinstall.

**Upgrading from 2.x to v3.0.0 or later, or deliberately starting with a clean configuration:**

1. Create a Backup v3 and download it off the VPS. Save custom outbounds, routing rules and other settings separately.
2. Install the new release on the same or a new VPS. Use the previous panel domain to retain subscription URLs, and skip automatic client creation.
3. Select **3 — Backups → 4 — Restore clients and subscriptions** and choose your archive.
4. Reapply your custom settings, refresh subscriptions and test connections.

A full Restore returns the archived configuration; it does not upgrade the project.

> [!CAUTION]
> **Installation is not an update!**
>
> Running the script again **without arguments** opens the manager; it does not reinstall the server. However, **Installation** is intended for a clean VPS or a full reinstall.
>
> If an existing installation is detected, the script warns that managed data will be removed and asks you to type **YES** exactly. Reinstalling replaces the 3X-UI database and nginx configuration. In addition:
> - if XTLS Vision was previously installed and you omit it or choose another domain, the installer removes the old Vision domain certificate it created. It warns you about this before asking for YES;
> - if AmneziaWG was previously installed and you do not select it again, port 8443/udp is closed (there is no separate warning).
>
> **Save a Backup outside the VPS beforehand.**
>
> **Change Domains** and **Backups** are separate operations that do not reinstall the panel.

Use the project's Update for verified 3x-ui and Xray releases. Updating them independently in the panel may block Update until a compatible project release is available. Update nginx, the Linux kernel and other system packages separately using OS tools. To access new manager-only features, download the current manager.

There are seven main installation questions, some with follow-up questions. The defaults are suitable for most users.

| # | Question | Default | Purpose |
| --- | --- | :---: | --- |
| 1 | Panel domain | - | Panel, subscriptions, cover website |
| 2 | REALITY domain | - | REALITY connection and cover website |
| 3 | Enhanced VPS security | `Y` | Fail2ban, encrypted DNS for the VPS itself, security updates |
| 4 | Additional connections (chosen separately) | `N` | WebSocket, Trojan gRPC, XTLS Vision, AmneziaWG, CDN-ready XHTTP |
| 5 | Russian websites via Cloudflare WARP | `N` (English), `Y` (Russian) | Hide the VPS IP from Russian sites |
| 6 | AdGuard Home | `N` | Personal DNS with DoH and optional DoT |
| 7 | Client and subscription | `Y` | Subscription link at the end of installation |

The details are below. Questions 4.1 through 4.4 are asked only if you enable additional connections.

---

### 1. 🌐 3X-UI panel domain

> `Enter the domain for your 3X-UI panel:`

Enter the domain used for the administration panel, subscriptions, and additional web services.

---

### 2. 🛰️ VLESS REALITY domain

> `Enter the domain for VLESS REALITY:`

Enter a second domain for REALITY. Both domains must point to your VPS IP address.

<details>
<summary>ℹ️ Additional startup parameters</summary>

You can supply both domains on the command line instead of entering them interactively:

```bash
bash x-ui-latest.sh -subdomain panel.example.com -reality_domain reality.example.com
```

Replace `panel.example.com` and `reality.example.com` with your own domains. Optional features are still selected interactively.

</details>

---

### 3. 🔐 Enhanced VPS security

> `Enhance VPS security? [Y/n]:`

*Recommended for less experienced users · Enabled by default (`Y`)*

The installer configures Fail2ban for SSH protection, encrypted system DNS (DoT), and automatic security updates. When conditions permit, it also creates a 1 GiB swap file, regardless of whether enhanced security is selected.

**Read the settings below before enabling this option**, particularly the automatic reboot schedule.

<details>
<summary>🔐 Detailed security settings</summary>

Choosing `Y` enables the additional security settings. Swap setup runs independently of this choice:

- **1 GiB swap.** If no swap is active, the filesystem is supported and at least 4 GiB is free, the installer creates and enables a swap file. Existing swap settings remain unchanged. If these conditions are not met, this step is skipped without stopping installation.
- **SSH protection — Fail2ban.** Limits repeated unsuccessful logins using the actual SSH ports. Existing SSH authentication settings are not changed. A working, compatible existing SSH jail is preserved.
- **Encrypted DNS for the VPS itself (DoT).** This protects the server's own DNS queries and is separate from AdGuard Home for your devices. Global Cloudflare and Google resolvers use strict DNS-over-TLS through `systemd-resolved`. Interface/VPN DNS routes and search domains are preserved; their queries may remain unencrypted. If DoT preparation fails and the original DNS is successfully restored, core, Fail2ban and automatic update installation continues independently of DoT; the final result reports the failure and exits nonzero.
- **Automatic security updates.** Package lists are updated at 02:30, with security updates installed at 02:50.
- **Automatic reboot — 04:38.** If updates require a reboot, it occurs at the scheduled time, **even when users are connected**. This may interrupt active connections.

Times are in the VPS's local time zone.

Existing settings are checked for compatibility before changes. If security configuration fails, a controlled rollback is provided.

**Important:** This does not disable SSH password login or replace proper server administration. Configure SSH key access first, then disable password authentication yourself.

</details>

---

### 4. 🧩 Additional connections

> `Set up additional connections? [y/N]:`

*Disabled by default (`N`)*

REALITY, XHTTP and Hysteria2 are set up by default. You can also choose VLESS WebSocket, Trojan gRPC, VLESS TCP/TLS (XTLS Vision), AmneziaWG and CDN-ready XHTTP.

> [!WARNING]
> These connections may not work in every country or on every network. Use them at your own risk. If unsure, answer `N`.

If you answer `N`, none of these extra connections is created and questions 4.1-4.4 are skipped. Answer `y` to choose them individually.

#### 4.1. WebSocket and Trojan gRPC

> `Set up legacy VLESS WebSocket and Trojan gRPC connections? [y/N]:`

Both are created enabled if you answer `y`; neither is created if you answer `N`.

---

<a id="4--vless-xtls-vision"></a>

#### 4.2. 🔗 VLESS XTLS Vision

> `Set up an additional VLESS TCP/TLS (XTLS Vision) connection? (requires a separate domain) [y/N]:`

*Additional connection · Disabled by default (`N`)*

An alternative VLESS connection over TLS that may help when REALITY is unstable or blocked on your network. It is created as a separate profile in 3X-UI.

> [!IMPORTANT]
> **XTLS Vision requires a third domain**, distinct from the panel and REALITY domains. Its DNS record must point to your VPS IP. HTTPS certificates and nginx routing are configured automatically.
>
> After you choose `Y`, the installer asks for this domain: `Enter the domain for VLESS TLS Vision (must differ from the panel and REALITY domains):`.

---

<a id="5--amnezia-wg"></a>

#### 4.3. 🔑 Amnezia WG

> `Set up an additional AmneziaWG connection? [y/N]:`

*Additional connection · Disabled by default (`N`)*

A separate AmneziaWG connection is created in the 3X-UI panel. No additional domain is required.

<details>
<summary>ℹ️ About Amnezia WG</summary>

The installer configures AmneziaWG 3.1 on **UDP/8443** and automatically allows the required UFW rule.

After installation, create a client in the 3X-UI panel to obtain a configuration or a `vpn://` link for a compatible Amnezia app.

The installer creates the server connection but does not automatically generate AmneziaWG clients.

The configuration and necessary firewall rule are included in **Backup / Restore v3**.

</details>

---

<a id="xhttp-cdn"></a>

#### 4.4. 🌐 VLESS + XHTTP for CDN

> `Add an additional CDN-compatible XHTTP connection? [y/N]:`

VLESS ML-KEM-768 native encryption between the client and Xray is enabled by default and supports Vision. The client core must support VLESS Encryption; it can be disabled in the panel.

*Additional connection · Not installed by default (`N`)*

Creates a separate VLESS connection using XHTTP in packet-up mode, preconfigured for use through a CDN. The connection is created disabled in 3X-UI.

It uses the panel’s existing HTTPS domain, so no additional domain or certificate is needed. Once enabled, it also works directly without a CDN.

To use a CDN, configure your provider yourself and adjust the Host settings in 3X-UI. The installer does not configure CDN services; it provides a server profile you can adapt to your setup.

---

<a id="warp-ru"></a>

### 5. 🌐 Russian websites via Cloudflare WARP

> `Route Russian websites through Cloudflare WARP to hide your VPS IP? [y/N]:`

*Optional routing · Enabled by default in the Russian menu (`Y`), disabled in the English menu (`N`)*

If this VPS will be a Cascade entry server, choose `N`. WARP is incompatible with the entry role, but it can be used on the exit server.

Selecting `Y` configures Cloudflare WARP (MASQUE) so Russian websites and certain network services see a Cloudflare IP address instead of your VPS IP address. This is needed to protect your VPS IP address.

The setting only affects traffic routed through your VPS. If an app accesses a website directly, WARP on the server has no effect.

When enabled, the installer registers a device with Cloudflare and accepts its terms of service.

If WARP setup fails, installation continues without it, and the final report indicates a partial result. If the rollback cannot be verified, check Xray in the panel.

<details>
<summary>ℹ️ <strong>Which sites use WARP and how to check the setup</strong></summary>

The installer adds an outbound named `warp-masque` and three routing rules to the Xray configuration:

- **WARP - RU:** Russian domains from `geosite:category-ru`.
- **WARP - RU2:** IP addresses from `geoip:ru`.
- **WARP - RU3:** `ipify.org`, `ifconfig.me`, `checkip.amazonaws.com`, `ipapi.is`, `iplocate.io`, `ip.sb`, and `2gis.com`.

The rules are appended to the routing list. Earlier rules keep their priority. This is not a separate client connection profile or a system-wide VPN: routing happens inside Xray.

After installation, check **Xray → Outbounds** for `warp-masque` and **Xray → Routing** for the three named rules in the 3X-UI panel. The settings are included in Backup v3 along with the 3X-UI configuration.

</details>

---

<a id="7--adguard-home--dns-over-https"></a>
<a id="6--adguard-home--dns-over-https"></a>

### 6. 🛡️ AdGuard Home + DNS-over-HTTPS and DNS-over-TLS

> `Install and configure AdGuard Home? [y/N]:`

*Optional DNS server · Disabled by default (`N`)*

Your own DNS server for blocking ads and trackers, managing clients and encrypting DNS queries. Devices can use DoH or optional DoT (for example, Android's Private DNS setting).

<details>
<summary>ℹ️ <strong>About AdGuard Home and access settings</strong></summary>

No additional domain is required.

After installation you can access:

- **AdGuard Home panel:** `https://panel.example.com/adg-.../`, with its own random path.
- **DNS-over-HTTPS:** `https://panel.example.com/dns-XXXXXXXXXXXX`. The `dns-XXXXXXXXXXXX` path is random for each installation.
- **DNS-over-TLS** (if selected): `panel.example.com`, port 853.

These addresses are shown after installation and later under **6 - My server details**. The AdGuard Home password cannot be retrieved there; save it during installation.

Public **53/tcp and 53/udp** are not opened. AdGuard Home needs only one extra public port, **853/tcp**, and only if you enable DoT. Its configuration and data are included in **Backup / Restore v3**. Servers installed before 2.7.0 retain `https://panel.example.com/dns-query`.

**Using DoH and optional ClientID**

AdGuard Home is separate from the VPS's system DNS. Installing it does not automatically redirect the VPS, Xray or client applications to it. Configure DNS in the intended device or application and check the AdGuard Home query log.

The generated random DoH path is sufficient for personal use when no allowed-clients list is configured. Keep the URL private: a random path is harder to guess, but it is not authentication.

`https://panel.example.com/dns-XXXXXXXXXXXX`

A [ClientID](https://adguard-dns.io/kb/adguard-home/clients/#clientid) is optional. It helps identify devices, assign individual rules and restrict access, especially on older installations using the predictable `/dns-query` path. Create persistent clients with long random identifiers, then add them in **Settings → DNS settings → Access settings → Allowed clients** if you want to restrict access. Keep any other required entries.

`https://panel.example.com/dns-XXXXXXXXXXXX/<ClientID>`

When the list is enabled, queries without a matching identifier or another allowed entry are rejected. See the [official AdGuard Home security guide](https://adguard-dns.io/kb/adguard-home/running-securely/).

**DoT and allowed clients:** DoT does not transmit a ClientID. If you use DoT and enable the list, add `127.0.0.1`; otherwise DoT stops responding. This allows anyone who knows the server address to use DoT.

**Happ example (RAW subscription):** in the active routing profile, set Remote DNS to DoH, enter the full URL in Remote DNS Domain and the VPS's public IP in Remote DNS IP. Save, reconnect and check the query log. With JSON subscriptions, Xray DNS may be defined by the JSON itself; Tunnel DNS is a separate setting.

AdGuard Home can update through its web interface on installations from v2.4.1 onwards. For older installations, see the [legacy enablement instructions (Russian)](docs/ADGUARD_UPDATES.md).

</details>

#### 6.1. DNS-over-TLS (DoT)

> `Enable DNS-over-TLS (DoT) for your devices? [y/N]:`

DoT is mainly useful for Android's **Private DNS** feature, which accepts a hostname rather than a DoH URL. Leave it disabled (`N`) if you do not need it.

- Available only during a fresh AdGuard Home installation.
- DoT listens on `panel.example.com`, port **853**. The installer adds the UFW rule and uses the panel domain certificate.
- **Port 853 is open to the entire internet.** DoT does not send a ClientID, so anyone who knows the server address can use it. Concurrent connections are limited to about 50 per source IP to reduce overload.
- AdGuard Home logs DoT queries as coming from `127.0.0.1`, because Nginx forwards them locally. Individual DoT devices are not distinguishable in its log.
- If an allowed-clients list is configured, add `127.0.0.1` to keep DoT working; see the ClientID instructions above.
- DoT settings are saved in Backup / Restore v3 and handled when changing domains.

**Android setup.** Find **Private DNS** in Android settings, choose the hostname option and enter the panel domain.

**Disable DoT.** Run the following command on the server as root:

```bash
ufw delete allow 853/tcp
```

This closes port 853 without affecting other services. Completely removing DoT requires reinstalling the server.

---

<a id="7--automatic-client-and-subscription"></a>

<a id="8--automatic-client-and-subscription"></a>

### 7. 👤 Automatic client and subscription

> `Create a client in 3X-UI and display a subscription link after installation? [Y/n]:`

*Recommended · Enabled by default (`Y`)*

The installer creates a 3X-UI client and generates a personal HTTPS subscription URL for the configured, supported connections.

The link appears in the final installation report. If you decline, you can create clients manually through the panel.

---

The installation report displays the **panel URL, username and password, diagnostics address, and subscription link** (if you chose to create a client). For AdGuard Home, it also includes the admin address, password, and DoH endpoint. The report also shows the WARP setup result.

If an OS update requires a reboot, the installer will notify you. **The initial reboot is manual**: run `sudo reboot`.

<a id="cover-sites"></a>

## 🌐 Cover websites and HTTP/3

**Complete websites instead of basic placeholders.** Fresh installations automatically create separate multi-page websites for the panel and REALITY. The project offers at least 2,592 ready-made design and text combinations, with working navigation, illustrations, and mobile-friendly layouts. Optional XTLS Vision gets its own website.

**Every installation looks different.** Site names, colors, images, icons, and text are personalized automatically. Additional pages have different articles and publication dates. The chosen site is preserved across Backup/Restore and domain changes. During Update, managed cover sites may be regenerated.

**Hysteria2 with HTTP/3 cover.** Ordinary HTTPS requests over TCP and unauthenticated HTTP/3 requests over UDP on the REALITY domain receive the same complete website. The panel and subscriptions remain isolated from this public HTTP/3 cover.

**Protection against excessive HTTP requests.** Nginx limits public cover websites to 20 requests per second per IP per site and 200 per second per site overall, with bursts allowed. Excess requests receive HTTP 429 with a matching error page. These limits do not apply to proxy connections, the panel, subscriptions, AdGuard Home or Telegram WEB Proxy. This is not a replacement for DDoS protection.

## 🚀 Connection profiles

| Connection | Created when |
| --- | :---: |
| VLESS + REALITY | ✅ Always, enabled |
| VLESS + XHTTP | ✅ Always, enabled |
| Hysteria2 | ✅ Always, enabled |
| VLESS + WebSocket (TLS) | 🔧 Optional, enabled when selected |
| Trojan + gRPC | 🔧 Optional, enabled when selected |
| VLESS + XTLS Vision | 🔧 Optional, requires a third domain |
| Amnezia WG | 🔧 Optional |
| VLESS + XHTTP for CDN | 🔧 Optional, created disabled |

REALITY, XHTTP and Hysteria2 work immediately after installation. Other connections are created only if you answer `y` to the additional-connections question (step 4). Unselected connections are not created.

CDN-ready XHTTP is disabled until you enable it and attach a client, so it does not appear in subscriptions. On servers installed before 2.7.0, WebSocket and Trojan gRPC remain created but disabled as before.

Create clients during installation or later in the panel. AmneziaWG requires a separate client. Compatibility depends on your client application and version; CDN-ready XHTTP also depends on your CDN provider.

### Subscriptions

Standard subscriptions, JSON, and **Mihomo / Clash** are served via nginx and HTTPS. `provider=1` returns the original subscription for proxy providers rather than a complete Clash configuration.


<a id="change-domains"></a>

## 🌐 Change domains

In the main menu, select **2 — Change Domains**. You can change the panel domain, REALITY domain, or both without reinstalling. New DNS records must point to your VPS, and TCP/80 must be reachable for certificate issuance. The domains must differ from each other and from the Vision domain.

A full Backup v3 is created automatically before any changes. Clients, keys, settings and traffic statistics are preserved. The Vision domain remains unchanged. After a successful change, you may need to update subscription URLs and client configurations.

Only recognized project installations are supported, including compatible v2.1.7 installations. If an error occurs, **do not delete backups or recovery data**. If you see `ROLLBACK_UNVERIFIED`, stop and preserve the diagnostics. [Details, limitations and recovery](docs/MANAGER.md#change-domains).

## 💾 Backup / Restore

Open **3 — Backups** in the main menu to create a backup, list archives or run Restore. You can use a backup after reinstalling the OS or to move to another VPS. To restore it, the operating system, its version and the CPU architecture must match the backup.

A full Backup v3 restores the archived managed stack; **Restore clients and subscriptions** transfers only user data into a fresh installation. Use [Update](#installation-vs-update) for compatible installations. The current manager supports Backup v3; older v2 archives require the corresponding older utility. TWPM data is managed separately, and full Restore is blocked on a target VPS with TWPM installed.

On a fresh VPS, Restore from the manager waits for automatic system updates to finish (up to 60 minutes). Do not restart the server during that time.

For command-line use, run the following as root:

**Before restoring:** the restore command replaces managed data. Unlike the menu, it does not enable UFW or configure additional Security settings. Replace `ARCHIVE.tar.gz` with your trusted backup's filename.

```bash
# Create a new backup
x-ui-backup backup

# List available backups
x-ui-backup list

# Restore a selected backup
x-ui-backup restore /var/backups/x-ui/ARCHIVE.tar.gz
```

Archives are saved in `/var/backups/x-ui/`.

> **Backups contain sensitive data:** the client database, passwords, and certificate private keys. Keep a copy **off the VPS** and restore only trusted archives.

If Restore is unavailable, extract the files for manual recovery without installing packages or running services. Only Bash, Python 3, and access to the archive and destination are needed; root is not required:

```bash
x-ui-backup extract /path/to/archive.tar.gz ./recovered-backup
```

The destination must be new; original paths are preserved under `files/` and links are skipped. The data contains secrets: review it before copying anything over a running server.

[Recovery on a new VPS](#restore-on-new-vps).

<a id="restore-clients"></a>

### Restore clients and subscriptions

After installation on a new or reinstalled VPS, open **3 — Backups → 4 — Restore clients and subscriptions**. Select a regular Backup v3 archive by number or enter its absolute path. Enter the new panel credentials and the 2FA code if enabled. This imports user data into the running installation without replacing the full stack.

The archive supplies client identifiers and credentials, status, limits, expiry, supported counters, and shared RAW and JSON paths. Clients receive all eligible active local connections. The running installation supplies the domain, certificates, connection settings, and the rest of the infrastructure. The panel allocates AmneziaWG addresses from its current subnet. Custom inbounds and individual client assignments, outbounds, routing rules, balancers and other server settings are not imported; recreate the settings you need.

Previous subscription URLs remain valid after successful import if you retain the panel domain and relevant subscription types. If the IP changes, point the domain to the new VPS through DNS. URLs previously issued by the new installation with different paths may stop working. Refresh subscriptions to load the new connection settings; individual connection links may change.

If an email or subscription identifier matches, the existing record is preserved and the archived client is skipped. An empty identifier or one shared by multiple archive records also causes a skip. `PARTIAL` means the paths were applied and some clients were skipped, some records were incomplete, or verification could not confirm operation. Check the clients, limits, expiry, and connections in 3x-ui.

<a id="cascade"></a>

## 🔗 Cascade between two servers

Cascade connects two VPS servers running 3X-UI AUTO NGINX. Your devices connect to the entry server, while the exit server provides the internet connection. This may help if connecting to a particular VPS is unreliable or you need to access the internet through a server in another country.

**Device → Entry VPS → Exit VPS → Internet**

Russian sites and Russian IP addresses are reached directly from the entry server. You can turn off this behavior after setup in the entry VPS's routing rules. Other traffic not matched by earlier routing rules goes through the exit server.

Open **5 — Cascade** in the main menu to set it up. You do not need to edit configuration files manually.

<details>
<summary><strong>Set up Cascade, check its status, or disconnect</strong></summary>

### What you need

- Two VPS servers with working 3X-UI AUTO NGINX installations.
- The 3X-UI panel login and password for each server.
- The latest installer, with **5 — Cascade** in the menu.
- No WARP configuration on the entry server (you can remove it manually).

### 1. Create a key on the exit server

On the VPS that you want to use for internet access, run the current installer.

Select **5 — Cascade → 1 — Create key (exit server)**.

Enter the 3X-UI panel login and password. Your input is hidden and neither credential is saved. The manager adds a special client named `cascade` to the existing REALITY connection and displays a key link starting with `vless://`.

Copy this link. You will need it on the second server.

**Keep the key link private:** anyone who has it can connect using this client.

Existing clients are not changed.

### 2. Connect the entry server

Now run the current installer on the VPS your devices connect to.

Select **5 — Cascade → 2 — Connect exit server (entry server)**.

Enter the 3X-UI panel login and password (again, neither is saved), then paste the key link from step 1. The link is visible in the terminal when pasted, but it is not saved.

The manager tests the connection to the exit server first. If the test succeeds, it adds an outbound connection called `cascade-exit` and three `CASCADE` routing rules.

Russian sites and IP addresses are routed directly from the entry server unless you later disable those rules. Other matching traffic uses the exit server.

**Applying the changes may briefly interrupt every user's connection.** If a check fails, the manager attempts to restore the previous configuration and reports the result.

Your existing client connections and subscriptions remain in use. No separate client profile is needed for Cascade.

### 3. Check the status

On each VPS, open **5 — Cascade → 3 — Status**.

The manager asks for the panel login and password, just as it does for other actions.

It shows whether the entry and exit roles are configured on that server and whether Xray is running. This check changes no settings.

### 4. Disconnect Cascade

On the relevant VPS, open **5 — Cascade → 4 — Disable**.

**On the entry server**, the manager offers to remove `cascade-exit` and the three `CASCADE` rules. Other connections and panel rules remain in place.

**On the exit server**, only the `cascade` client is removed. Entry servers using its key will then lose access to that exit server.

Each removal requires separate confirmation. Applying changes on the entry server may briefly interrupt user connections.

### What to know

- **WARP:** it cannot be configured on the entry server. If present, remove its outbound and routing rules in the 3X-UI panel before connecting. WARP is allowed on the exit server.
- **AmneziaWG:** these connections do not pass through the exit server.
- **Routing rules:** earlier user-defined rules may direct some traffic elsewhere. The manager warns you before connecting.
- **Exit unavailable:** traffic routed through the exit server stops working until it becomes reachable again, unless you have configured a balancer with a backup exit.
- **QUIC over Vision:** UDP/443 does not pass through this Cascade.
- **Domain changes:** after changing domains on the exit VPS, reconnect it from the entry server.

### Backups

Backup v3 saves Cascade settings. Restoring a backup on the entry server retains the Cascade configuration alongside the other 3X-UI data. Update in place also preserves Cascade; after a fresh installation with client-only import, configure Cascade again.

Cascade has been tested on two real VPS servers using REALITY, XHTTP, and Hysteria2. Compatibility with every client app and configuration has not been confirmed.

If you find a bug, [report it on GitHub](https://github.com/xPROMSx/3x-ui-auto-nginx/issues).

</details>

## 🔐 Security

Service interfaces are not exposed directly to the internet: the panel, subscriptions and additional services use nginx and HTTPS, with internal ports restricted to localhost. The only extra public service port is 853 if you enable DNS-over-TLS (DoT). Client connection ports are public by design: TCP and UDP 443, plus UDP 8443 if you select AmneziaWG.


**Not sure how to secure your VPS?** Enable enhanced security during installation to configure protection against SSH brute-force attempts, encrypted DNS, and automatic security updates. Read the detailed settings and the scheduled nighttime reboot policy in **Quick start** first.

**Important:** Secure access to the server itself. Use SSH keys, verify key-based login works, and only then disable password authentication. The installer does not disable it automatically.

<a id="telemt-web-manager"></a>

## ✈️ Telegram Web Proxy Manager

[Telegram Web Proxy Manager](https://github.com/xPROMSx/telegram-web-proxy-manager) is an independent companion project for running your own Telegram WEB Proxy, with HTTPS, a cover website, and updates with rollback.

**TWPM is not part of the default 3X-UI installation.** Main menu option 4 lets you launch it or start its separate installation after confirmation. Both projects are managed independently.

Telegram Web Proxy Manager can be installed on Ubuntu 24.04 and 26.04.

TWPM uses Telemt with real HTTPS, its own domain and the same Nginx instance. You can also configure its outbound traffic through SOCKS5 using an existing Cascade. Back up TWPM separately; its data is not part of the project's Backup v3.

<a id="technical-details"></a>

## ⚙️ Technical details and compatibility

- **Systems:** Ubuntu 24.04, Ubuntu 26.04, and Debian 13.
- **UFW:** allows 80/tcp, 443/tcp and 443/udp (plus 8443/udp when AmneziaWG is selected and 853/tcp when DoT is selected). During Installation, inactive UFW is enabled only after SSH access has been checked; otherwise it remains off with a warning. The manager can enable UFW after Restore **only if explicitly selected** and the safety checks pass. Standalone `x-ui-backup restore` does not enable UFW.
- **Certificates:** Let's Encrypt webroot and `certbot.timer` renew certificates automatically without stopping nginx.
- **Connection tracking:** Installation, Restore and Update can raise the Linux conntrack limit according to RAM when supported, without reducing an existing higher limit.
- **3x-ui version:** the installer uses the latest version verified for this project's configuration, which may differ from the newest upstream release. On compatible installations, use Update to obtain the verified 3x-ui and Xray versions. Independent updates in the panel are possible, but a newer version can block project Update. The installer also accepts another stable release with `-version <tag>` (minimum supported version: v3.8.0) and warns if compatibility has not been verified.

<a id="restore-on-new-vps"></a>

### Recovery on a new VPS

Use a clean VPS with the same OS family, version and architecture as the backup. Copy your Backup v3 archive to a private directory on the VPS (for example `/var/backups/x-ui/`). Point your domains at the new IP if it changes.

1. Connect by SSH as root and download and launch the stable manager using the command in [Quick start](#installation).
2. Select **3 — Backups → 3 — Restore backup**, not Installation. Choose the archive and confirm.
3. Follow the Security and UFW prompts. Check panel access, subscriptions, proxy connections and any additional services.

Do not install the project first. Full Restore returns the archived configuration. SSH access, OS settings and unrelated services remain your responsibility. Full Restore is blocked if TWPM is installed on the target; save and manage its data separately.

## 🤝 Credits / Origins

This project is based on [3x-ui-pro](https://github.com/mozaroc/3x-ui-pro) by mozaroc. It grew out of a fork of [x-ui-pro](https://github.com/mozaroc/x-ui-pro) and traces back to the original [x-ui-pro](https://github.com/GFW4Fun/x-ui-pro) by GFW4Fun. This project is now maintained independently. Since the fork, nginx/SNI routing and XHTTP configuration have been substantially reworked, TLS certificate issuance and automatic renewal redesigned, and Backup / Restore v3 introduced with recovery and rollback capabilities. Hysteria2 on UDP/443 was added, and identified security issues were fixed. In-place Update with data preservation and verified rollback has also been introduced.

The panel comes from [MHSanaei/3x-ui](https://github.com/MHSanaei/3x-ui). Third-party authors retain their rights and existing licenses.

Original xPROMSx developments and changes are offered under [GNU GPL-3.0-only](LICENSE). Copyright (C) 2026 xPROMSx contributors. The license covers only our rights in those materials; it does not relicense inherited code or establish GPL status for the entire repository. The scope of our license and notices for third-party components are explained in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

**If you want to reuse our code.** You don't need to do anything to install or use this project; it's still free. If you publish your own version or reuse our code in another project, keep the credit `© xPROMSx · github.com/xPROMSx/3x-ui-auto-nginx` in the README and, if your project has an interactive installer, on its start screen or main menu. Clearly state that your version has been modified. Don't present our work as your own. Full terms: [docs/NOTICE.md](docs/NOTICE.md).

<a id="windows-flag-emojis"></a>

## 🖥️ Country flags on Windows 11

If you see two-letter country codes (`FI`, `DE`, `US`) instead of flags when opening the **3x-ui** panel in **Chrome, Edge, or Brave** on Windows 11, try our small companion project [**Flag Emojis Windows Installer**](https://github.com/xPROMSx/flag-emojis-windows-installer). It restores flag emoji display, preserves the original Windows font, and lets you roll back the change.

<a id="support-development"></a>

## ❤️ Support Development

If you find this project useful, you can voluntarily support its continued development. The project and all its features remain free and open source, regardless of donations.

- **🌍 International:** [Support via Tribute (bank card)](https://web.tribute.tg/d/S0p)
- **🇷🇺 Russia:** [Support via SBP / Mir (bank app)](https://pay.cloudtips.ru/p/9f240c8b)
- [All donation methods](docs/SUPPORT.en.md) — including USDT, USDC, and Bitcoin.

<div align="center">

**Two domains. A few minutes. Your own 3x-ui / Xray server.**

⭐ If this installer helped you, consider giving it a [Star on GitHub](https://github.com/xPROMSx/3x-ui-auto-nginx). It helps others discover the project.

[Install](#installation) · [Releases](https://github.com/xPROMSx/3x-ui-auto-nginx/releases) · [Issues](https://github.com/xPROMSx/3x-ui-auto-nginx/issues)

</div>
