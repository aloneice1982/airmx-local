#!/bin/sh
# Installed as /etc/airmx-recovery/apply-rules.sh on this household's WRT.
set -eu
exec 9>/var/lock/airmx-recovery-rules.lock
flock -x 9
transaction=$(mktemp /tmp/airmx-rules.XXXXXX)
trap 'rm -f "$transaction"' EXIT HUP INT TERM
if nft list table ip airmx_recovery >/dev/null 2>&1; then
    printf '%s\n' 'delete table ip airmx_recovery' > "$transaction"
fi
cat /etc/airmx-recovery/recovery-rules.nft >> "$transaction"
# Delete/recreate together in one atomic transaction; never flush other tables.
nft -c -f "$transaction"
nft -f "$transaction"
