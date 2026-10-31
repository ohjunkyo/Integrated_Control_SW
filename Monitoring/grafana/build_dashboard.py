#!/usr/bin/env python3
"""Write dashboards/precal_overview.json; edit the panel list here, not the JSON."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ingest import HV_CODE_SQL, HV_GOOD_SQL, READERR_SQL  # noqa: E402  one HV state rule for collector and panels

MON = {"type": "frser-sqlite-datasource", "uid": "precal-monitor"}
HV = {"type": "frser-sqlite-datasource", "uid": "precal-hv"}
BUCKET = "MAX(($__to - $__from) / 600000, 1)"   # ~600 points from the range; public dashboards do not fill $__interval_ms
RANGE = "time >= $__from / 1000 AND time < $__to / 1000"
HV_RANGE = ("timestamp >= strftime('%Y-%m-%dT%H:%M:%S', $__from / 1000, 'unixepoch', 'localtime') "
            "AND timestamp < strftime('%Y-%m-%dT%H:%M:%S', $__to / 1000, 'unixepoch', 'localtime')")
HV_TIME = "CAST(strftime('%s', timestamp, 'utc') AS INTEGER)"
HV_MAP = [{"type": "value", "options": {"0": {"text": "NORMAL ON", "color": "green"},
                                         "1": {"text": "NORMAL OFF", "color": "green"},
                                         "2": {"text": "READERR", "color": "orange"},
                                         "3": {"text": "ILK/ALARM", "color": "red"},
                                         "4": {"text": "NODATA", "color": "gray"}}}]
HV_SEV = "CASE c WHEN 3 THEN 4 WHEN 2 THEN 3 WHEN 4 THEN 2 WHEN 1 THEN 1 ELSE 0 END"   # worst state wins a bucket
HV_BITS = [(0, "ON"), (1, "RUP"), (2, "RDN"), (3, "OVC"), (4, "OVV"), (6, "MAXV"), (7, "TRIP"), (8, "OVP"),
           (9, "OVT"), (10, "DIS"), (11, "KILL"), (12, "EXT_ILK")]
CHANNELS = [(0, "Monitor"), (1, "Rot1"), (2, "Rot2"), (3, "Ch3")]
BOXES = [(1, "Rot#1"), (2, "Rot#2"), (3, "Mon.")]
WLS = ["375nm", "405nm", "450nm", "473nm"]

panels, y = [], 0
BOARDS = {"main": ([], 0), "details": ([], 0)}
cur = "main"


def to(name):
    """Send the following rows and panels to dashboard `name` (main = common view, details = the rest)."""
    global panels, y, cur
    BOARDS[cur] = (panels, y)
    panels, y = BOARDS[name]
    cur = name


def q(sql, ds=MON, kind="table"):
    return {"datasource": ds, "refId": "A", "queryText": sql, "rawQueryText": sql, "queryType": kind,
            "timeColumns": ["time"]}


def row(title, collapsed=False):
    global y
    r = {"type": "row", "title": title, "collapsed": collapsed, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
         "panels": []}
    panels.append(r)
    y += 1
    return r


def panel(kind, title, sql, w, h, x, ds=MON, qkind="table", **extra):
    p = {"type": kind, "title": title, "datasource": ds, "targets": [q(sql, ds, qkind)],
         "gridPos": {"x": x, "y": y, "w": w, "h": h}, "fieldConfig": {"defaults": {}, "overrides": []},
         "options": {}}
    dflt = p["fieldConfig"]["defaults"]
    for k, v in extra.items():
        if k in ("unit", "mappings", "min", "max", "decimals", "color", "noValue"):
            dflt[k] = v
        elif k == "thresholds":
            dflt["thresholds"] = {"mode": "absolute", "steps": v}
        elif k == "overrides":
            p["fieldConfig"]["overrides"] = v
        elif k == "custom":
            dflt["custom"] = v
        elif k == "timeFrom":
            p["timeFrom"] = v
        else:
            p["options"][k] = v
    if kind == "timeseries":
        dflt.setdefault("custom", {"spanNulls": 900000})   # join 5-min remote samples, break on gaps over 15 min
    if kind in ("stat", "bargauge", "gauge"):   # show text fields too and only the latest value
        p["options"].setdefault("reduceOptions", {"values": False, "calcs": ["lastNotNull"], "fields": "/.*/"})
    panels.append(p)


def wide(cols, table, where=""):
    """Time-bucketed averages of numeric columns."""
    sel = ", ".join(f"AVG({c}) AS {a}" for c, a in cols)
    return (f"SELECT (ts_epoch / {BUCKET}) * {BUCKET} AS time, {sel} FROM {table} "
            f"WHERE ts_epoch >= $__from / 1000 AND ts_epoch < $__to / 1000 {where} GROUP BY 1 ORDER BY 1")


def per_wl(col):
    sel = ", ".join(f"AVG(CASE WHEN wl = '{w}' THEN {col} END) AS \"{w}\"" for w in WLS)
    return (f"SELECT (ts_epoch / {BUCKET}) * {BUCKET} AS time, {sel} FROM laser "
            f"WHERE ts_epoch >= $__from / 1000 AND ts_epoch < $__to / 1000 GROUP BY 1 ORDER BY 1")


LATEST = "FROM v_daq_status ORDER BY time DESC LIMIT 1"

NOW = "CAST(strftime('%s','now') AS INTEGER)"
HOSTS = [("kor", "KOR"), ("bfield", "B-field"), ("shifter", "Shifter")]
DISKS = [("kor", "disk_root", "KOR root"), ("kor", "disk_ext_hdd", "KOR ext HDD"), ("shifter", "disk_root", "Shifter root"),
         ("shifter", "disk_rawdata", "Shifter rawdata"), ("bfield", "disk_c", "B-field C:")]
UNITS = [("unit_ups_guard_service", "UPS guard"), ("unit_remote_ops_service", "Remote page"),
         ("unit_daq_data_monitoring_service", "Data monitor :8090"), ("unit_network_watchdog_timer", "Supervisor"),
         ("unit_bfield_log_sync_timer", "B-field sync"), ("unit_backup_logs_timer", "Log backup"),
         ("unit_precal_monitor_timer", "Collector")]
OK_AGES = [("backup_logs_last_ok", "Log backup", 26 * 3600), ("bfield_sync_last_ok", "B-field sync", 20 * 60),
           ("data_sync_last_ok", "Data sync", 26 * 3600)]
GREEN, YELLOW, RED, GRAY, ORANGE = "green", "yellow", "red", "gray", "orange"
PCT = lambda warn, crit: [{"color": GREEN, "value": None}, {"color": YELLOW, "value": warn}, {"color": RED, "value": crit}]


def latest(host, expr):
    return f"(SELECT {expr} FROM pc_health WHERE host = '{host}' ORDER BY ts_epoch DESC LIMIT 1)"


def per_host_series(expr, items):
    sel = ", ".join(f"AVG(CASE WHEN host = '{h}' THEN {expr(h, c)} END) AS \"{n}\"" for h, c, n in items)
    return (f"SELECT (ts_epoch / {BUCKET}) * {BUCKET} AS time, {sel} FROM pc_health "
            f"WHERE ts_epoch >= $__from / 1000 AND ts_epoch < $__to / 1000 GROUP BY 1 ORDER BY 1")


def lamp_map(on, off, stale=None):
    m = {"1": {"text": on, "color": GREEN}, "0": {"text": off, "color": RED}}
    if stale:
        m["-1"] = {"text": stale, "color": GRAY}
    return [{"type": "value", "options": m}]


row("Status")
lamps = [("lamp_hv_env_sensor", "HV / Env"), ("lamp_laser_controller", "Laser"), ("lamp_b_field_monitor", "B-field"),
         ("lamp_omron_ups", "UPS"), ("logic_unit_up", "Logic unit"), ("lamp_log_backup", "Log backup")]
# -1 (STALE) when the newest status row is older than 10 min.
panel("stat", "System lamps", "SELECT " + ", ".join(f'CASE WHEN {NOW} - time > 600 THEN -1 ELSE {c} END AS "{n}"'
                                                    for c, n in lamps) + " " + LATEST, 15, 4, 0,
      mappings=lamp_map("OK", "FAIL", "STALE"), colorMode="background", graphMode="none", textMode="value_and_name")
panel("stat", "HV state", f"SELECT hv_state_code AS \"HV\", scan_running AS \"Scan running\" {LATEST}", 9, 4, 15,
      colorMode="background", graphMode="none", textMode="value_and_name", mappings=HV_MAP,
      overrides=[{"matcher": {"id": "byName", "options": "Scan running"}, "properties": [{"id": "mappings", "value": [
          {"type": "value", "options": {"1": {"text": "YES", "color": GREEN}, "0": {"text": "no", "color": GRAY}}}]}]}])
y += 4

row("DAQ")
WL_COLORS = {"375 nm": "purple", "405 nm": "#6E40C9", "450 nm": "blue", "473 nm": "#1FB8CD", "dark": "gray"}
wl_over = [{"matcher": {"id": "byName", "options": n}, "properties": [{"id": "color", "value": {"mode": "fixed",
            "fixedColor": c}}]} for n, c in WL_COLORS.items()]
WL_KEY = "CASE WHEN CAST(mode AS TEXT) = 'dark' THEN 'dark' ELSE CAST(CAST(wavelength AS INT) AS TEXT) || ' nm' END"
DAY = "((ts_epoch + 32400) / 86400) * 86400 - 32400"   # local (KST) midnight
last_run = "FROM daq_run ORDER BY ts_epoch DESC LIMIT 1"
PUR_LAST = "COALESCE(NULLIF(CAST(purpose AS TEXT), ''), 'Other')"
run_label = (f"(SELECT CAST(CAST(date AS INT) AS TEXT) || '_' || printf('%03d', CAST(run AS INT)) || '  ' || "
             f"CAST(CAST(wavelength AS INT) AS TEXT) || ' nm  ' || {PUR_LAST} {last_run})")
progress = ("CASE WHEN scan_running = 1 AND scan_total > 0 THEN printf('%d / %d  (%.0f %%)', scan_done, scan_total, "
            "100.0 * scan_done / scan_total) || '  ' || COALESCE(NULLIF(CAST(scan_tag AS TEXT), ''), '') ELSE '—' END")
plain = {"id": "color", "value": {"mode": "fixed", "fixedColor": "text"}}
daq_over = [{"matcher": {"id": "byName", "options": n}, "properties": [plain]}
            for n in ("Progress", "Since last run", "Last run")]
daq_over.append({"matcher": {"id": "byName", "options": "Since last run"},
                 "properties": [{"id": "unit", "value": "s"}, {"id": "decimals", "value": 1}]})
panel("stat", "DAQ", f"SELECT scan_running AS \"Scan\", {progress} AS \"Progress\", "
      f"(SELECT {NOW} - MAX(ts_epoch) FROM daq_run) AS \"Since last run\", {run_label} AS \"Last run\" {LATEST}",
      24, 4, 0, colorMode="value", graphMode="none", textMode="value_and_name", justifyMode="center",
      text={"titleSize": 13, "valueSize": 24}, overrides=daq_over,
      mappings=[{"type": "value", "options": {"1": {"text": "SCANNING", "color": GREEN},
                                              "0": {"text": "IDLE", "color": GRAY}}}])
y += 4
PURPOSES = {"General scan": "blue", "Stability": "green", "Manual": "orange", "Self trigger (dark)": "purple",
            "External trigger (LDs off)": "red", "Other": "gray", "HK Digitizer": "#00A896"}
pur_over = [{"matcher": {"id": "byName", "options": n}, "properties": [{"id": "color", "value": {"mode": "fixed",
             "fixedColor": c}}]} for n, c in PURPOSES.items()]
PUR = "COALESCE(NULLIF(CAST(purpose AS TEXT), ''), 'Other')"
# CAEN DT5730 runs (run registry) and HK Digitizer runs (HK PC metadata) in one stream.
RUNS = "(SELECT ts_epoch, purpose, laser_ma FROM daq_run UNION ALL SELECT ts_epoch, 'HK Digitizer', NULL FROM hk_run)"
per_day = ", ".join(f"SUM(CASE WHEN {PUR} = '{n}' THEN 1 ELSE 0 END) AS \"{n}\"" for n in PURPOSES)
panel("timeseries", "Runs per day (30 days)", f"SELECT {DAY} AS time, {per_day} FROM {RUNS} "
      f"WHERE ts_epoch >= {NOW} - 30 * 86400 GROUP BY 1 ORDER BY 1", 10, 8, 0, timeFrom="30d", min=0,
      custom={"drawStyle": "bars", "fillOpacity": 85, "lineWidth": 1, "barAlignment": 0,
              "stacking": {"mode": "normal", "group": "A"}, "spanNulls": False},
      overrides=pur_over, legend={"displayMode": "list", "placement": "bottom"})
scan_over = [{"matcher": {"id": "byName", "options": "complete"},
              "properties": [{"id": "unit", "value": "percent"}, {"id": "min", "value": 0}, {"id": "max", "value": 100},
                             {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "basic"}},
                             {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                                 {"color": "orange", "value": None}, {"color": "green", "value": 100}]}}]}]
scan_over.append({"matcher": {"id": "byName", "options": "status"}, "properties": [
    {"id": "custom.cellOptions", "value": {"type": "color-text"}},
    {"id": "mappings", "value": [{"type": "value", "options": {"Complete": {"color": "green"},
                                                               "Interrupted": {"color": "orange"}}}]}]})
NPOS = "COUNT(DISTINCT CAST(rot2 AS TEXT) || '/' || CAST(tilt2 AS TEXT))"   # stage positions in the group
SCAN_GROUPS = "FROM daq_run WHERE scan_id IS NOT NULL AND scan_id != '' GROUP BY scan_id "
panel("table", "General scans (46 points each)", "SELECT scan_id AS scan, MIN(ts) AS start, MAX(ts) AS \"end\", "
      "CAST(MAX(CAST(wavelength AS INT)) AS TEXT) || ' nm' AS wl, COUNT(*) || ' / 46' AS runs, "
      "CASE WHEN COUNT(*) >= 46 THEN 'Complete' ELSE 'Interrupted' END AS status, "
      f"MIN(100.0 * COUNT(*) / 46, 100) AS complete {SCAN_GROUPS}HAVING {NPOS} > 1 "
      "ORDER BY MIN(ts_epoch) DESC LIMIT 10", 14, 8, 10, overrides=scan_over, cellHeight="sm")
y += 8

to("details")
row("DAQ")
tl = ", ".join(f"CASE WHEN {PUR} = '{n}' THEN " +
               ("0" if "dark" in n or "LDs off" in n or n == "HK Digitizer" else "laser_ma") +
               f" END AS \"{n}\"" for n in PURPOSES)
panel("timeseries", "Run timeline, 7 days (laser mA; dark and HK runs at 0)", f"SELECT ts_epoch AS time, {tl} FROM {RUNS} "
      f"WHERE ts_epoch >= {NOW} - 7 * 86400 ORDER BY ts_epoch", 24, 8, 0, min=0, timeFrom="7d",
      noValue="No runs in the last 7 days",
      custom={"drawStyle": "points", "pointSize": 7, "showPoints": "always", "spanNulls": False},
      overrides=pur_over, legend={"displayMode": "list", "placement": "bottom"})
y += 8
panel("table", "Single-position general runs", "SELECT scan_id AS \"group\", MIN(ts) AS start, MAX(ts) AS \"end\", "
      "CAST(MAX(CAST(wavelength AS INT)) AS TEXT) || ' nm' AS wl, COUNT(*) AS runs, "
      "GROUP_CONCAT(DISTINCT CAST(CAST(run AS INT) AS TEXT)) AS run_numbers, "
      "MAX(CAST(rot2 AS TEXT) || ' / ' || CAST(tilt2 AS TEXT)) AS \"rot2 / tilt2\", "
      f"GROUP_CONCAT(DISTINCT CAST(status AS TEXT)) AS status {SCAN_GROUPS}HAVING {NPOS} <= 1 "
      "ORDER BY MIN(ts_epoch) DESC LIMIT 50", 24, 8, 0, cellHeight="sm")
y += 8
panel("table", "Runs", "SELECT ts AS registered, COALESCE(scan_id, '') AS scan, " + PUR + " AS purpose, "
      "CAST(CAST(date AS INT) AS TEXT) AS date, CAST(run AS INT) AS run, CAST(kind AS TEXT) AS kind, "
      "CAST(mode AS TEXT) AS mode, CAST(status AS TEXT) AS status, CAST(CAST(wavelength AS INT) AS TEXT) AS wl, "
      "laser_ma, events, CAST(sn1 AS TEXT) AS sn1, CAST(sn2 AS TEXT) AS sn2, CAST(sn3 AS TEXT) AS sn3, "
      "CAST(dir1 AS TEXT) AS dir1, CAST(dir2 AS TEXT) AS dir2, CAST(dir3 AS TEXT) AS dir3, "
      "CAST(file AS TEXT) AS file, ts_epoch FROM daq_run "
      "UNION ALL SELECT ts, '', 'HK Digitizer', CAST(date AS TEXT), CAST(run AS INT), CAST(kind AS TEXT), "
      "CAST(mode AS TEXT), 'done', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, CAST(run_id AS TEXT), "
      "ts_epoch FROM hk_run ORDER BY ts_epoch DESC LIMIT 300", 24, 10, 0, custom={"filterable": True},
      overrides=[{"matcher": {"id": "byName", "options": "ts_epoch"},
                  "properties": [{"id": "custom.hidden", "value": True}]},
                 {"matcher": {"id": "byName", "options": "purpose"},
                  "properties": [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                 {"id": "mappings", "value": [{"type": "value", "options": {
                                     "HK Digitizer": {"color": PURPOSES["HK Digitizer"]}}}]}]}])
y += 10

to("main")
row("HV")
vsel = ", ".join(f'AVG(Ch{c}_V) AS "{n}"' for c, n in CHANNELS[:3])
isel = ", ".join(f'AVG(Ch{c}_I_H) AS "{n}"' for c, n in CHANNELS[:3])
hv_bucket = f"({HV_TIME} / {BUCKET}) * {BUCKET}"
panel("timeseries", "HV voltage [V]", f"SELECT {hv_bucket} AS time, {vsel} FROM monitoring_data WHERE {HV_RANGE} "
      f"AND NOT {READERR_SQL} GROUP BY 1 ORDER BY 1", 12, 8, 0, ds=HV, unit="volt", min=0)
panel("timeseries", "HV current [uA]", f"SELECT {hv_bucket} AS time, {isel} FROM monitoring_data WHERE {HV_RANGE} "
      f"AND NOT {READERR_SQL} GROUP BY 1 ORDER BY 1", 12, 8, 12, ds=HV, unit="suffix: µA", min=0)
y += 8
tsel = ", ".join(f'AVG(Dark_Box_{b}_T) AS "{n} T"' for b, n in BOXES)
hsel = ", ".join(f'AVG(Dark_Box_{b}_H) AS "{n} H"' for b, n in BOXES)
panel("timeseries", "Dark box temperature [C] and humidity [%]", f"SELECT {hv_bucket} AS time, {tsel}, {hsel} "
      f"FROM monitoring_data WHERE {HV_RANGE} GROUP BY 1 ORDER BY 1", 24, 7, 0, ds=HV, unit="celsius",
      overrides=[{"matcher": {"id": "byRegexp", "options": ".* H$"}, "properties": [
          {"id": "unit", "value": "humidity"}, {"id": "custom.axisPlacement", "value": "right"},
          {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}]}])
y += 7

to("details")
row("HV")
panel("state-timeline", "HV state", f"SELECT time, MAX({HV_SEV} * 10 + c) % 10 AS state FROM "
      f"(SELECT {hv_bucket} AS time, {HV_CODE_SQL} AS c FROM monitoring_data WHERE {HV_RANGE}) "
      "GROUP BY 1 ORDER BY 1", 9, 5, 0, ds=HV, showValue="never", mergeValues=True, mappings=HV_MAP)
panel("stat", "Last good HV read", f"SELECT hv_last_good_age_s AS \"ago\" {LATEST}", 3, 5, 9, unit="s",
      colorMode="value", graphMode="none", text={"valueSize": 18},
      thresholds=[{"color": GREEN, "value": None}, {"color": ORANGE, "value": 60}, {"color": RED, "value": 300}])
bits = lambda c: ", ".join(f"(CAST(Ch{c}_Stat AS INT) >> {b}) & 1 AS \"{lab}\"" for b, lab in HV_BITS)
panel("table", "HV status bits (last good read)", " UNION ALL ".join(
      f"SELECT timestamp, '{n}' AS channel, CAST(Ch{c}_Stat AS INT) AS stat, {bits(c)} FROM "
      f"(SELECT * FROM monitoring_data WHERE {HV_GOOD_SQL} ORDER BY timestamp DESC LIMIT 1)" for c, n in CHANNELS),
      12, 5, 12, ds=HV, cellHeight="sm")
y += 5
tsel = ", ".join(f'AVG(Dark_Box_{b}_T) AS "{n}"' for b, n in BOXES)
hsel = ", ".join(f'AVG(Dark_Box_{b}_H) AS "{n}"' for b, n in BOXES)
panel("timeseries", "Dark box temperature [C]", f"SELECT {hv_bucket} AS time, {tsel} FROM monitoring_data "
      f"WHERE {HV_RANGE} GROUP BY 1 ORDER BY 1", 12, 7, 0, ds=HV, unit="celsius")
panel("timeseries", "Dark box humidity [%]", f"SELECT {hv_bucket} AS time, {hsel} FROM monitoring_data "
      f"WHERE {HV_RANGE} GROUP BY 1 ORDER BY 1", 12, 7, 12, ds=HV, unit="humidity")
y += 7

to("main")
row("B-field")
panel("timeseries", "Coil current [A] (0 A = coils OFF)", wide([(f"i{n}", f"I{n}") for n in (1, 2, 3, 4)], "bfield"),
      8, 7, 0, unit="amp", thresholds=[{"color": RED, "value": None}, {"color": GREEN, "value": 0.01}],
      custom={"spanNulls": 900000, "thresholdsStyle": {"mode": "line+area"}})
panel("timeseries", "|B| [mG]", wide([(f"m{n}_mg", f"M{n}") for n in (2, 3)], "bfield"), 8, 7, 8)
panel("timeseries", "Temperature [C]", wide([(f"t{n}", f"T{n}") for n in (3, 5, 7)], "bfield"), 8, 7, 16,
      unit="celsius")
y += 7

to("details")
row("B-field")
panel("timeseries", "|B| all magnetometers [mG]", wide([(f"m{n}_mg", f"M{n}") for n in (1, 2, 3)], "bfield"),
      12, 7, 0)
panel("timeseries", "Temperature T1-T7 [C]", wide([(f"t{n}", f"T{n}") for n in (1, 2, 3, 4, 5, 6, 7)], "bfield"),
      12, 7, 12, unit="celsius")
y += 7

to("main")
row("Laser")
# LD lamp per wavelength: -1 when that controller has not logged for 5 min.
panel("stat", "LD", "SELECT " + ", ".join(
      f"(SELECT CASE WHEN {NOW} - ts_epoch > 300 THEN -1 ELSE ld_on END FROM laser WHERE wl = '{w}' "
      f"ORDER BY ts_epoch DESC LIMIT 1) AS \"{w}\"" for w in WLS), 6, 7, 0,
      mappings=[{"type": "value", "options": {"1": {"text": "ON", "color": GREEN}, "0": {"text": "OFF", "color": GRAY},
                                              "-1": {"text": "NO DATA", "color": ORANGE}}}],
      colorMode="background", graphMode="none", textMode="value_and_name")
lsel = ", ".join(f"AVG(CASE WHEN wl = '{w}' THEN temp_c END) AS \"{w} T\", "
                 f"AVG(CASE WHEN wl = '{w}' THEN pd_current_ma END) AS \"{w} PD\"" for w in WLS)
panel("timeseries", "Laser temperature [C] and photodiode [mA]", f"SELECT (ts_epoch / {BUCKET}) * {BUCKET} AS time, "
      f"{lsel} FROM laser WHERE ts_epoch >= $__from / 1000 AND ts_epoch < $__to / 1000 GROUP BY 1 ORDER BY 1",
      18, 7, 6, unit="celsius",
      overrides=[{"matcher": {"id": "byRegexp", "options": ".* PD$"}, "properties": [
          {"id": "unit", "value": "suffix: mA"}, {"id": "custom.axisPlacement", "value": "right"},
          {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}]}])
y += 7

to("details")
row("Laser")
panel("timeseries", "Laser temperature [C]", per_wl("temp_c"), 8, 7, 0, unit="celsius")
panel("timeseries", "Pulse current [mA]", per_wl("pulse_ma"), 8, 7, 8)
panel("timeseries", "LD on", per_wl("ld_on"), 8, 7, 16)
y += 7
panel("table", "Laser events (warnings)", "SELECT ts, CAST(wl AS TEXT) AS wl, CAST(level AS TEXT) AS level, "
      "CAST(message AS TEXT) AS message FROM laser_event "
      f"WHERE ts_epoch >= $__from / 1000 AND ts_epoch < $__to / 1000 AND level IN ('WARNING', 'ERROR', 'CRITICAL') "
      "ORDER BY ts_epoch DESC LIMIT 200", 24, 6, 0)
y += 6

to("main")
row("UPS")
panel("timeseries", "Input voltage [V] and load [W]", wide([("vin", "Vin"), ("watt", "load")], "ups"), 18, 6, 0,
      unit="volt", overrides=[{"matcher": {"id": "byName", "options": "load"}, "properties": [
          {"id": "unit", "value": "watt"}, {"id": "custom.axisPlacement", "value": "right"}]}])
panel("stat", "Battery", f"SELECT batt_pct AS \"battery\", CASE WHEN {NOW} - ts_epoch > 300 THEN -1 "
      "ELSE on_battery END AS \"on battery\" FROM ups_state ORDER BY ts_epoch DESC LIMIT 1", 6, 6, 18,
      colorMode="background", graphMode="none", textMode="value_and_name",
      overrides=[{"matcher": {"id": "byName", "options": "battery"}, "properties": [
                     {"id": "unit", "value": "percent"}, {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                         {"color": RED, "value": None}, {"color": YELLOW, "value": 50}, {"color": GREEN, "value": 90}]}}]},
                 {"matcher": {"id": "byName", "options": "on battery"}, "properties": [{"id": "mappings", "value": [
                     {"type": "value", "options": {"0": {"text": "MAINS", "color": GREEN},
                                                   "1": {"text": "ON BATTERY", "color": RED},
                                                   "-1": {"text": "NO DATA", "color": GRAY}}}]}]}])
y += 6

to("details")
row("UPS")
panel("timeseries", "Battery and load [%]", wide([("batt_pct", "battery"), ("load", "load")], "ups_state"),
      8, 7, 0, unit="percent")
panel("timeseries", "On battery", wide([("on_battery", "on_battery"), ("battery_low", "battery_low")], "ups_state"),
      8, 7, 8)
panel("timeseries", "Input / output [V]", wide([("vin", "Vin"), ("vout", "Vout")], "ups"), 8, 7, 16, unit="volt")
y += 7

to("main")
row("Services and sync")
svc_latest = f"FROM pc_health WHERE host = 'kor' ORDER BY ts_epoch DESC LIMIT 1"
# "Collector data": FAIL when any source has not run successfully for 3 of its periods (+1 min).
RUN_LATE = f"{NOW} - last_ok_epoch > 3 * COALESCE(period_s, 60) + 60"
panel("stat", "Services and timers", "SELECT " + ", ".join(f'{c} AS "{n}"' for c, n in UNITS) +
      f", (SELECT CASE WHEN SUM({RUN_LATE}) > 0 THEN 0 ELSE 1 END FROM source_status) "
      "AS \"Collector data\" " + svc_latest,
      16, 4, 0, mappings=lamp_map("RUNNING", "DOWN"), colorMode="background", graphMode="none",
      textMode="value_and_name",
      overrides=[{"matcher": {"id": "byName", "options": "Collector data"},
                  "properties": [{"id": "mappings", "value": lamp_map("OK", "FAIL")}]}])
age_over = [{"matcher": {"id": "byName", "options": n},
             "properties": [{"id": "thresholds", "value": {"mode": "absolute", "steps": [
                 {"color": GREEN, "value": None}, {"color": RED, "value": lim}]}}]} for _c, n, lim in OK_AGES]
panel("stat", "Last [OK]", "SELECT " + ", ".join(f"{NOW} - {c} AS \"{n}\"" for c, n, _l in OK_AGES) + " " + LATEST,
      8, 4, 16, unit="s", colorMode="background", graphMode="none", textMode="value_and_name", overrides=age_over)
y += 4

to("details")
row("Services and sync")
panel("state-timeline", "Services over time", "SELECT time, " + ", ".join(f'{c} AS "{n}"' for c, n in UNITS) +
      f" FROM v_pc_health WHERE host = 'kor' AND {RANGE} ORDER BY time", 24, 7, 0,
      mappings=lamp_map("RUNNING", "DOWN"), showValue="never", mergeValues=True)
y += 7
src_over = [{"matcher": {"id": "byName", "options": n}, "properties": [
    {"id": "custom.cellOptions", "value": {"type": "color-text"}},
    {"id": "mappings", "value": lamp_map("OK", "FAIL", "n/a")}]} for n in ("run", "data")]
src_over += [{"matcher": {"id": "byName", "options": n}, "properties": [{"id": "unit", "value": "s"}]}
             for n in ("last run", "newest data", "data limit")]
panel("table", "Collector sources", f"SELECT name AS source, {NOW} - last_ok_epoch AS \"last run\", "
      f"CASE WHEN {RUN_LATE} THEN 0 ELSE 1 END AS run, {NOW} - data_epoch AS \"newest data\", "
      f"max_age_s AS \"data limit\", CASE WHEN max_age_s IS NULL THEN -1 WHEN {NOW} - data_epoch > max_age_s "
      "THEN 0 ELSE 1 END AS data, CAST(COALESCE(last_error, '') AS TEXT) AS \"last error\" FROM source_status "
      "ORDER BY name", 24, 8, 0, overrides=src_over, cellHeight="sm")
y += 8

to("main")
row("PCs")
items = [(h, None, n) for h, n in HOSTS]
# One lamp per PC: STALE -1, DOWN 0, UP 1, MEM 2 (< 2 GB available or swap >= 95 %), HOT 3 (CPU >= 85 C).
pc_lamp = "SELECT " + ", ".join(
    f"CASE WHEN {NOW} - COALESCE({latest(h, 'ts_epoch')}, 0) > 600 THEN -1 WHEN {latest(h, 'up')} = 0 THEN 0 "
    f"WHEN COALESCE({latest(h, 'cpu_temp_c')}, 0) >= 85 THEN 3 "
    f"WHEN {latest(h, 'mem_total_gb - mem_used_gb')} < 2 OR COALESCE({latest(h, 'swap_used_pct')}, 0) >= 95 THEN 2 "
    f"ELSE 1 END AS \"{n}\"" for h, n in HOSTS)
pc_map = lamp_map("UP", "DOWN", "STALE")
pc_map[0]["options"].update({"2": {"text": "MEM", "color": YELLOW}, "3": {"text": "HOT", "color": YELLOW}})
panel("stat", "Up", pc_lamp, 6, 6, 0, mappings=pc_map, colorMode="background", graphMode="none",
      textMode="value_and_name")
for x, (col, title, warn, crit) in ((6, ("cpu_pct", "CPU", 70, 90)), (12, ("mem_used_pct", "RAM", 70, 90))):
    panel("bargauge", title, "SELECT " + ", ".join(f"{latest(h, col)} AS \"{n}\"" for h, n in HOSTS), 6, 6, x,
          unit="percent", min=0, max=100, thresholds=PCT(warn, crit), orientation="horizontal",
          displayMode="gradient", showUnfilled=True)
# -1 = data disk not mounted (shown red as UNMOUNTED).
disk_sel = [f"{latest(h, c + '_pct')} AS \"{n}\"" for h, c, n in DISKS if c in ("disk_root", "disk_c")] + [
    f"CASE WHEN {latest('kor', 'mount_ext_hdd')} = 0 THEN -1 ELSE {latest('kor', 'disk_ext_hdd_pct')} END "
    "AS \"KOR ext HDD\"",
    f"CASE WHEN {latest('shifter', 'mount_rawdata')} = 0 THEN -1 ELSE {latest('shifter', 'disk_rawdata_pct')} END "
    "AS \"Shifter rawdata\""]
panel("bargauge", "Disk", "SELECT " + ", ".join(disk_sel), 6, 6, 18, unit="percent", min=0, max=100,
      thresholds=PCT(80, 90), orientation="horizontal", displayMode="gradient", showUnfilled=True,
      mappings=[{"type": "value", "options": {"-1": {"text": "UNMOUNTED", "color": RED}}}])
y += 6
panel("timeseries", "CPU [%]", per_host_series(lambda h, c: "cpu_pct", items), 12, 7, 0, unit="percent", min=0, max=100)
panel("timeseries", "RAM [%]", per_host_series(lambda h, c: "mem_used_pct", items), 12, 7, 12, unit="percent",
      min=0, max=100)
y += 7

to("details")
row("PCs")
LINUX = [("kor", "KOR"), ("shifter", "Shifter")]


def load_sql(host):
    sel = ", ".join(f"AVG({c}) AS \"{c}\"" for c in ("load1", "load5", "load15"))
    return (f"SELECT (ts_epoch / {BUCKET}) * {BUCKET} AS time, {sel}, MAX(ncpu) AS \"cores\" FROM pc_health "
            f"WHERE host = '{host}' AND ts_epoch >= $__from / 1000 AND ts_epoch < $__to / 1000 GROUP BY 1 ORDER BY 1")


cores_dashed = [{"matcher": {"id": "byName", "options": "cores"},
                 "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
                                {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}]}]
panel("stat", "Uptime", "SELECT " + ", ".join(f"{latest(h, 'uptime_h')} / 24.0 AS \"{n}\"" for h, n in HOSTS),
      8, 4, 0, unit="d", decimals=1, colorMode="none", graphMode="none", textMode="value_and_name")
panel("stat", "Disk free", "SELECT " + ", ".join(f"{latest(h, c + '_free_gb')} AS \"{n}\"" for h, c, n in DISKS),
      16, 4, 8, unit="decgbytes", decimals=1, colorMode="none", graphMode="none", textMode="value_and_name")
y += 4
for x, (h, n) in zip((0, 8), LINUX):
    panel("timeseries", f"Load average {n}", load_sql(h), 8, 7, x, min=0, overrides=cores_dashed)
panel("timeseries", "CPU temperature [C]", per_host_series(lambda h, c: "cpu_temp_c", [(h, None, n) for h, n in LINUX]),
      8, 7, 16, unit="celsius")
y += 7
panel("timeseries", "RAM used [GB]", per_host_series(lambda h, c: "mem_used_gb", items), 8, 7, 0,
      unit="decgbytes", min=0)
panel("timeseries", "Swap [%]", per_host_series(lambda h, c: "swap_used_pct", items), 8, 7, 8, unit="percent",
      min=0, max=100)
panel("timeseries", "Disk used trend [%]", per_host_series(lambda h, c: c + "_pct", DISKS), 8, 7, 16,
      unit="percent", timeFrom="7d")
y += 7
panel("timeseries", "Network receive", per_host_series(lambda h, c: "net_rx_bps", items), 12, 7, 0,
      unit="Bps", min=0)
panel("timeseries", "Network send", per_host_series(lambda h, c: "net_tx_bps", items), 12, 7, 12, unit="Bps", min=0)
y += 7

to("main")
row("Camera")
panels.append({"type": "text", "title": "Camera", "gridPos": {"x": 0, "y": y, "w": 24, "h": 3},
               # target=_blank keeps Grafana from routing a same-host link inside its own app.
               "options": {"mode": "html",
                           "content": '<a href="https://hk-precal-kor.tailcec6f0.ts.net/camera/" target="_blank" '
                                      'rel="noopener noreferrer">Open camera (password)</a>'}})

to("main")
LINKS = {"main": ("Details", "/d/precal-details"), "details": ("Main (common view)", "/d/precal-overview")}
META = {"main": ("precal-overview", "Precalibration", "precal_overview.json"),
        "details": ("precal-details", "Precalibration Details", "precal_details.json")}
for name, (uid, title, fname) in META.items():
    plist = BOARDS[name][0] if name != cur else panels
    ids = iter(range(1, 1000))
    for p in plist:
        p["id"] = next(ids)
        for sub in p.get("panels", []):
            sub["id"] = next(ids)
    link_title, link_url = LINKS[name]
    dash = {"uid": uid, "title": title, "timezone": "browser", "editable": False,
            "refresh": "1m", "time": {"from": "now-24h", "to": "now"}, "schemaVersion": 39, "tags": ["precal"],
            "links": [{"title": link_title, "type": "link", "url": link_url, "targetBlank": False,
                       "icon": "dashboard", "asDropdown": False, "includeVars": False, "keepTime": True}],
            "panels": plist}
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboards", fname)
    with open(out, "w") as f:
        json.dump(dash, f, indent=1)
    print(out, len(plist), "panels,", sum(p["type"] != "row" for p in plist), "without rows")
