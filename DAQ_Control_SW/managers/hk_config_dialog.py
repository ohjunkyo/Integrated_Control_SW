"""HK Digitizer configuration dialog (customtkinter).

Acquisition options and post-processing steps are editable lists (Edit -> add,
delete, reorder); infrastructure fields sit in a folded, password-gated
"Detail settings" section; the final command is shown as one line with a
"Show full" toggle. Every Save also keeps a dated snapshot that "Load" lists.
"""
import os
import json
import threading
import tkinter as tk
from tkinter import messagebox, filedialog
from datetime import datetime

import customtkinter as ctk

MUTED = "#6c757d"
HINT = "#8a9099"
DANGER = "#d9534f"

PROTECTED = [  # (key, label, width; 0 = stretch)
    ("ssh_user", "HK PC user", 160),
    ("ssh_host", "HK PC host / IP", 200),
    ("setup_cmd", "Setup cmd", 0),
    ("scan_manager", "ScanManager", 0),
    ("dpb_host", "DPB board host / IP", 200),
    ("dpb_setup_cmd", "DPB setup (②)", 0),
    ("vmodem_device", "vmodem device (③)", 0),
    ("vmodem_keys", "vmodem keys (③)", 160),
]


def _font(size=13, weight="normal"):
    return ctk.CTkFont(size=size, weight=weight)


def _mono(size=12):
    return ctk.CTkFont(family="DejaVu Sans Mono", size=size)


def _small_btn(parent, text, cmd, width=32, **kw):
    return ctk.CTkButton(parent, text=text, command=cmd, width=width, height=26,
                         font=_font(12), **kw)


def open_hk_config_dialog(ui):
    """Build (or raise) the HK Config window. `ui` is the AutomationUI instance."""
    ctrl = ui.controller
    auto_mgr = ctrl.auto_mgr
    hk = auto_mgr.hk_config

    existing = getattr(ui, "_hk_cfg_win", None)
    if existing is not None:
        try:
            if existing.winfo_exists():
                existing.deiconify()
                existing.lift()
                existing.focus_force()
                return
        except tk.TclError:
            pass

    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("blue")
    win = ctk.CTkToplevel(ui.notebook)
    ui._hk_cfg_win = win
    win.title("HK Digitizer Configuration")
    win.geometry("940x860")
    win.minsize(700, 520)
    win.grid_columnconfigure(0, weight=1)
    win.grid_rowconfigure(2, weight=1)

    # ── header ──────────────────────────────────────────────────────────────
    ctk.CTkLabel(win, text="HK Digitizer Configuration", font=_font(20, "bold")).grid(
        row=0, column=0, sticky="w", padx=20, pady=(16, 0))
    head2 = ctk.CTkFrame(win, fg_color="transparent")
    head2.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 6))
    ctk.CTkLabel(head2, text="2nd-PC data path — local config3.h is not used.",
                 text_color=MUTED, font=_font(12)).pack(anchor="w")
    angle_lbl = ctk.CTkLabel(head2, text="", text_color="#2e7d32", font=_font(12, "bold"))
    angle_lbl.pack(anchor="w")

    def _refresh_angle_live():
        if not win.winfo_exists():
            return
        try:
            t2, r2 = ctrl.rot_mgr.read_angles(2)
            t3, r3 = ctrl.rot_mgr.read_angles(3)
            sn2 = f"{t2:.1f}°/{r2:.1f}°" if t2 is not None and r2 is not None else "—"
            sn3 = f"{t3:.1f}°/{r3:.1f}°" if t3 is not None and r3 is not None else "—"
            live = f"SN2 {sn2}   SN3 {sn3}"
        except Exception:
            live = "—"
        angle_lbl.configure(text=f"Angles (tilt / rot): automatic per scan point  |  live: {live}")
        win.after(1000, _refresh_angle_live)

    scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
    scroll.grid(row=2, column=0, sticky="nsew", padx=8)
    scroll.grid_columnconfigure(0, weight=1)

    def _card(row, title):
        card = ctk.CTkFrame(scroll, corner_radius=12)
        card.grid(row=row, column=0, sticky="ew", padx=10, pady=(0, 10))
        card.grid_columnconfigure(0, weight=1)
        bar = ctk.CTkFrame(card, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 4))
        ctk.CTkLabel(bar, text=title, font=_font(15, "bold")).pack(side=tk.LEFT)
        return card, bar

    preview_hooks = []

    def _changed(*_a):
        for f in preview_hooks:
            f()

    def _svar(value):
        v = tk.StringVar(value=str(value))
        v.trace_add("write", _changed)
        return v

    def _bvar(value):
        v = tk.BooleanVar(value=bool(value))
        v.trace_add("write", _changed)
        return v

    def _lbl(parent, text, r, c):
        ctk.CTkLabel(parent, text=text, font=_font(13), text_color="#33373d").grid(
            row=r, column=c, sticky="w", padx=(0, 8), pady=4)

    # ── Acquisition: fixed fields ───────────────────────────────────────────
    acq_card, acq_bar = _card(0, "Acquisition")
    core = ctk.CTkFrame(acq_card, fg_color="transparent")
    core.grid(row=1, column=0, sticky="ew", padx=14)
    for c in (1, 3):
        core.grid_columnconfigure(c, weight=1)

    v_run_id = _svar(hk.get("run_id", ""))
    v_run_no = _svar(hk.get("run_number", 0))
    v_acq = _svar(hk.get("acq_time", 10.0))
    v_delay = _svar(hk.get("move_delay", 20.0))
    v_wd = _svar(hk.get("work_dir", ""))

    _lbl(core, "Filename (-i)", 0, 0)
    ctk.CTkEntry(core, textvariable=v_run_id).grid(row=0, column=1, columnspan=3, sticky="ew", pady=4)
    ctk.CTkLabel(core, text="{run} {tilt} {rot2} {rot3} {trgch} {trgvth} {mode} fill in per point",
                 text_color=HINT, font=_font(11)).grid(row=1, column=1, columnspan=3, sticky="w")

    _lbl(core, "Run number", 2, 0)
    rn_box = ctk.CTkFrame(core, fg_color="transparent")
    rn_box.grid(row=2, column=1, sticky="w", pady=4)
    run_no_entry = ctk.CTkEntry(rn_box, textvariable=v_run_no, width=80)
    run_no_entry.pack(side=tk.LEFT)
    run_live_lbl = ctk.CTkLabel(rn_box, text="", text_color=ui.PALETTE["accent"], font=_font(12, "bold"))
    run_live_lbl.pack(side=tk.LEFT, padx=8)
    _small_btn(rn_box, "Sync from HK PC", lambda: _sync_run_number(), width=120).pack(side=tk.LEFT)

    _lbl(core, "Acq time --l (s)", 2, 2)
    ctk.CTkEntry(core, textvariable=v_acq, width=100).grid(row=2, column=3, sticky="w", pady=4)
    _lbl(core, "Move delay (s)", 3, 0)
    ctk.CTkEntry(core, textvariable=v_delay, width=100).grid(row=3, column=1, sticky="w", pady=4)
    _lbl(core, "Work dir (cd)", 3, 2)
    ctk.CTkEntry(core, textvariable=v_wd).grid(row=3, column=3, sticky="ew", pady=4)

    # ── editable list (shared by options and post-processing) ───────────────
    class EditList:
        """Rows of tk vars; Edit mode adds label/flag editing, reorder,
        delete and an add row."""

        def __init__(self, card, bar, items, kind, add_hint):
            self.items = items
            self.kind = kind            # "options" | "steps"
            self.editing = False
            self.add_hint = add_hint
            self.frame = ctk.CTkFrame(card, fg_color="transparent")
            self.frame.grid(row=2, column=0, sticky="ew", padx=14, pady=(2, 6))
            self.add_frame = ctk.CTkFrame(card, fg_color="transparent")
            self.btn = _small_btn(bar, "Edit", self.toggle, width=70)
            self.btn_color = self.btn.cget("fg_color")
            self.btn.pack(side=tk.RIGHT)
            self.render()

        def toggle(self):
            self.editing = not self.editing
            self.btn.configure(text="Done" if self.editing else "Edit",
                               fg_color=ui.PALETTE["start"] if self.editing else self.btn_color)
            self.render()

        def move(self, i, d):
            j = i + d
            if 0 <= j < len(self.items):
                self.items[i], self.items[j] = self.items[j], self.items[i]
                self.render()
                _changed()

        def delete(self, i):
            del self.items[i]
            self.render()
            _changed()

        def render(self):
            for w in self.frame.winfo_children():
                w.destroy()
            f = self.frame
            f.grid_columnconfigure(2, weight=1)
            if not self.items:
                ctk.CTkLabel(f, text="(none — press Edit to add)", text_color=HINT,
                             font=_font(12)).grid(row=0, column=0, sticky="w")
            for i, it in enumerate(self.items):
                (self._option_row if self.kind == "options" else self._step_row)(f, i, it)
                if self.editing:
                    bx = ctk.CTkFrame(f, fg_color="transparent")
                    bx.grid(row=i, column=3, sticky="e", padx=(6, 0))
                    _small_btn(bx, "↑", lambda i=i: self.move(i, -1), width=28).pack(side=tk.LEFT, padx=1)
                    _small_btn(bx, "↓", lambda i=i: self.move(i, 1), width=28).pack(side=tk.LEFT, padx=1)
                    _small_btn(bx, "×", lambda i=i: self.delete(i), width=28,
                               fg_color=DANGER, hover_color="#b52b27").pack(side=tk.LEFT, padx=(4, 0))
            self._render_add()

        def _check(self, f, i, it, text):
            ctk.CTkCheckBox(f, text=text, variable=it["on"], onvalue=True, offvalue=False,
                            font=_font(13), width=24 if not text else 170).grid(
                row=i, column=0, sticky="w", pady=2)

        def _option_row(self, f, i, it):
            if self.editing:
                self._check(f, i, it, "")
                box = ctk.CTkFrame(f, fg_color="transparent")
                box.grid(row=i, column=1, sticky="w", pady=2)
                ctk.CTkEntry(box, textvariable=it["label"], width=150).pack(side=tk.LEFT, padx=(0, 6))
                ctk.CTkEntry(box, textvariable=it["flag"], width=140, font=_mono()).pack(side=tk.LEFT)
            else:
                self._check(f, i, it, it["label"].get())
                ctk.CTkLabel(f, text=it["flag"].get(), font=_mono(), text_color=MUTED, width=130,
                             anchor="w").grid(row=i, column=1, sticky="w", padx=6)
            if it["bare"]:
                ctk.CTkLabel(f, text="flag only", text_color=HINT, font=_font(12)).grid(
                    row=i, column=2, sticky="w", padx=(6, 0))
            else:
                ctk.CTkEntry(f, textvariable=it["value"]).grid(row=i, column=2, sticky="ew", padx=(6, 0), pady=2)

        def _step_row(self, f, i, it):
            if self.editing:
                self._check(f, i, it, "")
                ctk.CTkEntry(f, textvariable=it["name"], width=150).grid(row=i, column=1, sticky="w", pady=2)
                ctk.CTkEntry(f, textvariable=it["cmd"], font=_mono()).grid(
                    row=i, column=2, sticky="ew", padx=(6, 0), pady=2)
            else:
                self._check(f, i, it, it["name"].get())
                cmd = it["cmd"].get()
                ctk.CTkLabel(f, text=cmd if len(cmd) <= 90 else cmd[:89] + "…", font=_mono(),
                             text_color=MUTED, anchor="w").grid(row=i, column=1, columnspan=2,
                                                               sticky="w", padx=(6, 0))

        def _render_add(self):
            for w in self.add_frame.winfo_children():
                w.destroy()
            if not self.editing:
                self.add_frame.grid_forget()
                return
            a = self.add_frame
            a.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 10))
            a.grid_columnconfigure(2, weight=1)
            opt = self.kind == "options"
            e1 = ctk.CTkEntry(a, placeholder_text="Label" if opt else "Step name", width=150)
            e2 = ctk.CTkEntry(a, placeholder_text="--flag", width=140, font=_mono()) if opt else None
            e3 = ctk.CTkEntry(a, placeholder_text="value (blank = flag only)" if opt
                              else 'command, e.g. Tool.py --in "{bin}" -o "{base}_out.root"',
                              font=_font(13) if opt else _mono())
            e1.grid(row=0, column=0, sticky="w")
            if e2:
                e2.grid(row=0, column=1, sticky="w", padx=(6, 0))
            e3.grid(row=0, column=2, sticky="ew", padx=6)
            err = ctk.CTkLabel(a, text="", text_color=DANGER, font=_font(12))
            err.grid(row=1, column=0, columnspan=4, sticky="w")
            ctk.CTkLabel(a, text=self.add_hint, text_color=HINT, font=_font(11)).grid(
                row=2, column=0, columnspan=4, sticky="w")

            def _add():
                name = e1.get().strip()
                if opt:
                    flag, val = e2.get().strip(), e3.get().strip()
                    if not name or not flag.startswith("-"):
                        err.configure(text="Enter a label and a flag starting with '-'.")
                        return
                    self.items.append(_opt_vars({"label": name, "flag": flag, "value": val,
                                                 "on": True, "bare": val == ""}))
                else:
                    cmd = e3.get().strip()
                    if not name or not cmd:
                        err.configure(text="Enter a step name and a command.")
                        return
                    self.items.append(_step_vars({"name": name, "cmd": cmd, "on": True}))
                self.render()
                _changed()

            ctk.CTkButton(a, text="+ Add parameter" if opt else "+ Add step", command=_add,
                          width=130, height=28).grid(row=0, column=3, sticky="e")
            for e in (e1, e2, e3):
                if e:
                    e.bind("<Key>", lambda _e: err.configure(text=""))

    def _opt_vars(o):
        return {"label": _svar(o.get("label", "")), "flag": _svar(o.get("flag", "")),
                "value": _svar(o.get("value", "")), "on": _bvar(o.get("on", True)),
                "bare": bool(o.get("bare"))}

    def _step_vars(p):
        return {"name": _svar(p.get("name", "")), "cmd": _svar(p.get("cmd", "")),
                "on": _bvar(p.get("on", False))}

    ctk.CTkLabel(acq_bar, text="  ScanManager options, sent in this order", text_color=HINT,
                 font=_font(12)).pack(side=tk.LEFT)
    opts = EditList(acq_card, acq_bar, [_opt_vars(o) for o in hk.get("scan_options", [])], "options",
                    "Blank value = flag only (sent when ticked). A valued option is left out while its value is blank.")

    post_card, post_bar = _card(1, "Post-processing")
    ctk.CTkLabel(post_bar, text="  runs on each new .bin, in order; stops at the first failure",
                 text_color=HINT, font=_font(12)).pack(side=tk.LEFT)
    steps = EditList(post_card, post_bar, [_step_vars(p) for p in hk.get("post_steps", [])], "steps",
                     '{bin} = the .bin just written, {base} = the same name without .bin. Quote them: "{bin}".')

    # ── Detail settings (folded, password-gated) ────────────────────────────
    det_card = ctk.CTkFrame(scroll, corner_radius=12)
    det_card.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
    det_card.grid_columnconfigure(0, weight=1)
    det_body = ctk.CTkFrame(det_card, fg_color="transparent")
    det_body.grid_columnconfigure(1, weight=1)
    det_open = [False]

    def _toggle_detail():
        det_open[0] = not det_open[0]
        det_btn.configure(text=("▾" if det_open[0] else "▸") + "  Detail settings  (admin)")
        if det_open[0]:
            det_body.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 12))
        else:
            det_body.grid_forget()

    det_btn = ctk.CTkButton(det_card, text="▸  Detail settings  (admin)", command=_toggle_detail,
                            fg_color="transparent", text_color="#1f2430", hover_color="#e3e6ea",
                            anchor="w", font=_font(15, "bold"))
    det_btn.grid(row=0, column=0, sticky="ew", padx=6, pady=6)

    lock_row = ctk.CTkFrame(det_body, fg_color="transparent")
    lock_row.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
    unlock_btn = ctk.CTkButton(lock_row, text="Admin unlock", width=120, height=28,
                               fg_color=ui.PALETTE["warn"], hover_color="#c98a10", text_color="#412402")
    unlock_btn.pack(side=tk.LEFT)
    lock_note = ctk.CTkLabel(lock_row, text="  Where and how commands run on the HK PC — locked by default.",
                             text_color=HINT, font=_font(12))
    lock_note.pack(side=tk.LEFT)

    # ssh_target is stored as "user@host" but shown as two boxes.
    u, _, h = str(hk.get("ssh_target", "") or "").rpartition("@")
    seed = {"ssh_user": u, "ssh_host": h}
    pvars, pentries = {}, []
    for r, (key, label, width) in enumerate(PROTECTED, start=1):
        _lbl(det_body, label, r, 0)
        v = _svar(seed.get(key, hk.get(key, "")))
        e = ctk.CTkEntry(det_body, textvariable=v, state="disabled", **({"width": width} if width else {}))
        e.grid(row=r, column=1, sticky="w" if width else "ew", pady=3)
        pvars[key] = v
        pentries.append(e)
    unlocked = [False]

    def _unlock():
        if ctrl.access_mgr.verify_password_prompt(
                "Security", "Enter Master Password (HK Infrastructure Fields):", parent=win):
            for e in pentries:
                e.configure(state="normal")
            unlocked[0] = True
            unlock_btn.configure(state="disabled", text="Unlocked")
            lock_note.configure(text="  Unlocked for this dialog session.")
            ctrl._log("[INFO] HK infrastructure fields unlocked (admin).")
        else:
            ctrl._log("[WARNING] Admin unlock denied for HK infrastructure fields.")
    unlock_btn.configure(command=_unlock)

    # ── collect ─────────────────────────────────────────────────────────────
    def _ssh_target(d):
        uu, hh = (d.get("ssh_user") or "").strip(), (d.get("ssh_host") or "").strip()
        return f"{uu}@{hh}" if uu else hh

    def _lists():
        o = [{"label": it["label"].get().strip(), "flag": it["flag"].get().strip(),
              "value": it["value"].get().strip(), "on": bool(it["on"].get()), "bare": it["bare"]}
             for it in opts.items]
        p = [{"name": it["name"].get().strip(), "cmd": it["cmd"].get().strip(), "on": bool(it["on"].get())}
             for it in steps.items]
        return o, p

    def _collect():
        new = {"run_id": v_run_id.get().strip(), "work_dir": v_wd.get().strip()}
        try:
            new["run_number"] = int(float(v_run_no.get()))
            new["acq_time"] = float(v_acq.get())
            new["move_delay"] = float(v_delay.get())
        except ValueError:
            raise ValueError("Run number, Acq time and Move delay must be numbers.")
        o, p = _lists()
        for x in o:
            if not x["flag"].startswith("-"):
                raise ValueError(f"Option '{x['label'] or '?'}': flag must start with '-'.")
        new["scan_options"], new["post_steps"] = o, p
        d = {k: v.get().strip() for k, v in pvars.items()}
        new["ssh_target"] = _ssh_target(d)
        for k, _l, _w in PROTECTED:
            if k not in ("ssh_user", "ssh_host"):
                new[k] = d[k]
        return new

    def _live():
        d = dict(hk)
        d.update({k: v.get() for k, v in pvars.items()})
        d["ssh_target"] = _ssh_target(d)
        d["run_id"], d["work_dir"] = v_run_id.get(), v_wd.get()
        d["scan_options"], d["post_steps"] = _lists()
        return d

    # ── final command ───────────────────────────────────────────────────────
    cmd_card = ctk.CTkFrame(win, corner_radius=12)
    cmd_card.grid(row=3, column=0, sticky="ew", padx=18, pady=(4, 6))
    cmd_card.grid_columnconfigure(1, weight=1)
    ctk.CTkLabel(cmd_card, text="Final command", font=_font(14, "bold")).grid(
        row=0, column=0, sticky="w", padx=(14, 10), pady=8)
    summary = ctk.CTkLabel(cmd_card, text="", font=_mono(), text_color="#33373d", anchor="w")
    summary.grid(row=0, column=1, sticky="ew")
    full_box = ctk.CTkTextbox(cmd_card, height=120, font=_mono(), wrap="char")
    full_shown = [False]
    current = {"acq": ""}

    def _toggle_full():
        full_shown[0] = not full_shown[0]
        full_btn.configure(text="Hide" if full_shown[0] else "Show full")
        if full_shown[0]:
            full_box.grid(row=1, column=0, columnspan=4, sticky="ew", padx=12, pady=(0, 10))
        else:
            full_box.grid_forget()

    def _copy():
        win.clipboard_clear()
        win.clipboard_append(current["acq"])
        status.configure(text="Command copied")

    full_btn = _small_btn(cmd_card, "Show full", _toggle_full, width=90)
    full_btn.grid(row=0, column=2, padx=4)
    _small_btn(cmd_card, "Copy", _copy, width=60).grid(row=0, column=3, padx=(0, 12))

    def _refresh_preview():
        d = _live()
        try:
            acq = float(v_acq.get() or 0)
        except ValueError:
            acq = 0.0
        try:
            run_no = int(float(v_run_no.get() or 0))
        except ValueError:
            run_no = 0
        run_id = d.get("run_id", "").replace("{run}", f"{run_no:03d}")
        try:
            full = auto_mgr.hk_format_remote(d, run_id, acq)
            short = (f"{os.path.basename(str(d.get('scan_manager', '')))} -i {run_id} --l {int(round(acq))}"
                     f"{auto_mgr.hk_option_args(d)}")
            n = sum(1 for p in d["post_steps"] if p["on"] and p["cmd"])
            if n:
                short += f"   + {n} post step{'s' if n > 1 else ''}"
        except Exception as e:
            full = short = f"(cannot build yet: {e})"
        try:
            vm = auto_mgr.hk_format_vmodem(d)
        except Exception as e:
            vm = f"(cannot build yet: {e})"
        summary.configure(text=short if len(short) <= 95 else short[:94] + "…")
        current["acq"] = full
        full_box.configure(state="normal")
        full_box.delete("1.0", "end")
        full_box.insert("1.0", f"Filename: {run_id}\n\n[④ Acquire]\n{full}\n\n[③ vmodem]\n{vm}")
        full_box.configure(state="disabled")

    preview_hooks.append(_refresh_preview)

    # ── run number live / sync ──────────────────────────────────────────────
    def _refresh_run_live():
        if not win.winfo_exists():
            return
        live = hk.get("run_number", "?")
        run_live_lbl.configure(text=f"● live: {live}")
        # Follow the live counter unless being typed in, so a later Save
        # can't push a stale number back and overwrite a file.
        try:
            focused = win.focus_get()
        except KeyError:
            focused = None
        if focused is not getattr(run_no_entry, "_entry", run_no_entry) and v_run_no.get() != str(live):
            v_run_no.set(str(live))
        win.after(1000, _refresh_run_live)

    def _apply_without_run_number():
        new = _collect()
        new.pop("run_number", None)
        hk.update(new)
        auto_mgr.save_hk_config()

    def _sync_run_number():
        try:
            _apply_without_run_number()
        except ValueError as e:
            messagebox.showerror("Invalid HK Config", str(e), parent=win)
            return
        status.configure(text="Reading HK data folder…")

        def _done(res):
            if not res.get("ok"):
                status.configure(text="Folder read failed")
                messagebox.showerror("Sync Run Number",
                                     f"Could not list the HK data folder.\n\nFolder: {res.get('dir', '?')}\n\n"
                                     f"{res.get('error', '')}", parent=win)
                return
            nxt, runs, d = res["next"], res["runs"], res["dir"]
            found = f"highest existing: Run{max(runs):03d}" if runs else "no Run-numbered files yet"
            if not messagebox.askyesno("Sync Run Number",
                                       f"Folder: {d}\n{res['n_files']} file(s), {found}.\n\n"
                                       f"Set Run Number to {nxt}?  (current: {hk.get('run_number')})",
                                       parent=win):
                status.configure(text="Sync cancelled")
                return
            hk["run_number"] = nxt
            auto_mgr.save_hk_config()
            v_run_no.set(str(nxt))
            status.configure(text=f"Run number → {nxt}")

        def _work():
            res = auto_mgr.hk_probe_remote_runs()
            win.after(0, lambda: _done(res))
        threading.Thread(target=_work, daemon=True).start()

    # ── load snapshot ───────────────────────────────────────────────────────
    def _apply_loaded(cfg, label):
        v_run_id.set(cfg.get("run_id", v_run_id.get()))
        v_acq.set(str(cfg.get("acq_time", v_acq.get())))
        v_delay.set(str(cfg.get("move_delay", v_delay.get())))
        v_wd.set(cfg.get("work_dir", v_wd.get()))
        opts.items[:] = [_opt_vars(o) for o in cfg.get("scan_options", [])]
        steps.items[:] = [_step_vars(p) for p in cfg.get("post_steps", [])]
        opts.render()
        steps.render()
        note = ""
        if unlocked[0]:
            uu, _, hh = str(cfg.get("ssh_target", "")).rpartition("@")
            for k, _l, _w in PROTECTED:
                val = {"ssh_user": uu, "ssh_host": hh}.get(k, cfg.get(k))
                if val is not None:
                    pvars[k].set(val)
        else:
            note = " (detail settings not loaded: unlock first)"
        _changed()
        status.configure(text=f"Loaded {label}{note} — press Save to apply")

    def _describe(cfg):
        """[(key, line)] summary of a config, keyed so two configs can be compared line by line."""
        rows = [("run_id", f"Filename      {cfg.get('run_id', '')}"),
                ("acq_time", f"Acq time      {cfg.get('acq_time', '?')} s"),
                ("move_delay", f"Move delay    {cfg.get('move_delay', '?')} s"),
                ("work_dir", f"Work dir      {cfg.get('work_dir', '')}"),
                ("", ""), ("", "ScanManager options (in order)")]
        for o in cfg.get("scan_options", []):
            val = "(flag only)" if o.get("bare") else (str(o.get("value", "")) or "(blank, not sent)")
            rows.append((f"opt:{o.get('flag')}",
                         f"  [{'x' if o.get('on') else ' '}] {o.get('label', ''):<18} {o.get('flag', ''):<14} {val}"))
        rows += [("", ""), ("", "Post-processing")]
        for p in cfg.get("post_steps", []):
            rows.append((f"step:{p.get('name')}",
                         f"  [{'x' if p.get('on') else ' '}] {p.get('name', ''):<12} {p.get('cmd', '')}"))
        rows += [("", ""), ("", "Detail settings"),
                 ("ssh_target", f"  SSH target    {cfg.get('ssh_target', '')}")]
        for k, label, _w in PROTECTED:
            if k not in ("ssh_user", "ssh_host"):
                rows.append((k, f"  {label:<20} {cfg.get(k, '')}"))
        return rows

    def _load_dialog():
        snaps = auto_mgr.hk_config_snapshots()
        dlg = ctk.CTkToplevel(win)
        dlg.title("Load HK config")
        dlg.geometry("900x720")
        dlg.transient(win)
        dlg.grid_columnconfigure(0, weight=1)
        dlg.grid_rowconfigure(1, weight=2)
        dlg.grid_rowconfigure(3, weight=3)
        ctk.CTkLabel(dlg, text="Saved configurations, newest first. View shows the details; "
                               "the run number is never loaded.",
                     text_color=MUTED, font=_font(12)).grid(row=0, column=0, sticky="w", padx=16, pady=(12, 4))
        lst = ctk.CTkScrollableFrame(dlg)
        lst.grid(row=1, column=0, sticky="nsew", padx=12, pady=4)
        lst.grid_columnconfigure(1, weight=1)

        vbar = ctk.CTkFrame(dlg, fg_color="transparent")
        vbar.grid(row=2, column=0, sticky="ew", padx=14, pady=(6, 0))
        view_lbl = ctk.CTkLabel(vbar, text="Press View on a configuration to see it here.",
                                font=_font(13, "bold"))
        view_lbl.pack(side=tk.LEFT)
        load_viewed = _small_btn(vbar, "Load this", lambda: None, width=90)
        ctk.CTkLabel(vbar, text="* = differs from the settings on screen", text_color="#b45309",
                     font=_font(11)).pack(side=tk.RIGHT, padx=10)
        box = ctk.CTkTextbox(dlg, font=_mono(12), wrap="none")
        box.grid(row=3, column=0, sticky="nsew", padx=12, pady=4)
        box.tag_config("diff", foreground="#b45309")
        box.configure(state="disabled")
        viewed = {}

        def _view(cfg, label):
            cur = dict(_describe(_live()))
            try:
                rn = int(float(v_run_no.get() or 0))
            except ValueError:
                rn = 0
            rid = str(cfg.get("run_id", "")).replace("{run}", f"{rn:03d}")
            try:
                cmd = auto_mgr.hk_format_remote(cfg, rid, float(cfg.get("acq_time", 0)))
            except Exception as e:
                cmd = f"(cannot build: {e})"
            box.configure(state="normal")
            box.delete("1.0", "end")
            for key, line in _describe(cfg):
                differs = bool(key) and cur.get(key) != line
                box.insert("end", ("* " if differs else "  ") + line + "\n", "diff" if differs else ())
            box.insert("end", f"\n  Command (run {rn:03d})\n  {cmd}\n")
            box.configure(state="disabled")
            view_lbl.configure(text=f"Viewing: {label}")
            viewed.update(cfg=cfg, label=label)
            load_viewed.configure(command=lambda: (_apply_loaded(viewed["cfg"], viewed["label"]), dlg.destroy()))
            load_viewed.pack(side=tk.LEFT, padx=10)

        if not snaps:
            ctk.CTkLabel(lst, text="No saved configurations yet — each Save adds one.",
                         text_color=HINT).grid(row=0, column=0, sticky="w", pady=8)
        for r, (_path, when, cfg) in enumerate(snaps):
            ctk.CTkLabel(lst, text=when, font=_font(13, "bold")).grid(row=2 * r, column=0, sticky="w", padx=(4, 10))
            rid = str(cfg.get("run_id", ""))
            ctk.CTkLabel(lst, text=rid if len(rid) <= 70 else rid[:69] + "…", font=_mono(11),
                         anchor="w").grid(row=2 * r, column=1, sticky="w")
            on = [p.get("name", "") for p in cfg.get("post_steps", []) if p.get("on")]
            info = (f"{cfg.get('acq_time', '?')} s ·{auto_mgr.hk_option_args(cfg) or ' no options'}"
                    + (f" · post: {', '.join(on)}" if on else ""))
            ctk.CTkLabel(lst, text=info, text_color=MUTED, font=_font(11), anchor="w").grid(
                row=2 * r + 1, column=1, sticky="w", pady=(0, 6))
            _small_btn(lst, "View", lambda c=cfg, w=when: _view(c, w), width=60, **grey).grid(
                row=2 * r, column=2, rowspan=2, padx=2)
            _small_btn(lst, "Load", lambda c=cfg, w=when: (_apply_loaded(c, w), dlg.destroy()),
                       width=60).grid(row=2 * r, column=3, rowspan=2, padx=(2, 4))

        def _browse():
            p = filedialog.askopenfilename(parent=dlg, title="Load HK config",
                                           filetypes=[("JSON", "*.json"), ("All files", "*")],
                                           initialdir=os.path.dirname(auto_mgr.hk_config_file))
            if not p:
                return
            try:
                with open(p, encoding="utf-8") as f:
                    cfg = auto_mgr.hk_legacy_options(json.load(f))
            except Exception as e:
                messagebox.showerror("Load HK config", f"Could not read {p}:\n{e}", parent=dlg)
                return
            _view(cfg, os.path.basename(p))

        bot = ctk.CTkFrame(dlg, fg_color="transparent")
        bot.grid(row=4, column=0, sticky="ew", padx=12, pady=(4, 12))
        ctk.CTkButton(bot, text="Browse file…", command=_browse, width=120).pack(side=tk.LEFT)
        ctk.CTkButton(bot, text="Close", command=dlg.destroy, width=90, **grey).pack(side=tk.RIGHT)
        if snaps:
            _view(snaps[0][2], snaps[0][1])
        dlg.after(100, dlg.lift)

    # ── pipeline stages ─────────────────────────────────────────────────────
    def _run_pipeline_stage(build_fn, name):
        # run_number is left alone: these buttons don't own the counter.
        try:
            _apply_without_run_number()
        except ValueError as e:
            messagebox.showerror("Invalid HK Config", str(e), parent=win)
            return
        try:
            cmd = (build_fn() or "").strip()
        except Exception as e:
            messagebox.showerror("Build Error", f"{name}: {e}", parent=win)
            return
        if not cmd:
            messagebox.showinfo("Nothing to run", f"{name}: command is empty.", parent=win)
            return
        auto_mgr.hk_run_in_console(cmd, job_name=name)

    def _run_stage_once(build_fn, name, done_attr, risk_note):
        if getattr(auto_mgr, done_attr, False):
            if not messagebox.askyesno(f"{name} already run",
                                       f"{name} was already run this session.\n\n{risk_note}\n\n"
                                       "Run it again anyway?", parent=win):
                return
        _run_pipeline_stage(build_fn, name)
        setattr(auto_mgr, done_attr, True)

    def _run_dpb_setup():
        # The session flag resets on app restart while the remote daemons
        # survive, so check the live socat count first.
        status.configure(text="Checking remote DPB state…")
        win.update_idletasks()
        count = auto_mgr.hk_dpb_alive_count()
        status.configure(text="")
        if count == -1:
            _run_stage_once(auto_mgr.hk_build_dpb_setup, "DPB Setup", "hk_dpb_setup_done",
                            "Re-running can spawn a DUPLICATE socat/daq daemon if the first is still up.\n"
                            "(Could not verify live remote state just now -- SSH check failed.)")
            return
        if count >= 2 and not messagebox.askyesno(
                "DPB already running remotely",
                f"Found {count} live 'socat' process(es) on the DPB board right now --\n"
                "DPB Setup already appears to be up and running.\n\n"
                "Running it again WILL spawn duplicate socat/daq daemons on the SAME ports.\n\n"
                "Run it again anyway?", parent=win):
            return
        _run_pipeline_stage(auto_mgr.hk_build_dpb_setup, "DPB Setup")
        auto_mgr.hk_dpb_setup_done = True

    lock_note_txt = ("On THIS setup the DPB bridge is started and owned by the HK PC side, "
                     "and it is already running.\n\nRunning this stage can lay a second, "
                     "differently-configured bridge over the working one and break the readout path.\n\n"
                     "Only continue if the HK-PC side has told you the bridge is down.")

    def _locked_stage(fn, label):
        def _go():
            if not messagebox.askyesno(f"{label} — normally not needed",
                                       f"{lock_note_txt}\n\nContinue to the password prompt?",
                                       parent=win, icon="warning"):
                return
            if not ctrl.access_mgr.verify_password_prompt(
                    "Security", f"Enter Master Password ({label}):", parent=win):
                ctrl._log(f"[WARNING] {label} denied (admin unlock failed).")
                return
            fn()
        return _go

    grey = {"fg_color": "gray75", "hover_color": "gray65", "text_color": "#1f2430"}
    stage_row = ctk.CTkFrame(win, fg_color="transparent")
    stage_row.grid(row=4, column=0, sticky="ew", padx=18, pady=(0, 4))
    ctk.CTkLabel(stage_row, text="Pipeline", font=_font(13, "bold")).pack(side=tk.LEFT, padx=(0, 10))
    ctk.CTkButton(stage_row, text="② DPB setup (admin)", width=160, height=30, **grey,
                  command=_locked_stage(_run_dpb_setup, "DPB Setup")).pack(side=tk.LEFT, padx=(0, 6))
    ctk.CTkButton(stage_row, text="③ vmodem (admin)", width=150, height=30, **grey,
                  command=_locked_stage(lambda: _run_stage_once(
                      auto_mgr.hk_build_vmodem_remote, "vmodem Process", "hk_vmodem_done",
                      "Re-sending the m/O keys may just re-toggle the mode instead of setting it."),
                      "vmodem Process")).pack(side=tk.LEFT, padx=(0, 6))
    ctk.CTkButton(stage_row, text="④ Acquire (ScanManager)", width=180, height=30,
                  command=lambda: _run_pipeline_stage(lambda: auto_mgr.hk_build_remote_current(),
                                                      "HK Acquire")).pack(side=tk.LEFT)

    # ── bottom buttons ──────────────────────────────────────────────────────
    def _test_connection():
        try:
            _apply_without_run_number()
        except ValueError as e:
            messagebox.showerror("Invalid HK Config", str(e), parent=win)
            return
        target, sm = hk.get("ssh_target", ""), hk.get("scan_manager", "")
        status.configure(text=f"Testing {target} …")
        conn_btn.configure(state="disabled")

        def _done(rc, out):
            conn_btn.configure(state="normal")
            if rc != 0:
                status.configure(text="SSH failed")
                messagebox.showerror("HK Connection Failed",
                                     f"Target: {target}\n\nssh exit code {rc}\n\n{out.strip()[:800]}\n\n"
                                     "Check the host/IP and user in Detail settings, and that this PC's "
                                     "SSH key is installed on the HK PC (ssh-copy-id).", parent=win)
                return
            ok_sm = "SCANMGR=OK" in out
            info = " ".join(l for l in out.splitlines() if l.startswith("HOST="))
            status.configure(text="Connected" if ok_sm else "Connected — ScanManager missing")
            messagebox.showinfo("HK Connection", f"Target: {target}\n{info}\n\n"
                                + ("ScanManager found:\n" if ok_sm else "ScanManager NOT found at:\n") + sm,
                                parent=win)

        def _work():
            remote = ('echo "HOST=$(hostname) USER=$(whoami)"; '
                      f'if [ -f {sm} ]; then echo SCANMGR=OK; else echo SCANMGR=MISSING; fi')
            rc, out = auto_mgr._hk_ssh(remote, wait=True, timeout=15)
            win.after(0, lambda: _done(rc, out or ""))
        threading.Thread(target=_work, daemon=True).start()

    def _save():
        # Stays open after Save: operators watch the live run number here.
        try:
            hk.update(_collect())
        except ValueError as e:
            messagebox.showerror("Invalid HK Config", f"{e}\n\nKeeping previous values.", parent=win)
            return
        auto_mgr.save_hk_config(snapshot=True)
        on = [p["name"] for p in hk["post_steps"] if p["on"]]
        ctrl._log(f"[INFO] HK config saved: filename {hk['run_id']}, run#{hk['run_number']}, "
                  f"acq {hk['acq_time']}s, delay {hk['move_delay']}s, options"
                  f"{auto_mgr.hk_option_args(hk) or ' (none)'}, post {', '.join(on) or 'none'}, "
                  f"target {hk['ssh_target']}")
        status.configure(text=f"Saved @ {datetime.now().strftime('%H:%M:%S')}")
        opts.render()
        steps.render()

    btns = ctk.CTkFrame(win, fg_color="transparent")
    btns.grid(row=5, column=0, sticky="ew", padx=18, pady=(4, 14))
    conn_btn = ctk.CTkButton(btns, text="Test connection", width=130, height=32, **grey,
                             command=_test_connection)
    conn_btn.pack(side=tk.LEFT, padx=(0, 6))
    ctk.CTkButton(btns, text="Test trigger (dummy)", width=150, height=32, fg_color=ui.PALETTE["move"],
                  command=lambda: _run_pipeline_stage(
                      lambda: auto_mgr.hk_build_remote_current(prefix="TEST_"), "HK Test")).pack(
        side=tk.LEFT, padx=(0, 6))
    ctk.CTkButton(btns, text="Load…", width=80, height=32, **grey, command=_load_dialog).pack(side=tk.LEFT)
    ctk.CTkButton(btns, text="Close", width=80, height=32, **grey, command=win.destroy).pack(side=tk.RIGHT)
    ctk.CTkButton(btns, text="Save", width=110, height=32, font=_font(14, "bold"),
                  fg_color=ui.PALETTE["start"], hover_color="#25803f", command=_save).pack(
        side=tk.RIGHT, padx=(0, 6))
    status = ctk.CTkLabel(btns, text="", text_color=ui.PALETTE["accent"], font=_font(12))
    status.pack(side=tk.RIGHT, padx=10)

    _refresh_angle_live()
    _refresh_run_live()
    _refresh_preview()
    win.after(150, win.lift)
