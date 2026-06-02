#!/usr/bin/env python3

"""
oled_wifi_status.py

Displays on a 1.3" SSD1306 OLED (via I2C):
  - Date (weekday 2-letter, day, month name, year) and time
  - SSID (once, shared)
  - For each WiFi interface:
      - Interface name (shortened)
      - Signal bars icon (cell-phone style)
      - dBm value

Three GPIO LEDs indicate WPA2 auth status (checked every 5 minutes):
  - GREEN  : authenticated OK
  - YELLOW : re-authenticating (in progress)
  - RED    : authentication failed

Requirements:
  sudo pip3 install luma.oled RPi.GPIO --break-system-packages
  sudo apt-get install i2c-tools fonts-dejavu -y
  Enable I2C: raspi-config -> Interfacing Options -> I2C -> Enable

Usage:
  sudo python3 oled_wifi_status.py
"""

import subprocess
import time
import threading
from datetime import datetime

import RPi.GPIO as GPIO
from luma.core.interface.serial import i2c
from luma.oled.device import sh1106
from luma.core.render import canvas
from PIL import ImageFont

# ─── Config ───────────────────────────────────────────────────────────────────
I2C_PORT       = 1
I2C_ADDRESS    = 0x3C
FONT_PATH      = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_SIZE      = 9
DISPLAY_REFRESH_SEC = 5
AUTH_CHECK_SEC      = 300   # 5 minutes
INTERFACES     = ["wlan0", "wlan1"]
W              = 128
H              = 64

# ─── GPIO pins for LEDs (BCM numbering) ──────────────────────────────────────
GPIO_LED_GREEN  = 16
GPIO_LED_YELLOW = 12
GPIO_LED_RED    = 13
# ─────────────────────────────────────────────────────────────────────────────

# Shared auth state — written by auth thread, read by display thread
auth_state = "initing"   # "ok", "checking", "fail", "unknown", "initing"
auth_lock  = threading.Lock()


# ─── GPIO setup ──────────────────────────────────────────────────────────────

def gpio_init_sequence():
    for pin in [GPIO_LED_GREEN, GPIO_LED_YELLOW, GPIO_LED_RED]:
        for _ in range(2):
            GPIO.output(pin, GPIO.HIGH)
            time.sleep(0.4)
            GPIO.output(pin, GPIO.LOW)
            time.sleep(0.2)

def gpio_setup():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in [GPIO_LED_GREEN, GPIO_LED_YELLOW, GPIO_LED_RED]:
        GPIO.setup(pin, GPIO.OUT)
        GPIO.output(pin, GPIO.LOW)


def set_led(state):
    """
    state: "ok", "checking", "fail", "unknown"
    """
    GPIO.output(GPIO_LED_GREEN,  GPIO.LOW)
    GPIO.output(GPIO_LED_YELLOW, GPIO.LOW)
    GPIO.output(GPIO_LED_RED,    GPIO.LOW)

    if state == "ok":
        GPIO.output(GPIO_LED_GREEN, GPIO.HIGH)
    elif state == "checking":
        GPIO.output(GPIO_LED_YELLOW, GPIO.HIGH)
    elif state == "fail":
        GPIO.output(GPIO_LED_RED, GPIO.HIGH)
    # "unknown" = all off


# ─── Auth check ──────────────────────────────────────────────────────────────

def check_auth(iface):
    """
    Trigger reassociation on iface and check wpa_cli status.
    Returns True if authenticated, False otherwise.
    """
    try:
        # Trigger reassociate
        subprocess.run(
            ["wpa_cli", "-i", iface, "reassociate"],
            capture_output=True, timeout=10
        )
        # Give it a moment to associate
        time.sleep(8)
        # Check status
        result = subprocess.run(
            ["wpa_cli", "-i", iface, "status"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if "wpa_state=" in line:
                state = line.split("=")[1].strip()
                return state == "COMPLETED"
        return False
    except Exception as e:
        print(f"Auth check error on {iface}: {e}")
        return False


def blink_yellow(stop_event):
    while not stop_event.is_set():
        GPIO.output(GPIO_LED_YELLOW, GPIO.HIGH)
        time.sleep(0.3)
        GPIO.output(GPIO_LED_YELLOW, GPIO.LOW)
        time.sleep(0.3)


def auth_thread_func():
    """
    Runs every AUTH_CHECK_SEC seconds.
    Checks auth on all interfaces, sets LED accordingly.
    """
    global auth_state
    while True:
        with auth_lock:
            auth_state = "checking"

        # Blink yellow while testing
        stop_blink = threading.Event()
        blink_thread = threading.Thread(target=blink_yellow, args=(stop_blink,), daemon=True)
        blink_thread.start()

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking WPA2 auth...")

        results = []
        for iface in INTERFACES:
            ok = check_auth(iface)
            results.append(ok)
            print(f"  {iface}: {'OK' if ok else 'FAIL'}")

        if all(results):
            new_state = "ok"
        elif any(results):
            new_state = "ok"       # at least one interface up = resilient connection ok
        else:
            new_state = "fail"

        # Stop blinking yellow
        stop_blink.set()
        blink_thread.join()

        with auth_lock:
            auth_state = new_state
        set_led(new_state)
        print(f"  Auth state: {new_state}")

        time.sleep(AUTH_CHECK_SEC)


# ─── WiFi info ────────────────────────────────────────────────────────────────

def get_wifi_info(iface):
    try:
        result = subprocess.run(
            ["iw", "dev", iface, "link"],
            capture_output=True, text=True, timeout=3
        )
        output = result.stdout

        if "Not connected" in output or "Not associated" in output:
            return "--", None

        ssid = "--"
        dbm  = None

        for line in output.splitlines():
            line = line.strip()
            if line.startswith("SSID:"):
                ssid = line.split("SSID:")[1].strip()
            if "signal:" in line:
                try:
                    dbm = int(line.split("signal:")[1].strip().split()[0])
                except ValueError:
                    dbm = None

        return ssid, dbm

    except Exception:
        return "err", None


def get_short_iface(iface):
    if len(iface) > 10:
        return iface[:4] + ".." + iface[-4:]
    return iface


def dbm_to_bars(dbm):
    if dbm is None:
        return 0
    if dbm >= -55:
        return 4
    if dbm >= -65:
        return 3
    if dbm >= -75:
        return 2
    if dbm >= -85:
        return 1
    return 0


# ─── Display ─────────────────────────────────────────────────────────────────

def draw_signal_bars(draw, x, y, bars, bar_w=4, gap=2, max_bars=4):
    max_h = max_bars * 3
    for i in range(max_bars):
        bar_h = (i + 1) * 3
        bx = x + i * (bar_w + gap)
        by = y + max_h - bar_h
        if i < bars:
            draw.rectangle([bx, by, bx + bar_w, y + max_h], fill=255)
        else:
            draw.rectangle([bx, by, bx + bar_w, y + max_h], outline=255)


def auth_state_label():
    with auth_lock:
        state = auth_state
    return {
        "ok":       "Auth: OK",
        "checking": "Auth: testing",
        "fail":     "Auth: FAIL",
        "initing":  "Auth: initing..",
        "unknown":  "Auth: ?",
    }.get(state, "Auth: ?")


def draw_screen(device, font):
    dt       = datetime.now()
    weekday  = dt.strftime("%a")[:2]
    date_str = dt.strftime(f"{weekday} %d %B %Y")
    time_str = dt.strftime("%H:%M:%S")

    wifi_data    = []
    ssid_display = "--"
    for iface in INTERFACES:
        ssid, dbm = get_wifi_info(iface)
        short     = get_short_iface(iface)
        bars      = dbm_to_bars(dbm)
        dbm_str   = f"{dbm}" if dbm is not None else "--"
        wifi_data.append((short, dbm_str, bars))
        if ssid != "--" and ssid != "err" and ssid_display == "--":
            ssid_display = ssid

    if len(ssid_display) > 12:
        ssid_display = ssid_display[:11] + "~"

    with canvas(device) as draw:

        # ── Border ──
        draw.rectangle([(0, 0), (W - 1, H - 1)], outline=255)

        # ── Date + Time on one line ──
        draw.text((3, 2), f"{date_str}  {time_str}", font=font, fill=255)

        # ── Divider ──
        draw.line([(1, 13), (W - 2, 13)], fill=255)

        # ── SSID + Auth on one line ──
        draw.text((3, 15), f"{ssid_display}  {auth_state_label()}", font=font, fill=255)

        # ── Divider ──
        draw.line([(1, 26), (W - 2, 26)], fill=255)

        # ── WiFi entries ──
        y = 32
        for short_iface, dbm_str, bars in wifi_data:
            draw.text((3, y), short_iface, font=font, fill=255)
            draw_signal_bars(draw, x=55, y=y, bars=bars)
            draw.text((83, y), f"{dbm_str}dBm", font=font, fill=255)
            y += 14


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    global auth_state
    gpio_setup()

    serial = i2c(port=I2C_PORT, address=I2C_ADDRESS)
    device = sh1106(serial, rotate=0)
    font   = ImageFont.truetype(FONT_PATH, FONT_SIZE)

    # Show initing on screen then run LED init sequence
    draw_screen(device, font)
    gpio_init_sequence()
    # Blink all LEDs during initing period
    end_time = time.time() + 5
    while time.time() < end_time:
        for pin in [GPIO_LED_GREEN, GPIO_LED_YELLOW, GPIO_LED_RED]:
            GPIO.output(pin, GPIO.HIGH)
        time.sleep(0.3)
        for pin in [GPIO_LED_GREEN, GPIO_LED_YELLOW, GPIO_LED_RED]:
            GPIO.output(pin, GPIO.LOW)
        time.sleep(0.3)

    # Show initing state before auth thread starts
    with auth_lock:
        auth_state = "initing"

    # Start auth check thread
    t = threading.Thread(target=auth_thread_func, daemon=True)
    t.start()

    print(f"OLED started on I2C {hex(I2C_ADDRESS)}, refreshing every {DISPLAY_REFRESH_SEC}s")
    print(f"Auth check every {AUTH_CHECK_SEC}s on interfaces: {INTERFACES}")
    print(f"LEDs: GREEN=GPIO{GPIO_LED_GREEN} YELLOW=GPIO{GPIO_LED_YELLOW} RED=GPIO{GPIO_LED_RED}")

    while True:
        try:
            draw_screen(device, font)
        except Exception as e:
            print(f"Display error: {e}")
        time.sleep(DISPLAY_REFRESH_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        GPIO.cleanup()



