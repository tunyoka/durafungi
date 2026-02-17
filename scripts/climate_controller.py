#!/usr/bin/env python3
"""
DuraFungi Climate Controller v3.2
Polls Shelly sensors, logs to InfluxDB.
Monitors: climate (temp/humidity), humidifier, heat pump, circulation pump
"""

import os
import requests
import time
import logging
import sys
from datetime import datetime, timezone
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# --- PHASE 2.1 PRODUCTION TARGETS ---
INFLUXDB_URL = "http://100.113.86.123:8086"
INFLUXDB_ORG = "DuraFungi"
INFLUXDB_BUCKET = "climate_raw"

def _read_token():
    try:
        with open("/etc/durafungi/influx_token", "r") as f:
            return f.read().strip()
    except:
        return os.environ.get("INFLUXDB_TOKEN")

INFLUXDB_TOKEN = _read_token()

if not INFLUXDB_TOKEN:
    raise SystemExit("No InfluxDB token available. Provide it via /etc/durafungi/influx_token or INFLUXDB_TOKEN env var.")

# =============================================================================
# CONFIGURATION
# =============================================================================
POLL_INTERVAL = 30
REQUEST_TIMEOUT = 5
ERROR_RETRY_DELAY = 5

DEVICES = {
    "gr1": {
        "name": "Grow Room 1",
        "sensor_ip": "192.168.2.245",
        "sensor_type": "st802",
        "controls_ip": "192.168.2.199",
        "humidifier_channel": 1,
        "heat_pump_channel": 0,
        "pump_ip": "192.168.2.112",
    },
    "gr2": {
        "name": "Grow Room 2",
        "sensor_ip": "192.168.2.200",
        "sensor_type": "ht_g3",
        "controls_ip": None,
    },
}

# =============================================================================
# SENSOR READERS
# =============================================================================

def read_st802_sensor(session, ip):
    """Read temp/humidity from ST-802 thermostat (HVAC number components)."""
    result = {"success": False, "temp": None, "humidity": None, "error": None}
    try:
        hum_resp = session.get(f"http://{ip}/rpc/Number.GetStatus?id=202", timeout=REQUEST_TIMEOUT)
        hum_resp.raise_for_status()
        raw_h = hum_resp.json()
        result["humidity"] = raw_h.get("value")

        temp_resp = session.get(f"http://{ip}/rpc/Number.GetStatus?id=203", timeout=REQUEST_TIMEOUT)
        temp_resp.raise_for_status()
        raw_t = temp_resp.json()
        result["temp"] = raw_t.get("value")

        if result["temp"] is not None and result["humidity"] is not None:
            result["success"] = True
        else:
            result["error"] = "Missing temp/humidity values in response"
            logging.debug(f"ST-802 raw humidity json: {raw_h}")
            logging.debug(f"ST-802 raw temp json: {raw_t}")
    except requests.exceptions.Timeout:
        result["error"] = "Timeout"
    except requests.exceptions.ConnectionError:
        result["error"] = "Connection failed"
    except Exception as e:
        result["error"] = str(e)
    return result


def read_ht_g3_sensor(session, ip):
    """Read temp/humidity from H&T G3 sensor."""
    result = {"success": False, "temp": None, "humidity": None, "error": None}
    try:
        temp_resp = session.get(f"http://{ip}/rpc/Temperature.GetStatus", timeout=REQUEST_TIMEOUT)
        temp_resp.raise_for_status()
        raw_t = temp_resp.json()
        result["temp"] = raw_t.get("tC")

        hum_resp = session.get(f"http://{ip}/rpc/Humidity.GetStatus", timeout=REQUEST_TIMEOUT)
        hum_resp.raise_for_status()
        raw_h = hum_resp.json()
        result["humidity"] = raw_h.get("rh")

        if result["temp"] is not None and result["humidity"] is not None:
            result["success"] = True
        else:
            result["error"] = "Missing temp/humidity values in response"
            logging.debug(f"H&T G3 raw temp json: {raw_t}")
            logging.debug(f"H&T G3 raw humidity json: {raw_h}")
    except requests.exceptions.Timeout:
        result["error"] = "Timeout"
    except requests.exceptions.ConnectionError:
        result["error"] = "Connection failed"
    except Exception as e:
        result["error"] = str(e)
    return result


def get_switch_status(session, ip, channel):
    """Read switch status from any Shelly Gen2/Gen3 device."""
    try:
        resp = session.get(f"http://{ip}/rpc/Switch.GetStatus?id={channel}", timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return {
            "success": True,
            "is_on": data.get("output", False),
            "power": data.get("apower", 0),
            "voltage": data.get("voltage", 0),
            "current": data.get("current", 0),
        }
    except requests.exceptions.Timeout:
        return {"success": False, "power": 0, "error": f"Timeout ({ip}:{channel})"}
    except requests.exceptions.ConnectionError:
        return {"success": False, "power": 0, "error": f"Connection failed ({ip}:{channel})"}
    except requests.exceptions.HTTPError as e:
        return {"success": False, "power": 0, "error": f"HTTP error ({ip}:{channel}): {e}"}
    except Exception as e:
        return {"success": False, "power": 0, "error": f"({ip}:{channel}): {e}"}


def write_point(write_api, point):
    """Write a single point to InfluxDB with error handling."""
    try:
        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
    except Exception as e:
        logging.error(f"InfluxDB write failed: {e}")


# =============================================================================
# MAIN LOOP
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)


def main():
    logging.info("DuraFungi Climate Controller v3.2 starting (Phase 2.1)")
    logging.info(f"Target: {INFLUXDB_URL} | Bucket: {INFLUXDB_BUCKET}")
    logging.info(f"Polling interval: {POLL_INTERVAL}s, request timeout: {REQUEST_TIMEOUT}s")

    if not INFLUXDB_TOKEN:
        logging.critical("No InfluxDB token available - cannot continue")
        sys.exit(1)

    session = requests.Session()
    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    while True:
        loop_start = time.monotonic()
        try:
            for room_id, config in DEVICES.items():
                # Initialize to prevent UnboundLocalError
                hp_status = {"success": False, "power": 0}
                hum_status = {"success": False, "power": 0}
                pump_status = {"success": False, "power": 0}
                now = datetime.now(timezone.utc)
                room_name = config["name"]

                if "sensor_ip" not in config:
                    logging.warning(f"{room_name}: missing sensor_ip, skipping")
                    continue

                # --- Climate sensor ---
                sensor_type = config.get("sensor_type")
                if sensor_type == "st802":
                    reading = read_st802_sensor(session, config["sensor_ip"])
                elif sensor_type == "ht_g3":
                    reading = read_ht_g3_sensor(session, config["sensor_ip"])
                else:
                    reading = {"success": False, "temp": None, "humidity": None,
                               "error": f"Unknown sensor_type: {sensor_type}"}

                if reading["success"]:
                    point = Point("climate") \
                        .tag("room_id", room_id) \
                        .tag("room_name", room_name) \
                        .tag("sensor_ip", config["sensor_ip"]) \
                        .field("temperature", float(reading["temp"])) \
                        .field("humidity", float(reading["humidity"])) \
                        .time(now)
                    write_point(write_api, point)
                else:
                    logging.warning(f"{room_name}: Sensor offline - {reading['error']}")

                # --- Device status (Pro 2PM channels) ---
                if config.get("controls_ip"):
                    controls_ip = config["controls_ip"]

                    # Humidifier
                    hum_status = get_switch_status(session, controls_ip,
                                                   config.get("humidifier_channel", 1))
                    if hum_status["success"]:
                        write_point(write_api, Point("device_status")
                            .tag("room_id", room_id)
                            .tag("room_name", room_name)
                            .tag("device", "humidifier")
                            .tag("ip", controls_ip)
                            .field("is_on", hum_status["is_on"])
                            .field("power_watts", float(hum_status["power"]))
                            .time(now))

                    # Heat pump
                    hp_status = get_switch_status(session, controls_ip,
                                                  config.get("heat_pump_channel", 0))
                    if hp_status["success"]:
                        write_point(write_api, Point("device_status")
                            .tag("room_id", room_id)
                            .tag("room_name", room_name)
                            .tag("device", "heat_pump")
                            .tag("ip", controls_ip)
                            .field("is_on", hp_status["is_on"])
                            .field("power_watts", float(hp_status["power"]))
                            .time(now))

                # --- Circulation pump (Shelly 1PM Mini Gen3) ---
                if config.get("pump_ip"):
                    pump_ip = config["pump_ip"]
                    pump_status = get_switch_status(session, pump_ip, 0)
                    if pump_status["success"]:
                        write_point(write_api, Point("device_status")
                            .tag("room_id", room_id)
                            .tag("room_name", room_name)
                            .tag("device", "pump")
                            .tag("ip", pump_ip)
                            .field("is_on", pump_status["is_on"])
                            .field("power_watts", float(pump_status["power"]))
                            .time(now))

                # --- Summary log ---
                if reading["success"]:
                    parts = [f"{reading['temp']:.1f}°C, {reading['humidity']:.1f}%"]
                    if hp_status.get("success"):
                        parts.append(f"HP:{hp_status['power']:.0f}W")
                    if hum_status.get("success"):
                        parts.append(f"UH:{hum_status['power']:.0f}W")
                    if pump_status.get("success"):
                        parts.append(f"Pump:{pump_status['power']:.0f}W")
                    logging.info(f"{room_name}: {' | '.join(parts)}")

            # Compensate for loop execution time
            elapsed = time.monotonic() - loop_start
            sleep_time = max(0, POLL_INTERVAL - elapsed)
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            logging.info("Shutting down...")
            client.close()
            session.close()
            break
        except Exception as e:
            logging.error(f"Loop error: {e}")
            time.sleep(ERROR_RETRY_DELAY)


if __name__ == "__main__":
    main()
