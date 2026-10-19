#!/bin/bash
# Separate proxy preparation for Baota images which exclude nginx RPMs.
# Does NOT enable the public service, alter firewall/DNS, or request a certificate.
set -euo pipefail
umask 077
[[ $EUID == 0 && $(uname -m) == x86_64 ]] || { echo 'root / Linux x86_64 required'; exit 1; }
SOURCE=$(cd -- "$(dirname -- "$0")/../.." && pwd)
VERSION=2.11.7
SHA256=727b91701a392de6ebc5027509f548bf39979e5216340d0faed8fa5e69c84f8b
BINARY_SHA256=678ade3bfc088749c81a681adc603333ee0bb023b6a6cfe3c0f58bef8ff854e9
DEST=/opt/interview-copilot/tools/caddy/$VERSION
install -d -m 0755 "$DEST"
if [[ ! -f $DEST/caddy ]]; then
    curl --proto '=https' --tlsv1.2 -fL --retry 2 --connect-timeout 20 --max-time 180 \
        "https://github.com/caddyserver/caddy/releases/download/v${VERSION}/caddy_${VERSION}_linux_amd64.tar.gz" \
        -o "$DEST/release.tar.gz"
    printf '%s  %s\n' "$SHA256" "$DEST/release.tar.gz" | sha256sum -c -
    tar -xzf "$DEST/release.tar.gz" -C "$DEST" caddy
    chmod 0755 "$DEST/caddy"
fi
printf '%s  %s\n' "$BINARY_SHA256" "$DEST/caddy" | sha256sum -c -
"$DEST/caddy" version
if ! id interview-proxy >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/interview-proxy --shell /sbin/nologin interview-proxy
fi
install -d -m 0755 /etc/interview-copilot-proxy
install -d -m 0750 -o interview-proxy -g interview-proxy /var/lib/interview-proxy
if [[ ! -e /etc/interview-copilot-proxy/Caddyfile ]]; then
    install -m 0644 "$SOURCE/deploy/linux/Caddyfile" /etc/interview-copilot-proxy/Caddyfile
fi
if [[ ! -e /etc/systemd/system/interview-copilot-proxy.service ]]; then
    install -m 0644 "$SOURCE/deploy/linux/interview-copilot-proxy.service" /etc/systemd/system/interview-copilot-proxy.service
    systemctl daemon-reload
fi
# Validate full public config without starting it or issuing a certificate.
runuser -u interview-proxy -- env HOME=/var/lib/interview-proxy XDG_DATA_HOME=/var/lib/interview-proxy/data \
    "$DEST/caddy" validate --config /etc/interview-copilot-proxy/Caddyfile
if ss -H -lnt 'sport = :18080' | grep -q .; then
    echo 'Test port 18080 is occupied; refuse to interfere with another listener'; exit 1
fi
CHECK=$(mktemp -d /var/tmp/interview-caddy-check.XXXXXX)
printf '{\n admin off\n auto_https off\n}\nhttp://127.0.0.1:18080 {\n reverse_proxy 127.0.0.1:8000\n}\n' > "$CHECK/Caddyfile"
"$DEST/caddy" run --config "$CHECK/Caddyfile" > "$CHECK/proxy.log" 2>&1 &
PROXY_PID=$!
trap 'kill "$PROXY_PID" 2>/dev/null || true; wait "$PROXY_PID" 2>/dev/null || true' EXIT
for attempt in {1..20}; do
    if curl -fsS --max-time 2 http://127.0.0.1:18080/api/v1/health >/dev/null 2>&1; then break; fi
    sleep 1
done
python3 /opt/interview-copilot/current/deploy/linux/manage.py smoke --base-url http://127.0.0.1:18080
echo 'CADDY_PROXY_PREPARED; PUBLIC_SERVICE_DISABLED; REAL_HTTPS_PENDING'
