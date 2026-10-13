#### Auto Minicom Logging with Date
minicom.sh<br>
I use it to automatically log all the sessions with the Winegard hardware. Great for helping document things and provides a backup in case something goes wrong.

Runs just like a regular Minicom command but automatically adds the date.
The destination folder for the log can be set by changing the `LOGDIR` in the script.
Example:
~/winegard/minicom.sh -b 57600 -D /dev/ttyUSB0

#### Chechsum Generator
checksum.sh<br>
I am making a lot of checksums so this is my solution.<br>
Run `checksum.sh -h` for usage.<br>
