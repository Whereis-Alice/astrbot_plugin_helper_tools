from __future__ import annotations

import unittest
from datetime import datetime, timezone

from astrbot_plugin_helper_tools.helper_utils import (
    format_timestamp,
    timestamp_has_expired,
)


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


class TimestampHasExpiredTests(unittest.TestCase):
    def test_non_positive_and_invalid_values_are_not_expiry_evidence(self) -> None:
        now = datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc)
        for value in (None, "", "invalid", 0, -1):
            with self.subTest(value=value):
                self.assertFalse(timestamp_has_expired(value, now=now))

    def test_positive_timestamp_is_expired_at_or_before_now(self) -> None:
        now = datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc)
        past = int(datetime(2026, 11, 8, 8, 59, tzinfo=timezone.utc).timestamp())
        exact = int(now.timestamp())
        future = int(datetime(2026, 11, 8, 9, 1, tzinfo=timezone.utc).timestamp())

        self.assertTrue(timestamp_has_expired(past, now=now))
        self.assertTrue(timestamp_has_expired(exact, now=now))
        self.assertFalse(timestamp_has_expired(future, now=now))


if __name__ == "__main__":
    unittest.main()
