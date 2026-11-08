#!/usr/bin/env python3
"""EasyPower: a simple power tray icon (GTK3 + UPower + power-profiles-daemon).

Tray: battery level (scroll = screen brightness). Panel: battery status in plain words, health,
power mode (Power saver / Balanced / Performance), screen brightness, Bluetooth gadget batteries,
and Battery care (stop charging at 80% on laptops that support charge thresholds).
"""
import os, shutil, subprocess, sys, time
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, Gio, GLib, Pango
try:
    gi.require_version("XApp", "1.0")
    from gi.repository import XApp
except (ValueError, ImportError):
    XApp = None

VERSION = "1.1.0"
APP_ID = "dev.tacticdev.easypower"

# --- shared by the TACTICDEV Easy* tray apps -------------------------------------------------
# Only one tray panel open at a time: opening one tells the others (over the session bus) to close.
TRAY_IFACE, TRAY_PATH = "dev.tacticdev.Tray", "/dev/tacticdev/tray"

def tray_announce():
    try:
        Gio.bus_get_sync(Gio.BusType.SESSION, None).emit_signal(
            None, TRAY_PATH, TRAY_IFACE, "Opened", GLib.Variant("(s)", (APP_ID,)))
    except GLib.Error:
        pass

def tray_listen(hide):
    Gio.bus_get_sync(Gio.BusType.SESSION, None).signal_subscribe(
        None, TRAY_IFACE, "Opened", TRAY_PATH, None, Gio.DBusSignalFlags.NONE,
        lambda c, s, p, i, n, params: params.unpack()[0] != APP_ID and hide())

def add_restart_action(app):
    """Self-restart that stays inside the desktop session (system permissions depend on it).
    Trigger: gdbus call --session -d <APP_ID> -o /<APP_ID with dots as slashes>
             -m org.freedesktop.Application.ActivateAction restart [] {}"""
    act = Gio.SimpleAction.new("restart", None)
    def restart(*_):
        script = (f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.2; done; "
                  f"exec {sys.executable} {os.path.abspath(__file__)} --hidden")
        subprocess.Popen(["sh", "-c", script])
        app.quit()
    act.connect("activate", restart)
    app.add_action(act)
# ---------------------------------------------------------------------------------------------
UP = "org.freedesktop.UPower"
PP_NAMES = (("org.freedesktop.UPower.PowerProfiles", "/org/freedesktop/UPower/PowerProfiles"),
            ("net.hadess.PowerProfiles", "/net/hadess/PowerProfiles"))
SCREEN = ("org.cinnamon.SettingsDaemon.Power", "/org/cinnamon/SettingsDaemon/Power",
          "org.cinnamon.SettingsDaemon.Power.Screen")
CARE_END, CARE_START = 80, 75          # Battery care window
NORMAL_END = 100
PROFILES = (("power-saver", "Power saver", "power-profile-power-saver-symbolic"),
            ("balanced", "Balanced", "power-profile-balanced-symbolic"),
            ("performance", "Performance", "power-profile-performance-symbolic"))
STATE = {1: "charging", 2: "discharging", 3: "empty", 4: "full", 5: "pending-charge", 6: "pending-discharge"}
KIND_ICON = {2: "battery-symbolic", 5: "input-mouse-symbolic", 6: "input-keyboard-symbolic",
             8: "phone-symbolic", 17: "audio-headphones-symbolic", 18: "audio-speakers-symbolic",
             19: "audio-headset-symbolic", 13: "input-gaming-symbolic", 26: "input-keyboard-symbolic"}

CSS = b"""
window.easypower { background-color: transparent; }
.ep-card { background-color: #2c2c2c; color: #ffffff; border: 1px solid rgba(255,255,255,0.08);
           border-radius: 18px; padding: 14px 14px 10px; }
.ep-big { font-size: 20pt; font-weight: bold; }
.ep-title { font-weight: bold; }
.ep-dim { color: rgba(255,255,255,0.55); font-size: 9pt; }
.ep-warn { color: #e3b341; }
.ep-section { font-weight: bold; font-size: 9pt; color: rgba(255,255,255,0.6); margin-top: 6px; }
.ep-status { background-color: rgba(255,255,255,0.05); border-radius: 14px; padding: 10px 12px; }
.ep-mode { padding: 6px 4px; border-radius: 10px; background-color: #373737; }
.ep-mode:hover { background-color: #424242; }
.ep-mode.active { background-color: #4b90ed; }
.ep-row { padding: 4px 10px; }
.ep-link { padding: 6px 10px; border-radius: 10px; }
"""


def fmt_time(secs):
    if not secs or secs <= 0: return ""
    h, m = divmod(int(secs) // 60, 60)
    return f"{h} h {m} min" if h else f"{m} min"

def battery_icon(pct, charging):
    lvl = min(100, max(0, int(round(pct / 10.0)) * 10))
    return f"battery-level-{lvl}{'-charging' if charging else ''}-symbolic" if lvl < 100 or not charging \
        else "battery-level-100-charged-symbolic"


class Power:
    """UPower devices + power profiles + screen brightness, with change callbacks."""
    def __init__(self, on_change):
        self.on_change = on_change
        self.sys = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.ses = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._pending = None
        self.devices = {}   # path -> props
        self._load()
        self.sys.signal_subscribe(UP, UP, None, "/org/freedesktop/UPower", None, Gio.DBusSignalFlags.NONE,
                                  lambda *_: (self._load(), self.changed()))
        self.sys.signal_subscribe(UP, "org.freedesktop.DBus.Properties", "PropertiesChanged", None, None,
                                  Gio.DBusSignalFlags.NONE, self._props)
        self.pp = None
        for name, path in PP_NAMES:
            try:
                self.sys.call_sync(name, path, "org.freedesktop.DBus.Properties", "Get",
                                   GLib.Variant("(ss)", (name, "ActiveProfile")), None, 0, 1500, None)
                self.pp = (name, path)
                break
            except GLib.Error:
                continue
        if self.pp:
            self.sys.signal_subscribe(self.pp[0], "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                      self.pp[1], None, Gio.DBusSignalFlags.NONE, lambda *_: self.changed())
        self.ses.signal_subscribe(SCREEN[0], "org.freedesktop.DBus.Properties", "PropertiesChanged", SCREEN[1],
                                  None, Gio.DBusSignalFlags.NONE, lambda *_: self.changed())

    def _getall(self, path, iface="org.freedesktop.UPower.Device"):
        try:
            return self.sys.call_sync(UP, path, "org.freedesktop.DBus.Properties", "GetAll",
                                      GLib.Variant("(s)", (iface,)), None, 0, 2000, None).unpack()[0]
        except GLib.Error:
            return {}

    def _load(self):
        try:
            paths = self.sys.call_sync(UP, "/org/freedesktop/UPower", UP, "EnumerateDevices", None, None, 0,
                                       2000, None).unpack()[0]
        except GLib.Error:
            paths = []
        self.devices = {p: self._getall(p) for p in paths}

    def _props(self, conn, sender, path, iface, signal, params):
        ifname, changed, _inv = params.unpack()
        if ifname == "org.freedesktop.UPower.Device" and path in self.devices:
            self.devices[path].update(changed); self.changed()

    def changed(self):
        if self._pending: GLib.source_remove(self._pending)
        self._pending = GLib.timeout_add(150, self._fire)

    def _fire(self):
        self._pending = None; self.on_change(); return False

    # ---- queries ----
    def battery(self):
        for p, d in self.devices.items():
            if d.get("Type") == 2 and d.get("PowerSupply") and d.get("IsPresent"):
                return p, d
        return None, None

    def on_ac(self):
        return any(d.get("Type") == 1 and d.get("Online") for d in self.devices.values())

    def gadgets(self):
        out = []
        for p, d in self.devices.items():
            if d.get("PowerSupply") or d.get("Type") in (0, 1) or "DisplayDevice" in p: continue
            if not d.get("IsPresent", True) and not d.get("Percentage"): continue
            out.append(d)
        return sorted(out, key=lambda d: (d.get("Model") or "").lower())

    def profile(self):
        if not self.pp: return None, []
        try:
            r = self.sys.call_sync(self.pp[0], self.pp[1], "org.freedesktop.DBus.Properties", "GetAll",
                                   GLib.Variant("(s)", (self.pp[0],)), None, 0, 1500, None).unpack()[0]
        except GLib.Error:
            return None, []
        names = [p.get("Profile") for p in r.get("Profiles", [])]
        return r.get("ActiveProfile"), names, r.get("PerformanceDegraded", "")

    def set_profile(self, name, cb):
        if not self.pp: return
        self.sys.call(self.pp[0], self.pp[1], "org.freedesktop.DBus.Properties", "Set",
                      GLib.Variant("(ssv)", (self.pp[0], "ActiveProfile", GLib.Variant("s", name))),
                      None, 0, 3000, None, lambda c, res: self._done(c, res, cb))

    def _done(self, c, res, cb):
        try:
            c.call_finish(res); cb and cb(None)
        except GLib.Error as e:
            cb and cb(e.message)

    def brightness(self):
        try:
            return self.ses.call_sync(SCREEN[0], SCREEN[1], SCREEN[2], "GetPercentage", None, None, 0, 1500,
                                      None).unpack()[0]
        except GLib.Error:
            return None

    def set_brightness(self, pct):
        self.ses.call(SCREEN[0], SCREEN[1], SCREEN[2], "SetPercentage",
                      GLib.Variant("(u)", (max(1, min(100, int(pct))),)), None, 0, 1500, None, None)

    def step_brightness(self, up):
        self.ses.call(SCREEN[0], SCREEN[1], SCREEN[2], "StepUp" if up else "StepDown", None, None, 0, 1500, None, None)

    # ---- battery care (charge thresholds; ThinkPads and many others) ----
    def threshold_paths(self, bat_dev):
        name = (bat_dev or {}).get("NativePath") or "BAT0"
        base = f"/sys/class/power_supply/{name}"
        s, e = f"{base}/charge_control_start_threshold", f"{base}/charge_control_end_threshold"
        return (s, e) if os.path.exists(e) else (None, None)

    def thresholds(self, bat_dev):
        s, e = self.threshold_paths(bat_dev)
        if not e: return None, None
        try:
            return int(open(s).read()) if s and os.path.exists(s) else None, int(open(e).read())
        except (OSError, ValueError):
            return None, None

    def set_care(self, bat_dev, on, cb):
        s, e = self.threshold_paths(bat_dev)
        if not e: return cb("This battery doesn't support charge limits.")
        start, end = (CARE_START, CARE_END) if on else (CARE_START, NORMAL_END)
        # Order matters: start must stay below end at every step.
        if on:
            script = f"echo {start} > {s}; echo {end} > {e}" if s else f"echo {end} > {e}"
        else:
            script = f"echo {end} > {e}; echo {start} > {s}" if s else f"echo {end} > {e}"
        def work():
            r = subprocess.run(["pkexec", "sh", "-c", script], capture_output=True, text=True)
            GLib.idle_add(cb, None if r.returncode == 0 else ("cancelled" if r.returncode in (126, 127) else r.stderr.strip()))
        import threading
        threading.Thread(target=work, daemon=True).start()


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.win = None
        self._updating = False
        self._bright_timer = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()
        css = Gtk.CssProvider(); css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.pw = Power(self.refresh)
        self.build()
        self.setup_tray()
        tray_listen(lambda: self.win.get_visible() and self.win.hide())
        add_restart_action(self)
        GLib.timeout_add_seconds(60, lambda: (self.refresh(), True)[-1])  # time-left estimates drift
        self.refresh()

    def do_command_line(self, cl):
        args = cl.get_arguments()[1:]
        if "--version" in args:
            cl.print_literal(f"EasyPower {VERSION}\n"); return 0
        if "--quit" in args:
            self.quit(); return 0
        if "--hidden" in args:
            return 0
        self.toggle(); return 0

    # ---- words ----
    def status(self):
        """(icon, big text, status line)"""
        _p, b = self.pw.battery()
        ac = self.pw.on_ac()
        if not b:
            return ("ac-adapter-symbolic", "Plugged in" if ac else "On power", "No battery")
        pct = b.get("Percentage", 0); st = STATE.get(b.get("State"), "")
        charging = st == "charging" or (ac and st in ("pending-charge", "full"))
        icon = battery_icon(pct, charging)
        if st == "charging":
            t = fmt_time(b.get("TimeToFull"))
            line = "Charging" + (f" · full in {t}" if t else "")
        elif st == "full":
            line = "Plugged in · fully charged"
        elif st == "pending-charge" and ac:
            start, end = self.pw.thresholds(b)
            if end and pct >= end - 1:
                line = f"Plugged in · charged to your {end}% limit"
            elif start:
                line = f"Plugged in · waits until below {start}% to charge"
            else:
                line = "Plugged in · not charging right now"
        elif st in ("discharging", "pending-discharge"):
            t = fmt_time(b.get("TimeToEmpty"))
            line = "On battery" + (f" · about {t} left" if t else "")
        else:
            line = "Plugged in" if ac else "On battery"
        return icon, f"{round(pct)}%", line

    # ---- tray ----
    def setup_tray(self):
        self.tray = None
        if XApp is None: return
        t = XApp.StatusIcon(); t.set_name("easypower")
        t.connect("activate", lambda *_: self.toggle())
        t.connect("scroll-event", lambda _i, _amt, direction, _t: self.pw.step_brightness(
            direction in (XApp.ScrollDirection.UP, XApp.ScrollDirection.RIGHT)))
        self.tray_menu = Gtk.Menu(); t.set_secondary_menu(self.tray_menu)
        self.tray = t

    def update_tray(self):
        if not self.tray: return
        icon, big, line = self.status()
        self.tray.set_icon_name(icon)
        prof = self.pw.profile()
        mode = dict((k, n) for k, n, _i in PROFILES).get(prof[0], "") if prof and prof[0] else ""
        self.tray.set_tooltip_text(f"{big} · {line}" + (f"\nMode: {mode}" if mode else "") + "\nScroll for brightness")
        m = self.tray_menu
        for c in m.get_children(): c.destroy()
        hd = Gtk.MenuItem(label=f"{big} · {line}"); hd.set_sensitive(False); m.append(hd)
        if prof and prof[0]:
            m.append(Gtk.SeparatorMenuItem())
            for key, name, _icon in PROFILES:
                if key in prof[1]:
                    it = Gtk.MenuItem(label=("✓ " if key == prof[0] else "    ") + name)
                    it.connect("activate", lambda _i, k=key: self.set_mode(k)); m.append(it)
        m.append(Gtk.SeparatorMenuItem())
        it = Gtk.MenuItem(label="Power settings…"); it.connect("activate", lambda *_: self.open_settings()); m.append(it)
        it = Gtk.MenuItem(label="Quit EasyPower"); it.connect("activate", lambda *_: self.quit()); m.append(it)
        m.show_all()

    # ---- panel ----
    def build(self):
        w = Gtk.Window(application=self, title="Power", decorated=False, resizable=False)
        w.get_style_context().add_class("easypower")
        vis = w.get_screen().get_rgba_visual()
        if vis: w.set_visual(vis)
        w.set_app_paintable(True)
        w.set_keep_above(True); w.set_skip_taskbar_hint(True); w.set_skip_pager_hint(True)
        w.set_type_hint(Gdk.WindowTypeHint.POPUP_MENU)
        w.connect("focus-out-event", lambda *_: (GLib.timeout_add(150, self._maybe_hide), False)[-1])
        w.connect("key-press-event", lambda _w, ev: (w.hide(), True)[-1] if ev.keyval == Gdk.KEY_Escape else False)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=8)
        card.get_style_context().add_class("ep-card"); card.set_size_request(360, -1)

        st = Gtk.Box(spacing=12); st.get_style_context().add_class("ep-status")
        self.b_icon = Gtk.Image(pixel_size=32)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        self.b_big = Gtk.Label(xalign=0); self.b_big.get_style_context().add_class("ep-big")
        self.b_line = Gtk.Label(xalign=0, wrap=True, max_width_chars=36)
        self.b_health = Gtk.Label(xalign=0, wrap=True, max_width_chars=40); self.b_health.get_style_context().add_class("ep-dim")
        for x in (self.b_big, self.b_line, self.b_health): col.pack_start(x, False, False, 0)
        st.pack_start(self.b_icon, False, False, 0); st.pack_start(col, True, True, 0)
        card.pack_start(st, False, False, 0)

        self.mode_head = Gtk.Label(label="Power mode", xalign=0); self.mode_head.get_style_context().add_class("ep-section")
        card.pack_start(self.mode_head, False, False, 0)
        self.mode_box = Gtk.Box(spacing=6, homogeneous=True)
        self.mode_btns = {}
        for key, name, icon in PROFILES:
            b = Gtk.Button(relief=Gtk.ReliefStyle.NONE); b.get_style_context().add_class("ep-mode")
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            img = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.LARGE_TOOLBAR)
            inner.pack_start(img, False, False, 0); inner.pack_start(Gtk.Label(label=name), False, False, 0)
            b.add(inner); b.connect("clicked", lambda _b, k=key: self.set_mode(k))
            self.mode_btns[key] = b; self.mode_box.pack_start(b, True, True, 0)
        card.pack_start(self.mode_box, False, False, 0)
        self.mode_note = Gtk.Label(xalign=0, wrap=True, max_width_chars=44); self.mode_note.get_style_context().add_class("ep-dim")
        card.pack_start(self.mode_note, False, False, 0)

        self.bright_head = Gtk.Label(label="Screen brightness", xalign=0); self.bright_head.get_style_context().add_class("ep-section")
        card.pack_start(self.bright_head, False, False, 0)
        self.bright_row = Gtk.Box(spacing=10)
        self.bright_row.pack_start(Gtk.Image.new_from_icon_name("display-brightness-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
        self.bright = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 100, 1)
        self.bright.set_draw_value(False); self.bright.set_hexpand(True)
        self.bright.connect("value-changed", self.on_bright)
        self.bright_row.pack_start(self.bright, True, True, 0)
        card.pack_start(self.bright_row, False, False, 0)

        self.gadget_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        card.pack_start(self.gadget_box, False, False, 0)

        self.care_box = Gtk.Box(spacing=10)
        cc = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        ct = Gtk.Label(label="Battery care", xalign=0); ct.get_style_context().add_class("ep-section")
        self.care_sub = Gtk.Label(xalign=0, wrap=True, max_width_chars=38); self.care_sub.get_style_context().add_class("ep-dim")
        cc.pack_start(ct, False, False, 0); cc.pack_start(self.care_sub, False, False, 0)
        self.care_sw = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.care_sw.connect("notify::active", self.on_care)
        self.care_box.pack_start(cc, True, True, 0); self.care_box.pack_end(self.care_sw, False, False, 0)
        self.care_box.set_no_show_all(True)
        card.pack_start(self.care_box, False, False, 0)

        foot = Gtk.Button(label="Power settings…", relief=Gtk.ReliefStyle.NONE, halign=Gtk.Align.START)
        foot.get_style_context().add_class("ep-link")
        foot.connect("clicked", lambda *_: (self.win.hide(), self.open_settings()))
        card.pack_start(foot, False, False, 0)
        self.toast = Gtk.Label(xalign=0, wrap=True, max_width_chars=44)
        card.pack_start(self.toast, False, False, 0)
        w.add(card)
        self.win = w

    def _maybe_hide(self):
        if self.win.get_visible() and not self.win.is_active() and not getattr(self, "_busy_dialog", False):
            self.win.hide()
        return False

    def toggle(self):
        if self.win.get_visible():
            self.win.hide(); return
        tray_announce()
        self.toast.set_text("")
        self.pw._load(); self.refresh()
        self.win.show_all()
        self.place_near_pointer()
        self.win.present_with_time(Gtk.get_current_event_time() or int(time.time()))

    def place_near_pointer(self):
        disp = Gdk.Display.get_default()
        _s, x, y = disp.get_default_seat().get_pointer().get_position()
        mon = disp.get_monitor_at_point(x, y).get_workarea()
        ww, wh = self.win.get_size()
        nx = min(max(mon.x + 8, x - ww // 2), mon.x + mon.width - ww - 8)
        ny = y - wh - 12 if y - wh - 12 > mon.y else y + 12
        self.win.move(nx, min(max(mon.y + 8, ny), mon.y + mon.height - wh - 8))

    def flash(self, msg):
        self.toast.set_markup(f"<span foreground='#e5534b'>{GLib.markup_escape_text(msg)}</span>")
        GLib.timeout_add_seconds(8, lambda: (self.toast.set_text(""), False)[-1])

    def refresh(self):
        if not self.win: return
        self._updating = True
        try:
            icon, big, line = self.status()
            self.b_icon.set_from_icon_name(icon, Gtk.IconSize.DND)
            self.b_big.set_text(big); self.b_line.set_text(line)
            _p, b = self.pw.battery()
            if b and b.get("EnergyFullDesign"):
                cap = b.get("Capacity") or 100 * b.get("EnergyFull", 0) / b["EnergyFullDesign"]
                txt = f"Battery health {round(cap)}% (holds {b.get('EnergyFull', 0):.0f} of {b['EnergyFullDesign']:.0f} Wh)"
                self.b_health.set_markup(f"<span foreground='#e3b341'>{txt}</span>" if cap < 60 else GLib.markup_escape_text(txt))
                self.b_health.show()
            else:
                self.b_health.hide()
            prof = self.pw.profile()
            if prof and prof[0]:
                active, names = prof[0], prof[1]
                for k, btn in self.mode_btns.items():
                    btn.set_visible(k in names)
                    ctx = btn.get_style_context()
                    (ctx.add_class if k == active else ctx.remove_class)("active")
                notes = []
                if len(prof) > 2 and prof[2]:
                    notes.append("Performance is limited right now (" + prof[2].replace("-", " ") + ").")
                if active == "power-saver" and self.pw.on_ac():
                    notes.append("You're plugged in, so Balanced costs no battery.")
                self.mode_note.set_text(" ".join(notes)); self.mode_note.set_visible(bool(notes))
                self.mode_head.show(); self.mode_box.show()
            else:
                self.mode_head.hide(); self.mode_box.hide(); self.mode_note.hide()
            br = self.pw.brightness()
            if br is not None:
                if not self.bright.has_grab(): self.bright.set_value(br)
                self.bright_head.show(); self.bright_row.show_all()
            else:
                self.bright_head.hide(); self.bright_row.hide()
            self.refresh_gadgets()
            start, end = self.pw.thresholds(b) if b else (None, None)
            if end is not None:
                on = end <= CARE_END
                self.care_sw.set_active(on)
                worn = b and b.get("Capacity") and b["Capacity"] < 80
                self.care_sub.set_text(
                    (f"On: charges to {end}% and stops. " if on else "Stop charging at 80%. ") +
                    ("Slows wear when you're plugged in most of the day." if not worn else
                     "Recommended: this battery is already worn, and it's plugged in most of the day.") +
                    " Asks for your password.")
                for c in self.care_box.get_children(): c.show_all()
                self.care_box.show()
            else:
                self.care_box.hide()
        finally:
            self._updating = False
        self.update_tray()

    def refresh_gadgets(self):
        for c in self.gadget_box.get_children(): c.destroy()
        gs = [g for g in self.pw.gadgets() if g.get("Percentage") is not None]
        if not gs: return
        h = Gtk.Label(label="Devices", xalign=0); h.get_style_context().add_class("ep-section")
        self.gadget_box.pack_start(h, False, False, 0)
        for g in gs:
            row = Gtk.Box(spacing=10); row.get_style_context().add_class("ep-row")
            row.pack_start(Gtk.Image.new_from_icon_name(KIND_ICON.get(g.get("Type"), "battery-symbolic"),
                                                        Gtk.IconSize.BUTTON), False, False, 0)
            row.pack_start(Gtk.Label(label=g.get("Model") or "Device", xalign=0, ellipsize=Pango.EllipsizeMode.END), True, True, 0)
            pct = round(g.get("Percentage", 0))
            lab = Gtk.Label(); lab.set_markup(f"<span foreground='#e5534b'>{pct}%</span>" if pct <= 15 else f"{pct}%")
            row.pack_start(Gtk.Image.new_from_icon_name(battery_icon(pct, g.get("State") == 1), Gtk.IconSize.MENU), False, False, 0)
            row.pack_start(lab, False, False, 0)
            self.gadget_box.pack_start(row, False, False, 0)
        self.gadget_box.show_all()

    # ---- actions ----
    def set_mode(self, key):
        def done(err):
            if err:
                self.flash("Couldn't change power mode: " + ("not allowed from here; reopen EasyPower from the menu."
                                                             if "authoriz" in err.lower() or "denied" in err.lower() else err))
            self.refresh()
        self.pw.set_profile(key, done)

    def on_bright(self, sc):
        if self._updating or self._bright_timer: return
        def apply():
            self._bright_timer = None
            self.pw.set_brightness(self.bright.get_value()); return False
        self._bright_timer = GLib.timeout_add(60, apply)

    def on_care(self, sw, _p):
        if self._updating: return
        _p2, b = self.pw.battery()
        self._busy_dialog = True   # the password prompt takes focus; don't close the panel for it
        def done(err):
            self._busy_dialog = False
            if err and err != "cancelled":
                self.flash("Battery care: " + err)
            self.refresh()
        self.pw.set_care(b, sw.get_active(), done)

    def open_settings(self):
        exe = shutil.which("cinnamon-settings")
        subprocess.Popen([exe, "power"] if exe else ["gnome-power-statistics"], start_new_session=True)


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
