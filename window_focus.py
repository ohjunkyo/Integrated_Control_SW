"""Bring another Integrated Control app's window to the front (X11).

DAQ (Tk) and HV Monitor (Qt) run as separate processes, so neither can raise
the other directly; this asks the window manager via _NET_ACTIVE_WINDOW.
"""
import subprocess

HV_TITLE = "Real-time Monitoring System"
DAQ_TITLE = "DAQ/LASER/UPS Control Panel"
HV_PROC = "HV_Control_SW/monitoring_app.py"
DAQ_PROC = "DAQ_Control_SW/main"


def focus_window(title_prefix):
    """Return 'raised', 'no-window', or 'error: <reason>'."""
    try:
        from Xlib import display, X
        from Xlib.protocol import event
    except ImportError as e:
        return f"error: {e}"
    d = None
    try:
        d = display.Display()
        root = d.screen().root
        clients = root.get_full_property(d.intern_atom('_NET_CLIENT_LIST'), X.AnyPropertyType)
        net_name, utf8 = d.intern_atom('_NET_WM_NAME'), d.intern_atom('UTF8_STRING')
        active = d.intern_atom('_NET_ACTIVE_WINDOW')
        for wid in (clients.value if clients else []):
            w = d.create_resource_object('window', wid)
            prop = w.get_full_property(net_name, utf8)
            name = prop.value.decode('utf-8', 'replace') if prop else (w.get_wm_name() or '')
            if name.startswith(title_prefix):
                # Source indication 2 (pager): honoured by Mutter, and it un-minimises.
                ev = event.ClientMessage(window=w, client_type=active,
                                         data=(32, [2, X.CurrentTime, 0, 0, 0]))
                root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
                d.flush()
                return 'raised'
        return 'no-window'
    except Exception as e:
        return f"error: {e}"
    finally:
        if d is not None:
            try:
                d.close()
            except Exception:
                pass


def is_running(proc_pattern):
    try:
        r = subprocess.run(['pgrep', '-f', proc_pattern], capture_output=True, text=True, timeout=3)
        return bool(r.stdout.strip())
    except Exception:
        return False
