# Tamadenshi LD Board — Standalone Linux Driver

Self-contained control software for the Tamadenshi (Tama Electric) pico-second
laser diode board. Copy this one directory to any Linux PC and it runs — no
dependency on `Integrated_Control_SW`, no absolute paths, no config files
elsewhere on the machine.

```
standalone/
├── laser_driver.py       # the driver library (import this from your own code)
├── laser_cli.py          # command-line front end
├── 99-tamadenshi.rules   # udev rule for non-root USB access
├── install.sh            # one-time setup
├── requirements.txt
└── log/                  # CSV drift logs (created on first write)
```

## Install

```bash
scp -r standalone/ user@newpc:~/laser
```

```bash
cd ~/laser && ./install.sh
```

Then **unplug and replug the laser's USB cable** so the new udev rule applies.

If you cannot use sudo, run `./install.sh --no-sudo` and either install
`99-tamadenshi.rules` yourself later or run the CLI as root.

## Use

```bash
source venv/bin/activate
```

```bash
./laser_cli.py list
```

| Command | What it does |
|---|---|
| `list` | Enumerate attached boards (index, serial, USB path) |
| `status` | One-shot snapshot: temperature, currents, pulse width, photodiode |
| `monitor --interval 2` | Poll continuously, print a table, append to the CSV log |
| `set --bias 20 --pulse 145` | Write drive currents (combined limit enforced) |
| `set --temp 25 --tec on` | Set TEC target and switch it on |
| `set --trigger ext` | Trigger source: `pg1` / `pg2` / `ext` / `off` |
| `on` / `off` | Turn the laser diode on or off |
| `pulse-width` | Read the pulse width; `--set 680` writes it |

Anything that changes what the hardware emits asks for confirmation. Pass `-y`
to skip that in scripts.

With several boards attached, select one with `--index N`:

```bash
./laser_cli.py --index 1 status
```

**These boards report no serial number** (`list` shows `(none)` and the generic
product string "Simple HID Device Demo" for every one), so `--serial` is not
usable in practice and the USB path is the only thing that distinguishes them.
The path reflects which physical port a board is plugged into — e.g.
`1-3.4.1:1.0` … `1-3.4.4:1.0` for a four-board hub — so **write down which
wavelength is in which port** and keep the cabling fixed. Index order follows
enumeration, not port number, and can change across reboots, so scripts should
address boards by path instead:

```bash
./laser_cli.py --path 1-3.4.1:1.0 status
```

## Logging

`monitor` appends a CSV row every 10 seconds **while the LD is on** to
`log/laser_data_YYYYMMDD.csv`:

```
timestamp,ld_on,tec_on,temp_c,bias_ma,pulse_ma,pulse_width_ps,pd_raw,pd_current
```

Point that somewhere else with `export LASER_LOG_DIR=/data/laser_logs`.

`pd_current` is **blank** rather than zero when the board's photodiode is dead
(see the hardware note below), so a drift fit reading this file skips those rows
instead of fitting a flat fake line.

## Using the driver from your own code

```python
from laser_driver import TamadenshiLaser, list_devices

dev = list_devices()[0]
with TamadenshiLaser(name="405nm") as laser:
    laser.connect(dev["path"])
    ok, msg = laser.set_currents(bias_ma=20, pulse_ma=145)   # enforces the sum
    laser.update_status()
    print(laser.status["ld_temp"], laser.status["pd_current"])
```

The `with` block matters: hidapi's libusb backend detaches the kernel `usbhid`
driver on open, and a process that dies without a clean `close()` leaves the
device unbound until the cable is replugged.

## Hardware notes worth knowing

**Combined current limit.** Bias and pulse current share one physical drive
path, so the manual's 200 mA ceiling applies to their **sum**. `set_currents()`
enforces this; the individual `set_bias_current()` / `set_pulse_current()`
setters cannot see the total, so prefer `set_currents()`.

**Photodiode monitor.** `pd_current` is the only genuinely *measured* optical
quantity — `bias` and `pulse` are DAC setpoints echoed back, and read the
commanded value even with the LD off, so they cannot show drift by
construction. On the four boards at the main site (measured 2026-08-09), only
the 450 nm board's monitor photodiode responds; 375/405/473 nm return exactly 0
even while confirmed firing, meaning their PD input is dead or unpopulated.
Check `status["pd_valid"]` before trusting a reading — and re-check on new
hardware, since this is a per-board property, not a driver limitation.

**Pulse width.** Reading uses EEPROM slot `address + 128`, which does not touch
the live output and is safe while firing. **Writing** slot 0 changes the emitted
pulse immediately *and* persists to EEPROM. Valid range is 100–10230 ps.

**Byte offsets.** The temperature and photodiode field offsets in
`update_status()` were recovered by decompiling the vendor's `tmHIDLD.dll` after
the hand-written values proved wrong (a room-temperature board read 3.2 °C).
The inline comments record the evidence — don't "simplify" them away.

## Troubleshooting

**No board found / permission denied** — udev rules not applied. Reinstall them
and replug the cable:

```bash
sudo cp 99-tamadenshi.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules && sudo udevadm trigger
```

**Board disappeared after a crash** — the kernel driver is still detached.
Replug the cable, or rebind it:

```bash
sudo udevadm trigger --subsystem-match=usb --action=add
```

**Pulse-width read fails intermittently** — the board sometimes returns a stale
response when polled right after another command. The driver rejects
out-of-range values rather than caching garbage; just retry.

**`ImportError: No module named hid`** — activate the venv (`source
venv/bin/activate`), or `pip install hidapi`.
