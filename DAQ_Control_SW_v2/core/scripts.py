"""Shell commands for the Execute Scripts buttons, built exactly as the Tk app builds them
(main.py run_produce / run_analysis / run_contour / run_uniformity / run_stability / run_overlay):
run_cpp_script_v2.sh <macro> <config header> <args...>, one command per run joined with &&."""
import glob
import json
import os
import re
import shlex

from core import env

RUN_RE = re.compile(r"(\d+)(?=[^\d]*\.root$)")   # last number before .root


class ScriptError(Exception):
    pass


def _ctx():
    cfg = env.read_config()
    daq = cfg.get("BasePath") or cfg.get("DaqProgramPath")
    if not daq:
        raise ScriptError("BasePath or DaqProgramPath not found in the config header.")
    return cfg, daq, os.path.join(env.DAQ_DIR, "run_cpp_script_v2.sh"), env.config_file()


def run_number(path):
    m = RUN_RE.search(os.path.basename(path))
    return str(int(m.group(1))) if m else None


def _join(helper, script, conf, runs, extra=()):
    parts = []
    for run, path in runs:
        path_arg = f'\\"{path}\\"' if path else '""'
        parts.append(" ".join([helper, script, conf, run, path_arg, *extra]))
    return " && ".join(parts)


def produce(paths):
    """RAW files only (others are skipped, as in Tk). -> (command, runs, skipped)."""
    _, daq, helper, conf = _ctx()
    runs, skipped = [], []
    for p in paths:
        run = run_number(p) if "raw" in p.lower() else None
        (runs.append((run, p)) if run else skipped.append(os.path.basename(p)))
    if not runs:
        raise ScriptError("No RAW files selected.")
    return _join(helper, os.path.join(daq, "prod_ntp_v7.C"), conf, runs), runs, skipped


def analysis(paths):
    """Production files as they are; RAW files map to their production file name."""
    cfg, daq, helper, conf = _ctx()
    runs, skipped = [], []
    for p in paths:
        name = os.path.basename(p)
        run = run_number(p)
        if not run:
            skipped.append(name)
            continue
        if "production" in p.lower() or "prd" in name.lower():
            prd = p
        else:
            new = name.replace("raw", "prd") if "raw" in name else f"prd_{name}"
            prd = os.path.join(cfg.get("ProcessedDataPath", ""), new)
        runs.append((run, prd))
    if not runs:
        raise ScriptError("No files with a run number selected.")
    return _join(helper, os.path.join(daq, "read_ntp_v7.C"), conf, runs), runs, skipped


def contour(paths, y_lo="3.0", y_hi="3.0", x_start="", x_end=""):
    _, daq, helper, conf = _ctx()
    runs, skipped = [], []
    for p in paths:
        run = run_number(p)
        (runs.append((run, p)) if run else skipped.append(os.path.basename(p)))
    if not runs:
        raise ScriptError("No files with a run number selected.")
    extra = (y_lo.strip() or "3.0", y_hi.strip() or "3.0", x_start.strip() or "-1", x_end.strip() or "-1")
    return _join(helper, os.path.join(daq, "Draw_Contour_v3.C"), conf, runs, extra), runs, skipped


def _macro(daq, name):
    path = os.path.join(daq, name)
    if not os.path.exists(path):
        raise ScriptError(f"Macro not found:\n{path}")
    return path


def uniformity(tag, start, end, channels):
    """Refit the block first (scan-level W, a no-op when done), then Draw_Uniformity_Norm_v7."""
    _, daq, helper, conf = _ctx()
    script = _macro(daq, "Draw_Uniformity_Norm_v7.C")
    chsel = ",".join(str(c) for c in channels)
    cmd = " ".join([helper, script, conf, f'\\"{tag}\\"', str(start), str(end), f'\\"{chsel}\\"'])
    refit = f"(cd {shlex.quote(daq)} && ./analyze.sh refit {shlex.quote(tag)} {start} {end}) ;"
    return f"{refit} {cmd}"


def stability(tag, start, end):
    _, daq, helper, conf = _ctx()
    script = _macro(daq, "Draw_MonitorDrift_Diagnostic.C")
    return " ".join([helper, script, conf, f'\\"{tag}\\"', str(start), str(end)])


def date_tags():
    """Date tags (YYYYMMDD) present in FinalResult, newest first."""
    cfg = env.read_config()
    rp = cfg.get("FinalResultPath", "")
    tags = set()
    if rp and os.path.isdir(rp):
        for fn in os.listdir(rp):
            m = re.search(r"_(\d{8})_", fn)
            if m:
                tags.add(m.group(1))
    return sorted(tags, reverse=True)


def overlay_datasets():
    _, daq, _, _ = _ctx()
    uni = os.path.join(daq, "Data", "UNIFORMITY")
    prefix, suffix = "Graphs_Uniformity_", ".root"
    return [os.path.basename(g)[len(prefix):-len(suffix)]
            for g in sorted(glob.glob(os.path.join(uni, f"{prefix}*{suffix}")))]


def uniformity_dir():
    _, daq, _, _ = _ctx()
    return os.path.join(daq, "Data", "UNIFORMITY")


def _json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def overlay_presets(uni=None):
    """-> (selected tags in file order, labels, ref tag, order numbers, channels)."""
    uni = uni or uniformity_dir()
    selected, labels, ref = [], {}, ""
    try:
        with open(os.path.join(uni, "overlay_tags.txt")) as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                parts = ln.split(",")
                tag = parts[0].strip()
                selected.append(tag)
                if len(parts) > 1 and parts[1].strip():
                    labels[tag] = parts[1].strip()
                if len(parts) > 2 and parts[2].strip().lower() == "ref":
                    ref = tag
    except OSError:
        pass
    saved = _json(os.path.join(uni, "overlay_labels.json"))
    for tag, lab in labels.items():
        saved.setdefault(tag, lab)
    try:
        chans = [int(c) for c in open(os.path.join(uni, "overlay_channels.txt")).read().strip().split(",") if c]
    except (OSError, ValueError):
        chans = [0, 1, 2]
    return selected, saved, ref, _json(os.path.join(uni, "overlay_order.json")), chans


def write_overlay(rows, ref, channels, uni=None):
    """rows: [(tag, selected, label, order)] in draw order. Same files and format as the Tk app."""
    uni = uni or uniformity_dir()
    os.makedirs(uni, exist_ok=True)
    sel = [r for r in rows if r[1]]
    if ref not in {r[0] for r in sel}:
        ref = ""
    with open(os.path.join(uni, "overlay_tags.txt"), "w") as f:
        f.write("# Overlay tag list (edited from the GUI). One 'tag', 'tag,Label' "
                "or 'tag,Label,ref' per line.\n")
        f.write("# Line order = draw/legend order. ',ref' marks the Dev[%] reference.\n")
        for tag, _, label, _ in sel:
            label = label.strip()
            if tag == ref:
                f.write(f"{tag},{label},ref\n")
            else:
                f.write(f"{tag},{label}\n" if label else f"{tag}\n")
    with open(os.path.join(uni, "overlay_channels.txt"), "w") as f:
        f.write(",".join(str(c) for c in channels) + "\n")
    for name, idx in (("overlay_labels.json", 2), ("overlay_order.json", 3)):
        path = os.path.join(uni, name)
        saved, before = _json(path), set(_json(path))
        for r in rows:
            val = r[idx].strip()
            if val:
                saved[r[0]] = val
            elif r[0] in before:   # cleared on purpose; untouched blanks keep older values
                saved.pop(r[0], None)
        with open(path, "w") as f:
            json.dump(saved, f, indent=2)


def overlay_command():
    _, daq, helper, conf = _ctx()
    return " ".join([helper, _macro(daq, "Draw_Overlay_Uniformity_v7.C"), conf])


BLOCKS = {("manual", "dark"): "700", ("manual", "laser"): "800"}   # General Scan category: block 0


def daq_running():
    """A real execute_DAQ acquisition (the -j connection probe does not count)."""
    import subprocess
    try:
        out = subprocess.run(["pgrep", "-af", "execute_DAQ"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(" -j" not in line and "pgrep" not in line for line in out.splitlines() if line.strip())


def daq(category, manual_type, file_format, angles):
    """Manual Run DAQ, as main.run_daq does for a click outside a General Scan.
    category 'auto'/'manual', manual_type 'laser'/'dark', angles {2: (tilt, rot), 3: (tilt, rot)}.
    -> (command, env)."""
    _, daq_dir, _, conf = _ctx()
    mode = manual_type if category == "manual" else "laser"
    block = BLOCKS.get((category, mode), "0")
    ints = []
    for dev in (2, 3):
        tilt, rot = angles.get(dev, (None, None))
        ints += [int(round(rot)) if rot is not None else 0, int(round(tilt)) if tilt is not None else 0]
    script = _macro(daq_dir, "script_v7.sh")
    command = " ".join([shlex.quote(script), mode, shlex.quote(conf), *map(str, ints), block])
    from core.daq_diag import probe_env
    run_env = probe_env(daq_dir)   # 64-bit CAEN libs first, as the Tk app gets from its login shell
    run_env["FILE_FORMAT"] = file_format
    run_env["SCAN_START_DATE"] = ""
    return command, run_env
