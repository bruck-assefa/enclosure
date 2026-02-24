import asyncio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import board
import adafruit_tca9548a
from adafruit_bme280 import basic as adafruit_bme280
import RPi.GPIO as GPIO

app = FastAPI(title="Bearded Dragon Enclosure API")

# --- Hardware Initialization (Sensors) ---
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

# --- Hardware Initialization (Relays) ---
# Map Relay IDs to BCM GPIO pins
RELAY_PINS = {
    '1': 4,
    '2': 22,
    '3': 6,
    '4': 26
}

# --- Models ---
class SensorReading(BaseModel):
    temp: float
    humidity: float
    pressure: float
    status: str

class RelayPayload(BaseModel):
    state: str  # Accepts "on" or "off"

# --- Sensor Logic ---
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
            except Exception:
                sensor_objects[global_id] = None

def read_all_sensors():
    global sensor_cache
    for global_id in range(16):
        sensor = sensor_objects.get(global_id)
        if sensor:
            try:
                sensor_cache[f"sensor_{global_id}"] = {
                    "temp": round(sensor.temperature, 2),
                    "humidity": round(sensor.relative_humidity, 2),
                    "pressure": round(sensor.pressure, 2),
                    "status": "online"
                }
            except Exception:
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

# --- Lifecycle Events ---
@app.on_event("startup")
async def startup_event():
    # 1. Setup Sensors
    init_sensors()
    asyncio.create_task(sensor_poller())

    # 2. Setup Relays
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in RELAY_PINS.values():
        # Setup as OUT. 
        # initial=GPIO.HIGH ensures relays start OFF (Active Low logic)
        GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)
    print("Relay GPIOs initialized.")

@app.on_event("shutdown")
def shutdown_event():
    print("Cleaning up GPIO...")
    GPIO.cleanup()

# --- Sensor Endpoints ---
@app.get("/sensors")
def get_all_sensors():
    return sensor_cache

@app.get("/sensors/{sensor_id}")
def get_sensor(sensor_id: str):
    return sensor_cache.get(sensor_id, {"status": "unknown"})

# --- Relay Endpoints ---
@app.get("/relays")
def get_relay_status():
    """Returns the current status of all relays."""
    status = {}
    for relay_id, pin in RELAY_PINS.items():
        # GPIO.input returns 0 (LOW) or 1 (HIGH)
        # Remember: Active LOW means LOW is ON
        state_bool = GPIO.input(pin)
        status[relay_id] = "on" if state_bool == GPIO.LOW else "off"
    return status

# REMOVE: class RelayPayload(BaseModel)... you don't need it anymore

@app.post("/relays/{relay_id}/{state}")
def control_relay(relay_id: str, state: str):
    """
    Control a relay using the URL path.
    Example: POST /relays/1/on
    """
    if relay_id not in RELAY_PINS:
        raise HTTPException(status_code=404, detail="Relay ID not found")
    
    # Normalize input
    action = state.lower()
    pin = RELAY_PINS[relay_id]

    if action == "on":
        GPIO.output(pin, GPIO.LOW)  # Active LOW logic
    elif action == "off":
        GPIO.output(pin, GPIO.HIGH)
    else:
        raise HTTPException(status_code=400, detail="State must be 'on' or 'off'")

    return {"relay_id": relay_id, "state": action, "status": "success"}