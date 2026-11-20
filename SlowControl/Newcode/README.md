# KoNova Slow Control System

Temperature, humidity and pressure monitoring for the KoNova experiment.

## Hardware

- **2× BME280** — T/H/P in Electric Box 1 (0x76) and Electric Box 2 (0x77)
- **4× HDC302x** — T/H in Detector Boxes 0–3 (0x44–0x47)
- **CH347 USB-I2C bridge** (Waveshare module)

## Files

| File             | Purpose                                      |
|------------------|----------------------------------------------|
| `sensors.py`     | Driver classes: CH347, BME280, HDC302x       |
| `konova_daq.py`  | Main acquisition loop + HTTP server          |
| `config.json`    | Sensor addresses, offsets, logging settings   |
| `dashboard.html` | Browser-based live display                   |

## Quick Start

```bash
# 1. Make sure libch347.so is in the working directory (or on LD_LIBRARY_PATH)
cd konova_slowcontrol

# 2. Start the DAQ
python3 konova_daq.py

# 3. Open the dashboard in any browser on the same machine
xdg-open dashboard.html
# or from another machine on the network:
# http://<DAQ-IP>:8880/data   (raw JSON)
# open dashboard.html locally and change API url to http://<DAQ-IP>:8880
```

## Configuration

Edit `config.json` to change:
- I2C bus speed (`i2c_mode`: 0=20kHz, 1=100kHz, 2=400kHz, 3=1MHz)
- Sensor addresses and calibration offsets
- Logging directory and intervals

The DAQ also exposes `GET /config` and `POST /config` on port 8880
for future GUI-based configuration editing.

## Data Files

Daily TSV files are written to `./data/` with the naming pattern:
```
konova_2025-11-15.tsv
```

Columns: timestamp, then T/H/P for each BME280 and T/H for each HDC302x.

## Architecture

```
  sensors.py          konova_daq.py           dashboard.html
 ┌──────────┐      ┌───────────────┐       ┌──────────────┐
 │  CH347   │◄────►│  main loop    │       │  browser     │
 │  BME280  │      │  (5s cycle)   │──HTTP──►│  polls /data │
 │  HDC302x │      │               │  :8880 │  every 5s    │
 └──────────┘      │  DailyLogger  │       └──────────────┘
                   │  (30s writes) │
                   └──────┬────────┘
                          │
                     ./data/*.tsv
```

## Next Steps

- [ ] Alarm thresholds (configurable via GUI)
- [ ] Time-series plots in dashboard
- [ ] Config editor in GUI
- [ ] PCA9615 differential I2C for long cable runs



KoNova password: Muons2025!
TeamViewer password: Hodoscope2026

To reinstall the waveshare driver:
cd SlowControl/USB-TO-UART-I2C-SPI-JTAG-Demo/Raspberry/Driver/driver
make ( if necessary, if ch34x_pis.ko does not exist)
sudo cp ch34x_pis.ko /lib/modules/$(uname -r)/kernel/drivers/usb/drivers/
sudo depmod
sudo modprobe ch34x_pis
ls /dev/ch34*  (you should see /dev/ch34x_pis0)

# Write DKMS-compatible Makefile
sudo bash -c 'cat > /usr/src/ch34x_pis-1.0/Makefile << "MAKEOF"
ifneq ($(KERNELRELEASE),)
obj-m := ch34x_pis.o
else
KERNELDIR ?= /lib/modules/$(shell uname -r)/build
PWD := $(shell pwd)

default:
	$(MAKE) -C $(KERNELDIR) M=$(PWD) modules

clean:
	$(MAKE) -C $(KERNELDIR) M=$(PWD) clean
endif
MAKEOF'

sudo tee /usr/src/ch34x_pis-1.0/dkms.conf << 'EOF'
PACKAGE_NAME="ch34x_pis"
PACKAGE_VERSION="1.0"
BUILT_MODULE_NAME[0]="ch34x_pis"
DEST_MODULE_LOCATION[0]="/kernel/drivers/usb/serial"
AUTOINSTALL="yes"
MAKE[0]="make -C /lib/modules/${kernelver}/build M=${dkms_tree}/${PACKAGE_NAME}/${PACKAGE_VERSION}/build modules"
CLEAN="make -C /lib/modules/${kernelver}/build M=${dkms_tree}/${PACKAGE_NAME}/${PACKAGE_VERSION}/build clean"
EOF

sudo dkms add ch34x_pis/1.0
sudo dkms build ch34x_pis/1.0
sudo dkms install ch34x_pis/1.0
echo "ch34x_pis" | sudo tee /etc/modules-load.d/ch34x_pis.conf
