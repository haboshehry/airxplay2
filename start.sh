#!/bin/sh
# Container entrypoint: system services for AirPlay 2, then the web panel,
# which in turn supervises shairport-sync.
set -e

echo "Zima AirPlay 2 starting ($(date))"

# --- D-Bus + mDNS (Avahi) ------------------------------------------------
if [ -z "${ENABLE_AVAHI+x}" ] || [ "${ENABLE_AVAHI:-1}" -eq 1 ]; then
  rm -f /run/dbus/dbus.pid /run/avahi-daemon/pid 2>/dev/null || true
  dbus-uuidgen --ensure 2>/dev/null || true
  dbus-daemon --system 2>/dev/null || echo "warning: dbus-daemon did not start"
  avahi-daemon --daemonize --no-chroot 2>/dev/null || echo "warning: avahi-daemon did not start"
fi

# --- NQPTP, required by AirPlay 2 ---------------------------------------
if [ -z "${ENABLE_NQPTP+x}" ] || [ "${ENABLE_NQPTP:-1}" -eq 1 ]; then
  echo "starting nqptp"
  (/usr/local/bin/nqptp >/dev/null 2>&1) &
fi

# Wait for avahi, but never block forever (host mDNS may already own 5353).
i=0
while [ ! -f /var/run/avahi-daemon/pid ] && [ "$i" -lt 8 ]; do
  echo "waiting for avahi ($i)"
  sleep 2
  i=$((i+1))
done

export XDG_RUNTIME_DIR=/tmp

mkdir -p /data
exec python3 /app/main.py
