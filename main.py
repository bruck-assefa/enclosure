import asyncio
from fastapi import FastAPI
from pydantic import BaseModel
import board
import adafruit_tca9548a
from adafruit_bme280 import basic as adafruit_bme280

app = FastAPI(title="Bearded Dragon Enclosure API")

# --- Hardware Initialization ---
i2c = board.I2C()
tca = adafruit_tca9548a.TCA9548A(i2c)

# Store SENSOR OBJECTS, not just data
sensor_objects = {}
sensor_cache = {}

def init_sensors():
    """Attempt to connect to all 8 sensors once at startup."""
    print("Initializing sensors...")
    for channel in range(8):
        try:
            # Try address 0x76 first (common), then 0x77 (Adafruit standard)
            try:
                sensor = adafruit_bme280.Adafruit_BME280_I2C(tca[channel], address=0x76)
            except ValueError:
                sensor = adafruit_bme280.Adafruit_BME280_I2C(tca[channel], address=0x77)
            
            sensor_objects[channel] = sensor
            print(f"Sensor {channel}: CONNECTED")
        except Exception as e:
            print(f"Sensor {channel}: FAILED ({e})")
            sensor_objects[channel] = None

# --- Models ---
class SensorReading(BaseModel):
    temp: float
    humidity: float
    pressure: float
    status: str

# --- Sensor Polling Logic ---
# In your original main.py

def read_all_sensors():
    global sensor_cache
    for channel in range(8):
        try:
            # CHANGE THIS LINE: Add address=0x76
            sensor = adafruit_bme280.Adafruit_BME280_I2C(tca[channel], address=0x76)
            
            sensor_cache[f"sensor_{channel}"] = {
                "temp": round(sensor.temperature, 2),
                "humidity": round(sensor.relative_humidity, 2),
                "pressure": round(sensor.pressure, 2),
                "status": "online"
            }
        except Exception:
            # This will happen for channels 0, 1, 2, 4, 5, 6, 7 since they are empty
            sensor_cache[f"sensor_{channel}"] = {
                "temp": 0, "humidity": 0, "pressure": 0, "status": "offline"
            }

async def sensor_poller():
    while True:
        read_all_sensors()
        await asyncio.sleep(5)

@app.on_event("startup")
async def startup_event():
    init_sensors()
    asyncio.create_task(sensor_poller())

@app.get("/sensors")
def get_all_sensors():
    return sensor_cache

@app.get("/sensors/{sensor_id}")
def get_sensor(sensor_id: str):
    return sensor_cache.get(sensor_id, {"status": "unknown"})