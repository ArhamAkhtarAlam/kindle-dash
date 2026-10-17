#!/bin/sh
# Find the Kindle's USB network interface and PRINT the nmcli command that gives the
# Pi a static 192.168.15.201/24 on it. Changes nothing itself.
# Run with the Kindle plugged in and USBNetwork enabled on it.
cd "$(dirname "$0")"
eval "$(venv/bin/python render.py --print-config)"   # KINDLE_IP, PI_USB_IP

echo "== Network interfaces (ip -br link) =="
ip -br link
echo
echo "== Recent USB network messages (dmesg) =="
MSGS=$(dmesg 2>/dev/null | grep -iE "cdc_ether|rndis|usb0|enx|g_ether|Kindle" | tail -n 10)
echo "${MSGS:-(nothing found, or dmesg needs root; try: sudo dmesg | grep -iE 'cdc_ether|rndis|enx|usb0')}"
echo

IFACE=$(ip -br link | awk '{print $1}' | grep -E '^(usb[0-9]+|enx[0-9a-f]{12})$' | head -n 1)
if [ -z "$IFACE" ]; then
    echo "No usb0/enx* interface found. Check the Kindle is plugged in and USBNetwork is ON,"
    echo "then run this again."
    exit 1
fi
echo "Kindle interface: $IFACE"
echo
echo "Run this to give the Pi $PI_USB_IP on $IFACE (no gateway, never the default route,"
echo "no IPv6, so your Ethernet/Wi-Fi internet is untouched):"
echo
echo "  sudo nmcli connection add type ethernet ifname $IFACE con-name kindle-usb \\"
echo "    ipv4.method manual ipv4.addresses $PI_USB_IP/24 ipv4.never-default yes \\"
echo "    ipv6.method disabled connection.autoconnect yes"
echo "  sudo nmcli connection up kindle-usb"
echo "  ping -c 3 $KINDLE_IP"
echo
case "$IFACE" in enx*)
    echo "Note: $IFACE is named after the Kindle's USB MAC address. If the name changes the next"
    echo "time you plug in, match by name pattern instead (all usb*/enx* interfaces):"
    echo "  sudo nmcli connection modify kindle-usb connection.interface-name '' match.interface-name 'usb* enx*'"
    ;;
esac
echo "To undo: sudo nmcli connection delete kindle-usb"
