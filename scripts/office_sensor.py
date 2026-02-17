import serial
import time
import logging
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Config
SERIAL_PORT = "/dev/ttyAMA3"  # Enabled via uart3 overlay
BAUD_RATE = 9600
INFLUXDB_URL = "http://100.113.86.123:8086"
# INFLUXDB_TOKEN loaded from /etc/durafungi/influx_writer_token
INFLUXDB_ORG = "DuraFungi"
INFLUXDB_BUCKET = "climate_raw"


TOKEN_PATH = "/etc/durafungi/influx_writer_token"

def _read_token():
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None

INFLUXDB_TOKEN = _read_token()
if not INFLUXDB_TOKEN:
    raise SystemExit(f"No InfluxDB token available at {TOKEN_PATH}")

def main():
    logger.info("Starting ZPHS01B Office Sensor poller on UART3")
    
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=2.0)
        client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
        write_api = client.write_api(write_options=SYNCHRONOUS)
        
        while True:
            # Command to request data from ZPHS01B
            ser.write(b"\xff\x01\x86\x00\x00\x00\x00\x00\x79")
            response = ser.read(26)
            
            if len(response) >= 26 and response[0] == 0xff and response[1] == 0x86:
                # Parsing ZPHS01B packet
                co2 = (response[2] << 8) | response[3]
                pm25 = (response[4] << 8) | response[5]
                ch2o = ((response[6] << 8) | response[7]) / 1000.0
                voc = response[8]
                temp = ((((response[11] << 8) | response[12]) - 435) / 10.0)
                hum = response[13]
                
                point = Point("air_quality") \
                    .tag("room", "office") \
                    .tag("sensor", "zphs01b") \
                    .field("co2_ppm", int(co2)) \
                    .field("humidity_pct", float(hum)) \
                    .field("temperature_c", float(temp)) \
                    .field("pm25_ug_m3", int(pm25)) \
                    .field("voc_index", int(voc)) \
                    .field("ch2o_mg_m3", float(ch2o))

                write_api.write(bucket=INFLUXDB_BUCKET, record=point)
                logger.info(f"Office Data -> CO2: {co2} | Temp: {temp}C | Hum: {hum}%")
            
            time.sleep(30)
            
    except Exception as e:
        logger.error(f"Office Sensor Error: {e}")
        time.sleep(10)

if __name__ == "__main__":
    main()
