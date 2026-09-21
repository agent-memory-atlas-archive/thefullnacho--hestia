# ESPHome devices

Small boards that feed Home Assistant over its native API. Each one reads something local and
does its own decoding, so a board keeps working on its own screen when HA is down.

## kennel-box

A Heltec WiFi LoRa 32 V3 placed next to the whelping box. It listens for the Govee H5075 in
the box over Bluetooth and publishes box temperature, humidity and the sensor's battery to
HA. Its OLED shows the box in °F with humidity and battery underneath.

The H5075 broadcasts its readings unencrypted, so nothing here touches the Govee app or cloud.
The decode is the one HA's own `govee-ble` parser uses, checked against a live packet.

When the sensor goes quiet for 10 minutes, the HA entities go to `unknown` and the screen
switches to "No reading for N min". A dead coin cell cannot leave a stale number looking
current.

There are no temperature thresholds on the board. Safe box temperatures change week by week
after whelping, so they live in `brain/box_watch.py`, which reads the litter's age from
records and alerts from there. Changing a band never needs a reflash.

### Flashing

```bash
cp secrets.yaml.example secrets.yaml      # then fill it in (2.4 GHz Wi-Fi only)
cd deploy/esphome
uvx --from esphome==2025.12.7 esphome run kennel-box.yaml --device /dev/ttyUSB0   # first flash, over USB
uvx --from esphome==2025.12.7 esphome run kennel-box.yaml                         # later flashes, over Wi-Fi
```

Find the sensor's MAC with `bluetoothctl --timeout 20 scan on | grep GVH5075`.

ESPHome is pinned to 2025.12.7 to match Home Assistant 2025.12. ESPHome 2026.x stopped sending
the `object_id` that HA 2025.12 builds entity unique IDs from, so every sensor arrives with the
same ID and HA keeps only the first. Move the pin when HA is upgraded.

### Adding to HA

Settings, Devices and services, Add integration, ESPHome. Enter the board's IP and the
`kennel_api_key` from `secrets.yaml`. Set a DHCP reservation for the board so the address holds.
