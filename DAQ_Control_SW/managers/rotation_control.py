# managers/rotation_control.py
import os
import json
import time
import struct
import threading
from tkinter import messagebox
from pymodbus.client.sync import ModbusTcpClient


class RotationManager:
    """Native Modbus TCP control for the two rotation/tilt stages (dev 2, 3).

    Motion-lock contract
    --------------------
    `is_moving[dev]` means "a move has been dispatched to this drive and has
    not been confirmed complete yet". While it is set, move_* refuses new
    commands for that device. It is cleared in exactly two ways:

      * monitor_loop, when read_angles POSITIVELY confirms the target angle
        (a failed/None read never counts as arrival -- see monitor_loop), or
      * _release_motion, for every path where the command did NOT get
        dispatched (refused, interlocked, connect failed, Modbus raised) and
        for an explicit stop.

    Every acquire therefore has a guaranteed matching release: _send_move
    releases in a `finally` unless the command actually reached the drive.
    This used to be open-coded at each call site, and the two `except Exception`
    paths were missing it -- on 2026-08-28 21:34 Device 2 kept a stale lock
    after one failed move, so every later command was answered with "already
    moving! Command ignored." and all 14 remaining scan points failed the same
    way 32s apart. Only the first was a real hardware event; the rest were the
    leaked lock. Keep the acquire/release pairing structural, not per-call-site.
    """

    # Per-axis Modbus register/coil map, so tilt and rotation share one code
    # path instead of two near-identical 40-line copies that have to be kept
    # in sync by hand.
    _AXES = {
        "tilt": {"write": "write_tilt", "move": "move_tilt", "stop": "stop_tilt",
                 "range": "tilt", "label": "TILT ONLY"},
        "rot":  {"write": "write_rot",  "move": "move_rot",  "stop": "stop_rot",
                 "range": "rotation", "label": "ROTATION ONLY"},
    }

    def __init__(self, controller):
        self.controller = controller
        self.controller._log("Native Modbus Manager initialized. Direct hardware control active.")

        self.devices = {
            2: {"host": "192.168.10.211", "port": 502, "unit": 1},
            3: {"host": "192.168.10.212", "port": 502, "unit": 1}
        }

        self.addr = {
            "write_tilt": 104, "write_rot": 4,
            "move_tilt": 511,  "move_rot": 501,
            "stop_tilt": 810,  "stop_rot": 800,
            "read_tilt": 432,  "read_rot": 422
        }

        self.is_monitoring = False

        # Persistent Modbus TCP connections, one per device, reused across
        # calls instead of connect()+close() on every single command/poll.
        # The old per-call reconnect pattern hammered the stage's embedded
        # Modbus server with a fresh TCP handshake every 0.5-1s from the
        # background monitor thread alone — the suspected root cause of the
        # comm timeouts that hung General Scan and Reset Angle (2026-07-12/13).
        self._clients = {2: None, 3: None}

        self.is_moving = {2: False, 3: False}
        self.target_angles = {
            2: {"tilt": None, "rot": None},
            3: {"tilt": None, "rot": None}
        }

        # Serializes a whole command SEQUENCE (stop pulse -> write target ->
        # move pulse) against the monitor thread's polling on the same socket.
        # pymodbus already locks each individual transaction, so frames never
        # interleave -- but nothing stopped the monitor thread's read failure
        # handler from calling _invalidate_client() and closing the socket
        # between this sequence's steps, which would leave the drive stopped
        # (stop pulse delivered) but never commanded (move pulse lost), with
        # no error on the scan side. RLock: same thread may re-enter.
        self._dev_lock = {2: threading.RLock(), 3: threading.RLock()}

        # Guards is_moving / target_angles, which are touched from the scan
        # thread, the monitor thread and the Tk thread.
        self._state_lock = threading.RLock()

        # Diagnostics for "did the drive stop answering, or did the motor not
        # physically get there?" -- see describe_motion_state(). Before this,
        # read failures were swallowed silently and the two were
        # indistinguishable after the fact.
        self._read_fail_streak = {2: 0, 3: 0}
        self._last_read = {2: None, 3: None}      # (tilt, rot, monotonic_ts)
        self._last_read_error = {2: None, 3: None}
        # When the angle last actually CHANGED. Lets us tell "the motor is
        # still travelling, just slower than the timeout allowed" from "the
        # motor is frozen short of target" -- two failures that look identical
        # from the motion lock alone, but need opposite responses (wait vs
        # re-command; re-commanding a moving stage pulses STOP and cuts the
        # motion it was about to complete).
        self._last_motion_ts = {2: 0.0, 3: 0.0}

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------
    # Modbus transport tuning, in one place instead of three copies of
    # `ModbusTcpClient(host=..., port=502, timeout=3)`.
    #
    # timeout: the stages sit on the local switch at ~0.3ms RTT, so 1.0s is
    #   still ~3000x the observed round trip. The old 3s only delayed the
    #   detection of a dead link; it never made a healthy read succeed.
    # retries / retry_on_empty: pymodbus defaults to RetryOnEmpty=False, which
    #   means a single DROPPED response returns failure immediately with no
    #   retry at all (transaction.py: "No response received and retries not
    #   enabled" -> break). Nothing in this file retried either, so one lost
    #   frame was a hard failure -- the most likely shape of the 2026-08-28
    #   21:34 Device 2 event, which happened once in 232 otherwise-clean moves
    #   that day. Turning retries on lets the library ride out a single drop.
    MODBUS_PORT = 502
    MODBUS_TIMEOUT_S = 1.0
    MODBUS_RETRIES = 3

    def _make_client(self, host):
        return ModbusTcpClient(
            host=host,
            port=self.MODBUS_PORT,
            timeout=self.MODBUS_TIMEOUT_S,
            retries=self.MODBUS_RETRIES,
            retry_on_empty=True,
        )

    def _get_config_and_client(self, dev_num):
        cfg_file = f"config_dev{dev_num}.json"
        cfg_path = os.path.join(self.controller.base_dir, cfg_file)

        if not os.path.exists(cfg_path):
            if dev_num in self.devices:
                cfg = {"connection": {"host": self.devices[dev_num]["host"], "unit": self.devices[dev_num]["unit"]}}
                client = self._make_client(cfg["connection"]["host"])
                return client, cfg
            else:
                self.controller._log(f"ERROR: No default settings for Dev {dev_num}.")
                return None, None

        try:
            with open(cfg_path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            client = self._make_client(cfg["connection"]["host"])
            return client, cfg
        except Exception as e:
            self.controller._log(f"ERROR: Failed to load {cfg_file}: {e}")
            return None, None

    def _get_persistent_client(self, dev_num):
        """Like _get_config_and_client, but reuses one long-lived TCP
        connection per device instead of reconnecting on every call. Returns
        (client, cfg), or (None, cfg-or-None) on failure."""
        cfg_file = f"config_dev{dev_num}.json"
        cfg_path = os.path.join(self.controller.base_dir, cfg_file)
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
            except Exception as e:
                self.controller._log(f"ERROR: Failed to load {cfg_file}: {e}")
                return None, None
        elif dev_num in self.devices:
            cfg = {"connection": {"host": self.devices[dev_num]["host"], "unit": self.devices[dev_num]["unit"]}}
        else:
            self.controller._log(f"ERROR: No default settings for Dev {dev_num}.")
            return None, None

        client = self._clients.get(dev_num)
        if client is not None and client.is_socket_open():
            return client, cfg

        if client is not None:
            try:
                client.close()
            except Exception:
                pass

        client = self._make_client(cfg["connection"]["host"])
        if not client.connect():
            self._clients[dev_num] = None
            return None, cfg
        self._clients[dev_num] = client
        return client, cfg

    def _invalidate_client(self, dev_num):
        """Drop the cached connection for dev_num after a Modbus error, so
        the next call opens a fresh socket instead of reusing a possibly-bad
        one."""
        client = self._clients.get(dev_num)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        self._clients[dev_num] = None

    # ------------------------------------------------------------------
    # Encoding helpers
    # ------------------------------------------------------------------
    def _pack_32bit(self, value):
        scaled_val = int(round(float(value) * 250.0))
        b = struct.pack(">i", scaled_val)
        hi, lo = (b[0] << 8) | b[1], (b[2] << 8) | b[3]
        return [lo, hi]

    def _unpack_32bit_read(self, regs):
        """Restore 32-bit signed int from two 16-bit registers (Little-endian words)."""
        lo, hi = regs[0], regs[1]
        b = bytes([(hi >> 8) & 0xFF, hi & 0xFF, (lo >> 8) & 0xFF, lo & 0xFF])
        return struct.unpack(">i", b)[0]

    def _pulse_trigger(self, client, unit, address):
        time.sleep(0.5)
        client.write_coil(address, True, unit=unit)
        time.sleep(0.5)
        client.write_coil(address, False, unit=unit)

    def _fast_pulse_trigger(self, client, unit, address):
        """A faster pulse trigger specifically for STOP commands to avoid mechanical delay."""
        client.write_coil(address, True, unit=unit)
        time.sleep(0.1)  # Minimal delay just to register the pulse
        client.write_coil(address, False, unit=unit)

    # ------------------------------------------------------------------
    # Motion lock
    # ------------------------------------------------------------------
    def _acquire_motion(self, dev_num, axis, value):
        """Claim the motion lock for dev_num. False if it is already held."""
        with self._state_lock:
            if self.is_moving.get(dev_num, False):
                self.controller._log(f"WARNING: Device {dev_num} is already moving! Command ignored.")
                return False
            self.is_moving[dev_num] = True
            self.target_angles[dev_num] = {
                "tilt": value if axis == "tilt" else None,
                "rot":  value if axis == "rot" else None,
            }
            return True

    def _release_motion(self, dev_num, reason=None):
        """Release the motion lock. Idempotent, so it is safe to call from a
        `finally` even when an earlier branch already released."""
        with self._state_lock:
            was_held = self.is_moving.get(dev_num, False)
            self.is_moving[dev_num] = False
            self.target_angles[dev_num] = {"tilt": None, "rot": None}
        if was_held and reason:
            self.controller._log(f"[INFO] Device {dev_num} motion lock released ({reason}).")
        return was_held

    # Single source of truth for "close enough to count as arrived". Used by
    # monitor_loop here and by rotation_manager's physical-angle wait, which
    # used to hard-code its own copy of 0.5 -- two numbers that had to be kept
    # equal by hand, and whose disagreement would mean the scan thread and the
    # lock owner never agree on whether a move finished.
    ARRIVAL_TOLERANCE_DEG = 0.5

    # wait_until_stopped outcomes
    WAIT_ARRIVED = "arrived"
    WAIT_TIMEOUT = "timeout"
    WAIT_CANCELLED = "cancelled"

    def wait_until_stopped(self, dev_num, timeout, should_continue=None,
                           poll=0.5, release_on_timeout=True):
        """Block until dev_num's motion lock clears. THE place to wait for a
        stage to finish moving.

        This used to be open-coded in three callers with three different
        policies: rotation_manager._wait_for_motors (adaptive timeout, skips
        the scan point), rotation_manager._wait_for_physical_angle (60s cap,
        aborts the scan) and ui_automation._wait_for_stop (no timeout at all,
        so a leaked lock spun that thread forever and the UI never re-synced).
        Centralising it means a timeout always (a) says WHY via
        describe_motion_state and (b) drops the stale lock, so one failed move
        can no longer poison every later command to that device.

        `should_continue` is polled each cycle; returning False aborts the
        wait with WAIT_CANCELLED (used for Stop / Reset-cancel).
        """
        deadline = time.monotonic() + timeout
        while True:
            if should_continue is not None and not should_continue():
                return self.WAIT_CANCELLED
            if not self.is_moving.get(dev_num, False):
                return self.WAIT_ARRIVED
            if time.monotonic() >= deadline:
                self.controller._log(
                    f"[CRITICAL] Device {dev_num} never confirmed arrival within "
                    f"{timeout:.0f}s. {self.describe_motion_state(dev_num)}")
                if release_on_timeout:
                    # Without this the lock stays set forever and move_* answers
                    # every later command with "already moving! Command ignored."
                    self._release_motion(dev_num, "arrival timeout")
                return self.WAIT_TIMEOUT
            time.sleep(poll)

    # Encoder resolution is 1/250 deg (0.004), so anything above this is real
    # movement rather than readback jitter.
    MOTION_NOISE_DEG = 0.05
    # How long the angle must sit unchanged before the stage counts as stopped
    # rather than "still travelling". Generous: a slow rot-fold move still
    # advances well within this.
    MOTION_STILL_AFTER_S = 3.0

    # classify_motion outcomes
    MOTION_CREEPING = "creeping"        # answering, angle still changing
    MOTION_STALLED = "stalled"          # answering, angle frozen short of target
    MOTION_UNREACHABLE = "unreachable"  # not answering
    MOTION_UNKNOWN = "unknown"          # no evidence either way

    def classify_motion(self, dev_num):
        """Why is this stage not confirming arrival? Measured, never assumed.

        The whole point of the 2026-08-28 post-mortem: the old code printed
        "Modbus comm failure suspected" for every non-arrival, and that guess
        was wrong -- reads were healthy and the motor simply had not moved.
        Callers use this to pick a response instead of applying one fixed
        retry policy to failures that need opposite handling."""
        if self._read_fail_streak.get(dev_num, 0) > 0:
            return self.MOTION_UNREACHABLE
        if self._last_read.get(dev_num) is None:
            return self.MOTION_UNKNOWN
        idle = time.monotonic() - self._last_motion_ts.get(dev_num, 0.0)
        return self.MOTION_STALLED if idle >= self.MOTION_STILL_AFTER_S else self.MOTION_CREEPING

    def is_stage_settled(self, dev_num, still_for_s=None):
        """True when the angle has been unchanged for `still_for_s` AND the
        drive is answering -- i.e. safe to start an acquisition against."""
        if self._read_fail_streak.get(dev_num, 0) > 0:
            return False
        if self._last_read.get(dev_num) is None:
            return False
        still_for_s = self.MOTION_STILL_AFTER_S if still_for_s is None else still_for_s
        return (time.monotonic() - self._last_motion_ts.get(dev_num, 0.0)) >= still_for_s

    def describe_motion_state(self, dev_num):
        """One-line diagnostic for a move that never confirmed arrival.

        Distinguishes the two failure modes that used to look identical in the
        log: the drive stopped answering (stale/failed reads) versus the motor
        answering fine but sitting short of target (mechanical stall, or an
        offset just outside the 0.5 deg arrival tolerance -- which would
        otherwise never 'arrive' and hold the lock forever)."""
        with self._state_lock:
            target = dict(self.target_angles.get(dev_num) or {})
        last = self._last_read.get(dev_num)
        err = self._last_read_error.get(dev_num)
        fails = self._read_fail_streak.get(dev_num, 0)

        if last is None and fails > 0:
            return (f"Device {dev_num}: no successful angle read on record and "
                    f"{fails} consecutive read failures (last error: {err}) "
                    f"-- the drive is not answering.")
        if last is None:
            # Say what is missing, do NOT name a cause. Asserting one without
            # evidence is exactly how the old "Modbus comm failure suspected"
            # message sent the 2026-08-28 investigation after a network fault
            # that had not happened: reads were healthy the whole time.
            return (f"Device {dev_num}: no angle readback recorded yet, and no read "
                    f"errors either -- cause undetermined from this process.")

        tilt, rot, ts = last
        age = time.monotonic() - ts
        want = target.get("tilt") if target.get("tilt") is not None else target.get("rot")
        axis = "tilt" if target.get("tilt") is not None else "rot"
        have = tilt if axis == "tilt" else rot

        if fails > 0:
            return (f"Device {dev_num}: last good read {age:.1f}s ago "
                    f"(tilt={tilt}, rot={rot}), then {fails} consecutive read "
                    f"failures (last error: {err}) -- the drive stopped answering.")
        if want is not None and have is not None:
            return (f"Device {dev_num}: reads are healthy (last {age:.1f}s ago) but "
                    f"{axis}={have:.2f} deg vs target {want:.2f} deg "
                    f"(off by {abs(have - want):.2f} deg, tolerance 0.5) "
                    f"-- the drive answers, the motor did not reach target.")
        return (f"Device {dev_num}: last read {age:.1f}s ago (tilt={tilt}, rot={rot}), "
                f"target={target}.")

    # ------------------------------------------------------------------
    # Motion commands
    # ------------------------------------------------------------------
    def _angle_in_range(self, axis_label, value, skip_lock):
        """Reject a move outside the axis' safe range.

        TILT is bounded by auto_mgr.scan_range [-55, 55]; ROTATION by
        auto_mgr.rot_range [0, 135] -- they are DIFFERENT mechanical limits.
        Applying the tilt range to rotation used to silently reject any
        rotation > 55deg: the motor never started, and the stall watchdog
        then raised a false 'MECHANICAL BLOCKAGE' abort (seen 2026-07-24 on
        Device 3 during a Repeat-Angles recheck at rot offset 45deg)."""
        if axis_label == "rotation":
            rng = self.controller.auto_mgr.rot_range
        else:
            rng = self.controller.auto_mgr.scan_range
        if rng["start"] <= value <= rng["end"]:
            return True
        msg = (f"Requested {axis_label} {value}° is outside the allowed range "
               f"[{rng['start']}°, {rng['end']}°].")
        self.controller._log(f"ERROR: {msg}")
        if not skip_lock:
            messagebox.showerror("Angle Out of Range", msg)
        return False

    def _send_move(self, dev_num, axis, value, skip_lock=False):
        """Single-axis move. Shared by move_tilt_only / move_rot_only.

        Returns True only when the command actually reached the drive; in that
        case the motion lock stays held and monitor_loop clears it on arrival.
        Every other exit releases the lock through the `finally` below."""
        spec = self._AXES[axis]

        if not skip_lock and not self.controller.access_mgr.unlocked:
            return False
        if not self._angle_in_range(spec["range"], value, skip_lock):
            return False
        if not self._acquire_motion(dev_num, axis, value):
            return False

        dispatched = False
        try:
            # Rotating with the stage tilted would crash the arm into the
            # frame. Checked after the lock is claimed so two callers can't
            # race past the interlock together.
            if axis == "rot":
                current_tilt, _ = self.read_angles(dev_num)
                if current_tilt is not None and abs(current_tilt) > 0.5:
                    self.controller._log(
                        f"ERROR: SAFETY INTERLOCK! Cannot rotate. "
                        f"Tilt is {current_tilt:.1f} deg. Must be 0.0 deg.")
                    # Release before the modal dialog: showerror blocks this
                    # thread until the operator clicks OK, and the device must
                    # not sit in a phantom "moving" state for that whole time.
                    self._release_motion(dev_num)
                    if not skip_lock:
                        messagebox.showerror(
                            "Safety Interlock",
                            f"Cannot rotate Device {dev_num}!\n\n"
                            f"Tilt must be 0.0° before rotating.\n"
                            f"Current tilt is {current_tilt:.1f}°.")
                    return False

            client, cfg = self._get_persistent_client(dev_num)
            if not client:
                self.controller._log(f"ERROR: Modbus connect failed (Dev {dev_num}). Lock released.")
                return False

            # Hold the device lock across the whole stop -> write -> move
            # sequence so a concurrent read failure cannot close the socket
            # halfway through and leave the drive stopped but uncommanded.
            with self._dev_lock[dev_num]:
                unit = cfg["connection"]["unit"]
                self._pulse_trigger(client, unit, self.addr[spec["stop"]])
                client.write_registers(self.addr[spec["write"]], self._pack_32bit(value), unit=unit)
                self._pulse_trigger(client, unit, self.addr[spec["move"]])

            self.controller._log(f"Device {dev_num} {spec['label']} command sent: {value} deg")
            dispatched = True
            return True

        except Exception as e:
            self.controller._log(f"ERROR: Modbus {axis.capitalize()} Error (Dev {dev_num}): {e}")
            self._invalidate_client(dev_num)
            return False
        finally:
            if not dispatched:
                self._release_motion(dev_num)

    def move_tilt_only(self, dev_num, tilt, skip_lock=False):
        return self._send_move(dev_num, "tilt", tilt, skip_lock=skip_lock)

    def move_rot_only(self, dev_num, rot, skip_lock=False):
        return self._send_move(dev_num, "rot", rot, skip_lock=skip_lock)

    def move_rotation(self, dev_num, tilt, rot):
        """Combined tilt+rotation sequence (manual panel path).

        Does not take the motion lock -- it is the one caller that drives both
        axes in a single sequence, and 'x' means "leave this axis alone"."""
        if not self.controller.access_mgr.unlocked:
            return
        client, cfg = self._get_persistent_client(dev_num)
        if not client:
            return

        try:
            with self._dev_lock[dev_num]:
                unit = cfg["connection"]["unit"]
                if tilt != 'x':
                    self._pulse_trigger(client, unit, self.addr["stop_tilt"])
                if rot != 'x':
                    self._pulse_trigger(client, unit, self.addr["stop_rot"])

                if tilt != 'x':
                    client.write_registers(self.addr["write_tilt"], self._pack_32bit(tilt), unit=unit)
                    self._pulse_trigger(client, unit, self.addr["move_tilt"])
                    self.controller._log(f"Device {dev_num} Tilt command sent: {tilt} deg")

                if rot != 'x':
                    client.write_registers(self.addr["write_rot"], self._pack_32bit(rot), unit=unit)
                    self._pulse_trigger(client, unit, self.addr["move_rot"])
                    self.controller._log(f"Device {dev_num} Rotation command sent: {rot} deg")
        except Exception as e:
            self.controller._log(f"ERROR: Modbus Rotation Error (Dev {dev_num}): {e}")
            self._invalidate_client(dev_num)

    def stop_rotation(self, dev_num):
        """Send hardware stop signals to the motors immediately."""
        self._release_motion(dev_num)
        self.controller._log(f"[INFO] Device {dev_num} Lock released due to STOP command.")

        client, cfg = self._get_persistent_client(dev_num)
        if not client:
            return

        try:
            with self._dev_lock[dev_num]:
                unit = cfg["connection"]["unit"]
                # Use the fast pulse trigger for immediate response
                self._fast_pulse_trigger(client, unit, self.addr["stop_tilt"])
                self._fast_pulse_trigger(client, unit, self.addr["stop_rot"])
            self.controller._log(f"[INFO] Device {dev_num} Hardware STOP command sent rapidly.")
        except Exception as e:
            self.controller._log(f"[ERROR] Modbus Stop Error (Dev {dev_num}): {e}")
            self._invalidate_client(dev_num)

    # ------------------------------------------------------------------
    # Readback
    # ------------------------------------------------------------------
    # Rate-limited so background monitoring (2 devices, every 0.5-1.0s) can't
    # flood the log, while still leaving a trace. Without this, read failures
    # were silent: the 2026-08-28 21:34 Device 2 stall logged ZERO Modbus
    # errors, which made "the drive stopped answering" and "the motor didn't
    # physically move" indistinguishable after the fact.
    _READ_FAIL_LOG_EVERY = 20

    def _note_read_failure(self, dev_num, detail):
        n = self._read_fail_streak.get(dev_num, 0) + 1
        self._read_fail_streak[dev_num] = n
        self._last_read_error[dev_num] = detail
        if n == 1 or n % self._READ_FAIL_LOG_EVERY == 0:
            self.controller._log(
                f"[WARNING] Device {dev_num} angle read failed "
                f"(consecutive: {n}) -- {detail}")

    def read_angles(self, dev_num):
        """Read actual Tilt and Rotation angles from hardware in a single Modbus request."""
        client, cfg = self._get_persistent_client(dev_num)
        if not client:
            self._note_read_failure(dev_num, "no Modbus connection")
            return None, None

        tilt_deg, rot_deg = None, None
        try:
            with self._dev_lock[dev_num]:
                unit = cfg["connection"]["unit"]

                # Read 12 registers starting from read_rot (422) to cover up to read_tilt (433)
                # registers[0:2] = Rotation (422, 423)
                # registers[10:12] = Tilt (432, 433)
                res = client.read_holding_registers(self.addr["read_rot"], 12, unit=unit)

            if not res.isError():
                rot_raw = self._unpack_32bit_read(res.registers[0:2])
                rot_deg = rot_raw / 250.0

                tilt_raw = self._unpack_32bit_read(res.registers[10:12])
                tilt_deg = tilt_raw / 250.0

                self._read_fail_streak[dev_num] = 0
                self._last_read_error[dev_num] = None
                now = time.monotonic()
                prev = self._last_read.get(dev_num)
                if (prev is None
                        or abs(tilt_deg - prev[0]) > self.MOTION_NOISE_DEG
                        or abs(rot_deg - prev[1]) > self.MOTION_NOISE_DEG):
                    self._last_motion_ts[dev_num] = now
                self._last_read[dev_num] = (tilt_deg, rot_deg, now)
            else:
                self._note_read_failure(dev_num, f"Modbus exception response: {res}")

        except Exception as e:
            self._invalidate_client(dev_num)
            self._note_read_failure(dev_num, repr(e))

        return tilt_deg, rot_deg

    # ------------------------------------------------------------------
    # Background monitoring
    # ------------------------------------------------------------------
    def start_monitoring(self, update_callback):
        """Starts a background thread to update the UI with current angles."""
        if self.is_monitoring:
            return
        self.is_monitoring = True

        def monitor_loop():
            self.controller._log("Started background hardware monitoring thread.")
            while self.is_monitoring:
                currently_moving = any(self.is_moving.values())
                for dev_num in [2, 3]:
                    tilt, rot = self.read_angles(dev_num)

                    if update_callback:
                        update_callback(dev_num, tilt, rot)

                    with self._state_lock:
                        if not self.is_moving.get(dev_num, False):
                            continue
                        target = self.target_angles[dev_num]
                        # An axis with a target counts as "reached" ONLY when we
                        # positively confirm it is within tolerance. If the angle
                        # read failed (None) while a move is in progress, do NOT
                        # assume it arrived — otherwise a transient Modbus read
                        # failure would release the lock early, letting the General
                        # Scan advance / take DAQ data while the motor is still
                        # physically moving (wrong-angle data, or a new command
                        # interrupting the move). An axis with no target (None) has
                        # nothing to wait for, so it starts as already reached.
                        reached_tilt = (target["tilt"] is None)
                        reached_rot = (target["rot"] is None)

                        tol = self.ARRIVAL_TOLERANCE_DEG
                        if target["tilt"] is not None and tilt is not None:
                            reached_tilt = abs(tilt - target["tilt"]) <= tol
                        if target["rot"] is not None and rot is not None:
                            reached_rot = abs(rot - target["rot"]) <= tol

                        arrived = reached_tilt and reached_rot
                        if arrived:
                            self.is_moving[dev_num] = False
                            self.target_angles[dev_num] = {"tilt": None, "rot": None}

                    if arrived:
                        self.controller._log(f"Device {dev_num} reached target. Lock automatically released.")

                sleep_time = 0.5 if currently_moving else 1.0
                time.sleep(sleep_time)

        threading.Thread(target=monitor_loop, daemon=True).start()

    def stop_monitoring(self):
        """Stops the background monitoring thread."""
        self.is_monitoring = False
        self.controller._log("Stopped background hardware monitoring.")
