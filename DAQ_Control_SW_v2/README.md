# DAQ Control v2 (Qt)

PyQt5 successor of the Tk app (`DAQ_Control_SW`), with the same tabs, pages and behaviour. The DAQ
left panel is a full-height sidebar, device status sits as lamps on the main tabs (tooltip = values),
and an HV Monitor tab raises the HV app. No icons, emoji or images (status lamps are drawn indicators).

    ~/Integrated_Control_SW/venv/bin/python ~/Integrated_Control_SW/DAQ_Control_SW_v2/main.py

or the launcher's "Start DAQ Control v2" button. One copy per PC (instance lock `daq_v2`).

## Hardware ownership

Only one app drives the hardware at a time.

- **Tk app open** (instance lock `daq` held): it owns the stages, lasers, UPS and scans. v2 only
  shows its files and refuses Unlock.
- **Tk app closed**: v2 drives everything itself. It polls the stages, lasers and UPS, logs to the
  same files, and runs scans, schedules, stability runs and jobs.
- **Unlock**: one master-password Unlock opens every control, as in the Tk app. While v2 is
  unlocked it holds the `daq_v2_control` lock, and the Tk app refuses its own Unlock.
- **Scheduled scans**: both apps read `queued_schedules.json`, and only the owner fires them
  (`core/schedule.py`).
- **Watchdog**: v2 writes `logs/heartbeat_v2.txt` every second from the GUI thread;
  `~/scripts/daq_heartbeat_watchdog.sh` alerts (scan running) or restarts it (idle) when it goes stale.
  While v2 owns the stages it writes `logs/live_angles.json` in the Tk app's format.

Shared Tk modules (one implementation for both apps):
- `managers/rotation_manager.AutomationManager` (General Scan engine)
- `managers/rotation_control.RotationManager` (stage Modbus)
- `config_manager.ConfigManager`
- `managers/notifier.py` (Slack)
- `managers/patlite_lamp.py`
- `Laser_Control_SW/app/laser_driver.py`

Their Tk dialogs go through hooks (`controller.dialogs`), so v2 supplies Qt dialogs. tkinter is never
imported in v2.

## Layout

```
main.py                  entry point: excepthook (log, never abort), theme, MainWindow, heartbeat
app/
  window.py              sidebar + tab tree, status lamps, menus (Files / About / View), lazy pages,
                         start-up services (scan engine, UPS, laser reconnect offer)
  theme.py               light/dark palettes, type scale, spacing, stylesheet, QPalette; loads fonts/
  scan_bridge.py         GUI-thread dispatcher + Qt dialogs for the scan engine (emoji stripped)
  fonts/                 Inter + JetBrains Mono (SIL OFL)
core/                    Qt-free logic
  env.py                 paths, settings (~/.config/precal/daq_v2.json), config_file(), NO_HW (V2_NO_HW=1)
  config.py              config header parse / atomic save / DAQ binary rebuild, protected keys
  access.py              Unlock / Lock
  jobs.py                background jobs in named slots (Output tab panes)
  scripts.py             Execute Scripts and Run DAQ commands, built exactly like the Tk app; overlay files
  scan.py                host that runs the Tk AutomationManager (General Scan) + ScanUI facade
  stage.py               SN2/SN3 Modbus drives (shared RotationManager): monitoring, moves, stop
  laser.py               laser boards: connect, LD/TEC, currents, pulse width, trigger, 1 s safety watchdog
  ups.py                 UPS serial service: polling, CSV logging, diagnosis, shutdown
  schedule.py            scheduled General Scans (queued_schedules.json)
  stability.py           Stability Run (NumSequences / IntervalTime run on the DAQ slot)
  quick_setup.py         Quick Configuration save, handover notes
  hk.py                  HK Digitizer config, snapshots, commands
  signal_lamp.py  file_ops.py  maintenance.py  applog.py
  status.py  history.py  live_scan.py  waveform.py  images.py  daq_diag.py
widgets/                 basic.py (lamps, placeholders), log_table.py (log tables, FileTail)
pages/
  daq/                   left_panel, script_actions, pmt_setup, output, waveform, data_files, images,
                         log, backup, diag
  general_scan/          quick_setup, control_panel, live_scan, scan_matrix (Full Grid), stability,
                         schedule, hk_config, scan_log, scan_history
  laser.py  signal_lamp.py  ups.py  logs.py  quick_start.py  information.py  emergency.py
```

To add a page: put the logic in `core/` (no widgets), check that it gives the same results as the Tk
app, then write `pages/.../<name>.py` and register it in `PORTED` in `app/window.py`.

## Testing

- Use `QT_QPA_PLATFORM=offscreen V2_NO_HW=1` and `widget.grab()`. Don't open test windows on the
  operator's screen.
- With `V2_NO_HW=1` no stage, laser or UPS connection is opened, and only TEST RUN scans start.
- Never test against the real config header, queued_schedules.json, hk_config.json or data. Use temp
  copies, and stub `make`, SSH and Slack.
