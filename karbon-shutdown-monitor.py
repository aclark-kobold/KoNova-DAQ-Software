#!/usr/bin/env python3
import serial
import subprocess
import time
import sys
import os
import json

PORT = "/dev/karbon-mcu"
BAUD = 9600
POLL_INTERVAL = 5
OFF_THRESHOLD = 240
COMMAND = b"lpmcu ignition-state\r"
STATE_FILE = "/run/karbon-ignition-state"

def write_state(state):
    data = json.dumps({"ignition": state, "timestamp": time.time()})
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(data)
    os.replace(tmp_path, STATE_FILE)
    os.chmod(STATE_FILE, 0o644)

def open_serial():
    """Keep retrying to open the port instead of giving up immediately."""
    while True:
        try:
            ser = serial.Serial(PORT, BAUD, timeout=1)
            ser.dtr = True
            time.sleep(1)
            return ser
        except serial.SerialException as e:
            print(f"Could not open {PORT}: {e} -- retrying in 5s", file=sys.stderr)
            time.sleep(5)

def read_ignition_state(ser):
    ser.dtr = True
    time.sleep(0.2)
    ser.reset_input_buffer()
    ser.write(b"\r")
    time.sleep(0.3)
    ser.read(ser.in_waiting)

    ser.write(COMMAND)
    time.sleep(0.5)
    response = ser.read(500).decode(errors="ignore").lower()

    if "true" in response:
        print("Main Power Active")
        return True
    elif "false" in response:
        print("Battery Power Active")
        return False
    print(f"DEBUG raw response: {response!r}")
    return None

def main():
    off_since = None
    ser = open_serial()

    while True:
        try:
            state = read_ignition_state(ser)
        except (serial.SerialException, OSError) as e:
            print(f"Serial error: {e} -- reconnecting", file=sys.stderr)
            try:
                ser.close()
            except Exception:
                pass
            ser = open_serial()
            continue

        if state is True:
            if off_since is not None:
                print("Main power restored, resetting timer.")
            off_since = None
            write_state(True)

        elif state is False:
            if off_since is None:
                off_since = time.monotonic()
                print("On battery power, starting shutdown timer.")
            elapsed = time.monotonic() - off_since
            print(f"On battery for {elapsed:.0f}s (threshold {OFF_THRESHOLD}s)")
            write_state(False)
            if elapsed >= OFF_THRESHOLD:
                print("Threshold reached, initiating graceful shutdown.")
                subprocess.run(["systemctl", "poweroff"])
                break
        else:
            print("Warning: could not read ignition state, retrying.")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
