# RF Unit interfacing via Pi Pico

DISCLAIMER: Use at your own risk!

## Preparation
1. Press and hold "BOOTSEL" button while plugging in your Pi Pico to the PC
2. Copy latest micropython uf2 file to Pi Pico's removable storage drive
3. Storage drive will disappear and Pico restarts - Micropython is now running!
4. Disconnect Pi Pico and solder the connections / bridge to the RF Unit according to the provided diagram
5. Plug back the Pico on the PC

## Dumping
1. Simply start the executable and wait a bit
2. On successful dump, dump.bin is saved alongside the executable

## Flashing
1. Copy flash.bin next to the executable
2. Start the executable and wait a bit
3. Enjoy limited-edition sounds

## Cleanup
- Unsolder the connections / bridge for normal operation of the RF unit before connecting it back to the console!
