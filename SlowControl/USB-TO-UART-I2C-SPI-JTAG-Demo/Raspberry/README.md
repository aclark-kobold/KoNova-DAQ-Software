# ch347 linux SDK

## Description

This directory contains 2 parts, application library and examples. This driver and applications support usb bus interface chip ch347,ch347 implements high-speed usb to uart/jtag/spi/i2c/gpio/etc. This driver is not applicable to uart.


## Application Operating Overview

1. Copy the dynamic library in "lib" directory to system default library path: "sudo cp libch347.so /usr/lib"
2. Switch to "demo" directory
3. Type "gcc ch347_demo.c -o app -lch347" to compile the demo

## Note

**ch341 supports 2 modes:**

mode0: [uart]

mode1: [spi + i2c + gpio]

**ch347 supports 4 modes:**

mode0: [uart * 2] in vcp/cdc driver mode, the devices are named /dev/tty*

mode1: [spi + i2c + uart * 1] in vcp driver mode, the devices are named /dev/ch34x_pis* and /dev/tty*

mode2: [spi + i2c + uart * 1] in hid driver mode, the devices are named /dev/hidraw*

mode3: [jtag + uart * 1] in vcp driver mode, the devices are named /dev/ch34x_pis* and /dev/tty*

This driver only can be used with **ch341 mode1** or **ch347 mode1/mode3**.

This application library and examples can be used with all modes.

Any question, you can send feedback to mail: tech@wch.cn
