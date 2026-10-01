#!/usr/bin/env bash
set -euo pipefail
# Optional Comparator swap, only creates a new file. Never replaces existing swap.
test "$(id -u)" -eq 0 || exit 2
task_swap=/var/lib/perimeter/comparator.swap
if test -e "$task_swap"; then
  echo 'Existing swap file preserved; verify swapon --show'; exit 0
fi
task_fs=$(findmnt -no FSTYPE -T /var/lib/perimeter)
case "$task_fs" in ext4|xfs) ;; *) echo 'This helper supports ext4 / xfs'; exit 2;; esac
task_available=$(df -B1 --output=avail /var/lib/perimeter | tail -1)
test "$task_available" -gt 12884901888 || { echo 'Need at least 12 GiB free'; exit 2; }
fallocate -l 8G "$task_swap"
chmod 600 "$task_swap"
mkswap "$task_swap"
swapon "$task_swap"
printf '%s none swap sw 0 0\n' "$task_swap" >> /etc/fstab
sysctl vm.swappiness=10
echo 'vm.swappiness=10' > /etc/sysctl.d/80-perimeter-swap.conf
swapon --show
