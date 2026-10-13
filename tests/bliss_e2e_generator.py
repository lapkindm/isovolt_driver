"""End-to-end test of IsovoltGenerator (+ the shutter on the same line).

    python tests/fake_generator.py <link>
    BEACON_HOST=localhost:25000 PYTHONPATH=. python tests/bliss_e2e_generator.py <link>
with a Beacon config = tests/generator_e2e_config.yml (<LINK> replaced).
"""
import os
import sys
import time

import gevent

from bliss.config.static import get_config

LINK = sys.argv[1]
res = []


def check(name, cond, detail=""):
    res.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


def log():
    time.sleep(0.05)
    with open(LINK + ".log") as f:
        return [s.strip() for s in f if s.strip()]


def controls(since=0):
    return [c for c in log()[since:] if c[:3] in ("SV:", "SC:", "HV:", "OS:", "CS:", "CL", "WU:")]


def sim(hook):
    fd = os.open(LINK, os.O_WRONLY | os.O_NOCTTY)
    os.write(fd, f"SIM:{hook}\r".encode())
    os.close(fd)
    time.sleep(0.05)


def raises(fn):
    try:
        fn()
    except BaseException as e:  # noqa: BLE001
        return e
    return None


cfg = get_config()
gen, sh = cfg.get("generator"), cfg.get("shutter")
check("readings", gen.hv_is_on and gen.kv == 40 and gen.ma == 30 and gen.anode == "Cu" and gen.water_flow == 203,
      (gen.hv_is_on, gen.kv, gen.ma, gen.anode, gen.water_flow))
check("info", "high voltage: ON" in gen.__info__() and "anode Cu" in gen.__info__(), gen.__info__().splitlines()[1:3])

n = len(log())
for kv, ma, why in ((45, 30, "allowed range"), (40, 35, "allowed range"), (5, 10, "allowed range")):
    err = raises(lambda: gen.ramp_to(kv, ma, step_time=0.01))
    check(f"ramp_to({kv}, {ma}) refused", isinstance(err, ValueError) and why in str(err), str(err)[:80])
check("nothing sent for refused ramps", controls(n) == [], controls(n))

n = len(log())
gen.generator_off(step_time=0.05)
check("generator_off = spec's sequence", controls(n) == ["SC:25", "SC:20", "SC:15", "SC:10", "SV:30", "SV:20", "SV:10", "HV:0"],
      controls(n))
check("HV off afterwards", not gen.hv_is_on)

err = raises(lambda: gen.ramp_to(40, 30, step_time=0.01))
check("ramp refused with HV off", err is not None and "high voltage is off" in str(err), str(err)[:80])

gen.hv_on()
check("hv_on", gen.hv_is_on and (gen.kv_setpoint, gen.ma_setpoint) == (10, 10))
n = len(log())
gen.ramp_to(40, 30, step_time=0.05)
check("ramp up: voltage first, then current", controls(n) == ["SV:20", "SV:30", "SV:40", "SC:15", "SC:20", "SC:25", "SC:30"],
      controls(n))
time.sleep(0.15)
check("at 40 kV / 30 mA", (gen.kv, gen.ma) == (40, 30), (gen.kv, gen.ma))
check("every value sent as two digits", all(len(c.split(":")[1]) == 2 for c in controls() if c[:3] in ("SV:", "SC:")))

# shutter and generator on one line, concurrently
states = []
g = gevent.spawn(lambda: [states.append(sh.state_string) or gevent.sleep(0.01) for _ in range(40)])
gen.ramp_to(30, 20, step_time=0.05)
g.join()
check("shutter queries during a ramp: no garbled replies", len(states) == 40 and set(states) == {"Closed"}, set(states))
gen.ramp_to(40, 30, step_time=0.05)
sh.open(); sh.close()
check("shutter works through the generator's line", controls()[-2:] == ["OS:1", "CS:1"] and sh.is_closed)

sim("door=2")
check("message shown", gen.message() == "TUERKONTAKT 2 GEOEFFNET")
sim("door=0")
sim("door=2"); gen.clear_message()
check("clear_message (CL)", gen.status_word(12) == 0 and controls()[-1] == "CL")
sim("reset")

err = raises(lambda: gen.warmup(5, 30))
check("warmup refuses an invalid pause", isinstance(err, ValueError))

print(f"\n{sum(res)}/{len(res)} passed")
sys.exit(0 if all(res) else 1)
