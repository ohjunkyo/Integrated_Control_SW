"""HK Digitizer (2nd PC) configuration and command building, same file, format and
formatters as the Tk app's AutomationManager hk_* methods (rotation_manager.py):
hk_config.json, dated snapshots in hk_configs/, ScanManager / vmodem / DPB commands, SSH.
Qt-free. CONFIG is the one shared dict (the Tk app's auto_mgr.hk_config)."""
import copy
import glob
import json
import os
import re
import shlex
import subprocess
from datetime import datetime

from core import env

CONFIG_FILE = os.path.join(env.DAQ_DIR, "hk_config.json")

DEFAULTS = {
    "run_number": 0,
    "acq_time": 10.0,
    "move_delay": 20.0,
    "scan_options": [
        {"label": "Threshold preset", "flag": "--threpreset", "value": "", "on": True, "bare": True},
        {"label": "Gate", "flag": "--gatelist", "value": "30", "on": True, "bare": False},
        {"label": "Pedestal run", "flag": "--pedestal", "value": "", "on": False, "bare": True},
        {"label": "Trigger ch", "flag": "--trgch", "value": "", "on": True, "bare": False},
        {"label": "Trigger Vth", "flag": "--trgch-Vth", "value": "", "on": True, "bare": False},
        {"label": "Channels", "flag": "--chanlist", "value": "", "on": True, "bare": False},
    ],
    "post_steps": [
        {"name": "Hit-tree", "on": False,
         "cmd": 'RunAnalysisChain.py --inputBinFile "{bin}" -t "{base}_hittree.root"'},
        {"name": "Event-tree", "on": False,
         "cmd": 'RunAnalysisChain.py --inputHitTreeFile "{base}_hittree.root" -T "{base}_eventtree.root"'},
    ],
    "work_dir": "~/hkelec/DiscreteSoftware/data/{date}/",
    "ssh_target": "hkpd@hkdaq",
    "setup_cmd": ". ~/setup_hkelec.sh",
    "dpb_host": "192.168.0.163",
    "dpb_setup_cmd": ("ssh -t root@{dpb_host} 'cd /run/media/*/script && "
                      "bash run-socat.sh 1 9001 && bash run-socat.sh 4 9004 && "
                      "bash run-timing.sh'"),
    "vmodem_device": "~/hkelec/DiscreteSoftware/DPBDaemon/vmodem/vmodem0",
    "vmodem_keys": "m,O",
    "scan_manager": "/home/hkpd/hkelec/pmt-scan/scripts/ScanManager_kawabata_precalib.py",
    "run_id": "Run{run}_normal_elecV4SN03_testrunDARK_daqch0_trigchnone_hvON_laserOFF_T{tilt}_R2{rot}_R3{rot3}",
}

# Infrastructure fields behind the admin password: (key, label, width; 0 = stretch)
PROTECTED = [
    ("ssh_user", "HK PC user", 160),
    ("ssh_host", "HK PC host / IP", 200),
    ("setup_cmd", "Setup cmd", 0),
    ("scan_manager", "ScanManager", 0),
    ("dpb_host", "DPB board host / IP", 200),
    ("dpb_setup_cmd", "DPB setup (2)", 0),
    ("vmodem_device", "vmodem device (3)", 0),
    ("vmodem_keys", "vmodem keys (3)", 160),
]

CONFIG = {}
SESSION = {"dpb_setup_done": False, "vmodem_done": False}   # not persisted, as in the Tk app
_engine = None   # the attached AutomationManager, which keeps these flags itself


def attach(auto_mgr):
    """Share the scan engine's hk_config dict and file (AutomationManager), so the dialog and
    the scan loop never hold two copies."""
    global CONFIG, CONFIG_FILE, _engine
    if auto_mgr is None or not isinstance(getattr(auto_mgr, "hk_config", None), dict):
        return
    CONFIG = auto_mgr.hk_config
    CONFIG_FILE = getattr(auto_mgr, "hk_config_file", CONFIG_FILE)
    _engine = auto_mgr


def session_flag(name, value=None):
    """dpb_setup_done / vmodem_done: get, or set when value is given (the engine's hk_<name> when attached)."""
    if value is None:
        return bool(getattr(_engine, f"hk_{name}", False)) if _engine is not None else SESSION.get(name, False)
    if _engine is not None:
        setattr(_engine, f"hk_{name}", value)
    else:
        SESSION[name] = value
    return value


def snapshot_dir():
    return os.path.join(os.path.dirname(CONFIG_FILE), "hk_configs")


def legacy_options(hk):
    """Pre-list hk_config (threshold_preset, trg_channel, ...) -> scan_options/post_steps."""
    d = dict(hk)
    if "scan_options" not in d:
        thr = str(d.get("threshold_preset") or "").strip()
        d["scan_options"] = [
            {"label": "Threshold preset", "flag": "--threpreset" if thr in ("", "1") else f"--threpreset{thr}",
             "value": "", "on": True, "bare": True},
            {"label": "Gate", "flag": "--gatelist", "value": str(d.get("gatelist", 30)), "on": True, "bare": False},
            {"label": "Pedestal run", "flag": "--pedestal", "value": "", "on": bool(d.get("pedestal_mode")), "bare": True},
            {"label": "Trigger ch", "flag": "--trgch", "value": str(d.get("trg_channel") or ""), "on": True, "bare": False},
            {"label": "Trigger Vth", "flag": "--trgch-Vth", "value": str(d.get("trg_vth") or ""), "on": True, "bare": False},
            {"label": "Channels", "flag": "--chanlist", "value": str(d.get("chanlist") or ""), "on": True, "bare": False},
        ]
    if "post_steps" not in d:
        ev = bool(d.get("auto_eventtree"))
        d["post_steps"] = [
            {"name": "Hit-tree", "on": bool(d.get("auto_hittree")) or ev,
             "cmd": 'RunAnalysisChain.py --inputBinFile "{bin}" -t "{base}_hittree.root"'},
            {"name": "Event-tree", "on": ev,
             "cmd": 'RunAnalysisChain.py --inputHitTreeFile "{base}_hittree.root" -T "{base}_eventtree.root"'},
        ]
    for k in ("gatelist", "chanlist", "threshold_preset", "trg_channel", "trg_vth",
              "pedestal_mode", "auto_hittree", "auto_eventtree"):
        d.pop(k, None)
    return d


def reload():
    """Defaults merged with hk_config.json, into the shared CONFIG (same object). -> warning or None."""
    cfg = copy.deepcopy(DEFAULTS)
    warning = None
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = legacy_options(json.load(f))
            cfg.update({k: saved[k] for k in saved if k in cfg})
    except Exception as e:
        warning = f"[WARNING] Failed to load hk_config: {e}"
    CONFIG.clear()
    CONFIG.update(cfg)
    return warning


def config():
    if not CONFIG:
        reload()
    return CONFIG


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save(snapshot=False):
    """hk_config.json; snapshot=True also keeps hk_configs/hk_config_<stamp>.json with saved_at."""
    hk = config()
    _write_json(CONFIG_FILE, hk)
    if snapshot:
        now = datetime.now()
        os.makedirs(snapshot_dir(), exist_ok=True)
        _write_json(os.path.join(snapshot_dir(), now.strftime("hk_config_%Y%m%d_%H%M%S.json")),
                    dict(hk, saved_at=now.strftime("%Y-%m-%d %H:%M:%S")))


def snapshots():
    """[(path, saved_at, cfg)] of hk_configs/*.json, newest first."""
    out = []
    for p in sorted(glob.glob(os.path.join(snapshot_dir(), "hk_config_*.json")), reverse=True):
        try:
            with open(p, encoding="utf-8") as f:
                cfg = legacy_options(json.load(f))
            out.append((p, cfg.get("saved_at", os.path.basename(p)), cfg))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- formatters
def option_args(hk):
    """ScanManager option string (leading space per option) from scan_options."""
    out = ""
    for o in hk.get("scan_options", []):
        flag = str(o.get("flag") or "").strip()
        if not flag or not o.get("on"):
            continue
        if o.get("bare"):
            out += f" {flag}"
        else:
            val = str(o.get("value") or "").strip()
            if val:
                out += f" {flag} {val}"
    return out


def option_on(hk, flag):
    return any(str(o.get("flag")).strip() == flag and o.get("on") for o in hk.get("scan_options", []))


def option_value(hk, flag):
    for o in hk.get("scan_options", []):
        if str(o.get("flag")).strip() == flag and o.get("on"):
            return str(o.get("value") or "").strip()
    return ""


def post_cmd(hk, run_id):
    """Loop over the .bin files the acquisition just wrote, chaining the enabled post_steps."""
    steps = [str(p.get("cmd") or "").strip() for p in hk.get("post_steps", []) if p.get("on")]
    steps = [c.replace("{bin}", "$f").replace("{base}", "${f%.bin}") for c in steps if c]
    if not steps:
        return ""
    return ('for f in ' + run_id + '*.bin; do '
            '[ -f "$f" ] || continue; '
            '[ "$(find "$f" -mmin -1)" ] || continue; '
            + " && ".join(steps) + '; done')


def format_remote(hk, run_id, acq_time):
    """<setup_cmd> && cd <work_dir> && <scan_manager> -i <run_id> --l <acq> <options> [&& <post loop>]"""
    work_dir = (hk.get("work_dir") or "").strip()
    if "{date}" in work_dir:
        work_dir = work_dir.replace("{date}", datetime.now().strftime("%Y%m%d"))
    cd_part = f"cd {work_dir} && " if work_dir else ""
    scan_cmd = f"{hk['scan_manager']} -i {run_id} --l {int(round(acq_time))}{option_args(hk)}"
    post = post_cmd(hk, run_id)
    return f"{hk['setup_cmd']} && {cd_part}{scan_cmd}" + (f" && {post}" if post else "")


def format_vmodem(hk):
    """minicom driven by a runscript written fresh from vmodem_keys on every run."""
    device = (hk.get("vmodem_device") or "").strip()
    keys = [k.strip() for k in (hk.get("vmodem_keys") or "").split(",") if k.strip()]
    sends = "\n".join(f'send "{k}"' for k in keys) if keys else 'send ""'
    return (f"cat > ~/vmodem.runscript <<'EOF'\n{sends}\n"
            f"! killall -q minicom || true\nEOF\n"
            f"minicom -S ~/vmodem.runscript -D {device}")


def format_run_id(hk, run_no, tilt, rot, rot3=None):
    """{run} (bare = 3-digit), {tilt}, {rot}/{rot2}, {rot3}, {trgch}, {trgvth}, {mode} in run_id."""
    base = hk.get("run_id", "Run%03d" % run_no)
    tmpl = base.replace("{run}", "{run:03d}")
    rot3_val = rot3 if rot3 is not None else rot
    mode = "ped" if option_on(hk, "--pedestal") else "normal"
    try:
        return tmpl.format(run=int(run_no), tilt=tilt, rot=rot, rot2=rot, rot3=rot3_val,
                           trgch=option_value(hk, "--trgch"), trgvth=option_value(hk, "--trgch-Vth"), mode=mode)
    except Exception:
        return base


def build_dpb_setup(hk):
    cmd = hk.get("dpb_setup_cmd", "") or ""
    try:
        return cmd.format(dpb_host=(hk.get("dpb_host") or "").strip())
    except (KeyError, IndexError):
        return cmd


def current_angles():
    """(tilt, rot) of SN2 from the drives, or (None, None)."""
    try:
        from core.stage import stage
        return stage().read_now(2)
    except Exception:
        return None, None


def build_remote_current(hk, prefix="", suffix="", angles=None):
    """One-off ScanManager command at the current angle; the run counter is not advanced."""
    t2, r2 = angles if angles is not None else current_angles()
    run_id = prefix + format_run_id(hk, hk.get("run_number", 0),
                                    t2 if t2 is not None else 0.0, r2 if r2 is not None else 0.0) + suffix
    return format_remote(hk, run_id, float(hk.get("acq_time", 10.0)))


def manual_acquire(job_name=None):
    """One hand-triggered acquisition at the stages' current angles, as the Tk hk_manual_acquire:
    "MAN_" + run_id, streamed to the Output tab, and the shared run counter advanced.
    -> (slot, run_id) or (None, reason)."""
    hk = config()
    run_no = int(hk.get("run_number", 0))
    try:
        from core.stage import stage
        t2, r2 = stage().read_now(2)
        _t3, r3 = stage().read_now(3)
    except Exception:
        t2 = r2 = r3 = None
    tilt = int(round(t2)) if t2 is not None else 0          # whole degrees, as the scan loop writes them
    rot2 = int(round(r2)) if r2 is not None else 0
    rot3 = int(round(r3)) if r3 is not None else None
    run_id = "MAN_" + format_run_id(hk, run_no, tilt, rot2, rot3)
    remote = format_remote(hk, run_id, float(hk.get("acq_time", 10.0)))
    slot, why = run_in_console(hk, remote, job_name=job_name or f"HK manual run#{run_no}")
    if slot is None:
        return None, why
    hk["run_number"] = run_no + 1
    save()
    return slot, run_id


def resolved_work_dir(hk):
    wd = (hk.get("work_dir") or "").strip()
    return wd.replace("{date}", datetime.now().strftime("%Y%m%d")) if "{date}" in wd else wd


# ---------------------------------------------------------------- SSH
def ssh_argv(hk, remote):
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", hk["ssh_target"],
            f"bash -lc {shlex.quote(remote)}"]


def ssh(hk, remote, wait=False, timeout=None):
    """wait=True -> (rc, output); otherwise fire and forget."""
    cmd = ssh_argv(hk, remote)
    if wait:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return p.returncode, (p.stdout + p.stderr)
        except Exception as e:
            return -1, str(e)
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return None


def dpb_alive_count(hk):
    """Live socat processes on the DPB board (read-only pgrep), or -1 when the check failed."""
    dpb_host = (hk.get("dpb_host") or "").strip()
    if not dpb_host:
        return -1
    rc, out = ssh(hk, f"ssh -o BatchMode=yes -o ConnectTimeout=8 root@{dpb_host} 'pgrep -c -f socat' 2>/dev/null",
                  wait=True, timeout=15)
    try:
        return int(out.strip().splitlines()[-1])
    except Exception:
        return -1


def probe_remote_runs(hk):
    """Highest run number in today's HK data folder (ls only). -> {ok, dir, n_files, runs, next, error}."""
    wd = resolved_work_dir(hk)
    if not wd:
        return {"ok": False, "error": "work_dir is empty", "dir": "", "n_files": 0, "runs": [], "next": None}
    rc, out = ssh(hk, f"ls -1 {wd} 2>/dev/null", wait=True, timeout=20)
    if rc != 0:
        return {"ok": False, "error": (out or "").strip()[:300] or f"ssh exit {rc}",
                "dir": wd, "n_files": 0, "runs": [], "next": None}
    names = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    runs = sorted({int(m.group(1)) for ln in names for m in [re.match(r"^(?:MAN_)?Run0*(\d+)", ln)] if m})
    return {"ok": True, "error": "", "dir": wd, "n_files": len(names), "runs": runs,
            "next": (max(runs) + 1) if runs else 0}


def test_connection(hk):
    """-> (rc, output) of a hostname/ScanManager check on the HK PC."""
    sm = hk.get("scan_manager", "")
    remote = ('echo "HOST=$(hostname) USER=$(whoami)"; '
              f'if [ -f {sm} ]; then echo SCANMGR=OK; else echo SCANMGR=MISSING; fi')
    return ssh(hk, remote, wait=True, timeout=15)


def console_command(hk, remote):
    """The streamed form the Tk app runs in its HK console slot."""
    inner = f"bash -lc {shlex.quote(remote)}"
    return f"ssh -tt -o BatchMode=yes -o ConnectTimeout=8 {hk['ssh_target']} {shlex.quote(inner)}"


def run_in_console(hk, remote, job_name="HK", on_complete=None):
    """Start the SSH command as a background job (Output tab). -> (slot, None) or (None, reason)."""
    from core import jobs
    kind = "hk" if "hk" in jobs.KINDS else "daq"
    return jobs.JOBS.start(kind, job_name, console_command(hk, remote), on_done=on_complete)
