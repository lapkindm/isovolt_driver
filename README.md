# isovolt-driver

Bliss shutter and generator control for the GE ISO-DEBYEFLEX (ISOVOLT) 3003
X-ray generator of the XRD-3003TT. It replaces spec's `shopen`, `shclose`,
`generator_on`, `generator_off` and the `GEIT_gen_*` macros from
`/usr/local/lib/spec.d/site.mac`.

```python
shutter.open()               # a2 to 6.0 (max attenuation), OS:1, wait until open
shutter.close()              # CS:1 (always sent), wait until closed
with shutter.open_context:   # open for the block, always closed afterwards
    loopscan(10, 1, mythen)
shutter                      # state, high voltage, kV/mA, generator message
```

Config: [bliss_config/xrd3003tt_shutter.yml](bliss_config/xrd3003tt_shutter.yml).
Install with `pip install -e .` in the Bliss environment.

## Generator control

`isovolt_driver.generator.IsovoltGenerator` owns the serial line. The shutter
uses it through `generator: generator` in its config, so their commands never
interleave. Config: [bliss_config/xrd3003tt_generator.yml](bliss_config/xrd3003tt_generator.yml).

```python
generator                    # HV, actual and setpoint kV/mA, anode, water flow, message
generator.ramp_to(40, 30)    # in steps, 30 s each: current down first, voltage, current up
generator.hv_on() / .hv_off()
generator.generator_off()    # spec's generator_off: SC 25..10, SV 30..10, then HV:0
generator.clear_message()    # CL, e.g. after a safety-circuit trip
generator.warmup(pause, kv)  # WU:x,yy, the generator's own conditioning programme
```

- **Limits.** `ramp_to` refuses setpoints outside `min_kv..max_kv`,
  `min_ma..max_ma` and above `max_power`. The defaults are spec's operating
  point, 40 kV / 30 mA / 1200 W.
- **Ramp order.** Current goes down first, then the voltage, then the current
  up, with step sizes as in spec's `generator_off`. Values are sent as two
  digits, as the manual requires.
- **Session commands.** `generator_on()` switches HV on. `generator_off()`
  closes the shutter, ramps down and switches HV off.

## Protocol

The protocol comes from the generator's operating manual, chapter 7
("Schnittstelle"), pp. 47–54, which is not included here:

- **Commands** are ASCII and end in CR. Queries answer `*` followed by
  10 decimal digits and CR.
- **Shutter state** is in status word `SR:03` (shutters 1, 2) or `SR:04`
  (shutters 3, 4). Each shutter has four bits: open command, open status,
  closed unexpectedly, not connected.
- **Messages:** `SR:12` gives the message code and `ER` its text (door
  contacts, emergency stop, "Fenster nicht geoeffnet", ...). `SR:01` bit 6
  is high voltage on. `VA` and `CA` give the actual kV and mA, as V and µA.
- **Never sent:** the driver sends only `OS`, `CS` and read-only queries.
  It never sends HV, kV, mA, warm-up or keyboard-lock commands.

## Behaviour

- **Opening is gated by the absorber.** `open()` moves the absorber to its
  maximum first. If the absorber does not get there, the shutter is not
  opened. An open is also refused if the generator reports the shutter as
  not connected.
- **Standing generator messages.** Before anything moves, `open()` reads the
  generator's current message (`SR:12`). A standing door, emergency-stop,
  external-stop or safety-circuit message (codes 43, 46, 63, 64, 65, 112;
  `interlock_codes`) refuses the open with the generator's text. This also
  covers `closed_context`'s re-open.
- **The generator does NOT report an open door before an open attempt**
  (tested 2026-10-06). With door 2 open, every status word read the same as
  with the doors closed. On `OS:1` the generator's safety circuit refused the
  shutter, showed "? Safety circuit 1 and 2 open" (code 63) and **switched HV
  off**. Do not open the shutter with a door open. A real door pre-check needs
  the door sensor itself, the Pilz PNOZ relay on /dev/ttyS6 (not implemented).
- **Confirmation.** `open()` and `close()` wait until the generator reports
  the new state (`move_timeout`, 5 s). If it doesn't, they raise with the
  generator's message, e.g. "TUERKONTAKT 2 GEOEFFNET, code 65 = door
  contact 2 open".
- **A failed open withdraws its open command** (sends `CS`), so the shutter
  cannot open by itself later, for instance once the door is closed.
- **States:** OPEN, CLOSED, MOVING (opening or closing), and FAULT when the
  shutter is not connected. An unexpected closure shows as
  "Closed (closed unexpectedly!)".
- **Context managers:** `open_context` always closes at the end, also on
  errors and Ctrl-C. `closed_context` reopens afterwards only if the shutter
  was open before.
- **Serial port.** A local device (`/dev/...`) is opened with pyserial at the
  first command. bliss.comm's Serial lost bytes on this instrument's ViX line.
- **The absorber is named, not referenced.** The config uses `absorber: a2`,
  so loading the shutter does not create the Trinamic controller. The serial
  port is opened only at the first command.

## Tests

`tests/bliss_e2e.py` runs the shutter against a fake generator on a
pseudo-terminal (`tests/fake_generator.py`, built from the manual), with a
mockup motor as `a2`. It covers the absorber gating, confirmation, an open
door, unexpected closure, a disconnected shutter, and the context managers.
The setup is in its docstring.

## Not yet verified on hardware

- **Status layout.** The manual's bit layout of `SR:03` and the reply format
  are taken from the scan.
- **Timing.** How long the shutter takes to report open or closed is unknown;
  5 s is assumed to be ample.
