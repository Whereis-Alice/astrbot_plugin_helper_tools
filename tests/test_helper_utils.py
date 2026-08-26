from __future__ import annotations

import unittest
from datetime import datetime, timezone

from astrbot_plugin_helper_tools.helper_utils import format_timestamp


class FormatTimestampTests(unittest.TestCase):
    def test_matches_local_wall_clock(self) -> None:
        timestamp = 1_785_000_000
        expected = (
            datetime.fromtimestamp(timestamp, tz=timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S")
        )

        self.assertEqual(format_timestamp(timestamp), expected)
        self.assertEqual(format_timestamp(str(timestamp)), expected)

    def test_invalid_values_return_empty_string(self) -> None:
        for value in (None, "", "abc", 0, -1):
            self.assertEqual(format_timestamp(value), "")


if __name__ == "__main__":
    unittest.main()
