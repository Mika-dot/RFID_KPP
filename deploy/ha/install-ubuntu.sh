#!/usr/bin/env bash
set -euo pipefail
# Run as root on an amd64 Ubuntu 22.04/24.04 VM. Existing disks are preserved.
test "$(id -u)" -eq 0 || { echo 'Run with sudo'; exit 2; }
test "$(dpkg --print-architecture)" = amd64 || { echo 'amd64 is required'; exit 2; }
task_root=/opt/perimeter
task_user=perimeter
source /etc/os-release
case "$VERSION_ID" in 22.04|24.04) ;; *) echo 'Supported Ubuntu: 22.04 / 24.04'; exit 2;; esac
dpkg --add-architecture i386
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y git curl unzip python3-venv python3-dev \
  build-essential unixodbc-dev wine wine32:i386 wine64 xvfb xauth tzdata
if ! odbcinst -q -d 2>/dev/null | grep -q 'ODBC Driver 18 for SQL Server'; then
  task_pkg=$(mktemp --suffix=.deb)
  curl --fail --location --output "$task_pkg" "https://packages.microsoft.com/config/ubuntu/$VERSION_ID/packages-microsoft-prod.deb"
  dpkg -i "$task_pkg"
  rm "$task_pkg"
  apt-get update
  ACCEPT_EULA=Y DEBIAN_FRONTEND=noninteractive apt-get install -y msodbcsql18
fi
id "$task_user" >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/perimeter --shell /usr/sbin/nologin "$task_user"
install -d -o "$task_user" -g "$task_user" "$task_root" /var/lib/perimeter /etc/perimeter "$task_root/releases"
install -d -m 750 -o "$task_user" -g "$task_user" \
  /var/lib/perimeter/.config /var/lib/perimeter/.config/Ultralytics
if ! test -d "$task_root/source/.git"; then
  runuser -u "$task_user" -- git clone --branch feature/perimeter-ha-guardian https://github.com/Mika-dot/RFID_KPP.git "$task_root/source"
fi
python3 -m venv "$task_root/venv"
# Both Ubuntu reserves run YOLO on CPU. Install its matching CPU wheels before
# Ultralytics resolves dependencies, avoiding the default Linux CUDA packages.
"$task_root/venv/bin/python" -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
"$task_root/venv/bin/python" -m pip install -r "$task_root/source/guardian/requirements.txt"
install -d -o "$task_user" -g "$task_user" "$task_root/python32"
if ! test -f "$task_root/python32/python.exe"; then
  task_zip=$(mktemp --suffix=.zip)
  curl --fail --location --output "$task_zip" https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-win32.zip
  unzip -q "$task_zip" -d "$task_root/python32"
  rm "$task_zip"
fi
chown -R "$task_user:$task_user" "$task_root" /var/lib/perimeter
runuser -u "$task_user" -- env WINEARCH=win32 WINEPREFIX=/var/lib/perimeter/wine32 WINEDEBUG=-all xvfb-run -a wineboot -u
if ! test -f /etc/perimeter/node.json; then
  install -m 600 -o "$task_user" -g "$task_user" "$task_root/source/deploy/ha/node.example.json" /etc/perimeter/node.json
fi
if ! test -f /etc/perimeter/environment; then
  install -m 600 -o "$task_user" -g "$task_user" "$task_root/source/deploy/ha/environment.example" /etc/perimeter/environment
fi
install -m 644 "$task_root/source/deploy/ha/perimeter-guardian.service" /etc/systemd/system/perimeter-guardian.service
systemctl daemon-reload
timedatectl set-timezone Europe/Moscow
echo 'Installed. Fill /etc/perimeter/node.json and /etc/perimeter/environment locally.'
echo 'Set node_id=comparator on Comparator. Replace both VM URLs on every node.'
echo 'After doctor passes and SQL cutover: systemctl enable --now perimeter-guardian'
