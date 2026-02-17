#!/usr/bin/env python3
"""
DuraFungi Air Housekeeper -> Influx (Tuya CLOUD mode) - Production
=================================================================

Why CLOUD mode:
- Phone app reads via Tuya cloud reliably.
- This specific device/firmware does not reliably respond to local LAN polling.

Inputs:
- tinytuya wizard config:  /home/verts/durafungi-climate/tinytuya.json
- Influx token file:       /etc/durafungi/influx_writer_token

Writes:
- Influx bucket: climate_raw
- measurement:  climate_raw
- tags: room, sensor, device_id
- fields: co2_ppm, humidity_pct, temperature_c, pm25_ug_m3, voc_ppm, ch2o_ppm, co2_zone, raw_tuya_json

Authoritative mapping (from wizard):
- DP 22 = co2_value (ppm, scale 0)
- DP 19 = humidity_value (% , scale 1) => /10
- DP 18 = temp_current (°C, scale 1) => /10
- DP 2  = pm25_value (ug/m3, scale 0)
- DP 21 = voc_value (ppm, scale 3) => /1000
- DP 20 = ch2o_value (ppm, scale 3) => /1000
"""

import time
import json
import logging
import tinytuya
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# -----------------------
# LOGGING
# -----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("tuya_air_sensor_cloud")

# -----------------------
# PATHS / INFLUX
# -----------------------
TINYTUYA_JSON_PATH = "/home/verts/durafungi-climate/tinytuya.json"
TOKEN_PATH = "/etc/durafungi/influx_writer_token"

INFLUXDB_URL = "http://100.113.86.123:8086"
INFLUXDB_ORG = "DuraFungi"
INFLUXDB_BUCKET = "climate_raw"

ROOM_TAG = "grow_room_1"
SENSOR_TAG = "tuya_air_housekeeper"

CO2_MIN, CO2_MAX = 800, 1500

# Tuya "code" fields (cloud returns code/value pairs)
CODE_PM25 = "pm25_value"
CODE_TEMP = "temp_current"
CODE_HUM  = "humidity_value"
CODE_CH2O = "ch2o_value"
CODE_VOC  = "voc_value"
CODE_CO2  = "co2_value"


def load_token() -> str:
    with open(TOKEN_PATH, "r", encoding="utf-8") as f:
        token = f.read().strip()
    if not token:
        raise ValueError("Influx token file is empty")
    return token


def load_tuya_cloud_conf() -> dict:
    with open(TINYTUYA_JSON_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # Wizard keys: apiKey, apiSecret, apiRegion, apiDeviceID
    required = ["apiKey", "apiSecret", "apiRegion", "apiDeviceID"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise KeyError(f"Missing keys in {TINYTUYA_JSON_PATH}: {missing}")

    return cfg


def compute_zone(co2_ppm: int) -> int:
    if co2_ppm is None:
        return -1
    if CO2_MIN <= co2_ppm <= CO2_MAX:
        return 1
    if co2_ppm < CO2_MIN:
        return 0
    return 2


def safe_int(v):
    try:
        return int(v) if v is not None else None
    except Exception:
        return None


def safe_float(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def parse_cloud_status(payload: dict) -> dict:
    """
    Tries to extract code->value pairs from common TinyTuya cloud responses.

    Expected shape often:
      { 'success': True, 'result': [ {'code':'co2_value','value':573}, ... ] }

    Returns dict: code -> value
    """
    if not isinstance(payload, dict):
        return {}

    result = payload.get("result")
    if isinstance(result, list):
        out = {}
        for item in result:
            if isinstance(item, dict) and "code" in item and "value" in item:
                out[item["code"]] = item["value"]
        return out

    # Some variants nest deeper; try a couple fallbacks
    for key in ("status", "properties", "data"):
        v = payload.get(key)
        if isinstance(v, list):
            out = {}
            for item in v:
                if isinstance(item, dict) and "code" in item and "value" in item:
                    out[item["code"]] = item["value"]
            if out:
                return out

    return {}


def main():
    # Load configs
    tuya_cfg = load_tuya_cloud_conf()
    influx_token = load_token()

    device_id = tuya_cfg["apiDeviceID"]
    region = tuya_cfg["apiRegion"]

    logger.info(f"Starting Tuya CLOUD monitor for device_id={device_id} region={region}")

    # TinyTuya cloud client
    cloud = tinytuya.Cloud(
        apiRegion=tuya_cfg["apiRegion"],
        apiKey=tuya_cfg["apiKey"],
        apiSecret=tuya_cfg["apiSecret"],
        apiDeviceID=device_id
    )

    # Influx client
    influx = InfluxDBClient(url=INFLUXDB_URL, token=influx_token, org=INFLUXDB_ORG)
    write_api = influx.write_api(write_options=SYNCHRONOUS)

    poll_seconds = 30
    fail_sleep = 30

    while True:
        try:
            # Pull device status from cloud
            # (Different tinytuya versions expose slightly different methods; try in a safe order.)
            payload = None
            try:
                payload = cloud.getstatus(device_id)
            except Exception:
                try:
                    payload = cloud.getstatus()
                except Exception:
                    payload = cloud.getproperties(device_id)

            codes = parse_cloud_status(payload)

            if not codes:
                logger.warning(f"No readable status from cloud. Raw: {str(payload)[:200]}")
                time.sleep(10)
                continue

            # Extract raw values
            pm25 = safe_int(codes.get(CODE_PM25))
            co2 = safe_int(codes.get(CODE_CO2))

            temp_raw = safe_float(codes.get(CODE_TEMP))   # scale 1
            hum_raw  = safe_float(codes.get(CODE_HUM))    # scale 1
            voc_raw  = safe_float(codes.get(CODE_VOC))    # scale 3
            ch2o_raw = safe_float(codes.get(CODE_CH2O))   # scale 3

            # Apply scaling per mapping
            temp_c = (temp_raw / 10.0) if temp_raw is not None else None
            hum_pct = (hum_raw / 10.0) if hum_raw is not None else None
            voc_ppm = (voc_raw / 1000.0) if voc_raw is not None else None
            ch2o_ppm = (ch2o_raw / 1000.0) if ch2o_raw is not None else None

            # Validity guards
            if hum_pct is not None and (hum_pct < 0 or hum_pct > 100):
                logger.warning(f"Invalid humidity {hum_pct}; dropping.")
                hum_pct = None
            if temp_c is not None and (temp_c < -20 or temp_c > 70):
                logger.warning(f"Invalid temp {temp_c}; dropping.")
                temp_c = None
            if co2 is not None and (co2 < 200 or co2 > 20000):
                logger.warning(f"Invalid CO2 {co2}; dropping.")
                co2 = None

            raw_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

            # Build point
            p = Point("climate_raw") \
                .tag("room", ROOM_TAG) \
                .tag("sensor", SENSOR_TAG) \
                .tag("device_id", device_id) \
                .field("raw_tuya_json", raw_json)

            if co2 is not None:
                p.field("co2_ppm", int(co2))
                p.field("co2_zone", compute_zone(int(co2)))
            if hum_pct is not None:
                p.field("humidity_pct", float(hum_pct))
            if temp_c is not None:
                p.field("temperature_c", float(temp_c))
            if pm25 is not None:
                p.field("pm25_ug_m3", int(pm25))
            if voc_ppm is not None:
                p.field("voc_ppm", float(voc_ppm))
            if ch2o_ppm is not None:
                p.field("ch2o_ppm", float(ch2o_ppm))

            write_api.write(bucket=INFLUXDB_BUCKET, record=p)

            logger.info(
                f"Wrote(CLOUD) -> CO2:{co2 if co2 is not None else 'NA'}ppm "
                f"Hum:{hum_pct if hum_pct is not None else 'NA'}% "
                f"Temp:{temp_c if temp_c is not None else 'NA'}C "
                f"PM2.5:{pm25 if pm25 is not None else 'NA'}ug/m3 "
                f"VOC:{voc_ppm if voc_ppm is not None else 'NA'}ppm "
                f"CH2O:{ch2o_ppm if ch2o_ppm is not None else 'NA'}ppm"
            )

            time.sleep(poll_seconds)

        except Exception as e:
            logger.error(f"Cloud poll failure: {e}")
            time.sleep(fail_sleep)


if __name__ == "__main__":
    main()

