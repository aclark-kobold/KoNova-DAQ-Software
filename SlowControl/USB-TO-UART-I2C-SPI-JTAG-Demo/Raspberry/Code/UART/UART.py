import time
import serial

# Open the serial port
ser0 = serial.Serial('/dev/ttyACM0', 115200)
ser1 = serial.Serial('/dev/ttyACM1', 115200)
data0 = b'Hello, World!'    # Data to be sent (in bytes)

while True:
    ser0.write(data0)       # send data
    if ser1.in_waiting:  
        data1 = ser1.read(13)
        print(data1)
        print()
        

# Close the serial port
ser.close()
