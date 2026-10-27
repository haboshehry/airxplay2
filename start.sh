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

  # Restrict Avahi to the LAN interface. The container uses host networking,
  # so a default Avahi advertises the AirPlay service on every Docker bridge
  # and virtual interface too (172.x, 10.x, 169.254.x). Senders that resolve
  # one of those addresses cannot reach the receiver, which makes AirPlay 2
  # group joins fail intermittently ("speaker unavailable" / playback stops).
  avahi_conf=/etc/avahi/avahi-daemon.conf
  if [ -f "$avahi_conf" ]; then
    lan_if=$(ip -4 route show default 2>/dev/null | awk '{print $5; exit}')
    [ -n "$lan_if" ] || lan_if=eth0
    {
      echo '[server]'
      echo 'use-ipv4=yes'
      echo 'use-ipv6=no'
      echo "allow-interfaces=$lan_if"
      echo 'ratelimit-interval-usec=1000000'
      echo 'ratelimit-burst=1000'
      echo '[wide-area]'
      echo '[publish]'
      echo 'publish-hinfo=no'
      echo 'publish-workstation=no'
      echo '[reflector]'
      echo '[rlimits]'
    } > "$avahi_conf"
    echo "avahi restricted to $lan_if (IPv4 only)"
  fi

  avahi-daemon --daemonize --no-chroot 2>/dev/null || echo "warning: avahi-daemon did not start"
fi

# --- NQPTP, required by AirPlay 2 ---------------------------------------
if [ -z "${ENABLE_NQPTP+x}" ] || [ "${ENABLE_NQPTP:-1}" -eq 1 ]; then
  # Exactly one NQPTP may own the shared memory interface and UDP 319/320.
  # A second copy leaves the clock table unusable ("clock information is not
  # valid"), which is what makes AirPlay 2 groups drop as soon as they form.
  pkill -x nqptp 2>/dev/null || true
  sleep 0.3
  echo "starting nqptp"
  (/usr/local/bin/nqptp -v >/data/nqptp.log 2>&1) &
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
