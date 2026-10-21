# AirxPlay2 — AirPlay 2 audio receiver with a web panel

A self-contained AirPlay 2 receiver for Linux/Docker hosts: **Shairport Sync 5.6**
(AirPlay 2, NQPTP, Avahi) plus a small **web control panel** and a **CLI**.

Designed for headless boxes (NAS, mini‑PC, ZimaOS/ZimaCube) where the audio comes
out of HDMI and there is no hardware volume mixer — the usual reason generic
AirPlay containers end up silent or unadjustable.

- Bilingual panel: **English (default)** and **Persian**, switchable in the UI
- Server name, output device, volume, playback controls, now‑playing + cover art
- **Sync delay** for aligning with other speakers (with safety rails)
- Output diagnostics: live HDMI/ELD detection, per‑output test tone, logs
- Standard library Python only — no pip installs, nothing to break at build time

## Quick start

```bash
git clone https://github.com/haboshehry/airxplay2.git
cd AirxPlay2

# 1. host prerequisites: the container needs the sound device and host networking
ls /dev/snd            # must exist
docker compose up -d --build

# 2. open the panel
#    http://<host-ip>:8090
```

`docker-compose.yml`:

```yaml
services:
  AirxPlay2:
    build: .
    container_name: AirxPlay2
    network_mode: host
    restart: unless-stopped
    cap_add: [SYS_NICE]
    volumes:
      - /dev/snd:/dev/snd
      - /proc/asound:/host/asound:ro
      - ./data:/data
    environment:
      - PANEL_PORT=8090
```

If port `8090` is busy, change `PANEL_PORT`.

## CLI

`cli/AirxPlay2` wraps the HTTP API so you can script it:

```bash
./cli/AirxPlay2 status                 # service + session state
./cli/AirxPlay2 devices                # list playback PCMs, marks the live HDMI one
./cli/AirxPlay2 test                   # play a test tone on the selected output
./cli/AirxPlay2 test-all               # test every output one by one
./cli/AirxPlay2 name "Living Room"     # rename (restarts the service)
./cli/AirxPlay2 device plughw:CARD=PCH,DEV=3
./cli/AirxPlay2 volume 40              # 0-100
./cli/AirxPlay2 delay -150             # ms, negative = earlier output
./cli/AirxPlay2 play | pause | next | previous
./cli/AirxPlay2 restart | stop | start
./cli/AirxPlay2 logs
```

Point it elsewhere with `AXP_URL=http://192.168.1.20:8090 ./cli/AirxPlay2 status`.

## How it works

```
AirPlay sender ──▶ shairport-sync ──▶ ALSA (plughw:CARD=…,DEV=…) ──▶ HDMI / DAC
                        │
                        ├── metadata FIFO ──▶ panel (now playing, cover art)
                        └── D-Bus ──────────▶ panel (volume, play/pause, next)
```

| Path | Purpose |
|------|---------|
| `app/main.py` | supervisor + HTTP/JSON API + ALSA/mDNS/D-Bus integration |
| `app/static/index.html` | panel UI (EN/FA), polling `/api/state` |
| `start.sh` | container entrypoint: D-Bus, Avahi, NQPTP, then the panel |
| `cli/AirxPlay2` | CLI over the HTTP API |

The panel supervises `shairport-sync` itself (restart on crash, log rotation),
so no init system is required inside the container.

## Configuration notes

### Volume

Many HDMI outputs expose **no mixer control at all** (only an IEC958 switch), so
hardware volume is impossible. AirxPlay2 therefore drives Shairport Sync's own
software mixer over D-Bus (`RemoteControl.SetAirplayVolume`). The panel slider and
the sender's slider stay in step; the value shown is the one the live session
reports.

### Sync delay

`audio_backend_latency_offset_in_seconds` — **negative = earlier, positive = later**.

Use it when the host's output chain (e.g. `HDMI → TV → soundbar`) adds latency and
the device sounds late compared with other speakers.

Constraints worth knowing:

- Applying it needs a service restart, so **playback (and any AirPlay group) stops**;
  the panel asks for confirmation and you press Play again afterwards.
- A fixed offset makes one member diverge from an AirPlay 2 group clock. Keep values
  small, or calibrate while playing on this device alone.
- The compensating range is bounded by `audio_backend_buffer_desired_length_in_seconds`
  (0.4 s by default → about −400 ms).

### Output device pinning

For stable grouping, the generated config pins the ALSA side:

```ini
airplay_2_44100_only_mode = "on";   # no 48k/lossless/surround switching
lossless_mode = "off";
six_channel_mode = "off";
eight_channel_mode = "off";
output_rate = 44100;
output_format = "S16_LE";
output_channels = 2;
```

A group switch to 48k/lossless forces an HDMI re‑negotiation that can fail while the
display holds the PCM, dropping the receiver out of the group. Set
`{"stable_44k": false}` through `POST /api/config` (or `cli` config) to get the
"auto" behaviour back at the cost of that stability.

## HTTP API

```bash
GET  /api/state                      # everything the panel renders
GET  /api/logs                       # shairport log, panel log, raw metadata trace
GET  /api/artwork                    # current cover art (jpeg/png)
POST /api/config   {"name":"Kitchen","device":"plughw:CARD=PCH,DEV=3",
                    "latency_offset_ms":-150,"password":"","service_type":"auto"}
POST /api/volume   {"volume":40}
POST /api/action   {"action":"restart|stop|start|play|pause|playpause|next|previous|
                              test-device|test-all"}
```

`/api/config` returns `409 {"needs_confirm": true}` when a change needs a restart
during playback; resend the same body with `"force": true` to apply it anyway.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Visible in AirPlay but silent | Display is on another HDMI PCM — run `cli/AirxPlay2 devices` and pick the row marked `✓` |
| `Output is busy` on test | Something else holds the PCM (the service while playing); use *test all* which stops it briefly |
| No display detected | `HDMI` with no sink produces no audio at all; check the cable/TV input |
| Still late after −400 ms | TV processing is heavy: `Sound Mode = Game/Standard`, `AV Sync = 0`, PCM instead of Dolby |
| Metadata missing | Depends on the sender. The panel says so explicitly when only volume arrives |
| Group drops out | See "Output device pinning"; also make sure NQPTP owns UDP 319/320 |

Logs:

```bash
docker logs --tail 50 AirxPlay2
docker exec -it AirxPlay2 sh -c 'tail -50 /data/shairport.log'
docker exec -it AirxPlay2 sh -c 'tail -20 /data/diag.log'
```

## ZimaOS / ZimaCube

ZimaOS ships a Docker CLI **without `docker build`**. Use the commit-based helper
(`./build_image.sh`) or build the image on another machine and push it.

Also note the container must use `network_mode: host` (mDNS + AirPlay ports) and
needs `SYS_NICE` for AirPlay 2 timing (NQPTP).

## License

MIT — see [LICENSE](LICENSE).
