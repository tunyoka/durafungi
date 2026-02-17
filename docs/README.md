# DuraFungi Production Scripts

## Overview
These are the production scripts retrieved from the Pi edge writer (durafungi-edge) on 2026-02-17.

## Scripts

| Script | Purpose | Target Bucket | Notes |
|--------|---------|---------------|-------|
| climate_controller.py | Shelly sensor polling (GR1) | climate_raw | Polls ST-802, Pro 2PM |
| jkbus_monitor.py | Heat pump JK BUS monitor | nergy_raw | v4 + cooling mode fix |
| 	uya_air_sensor.py | Tuya Air Housekeeper | climate_raw | Cloud API (not local) |
| office_sensor.py | ZPHS01B office sensor | climate_raw | UART3 serial |

## Service Files
Corresponding systemd service files are in /services/

## Configuration
- Tokens: /etc/durafungi/influx_energy.env and /etc/durafungi/influx_climate.env
- Tuya devices: config/devices.json (DO NOT COMMIT)
