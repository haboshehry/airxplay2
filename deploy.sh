#!/bin/sh
# Build (if needed) and (re)start the receiver. Safe to re-run.
# ZimaOS's docker CLI has no `build` command, so the image is assembled with
# `docker commit` -- see build_image.sh.
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
NAME="airplay2"
IMAGE="zima-airplay2:1.0"

mkdir -p "$DIR/data"

if [ "$1" = "--build" ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "== building image"
  sh "$DIR/build_image.sh"
fi

echo "== recreating container"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" \
  --restart unless-stopped \
  --network host \
  --cap-add SYS_NICE \
  --device /dev/snd \
  -v /dev/snd:/dev/snd \
  -v /proc/asound:/host/asound:ro \
  -v /sys/class/drm:/host/drm:ro \
  -v "$DIR/data:/data" \
  "$IMAGE"

sleep 8
docker ps --filter "name=$NAME" --format '{{.Names}}  {{.Status}}'
IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
echo "== panel: http://${IP}:8090"
echo "== follow logs: docker logs -f $NAME"
