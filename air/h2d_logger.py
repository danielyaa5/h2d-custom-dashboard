#!/usr/bin/env python3
"""Always-on, read-only status logger for the Bambu Lab H2D. Runs as a background service or in a terminal.

Connects to the printer's own MQTT broker (TLS, port 8883, user "bblp", password = the access code in config.json),
subscribes to device/<serial>/report and writes one line every EVERY seconds to logs/status-YYYY-MM-DD.csv. Files older
than KEEP_DAYS days are deleted. The only thing it sends to the printer besides the subscription is "pushall" (asks for
a full status report) after each connect; it sends nothing that changes the printer.

A web dashboard, status API and dated CSV downloads are served on port 8791. The web interface also controls an
optional Smart Life exhaust fan. Standard library only for logging and the dashboard (minimal MQTT 3.1.1 client).

It also runs the exhaust fan for ABS, ASA and PETG prints (FanWatch, user 2026-10-14, 2026-10-18, 2026-10-19):
during a job that uses ABS, ASA or PETG, once the printer is actually printing or the chamber has reached its target
(so the fan doesn't slow the warm-up), it runs `fan.py on` (at speed 5 in fan.json); during post-print air purification
(stage 66) and for 15 minutes after the print it runs at full blast (speed 10), and then `fan.py off`. fan.py runs in
its own Python (fan-venv/) because Tuya's library is not standard library; without fan.py, fan.json and fan-venv/ this
part stays off. Nothing is sent to the printer for this.
"""
import datetime
import glob
import http.server
import json
import os
import posixpath
import re
import socket
import ssl
import subprocess
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(HERE, "logs")
EVERY = 10          # seconds between log lines
KEEP_DAYS = 31      # about one month (user, 2026-10-11)
PING = 30           # seconds between keep-alive pings
STALE = 120         # reconnect after this long with nothing from the printer
HTTP_PORT = 8791    # 8768 was taken on the Air
FAN_FILAMENTS = re.compile(r"(ABS|ASA|PETG)\b", re.I)    # ABS, ABS-GF, ASA, ASA-CF, ASA-Aero, PETG, PETG-CF, PETG-HF; not PLA, HIPS ...
ACTIVE = ("PREPARE", "RUNNING", "PAUSE")              # gcode_state while a job is on the printer
PURGE_SECONDS = 15 * 60                               # 15 minutes at full blast after PETG, ABS, ASA prints (user, 2026-10-19)
FAN_RETRY = (60, 900)       # seconds before retrying a failed fan command: first, then doubling up to the second
FAN_CHECK = 12 * 3600       # with nothing to do, check the fan this often (keeps the Smart Life sign-in fresh)
FAN = None                  # the FanWatch main() sets up, if the fan is set up on this Mac
CURRENT_STATE = {}          # latest complete status from the printer
STATE_LOCK = threading.Lock()
LAST_SEEN = {"time": 0.0}
DEFAULT_SETTINGS = {
    "print_speed": 5,
    "filaments": ["ABS", "ASA", "PETG"],
    "post_print_duration_minutes": 15,
    "post_print_smartlife_speed": 10,
    "post_print_chamber_exhaust_percent": 100,
    "post_print_part_fan_percent": 100,
    "post_print_aux_fan_percent": 100,
    "post_print_chamber_circ_percent": 100,
}
HEADER = ("time,state,stage,percent,remaining_min,layer,chamber,chamber_target,bed,bed_target,"
          "nozzle_right,nozzle_right_target,nozzle_left,nozzle_left_target,job\n")


def say(msg):
    print(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


def varlen(n):
    out = bytearray()
    while True:
        d = n % 128
        n //= 128
        out.append(d | 0x80 if n else d)
        if not n:
            return bytes(out)


def mstr(s):
    b = s.encode("utf-8")
    return len(b).to_bytes(2, "big") + b


def packet(kind, body):
    return bytes([kind]) + varlen(len(body)) + body


def merge(into, new):
    """Report messages are partial updates; merge them into one running picture."""
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(into.get(k), dict):
            merge(into[k], v)
        else:
            into[k] = v


def get(state, path):
    cur = state
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
        if cur is None:
            return None
    return cur


def packed(state, path):
    """H2-series temperatures are packed: current in the low 16 bits, target in the high 16."""
    v = get(state, path)
    if not isinstance(v, (int, float)):
        return "", ""
    v = int(v)
    return str(v & 0xFFFF), str((v >> 16) & 0xFFFF)


def txt(state, path):
    v = get(state, path)
    return "" if v is None else str(v).replace(",", " ").replace("\n", " ")


def log_line(state):
    ch, ch_t = packed(state, "print.device.ctc.info.temp")
    bed, bed_t = packed(state, "print.device.bed.info.temp")
    nr, nr_t = packed(state, "print.device.extruder.info.0.temp")
    nl, nl_t = packed(state, "print.device.extruder.info.1.temp")
    now = datetime.datetime.now()
    row = [now.strftime("%Y-%m-%d %H:%M:%S"), txt(state, "print.gcode_state"), txt(state, "print.stg_cur"),
           txt(state, "print.mc_percent"), txt(state, "print.mc_remaining_time"), txt(state, "print.layer_num"),
           ch, ch_t, bed, bed_t, nr, nr_t, nl, nl_t, txt(state, "print.subtask_name")]
    path = os.path.join(LOGS, "status-%s.csv" % now.strftime("%Y-%m-%d"))
    new = not os.path.exists(path)
    with open(path, "a") as f:
        if new:
            f.write(HEADER)
        f.write(",".join(row) + "\n")
    if new:
        cleanup()


def cleanup():
    cutoff = (datetime.date.today() - datetime.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    for p in glob.glob(os.path.join(LOGS, "status-*.csv")):
        m = re.fullmatch(r"status-(\d{4}-\d{2}-\d{2})\.csv", os.path.basename(p))
        if m and m.group(1) < cutoff:    # only our own dated files
            os.remove(p)
            say("deleted old log " + os.path.basename(p))


def slot_types(state):
    """Filament type per slot, numbered as the H2D does in print.mapping: AMS id << 8 | tray (external spools 254, 255)."""
    types = {}

    def add(unit, tray, kind):
        try:
            types[int(unit) << 8 | int(tray)] = str(kind or "")
        except (TypeError, ValueError):
            pass
    for ams in get(state, "print.ams.ams") or []:
        if isinstance(ams, dict):
            for tray in ams.get("tray") or []:
                if isinstance(tray, dict):
                    add(ams.get("id"), tray.get("id"), tray.get("tray_type"))
    for ext in get(state, "print.vir_slot") or []:
        if isinstance(ext, dict):
            add(ext.get("id"), 0, ext.get("tray_type"))
    return types


def job_filaments(state):
    """The filament types the current job uses: its slot mapping, or failing that what each nozzle has loaded now."""
    types = slot_types(state)
    slots = get(state, "print.mapping")
    if not isinstance(slots, list) or not slots:
        slots = [get(state, "print.device.extruder.info.%d.snow" % i) for i in (0, 1)]
    out = set()
    for s in slots:
        try:
            t = types.get(int(s), "")
        except (TypeError, ValueError):
            continue
        if t:
            out.add(t)
    return sorted(out)


def warmed_up(state):
    """Why the fan may come on now, or None while the printer is still warming up (user, 2026-10-14: wait "until actual
    printing starts or for when chamber temp reaches target so as not to slow the warm up")."""
    if get(state, "print.gcode_state") == "RUNNING" and str(get(state, "print.stg_cur")) == "0":
        return "printing"
    v = get(state, "print.device.ctc.info.temp")
    if isinstance(v, (int, float)):
        now, target = int(v) & 0xFFFF, (int(v) >> 16) & 0xFFFF
        if target and now >= target:            # target 0 (none set, or the stock start G-code's stage 39) never counts
            return "chamber at its %d C target" % target
    return None


def job_key(state):
    for path in ("print.task_id", "print.job_id", "print.subtask_name"):
        v = get(state, path)
        if v not in (None, "", "0", 0):
            return str(v)
    return None


class FanWatch:
    """Fan on at speed 5 (or configured speed) during an ABS, ASA or PETG job once the warm-up is over; at full blast
    (speed 10) for 15 minutes (or configured duration) after the print (air purification stage 66 or job end); off when
    the purge ends. The job and purge state are kept in fan-state.json, and settings in fan-settings.json. Commands run
    one at a time in the background. A fan changed manually from the dashboard or by hand is respected."""

    def __init__(self, cmd, path, settings_path=None):
        self.cmd, self.path = list(cmd), path
        self.settings_path = settings_path or os.path.join(os.path.dirname(path), "fan-settings.json")
        self.lock = threading.Lock()
        self.busy, self.next_try, self.delay, self.next_check, self.checked = False, 0.0, FAN_RETRY[0], 0.0, False
        self.manual_override = None
        self.settings = dict(DEFAULT_SETTINGS)
        try:
            with open(self.settings_path) as f:
                s = json.load(f)
                if isinstance(s, dict):
                    self.settings.update(s)
        except (OSError, ValueError):
            pass
        try:
            with open(path) as f:
                d = json.load(f)
                self.fan_job = d.get("job")
                self.fan_speed = d.get("speed")
                self.purge_start = d.get("purge_start", 0.0)
        except (OSError, ValueError, AttributeError):
            self.fan_job, self.fan_speed, self.purge_start = None, None, 0.0

    def set_manual(self, action, speed=None, duration_seconds=None):
        with self.lock:
            target_speed = speed if action == "on" else 0
            until = (time.time() + duration_seconds) if duration_seconds else None
            self.manual_override = {"action": action, "speed": target_speed, "until": until}
            why = "manual override (%s%s)" % (action, (" %d" % target_speed) if action == "on" else "")
            w = (action, "manual", target_speed, why)
            self.busy = True
        threading.Thread(target=self.run, args=(*w, time.time()), daemon=True).start()

    def set_auto(self):
        with self.lock:
            self.manual_override = None

    def update_settings(self, new_settings):
        with self.lock:
            self.settings.update(new_settings)
            try:
                with open(self.settings_path, "w") as f:
                    json.dump(self.settings, f, indent=2)
            except OSError as e:
                say("fan: could not save %s: %s" % (self.settings_path, e))

    def status(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            manual = self.manual_override is not None
            manual_left = 0
            if manual and self.manual_override.get("until"):
                manual_left = max(0, int(self.manual_override["until"] - now))
            purge_left = 0
            if self.purge_start > 0:
                dur = self.settings.get("post_print_duration_minutes", 15) * 60
                purge_left = max(0, int(dur - (now - self.purge_start)))
            return {
                "job": self.fan_job,
                "speed": self.fan_speed,
                "is_on": self.fan_speed is not None and self.fan_speed > 0,
                "mode": "manual" if manual else "auto",
                "manual_seconds_left": manual_left,
                "purge_seconds_left": purge_left,
                "busy": self.busy,
            }

    def wanted(self, state, now=None):
        """("on", job, speed, why), ("off", None, 0, why) or None."""
        now = time.time() if now is None else now
        if self.manual_override is not None:
            until = self.manual_override.get("until")
            if until and now >= until:
                self.manual_override = None
            else:
                target = self.manual_override.get("speed", 0)
                if self.manual_override["action"] == "on":
                    if self.fan_speed != target:
                        return "on", "manual", target, "manual override at speed %d" % target
                elif self.manual_override["action"] == "off":
                    if self.fan_speed is not None and self.fan_speed > 0:
                        return "off", None, 0, "manual override off"
                return None
        gs = get(state, "print.gcode_state")
        if not isinstance(gs, str) or not gs:
            return None                     # no full status yet
        job = job_key(state)
        print_spd = self.settings.get("print_speed", 5)
        purge_spd = self.settings.get("post_print_smartlife_speed", 10)
        purge_dur = self.settings.get("post_print_duration_minutes", 15) * 60
        active_filaments = self.settings.get("filaments", ["ABS", "ASA", "PETG"])
        if gs in ACTIVE:
            if job is None or job == self.fan_job:
                if self.fan_job is not None and str(get(state, "print.stg_cur")) == "66":
                    if self.fan_speed != purge_spd:
                        return "on", self.fan_job, purge_spd, "job %s post-print air purification (stage 66) at full blast for %d min" % (
                            self.fan_job, int(purge_dur / 60))
                return None
            used = job_filaments(state)
            if any(FAN_FILAMENTS.match(t) for t in used) or any(t.upper() in active_filaments for t in used):
                ready = warmed_up(state)
                if ready is None:
                    if self.fan_job is not None:
                        return "off", None, 0, "fan off for warm-up of %s job %s" % (gs.lower(), job)
                    return None                 # still warming up; nothing to switch off either
                if ready:
                    stg = str(get(state, "print.stg_cur"))
                    speed = purge_spd if stg == "66" else print_spd
                    why = ("post-print air purification at full blast" if stg == "66"
                           else ("%s job %s uses %s, %s" % (gs.lower(), get(state, "print.subtask_name") or job,
                                                           "+".join(used), ready)))
                    return "on", job, speed, why
            elif self.fan_job is not None:
                return "off", None, 0, "job %s ended; replaced by %s job %s without ABS/ASA/PETG" % (
                    self.fan_job, gs.lower(), str(job))
        if self.fan_job is not None:
            if self.purge_start == 0.0:
                return "on", self.fan_job, purge_spd, "job %s %s, running %d min post-print purge at full blast" % (
                    self.fan_job, "ended" if gs not in ACTIVE else "replaced by " + str(job), int(purge_dur / 60))
            elapsed = now - self.purge_start
            if elapsed < purge_dur:
                if self.fan_speed != purge_spd:
                    return "on", self.fan_job, purge_spd, "job %s post-print purge at full blast (%d s left)" % (
                        self.fan_job, int(purge_dur - elapsed))
                return None
            return "off", None, 0, "job %s %d min post-print purge complete, fan off" % (self.fan_job, int(purge_dur / 60))
        return None

    def update(self, state, now=None):
        now = time.time() if now is None else now
        with self.lock:
            if self.busy:
                return
            w = self.wanted(state, now)
            if w is not None and now < self.next_try:
                return                      # waiting to retry a failed command
            if w is None:
                if now < self.next_check:
                    return
                w = ("status", self.fan_job, 0, "regular check")
            self.busy = True
        threading.Thread(target=self.run, args=(*w, now), daemon=True).start()

    def run(self, action, job, speed, why, now=None):
        if action != "status":
            say("fan %s%s: %s" % (action, (" %d" % speed) if action == "on" else "", why))
        try:
            cmd = self.cmd + ([action, str(speed)] if action == "on" else [action])
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            ok, out = r.returncode == 0, (r.stdout + r.stderr).strip()
        except Exception as e:              # missing interpreter, timeout, ...
            ok, out = False, str(e)
        now = time.time() if now is None else now
        with self.lock:
            self.busy = False
            if action == "status":
                self.next_check = now + (FAN_CHECK if ok else 3600)
            elif ok:
                if job != "manual":
                    self.fan_job = job
                self.fan_speed = speed if action == "on" else None
                purge_spd = self.settings.get("post_print_smartlife_speed", 10)
                if action == "on" and speed == purge_spd and self.purge_start == 0.0 and job != "manual":
                    self.purge_start = now
                elif action == "off":
                    self.purge_start = 0.0
                self.next_try, self.delay, self.next_check = 0.0, FAN_RETRY[0], now + FAN_CHECK
                job = self.fan_job
                try:
                    with open(self.path, "w") as f:
                        json.dump({"job": job, "speed": self.fan_speed, "purge_start": self.purge_start,
                                   "changed": datetime.datetime.now().isoformat(timespec="seconds")}, f)
                except OSError as e:
                    say("fan: could not save %s: %s" % (self.path, e))
            else:
                self.next_try, self.delay = now + self.delay, min(self.delay * 2, FAN_RETRY[1])
            first, self.checked = not self.checked, True
        if action != "status" or not ok or first:
            say("fan %s%s %s%s" % (action, (" %d" % speed) if action == "on" else "", "done" if ok else "FAILED", (": " + out[-300:]) if out else ""))


def session(cfg):
    """One connection; returns (or raises) when it drops."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False          # the printer presents Bambu's own certificate, not a public one
    ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((cfg["ip"], int(cfg.get("port", 8883))), timeout=15)
    s = ctx.wrap_socket(raw)
    try:
        client = "h2dlog-" + uuid.uuid4().hex[:8]
        body = mstr("MQTT") + bytes([4, 0xC2, 0, 60]) + mstr(client) + mstr("bblp") + mstr(cfg["access_code"])
        s.sendall(packet(0x10, body))
        s.settimeout(15)
        ack = b""
        while len(ack) < 4:                 # CONNACK is exactly 4 bytes; nothing else is sent before we subscribe
            part = s.recv(4 - len(ack))
            if not part:
                break
            ack += part
        if len(ack) < 4 or ack[0] != 0x20 or ack[3] != 0:
            raise RuntimeError("printer refused the connection (code %s); 4 or 5 = wrong access code"
                               % (ack[3] if len(ack) == 4 else "none"))
        say("connected to the printer")
        s.sendall(packet(0x82, bytes([0, 1]) + mstr("device/%s/report" % cfg["serial"]) + bytes([0])))
        s.sendall(packet(0x30, mstr("device/%s/request" % cfg["serial"])
                         + b'{"pushing":{"sequence_id":"0","command":"pushall"}}'))
        s.settimeout(1)
        state, buf = {}, bytearray()
        next_log, next_ping, last_rx = time.time() + min(3, EVERY), time.time() + PING, time.time()
        while True:
            try:
                chunk = s.recv(65536)
                if not chunk:
                    raise RuntimeError("connection closed by the printer")
                buf += chunk
                last_rx = time.time()
            except socket.timeout:
                pass
            while True:                 # take every complete packet out of the buffer
                if len(buf) < 2:
                    break
                n, mult, i = 0, 1, 1
                while True:
                    if i >= len(buf):
                        n = None
                        break
                    d = buf[i]
                    n += (d & 127) * mult
                    mult *= 128
                    i += 1
                    if not d & 128:
                        break
                if n is None or len(buf) < i + n:
                    break
                head, data = buf[0], bytes(buf[i:i + n])
                del buf[:i + n]
                if head & 0xF0 == 0x30:
                    tl = int.from_bytes(data[:2], "big")
                    off = 2 + tl + (2 if (head >> 1) & 3 else 0)
                    try:
                        msg = json.loads(data[off:].decode("utf-8", "replace"))
                        if isinstance(msg, dict):
                            merge(state, msg)
                            with STATE_LOCK:
                                merge(CURRENT_STATE, msg)
                                LAST_SEEN["time"] = time.time()
                    except ValueError:
                        pass
            now = time.time()
            if now >= next_ping:
                s.sendall(b"\xc0\x00")
                next_ping = now + PING
            if now - last_rx > STALE:
                raise RuntimeError("no data from the printer for %d s" % STALE)
            if now >= next_log:
                next_log = now + EVERY
                if state:
                    log_line(state)
                    if FAN is not None:
                        FAN.update(state)
    finally:
        try:
            s.close()
        except OSError:
            pass


def get_status_data():
    with STATE_LOCK:
        st = dict(CURRENT_STATE)
    now = time.time()
    online = (now - LAST_SEEN.get("time", 0)) < 60
    ch_now, ch_tgt = packed(st, "print.device.ctc.info.temp")
    bed_now, bed_tgt = packed(st, "print.device.bed.info.temp")
    nr_now, nr_tgt = packed(st, "print.device.extruder.info.0.temp")
    nl_now, nl_tgt = packed(st, "print.device.extruder.info.1.temp")
    return {
        "printer": {
            "online": online,
            "gcode_state": txt(st, "print.gcode_state") or "OFFLINE",
            "stg_cur": txt(st, "print.stg_cur"),
            "mc_percent": int(get(st, "print.mc_percent") or 0),
            "mc_remaining_time": int(get(st, "print.mc_remaining_time") or 0),
            "layer_num": int(get(st, "print.layer_num") or 0),
            "subtask_name": txt(st, "print.subtask_name"),
            "chamber_temp": int(ch_now) if ch_now else 0,
            "chamber_target": int(ch_tgt) if ch_tgt else 0,
            "bed_temp": int(bed_now) if bed_now else 0,
            "bed_target": int(bed_tgt) if bed_tgt else 0,
            "nozzle_right_temp": int(nr_now) if nr_now else 0,
            "nozzle_right_target": int(nr_tgt) if nr_tgt else 0,
            "nozzle_left_temp": int(nl_now) if nl_now else 0,
            "nozzle_left_target": int(nl_tgt) if nl_tgt else 0,
            "filaments": job_filaments(st) if st else [],
        },
        "fan": FAN.status(now) if FAN is not None else {
            "job": None, "speed": 0, "is_on": False, "mode": "disabled",
            "manual_seconds_left": 0, "purge_seconds_left": 0, "busy": False
        },
        "settings": FAN.settings if FAN is not None else DEFAULT_SETTINGS,
        "server_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bambu Lab H2D Status & Exhaust</title>
<link rel="icon" type="image/png" sizes="32x32" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAADJ0lEQVR4nOVXTWsUQRB91d2zsx8mfmwUFAUR/4JXj3oScoki8aygBIKK6MWA4kXZgIoKknOCRjx4CV485CfkYEAvalAhJkrUrHGnu0tqsqu7OxuzO1n14IOBnuZ116uaqppuguDhgMbRSbdtaHC3yqshBh+B5/1gGACEjYFB5EH0mhWe4lvl9sebD2YxMKAxOemoZrx49ng/ZYI70GoXrAM7j65CKZDRYO+XKLIXFkrj98V27F3x3IkjFJoncB5srQVIgzbseTMYDA9FmnIh+Gv59OLoxD3qGx7cyQFmoHQfrHMg6C4bbpLBHlqzhMJZOmC8xlmVCfp4peJAlDBOlAwEM7fcuy0ukYLzjkITaB+NGBD3s3WSKC1DHjmbmNNKvtAGuATNkWMGHTbE2AfnhZPkEaFY6Emkw+eVb3DsG2Y74cZglljkTRySFobFm2K+B8+Gr2FzLg/PDFUN0qFblzH7fg65TAgJcLtcmW8UAZY6XxM1rzaF2YZ5TSo2nJZbv8xgHYh3kkj1XnEXuDWY9QSIZ/GzRpan5bYt4LeGqkbqx53CpFkk/SqyFpHWP5OwVQn+MQFb8gX09fQiH1SrwK9WQZoomHaJ9Vs/PnkJzvtfk7z6GXrDXPxaS8CuCqhHT3bVUDdg0i5sVV70NwVQ2oVpBXCd0WNj1/Fy/h2yQSaet95ha24THp28iN5srqERdU1APV7Mv8PM21coVP8FFWexvbA5/ul0CtPxCgC5IBMbz1cFBE4jn8n8vRzw1X4vjwiojdNA4R9D/fcCqHh+UFI3eWKqHib2FnckSurNxw/4bqOfvb8TbjMMGHPQag+kt9cJkYGU1fP3c4m+F5qgYcNOuA1OMiqGwVMq0Kd4xSWO5bJMyqsZrTK+E67opcBoVOy0UopLPoqWoRXFl4YWGzQ/a6EtrlQuEbOXkzJfVQs3Jl6y9cOUDRWUkhOkw5+D7M2qkDWI3JWF0vi0kgvip9GJMV4un4FSZQoDjZTHq/UQhz3Q7L+Ury6Ojo/IDVnJzVgGi6WJu2TtQfZ+EsASQOlaW2tI2MvMfgoVd3ixNH4ZIyNKruc/AEC2lsEeQkJ3AAAAAElFTkSuQmCC">
<link rel="icon" type="image/svg+xml" sizes="any" href="data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2032%2032%22%3E%3Crect%20width%3D%2232%22%20height%3D%2232%22%20rx%3D%227%22%20fill%3D%22%2313745b%22%2F%3E%3Cpath%20d%3D%22M8%207h5v7h6V7h5v18h-5v-7h-6v7H8z%22%20fill%3D%22%23fff%22%2F%3E%3C%2Fsvg%3E">
<style>
:root {
  --bg: #0b1120; --card: #1e293b; --card-border: #334155; --text: #f8fafc;
  --text-muted: #94a3b8; --accent: #38bdf8; --accent-hover: #0284c7;
  --success: #22c55e; --warning: #f59e0b; --danger: #ef4444; --btn-bg: #334155;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); padding: 16px; line-height: 1.5; }
.container { max-width: 900px; margin: 0 auto; display: flex; flex-direction: column; gap: 16px; }
.header { display: flex; justify-content: space-between; align-items: center; padding-bottom: 12px; border-bottom: 1px solid var(--card-border); flex-wrap: wrap; gap: 8px; }
.header h1 { font-size: 1.25rem; font-weight: 700; display: flex; align-items: center; gap: 8px; }
.pill { padding: 4px 10px; border-radius: 999px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
.pill-online { background: rgba(34, 197, 94, 0.2); color: var(--success); border: 1px solid rgba(34, 197, 94, 0.4); }
.pill-offline { background: rgba(239, 68, 68, 0.2); color: var(--danger); border: 1px solid rgba(239, 68, 68, 0.4); }
.pill-state { background: rgba(56, 189, 248, 0.2); color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.4); }
.pill-auto { background: rgba(34, 197, 94, 0.15); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.3); }
.pill-manual { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
.card { background: var(--card); border: 1px solid var(--card-border); border-radius: 12px; padding: 16px; display: flex; flex-direction: column; gap: 12px; }
.card-title { font-size: 0.95rem; font-weight: 600; color: var(--text-muted); display: flex; justify-content: space-between; align-items: center; }
.temps-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }
.temp-box { background: rgba(11, 17, 32, 0.6); padding: 8px 10px; border-radius: 8px; border: 1px solid rgba(51, 65, 85, 0.4); }
.temp-label { font-size: 0.7rem; color: var(--text-muted); text-transform: uppercase; }
.temp-val { font-size: 1.15rem; font-weight: 700; color: var(--text); }
.temp-target { font-size: 0.75rem; color: var(--text-muted); }
.progress-container { background: rgba(11, 17, 32, 0.8); border-radius: 6px; height: 10px; overflow: hidden; margin: 4px 0; }
.progress-bar { background: linear-gradient(90deg, #38bdf8, #818cf8); height: 100%; width: 0%; transition: width 0.4s ease; }
.fan-status-box { display: flex; align-items: center; justify-content: space-between; background: rgba(11, 17, 32, 0.6); padding: 12px; border-radius: 8px; border: 1px solid var(--card-border); }
.fan-icon { width: 32px; height: 32px; fill: var(--text-muted); transition: fill 0.2s; }
.fan-spinning { animation: spin 1s linear infinite; fill: var(--accent); }
@keyframes spin { 100% { transform: rotate(360deg); } }
.fan-info { display: flex; flex-direction: column; }
.fan-name { font-weight: 700; font-size: 1rem; }
.btn-group { display: flex; flex-wrap: wrap; gap: 6px; }
button { background: var(--btn-bg); color: var(--text); border: 1px solid var(--card-border); padding: 6px 12px; border-radius: 6px; font-weight: 600; font-size: 0.8rem; cursor: pointer; transition: all 0.15s ease; }
button:hover { background: #475569; }
button:active { transform: scale(0.98); }
.btn-primary { background: #0284c7; border-color: #38bdf8; }
.btn-primary:hover { background: #0369a1; }
.btn-danger { background: #b91c1c; border-color: #ef4444; }
.btn-danger:hover { background: #991b1b; }
.btn-success { background: #15803d; border-color: #22c55e; }
.btn-success:hover { background: #166534; }
.btn-warn { background: #b45309; border-color: #f59e0b; }
.btn-warn:hover { background: #92400e; }
.btn-full { width: 100%; }
.slider-group { display: flex; flex-direction: column; gap: 4px; }
.slider-label { display: flex; justify-content: space-between; font-size: 0.8rem; color: var(--text-muted); }
input[type=range] { width: 100%; accent-color: var(--accent); }
input[type=number] { background: #0b1120; border: 1px solid var(--card-border); color: var(--text); padding: 4px 8px; border-radius: 6px; width: 70px; font-size: 0.85rem; }
.form-row { display: flex; justify-content: space-between; align-items: center; font-size: 0.85rem; padding: 4px 0; }
.checkbox-group { display: flex; gap: 10px; flex-wrap: wrap; }
.checkbox-label { display: flex; align-items: center; gap: 4px; font-size: 0.8rem; cursor: pointer; }
.toast { position: fixed; bottom: 20px; right: 20px; background: var(--success); color: #000; padding: 8px 16px; border-radius: 6px; font-weight: 600; font-size: 0.85rem; display: none; box-shadow: 0 4px 12px rgba(0,0,0,0.5); z-index: 100; }
.logs-table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
.logs-table th, .logs-table td { padding: 4px 8px; border-bottom: 1px solid var(--card-border); }
.logs-table a { color: var(--accent); text-decoration: none; }
.logs-table a:hover { text-decoration: underline; }
.countdown-banner { background: rgba(245, 158, 11, 0.15); border: 1px solid var(--warning); padding: 8px 12px; border-radius: 6px; font-size: 0.8rem; color: #fbbf24; display: none; align-items: center; justify-content: space-between; }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>Bambu Lab H2D Dashboard</h1>
    <div style="display:flex;align-items:center;gap:8px;">
      <span id="host-badge" style="font-size:0.75rem;color:var(--text-muted)">Printer dashboard</span>
      <span id="conn-pill" class="pill pill-online">Connected</span>
    </div>
  </div>

  <div class="grid">
    <!-- Printer Status Card -->
    <div class="card">
      <div class="card-title">
        <span>Printer Status</span>
        <span id="print-state" class="pill pill-state">--</span>
      </div>
      <div>
        <div id="job-name" style="font-weight:700;font-size:1.05rem;word-break:break-all;">--</div>
        <div style="display:flex;justify-content:space-between;font-size:0.8rem;color:var(--text-muted);margin-top:2px;">
          <span id="layer-info">Layer: --</span>
          <span id="time-remaining">Remaining: --</span>
        </div>
        <div class="progress-container"><div id="progress-bar" class="progress-bar"></div></div>
        <div style="font-size:0.75rem;color:var(--text-muted);text-align:right;" id="progress-pct">0%</div>
      </div>
      <div class="temps-grid">
        <div class="temp-box">
          <div class="temp-label">Chamber</div>
          <div class="temp-val" id="temp-ch">--</div>
          <div class="temp-target" id="target-ch">Target: --</div>
        </div>
        <div class="temp-box">
          <div class="temp-label">Heatbed</div>
          <div class="temp-val" id="temp-bed">--</div>
          <div class="temp-target" id="target-bed">Target: --</div>
        </div>
        <div class="temp-box">
          <div class="temp-label">Nozzle R (T0)</div>
          <div class="temp-val" id="temp-nr">--</div>
          <div class="temp-target" id="target-nr">Target: --</div>
        </div>
        <div class="temp-box">
          <div class="temp-label">Nozzle L (T1)</div>
          <div class="temp-val" id="temp-nl">--</div>
          <div class="temp-target" id="target-nl">Target: --</div>
        </div>
      </div>
      <div style="font-size:0.8rem;color:var(--text-muted);">
        Filaments: <span id="filaments-list" style="color:var(--text);font-weight:600;">--</span>
      </div>
    </div>

    <!-- Smart Life Fan Manual Card -->
    <div class="card">
      <div class="card-title">
        <span>Smart Life Exhaust Fan</span>
        <span id="fan-mode-pill" class="pill pill-auto">Auto</span>
      </div>
      <div class="fan-status-box">
        <div style="display:flex;align-items:center;gap:12px;">
          <svg id="fan-svg" class="fan-icon" viewBox="0 0 24 24"><path d="M12 11a1 1 0 1 0 0 2 1 1 0 0 0 0-2zm0-9a4 4 0 0 0-4 4c0 1.6.9 3 2.3 3.6A6 6 0 0 0 10 12a6 6 0 0 0 .3 2.4C8.9 15 8 16.4 8 18a4 4 0 0 0 8 0c0-1.6-.9-3-2.3-3.6A6 6 0 0 0 14 12a6 6 0 0 0-.3-2.4C15.1 9 16 7.6 16 6a4 4 0 0 0-4-4z"/></svg>
          <div class="fan-info">
            <span class="fan-name" id="fan-state-text">OFF</span>
            <span style="font-size:0.75rem;color:var(--text-muted);" id="fan-speed-text">Strength: 0 / 10</span>
          </div>
        </div>
        <div id="fan-active-badge" class="pill pill-offline">OFF</div>
      </div>
      <div id="countdown-banner" class="countdown-banner">
        <span id="countdown-text">Purging: 15m 00s left</span>
        <button onclick="fanCmd({action:'off'})" style="padding:2px 8px;font-size:0.75rem;">Stop</button>
      </div>
      <div class="btn-group">
        <button class="btn-danger" onclick="fanCmd({action:'off'})">Turn OFF</button>
        <button class="btn-primary" onclick="fanCmd({action:'on', speed:5})">Speed 5</button>
        <button class="btn-warn" onclick="fanCmd({action:'on', speed:10})">Full Blast (10)</button>
        <button class="btn-success" id="btn-return-auto" onclick="fanCmd({mode:'auto'})" style="display:none;">Return to Auto</button>
      </div>
      <div class="slider-group">
        <div class="slider-label">
          <span>Manual Speed</span>
          <span id="manual-speed-val" style="font-weight:700;color:var(--accent)">5</span>
        </div>
        <input type="range" id="manual-speed-slider" min="1" max="10" value="5" oninput="document.getElementById('manual-speed-val').textContent=this.value" onchange="fanCmd({action:'on', speed:parseInt(this.value)})">
      </div>
      <div class="btn-group" style="margin-top:2px;">
        <span style="font-size:0.75rem;color:var(--text-muted);display:flex;align-items:center;">Quick Purge:</span>
        <button onclick="fanCmd({action:'on', speed:10, duration_minutes:15})">15m</button>
        <button onclick="fanCmd({action:'on', speed:10, duration_minutes:30})">30m</button>
        <button onclick="fanCmd({action:'on', speed:10, duration_minutes:60})">60m</button>
      </div>
    </div>
  </div>

  <!-- Settings Card -->
  <div class="card">
    <div class="card-title">
      <span>Print & Exhaust Settings</span>
      <button class="btn-primary" onclick="saveSettings()">Save Settings</button>
    </div>
    <div class="grid">
      <div style="display:flex;flex-direction:column;gap:10px;">
        <div style="font-weight:600;font-size:0.85rem;color:var(--accent);">During Print</div>
        <div class="slider-group">
          <div class="slider-label">
            <span>Smart Life Fan Strength</span>
            <span id="cfg-print-spd-val" style="font-weight:700;color:var(--text)">5</span>
          </div>
          <input type="range" id="cfg-print-speed" min="1" max="10" value="5" oninput="document.getElementById('cfg-print-spd-val').textContent=this.value">
        </div>
        <div style="display:flex;flex-direction:column;gap:4px;">
          <span style="font-size:0.8rem;color:var(--text-muted);">Trigger Filaments:</span>
          <div class="checkbox-group">
            <label class="checkbox-label"><input type="checkbox" id="fil-abs" value="ABS"> ABS</label>
            <label class="checkbox-label"><input type="checkbox" id="fil-asa" value="ASA"> ASA</label>
            <label class="checkbox-label"><input type="checkbox" id="fil-petg" value="PETG"> PETG</label>
            <label class="checkbox-label"><input type="checkbox" id="fil-pla" value="PLA"> PLA</label>
            <label class="checkbox-label"><input type="checkbox" id="fil-tpu" value="TPU"> TPU</label>
          </div>
        </div>
      </div>
      <div style="display:flex;flex-direction:column;gap:10px;">
        <div style="font-weight:600;font-size:0.85rem;color:var(--accent);">After Print (Post-Print Exhaust)</div>
        <div class="form-row">
          <span style="color:var(--text-muted);">Purge Duration:</span>
          <div style="display:flex;align-items:center;gap:4px;">
            <input type="number" id="cfg-purge-mins" min="1" max="120" value="15">
            <span style="font-size:0.8rem;color:var(--text-muted)">min</span>
          </div>
        </div>
        <div class="slider-group">
          <div class="slider-label">
            <span>Smart Life Exhaust Speed</span>
            <span id="cfg-purge-spd-val" style="font-weight:700;color:var(--text)">10</span>
          </div>
          <input type="range" id="cfg-purge-speed" min="1" max="10" value="10" oninput="document.getElementById('cfg-purge-spd-val').textContent=this.value">
        </div>
        <div class="slider-group">
          <div class="slider-label">
            <span>Printer Chamber Exhaust Fan</span>
            <span id="cfg-fan-exhaust-val" style="font-weight:700;color:var(--text)">100%</span>
          </div>
          <input type="range" id="cfg-fan-exhaust" min="0" max="100" value="100" oninput="document.getElementById('cfg-fan-exhaust-val').textContent=this.value+'%'">
        </div>
        <div class="slider-group">
          <div class="slider-label">
            <span>Printer Part Cooling Fan</span>
            <span id="cfg-fan-part-val" style="font-weight:700;color:var(--text)">100%</span>
          </div>
          <input type="range" id="cfg-fan-part" min="0" max="100" value="100" oninput="document.getElementById('cfg-fan-part-val').textContent=this.value+'%'">
        </div>
        <div class="slider-group">
          <div class="slider-label">
            <span>Printer Aux Cooling Fan</span>
            <span id="cfg-fan-aux-val" style="font-weight:700;color:var(--text)">100%</span>
          </div>
          <input type="range" id="cfg-fan-aux" min="0" max="100" value="100" oninput="document.getElementById('cfg-fan-aux-val').textContent=this.value+'%'">
        </div>
        <div class="slider-group">
          <div class="slider-label">
            <span>Printer Chamber Circ Fan</span>
            <span id="cfg-fan-circ-val" style="font-weight:700;color:var(--text)">100%</span>
          </div>
          <input type="range" id="cfg-fan-circ" min="0" max="100" value="100" oninput="document.getElementById('cfg-fan-circ-val').textContent=this.value+'%'">
        </div>
      </div>
    </div>
  </div>

  <!-- CSV Logs Card -->
  <div class="card">
    <div class="card-title">Daily CSV Logs</div>
    <div style="max-height:160px;overflow-y:auto;">
      <table class="logs-table">
        <tbody>
<!-- CSV_LOGS_TABLE -->
        </tbody>
      </table>
    </div>
  </div>
</div>

<div id="toast" class="toast">Settings saved</div>

<script>
document.getElementById('host-badge').textContent = location.host;
let userIsDragging = false;
document.querySelectorAll('input[type=range]').forEach(el => {
  el.addEventListener('mousedown', () => userIsDragging = true);
  el.addEventListener('touchstart', () => userIsDragging = true);
  el.addEventListener('mouseup', () => userIsDragging = false);
  el.addEventListener('touchend', () => userIsDragging = false);
});

async function update() {
  try {
    const res = await fetch('/api/status');
    if (!res.ok) throw new Error('status error');
    const data = await res.json();
    document.getElementById('conn-pill').className = data.printer.online ? 'pill pill-online' : 'pill pill-offline';
    document.getElementById('conn-pill').textContent = data.printer.online ? 'Connected' : 'Offline';

    // Printer state
    const st = data.printer.gcode_state || 'OFFLINE';
    document.getElementById('print-state').textContent = st;
    document.getElementById('job-name').textContent = data.printer.subtask_name || (st === 'RUNNING' ? 'Active Print' : 'Idle');
    document.getElementById('layer-info').textContent = 'Layer: ' + (data.printer.layer_num || 0);
    const rem = data.printer.mc_remaining_time;
    document.getElementById('time-remaining').textContent = rem ? (Math.floor(rem/60) + 'h ' + (rem%60) + 'm left') : 'Remaining: --';
    const pct = data.printer.mc_percent || 0;
    document.getElementById('progress-bar').style.width = pct + '%';
    document.getElementById('progress-pct').textContent = pct + '%';

    // Temps
    document.getElementById('temp-ch').textContent = data.printer.chamber_temp + ' °C';
    document.getElementById('target-ch').textContent = 'Target: ' + data.printer.chamber_target + ' °C';
    document.getElementById('temp-bed').textContent = data.printer.bed_temp + ' °C';
    document.getElementById('target-bed').textContent = 'Target: ' + data.printer.bed_target + ' °C';
    document.getElementById('temp-nr').textContent = data.printer.nozzle_right_temp + ' °C';
    document.getElementById('target-nr').textContent = 'Target: ' + data.printer.nozzle_right_target + ' °C';
    document.getElementById('temp-nl').textContent = data.printer.nozzle_left_temp + ' °C';
    document.getElementById('target-nl').textContent = 'Target: ' + data.printer.nozzle_left_target + ' °C';
    document.getElementById('filaments-list').textContent = (data.printer.filaments && data.printer.filaments.length) ? data.printer.filaments.join(' + ') : 'None loaded';

    // Fan status
    const fan = data.fan;
    const isManual = fan.mode === 'manual';
    document.getElementById('fan-mode-pill').className = isManual ? 'pill pill-manual' : 'pill pill-auto';
    document.getElementById('fan-mode-pill').textContent = isManual ? 'Manual Override' : 'Auto';
    document.getElementById('btn-return-auto').style.display = isManual ? 'inline-block' : 'none';

    const fanActive = fan.is_on;
    document.getElementById('fan-active-badge').className = fanActive ? 'pill pill-online' : 'pill pill-offline';
    document.getElementById('fan-active-badge').textContent = fanActive ? 'ON' : 'OFF';
    document.getElementById('fan-state-text').textContent = fanActive ? ('ON (Speed ' + fan.speed + ')') : 'OFF';
    document.getElementById('fan-speed-text').textContent = 'Strength: ' + (fan.speed || 0) + ' / 10';
    document.getElementById('fan-svg').className = fanActive ? 'fan-icon fan-spinning' : 'fan-icon';

    // Countdown banner
    const cd = document.getElementById('countdown-banner');
    if (fan.purge_seconds_left > 0) {
      cd.style.display = 'flex';
      const m = Math.floor(fan.purge_seconds_left / 60);
      const s = fan.purge_seconds_left % 60;
      document.getElementById('countdown-text').textContent = 'Purging at full blast: ' + m + 'm ' + (s < 10 ? '0' : '') + s + 's left';
    } else if (fan.manual_seconds_left > 0) {
      cd.style.display = 'flex';
      const m = Math.floor(fan.manual_seconds_left / 60);
      const s = fan.manual_seconds_left % 60;
      document.getElementById('countdown-text').textContent = 'Manual timer: ' + m + 'm ' + (s < 10 ? '0' : '') + s + 's left';
    } else {
      cd.style.display = 'none';
    }

    // Populate settings if not dragging
    if (!userIsDragging && !window.settingsLoaded && data.settings) {
      window.settingsLoaded = true;
      const s = data.settings;
      if (s.print_speed) {
        document.getElementById('cfg-print-speed').value = s.print_speed;
        document.getElementById('cfg-print-spd-val').textContent = s.print_speed;
      }
      if (s.filaments) {
        ['ABS', 'ASA', 'PETG', 'PLA', 'TPU'].forEach(f => {
          const el = document.getElementById('fil-' + f.toLowerCase());
          if (el) el.checked = s.filaments.includes(f);
        });
      }
      if (s.post_print_duration_minutes) document.getElementById('cfg-purge-mins').value = s.post_print_duration_minutes;
      if (s.post_print_smartlife_speed) {
        document.getElementById('cfg-purge-speed').value = s.post_print_smartlife_speed;
        document.getElementById('cfg-purge-spd-val').textContent = s.post_print_smartlife_speed;
      }
      if (s.post_print_chamber_exhaust_percent !== undefined) {
        document.getElementById('cfg-fan-exhaust').value = s.post_print_chamber_exhaust_percent;
        document.getElementById('cfg-fan-exhaust-val').textContent = s.post_print_chamber_exhaust_percent + '%';
      }
      if (s.post_print_part_fan_percent !== undefined) {
        document.getElementById('cfg-fan-part').value = s.post_print_part_fan_percent;
        document.getElementById('cfg-fan-part-val').textContent = s.post_print_part_fan_percent + '%';
      }
      if (s.post_print_aux_fan_percent !== undefined) {
        document.getElementById('cfg-fan-aux').value = s.post_print_aux_fan_percent;
        document.getElementById('cfg-fan-aux-val').textContent = s.post_print_aux_fan_percent + '%';
      }
      if (s.post_print_chamber_circ_percent !== undefined) {
        document.getElementById('cfg-fan-circ').value = s.post_print_chamber_circ_percent;
        document.getElementById('cfg-fan-circ-val').textContent = s.post_print_chamber_circ_percent + '%';
      }
    }
  } catch (err) {
    document.getElementById('conn-pill').className = 'pill pill-offline';
    document.getElementById('conn-pill').textContent = 'Disconnected';
  }
}

async function fanCmd(payload) {
  try {
    const res = await fetch('/api/fan/manual', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (res.ok) update();
  } catch (e) {
    alert('Failed to send fan command');
  }
}

async function saveSettings() {
  const fils = [];
  ['ABS', 'ASA', 'PETG', 'PLA', 'TPU'].forEach(f => {
    const el = document.getElementById('fil-' + f.toLowerCase());
    if (el && el.checked) fils.push(f);
  });
  const payload = {
    print_speed: parseInt(document.getElementById('cfg-print-speed').value),
    filaments: fils,
    post_print_duration_minutes: parseInt(document.getElementById('cfg-purge-mins').value),
    post_print_smartlife_speed: parseInt(document.getElementById('cfg-purge-speed').value),
    post_print_chamber_exhaust_percent: parseInt(document.getElementById('cfg-fan-exhaust').value),
    post_print_part_fan_percent: parseInt(document.getElementById('cfg-fan-part').value),
    post_print_aux_fan_percent: parseInt(document.getElementById('cfg-fan-aux').value),
    post_print_chamber_circ_percent: parseInt(document.getElementById('cfg-fan-circ').value)
  };
  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (res.ok) {
      const t = document.getElementById('toast');
      t.style.display = 'block';
      setTimeout(() => t.style.display = 'none', 2500);
      update();
    }
  } catch (e) {
    alert('Failed to save settings');
  }
}

setInterval(update, 2000);
update();
</script>
</body>
</html>
"""


class LogsOnly(http.server.SimpleHTTPRequestHandler):
    """Dashboard, API, and read-only listing of logs/ (CSV files only)."""
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=LOGS, **kw)

    def allowed(self):
        name = self.path.split("?")[0].strip("/")
        if name.startswith("api/"):
            return True
        if name and not re.fullmatch(r"status-\d{4}-\d{2}-\d{2}\.csv", name):
            self.send_error(404)
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        clean = self.path.split("?")[0].strip("/")
        if clean == "api/status":
            self.send_json(get_status_data())
            return
        if not clean or clean == "index.html" or clean == "dashboard":
            self.send_dashboard()
            return
        super().do_GET()

    def do_HEAD(self):
        if self.allowed():
            clean = self.path.split("?")[0].strip("/")
            if not clean or clean == "index.html" or clean.startswith("api/"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8" if not clean.startswith("api/") else "application/json")
                self.end_headers()
                return
            super().do_HEAD()

    def do_POST(self):
        clean = self.path.split("?")[0].strip("/")
        if not clean.startswith("api/"):
            self.send_error(501, "Unsupported method (%r)" % self.command)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError):
            self.send_json({"ok": False, "error": "Invalid JSON"}, status=400)
            return
        if clean == "api/fan/manual":
            self.handle_fan_manual(data)
        elif clean == "api/settings":
            self.handle_settings(data)
        else:
            self.send_error(404)

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_fan_manual(self, data):
        if FAN is None:
            self.send_json({"ok": False, "error": "Fan control not enabled on this host"}, status=400)
            return
        mode = data.get("mode")
        action = data.get("action")
        speed = data.get("speed")
        dur_mins = data.get("duration_minutes")
        if mode == "auto":
            FAN.set_auto()
            self.send_json({"ok": True, "fan": FAN.status()})
            return
        if action in ("on", "off"):
            spd = int(speed) if (action == "on" and speed is not None) else (FAN.settings.get("print_speed", 5) if action == "on" else 0)
            spd = max(1, min(10, spd)) if action == "on" else 0
            dur_secs = int(dur_mins * 60) if dur_mins else None
            FAN.set_manual(action, spd, dur_secs)
            self.send_json({"ok": True, "fan": FAN.status()})
            return
        self.send_json({"ok": False, "error": "Invalid action or mode"}, status=400)

    def handle_settings(self, data):
        if FAN is None:
            self.send_json({"ok": False, "error": "Fan control not enabled on this host"}, status=400)
            return
        clean_settings = {}
        if "print_speed" in data:
            clean_settings["print_speed"] = max(1, min(10, int(data["print_speed"])))
        if "filaments" in data and isinstance(data["filaments"], list):
            clean_settings["filaments"] = [str(f).strip().upper() for f in data["filaments"] if str(f).strip()]
        if "post_print_duration_minutes" in data:
            clean_settings["post_print_duration_minutes"] = max(1, min(120, int(data["post_print_duration_minutes"])))
        if "post_print_smartlife_speed" in data:
            clean_settings["post_print_smartlife_speed"] = max(1, min(10, int(data["post_print_smartlife_speed"])))
        if "post_print_chamber_exhaust_percent" in data:
            clean_settings["post_print_chamber_exhaust_percent"] = max(0, min(100, int(data["post_print_chamber_exhaust_percent"])))
        if "post_print_part_fan_percent" in data:
            clean_settings["post_print_part_fan_percent"] = max(0, min(100, int(data["post_print_part_fan_percent"])))
        if "post_print_aux_fan_percent" in data:
            clean_settings["post_print_aux_fan_percent"] = max(0, min(100, int(data["post_print_aux_fan_percent"])))
        if "post_print_chamber_circ_percent" in data:
            clean_settings["post_print_chamber_circ_percent"] = max(0, min(100, int(data["post_print_chamber_circ_percent"])))
        FAN.update_settings(clean_settings)
        self.send_json({"ok": True, "settings": FAN.settings})

    def send_dashboard(self):
        links = []
        try:
            for p in sorted(glob.glob(os.path.join(self.directory, "status-*.csv")), reverse=True):
                m = re.fullmatch(r"status-\d{4}-\d{2}-\d{2}\.csv", os.path.basename(p))
                if m:
                    n = os.path.basename(p)
                    sz = os.path.getsize(p) / 1024.0
                    links.append('<tr><td><a href="/%s">%s</a></td><td style="text-align:right">%.1f KB</td></tr>' % (n, n, sz))
        except OSError:
            pass
        table_html = "\n".join(links) if links else '<tr><td colspan="2" style="text-align:center;color:#888;">No CSV logs found</td></tr>'
        html = DASHBOARD_HTML.replace("<!-- CSV_LOGS_TABLE -->", table_html)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    os.makedirs(LOGS, exist_ok=True)
    log = os.path.join(HERE, "logger.log")
    if os.path.exists(log) and os.path.getsize(log) > 1000000:
        open(log, "w").close()
    cfg_path = os.path.join(HERE, "config.json")
    if not os.path.exists(cfg_path):
        say("waiting for config.json (ip, serial, access_code) to be copied here")
        while not os.path.exists(cfg_path):
            time.sleep(15)
    with open(cfg_path) as f:
        cfg = json.load(f)
    cleanup()
    global FAN
    fan = [os.path.join(HERE, "fan-venv", "bin", "python"), os.path.join(HERE, "fan.py")]
    if all(os.path.exists(p) for p in fan + [os.path.join(HERE, "fan.json")]):
        FAN = FanWatch(fan, os.path.join(HERE, "fan-state.json"))
        say("exhaust fan control on (ABS, ASA and PETG prints)")
    else:
        say("exhaust fan control off: fan.py, fan.json or fan-venv/ missing")
    try:
        srv = http.server.ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), LogsOnly)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        say("serving logs/ read-only on port %d" % HTTP_PORT)
    except OSError as e:
        say("log server not started: %s" % e)
    while True:
        try:
            session(cfg)
        except Exception as e:          # printer off, Wi-Fi drop, ...: wait and reconnect
            say("disconnected: %s; retrying in 30 s" % e)
        time.sleep(30)


if __name__ == "__main__":
    main()
