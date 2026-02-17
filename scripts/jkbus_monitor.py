#!/usr/bin/env python3
import os
import time
import serial
import logging
from datetime import datetime
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# --- PHASE 2.1 PRODUCTION TARGETS ---
INFLUX_URL = "http://100.113.86.123:8086"
INFLUX_ORG = "DuraFungi"
INFLUX_BUCKET = "energy_raw"
SERIAL_PORT = "/dev/serial/by-id/usb-WCH.CN_USB_Quad_Serial_BC6227ABCD-if04"

# --- GLOBAL CLIENT SETUP ---
def _read_token():
    """Load InfluxDB token securely from file or environment variable."""
    try:
        with open("/etc/durafungi/influx_token", "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.warning("Token file /etc/durafungi/influx_token not found - falling back to environment variable")
        token = os.getenv("INFLUX_TOKEN")
        if not token:
            logger.critical("No InfluxDB token available (neither file nor INFLUX_TOKEN env var)")
            raise SystemExit(1)
        return token

# Logging setup (before token read so we can log issues)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

TOKEN = _read_token()
client = InfluxDBClient(url=INFLUX_URL, token=TOKEN, org=INFLUX_ORG)
write_api = client.write_api(write_options=SYNCHRONOUS)

# --- PROTOCOL HELPERS ---
def crc16_modbus(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc & 0xFFFF

def verify_crc(frame):
    if len(frame) < 4:
        return False
    payload = frame[1:-2]
    tail_le = int.from_bytes(frame[-2:], "little")
    return crc16_modbus(payload) == tail_le

def get_int16_signed(data, pos):
    val = (data[pos] << 8) | data[pos + 1]
    if val > 32767:
        val -= 65536
    return val

# --- FRAME DECODERS ---
def decode_telemetry(frame):
    if len(frame) < 47 or frame[1:5] != b"\xf1\xf1\x12\x2f":
        return None
    if frame[9] == 0 and frame[10] == 0:
        return None  # likely standby/empty frame
    return {
        'outdoor_ambient_c':        get_int16_signed(frame, 9)  / 10,
        'suction_temp_c':           get_int16_signed(frame, 11) / 10,
        'defrost_coil_temp_c':      get_int16_signed(frame, 15) / 10,
        'exhaust_temp_c':           get_int16_signed(frame, 25) / 10,
        'heating_water_setpoint_c': get_int16_signed(frame, 31) / 10,
        'liquid_tube_temp_c':       get_int16_signed(frame, 41) / 10,
    }

def decode_status(frame):
    if len(frame) < 51 or frame[1:3] != b"\x00\xf1":
        return None
    return {
        'return_water_temp_c': get_int16_signed(frame, 33) / 10,
        'outlet_water_temp_c': get_int16_signed(frame, 35) / 10,
        'dhw_setpoint_c':      get_int16_signed(frame, 37) / 10,
    }

# --- FRAME CAPTURE ---
def capture_frames(ser, duration=3.0):
    ser.reset_input_buffer()
    buf = b""
    t0 = time.time()
    while time.time() - t0 < duration:
        if ser.in_waiting > 0:
            buf += ser.read(ser.in_waiting)
        time.sleep(0.005)
    
    # Find all 0x7E delimited frames
    idx = [i for i, b in enumerate(buf) if b == 0x7E]
    frames = []
    for i in range(len(idx)):
        start = idx[i]
        end = idx[i + 1] if i + 1 < len(idx) else len(buf)
        fr = buf[start:end]
        if len(fr) >= 6 and verify_crc(fr):
            frames.append(fr)
    return frames

# --- MAIN APPLICATION ---
def main():
    logger.info("Aokol Heat Pump JK-BUS Monitor - Phase 2.1 Production")
    logger.info(f"InfluxDB: {INFLUX_URL} | Org: {INFLUX_ORG} | Bucket: {INFLUX_BUCKET}")
    logger.info(f"Serial:   {SERIAL_PORT}")

    ser = None
    while ser is None:
        try:
            ser = serial.Serial(
                port=SERIAL_PORT,
                baudrate=9600,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                bytesize=serial.EIGHTBITS,
                timeout=0.1
            )
            ser.flushInput()
            ser.flushOutput()
            logger.info("Serial port opened successfully")
        except serial.SerialException as e:
            logger.error(f"Cannot open serial port: {e} → retrying in 10 seconds")
            time.sleep(10)

    try:
        while True:
            frames = capture_frames(ser, duration=3.0)
            points = []
            tele_data = None
            status_data = None

            # Prefer most recent valid frames (reverse order)
            for f in reversed(frames):
                if len(f) >= 47:
                    tele = decode_telemetry(f)
                    if tele:
                        tele_data = tele
                        break

            for f in reversed(frames):
                if len(f) >= 51:
                    stat = decode_status(f)
                    if stat:
                        status_data = stat
                        break

            if tele_data or status_data:
                p = Point("heatpump") \
                    .tag("device", "aokol_ashp") \
                    .time(datetime.utcnow())

                if tele_data:
                    for k, v in tele_data.items():
                        p.field(k, float(v))
                if status_data:
                    for k, v in status_data.items():
                        p.field(k, float(v))

                points.append(p)

            if points:
                try:
                    write_api.write(bucket=INFLUX_BUCKET, record=points)
                    logger.info(f"Successfully wrote {len(points)} point(s) ({len(tele_data or {})} tele + {len(status_data or {})} status fields)")
                except Exception as e:
                    logger.error(f"InfluxDB write failed: {e}")
            else:
                logger.debug("No valid telemetry or status frames captured in this cycle")

            time.sleep(7.0)

    except KeyboardInterrupt:
        logger.info("Shutdown requested by user (Ctrl+C)")
    except Exception as e:
        logger.exception(f"Unexpected error in main loop: {e}")
    finally:
        if ser and ser.is_open:
            ser.close()
            logger.info("Serial port closed")

if __name__ == "__main__":
    main()
