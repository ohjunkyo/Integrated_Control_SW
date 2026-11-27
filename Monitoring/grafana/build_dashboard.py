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
T_NAMES = {7: '"T7 (AUS box)"'}   # T7 probe sits inside the AUS group's box
B_NAMES = {1: '"AUS (M_AUS)"', 2: '"Rot2 (M_KOR2)"', 3: '"Rot1 (M_KOR1)"'}   # B-field log names; M_KOR1 sits at Rot1, M_KOR2 at Rot2
B_COLS = {1: "m_aus", 2: "m_kor2", 3: "m_kor1"}   # component columns since the log header was renamed

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
        dflt.setdefault("custom", {"spanNulls": 7200000})   # join buckets up to 2 h apart (30 d ranges use 72 min)
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
HOSTS = [("kor", "KOR"), ("bfield", "B-field"), ("shifter", "Shifter"), ("aus", "AUS")]
DISKS = [("kor", "disk_root", "KOR root"), ("kor", "disk_ext_hdd", "KOR ext HDD"), ("shifter", "disk_root", "Shifter root"),
         ("shifter", "disk_rawdata", "Shifter rawdata"), ("bfield", "disk_c", "B-field C:"),
         ("aus", "disk_root", "AUS root")]
UNITS = [("unit_ups_guard_service", "UPS guard"), ("unit_remote_ops_service", "Remote page"),
         ("unit_daq_data_monitoring_service", "Data monitor :8090"), ("unit_network_watchdog_timer", "Supervisor"),
         ("unit_bfield_log_sync_timer", "B-field sync"), ("unit_backup_logs_timer", "Log backup"),
         ("unit_precal_monitor_timer", "Collector")]
OK_AGES = [("backup_logs_last_ok", "Log backup", 26 * 3600), ("bfield_sync_last_ok", "B-field sync", 20 * 60),
           ("data_sync_last_ok", "Data sync", 26 * 3600)]
GREEN, YELLOW, RED, GRAY, ORANGE = "green", "yellow", "red", "gray", "orange"
PCT = lambda warn, crit: [{"color": GREEN, "value": None}, {"color": YELLOW, "value": warn}, {"color": RED, "value": crit}]
# A collector source is late when it has not run successfully for 3 of its periods (+1 min).
RUN_LATE = f"{NOW} - last_ok_epoch > 3 * COALESCE(period_s, 60) + 60"


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
                                                    for c, n in lamps) + " " + LATEST, 24, 2, 0,
      mappings=lamp_map("OK", "FAIL", "STALE"), colorMode="background", graphMode="none", textMode="value_and_name",
      text={"titleSize": 12, "valueSize": 16})
y += 2

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
# Link (KOR CAEN on USB, HK DPB answers ping) and run activity per site.
# HK run activity: a run file written in the last 10 min (hk_run probe, every 5 min).
kor_scan = ("CASE WHEN scan_running = 1 THEN 'SCANNING' || CASE WHEN scan_total > 0 THEN '  ' || scan_done || '/' "
            "|| scan_total ELSE '' END ELSE 'IDLE' END")
hk_scan = (f"(SELECT CASE WHEN {NOW} - last_ok_epoch > 900 THEN 'STALE' WHEN {NOW} - "
           "(SELECT MAX(COALESCE(end_epoch, ts_epoch)) FROM hk_run) < 600 THEN 'RUNNING' ELSE 'IDLE' END "
           "FROM source_status WHERE name = 'hk_run')")
DAQ_MAP = [{"type": "value", "options": {"1": {"text": "OK", "color": GREEN}, "0": {"text": "OFF", "color": RED},
                                         "-1": {"text": "STALE", "color": GRAY}, "-2": {"text": "N/A", "color": GRAY},
                                         "RUNNING": {"color": GREEN}, "IDLE": {"color": GRAY},
                                         "STALE": {"color": GRAY}, "N/A": {"color": GRAY},
                                         "OFF": {"color": GRAY}, "RAMP": {"color": YELLOW},
                                         "PAUSED": {"color": YELLOW},
                                         "ALARM": {"color": RED}}},
           {"type": "regex", "options": {"pattern": "^(SCANNING|ON).*", "result": {"color": GREEN}}},
           {"type": "regex", "options": {"pattern": "^NEXT.*", "result": {"color": GRAY}}}]
box = dict(colorMode="background", graphMode="none", textMode="value_and_name", orientation="horizontal",
           text={"titleSize": 14, "valueSize": 20}, mappings=DAQ_MAP)
# Two columns: left = our side (KOR and HK), right = AUS (digitizer and linear stage); run tables below each.
# HK link = the DPB answers a ping from the Shifter PC (shifter_health probe, every 5 min); KOR only lends the stages.
hk_link = "COALESCE(" + latest("shifter", f"CASE WHEN {NOW} - ts_epoch > 900 THEN -1 ELSE ping_dpb END") + ", -2)"
kor_link = f"CASE WHEN {NOW} - time > 600 THEN -1 ELSE COALESCE(caen_connected, -2) END"
for x, (title, link, scan) in enumerate((("KOR (CAEN)", kor_link, kor_scan), ("HK", hk_link, hk_scan))):
    panel("stat", title, f'SELECT {link} AS "Link", {scan} AS "Scan" {LATEST}', 6, 4, 6 * x, **box)
panels[-1]["description"] = ("Link = the HK DPB answers a ping from the Shifter PC (read only, sampled every 5 min). "
                             "Scan = an HK Digitizer scan is running from DAQ Control.")
# AUS is read only (aus_health probe, every 5 min): digitizer on USB, HV supply state, robot arm ping, run queue.
AUS_STALE = f"{NOW} - ts_epoch > 900"
aus_lamp = lambda col: f"COALESCE({latest('aus', f'CASE WHEN {AUS_STALE} THEN -1 ELSE {col} END')}, -2)"
aus_hv = latest("aus", f"CASE WHEN {AUS_STALE} THEN 'STALE' WHEN COALESCE(hv_link, 0) = 0 THEN 'N/A' "
                "WHEN hv_alarm = 1 THEN 'ALARM' WHEN hv_ramp = 1 THEN 'RAMP' "
                "WHEN hv_n_on > 0 THEN 'ON  ' || hv_n_on || ' ch' ELSE 'OFF' END")
aus_scan = latest("aus", f"CASE WHEN {AUS_STALE} THEN 'STALE' WHEN run_running IS NULL THEN 'N/A' "
                  "WHEN run_running = 1 OR proc_wavedump = 1 THEN 'SCANNING' WHEN run_paused = 1 THEN 'PAUSED' "
                  "WHEN run_next IS NOT NULL THEN 'NEXT ' || substr(run_next, 12, 5) ELSE 'IDLE' END")
panel("stat", "AUS (CAEN)", f'SELECT {aus_lamp("usb_caen")} AS "Link", {aus_scan} AS "Scan", {aus_hv} AS "HV", '
      f'{aus_lamp("ping_arm")} AS "Arm"', 6, 4, 12, **box)
panels[-1]["description"] = ("Read only, sampled every 5 min. Link = CAEN digitizer on the AUS PC's USB. "
                             "HV = AUS CAEN HV supply (ON with the number of channels, RAMP, ALARM = trip or "
                             "over-limit flag). Arm = robot arm answers ping. Scan = AUS run queue: SCANNING, "
                             "NEXT with the start time of the next queued run, PAUSED, IDLE.")
# AUS linear stage and runs (aus_run probe, every 5 min, read only): position from the AUS terminal logs.
AUS_RUN_STALE = f"(SELECT {NOW} - last_ok_epoch > 900 FROM source_status WHERE name = 'aus_run')"
STAGE_LAST = "FROM aus_stage ORDER BY ts_epoch DESC, rowid DESC LIMIT 1"
STAGE_POS = "FROM aus_stage WHERE pmt IS NOT NULL ORDER BY ts_epoch DESC, rowid DESC LIMIT 1"
aus_stage = (f"CASE WHEN COALESCE({AUS_RUN_STALE}, 1) = 1 THEN 'STALE' "
             f"WHEN (SELECT CAST(event AS TEXT) {STAGE_LAST}) = 'move_start' THEN 'MOVING' "
             f"ELSE COALESCE((SELECT 'PMT' || CAST(pmt AS INT) || ' (' || CAST(mm AS INT) || ' mm)' {STAGE_POS}), 'N/A') END")
panel("stat", "AUS linear stage", f'SELECT {aus_stage} AS "Position", '
      f"COALESCE((SELECT substr(ts, 6, 11) {STAGE_LAST}), '-') AS \"Last stage log\"", 6, 4, 18,
      colorMode="background", graphMode="none", textMode="value_and_name", orientation="horizontal",
      text={"titleSize": 14, "valueSize": 20},
      mappings=[{"type": "value", "options": {"STALE": {"color": GRAY}, "N/A": {"color": GRAY},
                                              "MOVING": {"color": YELLOW}}},
                {"type": "regex", "options": {"pattern": "^PMT.*", "result": {"color": "blue"}}}],
      overrides=[{"matcher": {"id": "byName", "options": "Last stage log"}, "properties": [plain]}])
panels[-1]["description"] = ("Read only, sampled every 5 min from the AUS terminal logs. Position = PMT the linear "
                             "stage was last verified or moved to (PMT1 = 1074 mm, PMT2 = 240 mm); MOVING = a move "
                             "started and its arrival is not logged yet.")
y += 4
AUS_TYPE = "CASE WHEN instr(type, ' [') > 0 THEN substr(type, 1, instr(type, ' [') - 1) ELSE CAST(type AS TEXT) END"
aus_over = [{"matcher": {"id": "byName", "options": "status"}, "properties": [
                {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                {"id": "mappings", "value": [{"type": "value", "options": {"SUCCESS": {"color": GREEN},
                                                                           "FAILED": {"color": RED}}}]}]},
            {"matcher": {"id": "byName", "options": "duration"}, "properties": [{"id": "unit", "value": "s"}]}]
col_w = lambda col, px: {"matcher": {"id": "byName", "options": col}, "properties": [{"id": "custom.width", "value": px}]}
PURPOSES = {"General scan": "blue", "Stability": "green", "Manual": "orange", "Self trigger (dark)": "purple",
            "External trigger (LDs off)": "red", "Other": "gray", "HK Digitizer": "#00A896"}
pur_over = [{"matcher": {"id": "byName", "options": n}, "properties": [{"id": "color", "value": {"mode": "fixed",
             "fixedColor": c}}]} for n, c in PURPOSES.items()]
PUR = "COALESCE(NULLIF(CAST(purpose AS TEXT), ''), 'Other')"
# CAEN DT5730 runs (run registry) and HK Digitizer runs (HK PC metadata) in one stream.
RUNS = "(SELECT ts_epoch, purpose, laser_ma FROM daq_run UNION ALL SELECT ts_epoch, 'HK Digitizer', NULL FROM hk_run)"
per_day = ", ".join(f"SUM(CASE WHEN {PUR} = '{n}' THEN 1 ELSE 0 END) AS \"{n}\"" for n in PURPOSES)
day_bars = dict(timeFrom="30d", min=0, legend={"displayMode": "list", "placement": "bottom"},
                custom={"drawStyle": "bars", "fillOpacity": 85, "lineWidth": 1, "barAlignment": 0,
                        "stacking": {"mode": "normal", "group": "A"}, "spanNulls": False})
panel("timeseries", "KOR runs per day (30 days)", f"SELECT {DAY} AS time, {per_day} FROM {RUNS} "
      f"WHERE ts_epoch >= {NOW} - 30 * 86400 GROUP BY 1 ORDER BY 1", 12, 6, 0, overrides=pur_over, **day_bars)
# AUS run types of run_log.txt, same colours as the matching KOR purposes.
AUS_KINDS = [("PMT scan", "type LIKE '%PMT Scan%'", "blue"), ("Dark rate", "type LIKE 'Dark Rate%'", "purple"),
             ("Test", "type LIKE 'Test%'", "orange"), ("Stage move", "type LIKE 'Move Linear%'", "#00A896")]
aus_day = ", ".join(f"SUM(CASE WHEN {c} THEN 1 ELSE 0 END) AS \"{n}\"" for n, c, _ in AUS_KINDS)
aus_day += ", SUM(CASE WHEN " + " OR ".join(c for _, c, _ in AUS_KINDS) + " THEN 0 ELSE 1 END) AS \"Other\""
panel("timeseries", "AUS runs per day (30 days)", f"SELECT {DAY} AS time, {aus_day} FROM aus_run "
      f"WHERE ts_epoch >= {NOW} - 30 * 86400 GROUP BY 1 ORDER BY 1", 12, 6, 12, **day_bars,
      overrides=[{"matcher": {"id": "byName", "options": n}, "properties": [{"id": "color", "value": {
          "mode": "fixed", "fixedColor": c}}]} for n, c in [(n, c) for n, _, c in AUS_KINDS] + [("Other", "gray")]])
y += 6
scan_over = [{"matcher": {"id": "byName", "options": "complete"},
              "properties": [{"id": "unit", "value": "percent"}, {"id": "min", "value": 0}, {"id": "max", "value": 100},
                             {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "basic"}},
                             {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                                 {"color": "orange", "value": None}, {"color": "green", "value": 100}]}}]}]
scan_over.append({"matcher": {"id": "byName", "options": "status"}, "properties": [
    {"id": "custom.cellOptions", "value": {"type": "color-text"}},
    {"id": "mappings", "value": [{"type": "value", "options": {"Complete": {"color": "green"}, "Running": {"color": "blue"},
                                                               "Interrupted": {"color": "orange"}}}]}]})
# Newest scan while the General Scan flag is set: its last run came after the flag was last seen off (1 min samples).
SCAN_RUNNING = (f"(SELECT active_scan IS NOT NULL AND {NOW} - ts_epoch < 300 FROM daq_status ORDER BY ts_epoch DESC LIMIT 1) "
                "AND MAX(ts_epoch) = (SELECT MAX(ts_epoch) FROM daq_run WHERE scan_id IS NOT NULL AND scan_id != '') "
                "AND MAX(ts_epoch) > COALESCE((SELECT MAX(ts_epoch) FROM daq_status WHERE active_scan IS NULL), 0)")
NPOS = "COUNT(DISTINCT CAST(rot2 AS TEXT) || '/' || CAST(tilt2 AS TEXT))"   # stage positions in the group
SCAN_GROUPS = "FROM daq_run WHERE scan_id IS NOT NULL AND scan_id != '' GROUP BY scan_id "
# Both run tables: name, start, end (MM-DD HH:MM), details, status; fixed widths so seven columns fit half a row.
PTS = "COALESCE(MAX(CAST(scan_points AS INT)), 46)"   # points the scan was planned with (routine step), else the full grid
panel("table", "KOR general scans", "SELECT scan_id || COALESCE(' ' || NULLIF(MAX(scan_label), ''), '') AS scan, "
      "substr(MIN(ts), 6, 11) AS start, substr(MAX(ts), 6, 11) AS \"end\", "
      f"CAST(MAX(CAST(wavelength AS INT)) AS TEXT) || ' nm' AS wl, COUNT(*) || ' / ' || {PTS} AS runs, "
      f"CASE WHEN {SCAN_RUNNING} THEN 'Running' WHEN COUNT(*) >= {PTS} THEN 'Complete' ELSE 'Interrupted' END AS status, "
      f"MIN(100.0 * COUNT(*) / {PTS}, 100) AS complete {SCAN_GROUPS}HAVING {NPOS} > 1 "
      "ORDER BY MIN(ts_epoch) DESC LIMIT 15", 12, 8, 0, cellHeight="sm",
      overrides=scan_over + [col_w("scan", 265), col_w("start", 95), col_w("end", 95), col_w("wl", 65),
                             col_w("runs", 70), col_w("status", 95)])
panel("table", "AUS runs (latest)", "SELECT CAST(run AS TEXT) AS run, "
      "COALESCE(substr(datetime(start_epoch, 'unixepoch', 'localtime'), 6, 11), '-') AS start, "
      f"substr(ts, 6, 11) AS \"end\", {AUS_TYPE} AS type, CAST(pmts AS TEXT) AS pmt, "
      "COALESCE('PMT' || CAST(stage_pmt AS INT), '-') AS stage, CAST(status AS TEXT) AS status "
      "FROM aus_run ORDER BY ts_epoch DESC LIMIT 15", 12, 8, 12, cellHeight="sm",
      overrides=aus_over + [col_w("run", 185), col_w("start", 100), col_w("end", 100), col_w("pmt", 95),
                            col_w("stage", 65), col_w("status", 90)])
y += 8

row("Daily Routine")
ST_COLORS = {"Done": GREEN, "Failed": RED, "Running": "blue", "Skipped": ORANGE, "Pending": GRAY, "Disabled": GRAY,
             "finished": GREEN, "failed": RED, "stopped": ORANGE,
             "success": GREEN, "running": "blue", "pending": GRAY}   # lower case: AUS scheduler
st_over = lambda col: [{"matcher": {"id": "byName", "options": col}, "properties": [
    {"id": "custom.cellOptions", "value": {"type": "color-text"}},
    {"id": "mappings", "value": [{"type": "value", "options": {k: {"color": c} for k, c in ST_COLORS.items()}}]}]}]
# Name on the left, value on the right, one line per field (as the DAQ boxes).
rt_box = dict(colorMode="none", graphMode="none", textMode="value_and_name", orientation="horizontal",
              text={"titleSize": 13, "valueSize": 18})
panel("stat", "KOR routine", "SELECT (SELECT CAST(routine AS TEXT) FROM routine_now LIMIT 1) AS \"Routine\", "
      "COALESCE((SELECT CAST(step AS TEXT) FROM routine_now WHERE status = 'Running' LIMIT 1), 'Idle') AS \"Now\", "
      "COALESCE((SELECT CAST(step AS TEXT) || ' (' || CAST(plan AS TEXT) || ')' FROM routine_now "
      "WHERE status = 'Pending' AND enabled = 1 ORDER BY idx LIMIT 1), '-') AS \"Next\", "
      "COALESCE((SELECT CAST(routine AS TEXT) || ' ' || CAST(result AS TEXT) || ', ' || substr(run_start, 6, 11) "
      "FROM routine_run ORDER BY run_start DESC LIMIT 1), '-') AS \"Last run\"", 12, 5, 0, **rt_box)
# AUS scheduler queue (aus_telemetry probe, every 5 min, read only): same four lines as the KOR routine.
AUS_Q_STALE = f"COALESCE((SELECT {NOW} - last_ok_epoch > 900 FROM source_status WHERE name = 'aus_telemetry'), 1) = 1"
panel("stat", "AUS schedule", f"SELECT CASE WHEN {AUS_Q_STALE} THEN 'STALE' ELSE COALESCE((SELECT COUNT(*) || ' runs, ' || "
      "SUM(status = 'pending') || ' pending' || CASE WHEN MAX(paused) = 1 THEN ' (PAUSED)' ELSE '' END "
      "FROM aus_sched), 'empty') END AS \"Queue\", "
      "COALESCE((SELECT CAST(name AS TEXT) FROM aus_sched WHERE status = 'running' LIMIT 1), 'Idle') AS \"Now\", "
      "COALESCE((SELECT CAST(name AS TEXT) || ' (' || substr(plan, 6, 11) || ')' FROM aus_sched "
      "WHERE status = 'pending' ORDER BY plan_epoch LIMIT 1), '-') AS \"Next\", "
      "COALESCE((SELECT CAST(name AS TEXT) || ' ' || CAST(status AS TEXT) || ', ' || substr(finished, 6, 11) "
      "FROM aus_sched WHERE end_epoch IS NOT NULL ORDER BY end_epoch DESC LIMIT 1), '-') AS \"Last run\"",
      12, 5, 12, **rt_box)
panels[-1]["description"] = ("Read only, sampled every 5 min from the AUS scheduler queue (scheduled_runs.json). "
                             "The queue keeps finished runs until the AUS operator clears it.")
y += 5
rt_cols = lambda name: st_over("status") + [col_w("#", 45), col_w(name, 175), col_w("start", 100), col_w("status", 90),
                                            col_w("started", 100), col_w("finished", 100)]
panel("table", "KOR routine steps (current)", "SELECT idx + 1 AS \"#\", CAST(step AS TEXT) AS step, "
      "CAST(plan AS TEXT) AS start, CAST(status AS TEXT) AS status, substr(started, 6, 11) AS started, "
      "substr(finished, 6, 11) AS finished, CAST(note AS TEXT) AS note FROM routine_now ORDER BY idx",
      12, 8, 0, overrides=rt_cols("step"), cellHeight="sm")
panel("table", "AUS schedule (current queue)", "SELECT idx + 1 AS \"#\", CAST(name AS TEXT) AS run, "
      "substr(plan, 6, 11) AS start, CAST(status AS TEXT) AS status, substr(started, 6, 11) AS started, "
      "substr(finished, 6, 11) AS finished, CAST(type AS TEXT) || '  ' || COALESCE(CAST(serial AS TEXT), '') || "
      "COALESCE('  @PMT' || CAST(stage_pmt AS INT), '') || CASE WHEN COALESCE(note, '') != '' THEN '  - ' || "
      "CAST(note AS TEXT) ELSE '' END AS note FROM aus_sched ORDER BY idx",
      12, 8, 12, overrides=rt_cols("run"), cellHeight="sm")
y += 8

to("details")
row("Daily Routine")
# Finished runs from history plus the steps of the run in progress (not in history yet).
SINCE_HIST = "COALESCE((SELECT MAX(end_epoch) FROM routine_run), 0)"
RT_EVENTS = (f"SELECT start_epoch AS time, CAST(step AS TEXT) || ' - ' || CAST(status AS TEXT) AS label FROM routine_step "
             "WHERE start_epoch IS NOT NULL UNION ALL SELECT end_epoch, 'Idle' FROM routine_step WHERE end_epoch IS NOT NULL "
             f"UNION ALL SELECT start_epoch, CAST(step AS TEXT) || ' - ' || CAST(status AS TEXT) FROM routine_now "
             f"WHERE start_epoch > {SINCE_HIST} UNION ALL SELECT end_epoch, 'Idle' FROM routine_now "
             f"WHERE end_epoch > {SINCE_HIST}")
panel("state-timeline", "Routine steps over time", f"SELECT time, label AS step FROM ({RT_EVENTS}) "
      "WHERE time >= $__from / 1000 - 86400 AND time < $__to / 1000 "
      "ORDER BY time, CASE label WHEN 'Idle' THEN 0 ELSE 1 END", 24, 5, 0, timeFrom="7d",
      mappings=[{"type": "value", "options": {"Idle": {"color": "transparent", "text": " "}}}] +
               [{"type": "regex", "options": {"pattern": f".* - {k}$", "result": {"color": c}}}
                for k, c in ST_COLORS.items() if k[0].isupper()],
      showValue="auto", mergeValues=True, legend={"showLegend": False})
y += 5
# AUS runs on the same 7-day axis: label = type, PMT serial and stage position; start = end - duration.
AUS_LABEL = (f"{AUS_TYPE} || '  ' || CAST(pmts AS TEXT) || COALESCE('  @PMT' || CAST(stage_pmt AS INT), '') || "
             "CASE WHEN status = 'SUCCESS' THEN '' ELSE ' - ' || CAST(status AS TEXT) END")
AUS_EVENTS = (f"SELECT start_epoch AS time, {AUS_LABEL} AS label FROM aus_run WHERE start_epoch IS NOT NULL "
              "UNION ALL SELECT end_epoch, 'Idle' FROM aus_run WHERE start_epoch IS NOT NULL")
AUS_TL = [(".* - FAILED$", RED), ("^Single PMT Scan.*", "blue"), ("^Dark Rate.*", "purple"),
          ("^Move Linear Stage.*", ORANGE), ("^Test.*", GRAY)]
panel("state-timeline", "AUS runs over time", f"SELECT time, label AS run FROM ({AUS_EVENTS}) "
      "WHERE time >= $__from / 1000 - 86400 AND time < $__to / 1000 "
      "ORDER BY time, CASE label WHEN 'Idle' THEN 0 ELSE 1 END", 24, 4, 0, timeFrom="7d",
      mappings=[{"type": "value", "options": {"Idle": {"color": "transparent", "text": " "}}}] +
               [{"type": "regex", "options": {"pattern": pat, "result": {"color": c}}} for pat, c in AUS_TL],
      showValue="auto", mergeValues=True, legend={"showLegend": False})
y += 4
# KOR runs that were taking data while the AUS linear stage moved (run window = 240 s from its start).
STAGE_OVERLAP = ("SELECT r.ts AS \"KOR run start\", CAST(CAST(r.date AS INT) AS TEXT) AS date, CAST(CAST(r.run AS INT) AS TEXT) AS run, "
                 "CAST(r.kind AS TEXT) AS kind, CAST(r.wavelength AS TEXT) AS wl, s.ts AS \"stage move start\", "
                 "'PMT' || CAST(s.from_pmt AS INT) || ' to PMT' || CAST(s.to_pmt AS INT) AS move, "
                 "e.ts_epoch - s.ts_epoch AS \"move time\" FROM aus_stage s "
                 "LEFT JOIN aus_stage e ON e.folder = s.folder AND e.event = 'move_done' "
                 "JOIN daq_run r ON r.ts_epoch <= COALESCE(e.ts_epoch, s.ts_epoch + 60) AND r.ts_epoch + 240 >= s.ts_epoch "
                 "WHERE s.event = 'move_start' ORDER BY s.ts_epoch DESC, r.ts_epoch LIMIT 30")
panel("table", "KOR runs during an AUS stage move", STAGE_OVERLAP, 24, 4, 0, cellHeight="sm",
      overrides=[{"matcher": {"id": "byName", "options": "move time"}, "properties": [{"id": "unit", "value": "s"}]}])
panels[-1]["description"] = ("Flag only, nothing is paused: KOR runs that started less than 240 s before an AUS "
                             "linear stage move began and before it ended. Empty = no overlap.")
y += 4
panel("table", "Routine step history", "SELECT run_start AS routine_start, CAST(routine AS TEXT) AS routine, "
      "CAST(step AS TEXT) AS step, CAST(status AS TEXT) AS status, CAST(started AS TEXT) AS started, "
      "CAST(finished AS TEXT) AS finished, CAST(note AS TEXT) AS note FROM routine_step WHERE enabled = 1 "
      "ORDER BY run_start DESC, idx LIMIT 200", 24, 8, 0, overrides=st_over("status"), cellHeight="sm",
      custom={"filterable": True})
y += 8
row("DAQ")
panel("stat", "DAQ", f"SELECT {progress} AS \"Progress\", (SELECT {NOW} - MAX(ts_epoch) FROM daq_run) "
      f"AS \"Since last run\", {run_label} AS \"Last run\" {LATEST}", 24, 4, 0, colorMode="value", graphMode="none",
      textMode="value_and_name", justifyMode="center", text={"titleSize": 13, "valueSize": 24}, overrides=daq_over)
y += 4
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
# AUS HV and laser values exist only while an AUS run records its telemetry (1 min averages, read only).
aus_tele = lambda cols: wide([(c, f'"{n}"') for c, n in cols], "aus_telemetry")
AUS_CH = (1, 2, 3)
panel("timeseries", "KOR HV voltage [V]", f"SELECT {hv_bucket} AS time, {vsel} FROM monitoring_data WHERE {HV_RANGE} "
      f"AND NOT {READERR_SQL} GROUP BY 1 ORDER BY 1", 12, 6, 0, ds=HV, unit="volt", min=0)
panel("timeseries", "AUS HV voltage [V]", aus_tele([(f"caen_ch{c}_v_mon", f"ch{c}") for c in AUS_CH]), 12, 6, 12,
      unit="volt", min=0)
y += 6
panel("timeseries", "KOR HV current [uA]", f"SELECT {hv_bucket} AS time, {isel} FROM monitoring_data WHERE {HV_RANGE} "
      f"AND NOT {READERR_SQL} GROUP BY 1 ORDER BY 1", 12, 6, 0, ds=HV, unit="suffix: µA", min=0)
panel("timeseries", "AUS HV current [uA]", aus_tele([(f"caen_ch{c}_i_mon", f"ch{c}") for c in AUS_CH]), 12, 6, 12,
      unit="suffix: µA", min=0)
y += 6
BOX_COLORS = {"Rot#1": "blue", "Rot#2": "orange", "Mon.": "green"}   # same colour per box in both panels
box_over = [{"matcher": {"id": "byName", "options": n}, "properties": [
    {"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]} for n, c in BOX_COLORS.items()]
for x, (k, title, unit) in ((0, ("T", "KOR dark box temperature [C]", "celsius")),
                            (6, ("H", "KOR dark box humidity [%]", "humidity"))):
    sel = ", ".join(f'AVG(Dark_Box_{b}_{k}) AS "{n}"' for b, n in BOXES)
    panel("timeseries", title, f"SELECT {hv_bucket} AS time, {sel} FROM monitoring_data WHERE {HV_RANGE} "
          "GROUP BY 1 ORDER BY 1", 6, 7, x, ds=HV, unit=unit, overrides=box_over)
AUS_LAST = "FROM aus_telemetry ORDER BY ts_epoch DESC LIMIT 1"
panel("stat", "AUS HV (last sample)", f"SELECT COALESCE((SELECT substr(ts, 6, 11) {AUS_LAST}), '-') AS \"Sample\", " +
      ", ".join(f"COALESCE((SELECT printf('%.0f V   %.1f uA', caen_ch{c}_v_mon, caen_ch{c}_i_mon) {AUS_LAST}), '-') "
                f"AS \"ch{c}\"" for c in AUS_CH), 12, 7, 12, **rt_box)
panels[-1]["description"] = "Newest telemetry line of the AUS runs; there is no AUS HV data between runs."
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

to("main")
row("B-field")
panel("timeseries", "Coil current [A] (0 A = coils OFF)", wide([(f"i{n}", f"I{n}") for n in (1, 2, 3, 4)], "bfield"),
      8, 7, 0, unit="amp", thresholds=[{"color": RED, "value": None}, {"color": GREEN, "value": 0.01}],
      custom={"spanNulls": 7200000, "thresholdsStyle": {"mode": "line+area"}})
panel("timeseries", "|B| [mG]", wide([(f"m{n}_mg", B_NAMES[n]) for n in (3, 2, 1)], "bfield"), 8, 7, 8)
panel("timeseries", "Temperature [C]", wide([(f"t{n}", T_NAMES.get(n, f"T{n}")) for n in (3, 5, 7)], "bfield"),
      8, 7, 16, unit="celsius")
y += 7

to("details")
row("B-field")
panel("timeseries", "|B| all magnetometers [mG]", wide([(f"m{n}_mg", B_NAMES.get(n, f"M{n}")) for n in (3, 2, 1)], "bfield"),
      12, 7, 0)
panel("timeseries", "Temperature T1-T7 [C]",
      wide([(f"t{n}", T_NAMES.get(n, f"T{n}")) for n in (1, 2, 3, 4, 5, 6, 7)], "bfield"), 12, 7, 12, unit="celsius")
y += 7
for k, n in enumerate((3, 2, 1)):   # the log holds gauss; the panels show mG like |B|
    panel("timeseries", f"B components {B_NAMES[n].strip(chr(34))} [mG]",
          wide([(f"COALESCE(m{n}_{a}, {B_COLS[n]}_{a}) * 1000", a.upper()) for a in "xyz"], "bfield"), 8, 7, 8 * k)
y += 7

to("main")
row("Laser")
# LD lamp per wavelength: -1 when that controller has not logged for 5 min.
panel("stat", "KOR LD", "SELECT " + ", ".join(
      f"(SELECT CASE WHEN {NOW} - ts_epoch > 300 THEN -1 ELSE ld_on END FROM laser WHERE wl = '{w}' "
      f"ORDER BY ts_epoch DESC LIMIT 1) AS \"{w}\"" for w in WLS), 4, 7, 0,
      mappings=[{"type": "value", "options": {"1": {"text": "ON", "color": GREEN}, "0": {"text": "OFF", "color": GRAY},
                                              "-1": {"text": "NO DATA", "color": ORANGE}}}],
      colorMode="background", graphMode="none", textMode="value_and_name")
panel("timeseries", "KOR LD current while on (set) [mA]", per_wl("CASE WHEN ld_on = 1 THEN pulse_ma END"), 8, 7, 4,
      unit="suffix: mA", min=0, custom={"spanNulls": 7200000, "axisSoftMax": 200})
panel("timeseries", "AUS laser pulse current [mA]", aus_tele([("laser_pulse_current", "pulse")]), 6, 7, 12,
      unit="suffix: mA", min=0, custom={"spanNulls": 7200000, "axisSoftMax": 200})
panel("timeseries", "AUS SiPM current (as logged)", aus_tele([("sipm_ch1_current", "SiPM 1"), ("sipm_ch2_current", "SiPM 2")]),
      6, 7, 18)
y += 7
panel("timeseries", "KOR laser board temperature [C]", per_wl("temp_c"), 12, 7, 0, unit="celsius")
panel("timeseries", "AUS laser temperature [C]", aus_tele([("laser_tec_setpoint", "TEC set"), ("laser_tec_actual", "TEC actual"),
                                                         ("laser_board_temp", "board")]), 12, 7, 12, unit="celsius")
y += 7

to("details")
row("Laser")
# Only the 450 nm photodiode works; the others read garbage or nothing.
lsel = ", ".join(f"AVG(CASE WHEN wl = '{w}' THEN temp_c END) AS \"{w} T\"" for w in WLS) + \
    ", AVG(CASE WHEN wl = '450nm' AND ld_on = 1 THEN pd_current_ma END) AS \"450nm PD\""
panel("timeseries", "Laser temperature [C] and 450 nm photodiode [mA]", f"SELECT (ts_epoch / {BUCKET}) * {BUCKET} AS time, "
      f"{lsel} FROM laser WHERE ts_epoch >= $__from / 1000 AND ts_epoch < $__to / 1000 GROUP BY 1 ORDER BY 1",
      24, 7, 0, unit="celsius",
      overrides=[{"matcher": {"id": "byRegexp", "options": ".* PD$"}, "properties": [
          {"id": "unit", "value": "suffix: mA"}, {"id": "custom.axisPlacement", "value": "right"},
          {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}]}])
y += 7
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
# One lamp per PC as text: STALE, DOWN, UP, or UP with the reason (swap >= 95 %, < 2 GB free, CPU >= 85 C).
def pc_state(h):
    swap, free, temp = latest(h, "swap_used_pct"), latest(h, "mem_total_gb - mem_used_gb"), latest(h, "cpu_temp_c")
    return (f"CASE WHEN {NOW} - COALESCE({latest(h, 'ts_epoch')}, 0) > 600 THEN 'STALE' "
            f"WHEN {latest(h, 'up')} = 0 THEN 'DOWN' "
            f"WHEN COALESCE({temp}, 0) >= 85 THEN 'UP · ' || CAST(ROUND({temp}) AS INT) || ' °C' "
            f"WHEN COALESCE({swap}, 0) >= 95 THEN 'UP · SWAP ' || CAST(ROUND({swap}) AS INT) || '%' "
            f"WHEN {free} < 2 THEN 'UP · RAM ' || printf('%.1f', {free}) || ' GB free' ELSE 'UP' END")


panel("stat", "Up", "SELECT " + ", ".join(f'{pc_state(h)} AS "{n}"' for h, n in HOSTS), 6, 6, 0,
      mappings=[{"type": "value", "options": {"UP": {"color": GREEN}, "DOWN": {"color": RED}, "STALE": {"color": GRAY}}},
                {"type": "regex", "options": {"pattern": "^UP · .*", "result": {"color": YELLOW}}}],
      colorMode="background", graphMode="none", textMode="value_and_name")
panels[-1]["description"] = ("Yellow = up but short of resources: swap >= 95 %, under 2 GB RAM available, "
                             "or CPU temperature >= 85 C (KOR, Shifter). Gray STALE = no sample for 10 min.")
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
y += 3

to("main")
row("Services and sync")
svc_latest = f"FROM pc_health WHERE host = 'kor' ORDER BY ts_epoch DESC LIMIT 1"
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
