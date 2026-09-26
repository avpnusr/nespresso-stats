#!/bin/sh
# Run as root briefly to match PUID/PGID, then drop to that user.
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
case "$PUID" in ''|*[!0-9]*) echo "PUID must be numeric, got: $PUID" >&2; exit 1 ;; esac
case "$PGID" in ''|*[!0-9]*) echo "PGID must be numeric, got: $PGID" >&2; exit 1 ;; esac

GRP=$(awk -F: -v g="$PGID" '$3 == g { print $1; exit }' /etc/group)
[ -n "$GRP" ] || { addgroup -g "$PGID" app; GRP=app; }

USR=$(awk -F: -v u="$PUID" '$3 == u { print $1; exit }' /etc/passwd)
[ -n "$USR" ] || { adduser -D -u "$PUID" -G "$GRP" -H -s /sbin/nologin app; USR=app; }

mkdir -p /data
chown -R "$PUID:$PGID" /data

exec su-exec "$USR:$GRP" "$@"
