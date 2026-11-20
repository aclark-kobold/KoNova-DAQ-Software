import typing
import CH347T_Config
import time
from ctypes import *
import numpy as np

SSD1306_SETCONTRAST  = 0x81
SSD1306_DISPLAYALLON_RESUME  = 0xA4
SSD1306_DISPLAYALLON  = 0xA5
SSD1306_NORMALDISPLAY  = 0xA6
SSD1306_INVERTDISPLAY  = 0xA7
SSD1306_DISPLAYOFF  = 0xAE
SSD1306_DISPLAYON  = 0xAF
SSD1306_SETDISPLAYOFFSET  = 0xD3
SSD1306_SETCOMPINS  = 0xDA
SSD1306_SETVCOMDETECT  = 0xDB
SSD1306_SETDISPLAYCLOCKDIV  = 0xD5
SSD1306_SETPRECHARGE  = 0xD9
SSD1306_SETMULTIPLEX  = 0xA8
SSD1306_SETLOWCOLUMN  = 0x00
SSD1306_SETHIGHCOLUMN  = 0x10
SSD1306_SETSTARTLINE  = 0x40
SSD1306_MEMORYMODE  = 0x20
SSD1306_COLUMNADDR  = 0x21
SSD1306_PAGEADDR  = 0x22
SSD1306_COMSCANINC  = 0xC0
SSD1306_COMSCANDEC  = 0xC8
SSD1306_SEGREMAP  = 0xA0
SSD1306_CHARGEPUMP  = 0x8D
SSD1306_EXTERNALVCC  = 0x01
SSD1306_SWITCHCAPVCC  = 0x02

#Scrolling constants
SSD1306_ACTIVATE_SCROLL  = 0x2F
SSD1306_DEACTIVATE_SCROLL  = 0x2E
SSD1306_SET_VERTICAL_SCROLL_AREA  = 0xA3
SSD1306_RIGHT_HORIZONTAL_SCROLL  = 0x26
SSD1306_LEFT_HORIZONTAL_SCROLL  = 0x27
SSD1306_VERTICAL_AND_RIGHT_HORIZONTAL_SCROLL  = 0x29
SSD1306_VERTICAL_AND_LEFT_HORIZONTAL_SCROLL  = 0x2A

OLED_WIDTH   = 128 #OLED width
OLED_HEIGHT  = 64  #OLED height

"""GPIO Selection """          #(Please refer to the explanation below to modify)
#GPIO6
OLED_DC_Enable_x   = 0x40      ##Enable_x: Pin number, using GPIOx (0x10 if GPIO4 is enabled, the 4th bit, 0x18 if GPIO4 and GPIO3 are enabled)
OLED_DC_SetDirOut  = 0x40      #SetDirOut: Input or output (0x18 if GPIO4 and GPIO3 are output, 0x10 if GPIO4 is output)
OLED_DC_OUT_H = 0x40           #SetDataOut: High or low output (0x18 if GPIO4 and GPIO3 are set to output high, 0x10 if GPIO4 is set to output high, 0x00 if GPIO4 is set to output low, similarly, which operation is which position is high and low)
OLED_DC_OUT_L = 0x00
#GPIO7
OLED_Reset_Enable_x   = 0x80   ##Enable_x: Pin number, using GPIOx (0x10 if GPIO4 is enabled, the 4th bit, 0x18 if GPIO4 and GPIO3 are enabled)
OLED_Reset_SetDirOut  = 0x80   #SetDirOut: Input or output (0x18 if GPIO4 and GPIO3 are output, 0x10 if GPIO4 is output)
OLED_Reset_OUT_H = 0x80        #SetDataOut: High or low output (0x18 if GPIO4 and GPIO3 are set to output high, 0x10 if GPIO4 is set to output high, 0x00 if GPIO4 is set to output low, similarly, which operation is which position is high and low)
OLED_Reset_OUT_L = 0x00 


class OLED_0in96():
    SPI_Control = CH347T_Config.USBCH347T()
    def __init__(self):
        self.width = OLED_WIDTH
        self.height = OLED_HEIGHT
        #Initialize DC RST pin
        
    """    Write register address and data     """
    def command(self, cmd):
        self.OLED_DC_L()
        OLED_0in96.SPI_Control.SPI_WriteByte([cmd])

    def Init(self):
        if ( OLED_0in96.SPI_Control.SPI_Init() != 0):
            return -1
        self.reset()

        """Initialize dispaly""" 
        self.command(SSD1306_DISPLAYOFF) 
        self.command(SSD1306_SETDISPLAYCLOCKDIV) 
        self.command(0x80)                               # the suggested ratio 0x80

        self.command(SSD1306_SETMULTIPLEX) 
        self.command(0x3F) 
        self.command(SSD1306_SETDISPLAYOFFSET) 
        self.command(0x0)                                # no offset
        self.command(SSD1306_SETSTARTLINE | 0x0)         # line #0
        self.command(SSD1306_CHARGEPUMP)
        self.command(0x14) 

        self.command(SSD1306_MEMORYMODE) 
        self.command(0x00)                               # 0x0 act like ks0108

        self.command(SSD1306_SEGREMAP | 0x1) 
        self.command(SSD1306_COMSCANDEC) 
        self.command(SSD1306_SETCOMPINS) 
        self.command(0x12)            # TODO - calculate based on _rawHieght ?
        self.command(SSD1306_SETCONTRAST) 
        self.command(0xCF) 
        self.command(SSD1306_SETPRECHARGE) 
        self.command(0xF1) 
        self.command(SSD1306_SETVCOMDETECT) 
        self.command(0x40) 
        self.command(SSD1306_DISPLAYALLON_RESUME) 
        self.command(SSD1306_NORMALDISPLAY) 
        self.command(SSD1306_DISPLAYON) 
        print("OLED Open succeeded")
    def OLED_DC_H(self):                                                                 # Control GPIO3 as output and set high
         OLED_0in96.SPI_Control.CH347T_GPIO(OLED_DC_Enable_x,OLED_DC_SetDirOut,OLED_DC_OUT_H)
    def OLED_DC_L(self):                                                                 # Control GPIO3 as output and set low
         OLED_0in96.SPI_Control.CH347T_GPIO(OLED_DC_Enable_x,OLED_DC_SetDirOut,OLED_DC_OUT_L)
    def OLED_Reset_H(self):                                                              # Control GPIO4 as output and set high
         OLED_0in96.SPI_Control.CH347T_GPIO(OLED_Reset_Enable_x,OLED_Reset_SetDirOut,OLED_Reset_OUT_H)
    def OLED_Reset_L(self):                                                              # Control GPIO4 as output and set low
         OLED_0in96.SPI_Control.CH347T_GPIO(OLED_Reset_Enable_x,OLED_Reset_SetDirOut,OLED_Reset_OUT_L)

    def reset(self):
        """Reset the display"""
        self.OLED_Reset_H()
        time.sleep(0.1)
        self.OLED_Reset_L()
        time.sleep(0.1)
        self.OLED_Reset_H()
        time.sleep(0.1)
    
    def SetWindows(self, Xstart, Xend, Ystart, Yend):
        self.command(SSD1306_COLUMNADDR) 
        self.command(Xstart)            #cloumn start address
        self.command(Xend-1)              #cloumn end address
        self.command(SSD1306_PAGEADDR) 
        self.command(Ystart)          #page atart address
        self.command(Yend-1)            #page end address

    def getbuffer(self, image):
        buf = [0xFF] * ((self.width//8) * self.height)
        image_monocolor = image.convert('1')
        imwidth, imheight = image_monocolor.size
        #print (imwidth, imheight )
        pixels = image_monocolor.load()
        """ 

        for y in range(imheight):
            for x in range(imwidth):
                if pixels[x,y] == 255 :
                    pixels[x,y] =1
                print(pixels[x,y]," ", end="") 
                if pixels[x,y] == 1 :
                    pixels[x,y] =255
            print()
        
        """
        
        if(imwidth == self.width and imheight == self.height):
            # print ("Vertical")
            for y in range(imheight):
                for x in range(imwidth):
                    # Set the bits for the column of pixels at the current position.
                    if pixels[x, y] == 0:
                        buf[x + (y // 8) * self.width] &= ~(1 << (y % 8))
                        
        elif(imwidth == self.height and imheight == self.width):
            # print ("Vertical")
            for y in range(imheight):
                for x in range(imwidth):
                    newx = y
                    newy = self.height - x - 1
                    if pixels[x, y] == 0:
                        buf[(newx + (newy // 8 )*self.width) ] &= ~(1 << (y % 8))
       

        """ 
        for z in range(self.height//8):
            for y in range(8):
                for x in range(self.width*z,self.width*(z+1)):
                    if (buf[x]&(1<<(y))) !=0:
                        print(1," ", end="") 
                    else :
                        print(0," ", end="") 
                print() 

        """
        return buf
        
    def ShowImage(self,Image):
        self.SetWindows(0, self.width, 0, self.height//8)
        """
        for x in range((self.width//8) * self.height):
            Image[x]=(Image[x] ^ ((1<<8)-1))
            print(Image[x],", "," ", end="") 
            if x%(self.width) == 0:
                print() 
            
        """
        rec = (c_byte * 1)()
        for i in range(0,self.width * self.height//8):
            rec[0]=~Image[i]
            self.OLED_DC_H()
            OLED_0in96.SPI_Control.SPI_WriteByte(rec)

    def clear(self):
        """Clear contents of image buffer"""
        _buffer = [0xff]*(self.width * self.height//8)
        self.ShowImage(_buffer)          
       
