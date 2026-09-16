"""Only this child process imports and touches I2C. One owner per bus."""
def worker(connection):
    try:
        import board
        import adafruit_tca9548a
        from adafruit_bme280 import basic as bme280
        bus = board.I2C()
        muxes, devices = {}, {}
        while True:
            config = connection.recv()
            hw = config["hardware"]
            try:
                address = int(hw["multiplexer_address"], 16)
                if address not in muxes:
                    muxes[address] = adafruit_tca9548a.TCA9548A(bus, address=address)
                channel = muxes[address][hw["channel"]]
                sid = config["sensor_id"]
                if sid not in devices:
                    for device_address in hw["sensor_addresses"]:
                        try:
                            devices[sid] = bme280.Adafruit_BME280_I2C(channel, address=int(device_address, 16))
                            break
                        except (ValueError, OSError):
                            continue
                    if sid not in devices:
                        raise OSError("No device responded")
                sensor = devices[sid]
                connection.send({"reading": {"temperature_c": round(sensor.temperature, 2),
                    "humidity_pct": round(sensor.relative_humidity, 2),
                    "pressure_hpa": round(sensor.pressure, 2)}})
            except Exception:
                # Driver exceptions may leave a bus lock held: discard this process.
                connection.send({"error": "device_read_error"})
                return
    except (EOFError, BrokenPipeError):
        pass
    except Exception:
        try:
            connection.send({"error": "bus_initialization_error"})
        except (BrokenPipeError, EOFError):
            pass
    finally:
        connection.close()
