# ch347 linux SDK

## Description

This directory contains 1 parts, usb device driver. This driver and applications support usb bus interface chip ch347,  ch347 implements high-speed usb to uart/jtag/spi/i2c/gpio/etc. This driver is not applicable to uart.

## Driver Operating Overview

1. Open "Terminal"
2. Switch to "driver" directory
3. Compile the driver using "make", you will see the module "ch34x_pis.ko" if successful
4. Type "sudo make load" or "sudo insmod ch34x_pis.ko" to load the driver dynamically
5. Type "sudo make unload" or "sudo rmmod ch34x_pis.ko" to unload the driver
6. Type "sudo make install" to make the driver work permanently
7. Type "sudo make uninstall" to remove the driver

Before the driver works, you should make sure that the ch341/ch347 device has been plugged in and is working properly, you can use shell command "lsusb" or "dmesg" to confirm that, VID of ch341/ch347 is [1A86].

If ch341/ch347 device works well, the driver will created devices named "ch34x_pis*" in /dev directory.
