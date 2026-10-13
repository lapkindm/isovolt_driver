"""End-to-end test of IsovoltShutter with a mockup absorber.

Needs a fresh Beacon whose config is tests/e2e_config.yml with <LINK>
replaced by the fake generator link, and::

    python tests/fake_generator.py <link>
    BEACON_HOST=localhost:25000 PYTHONPATH=. python tests/bliss_e2e.py <link>
"""
import os
import sys
import time

from bliss.common.base_shutter import BaseShutterState as S
from bliss.config.static import get_config

LINK = sys.argv[1]
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


def moves():
    """OS/CS commands received by the generator so far."""
    time.sleep(0.05)
    with open(LINK + ".log") as f:
        return [s for s in (line.strip() for line in f) if s[:3] in ("OS:", "CS:")]


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
sh = cfg.get("shutter")
check("loading the shutter does not create the absorber", "a2" not in cfg._name2instance,
      list(cfg._name2instance))
check("initial state read from SR:03: CLOSED", sh.state == S.CLOSED, sh.state_string)

a2 = cfg.get("a2")
a2.move(1)
sh.open()
check("open moves absorber to 6 first", a2.position == 6.0, a2.position)
check("open sends OS:1 and waits for OPEN", moves() == ["OS:1"] and sh.state == S.OPEN, sh.state_string)

sh.close()
check("close waits for CLOSED", sh.state == S.CLOSED, sh.state_string)
sh.close()
check("close always sent", moves()[-2:] == ["CS:1", "CS:1"], moves())

n = len(moves())
a2.move(2)
a2.limits = (-1, 5)
err = raises(sh.open)
check("no open if absorber cannot reach 6", err is not None and moves()[n:] == [], f"{err!r:.80} sent={moves()[n:]}")
a2.limits = (-1, 7)

for door, code_text in (("2", "TUERKONTAKT 2"), ("both", "TUERKONTAKT 1 UND 2")):
    sim(f"door={door}")
    a2.move(3)
    n = len(moves())
    err = raises(sh.open)
    check(f"door {door} open: open() refused before anything moves",
          type(err).__name__ == "InterlockError" and code_text in str(err) and moves()[n:] == [] and a2.position == 3.0,
          (str(err)[:110], moves()[n:], a2.position))
    sim("reset")
sim("estop=1")
err = raises(sh.open)
check("emergency stop: open() refused", type(err).__name__ == "InterlockError" and "NOT - AUS" in str(err), str(err)[:100])
sim("reset")
check("doors closed again: open() works", raises(sh.open) is None and sh.is_open, sh.state_string)
sh.close()

sim("jam=1")
err = raises(lambda: sh.open(timeout=1))
time.sleep(0.4)
check("shutter that does not open: error with generator message", err is not None and "FENSTER NICHT GEOEFFNET" in str(err),
      str(err)[:120])
check("failed open withdraws the open command", moves()[-2:] == ["OS:1", "CS:1"] and sh.state == S.CLOSED
      and sh.state_string == "Closed", (moves()[-2:], sh.state_string))
sim("reset")

def _closed_ctx_reopen():
    sim("reset")
    sh.open()
    sim("door=2")  # door opened while the shutter is open
    with sh.closed_context:
        pass  # leaving the block re-opens: must be refused


sim("reset")
err = raises(_closed_ctx_reopen)
check("closed_context: re-open refused when a door was opened meanwhile",
      type(err).__name__ == "InterlockError" and sh.is_closed, (str(err)[:90], sh.state_string))
sim("reset")


def _ctx():
    with sh.open_context:
        check("open_context opens", sh.is_open)
        raise KeyboardInterrupt


n = len(moves())
err = raises(_ctx)
check("open_context closes, also on Ctrl-C", isinstance(err, KeyboardInterrupt) and moves()[n:] == ["OS:1", "CS:1"]
      and sh.is_closed, moves()[n:])

sh.open()
n = len(moves())
a2.move(3)
with sh.closed_context:
    check("closed_context closes", sh.is_closed)
check("closed_context re-opens (absorber to 6 again)", moves()[n:] == ["CS:1", "OS:1"] and a2.position == 6.0
      and sh.is_open, (moves()[n:], a2.position))

sim("unplanned=1")
check("unexpected closure reported", sh.state == S.CLOSED and "unexpectedly" in sh.state_string, sh.state_string)
sh.close()
sim("reset")

sim("disconnect=1")
check("not connected -> FAULT", sh.state == S.FAULT, sh.state_string)
n = len(moves())
err = raises(sh.open)
check("open refused when not connected", err is not None and moves()[n:] == [], str(err)[:80])
sim("reset")

info = sh.__info__()
check("info shows HV and kV/mA", "high voltage: ON" in info and "40 kV, 30 mA" in info, info.replace("\n", " | "))

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
