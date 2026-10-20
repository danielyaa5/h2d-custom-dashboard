#!/usr/bin/env python3
"""Switches the 4" inline exhaust fan through Smart Life (Tuya's cloud). h2d_logger.py's FanWatch runs it for ABS, ASA
and PETG prints (user, 2026-10-14, 2026-10-18: fan on when an ABS, ASA or PETG print starts, at speed 5; off again when it ends).

  fan.py login <user code>   one-time sign-in: prints the text for a QR code to scan in Smart Life, waits for approval
  fan.py devices             lists the Smart Life devices and what each one can be set to (never prints keys or tokens)
  fan.py use <device id>     picks the fan, works out its on/off and speed settings and writes fan.json
  fan.py on | off | status   what FanWatch runs; exit code 0 = done (status: 0 = fan online)

The user code is in the Smart Life app under Me > Settings > Account and Security > User Code. Sign-in tokens are kept in
fan-tokens.json (mode 600) and never printed. Needs Tuya's own library, tuya-device-sharing-sdk (the one Home
Assistant's built-in Tuya integration uses), in fan-venv/; the QR sign-in is registered to Home Assistant's client id,
so the app may name Home Assistant when it asks for approval.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOKENS = os.path.join(HERE, "fan-tokens.json")
SETTINGS = os.path.join(HERE, "fan.json")
CLIENT_ID = "HA_3y9q4ak7g4ephrvke"      # Home Assistant's, from homeassistant/components/tuya/const.py
SCHEMA = "haauthorize"
SPEED = 5                               # user, 2026-10-14: "power 7", then "use speed 3 instead"; 2026-10-18: 5 for all prints
SPEED_PURGE = 10                         # user, 2026-10-19: full blast (10) for 15 min after PETG, ABS, ASA prints
SWITCH_CODES = ("switch", "switch_1", "switch_fan", "fan_switch", "power")
SPEED_CODES = ("fan_speed", "fan_speed_enum", "fan_speed_percent", "speed", "level", "fan_level", "gear")


def load(path):
    with open(path) as f:
        return json.load(f)


def save(path, data, private=False):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def keeper(tokens):
    from tuya_sharing import SharingTokenListener

    class Keep(SharingTokenListener):
        def update_token(self, token_info):     # Tuya hands out a new token pair now and then; keep the newest
            tokens["token_info"] = token_info
            save(TOKENS, tokens, private=True)
    return Keep()


def api():
    from tuya_sharing import CustomerApi
    from tuya_sharing.customerapi import CustomerTokenInfo
    tokens = load(TOKENS)
    return CustomerApi(CustomerTokenInfo(tokens["token_info"]), CLIENT_ID, tokens["user_code"], tokens["endpoint"],
                       keeper(tokens))


def check(resp, what):
    if not isinstance(resp, dict) or not resp.get("success"):
        msg = resp.get("msg") if isinstance(resp, dict) else resp
        code = resp.get("code") if isinstance(resp, dict) else ""
        sys.exit("%s failed: %s %s" % (what, code, msg))
    return resp.get("result")


def login(user_code):
    from tuya_sharing import LoginControl
    lc = LoginControl()
    r = lc.qr_code(CLIENT_ID, SCHEMA, user_code)
    token = check(r, "QR code request")["qrcode"]
    print("QR: tuyaSmart--qrLogin?token=%s" % token, flush=True)
    print("Scan it in Smart Life (scan button, top right of Home) and approve. Waiting up to 10 minutes...", flush=True)
    end = time.time() + 600
    while time.time() < end:
        time.sleep(3)
        try:
            ok, info = lc.login_result(token, CLIENT_ID, user_code)
        except Exception as e:                  # network blip; keep waiting
            print("still waiting (%s)" % e, flush=True)
            continue
        if ok:
            save(TOKENS, {"user_code": user_code, "terminal_id": info["terminal_id"], "endpoint": info["endpoint"],
                          "username": info.get("username", ""),
                          "token_info": {k: info[k] for k in ("t", "uid", "expire_time", "access_token", "refresh_token")}},
                 private=True)
            print("signed in as %s" % info.get("username", "?"))
            return
    sys.exit("no approval within 10 minutes; run login again for a new QR code")


def all_devices():
    from tuya_sharing import Manager
    tokens = load(TOKENS)
    m = Manager(CLIENT_ID, tokens["user_code"], tokens["terminal_id"], tokens["endpoint"], tokens["token_info"],
                keeper(tokens))
    m.update_device_cache()
    return list(m.device_map.values())


def describe(d):
    out = ["%s  %s  (category %s, %s, %s)" % (d.id, d.name, d.category, d.product_name, "online" if d.online else "OFFLINE")]
    for code, fn in sorted(d.function.items()):
        out.append("  set %-20s %-8s %s" % (code, fn.type, fn.values))
    for code, value in sorted(d.status.items()):
        out.append("  now %-20s %r" % (code, value))
    return "\n".join(out)


def devices():
    for d in all_devices():
        print(describe(d))


def speed_value(fn, n):
    """The value to send for speed n, in the type the fan's speed setting takes, or None if n doesn't fit it."""
    try:
        values = json.loads(fn.values) if isinstance(fn.values, str) else (fn.values or {})
    except ValueError:
        values = {}
    if fn.type == "Enum":                       # "7", or "level_7" style
        hits = [v for v in values.get("range", []) if str(v) == str(n) or str(v).endswith("_%d" % n)]
        return hits[0] if len(hits) == 1 else None
    if fn.type == "Integer":
        lo, hi, scale = values.get("min", 0), values.get("max", 0), values.get("scale", 0)
        if not scale and lo <= n <= hi <= 10:   # a 1..10 (or 0..10) speed scale
            return n
    return None


def use(device_id):
    found = [d for d in all_devices() if d.id == device_id]
    if not found:
        sys.exit("no device %s in this Smart Life account; see fan.py devices" % device_id)
    d = found[0]
    print(describe(d))
    switch = next((c for c in SWITCH_CODES if c in d.function and d.function[c].type == "Boolean"), None)
    speed = next((c for c in SPEED_CODES if c in d.function), None)
    value = speed_value(d.function[speed], SPEED) if speed else None
    if not switch or value is None:
        sys.exit("could not work out the on/off (%s) or speed %d (%s) setting; write fan.json by hand" % (switch, SPEED, speed))
    save(SETTINGS, {"device_id": d.id, "name": d.name, "switch": switch, "speed_code": speed, "speed_on": value})
    print("fan.json: %s on/off = %s, speed %s = %r" % (d.name, switch, speed, value))


def send(commands):
    s = load(SETTINGS)
    check(api().post("/v1.1/m/thing/%s/commands" % s["device_id"], None, {"commands": commands}), "fan command")
    print("sent %s" % json.dumps(commands))


def status():
    s = load(SETTINGS)
    result = check(api().get("/v1.0/m/life/ha/devices/detail", {"devIds": s["device_id"]}), "status") or []
    if not result:
        sys.exit("fan %s not found in the account" % s["device_id"])
    d = result[0]
    now = {x.get("code"): x.get("value") for x in d.get("status", []) if isinstance(x, dict)}
    print("%s %s: %s = %r, %s = %r" % (s["name"], "online" if d.get("online") else "OFFLINE", s["switch"], now.get(s["switch"]),
                                      s["speed_code"], now.get(s["speed_code"])))
    if not d.get("online"):
        sys.exit(2)


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "login" and len(argv) == 3:
        login(argv[2])
    elif cmd == "devices":
        devices()
    elif cmd == "use" and len(argv) == 3:
        use(argv[2])
    elif cmd == "on":
        s = load(SETTINGS)
        speed = int(argv[2]) if len(argv) > 2 else s.get("speed_on", SPEED)
        send([{"code": s["switch"], "value": True}, {"code": s["speed_code"], "value": speed}])
    elif cmd == "off":
        s = load(SETTINGS)
        send([{"code": s["switch"], "value": False}])
    elif cmd == "status":
        status()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
