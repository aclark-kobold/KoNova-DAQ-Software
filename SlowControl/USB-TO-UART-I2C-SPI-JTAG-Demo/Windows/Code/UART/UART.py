import time
import serial

# Open the serial port
ser0 = serial.Serial("COM27", 115200, timeout=1)
ser1 = serial.Serial("COM28", 115200, timeout=1)
data0 = b'Hello, World!'    # Data to be sent (in bytes)

while True:
    ser0.write(data0)       # send data
    if ser1.in_waiting:  
        data1 = ser1.read(13)
        print(data1)
        print()
        

# Close the serial port
ser.close()
