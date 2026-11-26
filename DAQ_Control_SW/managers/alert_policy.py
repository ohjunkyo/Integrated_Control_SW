"""Per-alert Slack policy shared by the apps and ~/scripts: post now, digest only, or mute."""
import json
import os

CONFIG = os.path.expanduser("~/.config/precal/slack_alerts.json")
MODES = ("post", "digest", "mute")
STILL_CHOICES = ((6, "Every 6 h"), (24, "Every 24 h"), (0, "Never"))
DEFAULT_STILL_H = 6

# key, group, label shown in the checklist; defaults are "post" (the message keeps its own level)
CATALOG = (
    ("scan_started", "General Scan", "Scan started"),
    ("scan_finished_good", "General Scan", "Scan finished: GOOD RUN"),
    ("scan_finished_bad", "General Scan", "Scan finished: BAD RUN"),
    ("scan_point_skipped", "General Scan", "Point / axis skipped"),
    ("scan_aborted", "General Scan", "Scan ABORTED"),
    ("scan_not_started", "General Scan", "Scheduled scan NOT started"),
    ("scan_paused_laser", "General Scan", "Scan paused: laser not ON"),
    ("laser_not_ready", "General Scan", "Laser not ready, block skipped"),
    ("reset_angle", "General Scan", "Reset Angle completed"),
    ("reset_angle_failed", "General Scan", "Reset Angle FAILED"),
    ("stability_started", "Stability Run", "Run started"),
    ("stability_finished", "Stability Run", "Run finished"),
    ("stability_stopped", "Stability Run", "Run stopped by operator"),
    ("stability_failed", "Stability Run", "Run finished: FAILED"),
    ("dark_started", "Stability Run", "Dark run started"),
    ("dark_laser_on", "Stability Run", "Dark run stopped: laser ON"),
    ("routine_finished", "Daily Routine", "Routine finished"),
    ("routine_failed", "Daily Routine", "Routine FAILED / stopped"),
    ("hv_crate", "HV", "HV crate unreachable"),
    ("hv_noread", "HV", "HV values not read"),
    ("hv_interlock", "HV", "HV interlock active"),
    ("hv_trip", "HV", "HV channel trip"),
    ("hv_stale", "HV", "HV monitor not logging"),
    ("wifi", "Network", "Wi-Fi uplink no internet"),
    ("wired", "Network", "Wired uplink no internet"),
    ("internet", "Network", "No internet on either uplink"),
    ("tailscale", "Network", "Tailscale offline"),
    ("bfield", "System", "B-field log stale / coils OFF"),
    ("backup_old", "System", "Log backup over 26 h"),
    ("datasync_old", "System", "Data sync over 26 h"),
    ("hdd_unmounted", "System", "External HDD not mounted"),
    ("disk_full", "System", "Disk above 90 %"),
    ("collector_stale", "System", "Monitoring collector stale"),
    ("dup_apps", "System", "Duplicate app copies"),
    ("daq_frozen", "System", "DAQ panel frozen"),
)


def load():
    data = {"still_h": DEFAULT_STILL_H, "items": {}}
    try:
        with open(CONFIG, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw.get("still_h"), int):
            data["still_h"] = raw["still_h"]
        items = raw.get("items") or {}
        data["items"] = {k: v for k, v in items.items() if v in MODES}
    except (OSError, ValueError, AttributeError):
        pass
    return data


def save(data):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"still_h": int(data.get("still_h", DEFAULT_STILL_H)),
                   "items": {k: v for k, v in (data.get("items") or {}).items() if v in MODES}}, f, indent=2)
    os.replace(tmp, CONFIG)


def mode(key):
    return load()["items"].get(key, "post") if key else "post"


def resolve(key, level):
    """Level to send at, or None when the alert is muted."""
    m = mode(key)
    if m == "mute":
        return None
    return "info" if m == "digest" else level


def still_s():
    """Reminder interval while a condition persists, in seconds; 0 = never."""
    return max(0, int(load()["still_h"])) * 3600


if __name__ == "__main__":
    import sys
    print(still_s() if sys.argv[1:] == ["still_s"] else json.dumps(load()))
