This `dir` and its subs are for the ODU Firmware and other related files.<br>

Multiple version numbers can be found throughout the ODU debug terminal. The one used for file names is based off the `header` or `id` command.
- `header` is not shown with the `?` command. To see it and the description run `d?`.
  - `header` is not an available command on the older pre 2016 boards. That is when `id` is used for the version. 

The .bin files are labeled as follows:<br>
`(odu)_(#)_(ver)_(full/app).bin`<br>
- "full" is for the complete flash of the MCU, including the bootloader.
- "app" is for the portion of the MCU flash that we see in the debug menu.

IDU and ODU numbers do not correspond to each other unless there is a `@` with the numbers. See [ODU Wiki](https://github.com/jbu313mlb92ome8w/Winegard-Trav-ler/wiki/ODU) for more details.<br>

## Firmware Extraction Method
I advise creating a backup bin file and documenting the info like in the listed ODUs. Even if you don't delve too deep into the menus you never know what can happen.<br>

Run 'python3 odu_dump.py -h' for usage and examples.
- Use the `-v1` flag for older boards with "TRK>" prompt and takes around 30 minutes to run.
  - The primary MCU is a OKI L67Q5003A-NNN.
- Use the `-v3` flag for modern boards with "ODU>" prompt and takes around 2 hours to run.
  - The primary MCU is a NXP MK64FN1M0VLQ12.
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
I guarantee nothing. I advise to only do this if absolutely needed.<br>
I don't suggest running this to update App versions just because you can/want to. Thankfully this has worked in this one instance. I do not plan on testing this any further unless I brick another board. I don't see why this won't work for any of the NXP boards as long as the bootloader is intact. This method is for those that are experimenting and learning what the ODUs can do and mess something up.<br>

This currently only works for the ODU boards with the NXP MK64FN1M0VLQ12 MCU. The non NXP MCU boards operate differently.<br>
Successfully flashed extracted firmware from a MAY 2017 SK-1000 board to a bricked MAR 2016 SK-1000 board.<br>

Run `python3 odu_bl_upload.py --make-s19 input.bin output.s19` to create the needed file to stream to the ODU.
- Full or App bin files can be used for the s19 file. The booatloader only accepts the App section of the bin/s19.
- The bootloader protects the NVS and other settings, such as the steps per motor, even when erasing using the `ld` command.

Run `python3 odu_bl_upload.py /dev/PORT 57600 output.s19`
- Takes around 25 minutes to complete.
- Upon success the ODU will reboot on its own and show App boot output.
