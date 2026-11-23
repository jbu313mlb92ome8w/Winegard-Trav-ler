#### Use
Method to control Trav'ler using the dcmc menu using the rotctl protocol that is widely used.<br>
The .csv files are required for operation. They are a part of ensuring safe movements made by programs that give position data/movements. Custom tables can be generated  (see atos_table.py) or the included can be modified to suite your needs.<br>
While using the dcmc menu does allow for both elevation and azimuth moves to be done at the same time there appears to be a hard limit of 14.9° max elevation before triggering an elevation motor stall. This script accounts for that limitation by adding a delay between sending the elevation and azimuth movements, only if that criteria is met. The lowest delay I was able to find that worked is 1.1 seconds; this may also be a firmware limitation. If you encounter elevation motor stall events or find that the elevation does not move during 15°+ elevation movements trying increasing the `START_GAP_SEC` variable. As far as I know, this limitation only applies to going to a home/park position or positioning for another pass; either are not time sensitive events and the delay does not matter. Wait... meridian flips.. working on it.<br>

#### Config
- DCMC_ALLOWED = {"p", "s", "i", "t"} = Safeguard to make sure no other dcmc commands can be run. Don't change.
- HEADING_DEG = True north heading.
- AZ_MIN_DEG = Safeguard minimum azimuth degree. Must also be in atos csv file to go to value.
- AZ_MAX_DEG = Safeguard maximum azimuth degree. Must also be in atos csv file to go to value.
- EL_MIN_DEG = Safeguard minimum elevation degree. Must also be in atos csv file to go to value.
- EL_MAX_DEG = Safeguard maximum elevation degree. Must also be in atos csv file to go to value.
- DEADBAND_DEG = Incremental degrees to go to.
- EL_CONCURRENT_MAX_DEG = Elevation maximum degree difference before triggering START_GAP_SEC delay. Max is 14.9 degrees, anything over will trigger elevation motor stall.
- START_GAP_SEC = Time delay for large continuous movements. Min is 1.1 seconds.
- SETTLE = serial command delay.
- PARK_AZ_DEG = Azimuth park position when no commands have been received after IDLE_PARK_SEC is met. Flag needed.
- PARK_EL_DEG = Elevation park position when no commands have been received after IDLE_PARK_SEC is met. Flag needed.
- IDLE_PARK_SEC = Time to wait until moving to park position.
- MOTOR_AZ = Azimuth motor number. Feeds several features. Don't change.
- MOTOR_EL = Elevation motor number. Feeds several features. Don't change.
- EL_STEPS_TO_PKG = {20800: "lg", 10750: "sm"} = Used to determine is what Trav'ler elevation motors are present. Feeds several features. Don't change.

#### Safeguards
Multiple layers of safeguards have been implemented, even at the cost of some VERY minor increased processing.
- Only certain set of allowed dcmc commands.
- Script config limits.
- Angle to Step(atos) tables that only have safe positions to go to, degrees and motor steps. Information on default atos tables:
  - Azimuth: 5 to 445
  - Elevation: -5 to 94
  - Skew: -90 to 90
    - No skew data is currently output by any known program. These are safe values though.
  - The tables are generated at 0.1 steps increments to help with rounding and best accuracy. 
  - lg_*.csv if for LG models and sm_*.csv is for SM models. This matters do not misname custom atos tables.

#### Limitations
This script does not account for passes that cross the azimuth limits. That is a limit of receiving live position data from the controlling software. A software that preprocesses passes and then calculates the best start position to prevent or minimize azimuth resets needs to be used.
