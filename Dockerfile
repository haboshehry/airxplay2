# Zima AirPlay 2 Receiver
# Base image ships Shairport Sync 5.6 built with AirPlay 2, Avahi, ALSA,
# metadata, soxr and the D-Bus/MPRIS interfaces, plus NQPTP.
FROM mikebrady/shairport-sync:5.6

RUN apk add --no-cache python3 alsa-utils

COPY app/ /app/
COPY start.sh /start-panel.sh
RUN chmod +x /start-panel.sh && mkdir -p /data

EXPOSE 8090

ENTRYPOINT ["/start-panel.sh"]
