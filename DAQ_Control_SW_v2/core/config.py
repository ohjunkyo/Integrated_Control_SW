"""The DAQ configuration header (config3.h): every `const std::string NAME = "...";` /
`const int NAME = N;` becomes NAME -> str. Parsing and saving follow the Tk app's
ConfigManager (parse_all_variables, save_from_ui, _rebuild_daq_binary), without Tk."""
import os
import re
import subprocess

_PATTERN = re.compile(r'const\s+(std::string|int)\s+([A-Za-z0-9_]+)\s*=\s*(.*?);')


def parse(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return {}
    out = {}
    for m in _PATTERN.finditer(text):
        val = m.group(3).strip()
        if len(val) >= 2 and val[0] == val[-1] == '"':
            val = val[1:-1]
        out[m.group(2)] = val
    return out


def config_path(daq_dir):
    return os.path.join(daq_dir, "config3.h")


# Locked behind the admin password in Global Settings, as in the Tk ConfigWindow
PROTECTED = ["BasePath", "DaqProgramPath", "RawDataPath", "ProcessedDataPath", "FinalResultPath", "ExternalPath",
             "ImagePath", "LogDir", "ChannelMask", "PostTrigger", "Events", "TimeWindow", "NumSequences",
             "IntervalTime"]


def items(path):
    """[(name, value)] in file order."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    out = []
    for m in _PATTERN.finditer(text):
        val = m.group(3).strip()
        if len(val) >= 2 and val[0] == val[-1] == '"':
            val = val[1:-1]
        out.append((m.group(2), val))
    return out


def save(path, values):
    """Rewrite only the lines of the given names; atomic (temp file + os.replace)."""
    with open(path) as f:
        lines = f.readlines()
    out = []
    for line in lines:
        m = re.search(r"const\s+(std::string|int)\s+([A-Za-z0-9_]+)\s*=", line)
        if m and m.group(2) in values:
            name, kind, val = m.group(2), m.group(1), str(values[m.group(2)]).strip()
            line = f'const std::string {name} = "{val}";\n' if kind == "std::string" else f"const int {name} = {val};\n"
        out.append(line)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.writelines(out)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def rebuild_daq(adc_dir):
    """The header is compiled into execute_DAQ_v2, so a save needs `make`. -> None or a warning."""
    try:
        r = subprocess.run(["make"], cwd=adc_dir, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return f"Could not rebuild the DAQ binary automatically: {e}"
    if r.returncode == 0:
        return None
    return ("The DAQ binary (execute_DAQ_v2) failed to rebuild -- it still has the OLD config values "
            "until this is fixed.\n\n" + r.stderr[-500:])
