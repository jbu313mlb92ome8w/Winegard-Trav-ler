#### Spot Verify
spot_verify.py<br>
Spot checks a .bin using five random(time based) memory addresses against an active ODU connection.<br>
Will not work on IDUs, that firmware is extracted by JTAG.<br>
A bit more in-depth version of this built into `odu_dump.py` for resuming files.<br>

#### Header Parser
header_parser.py<br>
Uses a .bin file to extract the memory headers. I've only used it with full firmware extractions. Kinda pointless on partials?<br>

#### Angle to Step(atos) Table
atos_table.py<br>
Creates tables of angle(s) to step(s) for the motors. Detects steps automatically and knows if SM or LG model; relevant to the EL motor.<br>
Run `python3 atos_table.py -h` for different options.<br>

#### DCMC Stress
dcmc_stress.py<br>
Used to stress test and find working limits of Trav'ler to fine-tune dcmc_rotor script. Flags are a bit hectic, but allows for lots of configurations to be tested. Probably not needed by anyone else.<br>
Run `python3 dcmc_stress.py -h` for help.

#### rotctl
dcmc_rotor.py<br>
The .csv files are required for operation. See folder README.md for details.
