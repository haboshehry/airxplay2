#!/usr/bin/env python3
"""Zima AirPlay 2 Receiver -- control backend.

Runs inside the container, supervises shairport-sync, exposes a small JSON API
and a web panel. Standard library only (no pip dependencies).
"""
import base64
import json
import os
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from collections import deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import rtprio

DATA = "/data"
APP = "/app"
STATE_PATH = os.path.join(DATA, "config.json")
UA_PATH = os.path.join(DATA, "upstream.json")
SS_CONF = "/etc/shairport-sync.conf"
META_PIPE = os.path.join(DATA, "metadata.pipe")
LOG_PATH = os.path.join(DATA, "shairport.log")
ART_PATH = os.path.join(DATA, "artwork.bin")
DIAG_PATH = os.path.join(DATA, "diag.log")
HOST_ASOUND = "/host/asound"
SS_BIN = shutil.which("shairport-sync") or "/usr/local/bin/shairport-sync"
PANEL_PORT = int(os.environ.get("PANEL_PORT", "8090"))

DEFAULTS = {
    "name": "Zima AirPlay",
    "device": "",
    "volume": 60,
    "password": "",
    "service_type": "auto",
    "port": 0,
    "volume_range_db": 60,
    "latency_offset_ms": 0,   # + delays, - advances; syncs with other speakers
    "stable_44k": True,       # pin the *output* to 48k/S16/stereo (never restrict the input)
    "bind_address": "",       # "" = auto-detect the LAN IPv4 and bind RTSP to it
    "verbose": False,         # -v on shairport-sync, for deep diagnosis
    "interpolation": "auto",
    "autostart": True,
}


def diag(msg):
    try:
        line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        with open(DIAG_PATH, "a") as f:
            f.write(line)
    except Exception:
        pass


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------
class Settings:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(STATE_PATH) as f:
                disk = json.load(f)
            if isinstance(disk, dict):
                for k in DEFAULTS:
                    if k in disk:
                        self.data[k] = disk[k]
        except FileNotFoundError:
            pass
        except Exception as e:
            diag("settings load failed: %r" % (e,))

    def save(self):
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, STATE_PATH)

    def get(self, key):
        with self.lock:
            return self.data.get(key, DEFAULTS.get(key))

    def snapshot(self):
        with self.lock:
            return dict(self.data)

    def update(self, patch):
        with self.lock:
            for k, v in patch.items():
                if k in DEFAULTS:
                    self.data[k] = v
            snap = dict(self.data)
        self.save()
        return snap


CFG = Settings()


# --------------------------------------------------------------------------
# ALSA device discovery
# --------------------------------------------------------------------------
def asound_root():
    for root in (HOST_ASOUND, "/proc/asound"):
        if os.path.exists(os.path.join(root, "cards")):
            return root
    return HOST_ASOUND


def _read(path, default=""):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except Exception:
        return default


def list_devices():
    """Enumerate playback PCMs from /proc/asound (mounted from the host)."""
    cards = {}
    card_txt = _read(os.path.join(asound_root(), "cards"))
    for line in card_txt.splitlines():
        m = re.match(r"\s*(\d+)\s+\[([^\]]+)\]:\s*(.*?)\s*-\s*(.*)$", line)
        if m:
            idx, cid = int(m.group(1)), m.group(2).strip()
            cards[idx] = {"id": cid, "driver": m.group(3).strip(), "name": m.group(4).strip()}

    devices = []
    pcm_txt = _read(os.path.join(asound_root(), "pcm"))
    for line in pcm_txt.splitlines():
        # 00-03: HDMI 0 : HDMI 0 : playback 1
        m = re.match(r"\s*(\d+)-(\d+):\s*(.*?)\s*:\s*(.*?)\s*:\s*(playback|capture)\s+(\d+)", line)
        if not m or m.group(5) != "playback":
            continue
        card = int(m.group(1))
        dev = int(m.group(2))
        cardinfo = cards.get(card, {"id": "card%d" % card, "name": "Card %d" % card})
        devname = m.group(3).strip()
        devices.append({
            "card": card,
            "device": dev,
            "card_id": cardinfo["id"],
            "card_name": cardinfo["name"],
            "pcm_name": devname,
            "label": "%s · %s" % (cardinfo["name"], devname),
            "value": "plughw:CARD=%s,DEV=%d" % (cardinfo["id"], dev),
            "hw": "hw:%d,%d" % (card, dev),
            "connected": None,
        })
    if not devices:
        devices = _devices_from_aplay()
    return devices


def _devices_from_aplay():
    out = []
    try:
        txt = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return out
    for line in txt.splitlines():
        m = re.match(r"card (\d+): (\S+) \[([^\]]*)\], device (\d+): ([^\[]*)\[", line)
        if m:
            out.append({
                "card": int(m.group(1)), "device": int(m.group(4)),
                "card_id": m.group(2), "card_name": m.group(3).strip(),
                "pcm_name": m.group(5).strip(),
                "label": "%s · %s" % (m.group(3).strip(), m.group(5).strip()),
                "value": "plughw:CARD=%s,DEV=%d" % (m.group(2), int(m.group(4))),
                "hw": "hw:%s,%s" % (m.group(1), m.group(4)),
                "connected": None,
            })
    return out


_ELD_CACHE = {"t": 0.0, "v": []}


def eld_sinks():
    """Live HDMI sinks reported by the HDA driver: monitor name + converter."""
    now = time.time()
    if now - _ELD_CACHE["t"] < 5:
        return _ELD_CACHE["v"]
    out = []
    root = asound_root()
    try:
        for card in sorted(os.listdir(root)):
            if not re.match(r"^card\d+$", card):
                continue
            cdir = os.path.join(root, card)
            for name in sorted(os.listdir(cdir)):
                if not name.startswith("eld#"):
                    continue
                txt = _read(os.path.join(cdir, name))
                if not (re.search(r"monitor_present\s+1", txt) and re.search(r"eld_valid\s+1", txt)):
                    continue
                mon = re.search(r"monitor_name\s+(.+)", txt)
                cvt = re.search(r"codec_cvt_nid\s+0x([0-9a-fA-F]+)", txt)
                out.append({
                    "monitor": mon.group(1).strip() if mon else "",
                    "cvt": int(cvt.group(1), 16) if cvt else None,
                })
    except Exception:
        out = []
    _ELD_CACHE.update({"t": now, "v": out})
    return out


def _hdmi_converters():
    """Converter node ids in the order the driver creates PCM devices."""
    root = asound_root()
    try:
        for name in sorted(os.listdir(os.path.join(root, "card0"))):
            if not name.startswith("codec#"):
                continue
            txt = _read(os.path.join(root, "card0", name))
            nodes = re.findall(r"^Node 0x([0-9a-f]+) \[Audio Output\]", txt, re.M)
            if len(nodes) >= 2:
                return [int(n, 16) for n in nodes]
    except Exception:
        pass
    return []


def display_map(devices):
    """device number -> monitor name, pairing converters with PCM devices."""
    sinks = {s["cvt"]: s["monitor"] for s in eld_sinks() if s["cvt"] is not None}
    if not sinks:
        return {}
    cvts = _hdmi_converters()
    nums = sorted({d["device"] for d in devices})
    out = {}
    for i, cvt in enumerate(cvts):
        if i < len(nums) and cvt in sinks:
            out[nums[i]] = sinks[cvt]
    return out

def default_device():
    devs = list_devices()
    for d in devs:
        if d.get("connected") is True:
            return d["value"]
    if devs:
        return devs[0]["value"]
    return "plughw:0,3"


# --------------------------------------------------------------------------
# volume control (adaptive)
# --------------------------------------------------------------------------
class VolumeCtl:
    """Controls playback volume.

    Modes, in order of preference:
      dbus   - shairport-sync D-Bus Volume property (smooth, native)
      amixer - an ALSA mixer/softvol control (fallback)
      none   - only the AirPlay source can change volume
    """

    def __init__(self):
        self.mode = "unknown"
        self.iface = None
        self.path = "/org/gnome/ShairportSync"
        self.sig = None
        self.mixer = None
        self.note = ""
        self.db_lo = -30.0      # shairport's software mixer floor, in dB
        self.last_err = ""

    # -- dbus ------------------------------------------------------------
    def _dbus(self, *args, timeout=6):
        if not shutil.which("dbus-send"):
            return None
        cmd = ["dbus-send", "--system", "--print-reply"] + list(args)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            self.last_err = (p.stderr or "").strip()
            if p.returncode == 0:
                return p.stdout
            diag("dbus err: %s" % (self.last_err[:300],))
        except Exception as e:
            self.last_err = repr(e)
            diag("dbus exec failed: %r" % (e,))
        return None

    def detect(self):
        prev = self.mode
        self.mode = "none"
        self.note = ""
        xml = self._dbus("--dest=org.gnome.ShairportSync", self.path,
                         "org.freedesktop.DBus.Introspectable.Introspect")
        if xml:
            # dbus-send returns the XML as one quoted string: unescape it first
            x = xml.replace('\\"', '"')
            best = None
            for m in re.finditer(r'<property\b[^>]*name="Volume"[^>]*>', x):
                tag = m.group(0)
                tm = re.search(r'type="([^"]+)"', tag)
                if not tm or "readwrite" not in tag:
                    continue
                start = x.rfind("<interface", 0, m.start())
                im = re.search(r'<interface\s+name="([^"]+)"', x[start:m.start()])
                iface = im.group(1) if im else "org.gnome.ShairportSync"
                # prefer the core interface (dB scale), fall back to RemoteControl
                cand = {"iface": iface, "sig": tm.group(1)}
                if iface == "org.gnome.ShairportSync":
                    best = cand
                    break
                if best is None:
                    best = cand
            if best:
                self.iface = best["iface"]
                self.sig = best["sig"]
                self.mode = "dbus"
                self.note = "%s.%s" % (self.iface, "Volume")
        if self.mode == "none":
            self._detect_mixer()
        if self.mode != prev:
            diag("volume mode -> %s (%s)" % (self.mode, self.note))
        return self.mode

    def _db_for_pct(self, pct):
        if pct <= 0:
            return "-144.0"
        if self.sig in ("d", "f"):
            return "%.2f" % (self.db_lo * (1.0 - pct / 100.0))
        return "%d" % pct

    def _detect_mixer(self):
        self.mixer = None
        card = None
        m = re.search(r"CARD=([^,\s]+)", CFG.get("device") or "")
        if m:
            card = m.group(1)
        for ctl in ("AirPlayVol", "PCM", "Master"):
            cmd = ["amixer"] + (["-c", card] if card else []) + ["sset", ctl, "1%"]
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            except Exception:
                continue
            if p.returncode == 0:
                self.mixer = {"control": ctl, "card": card}
                self.mode = "amixer"
                self.note = "%s on %s" % (ctl, card or "default")
                return

    # -- public ----------------------------------------------------------
    def set_pct(self, pct):
        pct = max(0, min(100, int(round(pct))))
        if self.mode in ("unknown", "none"):
            self.detect()
        if self.mode == "dbus" and self.sig:
            vsig = {"d": "double", "f": "double", "i": "int32", "u": "uint32",
                    "n": "int16", "q": "uint16", "y": "byte"}.get(self.sig, "int32")
            # The Volume *property* is applied "locally only" and never reaches
            # an active AirPlay 2 session, so the listener hears no change.
            # RemoteControl.SetAirplayVolume pushes the value into the session.
            if self.sig in ("d", "f"):
                out = self._dbus("--dest=org.gnome.ShairportSync", self.path,
                                 "org.gnome.ShairportSync.RemoteControl.SetAirplayVolume",
                                 "double:%s" % self._db_for_pct(pct))
                if out is not None:
                    return True, "SetAirplayVolume"
            for attempt in (1, 2):
                out = self._dbus("--dest=org.gnome.ShairportSync", self.path,
                                 "org.freedesktop.DBus.Properties.Set",
                                 "string:%s" % self.iface, "string:Volume",
                                 "variant:%s:%s" % (vsig, self._db_for_pct(pct)))
                if out is not None:
                    return True, "dbus"
                # learn the real dB range from the error and retry once
                rm = re.search(r"range\s+(-?[\d.]+)\s+to\s+(-?[\d.]+)", self.last_err or "")
                if attempt == 1 and rm and self.sig in ("d", "f"):
                    try:
                        new_lo = float(rm.group(1))
                        if new_lo < 0 and abs(new_lo - self.db_lo) > 0.5:
                            self.db_lo = new_lo
                            diag("volume dB floor learned: %s" % new_lo)
                            continue
                    except Exception:
                        pass
                break
        if self.mode == "amixer" and self.mixer:
            cmd = ["amixer"] + (["-c", self.mixer["card"]] if self.mixer["card"] else []) + \
                  ["sset", self.mixer["control"], "%d%%" % pct]
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if p.returncode == 0:
                    return True, "amixer"
            except Exception:
                pass
        return False, self.mode

    def get_pct(self):
        if self.mode in ("unknown", "none"):
            self.detect()
        if self.mode == "dbus":
            out = self._dbus("--dest=org.gnome.ShairportSync", self.path,
                             "org.freedesktop.DBus.Properties.Get",
                             "string:%s" % self.iface, "string:Volume")
            if out:
                m = re.search(r"variant\s+(\S+)\s+(-?[\d.]+)", out)
                if m:
                    v = float(m.group(2))
                    if m.group(1) in ("double", "float"):
                        if v <= -100:      # mute
                            return 0
                        pct = 100.0 * (1.0 - (v / self.db_lo)) if self.db_lo else v
                        return int(round(max(0.0, min(100.0, pct))))
                    return int(round(v))
        if self.mode == "amixer" and self.mixer:
            cmd = ["amixer"] + (["-c", self.mixer["card"]] if self.mixer["card"] else []) + \
                  ["sget", self.mixer["control"]]
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
                m = re.search(r"\[(\d+)%\]", out)
                if m:
                    return int(m.group(1))
            except Exception:
                pass
        return None


VOL = VolumeCtl()


def apply_volume_async(attempts=12, delay=2.0):
    """Push the saved volume into shairport-sync once it is reachable.

    Retries because the D-Bus name only appears a moment after the service
    starts. Deliberately not a periodic job: the AirPlay sender owns the
    volume between panel changes, and a timer would fight the phone's slider.
    """
    def run():
        for i in range(attempts):
            ok, how = VOL.set_pct(CFG.get("volume"))
            diag("volume apply %s%% -> %s (%s, try %d)" % (CFG.get("volume"), ok, how, i + 1))
            if ok:
                return
            time.sleep(delay)

    threading.Thread(target=run, daemon=True).start()


# --------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------
CODE_NAMES = {
    "minm": "title", "asar": "artist", "asal": "album", "asgn": "genre",
    "snam": "stream", "snna": "stream", "mikd": "kind", "pvol": "volume",
    "prgr": "progress", "pbeg": "playing", "pend": "stopped", "paus": "paused",
    "prsm": "playing", "pfls": "playing", "mdst": "meta_start", "mden": "meta_end",
}


class NowPlaying:
    def __init__(self):
        self.lock = threading.Lock()
        self.info = {"title": "", "artist": "", "album": "", "genre": "",
                     "state": "idle", "source": "", "art_version": 0,
                     "progress": 0.0, "duration": 0.0, "raw": []}
        self.copl_dump = ""

    def reset_session(self):
        with self.lock:
            self.info.update({"title": "", "artist": "", "album": "", "genre": "",
                              "state": "idle", "progress": 0.0, "duration": 0.0})

    def snapshot(self):
        with self.lock:
            return dict(self.info)

    def apply(self, typ, code, data):
        name = CODE_NAMES.get(code)
        with self.lock:
            if code == "pict":
                try:
                    with open(ART_PATH, "wb") as f:
                        f.write(data)
                    self.info["art_version"] += 1
                except Exception as e:
                    diag("art save failed: %r" % (e,))
                return
            if code == "copl":
                self._apply_copl(data)
                return
            if name in ("title", "artist", "album", "genre") and data:
                try:
                    self.info[name] = data.decode("utf-8", "replace").strip()
                except Exception:
                    pass
                return
            if name == "state":
                self.info["state"] = data
                return
            if code in ("pbeg", "prsm", "pfls"):
                self.info["state"] = "playing"
                return
            if code == "paus":
                self.info["state"] = "paused"
                return
            if code in ("pend", "pfls"):
                self.info["state"] = "idle"
                return
            if code == "pvol" and data:
                # "-4.50,-4.50,-60.00,0.00": current dB, ?, floor dB, ?
                try:
                    parts = [p.strip() for p in data.decode("ascii", "replace").split(",")]
                    self.info["ap_volume_db"] = float(parts[0])
                    if len(parts) > 2:
                        floor = float(parts[2])
                        if floor < 0:
                            self.info["ap_volume_floor"] = floor
                except Exception:
                    pass
                return
            if code == "prgr" and data:
                try:
                    parts = [p for p in data.decode("ascii", "replace").split("/")]
                    if len(parts) >= 3:
                        cur, start, end = float(parts[0]), float(parts[1]), float(parts[2])
                        dur = (end - start) / 44100.0
                        el = (cur - start) / 44100.0
                        if dur > 0:
                            self.info["duration"] = round(dur, 1)
                            self.info["progress"] = round(max(0.0, min(1.0, el / dur)), 4)
                except Exception:
                    pass
                return

    def _apply_copl(self, data):
        """AirPlay 2 rich metadata arrives as a plist."""
        try:
            obj = plistlib.loads(data)
        except Exception:
            try:
                obj = plistlib.loads(data, fmt=plistlib.FMT_XML)
            except Exception as e:
                diag("copl parse failed: %r" % (e,))
                return
        try:
            self.copl_dump = repr(obj)[:4000]
            with open(os.path.join(DATA, "copl-last.txt"), "w") as f:
                f.write(self.copl_dump)
        except Exception:
            pass
        art = find_artwork(obj)
        if art:
            commit_art_locked(self, art)
        fields = best_effort_metadata(obj)
        for k in ("title", "artist", "album", "genre", "duration"):
            v = fields.get(k)
            if v:
                if k == "duration":
                    try:
                        self.info["duration"] = round(float(v), 1)
                    except Exception:
                        pass
                else:
                    self.info[k] = str(v).strip()


TITLE_KEYS = ("title", "name", "itemName", "kMDItemTitle", "minm", "trackName",
              "kMRMediaRemoteNowPlayingInfoTitle")
ARTIST_KEYS = ("artist", "artistName", "albumArtist", "kMDItemAuthors", "asar", "creator",
               "kMRMediaRemoteNowPlayingInfoArtist")
ALBUM_KEYS = ("album", "albumName", "kMDItemAlbum", "asal", "disco",
              "kMRMediaRemoteNowPlayingInfoAlbum")
GENRE_KEYS = ("genre", "kMDItemGenre", "asgn",
              "kMRMediaRemoteNowPlayingInfoGenre")
DUR_KEYS = ("duration", "totalTime", "kMDItemDurationSeconds", "durationInMilliseconds",
            "kMRMediaRemoteNowPlayingInfoDuration")


def find_artwork(obj):
    """Return image bytes embedded in an AirPlay 2 metadata plist, if any."""
    found = {"v": None}

    def walk(node):
        if found["v"] is not None:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and "artwork" in k.lower() and isinstance(v, bytes) and len(v) > 200:
                    found["v"] = v
                    return
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    try:
        walk(obj)
    except Exception:
        return None
    data = found["v"]
    if not data:
        return None
    if data[:2] == b"\xff\xd8" or data[:4] == b"\x89PNG":
        return data
    return None


def classify_key(key):
    """Map Apple/MediaRemote style keys onto our four display fields."""
    k = key.lower()
    if "artwork" in k or "picture" in k:
        return None
    if k.endswith("title") or k in ("title", "minm", "name", "itemname"):
        return "title"
    if "artist" in k or k in ("asar", "creator"):
        return "artist"
    if "album" in k or k in ("asal", "disco"):
        return "album"
    if "genre" in k or k == "asgn":
        return "genre"
    if "duration" in k or "totaltime" in k:
        return "duration"
    return None


def best_effort_metadata(obj, depth=0):
    """Walk an arbitrary plist looking for the usual now-playing fields."""
    found = {}

    def put(key, val):
        if isinstance(val, (dict, list)):
            return
        target = classify_key(key)
        if not target:
            return
        if isinstance(val, bytes) and len(val) > 200:
            return
        if isinstance(val, bytes):
            try:
                val = val.decode("utf-8", "replace")
            except Exception:
                return
        if isinstance(val, (int, float)) and target == "duration" and val > 10000:
            val = val / 1000.0 if val < 1e8 else val / 44100.0
        if target not in found or (isinstance(val, str) and len(str(val)) > len(str(found[target]))):
            found[target] = val

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str):
                    put(k, v)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    if depth == 0:
        walk(obj)
    return found


NP = NowPlaying()


def mpris_status(timeout=4):
    """PlaybackStatus from shairport's MPRIS interface ("playing"/"paused"/"stopped").

    The metadata pipe is not a reliable "is a session live?" signal: a sender in
    an AirPlay 2 group may send audio to this receiver while the metadata (and
    therefore ``NP``'s state) stays empty or goes stale. Deciding "idle" from
    metadata made the panel restart shairport in the middle of grouped playback,
    which drops the receiver out of the group. MPRIS reflects the real player.
    """
    if not shutil.which("dbus-send"):
        return None
    try:
        p = subprocess.run(
            ["dbus-send", "--system", "--print-reply",
             "--dest=org.mpris.MediaPlayer2.ShairportSync",
             "/org/mpris/MediaPlayer2",
             "org.freedesktop.DBus.Properties.Get",
             "string:org.mpris.MediaPlayer2.Player",
             "string:PlaybackStatus"],
            capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    m = re.search(r'variant\s+string\s+"?([A-Za-z]+)"?', p.stdout or "")
    return m.group(1).lower() if m else None


class SessionWatch:
    """Follow shairport's log to know whether a session is really playing.

    MPRIS cannot be trusted here: during an AirPlay 2 *group member* session
    (buffered audio) it kept reporting "Stopped" while audio was playing, and
    the metadata pipe can stay empty because the sender talks to this receiver
    on a non-principal connection. The log is the only signal that matches
    reality, and reading only the bytes appended since the last poll is cheap.

    It also counts "clock change" lines per session: a mixed Apple + Sonos
    group flips the PTP grandmaster every few seconds, and shairport's session
    state / NQPTP mapping does not always survive that. The count lets the
    watchdog reset the receiver between such sessions instead of leaving a
    half-broken state behind (which made the *next* group join fail).
    """

    def __init__(self):
        self.pos = None
        self.live = False
        self.started = 0.0
        self.clocks = 0
        self.stopped = 0.0
        self.last_reset = 0.0
        self.lock = threading.Lock()

    def poll(self):
        with self.lock:
            try:
                size = os.path.getsize(LOG_PATH)
            except Exception:
                return self.live
            if self.pos is None:
                self.pos = size
                return self.live
            if size < self.pos:          # rotated
                self.pos = 0
            if size == self.pos:
                return self.live
            try:
                with open(LOG_PATH, "rb") as f:
                    f.seek(self.pos)
                    chunk = f.read(size - self.pos)
                self.pos = size
            except Exception:
                return self.live
            for line in chunk.decode("utf-8", "replace").splitlines():
                if "clock change from:" in line:
                    self.clocks += 1
                elif ("player_play." in line or "RECORD (AP2)" in line
                      or "Start playing, with anchor clock" in line):
                    if not self.live:
                        self.live = True
                        self.started = time.time()
                        self.clocks = 0
                elif ("player_stop successful" in line or "ffmpeg flush at stop" in line):
                    if self.live:
                        self.live = False
                        self.stopped = time.time()
            return self.live

    def duration(self):
        end = self.stopped if not self.live else time.time()
        return max(0.0, end - self.started) if self.started else 0.0


SW = SessionWatch()


def session_live(force=False):
    """True while a sender has an open, playing session on this receiver."""
    return SW.poll()


# Sonos PTP: in a mixed Apple + Sonos AirPlay 2 group the *anchor* clock named
# by SETRATEANCHORI is usually a Sonos speaker, and its PTP time base is its own
# uptime, not Apple's. NQPTP is told (by shairport) to monitor only the sender's
# IP, so when the group clock switches to the Sonos the published offset is
# taken from the sender's packets and is off by hours -- shairport then sees a
# lead time of ~350000 s for every frame and simply never plays them (silence,
# while iOS still shows this member as selected). Teaching NQPTP to monitor the
# Sonos speakers as well makes the offset it publishes belong to the same clock
# the sender is using as its anchor.
SONOS_OUIS = {
    "94:9f:3e", "48:a6:b8", "5c:aa:fd", "78:28:ca",
    "b8:e9:37", "d8:a8:11", "00:0e:58", "34:7e:5c",
}
SONOS = {"t": 0.0, "ips": []}


def sender_ip():
    """IPv4 address of the RTSP client that currently has a session open."""
    try:
        with open("/proc/net/tcp") as fh:
            for line in fh.readlines()[1:]:
                f = line.split()
                if len(f) < 4 or f[3] != "01":      # 01 == ESTABLISHED
                    continue
                if f[1].endswith(":1B58"):          # local port 7000
                    h = f[2].split(":")[0]
                    b = [int(h[i:i + 2], 16) for i in (6, 4, 2, 0)]
                    return ".".join(str(x) for x in b)
    except Exception:
        pass
    return None


def sonos_ips():
    """IPv4 addresses of Sonos speakers on the LAN (from the neighbour table)."""
    now = time.time()
    if now - SONOS["t"] < 300:
        return SONOS["ips"]
    ips = []
    try:
        out = subprocess.run(["ip", "neigh"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            f = line.split()
            if len(f) >= 5 and f[3] == "lladdr" and f[0].count(".") == 3:
                if f[4].lower()[:8] in SONOS_OUIS:
                    ips.append(f[0])
    except Exception as e:
        diag("sonos peer discovery failed: %r" % (e,))
    ips = sorted(set(ips))
    if ips != SONOS["ips"]:
        diag("sonos ptp peers: %s" % (",".join(ips) or "none"))
    SONOS.update({"t": now, "ips": ips})
    return ips


def nqptp_set_peers(ips):
    """Replace the client's PTP timing peer group (NQPTP control port 9000)."""
    smi = (CFG.get("name") or "AirPlay").replace(" ", "-")
    msg = ("/nqptp-%s T %s" % (smi, " ".join(ips))).encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(msg, ("127.0.0.1", 9000))
        diag("nqptp timing peers set to: %s" % " ".join(ips))
    except Exception as e:
        diag("nqptp peer update failed: %r" % (e,))
    finally:
        s.close()


def _anchor_peers(cid):
    """Peer list for the clock the sender named as the group's anchor."""
    if not cid:
        return []
    mac = ":".join(cid[i:i + 2] for i in range(0, 6, 2))
    if mac in SONOS_OUIS:
        return sonos_ips()
    # The anchor is the sender's own clock: shairport already registered that
    # IP in NQPTP with its own "T" message during SETUP. Sending ours again
    # would clear the clock table and cost ~400 ms of invalid timing right at
    # the start of playback -- audible as a late start. Leave it alone.
    return []


def ptp_anchor_watch():
    """Keep NQPTP pointed at exactly the clock the sender names as its anchor.

    Derived from the airplay-esp32 reference implementation
    (https://github.com/Kristian8606/airplay-esp32): "speakers never talk to
    each other", timing comes only from the sender/anchor, and a clock is only
    adopted once it has been stable for a while (its ``handover_ready`` rule:
    new master locked + old anchor unused for 5 s). Two behaviours follow:

    * the peer group is switched only after the same anchor has been named for
      a second or more and at least 5 s have passed since the previous switch,
      so a sender that re-announces its anchor in bursts cannot make NQPTP
      thrash its clock table;
    * if shairport computes an absurd lead time (the signature of a clock whose
      PTP time base does not match the anchor -- observed at 353539 s), the peer
      group is re-pointed immediately instead of leaving the session silent
      forever, like the reference refusing to convert with a mismatched id.
    """
    pos = None
    last_key = None
    last_t = 0.0
    anchor_cid = None
    want = None
    last_bad = 0.0
    while True:
        try:
            size = os.path.getsize(LOG_PATH)
            if pos is None:
                pos = size
            elif size < pos:
                pos = 0
            if size > pos:
                with open(LOG_PATH, "rb") as f:
                    f.seek(pos)
                    chunk = f.read(size - pos)
                pos = size
                for line in chunk.decode("utf-8", "replace").splitlines():
                    m = re.search(r"anchor clock ([0-9a-fA-F]{16})", line)
                    if m:
                        anchor_cid = m.group(1).lower()
                        want = None
                    lt = re.search(r"lead time for first frame \d+: ([0-9.]+) seconds", line)
                    if lt and float(lt.group(1)) > 5.0:
                        # Do NOT re-point NQPTP here. Every "T" clears the clock
                        # table and resets mastership for ~400 ms, which makes a
                        # *healthy* parallel stream resync over and over (observed:
                        # a "reset first_packet_timestamp / sync error out of
                        # bounds" loop every ~20 ms). Some senders open several
                        # streams at once and one of them can carry a clock whose
                        # time base does not match; only report it.
                        if time.time() - last_bad > 60.0:
                            last_bad = time.time()
                            diag("clock mismatch seen (lead time %.0fs) - left untouched"
                                 % float(lt.group(1)))
            if anchor_cid:
                ips = _anchor_peers(anchor_cid)
                key = tuple(ips) if ips else None
                if key and key != last_key:
                    if want is None or want[0] != key:
                        want = (key, ips, time.time())
                    elif time.time() - want[2] >= 0.5 and time.time() - last_t >= 5.0:
                        nqptp_set_peers(ips)
                        last_key = key
                        last_t = time.time()
                        want = None
        except Exception as e:
            diag("ptp anchor watch: %r" % (e,))
        time.sleep(0.4)


def metadata_reader():
    """Read the shairport-sync metadata FIFO forever."""
    item_re = re.compile(r"<item>.*?</item>", re.S)
    while True:
        try:
            if not os.path.exists(META_PIPE):
                os.makedirs(DATA, exist_ok=True)
                os.mkfifo(META_PIPE)
            fd = os.open(META_PIPE, os.O_RDONLY)
            buf = b""
            with os.fdopen(fd, "rb", buffering=0) as f:
                while True:
                    chunk = f.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"</item>" in buf:
                        idx = buf.index(b"</item>") + 7
                        raw, buf = buf[:idx], buf[idx:]
                        handle_item(raw)
                    if len(buf) > 1_000_000:
                        buf = b""
        except Exception as e:
            diag("metadata reader error: %r" % (e,))
            time.sleep(2)


def handle_item(raw):
    try:
        txt = raw.decode("utf-8", "replace")
    except Exception:
        return
    tm = re.search(r"<type>([0-9a-fA-F]+)</type>", txt)
    cm = re.search(r"<code>([0-9a-fA-F]+)</code>", txt)
    if not (tm and cm):
        return
    try:
        typ = bytes.fromhex(tm.group(1)).decode("ascii", "replace")
        code = bytes.fromhex(cm.group(1)).decode("ascii", "replace")
    except Exception:
        return
    # Keep a compact trace of what real sources actually send, so a missing
    # now-playing display can be diagnosed instead of guessed at.
    try:
        preview = re.sub(r"\s+", " ", txt)[:220]
        line = "%s %s/%s %s\n" % (time.strftime("%H:%M:%S"), typ, code, preview)
        p = os.path.join(DATA, "metadata-raw.log")
        if os.path.exists(p) and os.path.getsize(p) > 1_000_000:
            os.replace(p, p + ".1")
        with open(p, "a") as f:
            f.write(line)
    except Exception:
        pass
    dm = re.search(r"<data([^>]*)>(.*?)</data>", txt, re.S)
    data = b""
    if dm:
        attrs, payload = dm.group(1), dm.group(2)
        if "base64" in attrs:
            try:
                data = base64.b64decode(payload.strip())
            except Exception:
                data = b""
        else:
            data = payload.encode("utf-8", "replace")
    if code == "pict":
        # cover art can arrive in several chunks; accumulate then commit
        with NP.lock:
            buf = getattr(NP, "_artbuf", b"") + data
            NP._artbuf = buf
            if buf.startswith(b"\xff\xd8") and buf.rstrip(b"\x00").endswith(b"\xff\xd9"):
                commit_art_locked(NP, buf)
            elif buf.startswith(b"\x89PNG") and b"IEND" in buf[-16:]:
                commit_art_locked(NP, buf)
        return
    with NP.lock:
        pending = getattr(NP, "_artbuf", b"")
        if pending:
            commit_art_locked(NP, pending)
    NP.apply(typ, code, data)


def commit_art_locked(np, buf):
    np._artbuf = b""
    if len(buf) < 128:
        return
    try:
        with open(ART_PATH, "wb") as f:
            f.write(buf)
        np.info["art_version"] += 1
    except Exception as e:
        diag("art save failed: %r" % (e,))
# --------------------------------------------------------------------------
# configuration + supervisor
# --------------------------------------------------------------------------
def _esc(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def lan_ipv4():
    """IPv4 address of the interface that carries the default route.

    Used to pin the RTSP socket to IPv4 (see write_config). A UDP connect()
    only selects a route -- it does not put a packet on the wire.
    """
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("192.0.2.1", 9))  # TEST-NET-1, black hole
        ip = s.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
    return None


def write_config():
    snap = CFG.snapshot()
    dev = snap.get("device") or default_device()
    if snap.get("device") != dev:
        CFG.update({"device": dev})
        snap = CFG.snapshot()
    lines = [
        "// generated by the Zima AirPlay panel -- edits are overwritten",
        "general =",
        "{",
        '\tname = "%s";' % _esc(snap["name"]),
        '\tservice_type = "%s";' % _esc(snap.get("service_type") or "auto"),
        '\toutput_backend = "alsa";',
        '\tinterpolation = "%s";' % _esc(snap.get("interpolation") or "auto"),
        "\tvolume_range_db = %d;" % int(snap.get("volume_range_db") or 60),
        "\taudio_backend_latency_offset_in_seconds = %.3f;" % (int(snap.get("latency_offset_ms") or 0) / 1000.0),
        "\taudio_backend_buffer_desired_length_in_seconds = 1.0;",
        # A mixed Apple + Sonos AirPlay 2 group has two PTP clocks claiming the
        # grandmaster role; NQPTP follows whichever spoke last, so "clock change"
        # fires every few seconds. Each change invalidates NQPTP's timing for
        # about 400 ms (a new mastership has to settle). With a 0.4 s output
        # buffer that window underruns the DAC and the audio stops until the
        # sender gives up on this member -- the "works at random with the Sonos
        # group" symptom. The buffers below are one second (output) and two
        # seconds (decoded) so a clock change is absorbed instead of heard.
        "\taudio_decoded_buffer_desired_length_in_seconds = 1.0;",
        # AirPlay 2 groups whose clock master flips between members (e.g. a
        # Sonos and an Apple device) used to force a hard resync on every flip
        # and cut the audio. Tolerate a wider error before resyncing.
        "\tresync_threshold_in_seconds = 0.25;",
        "\tdrift_tolerance_in_seconds = 0.005;",
    ]
    # Bind RTSP to the LAN IPv4 address only. Otherwise an iPhone connects to
    # the host's global IPv6 (2605:...) -- a path that runs through the router
    # and periodically dies mid-session, which is why the same song stops after
    # a few seconds at random. IPv4 is on-link and stable, and the service is
    # advertised over IPv4 only (avahi use-ipv6=no), so the sender falls back
    # to it as soon as the IPv6 attempt is refused.
    bind = snap.get("bind_address") or lan_ipv4()
    if bind:
        lines.insert(3, '\taddress = "%s";' % _esc(bind))
    if snap.get("stable_44k", True):
        # An iPhone sends 48k AAC in AirPlay 2, and a group (iPhone + other
        # AirPlay 2 speakers) is negotiated at 48k. Advertising "44.1k only"
        # makes the sender accept this receiver, then drop it the moment the
        # group is formed -- playback stops with an error on the phone.
        # So keep every input format (44.1k included) reachable and pin only
        # the *output* side to one fixed rate, see the alsa section below.
        lines += [
            "\tlossless_mode = \"off\";",
            "\tsix_channel_mode = \"off\";",
            "\teight_channel_mode = \"off\";",
        ]
    if int(snap.get("port") or 0) > 0:
        lines.append("\tport = %d;" % int(snap["port"]))
    if snap.get("password"):
        lines.append('\tpassword = "%s";' % _esc(snap["password"]))
    lines += [
        "};",
        "",
        "alsa =",
        "{",
        '\toutput_device = "%s";' % _esc(dev),
    ]
    if snap.get("stable_44k", True):
        # One fixed rate/format keeps the HDMI PCM at a single rate, so a
        # session start or a group join never re-negotiates the HDMI link.
        # 48000 is what the sink (TV) and every AirPlay 2 sender agree on;
        # 44.1k input is resampled internally.
        lines += [
            "\toutput_rate = 48000;",
            '\toutput_format = "S16_LE";',
            "\toutput_channels = 2;",
        ]
    else:
        lines += [
            '\toutput_rate = "auto";',
            '\toutput_format = "auto";',
            '\toutput_channels = "auto";',
        ]
    lines += [
        '\tdisable_standby_mode = "auto";',
    ]
    if VOL.mode == "amixer" and VOL.mixer:
        lines.append('\tmixer_control_name = "%s";' % _esc(VOL.mixer["control"]))
    lines += [
        "};",
        "",
        "metadata =",
        "{",
        '\tenabled = "yes";',
        '\tinclude_cover_art = "yes";',
        '\tpipe_name = "%s";' % META_PIPE,
        "};",
        "",
        "sessioncontrol =",
        "{",
        "\tactive_state_timeout = 10.0;",
        "};",
        "",
    ]
    with open(SS_CONF, "w") as f:
        f.write("\n".join(lines) + "\n")
    diag("config written (device=%s, volume_mode=%s)" % (dev, VOL.mode))


class Supervisor:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.started_at = 0
        self.wanted = True
        self._fails = 0
        self._reader_started = False

    def _ensure_pipe(self):
        os.makedirs(DATA, exist_ok=True)
        if not os.path.exists(META_PIPE):
            try:
                os.mkfifo(META_PIPE)
            except FileExistsError:
                pass
        if not self._reader_started:
            threading.Thread(target=metadata_reader, daemon=True).start()
            self._reader_started = True

    def reap_strays(self):
        """Kill shairport-sync processes this panel does not own.

        A second instance cannot bind RTSP port 7000, but it does publish a
        duplicate mDNS service and pushes its own timing requests into NQPTP,
        which corrupts the PTP clock table. The sender then sees two receivers
        with the same name and drops both out of an AirPlay 2 group. Leftovers
        can appear after a manual debugging run, so reap them defensively.
        """
        mine = self.proc.pid if (self.proc and self.proc.poll() is None) else None
        killed = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid in (mine, os.getpid()):
                continue
            try:
                with open("/proc/%d/cmdline" % pid, "rb") as f:
                    cmd = f.read().decode("utf-8", "replace")
            except Exception:
                continue
            if "shairport-sync" not in cmd or "main.py" in cmd:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
                killed.append(pid)
            except Exception:
                pass
        if killed:
            diag("reaped stray shairport-sync processes: %s" % killed)
            time.sleep(0.5)
        return killed

    def start(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return False, "already running"
            self._ensure_pipe()
            self.reap_strays()
            write_config()
            try:
                if os.path.getsize(LOG_PATH) > 1_000_000:
                    os.replace(LOG_PATH, LOG_PATH + ".1")
            except Exception:
                pass
            log = open(LOG_PATH, "ab", buffering=0)
            try:
                self.proc = subprocess.Popen(
                    [SS_BIN, "-c", SS_CONF] + (["-vv"] if CFG.get("verbose") else []),
                    stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except Exception as e:
                diag("spawn failed: %r" % (e,))
                return False, "spawn failed: %s" % e
            # AirPlay 2 group timing is PTP-driven: without realtime scheduling
            # the audio and PTP threads get preempted, the sender rejects a
            # group join and an established group drops out after a few
            # seconds. rtprio falls back to the raw syscall because musl's
            # sched_setscheduler() wrapper returns ENOSYS in this image.
            ok, how = rtprio.promote_threads(self.proc.pid, 20)
            diag("shairport-sync realtime promotion: %s (%s)" % ("ok" if ok else "FAILED", how))
            self.started_at = time.time()
            self.wanted = True
            diag("shairport-sync started pid=%s" % self.proc.pid)
        apply_volume_async()
        return True, "started"

    def _apply_volume(self):
        apply_volume_async()

    def stop(self, wanted=False):
        with self.lock:
            self.wanted = wanted
            p, self.proc = self.proc, None
        if not p or p.poll() is not None:
            return True, "not running"
        try:
            p.terminate()
            for _ in range(30):
                if p.poll() is not None:
                    break
                time.sleep(0.1)
            else:
                p.kill()
            diag("shairport-sync stopped")
            return True, "stopped"
        except Exception as e:
            return False, str(e)

    def restart(self):
        self.stop(wanted=True)
        time.sleep(0.4)
        self.wanted = True
        return self.start()

    def running(self):
        with self.lock:
            return bool(self.proc and self.proc.poll() is None)

    def pid(self):
        with self.lock:
            return self.proc.pid if self.proc and self.proc.poll() is None else None

    def policy(self):
        pid = self.pid()
        return rtprio.threads_policy(pid) if pid else "not running"

    def ensure_realtime(self):
        """Re-apply SCHED_FIFO if the process lost it (or never got it)."""
        pid = self.pid()
        if not pid:
            return
        if rtprio.threads_policy(pid) == "SCHED_FIFO":
            return
        ok, how = rtprio.promote_threads(pid, 20)
        diag("watchdog: re-applied realtime to shairport-sync: %s (%s)"
             % ("ok" if ok else "FAILED", how))

    def ensure(self):
        """Watchdog: restart if it died unexpectedly."""
        if not self.wanted:
            return
        with self.lock:
            alive = bool(self.proc and self.proc.poll() is None)
        if alive:
            return
        if CFG.get("autostart") is False:
            return
        self._fails += 1
        delay = 2 if self._fails < 5 else 15
        diag("watchdog: not running, restart in %ss (fail #%d)" % (delay, self._fails))
        time.sleep(delay)
        self.start()


SUP = Supervisor()


def proc_state(pid):
    """Single-letter process state from /proc (Z = zombie)."""
    try:
        with open("/proc/%d/stat" % pid, "r", errors="replace") as f:
            s = f.read()
        return s[s.rindex(")") + 2:].split()[0]
    except Exception:
        return "?"


def proc_pid_by_name(name):
    """Live pid of `name`, ignoring (and reaping) zombies.

    start.sh spawns nqptp before the panel becomes PID 1, so an nqptp that
    exits stays as a zombie child of main.py. A zombie still reports its
    comm as "nqptp", which used to make the watchdog believe the AirPlay 2
    clock was present and never restart it.
    """
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open("/proc/%s/comm" % entry, "r", errors="replace") as f:
                if f.read().strip() != name:
                    continue
        except Exception:
            continue
        if proc_state(pid) == "Z":
            try:
                os.waitpid(pid, os.WNOHANG)
            except Exception:
                pass
            continue
        return pid
    return None


NQPTP_BIN = shutil.which("nqptp") or "/usr/local/bin/nqptp"
NQPTP_LOG = os.path.join(DATA, "nqptp.log")
NQPTP_RETRY = {"t": 0.0}


def nqptp_status():
    """NQPTP is the AirPlay 2 clock; without it a group cannot be joined."""
    pid = proc_pid_by_name("nqptp")
    if not pid:
        return {"running": False, "pid": None, "sched": "not running"}
    return {"running": True, "pid": pid, "sched": rtprio.policy_of(pid)}


def start_nqptp():
    """Start NQPTP when the watchdog finds it gone (start.sh starts the first).

    Only ever called when no nqptp process exists: a second daemon would fight
    for UDP 319/320 and leave the clock table invalid, which is worse than an
    absent one (playback keeps working until the next group join).
    """
    if os.environ.get("ENABLE_NQPTP", "1") == "0":
        return None
    try:
        log = open(NQPTP_LOG, "ab", buffering=0)
    except Exception:
        log = subprocess.DEVNULL
    try:
        flags = ["-vvv"] if CFG.get("verbose") else ["-v"]
        p = subprocess.Popen([NQPTP_BIN] + flags, stdout=log,
                             stderr=subprocess.STDOUT, start_new_session=True)
        time.sleep(0.6)
        if p.poll() is not None:
            reason = " | ".join(l.strip() for l in tail(NQPTP_LOG, 4).splitlines() if l.strip())
            diag("watchdog: nqptp exited immediately (rc=%s): %s" % (p.returncode, reason[-400:]))
            return None
        return p.pid
    except Exception as e:
        diag("watchdog: nqptp start failed: %r" % (e,))
        return None


def tail(path, n=120):
    try:
        with open(path, "r", errors="replace") as f:
            return "".join(deque(f, maxlen=n))
    except Exception:
        return ""


# --------------------------------------------------------------------------
# device test tone
# --------------------------------------------------------------------------
TEST_WAV = os.path.join(DATA, "test-tone.wav")


def make_test_wav(path=TEST_WAV, seconds=2.0, rate=44100, freq=440.0):
    import math
    import struct
    import wave
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    n = int(rate * seconds)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            # gentle fade in/out so nothing pops
            env = min(1.0, i / (rate * 0.05), (n - i) / (rate * 0.05))
            v = int(12000 * env * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<hh", v, v)
        w.writeframes(bytes(frames))
    return path


def test_device(device, seconds=2):
    """Play a short tone on one PCM and report exactly what ALSA said."""
    if not device:
        return {"ok": False, "error": "no device selected"}
    path = make_test_wav()
    t0 = time.time()
    try:
        p = subprocess.run(["aplay", "-q", "-D", device, path],
                           capture_output=True, text=True, timeout=max(8, seconds + 8))
        err = (p.stderr or "").strip()
        return {
            "ok": p.returncode == 0,
            "device": device,
            "rc": p.returncode,
            "stderr": err[-800:],
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "device": device, "error": "timeout while playing"}
    except Exception as e:
        return {"ok": False, "device": device, "error": repr(e)}


TEST_ALL = {"running": False, "results": [], "started": 0}
# A deferred service restart: config is saved now, applied when playback stops,
# so tweaking delay/latency never kills a live (possibly grouped) session.
PENDING = {"restart": False}


def test_all_devices():
    TEST_ALL.update({"running": True, "results": [], "started": time.time()})
    try:
        was_running = SUP.running()
        if was_running:
            SUP.stop(wanted=True)  # free the device
            time.sleep(1.0)
        for d in list_devices():
            r = test_device(d["value"])
            r["label"] = d["label"]
            r["connected"] = d.get("connected")
            TEST_ALL["results"].append(r)
        if was_running:
            SUP.start()
    finally:
        TEST_ALL["running"] = False


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "ZimaAirPlay/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8", "replace") or "{}")
        except Exception:
            return {}

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            try:
                with open(os.path.join(APP, "static", "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                return self._send(500, {"error": "ui missing: %r" % (e,)})
        if p == "/api/state":
            return self._send(200, build_state())
        if p == "/api/logs":
            return self._send(200, {"log": tail(LOG_PATH, 200), "diag": tail(DIAG_PATH, 80),
                                    "raw": tail(os.path.join(DATA, "metadata-raw.log"), 80)})
        if p == "/api/artwork":
            try:
                with open(ART_PATH, "rb") as f:
                    data = f.read()
            except Exception:
                return self._send(404, {"error": "no artwork"})
            ctype = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
            return self._send(200, data, ctype, {"Cache-Control": "max-age=5"})
        if p == "/api/test-all":
            return self._send(200, dict(TEST_ALL, results=list(TEST_ALL["results"])))
        if p == "/api/health":
            return self._send(200, {"ok": True, "running": SUP.running()})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        body = self._json_body()
        if p == "/api/config":
            patch = {}
            if "name" in body:
                name = str(body["name"]).strip()[:50]
                if not name:
                    return self._send(400, {"error": "name required"})
                patch["name"] = name
            if "device" in body:
                devs = [d["value"] for d in list_devices()]
                if body["device"] not in devs and body["device"] != CFG.get("device"):
                    return self._send(400, {"error": "unknown device"})
                patch["device"] = body["device"]
            if "password" in body:
                patch["password"] = str(body["password"])[:60]
            if "service_type" in body and body["service_type"] in ("auto", "airplay2", "classic"):
                patch["service_type"] = body["service_type"]
            if "volume_range_db" in body:
                try:
                    patch["volume_range_db"] = max(30, min(150, int(body["volume_range_db"])))
                except Exception:
                    pass
            if "stable_44k" in body:
                patch["stable_44k"] = bool(body["stable_44k"])
            if "verbose" in body:
                patch["verbose"] = bool(body["verbose"])
            if "latency_offset_ms" in body:
                try:
                    new_lat = max(-1000, min(1000, int(round(float(body["latency_offset_ms"])))))
                except Exception:
                    new_lat = None
                if new_lat is not None:
                    live = session_live()
                    if new_lat != CFG.get("latency_offset_ms") and live and not body.get("force"):
                        # Applying needs a restart, which tears the session (and
                        # any group) down. Require an explicit confirmation so
                        # the panel can warn the user first.
                        return self._send(409, {"error": "برای اعمال، پخش فعلی قطع میشود و باید دوباره Play بزنید.",
                                                "needs_confirm": True})
                    patch["latency_offset_ms"] = new_lat
            need_restart = bool({"name", "device", "password", "service_type",
                                 "volume_range_db", "latency_offset_ms",
                                 "stable_44k", "verbose"} & set(patch))
            CFG.update(patch)
            if "volume" in body:
                set_volume(body["volume"])
            if need_restart:
                write_config()   # persist now; applied by the next (re)start
                soft = {"latency_offset_ms", "volume_range_db", "stable_44k", "verbose"}
                active = session_live()
                if active and not body.get("force") and set(patch) <= soft:
                    # Never tear down a live session (in a group that drops the
                    # whole group). Defer the restart until playback idles.
                    PENDING["restart"] = True
                    return self._send(200, {"ok": True, "pending": True,
                                            "state": build_state()})
                PENDING["restart"] = False
                ok, msg = SUP.restart()
                return self._send(200, {"ok": ok, "restarted": ok, "msg": msg,
                                        "state": build_state()})
            return self._send(200, {"ok": True, "state": build_state()})
        if p == "/api/volume":
            return self._send(200, set_volume(body.get("volume")))
        if p == "/api/action":
            act = body.get("action")
            if act in ("restart", "apply-now"):
                PENDING["restart"] = False
                ok, msg = SUP.restart()
            elif act == "stop":
                ok, msg = SUP.stop()
            elif act == "start":
                ok, msg = SUP.start()
            elif act in ("play", "pause", "playpause", "next", "previous", "stop-play"):
                ok, msg = remote_control({"play": "Play", "pause": "Pause",
                                          "playpause": "PlayPause", "next": "Next",
                                          "previous": "Previous",
                                          "stop-play": "Stop"}[act])
                return self._send(200, {"ok": ok, "msg": msg, "state": build_state()})
            elif act == "test-device":
                ok, msg = True, test_device(body.get("device") or CFG.get("device"))
                return self._send(200, {"ok": ok, "result": msg})
            elif act == "test-all":
                if not TEST_ALL["running"]:
                    threading.Thread(target=test_all_devices, daemon=True).start()
                return self._send(200, {"ok": True, "started": True})
            else:
                return self._send(400, {"error": "unknown action"})
            return self._send(200, {"ok": ok, "msg": msg, "state": build_state()})
        return self._send(404, {"error": "not found"})


def remote_control(method):
    """Send a transport command (Play/Pause/Next...) to the active session."""
    if not shutil.which("dbus-send"):
        return False, "dbus-send missing"
    cmd = ["dbus-send", "--system", "--print-reply",
           "--dest=org.gnome.ShairportSync", "/org/gnome/ShairportSync",
           "org.gnome.ShairportSync.RemoteControl.%s" % method]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
        if p.returncode == 0:
            diag("remote %s ok" % method)
            return True, method
        err = (p.stderr or "failed").strip()
        diag("remote %s failed: %s" % (method, err[:200]))
        return False, err[:200]
    except Exception as e:
        return False, repr(e)


def set_volume(v):
    try:
        pct = max(0, min(100, int(round(float(v)))))
    except Exception:
        return {"ok": False, "error": "bad volume"}
    CFG.update({"volume": pct})
    ok, how = VOL.set_pct(pct)
    return {"ok": ok, "volume": pct, "method": how, "mode": VOL.mode}


def build_state():
    np = NP.snapshot()
    dev = CFG.get("device")
    devs = list_devices()
    dmap = display_map(devs)
    for d in devs:
        if d["device"] in dmap:
            d["connected"] = True
            d["display"] = dmap[d["device"]]
    cur = next((d for d in devs if d["value"] == dev), None)
    # Prefer the volume the live AirPlay session reports (pvol) over the D-Bus
    # property, which keeps its own stale value and made the panel lie.
    vol = VOL.get_pct()
    apdb = np.get("ap_volume_db")
    floor = np.get("ap_volume_floor") or -30.0
    if apdb is not None:
        vol = 0 if apdb <= -100 else int(round(max(0.0, min(100.0, 100.0 * (1.0 - apdb / floor)))))
    meta_state = "ok" if (np.get("title") or np.get("art_version")) else (
        "source-silent" if np.get("state") == "playing" else "idle")
    return {
        "config": CFG.snapshot(),
        "server": {
            "running": SUP.running(),
            "pid": SUP.pid(),
            "uptime": int(time.time() - SUP.started_at) if SUP.running() else 0,
            "volume_mode": VOL.mode,
            "volume_note": VOL.note,
            "sched": SUP.policy(),
            "realtime": SUP.policy() == "SCHED_FIFO",
        },
        "nqptp": nqptp_status(),
        "volume": {"app": CFG.get("volume"), "reported": vol},
        "metadata_state": meta_state,
        "pending_restart": PENDING["restart"],
        "session_live": session_live(),
        "now_playing": np,
        "devices": devs,
        "current_device": cur,
        "displays": sorted(set(dmap.values())),
        "hdmi_sink": bool(dmap),
        "test": {"running": TEST_ALL["running"], "results": TEST_ALL["results"]},
    }


def main():
    os.makedirs(DATA, exist_ok=True)
    make_test_wav()
    VOL.detect()
    if not CFG.get("device"):
        CFG.update({"device": default_device()})
    SUP.start()

    def watchdog():
        while True:
            time.sleep(5)
            try:
                live = session_live(force=True)
                if PENDING["restart"] and not live:
                    diag("deferred restart: applying saved settings")
                    PENDING["restart"] = False
                    SUP.restart()
                elif (not live and not PENDING["restart"] and SW.stopped
                      and SW.clocks >= 8 and SW.duration() >= 20
                      and time.time() - SW.stopped > 60
                      and time.time() - SW.last_reset > 600):
                    # A mixed Apple + Sonos group flips the PTP grandmaster
                    # several times per session. Even when audio survives it,
                    # shairport's PTP anchor / pairing state is left half
                    # switched and the *next* group join fails ("only the
                    # first attempt works"). Start from a clean process while
                    # nothing is playing, so every attempt looks like a first.
                    diag("group session ended after %d clock changes in %ds; "
                         "restarting shairport-sync to clear PTP/session state"
                         % (SW.clocks, int(SW.duration())))
                    SW.last_reset = time.time()
                    SW.stopped = 0.0
                    SW.started = 0.0
                    SW.clocks = 0
                    SUP.restart()
                elif (not live and not PENDING["restart"] and SW.stopped
                      and SW.duration() < 5
                      and time.time() - SW.stopped > 10
                      and time.time() - SW.last_reset > 120):
                    # A sender that starts a group session and tears it down
                    # again within a second (shairport logs player_play followed
                    # by player_stop, without ever receiving an anchor) leaves
                    # the receiver with a half-open session; the next attempt
                    # then fails too. Clear it while idle so the next attempt
                    # starts from a known state.
                    diag("aborted session after %.1fs; restarting shairport-sync"
                         % SW.duration())
                    SW.last_reset = time.time()
                    SW.stopped = 0.0
                    SW.started = 0.0
                    SW.clocks = 0
                    SUP.restart()
                SUP.ensure()
                SUP.ensure_realtime()
                if not nqptp_status()["running"] and time.time() - NQPTP_RETRY["t"] > 30:
                    NQPTP_RETRY["t"] = time.time()
                    npid = start_nqptp()
                    if npid:
                        diag("watchdog: nqptp restarted pid=%s" % npid)
                        # The new daemon publishes a fresh shared-memory clock
                        # segment; shairport-sync still maps the old one, so it
                        # has to be restarted once the pipe is idle.
                        PENDING["restart"] = True
            except Exception as e:
                diag("watchdog error: %r" % (e,))
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=ptp_anchor_watch, daemon=True).start()

    srv = ThreadingHTTPServer(("0.0.0.0", PANEL_PORT), Handler)
    srv.daemon_threads = True
    print("panel listening on %d" % PANEL_PORT, flush=True)
    diag("panel listening on %d" % PANEL_PORT)
    srv.serve_forever()


if __name__ == "__main__":
    main()