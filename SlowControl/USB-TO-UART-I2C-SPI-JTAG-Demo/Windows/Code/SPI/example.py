import os
import logging    
import time
from ctypes import *
import OLED_Library_Function
from PIL import Image,ImageDraw,ImageFont

picdir = "./pic" # Please change to the new image file path
logging.basicConfig(level=logging.DEBUG)
""" Update data to the image to be displayed """


try:
# SPI Control OLED
    disp = OLED_Library_Function.OLED_0in96()

    logging.info("\r 0.96inch OLED ")
    # Initialize library.
    if ( disp.Init() == -1):
        raise Exception ("InitFail")
    if ( disp.Init() == -1):
        raise Exception ("InitFail")
    # Clear display.
    logging.info("clear display")
    disp.clear()

    # Create blank image for drawing.
    image1 = Image.new('1', (disp.width, disp.height), "WHITE")             #1 Creates a binary (parameter: 1) blank image of size (parameter: disp.width, disp.height) with an initial color of WHITE (parameter: WHITE).
    draw = ImageDraw.Draw(image1)                                           #1 This line of code creates a drawing object, draw, to perform drawing operations on the image
    font1 = ImageFont.truetype(os.path.join(picdir, 'Font.ttc'), 18)        #2 Two fonts, 18 and 24, are loaded. The Font file path is concatenated with 'Font.ttc' using the os.path.join() function, which may be defined in the picdir variable.
    font2 = ImageFont.truetype(os.path.join(picdir, 'Font.ttc'), 24)
    logging.info ("***draw line")                                           #1 Log information recording
    draw.line([(0,0),(127,0)], fill = 0)                                    #4 Using the draw.line() function to draw four line segments, draw a rectangular box (parameters: start coordinates, end coordinates, line color)
    draw.line([(0,0),(0,63)], fill = 0)
    draw.line([(0,63),(127,63)], fill = 0)
    draw.line([(127,0),(127,63)], fill = 0)
    logging.info ("***draw text")                                           #1 Log information recording
    draw.text((20,0), 'Waveshare ', font = font1, fill = 0)                 #2 These two lines of code draw two lines of text over the image. The draw.text() function takes four parameters: starting coordinates, text content, font, and color
    draw.text((20,24), u'微雪电子 ', font = font2, fill = 0)
    image1 = image1.rotate(0)                                               #1 This line of code rotates the image. The rotate() method takes an Angle argument, where 0 is passed to indicate no rotation.
    disp.ShowImage(disp.getbuffer(image1))                                  #1 Use the ShowImage() method of the display object to display the image. The getbuffer() method converts the image into a data format suitable for display.
    time.sleep(2)
    
    logging.info ("***draw image")                                          #1 Log information recording
    Himage2 = Image.new('1', (disp.width, disp.height), 255)                #1 Creates a binary (parameter: 1) blank image of size (parameter: disp.width, disp.height) with an initial color of WHITE (parameter: 255).
    bmp = Image.open(os.path.join(picdir, '0in96.bmp'))                     #1 open an image file named "0in96.bmp" using the open() function of the PIL library. The picdir path variable is concatenated with the image by the os.path.join() function. This image file will be inserted into the Himage2 image.
    Himage2.paste(bmp, (0,0))                                               #1 This line of code inserts the open image file bmp into the Himage2 image using the paste() method, starting at (0,0).
    Himage2=Himage2.rotate(0) 	                                            #1 This line of code rotates the image. The rotate() method takes an Angle argument, where 0 is passed to indicate no rotation.
    disp.ShowImage(disp.getbuffer(Himage2))                                 #1 Use the ShowImage() method of the display object to display the image. The getbuffer() method converts the image into a data format suitable for display.
    time.sleep(2)


# major cycle

    
    Data = (c_float * 3)()
    while True :
        disp.ShowImage(disp.getbuffer(image1))                         #1 Use the ShowImage() method of the display object to display the image. The getbuffer() method converts the image into a data format suitable for display.
        time.sleep(2)
        disp.ShowImage(disp.getbuffer(Himage2))                           #1 Use the ShowImage() method of the display object to display the image. The getbuffer() method converts the image into a data format suitable for display.
        time.sleep(2)

except IOError as e:
    logging.info(e)
    
except KeyboardInterrupt:    
    logging.info("ctrl + c:")
    disp.OLED_Reset_L()
    disp.OLED_DC_L()
    disp.SPI_Control.Close_SPI()
    exit()
except Exception :
    logging.info ("Please re-run!!!!") 
    logging.info ("Please re-run!!!!") 
    logging.info ("Please re-run!!!!") 
    logging.info ("Please re-run!!!!") 
    logging.info ("Please re-run!!!!") 
    logging.info ("Please re-run!!!!") 
    logging.info ("Please re-run!!!!") 
    logging.info ("Please re-run!!!!") 
    logging.info ("Please re-run!!!!") 
    logging.info ("Please re-run!!!!") 
    logging.info ("Please re-run!!!!") 
    disp.OLED_Reset_L()
    disp.OLED_DC_L()
    disp.SPI_Control.Close_SPI()
    exit()
