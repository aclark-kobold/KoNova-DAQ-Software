import serial
import struct
import time

# Open the file in append mode ('a').
# If the file doesn't exist, it will be created.
# If the file exists, new data will be added to the end.
file = open("Accel.txt", "a")

# Append new data to the file
#file_object.write("This line is appended.\n")

# Close the file
#file_object.close()




ser = serial.Serial('/dev/ttyUSB0', 9600, timeout=1)

def checksum(data):
    return sum(data) & 0xFF
count=0
while True:
    # look for header byte
    if ser.read() != b'\x55':
        continue

    t = ser.read(1)
    payload = ser.read(8)
    csum = ser.read(1)

    if checksum(b'\x55' + t + payload) != csum[0]:
        continue  # bad frame

    frame_type = t[0]

    if (count%100) <4:
      Tnow=int(time.time())
      #print("count",count,count%1000, "frame_type",frame_type)


      if frame_type == 0x51:  # Acceleration
        ax, ay, az, temp = struct.unpack('<hhhh', payload)
        aX=ax/32768*16
        aY=ay/32768*16
        aZ=az/32768*16 
        print(Tnow, "Accel (g):", aX,aY,aZ,temp/100)
        # Append new data to the file
        file.write(str(Tnow)+'\t'+str(aX)+'\t'+ str(aY)+'\t'+str(aZ)+'\t')


      elif frame_type == 0x52:  # Gyro
        gx, gy, gz, temp = struct.unpack('<hhhh', payload)
        #print("Gyro (°/s):", gx/32768*2000, gy/32768*2000, gz/32768*2000)

      elif frame_type == 0x53:  # Euler angles
        roll, pitch, yaw, temp = struct.unpack('<hhhh', payload)
        #print("mv",temp)
        #print("Angles (°):", roll/32768*180, pitch/32768*180, yaw/32768*180)

      elif frame_type == 0x54:  # Magnetometer
        mx, my, mz, temp = struct.unpack('<hhhh', payload)
        mx_uT = mx*.3
        my_uT = my*.3
        mz_uT = mz*.3
        print("Mag:", mx_uT, my_uT, mz_uT)
        file.write(str(mx_uT)+'\t'+ str(my_uT)+'\t'+str(mz_uT)+'\n')
    count +=1
