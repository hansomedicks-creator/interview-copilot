#!/bin/bash
# Initial preparation only. No DNS, firewall, HTTPS, paid API calls or candidate imports.
set -euo pipefail
umask 077
[[ $EUID == 0 ]] || { echo 'Run as root'; exit 1; }
source /etc/os-release
[[ $ID == opencloudos && $VERSION_ID == 9* ]] || { echo 'OpenCloudOS 9 required'; exit 1; }
SOURCE=$(cd -- "$(dirname -- "$0")/../.." && pwd)
REVISION=$(git -C "$SOURCE" rev-parse HEAD)
[[ ${1:-} == "$REVISION" && $REVISION =~ ^[a-f0-9]{40}$ ]] || { echo 'Pass the full checked-out commit SHA'; exit 1; }
[[ -z $(git -C "$SOURCE" status --porcelain) ]] || { echo 'Source checkout must be clean'; exit 1; }
BASE=/opt/interview-copilot
RELEASE=$BASE/releases/$REVISION
CONFIG=/etc/interview-copilot
STATE=/var/lib/interview-copilot
if [[ -e $BASE/current || -L $BASE/current ]]; then
    echo 'Already prepared; initial installer will not replace an active release'; exit 1
fi
if [[ -e /var/lib/pgsql/data/PG_VERSION && ! -e $CONFIG/postgres-managed ]]; then
    echo 'Existing PostgreSQL cluster detected; refuse to change it'; exit 1
fi
if ss -H -lnt 'sport = :8000' | grep -q .; then
    echo 'Port 8000 is already in use; refuse to replace another service'; exit 1
fi
dnf -y install python3-pip postgresql-server postgresql-contrib rsync
python3 -c 'import sys; assert sys.version_info >= (3, 11)'
if ! id interview-copilot >/dev/null 2>&1; then
    useradd --system --home-dir "$STATE" --shell /sbin/nologin interview-copilot
fi
install -d -m 0755 "$BASE" "$BASE/releases"
install -d -m 0700 "$CONFIG" /var/backups/interview-copilot
install -d -m 0750 -o interview-copilot -g interview-copilot "$STATE" "$STATE/recordings" "$STATE/knowledge" "$STATE/knowledge/.obsidian" /var/cache/interview-copilot
if [[ ! -d $RELEASE ]]; then
    install -d -m 0755 "$RELEASE"
    git -C "$SOURCE" archive "$REVISION" | tar -x -C "$RELEASE"
    # Code is root-owned, readable by the service; secrets never enter this tree.
    chmod -R a+rX "$RELEASE"
fi
python3 -m venv "$RELEASE/.venv"
"$RELEASE/.venv/bin/python" -m pip install --no-cache-dir --prefer-binary "$RELEASE/backend[voice]"
"$RELEASE/.venv/bin/python" -m pip check
"$RELEASE/.venv/bin/python" -m pip freeze > "$RELEASE/requirements-linux.lock"
chmod -R a+rX "$RELEASE/.venv"
python3 "$RELEASE/deploy/linux/manage.py" init-env
if [[ ! -e /var/lib/pgsql/data/PG_VERSION ]]; then
    postgresql-setup --initdb
    touch "$CONFIG/postgres-managed"
fi
install -o postgres -g postgres -m 0600 "$RELEASE/deploy/linux/postgres-low-memory.conf" /var/lib/pgsql/data/interview-copilot.conf
python3 - <<'PY'
from pathlib import Path
config = Path('/var/lib/pgsql/data/postgresql.conf')
line = "include_if_exists = 'interview-copilot.conf'"
text = config.read_text()
if line not in text.splitlines():
    with config.open('a') as stream:
        stream.write('\n' + line + '\n')
hba = Path('/var/lib/pgsql/data/pg_hba.conf')
rule = 'host interview_copilot interview_copilot 127.0.0.1/32 scram-sha-256'
text = hba.read_text()
if rule not in text.splitlines():
    hba.write_text(rule + '\n' + text)
PY
systemctl enable --now postgresql
python3 "$RELEASE/deploy/linux/manage.py" init-db
ln -s "$RELEASE" "$BASE/current"
install -m 0644 "$RELEASE/deploy/linux/interview-copilot.service" /etc/systemd/system/interview-copilot.service
install -m 0644 "$RELEASE/deploy/linux/interview-copilot-backup.service" /etc/systemd/system/interview-copilot-backup.service
install -m 0644 "$RELEASE/deploy/linux/interview-copilot-backup.timer" /etc/systemd/system/interview-copilot-backup.timer
systemctl daemon-reload
systemctl enable --now interview-copilot
for attempt in {1..30}; do
    if curl -fsS --max-time 2 http://127.0.0.1:8000/api/v1/health >/dev/null; then break; fi
    sleep 2
done
python3 "$RELEASE/deploy/linux/manage.py" smoke
python3 "$RELEASE/deploy/linux/manage.py" status
systemctl start interview-copilot-backup.service
python3 "$RELEASE/deploy/linux/manage.py" verify-backup
systemctl enable --now interview-copilot-backup.timer
printf 'PREPARED_COMMIT=%s\n' "$REVISION"
systemctl show interview-copilot -p ActiveState -p MemoryCurrent -p NRestarts
free -m
echo 'PREPARATION_ONLY: loopback service; credentials/HTTPS/DNS/data migration still pending'
