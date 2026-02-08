import asyncio
from fastapi import FastAPI
from pydantic import BaseModel
import board
import adafruit_tca9548a
from adafruit_bme280 import basic as adafruit_bme280

app = FastAPI(title="Bearded Dragon Enclosure API")

# --- Hardware Initialization ---
i2c = board.I2C()

# Initialize BOTH multiplexers
# 0x70 is the default address, 0x71 is your new one
tca_1 = adafruit_tca9548a.TCA9548A(i2c, address=0x70)
tca_2 = adafruit_tca9548a.TCA9548A(i2c, address=0x72)

# Create a list to easily loop through them
# Format: (mux_object, id_offset)
# Mux 1 covers IDs 0-7, Mux 2 covers IDs 8-15
multiplexers = [
    (tca_1, 0),
    (tca_2, 8)
]

# Store SENSOR OBJECTS to avoid re-initializing them constantly
sensor_objects = {}
sensor_cache = {}

def init_sensors():
    """Attempt to connect to all 16 channels across both muxes."""
    print("Initializing sensors...")
    
    for mux, offset in multiplexers:
        for channel in range(8):
            # Calculate a unique global ID (e.g., 0-7 for mux1, 8-15 for mux2)
            global_id = channel + offset
            
            try:
                # Try address 0x76 first, then 0x77
                try:
                    sensor = adafruit_bme280.Adafruit_BME280_I2C(mux[channel], address=0x76)
                except ValueError:
                    sensor = adafruit_bme280.Adafruit_BME280_I2C(mux[channel], address=0x77)
                
                sensor_objects[global_id] = sensor
                print(f"Sensor {global_id} (Mux {hex(mux.address)} Ch {channel}): CONNECTED")
                
            except Exception as e:
                # If no sensor is found, store None so we know it's empty
                # print(f"Sensor {global_id}: Not found") # Optional: uncomment to debug
                sensor_objects[global_id] = None

# --- Models ---
class SensorReading(BaseModel):
    temp: float
    humidity: float
    pressure: float
    status: str

# -- Sensor Polling Logic --
def read_all_sensors():
    global sensor_cache
    
    # Iterate through all 16 potential sensor slots
    for global_id in range(16):
        sensor = sensor_objects.get(global_id)
        
        if sensor:
            try:
                # We use the EXISTING sensor object rather than creating a new one
                sensor_cache[f"sensor_{global_id}"] = {
                    "temp": round(sensor.temperature, 2),
                    "humidity": round(sensor.relative_humidity, 2),
                    "pressure": round(sensor.pressure, 2),
                    "status": "online"
                }
            except Exception as e:
                # If reading fails (sensor disconnected while running)
                sensor_cache[f"sensor_{global_id}"] = {
                    "temp": 0, "humidity": 0, "pressure": 0, "status": "error"
                }
        else:
            # If the sensor was never found during init
            sensor_cache[f"sensor_{global_id}"] = {
                "temp": 0, "humidity": 0, "pressure": 0, "status": "offline"
            }

async def sensor_poller():
    while True:
        # Run the read function in a thread to avoid blocking the API
        await asyncio.to_thread(read_all_sensors)
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