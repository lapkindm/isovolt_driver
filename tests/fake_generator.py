"""Fake ISO-DEBYEFLEX 3003 generator on a pseudo-terminal (manual chapter 7).

Usage: python tests/fake_generator.py LINK

Records each CR-terminated command to LINK + ".log" (one per line).
Implements OS/CS (shutters open/close after a short delay), SR:01/03/04/12,
ER, VA, CA. Test hooks (not logged): ``SIM:door=2`` (door contact 2 open,
code 65; also ``door=1``, ``door=both``; shutters cannot open), ``SIM:estop=1``,
``SIM:jam=1`` (the shutter does not open, code 98), ``SIM:unplanned=N`` (shutter N closes unexpectedly),
``SIM:trip`` (safety circuit: open shutters close unexpectedly, HV off, code 63),
``SIM:disconnect=N``, ``SIM:reset`` (everything back to normal).
"""
import os
import sys
import termios
import time
import tty

MOVE_TIME = 0.3
MESSAGES = {0: "* No message available", 46: "NOT - AUS", 63: "TUERKONTAKT 1 UND 2",
            65: "TUERKONTAKT 2 GEOEFFNET", 96: "FENSTER UNPLANMAESSIG ZUGEFALLEN",
            98: "FENSTER NICHT GEOEFFNET"}


class Generator:
    def __init__(self):
        self.reset()

    def reset(self):
        self.cmd = [0] * 5  # index 1..4
        self.status = [0] * 5
        self.unplanned = [0] * 5
        self.disconnected = [0] * 5
        self.pending = {}  # shutter -> (time, new status)
        self.door_open = False
        self.jammed = False  # shutter mechanism does not open (code 98)
        self.code = 0
        self.hv = True
        self.kv_set, self.ma_set = 40, 30
        self.kv_act, self.ma_act = 40.0, 30.0
        self.changed = 0.0  # time of the last setpoint/HV change (actual values follow after LAG)
        self.bad_format = []  # commands with a value not sent as two digits

    def update(self):
        now = time.monotonic()
        for n, (t, value) in list(self.pending.items()):
            if now >= t:
                del self.pending[n]
                if value and (self.door_open or self.disconnected[n] or self.jammed):
                    if not self.door_open:
                        self.code = 98 if self.jammed else 97
                    continue
                self.status[n] = value

    LAG = 0.1

    def actual(self):
        if time.monotonic() - self.changed < self.LAG:
            return self.kv_act, self.ma_act
        self.kv_act, self.ma_act = (self.kv_set, self.ma_set) if self.hv else (0.0, 0.0)
        return self.kv_act, self.ma_act

    def word(self, number):
        if number == 1:
            kv, ma = self.actual()
            w = 0x40 if self.hv else 0
            if self.hv and kv != self.kv_set:
                w |= 0x04
            if self.hv and ma != self.ma_set:
                w |= 0x08
            return w
        if number == 15:
            return 203
        if number in (3, 4):
            first, second = (1, 2) if number == 3 else (3, 4)
            nib = lambda n: self.cmd[n] << 3 | self.status[n] << 2 | self.unplanned[n] << 1 | self.disconnected[n]  # noqa: E731
            return nib(first) << 4 | nib(second)
        if number == 12:
            return self.code
        return 0

    def handle(self, line: str):
        self.update()
        cmd, _, arg = line.strip().upper().partition(":")
        if cmd == "OS":
            n = int(arg)
            self.cmd[n], self.unplanned[n] = 1, 0
            self.pending[n] = (time.monotonic() + MOVE_TIME, 1)
        elif cmd == "CS":
            n = int(arg)
            self.cmd[n] = 0
            self.pending[n] = (time.monotonic() + MOVE_TIME, 0)
        elif cmd == "SR" and len(arg) == 2:
            return f"*{self.word(int(arg)):010d}"
        elif cmd == "ER":
            return MESSAGES.get(self.code, f"MELDUNG {self.code}")
        elif cmd == "VA":
            return f"*{round(self.actual()[0] * 1000):010d}"
        elif cmd == "CA":
            return f"*{round(self.actual()[1] * 1000):010d}"
        elif cmd == "VN":
            return f"*{self.kv_set * 1000:010d}"
        elif cmd == "CN":
            return f"*{self.ma_set * 1000:010d}"
        elif cmd == "RM":
            return f"*{5:010d}"
        elif cmd in ("SV", "SC"):
            if len(arg) != 2 or not arg.isdigit():
                self.bad_format.append(line)
                return None
            self.actual()
            if cmd == "SV":
                self.kv_set = int(arg)
            else:
                self.ma_set = int(arg)
            self.changed = time.monotonic()
        elif cmd == "HV":
            self.actual()
            self.hv = arg == "1"
            self.changed = time.monotonic()
        elif cmd == "CL":
            self.code = 0
        return None

    def sim(self, hook: str):
        key, _, value = hook.partition("=")
        if key == "door":  # 1, 2 or both
            self.door_open = value in ("1", "2", "both")
            self.code = {"1": 64, "2": 65, "both": 63}.get(value, 0)
        elif key == "estop":
            self.door_open = value == "1"  # blocks opening like a door
            self.code = 46 if value == "1" else 0
        elif key == "jam":
            self.jammed = value == "1"
        elif key == "unplanned":
            n = int(value)
            self.status[n], self.unplanned[n], self.code = 0, 1, 96
        elif key == "trip":  # safety circuit broken: as the real generator (2026-10-06)
            for n in range(1, 5):
                if self.status[n]:
                    self.status[n], self.unplanned[n] = 0, 1
            self.hv, self.code = False, 63
            self.changed = time.monotonic()
        elif key == "disconnect":
            self.disconnected[int(value)] = 1
        elif key == "reset":
            self.reset()


def main():
    link = sys.argv[1]
    master, slave = os.openpty()
    tty.setraw(slave)
    attrs = termios.tcgetattr(slave)
    attrs[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(os.ttyname(slave), link)
    log = open(link + ".log", "w", buffering=1)
    gen = Generator()
    buf = b""
    while True:
        buf += os.read(master, 1024)
        while b"\r" in buf:
            raw, buf = buf.split(b"\r", 1)
            line = raw.decode(errors="replace")
            if line.startswith("SIM:"):
                gen.sim(line[4:])
                continue
            log.write(line + "\n")
            reply = gen.handle(line)
            if reply is not None:
                os.write(master, reply.encode() + b"\r")


if __name__ == "__main__":
    main()
