"""Control of the GE ISO-DEBYEFLEX 3003 X-ray generator from Bliss.

Replaces spec's generator macros (``/usr/local/lib/spec.d/site.mac``):

=====================  ===========================  ==========================
spec                   sends                        here
=====================  ===========================  ==========================
GEIT_gen_hv 1 / 0      HV:1 / HV:0                  hv_on() / hv_off()
GEIT_gen_set_v x       SV:x                         ramp_to(kv, ma) (steps)
GEIT_gen_set_c x       SC:x                         ramp_to(kv, ma) (steps)
generator_off          SC 25..10, SV 30..10, HV:0   generator_off()
                       (30 s per step)
GEIT_gen_soft_start    WU:x,yy                      warmup(pause, kv)
=====================  ===========================  ==========================

Protocol from the *Bedienungsanleitung ISO-DEBYEFLEX 3003*, chapter 7
(``docs/ISO-DEBYEFLEX_3003_Schnittstelle_p47-54.pdf``). Values are sent as
two digits ("05", not "5"). VN/VA are in V, CN/CA in uA.

Example YAML::

    - plugin: bliss
      package: isovolt_driver.generator
      class: IsovoltGenerator
      name: generator
      serial:
        url: /dev/ttyS7
        baudrate: 9600
      max_kv: 40            # ramp_to() refuses more (tube / operating limits)
      max_ma: 30
      max_power: 1200       # W
      min_kv: 10            # lowest setpoint used (spec's generator_off end point)
      min_ma: 10
      step_kv: 10           # ramp steps and dwell, as spec's generator_off
      step_ma: 5
      step_time: 30         # s

The shutter shares this object's serial line (shutter config: ``generator:
generator``), so commands of both never interleave.
"""

from __future__ import annotations

import time

import gevent

from bliss import global_map
from bliss.common.logtools import log_info

from .bliss_isovolt import GeneratorError, IsovoltLine

ANODES = {1: "Ti", 2: "Cr", 3: "Fe", 4: "Co", 5: "Cu", 6: "Mo", 7: "Ag", 8: "W", 9: "Au"}


def ramp_steps(kv0: int, ma0: int, kv1: int, ma1: int, step_kv: int, step_ma: int) -> list[tuple[str, int]]:
    """Setpoint commands to go from (kv0, ma0) to (kv1, ma1), as spec does it.

    Current goes down first, then the voltage moves (either way), then the
    current goes up: spec's generator_off is SC 25, 20, 15, 10 then
    SV 30, 20, 10. Intermediate values are multiples of the step; the
    target is always the last value.
    """
    def path(a: int, b: int, step: int) -> list[int]:
        if a == b:
            return []
        out = []
        if b < a:
            v = (a - 1) // step * step  # first multiple of step below a
            while v > b:
                out.append(v)
                v -= step
        else:
            v = (a // step + 1) * step
            while v < b:
                out.append(v)
                v += step
        return out + [b]

    steps = []
    if ma1 < ma0:
        steps += [("SC", v) for v in path(ma0, ma1, step_ma)]
    steps += [("SV", v) for v in path(kv0, kv1, step_kv)]
    if ma1 > ma0:
        steps += [("SC", v) for v in path(ma0, ma1, step_ma)]
    return steps


class IsovoltGenerator:
    def __init__(self, name, config):
        self.name = name
        self._config = config
        self.line = IsovoltLine(name, config)
        get = lambda key, default: int(config.get(key, default))  # noqa: E731
        self.max_kv, self.max_ma = get("max_kv", 40), get("max_ma", 30)
        self.max_power = get("max_power", 1200)
        self.min_kv, self.min_ma = get("min_kv", 10), get("min_ma", 10)
        self.step_kv, self.step_ma = get("step_kv", 10), get("step_ma", 5)
        self.step_time = float(config.get("step_time", 30))
        global_map.register(self, parents_list=["controllers"])

    @property
    def config(self):
        return self._config

    # -- readings -------------------------------------------------------------

    def status_word(self, number: int) -> int:
        return self.line.status_word(number)

    def message(self) -> str:
        return self.line.message()

    @property
    def hv_is_on(self) -> bool:
        return bool(self.line.status_word(1) & 0x40)

    @property
    def kv(self) -> float:
        """Actual voltage (VA), kV."""
        return self.line.query_value("VA") / 1000

    @property
    def ma(self) -> float:
        """Actual current (CA), mA."""
        return self.line.query_value("CA") / 1000

    @property
    def kv_setpoint(self) -> int:
        return round(self.line.query_value("VN") / 1000)

    @property
    def ma_setpoint(self) -> int:
        return round(self.line.query_value("CN") / 1000)

    @property
    def anode(self) -> str:
        code = self.line.query_value("RM")
        return ANODES.get(code, f"code {code}")

    @property
    def water_flow(self) -> int:
        """Cooling water flow (SR:15), Hz."""
        return self.line.status_word(15)

    def __info__(self):
        try:
            w1 = self.line.status_word(1)
            lines = [
                f"{self.name}: ISO-DEBYEFLEX 3003 on {self.line.url}",
                f"  high voltage: {'ON' if w1 & 0x40 else 'off'}",
                f"  actual {self.kv:g} kV, {self.ma:g} mA   (setpoints {self.kv_setpoint} kV, {self.ma_setpoint} mA"
                + (", kV not at setpoint" if w1 & 0x04 else "") + (", mA not at setpoint" if w1 & 0x08 else "") + ")",
                f"  anode {self.anode}, water flow {self.water_flow} Hz"
                + ("  COOLING NOT OK" if w1 & 0x20 else ""),
                f"  message: {self.message()!r}",
                f"  limits: {self.max_kv} kV, {self.max_ma} mA, {self.max_power} W;"
                f" ramp {self.step_kv} kV / {self.step_ma} mA per {self.step_time:g} s",
            ]
        except GeneratorError as exc:
            lines = [f"{self.name}: generator not readable: {exc}"]
        return "\n".join(lines)

    # -- commands -------------------------------------------------------------

    def _check(self, kv: int, ma: int) -> None:
        if not (self.min_kv <= kv <= self.max_kv and self.min_ma <= ma <= self.max_ma):
            raise ValueError(f"{self.name}: {kv} kV / {ma} mA outside the allowed range "
                             f"{self.min_kv}..{self.max_kv} kV, {self.min_ma}..{self.max_ma} mA")
        if kv * ma > self.max_power:
            raise ValueError(f"{self.name}: {kv} kV x {ma} mA = {kv * ma} W exceeds max_power {self.max_power} W")

    def ramp_to(self, kv: int, ma: int, step_time: float | None = None) -> None:
        """Ramp the setpoints to ``kv``/``ma`` in steps (spec's order and step
        sizes), waiting ``step_time`` s after each step. HV must be on.
        Ctrl-C stops the ramp at the last setpoint sent."""
        kv, ma = int(kv), int(ma)
        self._check(kv, ma)
        if not self.hv_is_on:
            raise GeneratorError(f"{self.name}: high voltage is off; switch it on first (hv_on())")
        dwell = self.step_time if step_time is None else float(step_time)
        steps = ramp_steps(self.kv_setpoint, self.ma_setpoint, kv, ma, self.step_kv, self.step_ma)
        if not steps:
            print(f"{self.name}: already at {kv} kV / {ma} mA")
            return
        print(f"{self.name}: ramping to {kv} kV / {ma} mA in {len(steps)} steps, {dwell:g} s each")
        for i, (cmd, value) in enumerate(steps, 1):
            self.line.write(f"{cmd}:{value:02d}")
            print(f"  {i}/{len(steps)}  {'voltage' if cmd == 'SV' else 'current'} -> {value} "
                  f"{'kV' if cmd == 'SV' else 'mA'}")
            gevent.sleep(dwell)
        w1 = self.line.status_word(1)
        if w1 & 0x0C:
            print(f"  warning: generator reports {'kV' if w1 & 4 else ''}{' and ' if w1 & 12 == 12 else ''}"
                  f"{'mA' if w1 & 8 else ''} not yet at setpoint (actual {self.kv:g} kV, {self.ma:g} mA)")
        log_info(self, "%s: at %d kV / %d mA", self.name, kv, ma)

    def hv_on(self, timeout: float = 10.0) -> None:
        """spec GEIT_gen_hv 1: switch high voltage on and wait until the
        generator reports it on. The setpoints are whatever was set before."""
        self.line.write("HV:1")
        deadline = time.monotonic() + timeout
        while not self.hv_is_on:
            if time.monotonic() > deadline:
                raise GeneratorError(f"{self.name}: high voltage did not come on (generator: {self.message()!r})")
            gevent.sleep(0.2)
        print(f"{self.name}: high voltage ON at {self.kv_setpoint} kV / {self.ma_setpoint} mA (setpoints)")

    def hv_off(self) -> None:
        """Switch high voltage off at once (spec GEIT_gen_hv 0). The usual way
        is generator_off(), which ramps down first."""
        self.line.write("HV:0")
        print(f"{self.name}: high voltage OFF")

    def generator_off(self, step_time: float | None = None) -> None:
        """spec generator_off: ramp down to min_kv/min_ma (current first,
        30 s per step), then switch high voltage off."""
        if self.hv_is_on:
            self.ramp_to(self.min_kv, self.min_ma, step_time)
        self.hv_off()

    def warmup(self, pause: int, kv: int) -> None:
        """spec GEIT_gen_soft_start (WU:x,yy): run the generator's own
        conditioning programme. pause: 0 = none, 1 = 1 day, 2 = 2 days,
        3 = a week, 4 = via RTC; kv: test voltage."""
        if not 0 <= int(pause) <= 4:
            raise ValueError("pause must be 0..4 (see manual, WU)")
        if not self.min_kv <= int(kv) <= self.max_kv:
            raise ValueError(f"test voltage {kv} kV outside {self.min_kv}..{self.max_kv} kV")
        self.line.write(f"WU:{int(pause)},{int(kv):02d}")
        print(f"{self.name}: warm-up programme started (WU:{int(pause)},{int(kv):02d}); follow it on the generator")

    def clear_message(self) -> None:
        """CL: acknowledge the current generator message (e.g. after a trip)."""
        self.line.write("CL")
