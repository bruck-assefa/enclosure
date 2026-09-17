from contextlib import closing
import asyncio
import sqlite3
import logging
import sys
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sensor_collector import configured_collector
import RPi.GPIO as GPIO
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import relay_settings

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

# Hardware collection is supervised separately from HTTP requests.
sensors = configured_collector()

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
    on_time: str = ''
    off_time: str = ''
    mode: str
    name: str | None = None

# --- Database & Logging Logic ---
def init_db():
    logger.info("Initializing Database...")
    try:
        with closing(sqlite3.connect(DB_FILE)) as conn, conn:
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
        relay_settings.migrate(DB_FILE)
        logger.info("Database initialization complete.")
    except Exception as e:
        logger.error(f"Database Initialization Failed: {e}")
        raise

def log_relay_event(relay_id: str, state: str, source: str):
    with closing(sqlite3.connect(DB_FILE)) as conn, conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO relay_event_logs (relay_id, state, trigger_source) VALUES (?, ?, ?)",
            (relay_id, state, source)
        )
        conn.commit()

def update_relay_mode(relay_id: str, mode: str):
    with closing(sqlite3.connect(DB_FILE)) as conn, conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE relay_schedules SET mode = ?, last_updated = CURRENT_TIMESTAMP WHERE relay_id = ?",
            (mode, relay_id)
        )
        conn.commit()

# --- Scheduler Logic ---
def sync_sun_times():
    try:
        relay_settings.sync_solar(DB_FILE)
    except Exception:
        logger.exception('Solar schedule sync failed')


def check_schedules():
    now_str = datetime.now(relay_settings.ZONE).strftime("%H:%M")
    try:
        with closing(sqlite3.connect(DB_FILE)) as conn, conn:
            cursor = conn.cursor()
            # Solar sync resolves sun rows; custom rows retain their saved times.
            cursor.execute("SELECT relay_id, on_time, off_time FROM relay_schedules")
            schedules = cursor.fetchall()
            
        for relay_id, on_time, off_time in schedules:
            if relay_id not in RELAY_PINS:
                continue
                
            pin = RELAY_PINS[relay_id]
            current_state_bool = GPIO.input(pin)
            is_currently_on = (current_state_bool == GPIO.LOW)
            
            should_be_on = relay_settings.is_on(on_time, off_time, now_str)

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
scheduler = AsyncIOScheduler(timezone=relay_settings.ZONE)

@app.on_event("startup")
async def startup_event():
    logger.info("=== APPLICATION STARTUP SEQUENCE INITIATED ===")
    
    init_db()
    
    await sensors.start()

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
    scheduler.add_job(sync_sun_times, 'cron', hour=0, minute=0)
    scheduler.start()
    
    sync_sun_times()
    check_schedules()
    logger.info("=== APPLICATION STARTUP COMPLETE ===")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Cleaning up GPIO and Scheduler...")
    await sensors.stop()
    scheduler.shutdown()
    GPIO.cleanup()

# --- Sensor Endpoints ---
@app.get("/v1/sensors")
async def sensor_snapshot():
    return sensors.snapshot()


def legacy_sensors():
    # Compatibility endpoint: failed values are null, never fabricated zeroes.
    result = {}
    for sensor in sensors.snapshot()["sensors"]:
        reading = sensor["last_good_reading"] or {}
        status = {"healthy": "online", "disabled": "offline", "unavailable": "offline"}.get(
            sensor["status"], "error")
        result[sensor["sensor_id"]] = {
            "temp": reading.get("temperature_c"), "humidity": reading.get("humidity_pct"),
            "pressure": reading.get("pressure_hpa"), "status": status,
            "health": sensor["status"], "last_success_at": sensor["last_success_at"]}
    return result


@app.get("/sensors")
async def get_all_sensors():
    return legacy_sensors()


@app.get("/sensors/{sensor_id}")
async def get_sensor(sensor_id: str):
    return legacy_sensors().get(sensor_id, {"status": "unknown"})


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

@app.get("/system/scan")
async def cached_hardware_health():
    """Compatibility health map. Does not probe or acquire the I2C bus."""
    return {sid: value["status"] for sid, value in legacy_sensors().items()}


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
        with closing(sqlite3.connect(DB_FILE)) as conn, conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM relay_schedules")
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Failed to fetch schedules: {e}")
        return []

@app.put("/schedules/{relay_id}")
def update_schedule(relay_id: str, payload: ScheduleUpdate):
    try:
        return relay_settings.save(DB_FILE, relay_id, payload.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))


@app.get('/daylight')
def get_daylight():
    return relay_settings.daylight()


@app.get("/logs")
def get_logs(limit: int = 50):
    try:
        with closing(sqlite3.connect(DB_FILE)) as conn, conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM relay_event_logs ORDER BY timestamp DESC LIMIT ?", (limit,))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Failed to fetch logs: {e}")
        return []
