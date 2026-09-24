#!/usr/bin/env python3
"""Read bootloader metadata or change its CAN board ID using a PEAK PCAN adapter.

Install the dependency with ``python -m pip install -r tools/requirements.txt``.
The PEAK PCAN Basic driver must also be installed on the host machine.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
import zlib
from pathlib import Path
from typing import Sequence


CAN_READ_RECORD_ID = 0x19040000
CAN_SET_BOARD_ID_ID = 0x19050000
CAN_START_FLASH_ID = 0x19010000
CAN_DATA_ID = 0x19020000
CAN_FINISH_ID = 0x19030000
CAN_REPLY_BASE = 0x19000000
BOARD_COUNT = 32
WORD_COUNT = 19
MAX_IMAGE_SIZE = 0x40000

STATUS_OK = 0
STATUS_NO_RECORD = 1
STATUS_BAD_REQUEST = 2
STATUS_SET_ID_FILTER_FAILED = 2
STATUS_SET_ID_WRITE_FAILED = 3

WORD_NAMES = (
    "magic",
    "format_version",
    "record_size",
    "sequence",
    "board_id",
    "active_slot",
    "update_slot",
    "transaction_state",
    "valid_slots",
    "image_size_a",
    "image_size_b",
    "image_crc32_a",
    "image_crc32_b",
    "image_version_a",
    "image_version_b",
    "reserved_0",
    "reserved_1",
    "record_crc32",
    "commit_word",
)


class ToolError(Exception):
    """A user-facing PCAN or protocol error."""


def load_can_module():
    try:
        import can
    except ImportError as exc:
        raise ToolError(
            "python-can is missing; install it with "
            "python -m pip install -r tools/requirements.txt"
        ) from exc
    return can


def open_bus(args):
    can = load_can_module()
    try:
        return can.Bus(
            interface="pcan",
            channel=args.channel,
            bitrate=args.bitrate,
        )
    except Exception as exc:
        raise ToolError(
            f"Could not open PCAN channel {args.channel!r}: {exc}"
        ) from exc


def send_frame(bus, arbitration_id: int, payload: bytes) -> None:
    can = load_can_module()
    message = can.Message(
        arbitration_id=arbitration_id,
        is_extended_id=True,
        data=payload,
    )
    try:
        bus.send(message, timeout=1.0)
    except Exception as exc:
        raise ToolError(f"CAN transmit failed: {exc}") from exc


def receive_reply(bus, expected_id: int, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ToolError(
                f"Timed out waiting for reply on extended ID 0x{expected_id:08X}. "
                "Check the board ID, CAN bitrate, wiring, and that the "
                "configuration firmware is running."
            )
        try:
            message = bus.recv(timeout=remaining)
        except Exception as exc:
            raise ToolError(f"CAN receive failed: {exc}") from exc
        if message is None:
            continue
        if message.is_extended_id and message.arbitration_id == expected_id:
            return message


def read_word(bus, board_id: int, index: int, timeout: float) -> tuple[int, int]:
    command_id = CAN_READ_RECORD_ID + board_id
    reply_id = CAN_REPLY_BASE + board_id
    send_frame(bus, command_id, bytes((index,)))
    reply = receive_reply(bus, reply_id, timeout)

    if len(reply.data) != 6:
        raise ToolError(f"Read reply has DLC {len(reply.data)}; expected 6.")

    received_index, status = reply.data[:2]
    value = struct.unpack_from("<I", reply.data, 2)[0]
    if received_index != index:
        raise ToolError(
            f"Read reply index {received_index} did not match requested index {index}."
        )
    if status == STATUS_NO_RECORD:
        raise ToolError("The device has no valid configuration record yet.")
    if status == STATUS_BAD_REQUEST:
        raise ToolError(f"The device rejected metadata word index {index}.")
    if status != STATUS_OK:
        raise ToolError(f"Device returned unknown read status {status}.")
    return status, value


def read_record(bus, board_id: int, timeout: float) -> None:
    values = []
    for index in range(WORD_COUNT):
        _, value = read_word(bus, board_id, index, timeout)
        values.append(value)

    print(f"Configuration record for board ID {board_id}:")
    for name, value in zip(WORD_NAMES, values):
        print(f"  {name:18} 0x{value:08X} ({value})")


def set_board_id(bus, current_id: int, requested_id: int, timeout: float) -> None:
    command_id = CAN_SET_BOARD_ID_ID + current_id
    reply_id = CAN_REPLY_BASE + current_id
    send_frame(bus, command_id, struct.pack("<I", requested_id))
    reply = receive_reply(bus, reply_id, timeout)

    if len(reply.data) != 7:
        raise ToolError(f"Set-ID reply has DLC {len(reply.data)}; expected 7.")

    status = reply.data[0]
    old_id = reply.data[1]
    returned_requested_id = struct.unpack_from("<I", reply.data, 2)[0]
    effective_id = reply.data[6]
    if old_id != current_id or returned_requested_id != requested_id:
        raise ToolError("Set-ID reply did not match the request.")

    if status == STATUS_OK:
        if effective_id != requested_id:
            raise ToolError(
                f"Device reported success but effective ID is {effective_id}."
            )
        print(f"Board ID changed from {old_id} to {effective_id} and saved.")
    elif status == STATUS_BAD_REQUEST:
        raise ToolError("Device rejected the requested board ID.")
    elif status == STATUS_SET_ID_FILTER_FAILED:
        raise ToolError("Device could not configure the CAN filter for that ID.")
    elif status == STATUS_SET_ID_WRITE_FAILED:
        raise ToolError("Device could not save the new board ID to flash.")
    else:
        raise ToolError(f"Device returned unknown set-ID status {status}.")


def start_flash(bus, board_id: int, timeout: float) -> int:
    command_id = CAN_START_FLASH_ID + board_id
    reply_id = CAN_REPLY_BASE + board_id
    send_frame(bus, command_id, b"")
    reply = receive_reply(bus, reply_id, timeout)

    if len(reply.data) != 8:
        raise ToolError(f"Start-flash reply has DLC {len(reply.data)}; expected 8.")
    returned_board_id, slot = struct.unpack("<II", reply.data)
    if returned_board_id != board_id:
        raise ToolError(
            f"Start-flash reply board ID {returned_board_id} did not match {board_id}."
        )
    if slot not in (0, 1):
        raise ToolError(f"Device returned invalid flash slot {slot}.")

    slot_name = "A" if slot == 0 else "B"
    print(f"Device {board_id} selected flash slot {slot} ({slot_name}).")
    return slot


def finish_flash(bus, board_id: int, image_size: int, image_crc: int,
                 timeout: float) -> int:
    reply_id = CAN_REPLY_BASE + board_id
    send_frame(
        bus,
        CAN_FINISH_ID + board_id,
        struct.pack("<II", image_size, image_crc),
    )
    reply = receive_reply(bus, reply_id, timeout)
    if len(reply.data) != 5:
        raise ToolError(f"Finish reply has DLC {len(reply.data)}; expected 5.")
    status = reply.data[0]
    returned_size = struct.unpack_from("<I", reply.data, 1)[0]
    if returned_size != image_size:
        raise ToolError(
            f"Finish reply size {returned_size} did not match {image_size}."
        )
    return status


def show_progress(completed: int, total: int, started_at: float) -> None:
    width = 30
    fraction = completed / total
    filled = int(width * fraction)
    bar = "#" * filled + "-" * (width - filled)
    elapsed = max(time.monotonic() - started_at, 0.001)
    rate_kb_s = completed / 1000.0 / elapsed
    print(
        f"\r[{bar}] {fraction * 100:5.1f}%  {completed}/{total} bytes  "
        f"{elapsed:6.1f}s  {rate_kb_s:7.2f} KB/s",
        end="",
        file=sys.stderr,
        flush=True,
    )


def load_image(image_path: Path) -> bytes:
    try:
        image = image_path.read_bytes()
    except OSError as exc:
        raise ToolError(f"Could not read image file {image_path}: {exc}") from exc
    if not image:
        raise ToolError("Image file is empty.")
    if len(image) > MAX_IMAGE_SIZE:
        raise ToolError(
            f"Image is {len(image)} bytes; maximum supported size is "
            f"{MAX_IMAGE_SIZE} bytes."
        )
    return image


def write_started_flash(bus, board_id: int, image: bytes, slot: int,
                        timeout: float) -> None:
    reply_id = CAN_REPLY_BASE + board_id
    expected_crc = zlib.crc32(image) & 0xFFFFFFFF
    transfer_started = time.monotonic()
    show_progress(0, len(image), transfer_started)
    try:
        for offset in range(0, len(image), 8):
            chunk = image[offset : offset + 8].ljust(8, b"\xFF")
            send_frame(bus, CAN_DATA_ID + board_id, chunk)
            reply = receive_reply(bus, reply_id, timeout)
            if len(reply.data) != 5:
                raise ToolError(
                    f"Data reply at offset {offset} has DLC "
                    f"{len(reply.data)}; expected 5."
                )
            status = reply.data[0]
            next_offset = struct.unpack_from("<I", reply.data, 1)[0]
            expected_offset = offset + 8
            if status != STATUS_OK:
                raise ToolError(
                    f"Device rejected data at offset {offset}; "
                    f"it still expects offset {next_offset}."
                )
            if next_offset != expected_offset:
                raise ToolError(
                    f"Unexpected next offset {next_offset}; "
                    f"expected {expected_offset}."
                )
            show_progress(
                min(expected_offset, len(image)), len(image), transfer_started
            )
    except ToolError:
        print(file=sys.stderr)
        # A rejected or interrupted stream still gets a stop request so
        # the device can leave FLASHING and return to its command state.
        try:
            finish_flash(bus, board_id, len(image), expected_crc, timeout)
        except ToolError:
            pass
        raise

    print(file=sys.stderr)
    status = finish_flash(bus, board_id, len(image), expected_crc, timeout)
    if status != STATUS_OK:
        raise ToolError("Device rejected the image size or CRC-32.")

    slot_name = "A" if slot == 0 else "B"
    print(
        f"Image written: {len(image)} bytes, CRC-32 0x{expected_crc:08X}, "
        f"slot {slot_name}."
    )


def flash_images(bus, board_id: int, bank_a_path: Path, bank_b_path: Path,
                 timeout: float) -> None:
    # Validate both paths before starting a transaction, so a missing or
    # invalid second image cannot leave the device with only one bank updated.
    images = {
        0: load_image(bank_a_path),
        1: load_image(bank_b_path),
    }
    written_slots = set()
    while len(written_slots) < 2:
        slot = start_flash(bus, board_id, timeout)
        if slot in written_slots:
            raise ToolError(
                f"Device selected slot {'A' if slot == 0 else 'B'} twice; "
                "cannot write both bank images."
            )
        write_started_flash(bus, board_id, images[slot], slot, timeout)
        written_slots.add(slot)


def board_id_arg(value: str) -> int:
    try:
        board_id = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("board ID must be an integer") from exc
    if not 0 <= board_id < BOARD_COUNT:
        raise argparse.ArgumentTypeError("board ID must be from 0 through 31")
    return board_id


def add_connection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--channel",
        default="PCAN_USBBUS1",
        help="PCAN channel name (default: %(default)s)",
    )
    parser.add_argument(
        "--bitrate",
        type=int,
        default=500_000,
        help="CAN bitrate in bits/second (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="reply timeout in seconds for each command (default: %(default)s)",
    )


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read bootloader config, set its board ID, start flash mode, "
            "or write both bank images over PEAK PCAN."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    read_parser = commands.add_parser("read", help="read all 19 metadata words")
    read_parser.add_argument("--board-id", required=True, type=board_id_arg)
    add_connection_options(read_parser)

    set_parser = commands.add_parser("set-board-id", help="save a new board ID")
    set_parser.add_argument("--board-id", required=True, type=board_id_arg,
                            help="current ID used to address the device")
    set_parser.add_argument("--new-board-id", required=True, type=board_id_arg)
    add_connection_options(set_parser)

    start_parser = commands.add_parser(
        "start-flash", help="select and return the target flash slot"
    )
    start_parser.add_argument("--board-id", required=True, type=board_id_arg)
    add_connection_options(start_parser)

    flash_parser = commands.add_parser(
        "flash", help="erase and write both bank images"
    )
    flash_parser.add_argument("--board-id", required=True, type=board_id_arg)
    flash_parser.add_argument("--bank-a", required=True, type=Path,
                              help="binary linked for flash bank A")
    flash_parser.add_argument("--bank-b", required=True, type=Path,
                              help="binary linked for flash bank B")
    add_connection_options(flash_parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.bitrate <= 0:
        parser.error("--bitrate must be greater than zero")

    try:
        bus = open_bus(args)
        try:
            if args.command == "read":
                read_record(bus, args.board_id, args.timeout)
            elif args.command == "set-board-id":
                set_board_id(bus, args.board_id, args.new_board_id, args.timeout)
            elif args.command == "start-flash":
                start_flash(bus, args.board_id, args.timeout)
            else:
                flash_images(bus, args.board_id, args.bank_a, args.bank_b,
                             args.timeout)
        finally:
            bus.shutdown()
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
