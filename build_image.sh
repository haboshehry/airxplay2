#!/bin/sh
# Build the zima-airplay2 image without docker build support (commit-based).
set -e
DIR=/DATA/AppData/airplay2
IMG=zima-airplay2:1.0

docker rm -f a2build >/dev/null 2>&1 || true
echo "== creating container from base image"
docker run --name a2build --entrypoint sh mikebrady/shairport-sync:5.6 \
  -c 'apk add --no-cache python3 alsa-utils >/tmp/apk.log 2>&1 && echo installed' 

echo "== copying app"
docker cp "$DIR/app" a2build:/app
docker cp "$DIR/start.sh" a2build:/start-panel.sh
docker start a2build >/dev/null
docker exec a2build chmod +x /start-panel.sh
docker exec a2build mkdir -p /data
docker stop a2build >/dev/null

echo "== committing image"
docker commit \
  --change 'ENTRYPOINT ["/start-panel.sh"]' \
  --change 'CMD []' \
  --change 'WORKDIR /app' \
  --change 'EXPOSE 8080' \
  a2build "$IMG"
docker rm -f a2build >/dev/null 2>&1 || true
docker inspect "$IMG" --format 'ENTRYPOINT={{json .Config.Entrypoint}} CMD={{json .Config.Cmd}} SIZE={{.Size}}'
