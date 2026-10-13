"""Bliss shutter of the GE ISOVOLT / ISO-DEBYEFLEX 3003 X-ray generator.

Replaces spec's ``shopen`` / ``shclose`` (``/usr/local/lib/spec.d/site.mac``):

- open:  move the absorber (``a2``) to its maximum (6.0), wait, send ``OS:<n>``
- close: send ``CS:<n>``

Protocol from the *Bedienungsanleitung ISO-DEBYEFLEX 3003*, chapter 7
(Schnittstellen-Beschreibung, pp. 47-54, scan in ``docs/``): ASCII commands
terminated by CR; requested values come back as ``*`` + 10 decimal digits +
CR. ``OS:``/``CS:`` have no reply (factory setting "ohne Fehlercode").

The shutter ("Fenster") state is read from the status words:
``SR:03`` holds shutters 1 and 2, ``SR:04`` shutters 3 and 4, four bits each
(high nibble = first shutter of the word):

    bit 3  command        0 = closed, 1 = open
    bit 2  status         0 = closed, 1 = open
    bit 1  closed unplanned (0 = no, 1 = yes)
    bit 0  connected      0 = yes, 1 = no

Example YAML::

    - plugin: bliss
      package: isovolt_driver.bliss_isovolt
      class: IsovoltShutter
      name: shutter
      serial:
        url: /dev/ttyS7
        baudrate: 9600
      shutter: 1                 # generator shutter number 1..4
      absorber: a2               # axis name (not a $reference): moved before opening
      absorber_position: 6.0     # maximum attenuation
      absorber_tolerance: 0.01   # refuse to open if the absorber is further away
      move_timeout: 5.0          # s to wait for the generator to confirm open/close
      # interlock_codes: [43, 46, 63, 64, 65, 112]   # SR:12 codes that refuse open()
      door_check: doors          # object name; open() refuses unless it reports all doors closed

``open()`` first checks the generator's current message (SR:12): a door
contact (63/64/65), emergency stop (46), external stop (43) or open shutter
safety circuit (112) refuses the open before the absorber moves.
"""

from __future__ import annotations

import time
from contextlib import contextmanager

import gevent
import gevent.lock

from bliss import global_map
from bliss.comm.exceptions import CommunicationTimeout
from bliss.comm.util import SERIAL, get_comm
from bliss.common.base_shutter import BaseShutter, BaseShutterState
from bliss.common.logtools import log_debug, log_info

# Status word 12 / ER message codes relevant to the shutter (manual p. 54)
SHUTTER_MESSAGES = {
    43: "external stop",
    46: "emergency stop (NOT-AUS)",
    63: "door contacts 1 and 2 open",
    64: "door contact 1 open",
    65: "door contact 2 open",
    96: "shutter closed unexpectedly",
    97: "shutter not connected",
    98: "shutter did not open",
    99: "shutter did not close",
    112: "shutter safety circuit open",
}


class GeneratorError(RuntimeError):
    """The generator did not do what was asked, or answered unexpectedly."""


class InterlockError(GeneratorError):
    """open() refused: the generator reports a door open, an emergency stop or
    an open shutter safety circuit."""


# Message codes (SR:12, manual p. 54) that refuse open(): doors, emergency
# stop, external stop, shutter safety circuit.
INTERLOCK_CODES = (43, 46, 63, 64, 65, 112)


class IsovoltLine:
    """The generator's serial line: one port and one lock, shared by every
    object talking to the generator (shutter, generator control)."""

    def __init__(self, name: str, config):
        self.name = name
        serial_cfg = config.get("serial", {}) or {}
        self.url = str(serial_cfg.get("url", ""))
        self._baudrate = int(serial_cfg.get("baudrate", 9600))
        if self.url.startswith("/"):
            # Local device: pyserial, opened at the first command (see _port).
            # bliss.comm's Serial lost the first bytes of replies from the
            # ViX drives on this instrument (2026-10-06); pyserial never did.
            self.comm = None
        else:  # ser2net://, rfc2217://, tango://
            self.comm = get_comm(config, ctype=SERIAL, baudrate=9600, eol="\r", timeout=1.0)
        self.lock = gevent.lock.RLock()

    def _port(self):
        import serial

        if self.comm is None:
            self.comm = serial.Serial(self.url, self._baudrate, timeout=0.05)
        return self.comm

    def write(self, command: str) -> None:
        data = f"{command}\r".encode()
        with self.lock:
            if self.url.startswith("/"):
                port = self._port()
                port.reset_input_buffer()
                port.write(data)
            else:
                self.comm.flush()
                self.comm.write(data)
        log_debug(self, "sent %r", command)

    def query_text(self, command: str, timeout: float = 1.0) -> str:
        data = f"{command}\r".encode()
        with self.lock:
            if self.url.startswith("/"):
                port = self._port()
                port.reset_input_buffer()
                port.write(data)
                reply, deadline = b"", time.monotonic() + timeout
                while b"\r" not in reply:
                    if time.monotonic() >= deadline:
                        raise GeneratorError(f"{self.name}: no reply from the generator to {command}"
                                             + (f" (got {reply!r})" if reply else ""))
                    chunk = port.read(1)  # returns as soon as a byte arrives
                    if chunk and port.in_waiting:
                        chunk += port.read(port.in_waiting)
                    reply += chunk
                reply = reply.split(b"\r", 1)[0]
            else:
                self.comm.flush()
                try:
                    reply = self.comm.write_readline(data, eol=b"\r", timeout=timeout)
                except CommunicationTimeout as exc:
                    raise GeneratorError(f"{self.name}: no reply from the generator to {command}") from exc
        text = reply.decode(errors="replace").strip()
        log_debug(self, "%r -> %r", command, text)
        return text

    def query_value(self, command: str) -> int:
        text = self.query_text(command)
        if not text.startswith("*") or not text[1:].strip().isdigit():
            raise GeneratorError(f"{self.name}: unexpected reply to {command}: {text!r}")
        return int(text[1:])

    def status_word(self, number: int) -> int:
        """Status word ``SR:<nn>`` (manual 7.5)."""
        return self.query_value(f"SR:{number:02d}")

    def message(self) -> str:
        """Current message (``ER``); the real reply looks like "* No message available"."""
        return self.query_text("ER").lstrip("*").strip()


class ShutterBits:
    """The four status bits of one shutter."""

    def __init__(self, nibble: int):
        self.commanded_open = bool(nibble & 0b1000)
        self.open = bool(nibble & 0b0100)
        self.closed_unplanned = bool(nibble & 0b0010)
        self.connected = not nibble & 0b0001

    def __repr__(self):
        return (f"ShutterBits(commanded_open={self.commanded_open}, open={self.open}, "
                f"closed_unplanned={self.closed_unplanned}, connected={self.connected})")


class IsovoltShutter(BaseShutter):
    def __init__(self, name, config):
        self._name = name
        self._config = config
        # Either the line of a generator object (shared port) or an own line.
        self._generator_name = config.get("generator")
        if not isinstance(self._generator_name, (str, type(None))):
            raise ValueError(f"{name}: 'generator' must be an object name, not a $reference")
        self._own_line = None if self._generator_name else IsovoltLine(name, config)
        self._number = int(config.get("shutter", 1))
        if not 1 <= self._number <= 4:
            raise ValueError(f"{name}: shutter must be 1..4")
        self._absorber_name = config.get("absorber")
        if not isinstance(self._absorber_name, (str, type(None))):
            raise ValueError(f"{name}: 'absorber' must be an axis name (e.g. a2), not a $reference")
        self._absorber_position = float(config.get("absorber_position", 6.0))
        self._absorber_tolerance = float(config.get("absorber_tolerance", 0.01))
        self._move_timeout = float(config.get("move_timeout", 5.0))
        self._interlock_codes = tuple(int(c) for c in config.get("interlock_codes", INTERLOCK_CODES))
        # Name of a door object with check() (e.g. the PNOZ reader), consulted
        # before anything moves; refusing also when the doors cannot be read.
        self._door_check = config.get("door_check")
        if not isinstance(self._door_check, (str, type(None))):
            raise ValueError(f"{name}: 'door_check' must be an object name, not a $reference")
        global_map.register(self, parents_list=["shutters"])

    # -- generator protocol ------------------------------------------------

    @property
    def _line(self) -> IsovoltLine:
        if self._own_line is not None:
            return self._own_line
        from bliss.config.static import get_config

        return get_config().get(self._generator_name).line

    def _write(self, command: str) -> None:
        self._line.write(command)

    def _query_text(self, command: str, timeout: float = 1.0) -> str:
        return self._line.query_text(command, timeout)

    def _query_value(self, command: str) -> int:
        return self._line.query_value(command)

    def status_word(self, number: int) -> int:
        """Status word ``SR:<nn>`` (manual 7.5)."""
        return self._line.status_word(number)

    def message(self) -> str:
        """Current message of the generator (``ER``)."""
        return self._line.message()

    def _bits(self) -> ShutterBits:
        word = self.status_word(3 if self._number <= 2 else 4)
        nibble = (word >> 4) & 0xF if self._number % 2 == 1 else word & 0xF
        return ShutterBits(nibble)

    # -- BaseShutter -------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def config(self):
        return self._config

    @property
    def state(self) -> BaseShutterState:
        bits = self._bits()
        if not bits.connected:
            return BaseShutterState.FAULT
        if bits.open:
            return BaseShutterState.OPEN if bits.commanded_open else BaseShutterState.MOVING
        if bits.commanded_open and not bits.closed_unplanned:
            return BaseShutterState.MOVING
        return BaseShutterState.CLOSED

    @property
    def state_string(self) -> str:
        bits = self._bits()
        if not bits.connected:
            return "Fault: shutter not connected"
        if bits.open:
            return "Open" if bits.commanded_open else "Closing"
        if bits.closed_unplanned:
            return "Closed (closed unexpectedly!)"
        return "Opening" if bits.commanded_open else "Closed"

    def open(self, timeout: float | None = None):
        """Move the absorber to maximum attenuation, open, and wait until the
        generator reports the shutter open (``timeout`` s, default move_timeout)."""
        if not self._bits().connected:
            raise GeneratorError(f"{self.name}: the generator reports shutter {self._number} not connected")
        self.check_doors()
        self.check_interlock()
        self._absorber_to_maximum()
        self._write(f"OS:{self._number}")
        try:
            self._wait(True, timeout)
        except BaseException:
            # Withdraw the open command: otherwise the generator may still
            # open later (e.g. once the door is closed).
            self._write(f"CS:{self._number}")
            raise
        log_info(self, "%s: open", self.name)

    def check_doors(self) -> None:
        """Raise InterlockError unless the door object reports every door
        closed. The generator itself cannot tell (see check_interlock), and a
        blocked open attempt makes it switch HV off."""
        if not self._door_check:
            return
        from bliss.config.static import get_config

        try:
            get_config().get(self._door_check).check()
        except Exception as exc:
            raise InterlockError(f"{self.name}: NOT opened: {exc}") from exc

    def check_interlock(self) -> None:
        """Raise InterlockError if the generator's current message (SR:12) is a
        door contact, emergency stop, external stop or shutter safety circuit.

        The generator shows one message at a time, and it does NOT report an
        open door before an open attempt (tested 2026-10-06): this only
        catches standing messages. With a door open, OS makes the generator's
        safety circuit refuse the shutter and switch HV off.
        """
        code = self.status_word(12)
        if code in self._interlock_codes:
            text = self.message()
            known = SHUTTER_MESSAGES.get(code, "")
            raise InterlockError(
                f"{self.name}: NOT opened: generator reports {text!r} (code {code}"
                + (f" = {known}" if known else "") + ")"
            )

    def close(self, timeout: float | None = None):
        """Close (always sent, whatever the state) and wait until the generator
        reports it closed."""
        self._write(f"CS:{self._number}")
        self._wait(False, timeout)
        log_info(self, "%s: closed", self.name)

    def _wait(self, want_open: bool, timeout: float | None) -> None:
        timeout = self._move_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while True:
            if self._bits().open == want_open:
                return
            if time.monotonic() >= deadline:
                break
            gevent.sleep(0.1)
        word = "open" if want_open else "close"
        raise GeneratorError(f"{self.name}: shutter {self._number} did not {word} within {timeout:g} s "
                             f"(generator: {self._explain()})")

    def _explain(self) -> str:
        try:
            code = self.status_word(12)
            text = self.message()
        except GeneratorError as exc:
            return f"cannot read the message: {exc}"
        known = SHUTTER_MESSAGES.get(code)
        return f"{text!r}, code {code}" + (f" = {known}" if known else "")

    @property
    @contextmanager
    def open_context(self):
        """Open for the duration of the block, then always close (also on
        errors and Ctrl-C), whatever the state was before."""
        try:
            self.open()
            yield
        finally:
            self.close()

    @property
    @contextmanager
    def closed_context(self):
        """Close for the duration of the block; re-open afterwards only if
        the shutter was open before."""
        was_open = self.is_open
        try:
            self.close()
            yield
        finally:
            if was_open:
                self.open()

    def __info__(self):
        lines = [f"{self.name}: ISOVOLT 3003 shutter {self._number} on {self._line.url or self._line.comm}"]
        try:
            lines.append(f"  state: {self.state_string}")
            hv = bool(self.status_word(1) & 0x40)
            lines.append(f"  high voltage: {'ON' if hv else 'off'}")
            if hv:
                kv = self._query_value("VA") / 1000  # V
                ma = self._query_value("CA") / 1000  # uA
                lines.append(f"  actual: {kv:g} kV, {ma:g} mA")
            lines.append(f"  generator message: {self.message()!r}")
        except GeneratorError as exc:
            lines.append(f"  generator not readable: {exc}")
        if self._absorber_name:
            lines.append(f"  open() first moves {self._absorber_name} to {self._absorber_position:g}")
        return "\n".join(lines)

    # -- absorber ----------------------------------------------------------

    def _absorber(self):
        from bliss.config.static import get_config

        return get_config().get(self._absorber_name)

    def _absorber_to_maximum(self) -> None:
        if not self._absorber_name:
            return
        absorber = self._absorber()
        target = self._absorber_position
        if abs(absorber.position - target) > self._absorber_tolerance:
            print(f"Moving {absorber.name} to {target:g} (maximum attenuation) before opening")
            absorber.move(target)
        position = absorber.position
        if abs(position - target) > self._absorber_tolerance:
            raise RuntimeError(
                f"{self.name}: {absorber.name} is at {position:g}, not {target:g}; shutter NOT opened"
            )
