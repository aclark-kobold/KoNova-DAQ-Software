import os
import time
from ctypes import *
import ctypes

I2C_ADDR  = 0x20  #I2C ADDR
DevIndex = '/dev/ch34x_pis0'
DevIndex_c = ctypes.c_char_p(DevIndex.encode('utf-8'))		#Convert DevIndex to const char *

class spi_config(Structure):
        _fields_ = [
            ("iMode", c_ubyte),
            ("iClock", c_ubyte),
            ("iByteOrder", c_ubyte),
            ("iSpiWriteReadInterval", c_ushort),
            ("iSpiOutDefaultData",c_ubyte),
            ("iChipSelect", c_ulong),
            ("CS1Polarity",c_ubyte),  
            ("CS2Polarity", c_ubyte), 
            ("iIsAutoDeativeCS", c_ushort), 
            ("iActiveDelay", c_ushort), 
            ("iDelayDeactive", c_ulong),
        ]

def delay_ms(delaytime):
    time.sleep(delaytime / 1000.0)
    
class USBCH347T():
    ch347 = cdll.LoadLibrary("./libch347.so")
    def __init__(self, usb_dev = DevIndex_c):
        self.USB_id = usb_dev
        self.handle = self.ch347.CH347OpenDevice(self.USB_id)
        if self.handle == -1:
            print("USB CH347 Open Failed!")
 
###########################################################################################################################################################################################################################################################
#SPI
    """ SPI Init """
    def SPI_Init(self):  
        print("CH347 Open succeeded")
        CH347_SPI = spi_config()
        CH347_SPI.iMode = 0x03                    # 0-3:SPI Mode0/1/2/3
        CH347_SPI.iClock = 0x04                   # 0=60MHz, 1=30MHz, 2=15MHz, 3=7.5MHz, 4=3.75MHz, 5=1.875MHz, 6=937.5KHz,7=468.75KHz
        CH347_SPI.iByteOrder = 0x01               # 0=(LSB), 1=(MSB)
        CH347_SPI.iSpiOutDefaultData = 0xff       # The SPI outputs data by default when it reads data
        CH347_SPI.iChipSelect = 0x80              # CS control, bit 7 is 0, the CS control is ignored, bit 7 is 1, the parameter is valid: bit 1, bit 0 is 00/01, respectively select CS1/CS2 pin as a low level active CS

        if self.ch347.CH347SPI_Init(self.handle, CH347_SPI) != 1 :
            print("SPI Initialization Failed")
            self.ch347.CH347SPI_Init(self.handle, CH347_SPI)
            if self.ch347.CH347SPI_Init(self.handle, CH347_SPI) != 1 :
                print("SPI Initialization Failed")
                return -1
            else :
                print("SPI Initialization succeeded")
                return 0 
        else :
            print("SPI Initialization succeeded")
            return 0 
 
    """ GPIO Control """
    def CH347T_GPIO(self,Enable_x,SetDirOut,SetDataOut):        #1 Parameters: GPIO number, input/output, high/low
        self.ch347.CH347GPIO_Set(self.handle, Enable_x, SetDirOut, SetDataOut)
   
    """ SPI Write Byte """
    def SPI_WriteByte(self,data):                               #1 Parameter: Data to be sent
        data = bytes(data)
        buffer_ptr = ctypes.create_string_buffer(data)
        self.ch347.CH347SPI_WriteRead(self.handle,0, 0x80, 1, buffer_ptr)

    """ Close Derive"""   
    def Close_SPI(self):
        self.ch347.CH347CloseDevice(self.handle)
    
###########################################################################################################################################################################################################################################################
#I2C
    """ I2C Read Byte """
    def I2C_Read(self):                #1 Parameter: The address of the I2C device to be read
        rec  = (c_byte * 1)()
        ibuf = (c_byte * 9)()
        rec[0] = I2C_ADDR
        
        self.ch347.CH347StreamI2C(self.handle, 1, rec, 9, ibuf)
        return ibuf
    
    """ I2C Write Byte """
    def I2C_Write(self,data,size):     #1 Parameters: I2C device address, data to be sent, length of data to be sent
            tcmd = (c_byte * (size + 1))()
            ibuf = (c_byte * 1)()
            tcmd[0] = I2C_ADDR
            
            for i in range (size):              #2 Adjust data sequence number
                tcmd[i+1] = data[i] & 0xff      

            self.ch347.CH347StreamI2C(self.handle, 6, tcmd, 0, ibuf)


    ### END OF FILE ###

