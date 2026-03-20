import asyncio
import sqlite3
import logging
import sys
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

# --- Logging Setup ---
# This forces logs to write immediately with timestamps
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Bearded Dragon Enclosure API",
    root_path="/pi"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_FILE = "enclosure.db"

# --- Hardware Initialization (Sensors) ---
try:
    logger.info("Initializing I2C Bus...")
    i2c = board.I2C()
except Exception as e:
    logger.error(f"Failed to initialize I2C bus: {e}")

# Initialize BOTH multiplexers
try:
    logger.info("Connecting to Multiplexer at 0x70...")
    tca_1 = adafruit_tca9548a.TCA9548A(i2c, address=0x70)
    logger.info("Connecting to Multiplexer at 0x72...")
    tca_2 = adafruit_tca9548a.TCA9548A(i2c, address=0x72)
except Exception as e:
    logger.error(f"Failed to find Multiplexer: {e}")

multiplexers = [
    (tca_1, 0),
    (tca_2, 8)
]

sensor_objects = {}
sensor_cache = {}

# --- Hardware Initialization (Relays) ---
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
    logger.info("Initializing Database...")
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS relay_schedules (
                    relay_id TEXT PRIMARY KEY,
                    on_time TEXT,
                    off_time TEXT,
                    mode TEXT DEFAULT 'auto',
                    last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS relay_event_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    relay_id TEXT,
                    state TEXT,
                    trigger_source TEXT
                )
            """)
            cursor.execute("SELECT COUNT(*) FROM relay_schedules")
            if cursor.fetchone()[0] == 0:
                logger.info("Populating default schedules...")
                for r_id in RELAY_PINS.keys():
                    cursor.execute(
                        "INSERT INTO relay_schedules (relay_id, on_time, off_time) VALUES (?, ?, ?)",
                        (r_id, "07:00", "19:00")
                    )
            conn.commit()
        logger.info("Database initialization complete.")
    except Exception as e:
        logger.error(f"Database Initialization Failed: {e}")

def log_relay_event(relay_id: str, state: str, source: str):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO relay_event_logs (relay_id, state, trigger_source) VALUES (?, ?, ?)",
            (relay_id, state, source)
        )
        conn.commit()

def update_relay_mode(relay_id: str, mode: str):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE relay_schedules SET mode = ?, last_updated = CURRENT_TIMESTAMP WHERE relay_id = ?",
            (mode, relay_id)
        )
        conn.commit()

# --- Sensor Logic ---
def init_sensors():
    logger.info("Starting sensor scanning sequence...")
    for mux, offset in multiplexers:
        for channel in range(8):
            global_id = channel + offset
            # Log BEFORE we touch the hardware. If it freezes here, we know exactly which channel killed it.
            logger.info(f"Attempting to probe Sensor {global_id} on Mux {hex(mux.address)} Channel {channel}...")
            try:
                try:
                    sensor = adafruit_bme280.Adafruit_BME280_I2C(mux[channel], address=0x76)
                except ValueError:
                    sensor = adafruit_bme280.Adafruit_BME280_I2C(mux[channel], address=0x77)
                
                sensor_objects[global_id] = sensor
                logger.info(f"-> SUCCESS: Sensor {global_id} connected.")
            except Exception as e:
                sensor_objects[global_id] = None
                logger.warning(f"-> FAILED: No sensor found at ID {global_id} ({e})")

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
            except Exception as e:
                logger.error(f"Hardware read failure on Sensor {global_id}: {e}")
                sensor_cache[f"sensor_{global_id}"] = {
                    "temp": 0, "humidity": 0, "pressure": 0, "status": "error"
                }
        else:
            sensor_cache[f"sensor_{global_id}"] = {
                "temp": 0, "humidity": 0, "pressure": 0, "status": "offline"
            }

async def sensor_poller():
    logger.info("Background sensor poller started.")
    while True:
        await asyncio.to_thread(read_all_sensors)
        await asyncio.sleep(5)

# --- Scheduler Logic ---
def sync_sun_times():
    try:
        city = LocationInfo("McNair", "Virginia", "US", 38.93, -77.40)
        local_tz = ZoneInfo("America/New_York")
        s = sun(city.observer, date=datetime.now(local_tz), tzinfo=local_tz)
        
        sunrise_str = s['sunrise'].strftime("%H:%M")
        sunset_str = s['sunset'].strftime("%H:%M")
        
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            for relay_id in ['1', '2', '4', '3']:
                cursor.execute("""
                    UPDATE relay_schedules 
                    SET on_time = ?, off_time = ?, mode = 'auto', last_updated = CURRENT_TIMESTAMP
                    WHERE relay_id = ?
                """, (sunrise_str, sunset_str, relay_id))
            conn.commit()
        logger.info(f"Sun sync complete. Sunrise: {sunrise_str}, Sunset: {sunset_str}")
    except Exception as e:
        logger.error(f"Failed to sync sun times: {e}")

def check_schedules():
    now_str = datetime.now().strftime("%H:%M")
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            # Grab all schedules, completely ignoring the 'mode' column
            cursor.execute("SELECT relay_id, on_time, off_time FROM relay_schedules")
            schedules = cursor.fetchall()
            
        for relay_id, on_time, off_time in schedules:
            if relay_id not in RELAY_PINS:
                continue
                
            pin = RELAY_PINS[relay_id]
            current_state_bool = GPIO.input(pin)
            is_currently_on = (current_state_bool == GPIO.LOW)
            
            # Determine if the schedule dictates the light SHOULD be ON right now
            if on_time < off_time:
                # Normal day schedule (e.g., ON at 07:00, OFF at 19:00)
                should_be_on = on_time <= now_str < off_time
            else:
                # Night schedule crossing midnight (e.g., ON at 20:00, OFF at 06:00)
                should_be_on = now_str >= on_time or now_str < off_time
            
            # Enforce the schedule if the physical hardware is currently wrong
            if should_be_on and not is_currently_on:
                GPIO.output(pin, GPIO.LOW)
                log_relay_event(relay_id, "on", "scheduler_correction")
                logger.info(f"Scheduler corrected Relay {relay_id} to ON (Matches Schedule)")
                
            elif not should_be_on and is_currently_on:
                GPIO.output(pin, GPIO.HIGH)
                log_relay_event(relay_id, "off", "scheduler_correction")
                logger.info(f"Scheduler corrected Relay {relay_id} to OFF (Matches Schedule)")
                
    except Exception as e:
        logger.error(f"Scheduler check failed: {e}")

# --- Lifecycle Events ---
scheduler = AsyncIOScheduler()

@app.on_event("startup")
async def startup_event():
    logger.info("=== APPLICATION STARTUP SEQUENCE INITIATED ===")
    
    init_db()
    
    init_sensors()
    asyncio.create_task(sensor_poller())

    logger.info("Initializing GPIO Relays...")
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for pin in RELAY_PINS.values():
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)
        logger.info("Relay GPIOs initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize GPIO: {e}")

    logger.info("Starting background tasks...")
    scheduler.add_job(check_schedules, 'cron', minute='*')
    scheduler.add_job(sync_sun_times, 'cron', hour=0, minute=5)
    scheduler.start()
    
    sync_sun_times()
    logger.info("=== APPLICATION STARTUP COMPLETE ===")

@app.on_event("shutdown")
def shutdown_event():
    logger.info("Cleaning up GPIO and Scheduler...")
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
    try:
        for relay_id, pin in RELAY_PINS.items():
            state_bool = GPIO.input(pin)
            status[relay_id] = "on" if state_bool == GPIO.LOW else "off"
    except Exception as e:
        logger.error(f"Failed to read relay status: {e}")
    return status

@app.post("/relays/{relay_id}/{state}")
def control_relay(relay_id: str, state: str):
    if relay_id not in RELAY_PINS:
        raise HTTPException(status_code=404, detail="Relay ID not found")
    
    action = state.lower()
    pin = RELAY_PINS[relay_id]

    try:
        if action == "on":
            GPIO.output(pin, GPIO.LOW)
        elif action == "off":
            GPIO.output(pin, GPIO.HIGH)
        else:
            raise HTTPException(status_code=400, detail="State must be 'on' or 'off'")

        # Log the click as a momentary test, and DO NOT update the database mode
        log_relay_event(relay_id, action, "manual_test")
        logger.info(f"Momentary Test: Relay {relay_id} turned {action.upper()}")
        
        return {"relay_id": relay_id, "state": action, "status": "success", "mode": "auto"}
    except Exception as e:
        logger.error(f"Failed to trigger relay {relay_id}: {e}")
        raise HTTPException(status_code=500, detail="Hardware toggle failed")

# --- Database Endpoints (Schedules & Logs) ---
@app.get("/schedules")
def get_schedules():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM relay_schedules")
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Failed to fetch schedules: {e}")
        return []

@app.put("/schedules/{relay_id}")
def update_schedule(relay_id: str, payload: ScheduleUpdate):
    if relay_id not in RELAY_PINS:
        raise HTTPException(status_code=404, detail="Relay ID not found")
        
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE relay_schedules 
                SET on_time = ?, off_time = ?, mode = ?, last_updated = CURRENT_TIMESTAMP
                WHERE relay_id = ?
            """, (payload.on_time, payload.off_time, payload.mode, relay_id))
            conn.commit()
        logger.info(f"Schedule updated for Relay {relay_id}: ON at {payload.on_time}, OFF at {payload.off_time}")
        return {"message": f"Schedule for Relay {relay_id} updated successfully"}
    except Exception as e:
        logger.error(f"Failed to update schedule: {e}")
        raise HTTPException(status_code=500, detail="Database error")

@app.get("/logs")
def get_logs(limit: int = 50):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM relay_event_logs ORDER BY timestamp DESC LIMIT ?", (limit,))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Failed to fetch logs: {e}")
        return []