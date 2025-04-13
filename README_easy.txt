# RF Unit dumping via Pi Pico

1. Press and hold "BOOTSEL" button while plugging in your Pi Pico to the PC
2. Copy latest micropython uf2 file to Pi Pico's removable storage drive
3. Storage drive will disappear and Pico restarts - Micropython is now running!
4. Disconnect Pi Pico and solder the connections / bridge to the RF Unit according to the provided diagram
5. Plug back the Pico on the PC
6. Start the dumping executable and wait a bit
7. On successful dump, dump.bin is saved alongside the executable
8. Unsolder the connections / bridge for normal operation of the RF unit before connecting it back to the console!
