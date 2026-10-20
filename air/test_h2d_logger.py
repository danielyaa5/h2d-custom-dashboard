#!/usr/bin/env python3
"""Tests for h2d_logger.py.   python3 test_h2d_logger.py -v   (needs the openssl command for the fake printer's
certificate). Everything runs against a temporary copy of the logger and a fake printer on localhost: the real
printer, the real config.json and the real logs are not touched."""
import datetime
import http.client
import http.server
import importlib.util
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "h2d_logger.py")
CERTS = None
SECRET = "sekret-code-42"
SERIAL = "SER123"
REPORT = "device/%s/report" % SERIAL


def setUpModule():
    global CERTS
    CERTS = tempfile.mkdtemp(prefix="h2dtest-cert-")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", os.path.join(CERTS, "key.pem"),
                    "-out", os.path.join(CERTS, "cert.pem"), "-days", "2", "-subj", "/CN=fake-h2d"],
                   check=True, capture_output=True)


def tearDownModule():
    shutil.rmtree(CERTS, ignore_errors=True)


def load(tmp):
    """A fresh copy of the logger whose HERE / LOGS point into tmp."""
    dst = os.path.join(tmp, "h2d_logger.py")
    shutil.copy(SRC, dst)
    spec = importlib.util.spec_from_file_location("h2d_logger_under_test", dst)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    os.makedirs(mod.LOGS, exist_ok=True)
    mod.said = []
    mod.say = mod.said.append
    return mod


def varlen(n):
    out = bytearray()
    while True:
        d = n % 128
        n //= 128
        out.append(d | 0x80 if n else d)
        if not n:
            return bytes(out)


def pub(topic, payload, qos=0, pid=1):
    t = topic.encode()
    body = len(t).to_bytes(2, "big") + t + (pid.to_bytes(2, "big") if qos else b"") + payload
    return bytes([0x30 | (qos << 1)]) + varlen(len(body)) + body


def recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        part = conn.recv(n - len(buf))
        if not part:
            raise EOFError("client closed")
        buf += part
    return buf


def read_packet(conn):
    head = recv_exact(conn, 1)[0]
    n, mult = 0, 1
    while True:
        d = recv_exact(conn, 1)[0]
        n += (d & 127) * mult
        mult *= 128
        if not d & 128:
            break
    return head, recv_exact(conn, n)


def take_str(data, off):
    n = int.from_bytes(data[off:off + 2], "big")
    return data[off + 2:off + 2 + n].decode(), off + 2 + n


class FakePrinter:
    """One-connection TLS MQTT broker; `script(self, conn)` plays the printer."""
    def __init__(self, script):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(os.path.join(CERTS, "cert.pem"), os.path.join(CERTS, "key.pem"))
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.script, self.error, self.seen = script, None, {}
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            raw, _ = self.sock.accept()
            conn = self.ctx.wrap_socket(raw, server_side=True)
            try:
                self.script(self, conn)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
        except Exception as e:      # reported by the test through .error
            self.error = e
        finally:
            self.sock.close()

    def handshake(self, conn, rc=0, split_ack=False):
        head, data = read_packet(conn)
        proto, off = take_str(data, 0)
        level, flags, keepalive = data[off], data[off + 1], int.from_bytes(data[off + 2:off + 4], "big")
        client, off = take_str(data, off + 4)
        user, off = take_str(data, off)
        password, off = take_str(data, off)
        self.seen["connect"] = dict(head=head, proto=proto, level=level, flags=flags, keepalive=keepalive,
                                    client=client, user=user, password=password, rest=data[off:])
        ack = bytes([0x20, 2, 0, rc])
        if split_ack:
            conn.sendall(ack[:2])
            time.sleep(0.4)
            conn.sendall(ack[2:])
        else:
            conn.sendall(ack)
        if rc:
            return
        head, data = read_packet(conn)
        topic, off = take_str(data, 2)
        self.seen["subscribe"] = dict(head=head, pid=data[:2], topic=topic, qos=data[off], rest=data[off + 1:])
        conn.sendall(bytes([0x90, 3]) + data[:2] + b"\x00")
        head, data = read_packet(conn)
        topic, off = take_str(data, 0)
        self.seen["request"] = dict(head=head, topic=topic, payload=data[off:])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="h2dtest-")
        self.mod = load(self.tmp)
        self.mod.EVERY = 1

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cfg(self, port):
        return {"ip": "127.0.0.1", "port": port, "serial": SERIAL, "access_code": SECRET}

    def rows(self):
        out = []
        for name in sorted(os.listdir(self.mod.LOGS)):
            with open(os.path.join(self.mod.LOGS, name)) as f:
                out += [line.rstrip("\n").split(",") for line in f]
        return out

    def run_session(self, printer):
        """Runs one session to its end; returns (exception, seconds)."""
        t0 = time.time()
        try:
            self.mod.session(self.cfg(printer.port))
            err = None
        except Exception as e:
            err = e
        dt = time.time() - t0
        printer.thread.join(10)
        return err, dt


class Helpers(Base):
    def test_varlen(self):
        for n, want in [(0, "00"), (1, "01"), (127, "7f"), (128, "8001"), (16383, "ff7f"), (16384, "808001"),
                        (2097151, "ffff7f"), (2097152, "80808001")]:
            self.assertEqual(self.mod.varlen(n).hex(), want, n)

    def test_mstr_and_packet(self):
        self.assertEqual(self.mod.mstr("bblp"), b"\x00\x04bblp")
        self.assertEqual(self.mod.mstr("é"), b"\x00\x02\xc3\xa9")
        self.assertEqual(self.mod.packet(0x82, b"abc"), b"\x82\x03abc")
        self.assertEqual(self.mod.packet(0x30, b"x" * 200)[:3], b"\x30\xc8\x01")

    def test_merge(self):
        s = {"print": {"a": 1, "dev": {"x": 1, "y": 2}, "lst": [1, 2, 3]}}
        self.mod.merge(s, {"print": {"dev": {"y": 5, "z": 9}, "lst": [7], "b": "new"}, "info": {"v": 1}})
        self.assertEqual(s, {"print": {"a": 1, "dev": {"x": 1, "y": 5, "z": 9}, "lst": [7], "b": "new"}, "info": {"v": 1}})
        self.mod.merge(s, {"print": {"dev": 3}})            # a dict replaced by a plain value
        self.assertEqual(s["print"]["dev"], 3)
        self.mod.merge(s, {"print": {"dev": {"k": 1}}})     # and back again
        self.assertEqual(s["print"]["dev"], {"k": 1})

    def test_get(self):
        s = {"print": {"device": {"extruder": {"info": [{"temp": 11}, {"temp": 22}]}}, "n": None, "zero": 0}}
        g = self.mod.get
        self.assertEqual(g(s, "print.device.extruder.info.1.temp"), 22)
        self.assertEqual(g(s, "print.zero"), 0)
        for missing in ["print.device.extruder.info.2.temp", "print.device.extruder.info.x.temp", "print.nope.deeper",
                        "print.n.deeper", "print.zero.deeper", "nothing"]:
            self.assertIsNone(g(s, missing), missing)

    def test_packed_and_txt(self):
        s = {"p": {"cur": 25, "both": (60 << 16) | 43, "f": 25.0, "s": "25", "none": None, "name": "a,b\nc", "n": 0}}
        self.assertEqual(self.mod.packed(s, "p.cur"), ("25", "0"))
        self.assertEqual(self.mod.packed(s, "p.both"), ("43", "60"))
        self.assertEqual(self.mod.packed(s, "p.f"), ("25", "0"))
        self.assertEqual(self.mod.packed(s, "p.s"), ("", ""))
        self.assertEqual(self.mod.packed(s, "p.none"), ("", ""))
        self.assertEqual(self.mod.packed(s, "p.missing"), ("", ""))
        self.assertEqual(self.mod.txt(s, "p.name"), "a b c")
        self.assertEqual(self.mod.txt(s, "p.n"), "0")
        self.assertEqual(self.mod.txt(s, "p.missing"), "")


class Files(Base):
    def test_log_line_header_once_and_columns(self):
        state = {"print": {"gcode_state": "RUNNING", "stg_cur": 0, "mc_percent": 40, "mc_remaining_time": 33,
                           "layer_num": 21, "subtask_name": "knob, v27",
                           "device": {"ctc": {"info": {"temp": (60 << 16) | 58}}, "bed": {"info": {"temp": (100 << 16) | 100}},
                                      "extruder": {"info": [{"temp": (260 << 16) | 259}, {"temp": 30}]}}}}
        self.mod.log_line(state)
        self.mod.log_line({})                   # nothing known yet: still a well-formed row
        rows = self.rows()
        self.assertEqual(",".join(rows[0]) + "\n", self.mod.HEADER)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(len(r) == 15 for r in rows), rows)
        self.assertEqual(rows[1][1:], ["RUNNING", "0", "40", "33", "21", "58", "60", "100", "100", "259", "260", "30", "0", "knob  v27"])
        self.assertEqual(rows[2][1:], [""] * 14)
        datetime.datetime.strptime(rows[1][0], "%Y-%m-%d %H:%M:%S")
        self.assertEqual(os.listdir(self.mod.LOGS), ["status-%s.csv" % datetime.date.today().strftime("%Y-%m-%d")])

    def test_new_day_new_file_and_cleanup(self):
        clock = {"now": datetime.datetime(2026, 10, 11, 23, 59, 58)}

        class FakeDT(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return clock["now"]
        self.mod.datetime = types.SimpleNamespace(datetime=FakeDT, date=datetime.date, timedelta=datetime.timedelta)
        old = "status-%s.csv" % (datetime.date.today() - datetime.timedelta(days=40)).strftime("%Y-%m-%d")
        open(os.path.join(self.mod.LOGS, old), "w").close()
        self.mod.log_line({"print": {"gcode_state": "A"}})
        self.assertNotIn(old, os.listdir(self.mod.LOGS))        # a new day's file triggers the cleanup
        clock["now"] = datetime.datetime(2026, 10, 12, 0, 0, 3)
        self.mod.log_line({"print": {"gcode_state": "B"}})
        self.assertEqual(sorted(os.listdir(self.mod.LOGS)), ["status-2026-10-11.csv", "status-2026-10-12.csv"])
        for name, state in [("status-2026-10-11.csv", "A"), ("status-2026-10-12.csv", "B")]:
            with open(os.path.join(self.mod.LOGS, name)) as f:
                lines = f.read().splitlines()
            self.assertEqual(lines[0] + "\n", self.mod.HEADER)
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[1].split(",")[1], state)

    def test_cleanup_keeps_one_month(self):
        self.assertEqual(self.mod.KEEP_DAYS, 31)
        today = datetime.date.today()
        name = lambda days: "status-%s.csv" % (today - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        keep = [name(0), name(1), name(30), name(31), "status-notes.csv", "status-2020-01-01.csv.bak", "other.txt",
                "status-2020-1-1.csv"]
        gone = [name(32), name(33), name(400)]
        for n in keep + gone:
            open(os.path.join(self.mod.LOGS, n), "w").close()
        os.mkdir(os.path.join(self.mod.LOGS, "status-2001-01-01"))
        outside = os.path.join(self.tmp, name(400))             # same name outside logs/: not ours to delete
        open(outside, "w").close()
        self.mod.cleanup()
        self.assertEqual(sorted(os.listdir(self.mod.LOGS)), sorted(keep + ["status-2001-01-01"]))
        self.assertTrue(os.path.exists(outside))
        self.assertEqual(len(self.mod.said), 3)
        self.mod.cleanup()                                      # running it again changes nothing
        self.assertEqual(sorted(os.listdir(self.mod.LOGS)), sorted(keep + ["status-2001-01-01"]))


class Session(Base):
    def test_full_conversation(self):
        full = {"print": {"gcode_state": "PREPARE", "stg_cur": 49, "mc_percent": 0, "mc_remaining_time": 75, "layer_num": 0,
                          "subtask_name": "fit, plate",
                          "device": {"ctc": {"info": {"temp": (60 << 16) | 31}}, "bed": {"info": {"temp": (100 << 16) | 88}},
                                     "extruder": {"info": [{"id": 0, "temp": (260 << 16) | 140}, {"id": 1, "temp": 27}]}},
                          "pad": "x" * 40000}}

        def script(fp, conn):
            fp.handshake(conn)
            conn.sendall(pub(REPORT, json.dumps(full).encode()))            # > 16383 bytes: 3-byte length
            time.sleep(1.7)
            d1 = pub(REPORT, json.dumps({"print": {"device": {"ctc": {"info": {"temp": (60 << 16) | 43}}}}}).encode())
            for part in (d1[:1], d1[1:3], d1[3:10], d1[10:]):                # split inside the header and the payload
                conn.sendall(part)
                time.sleep(0.15)
            d2 = pub(REPORT, json.dumps({"print": {"gcode_state": "RUNNING", "stg_cur": 0}}).encode())
            d3 = pub(REPORT, json.dumps({"print": {"layer_num": 7, "mc_percent": 12}}).encode(), qos=1, pid=77)
            conn.sendall(d2 + d3 + b"\xd0\x00" + pub(REPORT, b"{not json") + pub(REPORT, b"[1,2,3]")
                         + pub(REPORT, b"\xff\xfe") + bytes([0x90, 3, 0, 1, 0]))   # several packets in one write, some junk
            time.sleep(2.6)

        printer = FakePrinter(script)
        err, dt = self.run_session(printer)
        self.assertIsNone(printer.error)
        self.assertIsInstance(err, RuntimeError)
        self.assertIn("closed", str(err))
        c = printer.seen["connect"]
        self.assertEqual((c["head"], c["proto"], c["level"], c["flags"], c["keepalive"], c["user"], c["password"], c["rest"]),
                         (0x10, "MQTT", 4, 0xC2, 60, "bblp", SECRET, b""))
        self.assertTrue(c["client"].startswith("h2dlog-") and len(c["client"]) == 15, c["client"])
        s = printer.seen["subscribe"]
        self.assertEqual((s["head"], s["topic"], s["qos"], s["rest"]), (0x82, REPORT, 0, b""))
        r = printer.seen["request"]
        self.assertEqual((r["head"], r["topic"]), (0x30, "device/%s/request" % SERIAL))
        self.assertEqual(json.loads(r["payload"]), {"pushing": {"sequence_id": "0", "command": "pushall"}})
        rows = self.rows()
        self.assertEqual(",".join(rows[0]) + "\n", self.mod.HEADER)
        self.assertGreaterEqual(len(rows), 4)
        self.assertTrue(all(len(x) == 15 for x in rows))
        self.assertEqual(rows[1][1:], ["PREPARE", "49", "0", "75", "0", "31", "60", "88", "100", "140", "260", "27", "0", "fit  plate"])
        # the partial updates changed only their own fields; everything else was kept
        self.assertEqual(rows[-1][1:], ["RUNNING", "0", "12", "75", "7", "43", "60", "88", "100", "140", "260", "27", "0", "fit  plate"])
        times = [datetime.datetime.strptime(x[0], "%Y-%m-%d %H:%M:%S") for x in rows[1:]]
        self.assertEqual(times, sorted(times))
        blob = repr(self.rows()) + repr(self.mod.said)
        self.assertNotIn(SECRET, blob)                                      # the access code is never written or printed
        self.assertIn("connected to the printer", self.mod.said)

    def test_wrong_access_code(self):
        printer = FakePrinter(lambda fp, conn: (fp.handshake(conn, rc=5), time.sleep(0.3)))
        err, dt = self.run_session(printer)
        self.assertIsInstance(err, RuntimeError)
        self.assertIn("code 5", str(err))
        self.assertNotIn(SECRET, str(err))
        self.assertNotIn("subscribe", printer.seen)                         # nothing more was sent after the refusal
        self.assertEqual(self.rows(), [])

    def test_closed_before_answer(self):
        printer = FakePrinter(lambda fp, conn: read_packet(conn))
        err, dt = self.run_session(printer)
        self.assertIsInstance(err, RuntimeError)
        self.assertIn("code none", str(err))
        self.assertEqual(self.rows(), [])

    def test_answer_arrives_in_two_pieces(self):
        def script(fp, conn):
            fp.handshake(conn, split_ack=True)
            conn.sendall(pub(REPORT, b'{"print":{"gcode_state":"IDLE"}}'))
            time.sleep(1.6)
        printer = FakePrinter(script)
        err, dt = self.run_session(printer)
        self.assertIsNone(printer.error)
        self.assertIn("request", printer.seen)
        self.assertEqual(self.rows()[1][1], "IDLE")

    def test_silent_printer_logs_nothing_and_goes_stale(self):
        self.mod.PING, self.mod.STALE = 1, 3
        pings = []

        def script(fp, conn):
            fp.handshake(conn)
            conn.settimeout(0.5)
            end = time.time() + 9
            while time.time() < end:
                try:
                    head, data = read_packet(conn)
                    pings.append(head)
                except (socket.timeout, TimeoutError):
                    pass
                except (EOFError, OSError):
                    return
        printer = FakePrinter(script)
        err, dt = self.run_session(printer)
        self.assertIsInstance(err, RuntimeError)
        self.assertIn("no data", str(err))
        self.assertTrue(3 <= dt <= 6.5, dt)
        self.assertGreaterEqual(pings.count(0xC0), 2)                       # it did ping before giving up
        self.assertEqual(self.rows(), [])                                   # nothing known, nothing logged

    def test_ping_answers_keep_it_alive(self):
        self.mod.PING, self.mod.STALE = 1, 3

        def script(fp, conn):
            fp.handshake(conn)
            conn.settimeout(0.5)
            end = time.time() + 7.5
            while time.time() < end:
                try:
                    head, data = read_packet(conn)
                    if head == 0xC0:
                        conn.sendall(b"\xd0\x00")
                except (socket.timeout, TimeoutError):
                    pass
        printer = FakePrinter(script)
        err, dt = self.run_session(printer)
        self.assertIsNone(printer.error)
        self.assertIn("closed", str(err))                                   # ended by the printer, not by the stale check
        self.assertGreaterEqual(dt, 7)

    def test_printer_off(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(OSError):
            self.mod.session(self.cfg(port))


def h2d_state(gs, job="111", mapping=(3, 65024), name="duct", stage=0, chamber=(60, 60)):
    """A trimmed H2D report: AMS 0 with ASA in slots 0-2 and ABS in slot 3, HIPS on external spool 254. By default
    the job is printing (stage 0) with the chamber at its 60 C target."""
    trays = [{"id": str(i), "tray_type": t} for i, t in enumerate(["ASA", "PLA", "PETG", "ABS"])]
    st = {"print": {"gcode_state": gs, "task_id": job, "subtask_name": name, "stg_cur": stage,
                    "ams": {"ams": [{"id": "0", "tray": trays}]},
                    "vir_slot": [{"id": "254", "tray_type": "HIPS"}, {"id": "255", "tray_type": ""}],
                    "device": {"ctc": {"info": {"temp": chamber[1] << 16 | chamber[0]}},
                               "extruder": {"info": [{"id": 0, "snow": 65535}, {"id": 1, "snow": 65024}]}}}}
    if mapping is not None:
        st["print"]["mapping"] = list(mapping)
    return st


class Fan(Base):
    def setUp(self):
        super().setUp()
        self.calls = os.path.join(self.tmp, "fan-calls.txt")
        self.fake = os.path.join(self.tmp, "fake_fan.py")
        self.fail_next = os.path.join(self.tmp, "fail-next")
        with open(self.fake, "w") as f:
            f.write("import os, sys\n"
                    "with open(%r, 'a') as f: f.write(sys.argv[1] + '\\n')\n"
                    "if os.path.exists(%r):\n    os.remove(%r); print('cloud said no'); sys.exit(1)\n"
                    % (self.calls, self.fail_next, self.fail_next, ))
        self.state_file = os.path.join(self.tmp, "fan-state.json")

    def watch(self, checked=True):
        w = self.mod.FanWatch([sys.executable, self.fake], self.state_file)
        if checked:
            w.next_check = time.time() + 3600   # no health check in the way
        return w

    def step(self, w, state, now=None):
        w.update(state, now)
        end = time.time() + 10
        while w.busy and time.time() < end:
            time.sleep(0.02)
        self.assertFalse(w.busy)

    def sent(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            return f.read().split()

    def test_job_filaments(self):
        self.assertEqual(self.mod.job_filaments(h2d_state("RUNNING")), ["ABS", "HIPS"])          # the duct adapter job
        self.assertEqual(self.mod.job_filaments(h2d_state("RUNNING", mapping=[1, 65535])), ["PLA"])
        self.assertEqual(self.mod.job_filaments(h2d_state("RUNNING", mapping=["2"])), ["PETG"])  # numbers as text too
        self.assertEqual(self.mod.job_filaments(h2d_state("RUNNING", mapping=None)), ["HIPS"])   # falls back to what's loaded
        self.assertEqual(self.mod.job_filaments({"print": {"mapping": [3]}}), [])

    def test_abs_job_turns_fan_on_then_off_at_the_end(self):
        w = self.watch()
        t0 = time.time()
        for gs in ("IDLE", "PREPARE", "RUNNING", "PAUSE", "RUNNING"):
            self.step(w, h2d_state(gs, job="111" if gs != "IDLE" else "100", mapping=(1,) if gs == "IDLE" else (3, 65024)), now=t0)
        self.assertEqual(self.sent(), ["on"])                       # once, when the job started
        with open(self.state_file) as f:
            st = json.load(f)
            self.assertEqual(st["job"], "111")
            self.assertEqual(st["speed"], 5)
        # stage 66 starts post-print air purification at full blast (speed 10)
        self.step(w, h2d_state("RUNNING", stage=66), now=t0 + 100)
        self.assertEqual(self.sent(), ["on", "on"])
        with open(self.state_file) as f:
            st = json.load(f)
            self.assertEqual(st["speed"], 10)
            self.assertEqual(st["purge_start"], t0 + 100)
        self.step(w, h2d_state("FINISH"), now=t0 + 200)             # still in 15 min purge window
        self.assertEqual(self.sent(), ["on", "on"])
        self.step(w, h2d_state("FINISH"), now=t0 + 1001)            # 15 min elapsed
        self.assertEqual(self.sent(), ["on", "on", "off"])
        with open(self.state_file) as f:
            self.assertIsNone(json.load(f)["job"])
        said = " | ".join(self.mod.said)
        self.assertIn("fan on 5: prepare job duct uses ABS+HIPS", said)
        self.assertIn("fan on 10: job 111 post-print air purification", said)
        self.assertIn("fan off: job 111 15 min post-print purge complete, fan off", said)

    def test_asa_variants_count_and_others_dont(self):
        for kind, on in (("ASA", True), ("ASA-CF", True), ("abs-gf", True), ("ASA Aero", True),
                         ("PETG", True), ("PETG-CF", True), ("petg-hf", True),
                         ("PLA", False), ("HIPS", False), ("PC", False), ("PCABS", False)):
            st = h2d_state("RUNNING", mapping=[0])
            st["print"]["ams"]["ams"][0]["tray"][0]["tray_type"] = kind
            self.assertEqual(self.watch().wanted(st) is not None, on, kind)

    def test_failed_and_cancelled_jobs_also_turn_it_off(self):
        t0 = time.time()
        for end in ("FAILED", "IDLE"):
            w = self.watch()
            self.step(w, h2d_state("RUNNING", job=end), now=t0)
            self.step(w, h2d_state(end, job=end), now=t0)
            self.step(w, h2d_state(end, job=end), now=t0 + 901)
            t0 += 1000
        self.assertEqual(self.sent(), ["on", "on", "off", "on", "on", "off"])

    def test_waits_for_printing_or_chamber_target(self):
        w = self.watch()
        # a cold start, as on the duct adapter print (2026-10-13): homing, heating, levelling, then printing at stage 0
        for stage, ch in ((-1, (27, 0)), (13, (27, 60)), (74, (26, 60)), (1, (48, 60)), (39, (58, 0)), (3, (59, 60))):
            self.step(w, h2d_state("RUNNING", stage=stage, chamber=ch))
        self.step(w, h2d_state("PAUSE", stage=0, chamber=(50, 60)))     # paused before printing: still waiting
        self.assertEqual(self.sent(), [])
        self.step(w, h2d_state("RUNNING", stage=0, chamber=(58, 60)))
        self.assertEqual(self.sent(), ["on"])
        self.assertIn("printing", self.mod.said[-2])
        # or the chamber reaches its target first, while the bed is still being levelled
        os.remove(self.state_file)
        os.remove(self.calls)
        w = self.watch()
        self.step(w, h2d_state("RUNNING", job="222", stage=1, chamber=(59, 60)))
        self.step(w, h2d_state("PREPARE", job="222", stage=-1, chamber=(55, 0)))
        self.assertEqual(self.sent(), [])
        self.step(w, h2d_state("RUNNING", job="222", stage=1, chamber=(60, 60)))
        self.assertEqual(self.sent(), ["on"])
        self.assertIn("chamber at its 60 C target", self.mod.said[-2])

    def test_fan_left_on_by_an_earlier_job_goes_off_for_the_warm_up(self):
        w = self.watch()
        w.fan_job = "old"                   # its end was missed
        self.step(w, h2d_state("RUNNING", job="new", stage=13, chamber=(30, 60)))
        self.step(w, h2d_state("RUNNING", job="new", stage=0, chamber=(50, 60)))
        self.assertEqual(self.sent(), ["off", "on"])

    def test_pla_job_leaves_the_fan_alone(self):
        w = self.watch()
        for gs in ("PREPARE", "RUNNING", "FINISH", "IDLE"):
            self.step(w, h2d_state(gs, mapping=(1,)))
        self.assertEqual(self.sent(), [])

    def test_next_job_without_abs_turns_it_off(self):
        w = self.watch()
        self.step(w, h2d_state("RUNNING", job="1"))
        self.step(w, h2d_state("PREPARE", job="2", mapping=(1,)))
        self.step(w, h2d_state("RUNNING", job="3"))
        self.assertEqual(self.sent(), ["on", "off", "on"])

    def test_restart_mid_print_keeps_the_job_and_turns_off_at_the_end(self):
        t0 = time.time()
        self.step(self.watch(), h2d_state("RUNNING"), now=t0)
        w = self.watch()                                    # the logger restarted
        self.assertEqual(w.fan_job, "111")
        self.step(w, h2d_state("RUNNING"), now=t0 + 10)     # no second "on"
        self.step(w, h2d_state("FINISH"), now=t0 + 20)      # purge starts
        self.step(w, h2d_state("FINISH"), now=t0 + 950)     # purge ends
        self.assertEqual(self.sent(), ["on", "on", "off"])

    def test_incomplete_status_does_nothing(self):
        w = self.watch()
        w.fan_job = "111"
        for st in ({}, {"print": {"mc_percent": 5}}, {"print": {"gcode_state": "RUNNING"}}):
            self.assertIsNone(w.wanted(st))                 # never "off" mid-print because a report was partial

    def test_failed_command_is_retried_later(self):
        open(self.fail_next, "w").close()
        w = self.watch()
        t0 = time.time()
        self.step(w, h2d_state("RUNNING"), now=t0)
        self.assertIsNone(w.fan_job)
        self.step(w, h2d_state("RUNNING"), now=t0 + 10)       # too soon: nothing sent
        self.assertEqual(self.sent(), ["on"])
        self.step(w, h2d_state("RUNNING"), now=t0 + 61)
        self.assertEqual(self.sent(), ["on", "on"])
        self.assertEqual(w.fan_job, "111")
        self.assertIn("fan on 5 FAILED: cloud said no", self.mod.said)

    def test_retry_wait_doubles_up_to_15_minutes(self):
        w = self.watch()
        delays = []
        for _ in range(6):
            open(self.fail_next, "w").close()
            w.next_try = 0
            before = time.time()
            self.step(w, h2d_state("RUNNING"))
            delays.append(round(w.next_try - before, -1))
        self.assertEqual(delays, [60, 120, 240, 480, 900, 900])

    def test_regular_check_when_idle(self):
        w = self.watch(checked=False)
        self.step(w, h2d_state("IDLE", mapping=(1,)))
        self.assertEqual(self.sent(), ["status"])          # right after start, and its result is logged once
        self.assertTrue(any(m.startswith("fan status done") for m in self.mod.said))
        self.step(w, h2d_state("IDLE", mapping=(1,)))
        self.assertEqual(self.sent(), ["status"])          # then only every FAN_CHECK seconds
        self.assertGreater(w.next_check, time.time() + self.mod.FAN_CHECK - 60)
        self.step(w, h2d_state("IDLE", mapping=(1,)), now=w.next_check + 1)
        self.assertEqual(self.sent(), ["status", "status"])
        self.assertEqual(sum(m.startswith("fan status") for m in self.mod.said), 1)   # a passing check isn't logged again

    def test_failed_check_does_not_hold_up_a_print(self):
        open(self.fail_next, "w").close()
        w = self.watch(checked=False)
        self.step(w, h2d_state("IDLE", mapping=(1,)))
        self.step(w, h2d_state("RUNNING"))
        self.assertEqual(self.sent(), ["status", "on"])

    def test_session_hands_each_report_to_the_fan(self):
        seen = []
        self.mod.FAN = types.SimpleNamespace(update=lambda st: seen.append(self.mod.get(st, "print.gcode_state")))

        def script(fp, conn):
            fp.handshake(conn)
            conn.sendall(pub(REPORT, json.dumps(h2d_state("PREPARE")).encode()))
            time.sleep(2.5)

        printer = FakePrinter(script)
        self.run_session(printer)
        self.assertIn("PREPARE", seen)


class LogServer(Base):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.mod.LOGS, "status-2026-10-11.csv"), "w") as f:
            f.write("a,b\n1,2\n")
        with open(os.path.join(self.mod.LOGS, "secret.txt"), "w") as f:
            f.write("TOPSECRET in logs")
        for name in ("config.json", "logger.log"):
            with open(os.path.join(self.tmp, name), "w") as f:
                f.write('{"access_code": "TOPSECRET"}')
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.mod.LogsOnly)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def ask(self, path, method="GET", body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        headers = dict(headers or {})
        c.putrequest(method, path, skip_host=False, skip_accept_encoding=True)
        if body is not None:
            c.putheader("Content-Length", str(len(body)))
        for k, v in headers.items():
            c.putheader(k, v)
        c.endheaders(body)
        r = c.getresponse()
        resp_body = r.read()
        c.close()
        return r.status, resp_body

    def test_serves_dated_csv_and_listing(self):
        self.assertEqual(self.ask("/status-2026-10-11.csv"), (200, b"a,b\n1,2\n"))
        self.assertEqual(self.ask("/status-2026-10-11.csv?x=1"), (200, b"a,b\n1,2\n"))
        status, body = self.ask("/")
        self.assertEqual(status, 200)
        self.assertIn(b"status-2026-10-11.csv", body)
        self.assertIn(b"Bambu Lab H2D Dashboard", body)
        self.assertEqual(self.ask("/status-2026-10-11.csv", "HEAD"), (200, b""))
        self.assertEqual(self.ask("/status-2026-10-12.csv")[0], 404)        # allowed name, no such file

    def test_api_status_endpoint(self):
        status, body = self.ask("/api/status")
        self.assertEqual(status, 200)
        d = json.loads(body.decode("utf-8"))
        self.assertIn("printer", d)
        self.assertIn("fan", d)
        self.assertIn("settings", d)
        self.assertEqual(d["settings"]["print_speed"], 5)

    def test_api_fan_manual_and_settings(self):
        fake_fan = self.mod.FanWatch([sys.executable, "-c", ""], os.path.join(self.tmp, "fan-state.json"),
                                     os.path.join(self.tmp, "fan-settings.json"))
        self.mod.FAN = fake_fan
        try:
            # test fan manual
            req = json.dumps({"action": "on", "speed": 8}).encode("utf-8")
            status, body = self.ask("/api/fan/manual", method="POST", body=req, headers={"Content-Type": "application/json"})
            self.assertEqual(status, 200)
            res = json.loads(body.decode("utf-8"))
            self.assertTrue(res["ok"])
            self.assertEqual(fake_fan.manual_override["action"], "on")
            self.assertEqual(fake_fan.manual_override["speed"], 8)

            # test settings
            cfg_req = json.dumps({"print_speed": 7, "post_print_duration_minutes": 25}).encode("utf-8")
            status, body = self.ask("/api/settings", method="POST", body=cfg_req, headers={"Content-Type": "application/json"})
            self.assertEqual(status, 200)
            res = json.loads(body.decode("utf-8"))
            self.assertTrue(res["ok"])
            self.assertEqual(fake_fan.settings["print_speed"], 7)
            self.assertEqual(fake_fan.settings["post_print_duration_minutes"], 25)
        finally:
            self.mod.FAN = None

    def test_everything_else_is_refused(self):
        bad = ["/config.json", "/../config.json", "/%2e%2e/config.json", "/..%2fconfig.json", "/..%2Fconfig.json",
               "/status-2026-10-11.csv/../../config.json", "/status-2026-10-11.csv/..%2f..%2fconfig.json",
               "/logger.log", "/../logger.log", "/secret.txt", "/h2d_logger.py", "/../h2d_logger.py", "//etc/passwd",
               "/etc/passwd", "/status-../../config.json", "/STATUS-2026-10-11.CSV", "/status-2026-10-11.csv%00",
               "/status-2026-10-11.csv.bak", "/status-2026-10-1.csv", "/logs/status-2026-10-11.csv", "/.", "/..",
               "/%2e%2e%2f%2e%2e%2fconfig.json", "/?file=../config.json", "/status-2026-10-11.csv/"]
        for path in bad:
            for method in ("GET", "HEAD"):
                status, body = self.ask(path, method)
                if path in ("/.", "/?file=../config.json", "/status-2026-10-11.csv/") and status == 200:
                    self.assertNotIn(b"TOPSECRET", body, path)              # resolves to the listing or the CSV itself
                    continue
                self.assertEqual(status, 404, (method, path))
                self.assertNotIn(b"TOPSECRET", body, path)

    def test_no_writing(self):
        for method in ("POST", "PUT", "DELETE"):
            status, body = self.ask("/status-2026-10-11.csv", method)
            self.assertEqual(status, 501, method)
        self.assertEqual(self.ask("/status-2026-10-11.csv"), (200, b"a,b\n1,2\n"))


class MainLoop(Base):
    def start_main(self, http_port):
        calls, block = [], threading.Event()

        def fake_session(cfg):
            calls.append(dict(cfg))
            if len(calls) <= 2:
                raise RuntimeError("boom %d" % len(calls))
            block.wait()
        self.mod.session = fake_session
        self.mod.HTTP_PORT = http_port
        self.mod.time = types.SimpleNamespace(time=time.time, sleep=lambda s: time.sleep(0.02))
        threading.Thread(target=self.mod.main, daemon=True).start()
        return calls

    def wait(self, cond, what, seconds=5):
        end = time.time() + seconds
        while time.time() < end:
            if cond():
                return
            time.sleep(0.02)
        self.fail("timed out waiting for " + what)

    def free_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def test_waits_for_config_then_retries_forever(self):
        with open(os.path.join(self.tmp, "logger.log"), "w") as f:
            f.write("x" * 1100000)
        port = self.free_port()
        calls = self.start_main(port)
        self.wait(lambda: any("waiting for config.json" in m for m in self.mod.said), "the waiting message")
        time.sleep(0.2)
        self.assertEqual(calls, [])                                         # no connection attempt without a config
        self.assertEqual(os.path.getsize(os.path.join(self.tmp, "logger.log")), 0)   # oversized service log was emptied
        with open(os.path.join(self.tmp, "config.json"), "w") as f:
            json.dump({"ip": "10.0.0.9", "serial": SERIAL, "access_code": SECRET}, f)
        self.wait(lambda: len(calls) >= 3, "three connection attempts")
        self.assertEqual(calls[0], {"ip": "10.0.0.9", "serial": SERIAL, "access_code": SECRET})
        said = " | ".join(self.mod.said)
        self.assertIn("disconnected: boom 1; retrying", said)
        self.assertIn("disconnected: boom 2; retrying", said)
        self.assertNotIn(SECRET, said)
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/")
        self.assertEqual(c.getresponse().status, 200)
        c.close()

    def test_fan_control_only_when_set_up(self):
        with open(os.path.join(self.tmp, "config.json"), "w") as f:
            json.dump({"ip": "10.0.0.9", "serial": SERIAL, "access_code": SECRET}, f)
        calls = self.start_main(self.free_port())
        self.wait(lambda: len(calls) >= 1, "a connection attempt")
        self.assertIsNone(self.mod.FAN)
        self.assertTrue(any("exhaust fan control off" in m for m in self.mod.said))

    def test_fan_control_on_when_set_up(self):
        with open(os.path.join(self.tmp, "config.json"), "w") as f:
            json.dump({"ip": "10.0.0.9", "serial": SERIAL, "access_code": SECRET}, f)
        os.makedirs(os.path.join(self.tmp, "fan-venv", "bin"))
        for p in ("fan-venv/bin/python", "fan.py", "fan.json"):
            open(os.path.join(self.tmp, p), "w").close()
        calls = self.start_main(self.free_port())
        self.wait(lambda: len(calls) >= 1, "a connection attempt")
        self.assertEqual(self.mod.FAN.cmd, [os.path.join(self.tmp, "fan-venv", "bin", "python"), os.path.join(self.tmp, "fan.py")])
        self.assertEqual(self.mod.FAN.path, os.path.join(self.tmp, "fan-state.json"))
        self.assertTrue(any("exhaust fan control on" in m for m in self.mod.said))

    def test_runs_even_if_the_log_port_is_taken(self):
        busy = socket.socket()
        busy.bind(("0.0.0.0", 0))
        busy.listen(1)
        with open(os.path.join(self.tmp, "config.json"), "w") as f:
            json.dump({"ip": "10.0.0.9", "serial": SERIAL, "access_code": SECRET}, f)
        calls = self.start_main(busy.getsockname()[1])
        self.wait(lambda: len(calls) >= 3, "three connection attempts")
        self.assertTrue(any("log server not started" in m for m in self.mod.said))
        busy.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
