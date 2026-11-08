# EasyPower 1.0.0

A simple power tray icon for Linux desktops (GTK3 + UPower + power-profiles-daemon). Replaces Cinnamon's power applet.

- **Tray:** battery level (with a bolt when plugged in). Scroll changes screen brightness. The tooltip gives status in plain words.
- **Panel:**
  - Battery %, what it's doing ("Plugged in · waits until below 75% to charge", "about 2 h 10 min left"), and battery health.
  - Power mode buttons (Power saver / Balanced / Performance), with a hint when Power saver is on while plugged in.
  - Screen brightness.
  - Bluetooth gadget batteries (keyboard, phone, headphones).
  - **Battery care**: stop charging at 80% on laptops with charge thresholds (ThinkPads etc.). It asks for your password.
- **Right-click:** power modes, power settings, quit.

Run: `python3 easypower.py` (`--hidden`, `--show`, `--quit`). Needs python3-gi, gir1.2-gtk-3.0, upower, power-profiles-daemon, optional gir1.2-xapp-1.0.
