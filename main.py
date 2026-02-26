import asyncio
import sqlite3
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import board
import adafruit_tca9548a
from adafruit_bme280 import basic as adafruit_bme280
import RPi.GPIO as GPIO
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from astral import LocationInfo
from astral.sun import sun
from zoneinfo import ZoneInfo

app = FastAPI(
    title="Bearded Dragon Enclosure API",
    root_path="/pi"
)

app.add_middleware(
    CORSMiddleware,
    # In production, replace "*" with your AWS frontend's specific URL (e.g., ["http://your-aws-ip"])
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_FILE = "enclosure.db"

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

class ScheduleUpdate(BaseModel):
    on_time: str
    off_time: str
    mode: str

# --- Database & Logging Logic ---
def init_db():
    """Initializes SQLite tables and default schedules if they don't exist."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        
        # Create schedules table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS relay_schedules (
                relay_id TEXT PRIMARY KEY,
                on_time TEXT,
                off_time TEXT,
                mode TEXT DEFAULT 'auto',
                last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create history log table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS relay_event_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                relay_id TEXT,
                state TEXT,
                trigger_source TEXT
            )
        """)
        
        # Pre-populate default schedules if empty
        cursor.execute("SELECT COUNT(*) FROM relay_schedules")
        if cursor.fetchone()[0] == 0:
            for r_id in RELAY_PINS.keys():
                cursor.execute(
                    "INSERT INTO relay_schedules (relay_id, on_time, off_time) VALUES (?, ?, ?)",
                    (r_id, "07:00", "19:00")
                )
        conn.commit()

def log_relay_event(relay_id: str, state: str, source: str):
    """Writes a relay state change to the log table."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO relay_event_logs (relay_id, state, trigger_source) VALUES (?, ?, ?)",
            (relay_id, state, source)
        )
        conn.commit()

def update_relay_mode(relay_id: str, mode: str):
    """Updates the mode (auto/manual) of a relay."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE relay_schedules SET mode = ?, last_updated = CURRENT_TIMESTAMP WHERE relay_id = ?",
            (mode, relay_id)
        )
        conn.commit()

# --- Sensor Logic ---
def init_sensors():
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

# --- Scheduler Logic ---
def sync_sun_times():
    """Calculates sunrise/sunset and updates schedules (Runs daily)."""
    # Coordinates for McNair, VA
    city = LocationInfo("McNair", "Virginia", "US", 38.93, -77.40)
    local_tz = ZoneInfo("America/New_York")
    
    s = sun(city.observer, date=datetime.now(local_tz), tzinfo=local_tz)
    
    sunrise_str = s['sunrise'].strftime("%H:%M")
    sunset_str = s['sunset'].strftime("%H:%M")
    
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        # Example: Automatically sync Relays 1 and 2 to the sun.
        for relay_id in ['1', '2']:
            cursor.execute("""
                UPDATE relay_schedules 
                SET on_time = ?, off_time = ?, mode = 'auto', last_updated = CURRENT_TIMESTAMP
                WHERE relay_id = ?
            """, (sunrise_str, sunset_str, relay_id))
        conn.commit()
    print(f"Sun sync complete. Sunrise: {sunrise_str}, Sunset: {sunset_str}")

def check_schedules():
    """Checks current time against database schedules (Runs every minute)."""
    now_str = datetime.now().strftime("%H:%M")
    
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT relay_id, on_time, off_time FROM relay_schedules WHERE mode='auto'")
        schedules = cursor.fetchall()
        
    for relay_id, on_time, off_time in schedules:
        if relay_id not in RELAY_PINS:
            continue
            
        pin = RELAY_PINS[relay_id]
        current_state_bool = GPIO.input(pin)
        is_currently_on = (current_state_bool == GPIO.LOW)
        
        # Trigger ON
        if now_str == on_time and not is_currently_on:
            GPIO.output(pin, GPIO.LOW)
            log_relay_event(relay_id, "on", "scheduler")
            print(f"Scheduler turned ON relay {relay_id}")
            
        # Trigger OFF
        elif now_str == off_time and is_currently_on:
            GPIO.output(pin, GPIO.HIGH)
            log_relay_event(relay_id, "off", "scheduler")
            print(f"Scheduler turned OFF relay {relay_id}")

# --- Lifecycle Events ---
scheduler = AsyncIOScheduler()

@app.on_event("startup")
async def startup_event():
    # 1. Setup Database
    init_db()

    # 2. Setup Sensors
    init_sensors()
    asyncio.create_task(sensor_poller())

    # 3. Setup Relays
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in RELAY_PINS.values():
        GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)
    print("Relay GPIOs initialized.")

    # 4. Setup Background Scheduler
    # Check times every minute
    scheduler.add_job(check_schedules, 'cron', minute='*')
    # Update sun times daily at 00:05
    scheduler.add_job(sync_sun_times, 'cron', hour=0, minute=5)
    scheduler.start()
    
    # Run a sun sync immediately on boot so times are accurate today
    sync_sun_times() 

@app.on_event("shutdown")
def shutdown_event():
    print("Cleaning up GPIO and Scheduler...")
    scheduler.shutdown()
    GPIO.cleanup()

# --- Sensor Endpoints ---
@app.get("/sensors")
def get_all_sensors():
    return sensor_cache

@app.get("/sensors/{sensor_id}")
def get_sensor(sensor_id: str):
    return sensor_cache.get(sensor_id, {"status": "unknown"})

# --- Relay Status & Control Endpoints ---
@app.get("/relays")
def get_relay_status():
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

    # Log the manual override and switch that relay to manual mode
    log_relay_event(relay_id, action, "manual_ui")
    update_relay_mode(relay_id, "manual")

    return {"relay_id": relay_id, "state": action, "status": "success", "mode": "manual"}

# --- Database Endpoints (Schedules & Logs) ---
@app.get("/schedules")
def get_schedules():
    """Retrieve all current schedules."""
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM relay_schedules")
        return [dict(row) for row in cursor.fetchall()]

@app.put("/schedules/{relay_id}")
def update_schedule(relay_id: str, payload: ScheduleUpdate):
    """Manually update the schedule for a specific relay from the UI."""
    if relay_id not in RELAY_PINS:
        raise HTTPException(status_code=404, detail="Relay ID not found")
        
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE relay_schedules 
            SET on_time = ?, off_time = ?, mode = ?, last_updated = CURRENT_TIMESTAMP
            WHERE relay_id = ?
        """, (payload.on_time, payload.off_time, payload.mode, relay_id))
        conn.commit()
    
    return {"message": f"Schedule for Relay {relay_id} updated successfully"}

@app.get("/logs")
def get_logs(limit: int = 50):
    """Retrieve the most recent relay events."""
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM relay_event_logs ORDER BY timestamp DESC LIMIT ?", (limit,))
        return [dict(row) for row in cursor.fetchall()]