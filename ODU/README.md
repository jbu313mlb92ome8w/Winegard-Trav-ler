This `dir` and its subs are for the ODU Firmware and other related files.<br>

Multiple version numbers can be found throughout the ODU debug terminal. The one used for file names is based off the `header` command from the `ODU>` (main menu) prompt.
- `header` is not shown with the `?` command. To see `header` and its description run `d?`.

The bin files are labeled as follows:<br>
(IDU/ODU)_(#)_(ver)_(oe/pf).bin<br>
  - "oe" is for `.bin` files that have not used (to my knowledge) the "Reflash Mode" on the IDU.
  - "pf" is for IDUs the have used the "Reflash Mode" or ODUs that have been flashed by either the IDU or, if determined to be an option, by a user.<br>

IDU and ODU numbers do not correspond to each other unless there is a `@` with the numbers.<br>

See [ODU](https://github.com/jbu313mlb92ome8w/Winegard-Trav-ler/wiki/Firmware/ODU) for more details.<br>

## Firmware Extraction Method

Use the `odu_dump.py` python script.<br>

Example:<br>
python3 odu_dump.py /dev/ttyUSB0 57600 odu_full.bin 0x00000000 0x00100000
- This is run in the folder with the script and the output file is put into the same folder
- 0x00000000 0x00100000 reads the full 1MB flash off the NXP MK64FN1M0VLQ12.
  - Both the ODU versions of MAR 2016 and MAY 2017 use the NXP MK64FN1M0VLQ12.
  - The ODU board version of NOV 2012 DOES NOT use the NXP MK64FN1M0VLQ12.
- This script takes a VERY long time to run. Start it and go do something else.

Notes:
- I am running the script on a dismounted ODU and connected directly to the ODU debug port.
- The ODU does get warm during the extraction, but I would say no warmer than it being in the turret on summer days (probably gets hotter).
- In the script there are two lines labeled `LINES_PER_CMD`.
  - Recommend/Default: The "Safe" option takes longer but makes sure that the serial buffer doesn't overflow.
  - The "Max" option is the maximum lines that can be output before the serial buffer overflows. Probably won't cause an issue, but safe than sorry. 

Script currently being tested with MAY 2017 board version.

## Firmware Flash to ODU Method
Coming soon. Hopefully.
