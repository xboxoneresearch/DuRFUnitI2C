

# Durango RF Unit I2C tooling

> [!WARNING]
> Use at your own risk!

Technical infos: <https://xboxoneresearch.github.io/wiki/hardware/rf-unit/>

## Requirements

- (Xbox One PHAT/One S) RF Unit board

I2C Device clients:
- GreatFET One board
or
- Raspberry Pi (untested)
or
- Micropython device (ESP8266, ESP32, Pi Pico ..)

Depending on the device, pull-up resistors might be necessary.

## Connections

| RF Unit      | Greatfet ONE | RasPi                    | RasPi Pico      | ESP8266     | Notes                            |
| ------------ | ------------ | ------------------------ | --------------- | ----------- |----------------------------------|
| Pin 4  (5V)  | 5V           | 5V                       | 5V              | 5V          |Only necessary for sound playback |
| Pin 12 (3V3) | 3.3V         | 3.3V                     | 3.3V            | 3.3V        |                                  |
| GND 9  (GND) | Any GND      | Any GND                  | Any GND         | Any GND     |                                  |
| Pin 6  (SDA) | SDA (Pin 39) | GPIO2 (I2C1 SDA) - Pin 3 | Pin 1 (GP0)     | GPIO 4      |                                  |
| Pin 5  (SCL) | SCL (Pin 40) | GPIO3 (I2C1 SCL) - Pin 5 | Pin 2 (GP1)     | GPIO 5      |                                  |

### Pi Pico diagram

Xbox One PHAT

![Pi Pico Rf Unit connection diagram PHAT](./pi_pico_diagram_phat.png)

Xbox One S

![Pi Pico Rf Unit connection diagram One S](./pi_pico_diagram_one_s.png)

## Features

- Play sounds
- Dump flash
- Write flash

## Usage

- Solder I2C connections and 5V/3.3V/GND
- Install python requirements, preferrably in a python venv
  - GreatFET: `pip install greatfet`
  - Raspberry Pi (not Pico): `pip install smbus2`
  - Micropython: `pip install pyserial tqdm`
- For flashing: Copy `flash.bin` next to script/executable
- Execute `rfunit.py` (see below for micropython)

### Micropython

Check [Pyboard tool](https://docs.micropython.org/en/latest/reference/pyboard.py.html) for an overview.

Easy:

- For flashing: Copy `flash.bin` next to script/executable
- Execute `micropython_rfunit.py`

Manual:

- Identify the serial port where you micropython device got enumerated (via `dmesg`), f.e. `/dev/ttyACM0`

- Dumping
  - Execute the script: `./vendor/pyboard.py --device /dev/ttyACM0 rfunit.py`
  - Copy the dump to the PC: `./vendor/pyboard.py --device /dev/ttyACM0 -f cp :dump.bin .`
- Flashing
  - Copy flash.bin to the Micropython device: `./vendor/pyboard.py --device /dev/ttyACM0 -f cp flash.bin :flash.bin`
  - Execute the script: `./vendor/pyboard.py --device /dev/ttyACM0 rfunit.py`

## Flashdump

Size: 0x24400

Checksum (SHA256)
```
8150af1889203260bbbd25884447c0540f1cbc843a13e86bb0b6d501a6d032e1  dump_phat_cod_aw.bin
3789e76fd6c37bdf39751afd6164c1d8d67a53c31eca5b630901210b55753dba  dump_phat_forza6.bin
388d252d7783fd2946b9b3fca57ecd1ed13a4f17ea6665f577da34224f9930da  dump_phat_halo5.bin
abc699513959372faee038c78a1d7509c2020f65cb78ad07ab9c90b21b406a87  dump_phat_retail.bin
4812af697716464dff1a49e0ab2f45497492fec71aa0a73308f522db45d2b42e  dump_one_s_minecraft.bin
c39871fcfef69c632955658f8e876d35a35dafb9c88c7fc082dca23d2102289f  dump_one_x_tacobell.bin
```

Some strings
```
ISD9160FIMS03 FW Jun 14 2013 at 10:41:12 (C) Nuvoton 2013
Nuvoton ISD9160MS Boot FW Jun 14 2013 10:40:21 
ISD-VPE Ver 920.000c 08/05/2013 PV_Prod_Units_Rev5 VERSION:0x10000007
```
