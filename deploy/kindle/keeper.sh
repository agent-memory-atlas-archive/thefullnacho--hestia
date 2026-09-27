#!/bin/sh
# Hestia board keeper. Run by the Kindle's own cron every minute.
# Keeps the two things the board's SSH path needs: the firewall hole USBNetwork adds at
# runtime (a network restart reloads the stock rules without it) and the dropbear daemon.
export PATH="$PATH:/sbin:/usr/sbin"
LOG=/mnt/us/hestia/keeper.log

if ! iptables -L INPUT -n | grep -q "dpt:22"; then
    iptables -A INPUT -i wlan0 -p tcp --dport 22 -j ACCEPT
    echo "$(date '+%F %T') re-added wlan0 ssh rule" >> "$LOG"
fi
if ! netstat -ltn | grep -q ":22 "; then
    rm -f /mnt/us/usbnet/run/sshd.pid
    /usr/bin/dropbear -P /mnt/us/usbnet/run/sshd.pid -K 15
    echo "$(date '+%F %T') restarted dropbear" >> "$LOG"
fi
# keep the log from growing without bound
[ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 500 ] && tail -n 200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
exit 0
