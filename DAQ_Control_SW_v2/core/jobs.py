"""Background jobs (Produce, Analysis, Contour, Uniformity, Stability, Overlay), same model as
the Tk app's _run_job_in_console: each job runs `bash -c` in its own process group inside a
named slot, one job per slot, up to three parallel slots per kind; output is streamed raw
(\\r kept for progress lines). Qt-free: listeners are called from worker threads."""
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime

KINDS = {   # kind -> parallel slots (Overlay shares one config file, so a single slot)
    "produce": ("produce_1", "produce_2", "produce_3"),
    "analysis": ("analysis_1", "analysis_2", "analysis_3"),
    "contour": ("contour_1", "contour_2", "contour_3"),
    "uniformity": ("uniformity_1", "uniformity_2", "uniformity_3"),   # Stability shares these, as in Tk
    "overlay": ("overlay",),
    "daq": ("daq",),          # one acquisition at a time
    "backup": ("backup",),
    "general_scan": ("general_scan",),
    "hk": ("hk",),
    "stability": ("stability",),
}
LABELS = {s: s.replace("_", " ").title() for slots in KINDS.values() for s in slots}
LABELS["daq"] = "DAQ Stream"
LABELS["backup"] = "Backup"
LABELS.update({"general_scan": "General Scan", "hk": "HK Digitizer", "stability": "Stability Run"})
STATES = ("idle", "running", "done", "failed", "stopped")
MAX_KEEP = 400_000   # characters of output kept per job
EVT_LINE = re.compile(r"^\[Evt:\s*(\d+)\]\s*(.*)$")   # execute_DAQ_v2 progress, shown as a bar instead of text
SEQ_LINE = re.compile(r"Sequence (\d+)/(\d+)")
PROGRESS_EVERY_S = 0.3
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")   # colour codes; the console shows plain text


class Job:
    def __init__(self, slot, name, command):
        self.slot, self.name, self.command = slot, name, command
        self.state = "running"
        self.code = None
        self.started = datetime.now()
        self.proc = None
        self.output = []          # chunks, for a pane opened after the job started
        self.progress = None      # Progress once the job prints [Evt: N] lines
        self._size = 0

    def add(self, text):
        self.output.append(text)
        self._size += len(text)
        while self._size > MAX_KEEP and len(self.output) > 1:
            self._size -= len(self.output.pop(0))


class Progress:
    """Acquisition progress from execute_DAQ_v2's [Evt: N] Pedestal lines."""

    def __init__(self, total):
        self.total = total        # events per sequence (config Events), or None
        self.evt = 0
        self.seq = None           # (i, n) from "Sequence i/n"
        self.ped = ""
        self.rate = None          # events / s in the current sequence
        self._t0, self._e0 = None, 0

    def update(self, evt, rest):
        now = time.time()
        if self._t0 is None or evt < self.evt:   # first line, or a new sequence / scan point
            self._t0, self._e0, self.rate = now, evt, None
        elif now - self._t0 > 0.5:
            self.rate = (evt - self._e0) / (now - self._t0)
        self.evt = evt
        ped = re.sub(r"^Pedestal\s*\S+\s*", "", rest).strip().strip("|").strip()
        if ped:
            self.ped = ped

    def remaining_s(self):
        if not self.rate or not self.total:
            return None
        left = max(0, self.total - self.evt)
        if self.seq:
            left += (self.seq[1] - self.seq[0]) * self.total
        return left / self.rate


def _total_events():
    try:
        from core import env
        return int(float(env.read_config().get("Events", "")))
    except (ImportError, ValueError, OSError):
        return None


class JobManager:
    def __init__(self):
        self.jobs = {}            # slot -> latest Job
        self.listeners = []       # fn(event, slot, payload); event in {"state", "output", "progress"}
        self._lock = threading.Lock()

    def _emit(self, event, slot, payload):
        if event == "output" and slot in self.jobs:
            self.jobs[slot].add(payload)
        for fn in list(self.listeners):
            try:
                fn(event, slot, payload)
            except Exception:
                pass

    def running(self, slot):
        job = self.jobs.get(slot)
        return job is not None and job.state == "running"

    def free_slot(self, kind):
        for slot in KINDS[kind]:
            if not self.running(slot):
                return slot
        return None

    def any_running(self):
        return any(j.state == "running" for j in self.jobs.values())

    def start(self, kind, name, command, env=None, on_done=None):
        """command: one shell string (run with bash -c). -> (slot, None) or (None, reason)."""
        with self._lock:
            slot = self.free_slot(kind)
            if slot is None:
                return None, f"{len(KINDS[kind])} {name} jobs are already running. Wait for one to finish."
        return self.start_in(slot, name, command, env=env, on_done=on_done)

    def start_in(self, slot, name, command, env=None, on_done=None):
        """Run in this exact slot (the scan engine names its own slots). -> (slot, None) or (None, reason)."""
        with self._lock:
            if self.running(slot):
                return None, f"A job is already running in {LABELS.get(slot, slot)}."
            job = Job(slot, name, command)
            self.jobs[slot] = job
        self._emit("state", slot, job)
        self._emit("output", slot, f"\n===== {name} started @ {job.started:%H:%M:%S} =====\n")
        try:
            job.proc = subprocess.Popen(["bash", "-c", command], env=env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        except Exception as e:
            job.state, job.code = "failed", -1
            self._emit("output", slot, f"[ERROR] Failed to start job: {e}\n")
            self._emit("state", slot, job)
            return slot, None
        threading.Thread(target=self._read, args=(job, on_done), daemon=True).start()
        return slot, None

    def _read(self, job, on_done):
        pending, last_progress = "", 0.0
        try:
            while True:
                chunk = job.proc.stdout.read1(32 * 1024)
                if not chunk:
                    break
                text = pending + ANSI.sub("", chunk.decode("utf-8", errors="replace"))
                cut = text.rfind("\n") + 1
                body, pending = text[:cut], text[cut:]
                if "\r" in pending:   # progress line without newline: show it now
                    body, pending = body + pending, ""
                keep, moved = [], False
                for line in body.splitlines(keepends=True):
                    m = EVT_LINE.match(line)
                    if m:
                        if job.progress is None:
                            job.progress = Progress(_total_events())
                        job.progress.update(int(m.group(1)), m.group(2))
                        moved = True
                        continue
                    s = SEQ_LINE.search(line)
                    if s:
                        if job.progress is None:
                            job.progress = Progress(_total_events())
                        job.progress.seq = (int(s.group(1)), int(s.group(2)))
                    keep.append(line)
                if keep:
                    self._emit("output", job.slot, "".join(keep))
                if moved and time.time() - last_progress >= PROGRESS_EVERY_S:
                    last_progress = time.time()
                    self._emit("progress", job.slot, job.progress)
        except Exception:
            pass
        code = job.proc.wait()
        if job.progress is not None:
            self._emit("progress", job.slot, job.progress)
        if pending:
            self._emit("output", job.slot, pending)
        job.code = code
        if job.state != "stopped":
            job.state = "done" if code == 0 else "failed"
        tail = "finished (exit 0)" if code == 0 else f"FAILED (exit {code})"
        self._emit("output", job.slot, f"===== {job.name} {tail} =====\n\n")
        self._emit("state", job.slot, job)
        if on_done:
            on_done(code)

    def stop(self, slot):
        job = self.jobs.get(slot)
        if job is None or job.state != "running" or job.proc is None:
            return False
        try:
            os.killpg(os.getpgid(job.proc.pid), signal.SIGTERM)
        except OSError:
            return False
        job.state = "stopped"
        self._emit("output", slot, "\n[STOP] Sent SIGTERM to running job.\n")
        self._emit("state", slot, job)
        return True


JOBS = JobManager()
