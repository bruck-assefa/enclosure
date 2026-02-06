import time
import asyncio
from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import board
import adafruit_tca9548a
import adafruit_bme280

app = FastAPI(title="Bearded Dragon Enclosure API")

# --- Hardware Initialization ---
i2c = board.I2C()
# Multiplexer 1 at default address 0x70
tca = adafruit_tca9548a.TCA9548A(i2c)

# Global store for sensor data
sensor_cache = {}

# --- Models ---
class SensorReading(BaseModel):
    temp: float
    humidity: float
    pressure: float
    status: str

# --- Sensor Polling Logic ---
def read_all_sensors():
    """Loops through multiplexer channels and reads BME280s."""
    global sensor_cache
    # Adjust range(8) if using multiple multiplexers
    for channel in range(8):
        try:
            # Select the channel on the multiplexer
            sensor = adafruit_bme280.Adafruit_BME280_I2C(tca[channel])
            
            sensor_cache[f"sensor_{channel}"] = {
                "temp": round(sensor.temperature, 2),
                "humidity": round(sensor.relative_humidity, 2),
                "pressure": round(sensor.pressure, 2),
                "status": "online"
            }
        except Exception:
            sensor_cache[f"sensor_{channel}"] = {
                "temp": 0, "humidity": 0, "pressure": 0, "status": "offline"
            }

async def sensor_poller():
    """Background loop to refresh data every 5 seconds."""
    while True:
        read_all_sensors()
        await asyncio.sleep(5)

@app.on_event("startup")
async def startup_event():
    # Start the background poller when the API starts
    asyncio.create_task(sensor_poller())

# --- API Endpoints ---

@app.get("/sensors")
def get_all_sensors():
    return sensor_cache

@app.get("/sensors/{sensor_id}", response_model=SensorReading)
def get_sensor(sensor_id: str):
    return sensor_cache.get(sensor_id, {"temp": 0, "humidity": 0, "pressure": 0, "status": "unknown"})

@app.post("/relays/{relay_id}/{state}")
def control_relay(relay_id: int, state: bool):
    # Logic for your 4-relay hat goes here
    # Example: GPIO.output(relay_pins[relay_id], GPIO.HIGH if state else GPIO.LOW)
    return {"relay": relay_id, "active": state}