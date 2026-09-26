"""The suite's own guard rails, tested as carefully as anything else it checks.

The guard exists because of a specific unreadable failure (see `conftest.temp_space`): a gate run reporting
`1272 tests, exit 1, tail 'FAILED (errors=33)'` when the real answer was "the temp filesystem is full". A
guard that fires is only better than the mess it replaces if it says what to do, so the assertions below are
about the *message* as much as the verdict: a future edit that reduces it to "no space" should go red here.

These tests must also not be able to pass vacuously: each case asserts the exact verdict string, so a
`temp_space` that returned "ok" for every input fails, and one that always returned "full" fails too.
"""
from __future__ import annotations
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import conftest
from conftest import TEMP_NEED_MB, TEMP_WARN_MB, temp_space


class TestTempSpaceVerdict(unittest.TestCase):
    def _at(self, free_mb: int) -> str:
        usage = shutil._ntuple_diskusage(total=10 * 1024**3, used=0, free=free_mb * 1024**2)
        with mock.patch.object(shutil, "disk_usage", return_value=usage):
            return temp_space(Path(tempfile.gettempdir()))[1]

    def test_below_the_need_it_refuses(self):
        for mb in (0, 1, TEMP_NEED_MB - 1):
            self.assertEqual(self._at(mb), "full", "%d MB must be refused" % mb)

    def test_between_need_and_warn_it_runs_loudly(self):
        for mb in (TEMP_NEED_MB, TEMP_WARN_MB - 1):
            self.assertEqual(self._at(mb), "warn", "%d MB must run with a warning" % mb)

    def test_with_room_it_is_quiet(self):
        for mb in (TEMP_WARN_MB, 5000):
            self.assertEqual(self._at(mb), "ok", "%d MB must be quiet" % mb)

    def test_these_tests_are_not_vacuous(self):
        self.assertLess(TEMP_NEED_MB, TEMP_WARN_MB)
        self.assertGreater(TEMP_NEED_MB, 200, "a full run peaks near 360 MB; a lower floor would not prevent ENOSPC")

    def test_free_space_is_reported_as_whole_megabytes(self):
        usage = shutil._ntuple_diskusage(total=10 * 1024**3, used=0, free=(TEMP_NEED_MB + 7) * 1024**2 + 512)
        with mock.patch.object(shutil, "disk_usage", return_value=usage):
            self.assertEqual(temp_space(Path("/"))[0], TEMP_NEED_MB + 7)


class TestTheRefusalSaysWhatToDo(unittest.TestCase):
    """`_tmp_root` raises at import, so this message is the only thing a person sees: it has to carry the
    number, the reason, and the way out."""

    def test_it_names_the_number_the_reason_and_the_remedy(self):
        with mock.patch.object(conftest, "temp_space", return_value=(12, "full")):
            with self.assertRaises(RuntimeError) as caught:
                conftest._tmp_root()
        msg = str(caught.exception)
        self.assertIn("12 MB", msg, "the free number is the whole point of the guard")
        self.assertIn("PGM_TEST_TMPDIR", msg, "a refusal without a way out is a dead end")
        self.assertIn("ENOSPC", msg, "the report should name the failure this replaces")

    def test_it_does_not_raise_when_there_is_room(self):
        with mock.patch.object(conftest, "temp_space", return_value=(4096, "ok")):
            base = conftest._tmp_root()
        self.assertTrue(base.is_dir())

    def test_the_threshold_is_rehearsable_from_the_command_line(self):
        """The point of `PGM_TEST_TMP_MIN_MB`: prove the refusal fires, on a box with plenty of room."""
        with mock.patch.dict(os.environ, {"PGM_TEST_TMP_MIN_MB": "999999"}):
            free_mb, verdict = temp_space(Path(tempfile.gettempdir()))
            self.assertEqual(verdict, "full")
            self.assertLess(free_mb, 999999)
            with self.assertRaises(RuntimeError) as caught:
                conftest._tmp_root()
        self.assertIn("PGM_TEST_TMPDIR", str(caught.exception))

    def test_a_warning_is_not_a_refusal(self):
        with mock.patch.object(conftest, "temp_space", return_value=(500, "warn")):
            self.assertTrue(conftest._tmp_root().is_dir())


if __name__ == "__main__":
    unittest.main()
