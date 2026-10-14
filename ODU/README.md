This `dir` and its subs are for the ODU Firmware and other related files.<br>

Multiple version numbers can be found throughout the ODU debug terminal. The one used for file names is based off the `header` or `id` command.
- `header` and `id` are not shown with the `?` command. To see them and their description run `d?`.
  - `header` is not an available command on the older boards. That is when `id` is used for the version. 

The .bin files are labeled as follows:<br>
`(odu)_(#)_(ver)_(full/app).bin`<br>
- "full" is for the complete flash of the MCU, including the bootloader.
- "app" is for the portion of the MCU flash that we see in the debug menu.

IDU and ODU numbers do not correspond to each other unless there is a `@` with the numbers.<br>

See [ODU Wiki](https://github.com/jbu313mlb92ome8w/Winegard-Trav-ler/wiki/ODU) for more details.<br>

## Firmware Extraction Method
This is still in testing.<br>
I plan to use the extracted firmware from a MAY 2017 SK-1000 board and flash it to a bricked MAR 2016 SK-1000 board. Hopefully the only differences between the two is the slight layout change.<br>

Run 'odu_dump.py -h' for usage and examples.
- Use the `-v1` flag for older boards with "TRK>" prompt and takes around 30 minutes to run. 
- Use the `-v3` flag for modern boards with "ODU>" prompt and takes around 2 hours to run.
- The difference is because of the ODU TX buffer size limit and flash sizes of the MCUs.

Overview: Extract the firmware in chunks using the `dw` command. The chunks are read from the serial output and wrote to the destination .bin file.
- `dw` is not shown with the `?` command. To see `dw` and its description run `d?`.
  - Description: `dw` [addr [lines]] Dump memory (words)
  - Only READS from the flash; has no option to write.
  - Example output using `dw 0 1`
    - 00000000: fff0 2002 2adf 0000 f000 f802 f000 fa06

Notes:
- I am running the script on a dismounted ODU and connected directly to the ODU debug port.
- The ODU does get warm during the extraction, but I would say no warmer than it being in the turret on summer days (probably gets hotter).

## Firmware Flash to ODU Method
Coming soon. Hopefully.
