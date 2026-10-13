#### Spot Verify
spot_verify.py<br>
Spot checks a .bin using five random(time based) memory addresses against an active ODU connection.<br>
Will not work on IDUs, that firmware is extracted by JTAG.<br>
A bit more in-depth version of this built into `odu_dump.py` for resuming files.<br>

#### Header Parser
header_parser.py<br>
Uses a .bin file to extract the memory headers. I've only used it will full firmware extractions. Kinda pointless on partials?<br>
