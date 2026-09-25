"""Protocol checks that do not require a CAN adapter."""

import argparse
import struct
import unittest
from unittest.mock import patch

import flasher


class StartFlashTests(unittest.TestCase):
    def test_start_flash_sends_version_as_little_endian_word(self):
        reply = type("Reply", (), {"data": struct.pack("<II", 3, 1)})()
        with patch.object(flasher, "send_frame") as send, patch.object(
            flasher, "receive_reply", return_value=reply
        ) as receive:
            slot = flasher.start_flash(None, 3, 0x12345678, 1.0)

        self.assertEqual(slot, 1)
        send.assert_called_once_with(
            None, flasher.CAN_START_FLASH_ID + 3, b"\x78\x56\x34\x12"
        )
        receive.assert_called_once_with(None, flasher.CAN_REPLY_BASE + 3, 1.0)

    def test_version_required_for_both_flash_commands(self):
        parser = flasher.make_parser()
        for command in (
            ["start-flash", "--board-id", "3"],
            ["flash", "--board-id", "3", "--bank-a", "a", "--bank-b", "b"],
        ):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                parser.parse_args(command)

    def test_version_is_a_32_bit_unsigned_integer(self):
        self.assertEqual(flasher.image_version_arg("0xFFFFFFFF"), 0xFFFFFFFF)
        for value in ("-1", "0x100000000", "invalid"):
            with self.subTest(value=value), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                flasher.image_version_arg(value)


if __name__ == "__main__":
    unittest.main()
