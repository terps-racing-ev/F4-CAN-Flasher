# Bootloader config CLI

Install `python-can` and the PEAK PCAN Basic driver:

```powershell
python -m pip install -r requirements.txt
```

Read all 19 words of the metadata record:

```powershell
python flasher.py read --board-id 1
```

Set a device's board ID (the `--board-id` value is its current ID):

```powershell
python flasher.py set-board-id --board-id 1 --new-board-id 2
```

Start the flash state and report the selected target slot:

```powershell
python flasher.py start-flash --board-id 1 --image-version 42
```

This sends an extended CAN data frame on `0x19010000 + board_id` with DLC 4:
the required 32-bit image version in little endian byte order. The bootloader
saves it as `image_version` for the selected slot in the metadata record.
The board replies on `0x19000000 + board_id` with its ID and selected slot
(`0` = A, `1` = B), then enters `FLASHING`.

Write both bank images. Each binary must be linked for its named slot; the tool
uses the slot returned by the bootloader to send the matching image. It runs two
flash transactions, waiting for an ACK after every 8-byte frame, then sends each
image's size and CRC-32:

```powershell
python flasher.py flash --board-id 1 --image-version 42 --bank-a .\firmware_a.bin --bank-b .\firmware_b.bin
```

The `flash` command records the same image version for both slots.

For each transaction, the firmware erases the inactive slot, programs and
verifies each frame, then checks the final image CRC before marking the slot
valid in metadata. A progress bar tracks bytes acknowledged by the device,
elapsed transfer time, and average throughput in decimal KB/s. Each image is
limited to 256 KiB, matching either two-sector slot.

After five seconds without a bootloader command, the device checks the active
image's CRC and vector table and jumps to it. If that image is invalid, it
tries the other valid slot; if neither slot contains a valid app, it stays in
the bootloader. Each binary must be linked for the slot address returned by
`start-flash` (slot A: `0x08020000`, slot B: `0x08060000`).

Select another PCAN channel, bitrate, or reply timeout with `--channel`,
`--bitrate`, and `--timeout`. Defaults are `PCAN_USBBUS1`, 500000 bits/s, and
10 seconds per command to allow time for slot erase. The script sends standard
bootloader extended CAN data frames and waits for each response before sending
the next command.
