# Raspberry Pi Zero 2W — Dual WiFi Failover

Automatic WiFi failover on a Raspberry Pi Zero 2W using two interfaces: onboard WiFi (`wlan0`) and a USB RTL8188FU adapter (`wlan1`). No bonding daemons, no extra software — just Linux kernel routing metrics.

<img width="380" height="492" alt="image" src="https://github.com/user-attachments/assets/7911a609-6ead-4045-a956-db23583ff348" />

## Hardware

- Raspberry Pi Zero 2W
- USB WiFi adapter: **Realtek RTL8188FU** (RTL8188FTV 802.11b/g/n)
  - Chipset: `0bda:f179`
  - Firmware: `rtlwifi/rtl8188fufw.bin` (included in Pi OS kernel)
  - Requires micro-USB OTG adapter to connect to Pi Zero
  - A good 5V 2.5 A power adapter is suggested

## How It Works

Both `wlan0` and `wlan1` connect to the same access point independently. The kernel maintains two default routes with different metrics:

```
default via 192.168.0.1 dev wlan0 metric 600    primary
default via 192.168.0.1 dev wlan1 metric 601    failover
```

When `wlan0` goes down, the kernel automatically removes its route and traffic flows through `wlan1`. When `wlan0` comes back, it reasserts the lower-metric route and becomes primary again. No daemons, no scripts required.

## Setup

### 1. Flash Pi OS

Flash **Raspberry Pi OS Lite (64-bit)** using Raspberry Pi Imager (Debian GNU/Linux 13 (trixie) used here).                                                              

In the OS Customisation screen, configure:

- Hostname
- Username / password
- WiFi SSID and password
- Enable SSH
- Set country code (required for WiFi)

### 2. Connect USB WiFi Adapter

Plug the RTL8188FU adapter into the Pi Zero OTG port via a micro-USB OTG adapter. On boot, the kernel loads the driver automatically:

```
usb 1-1: RTL8188FU rev B (SMIC) romver 0, 1T1R
usb 1-1: rtl8xxxu: Loading firmware rtlwifi/rtl8188fufw.bin
usb 1-1: Firmware revision 4.0 (signature 0x88f1)
```

Verify both interfaces are present:

```bash
ifconfig
# wlan0 = onboard (connected, has IP)
# wlan1 = USB adapter (up, no IP yet)
```

### 3. Install Bonding Support

```bash
sudo apt install ifenslave
```

### 4. Create the Bond (Optional — for active-backup bonding)

If you want full kernel-level bonding instead of metric-based failover:

```bash
# Create the bond interface
nmcli con add type bond con-name bond0 ifname bond0 \
  bond.options "mode=active-backup,miimon=100"

# Add wlan0 as primary slave
nmcli con add type wifi con-name bond-wlan0 ifname wlan0 \
  master bond0 ssid "YourSSID" \
  -- wifi-sec.key-mgmt wpa-psk wifi-sec.psk "YourPassword"

# Add wlan1 as secondary slave
nmcli con add type wifi con-name bond-wlan1 ifname wlan1 \
  master bond0 ssid "YourSSID" \
  -- wifi-sec.key-mgmt wpa-psk wifi-sec.psk "YourPassword"

# Bring the bond up
nmcli con up bond0
```

> **Note:** Metric-based failover (see below) is simpler and works just as well for most use cases. Bonding adds complexity and some APs may behave unexpectedly with it.

### 5. Connect wlan1 to the Network

```bash
nmcli dev wifi connect "YourSSID" password "YourPassword" ifname wlan1
```

Verify both interfaces have IPs:

```bash
ifconfig wlan0
ifconfig wlan1
```

### 4. Verify Routing

```bash
ip route show
```

Expected output:

```
default via 192.168.0.1 dev wlan0 proto dhcp src 192.168.0.x metric 600
default via 192.168.0.1 dev wlan1 proto dhcp src 192.168.0.y metric 601
192.168.0.0/24 dev wlan0 proto kernel scope link src 192.168.0.x metric 600
192.168.0.0/24 dev wlan1 proto kernel scope link src 192.168.0.y metric 601
```

`wlan0` wins as primary due to lower metric (600 < 601).

## Testing Failover

Run a continuous ping from a remote host first:
```bash
# From your desktop
ping 192.168.0.x   # Pi's wlan0 IP
```

Then on the Pi, simulate failures:

```bash
# Kill primary — traffic shifts to wlan1 instantly
sudo ip link set wlan0 down
ip route show
# default via 192.168.0.1 dev wlan1 metric 601  ← wlan1 now primary

# Restore wlan0 — traffic shifts back
sudo ip link set wlan0 up
ip route show
# default via 192.168.0.1 dev wlan0 metric 600  ← wlan0 primary again
# default via 192.168.0.1 dev wlan1 metric 601  ← wlan1 back to standby

# Test wlan1 failure too
sudo ip link set wlan1 down
ip route show
# default via 192.168.0.1 dev wlan0 metric 600  ← wlan0 unaffected
sudo ip link set wlan1 up
```

Pings from the remote host continue uninterrupted throughout all of the above.

## Interface Naming

On the Pi Zero 2W, both interfaces use simple `wlan0`/`wlan1` names because Pi OS disables predictable interface naming by default.

On a standard Ubuntu desktop, the USB adapter would get a MAC-based name instead:
```
wlx6c60ebce2827   ←  "wl" + "x" (USB) + MAC address with colons removed
```

If you want simple names on Ubuntu too, add to `/boot/cmdline.txt`:
```
net.ifnames=0 biosdevname=0
```

## Viewing the Full USB Device Tree

```bash
usb-devices
# or
lsusb -t
```

Useful for confirming the adapter is detected and identifying its exact chipset/firmware.

## Notes

- Both interfaces connect to the AP independently with different MAC addresses — the AP sees two normal clients
- Metrics are assigned automatically by NetworkManager via DHCP; the 1-point difference (600 vs 601) is sufficient for failover
- This approach works for any two WiFi interfaces on the same SSID
- For the Pi Zero 1 (original), use **Raspberry Pi OS Lite 32-bit** — the 64-bit image will not boot (7 LED blinks = kernel not found)

## Tested Environment

- **Board:** Raspberry Pi Zero 2W
- **OS:** Raspberry Pi OS Lite 64-bit (Debian Trixie, April 2026)
- **Kernel:** Linux 6.x
- **USB adapter:** Realtek RTL8188FU (`0bda:f179`)
- **Network manager:** NetworkManager + netplan

