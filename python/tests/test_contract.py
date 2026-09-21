"""Contract guards: the public package surface stays consistent."""

import re
import unittest

import mwm


class VersionTests(unittest.TestCase):
    def test_version_is_pep440_like(self):
        self.assertRegex(mwm.__version__, r"^\d+\.\d+(?:\.\d+)*$")

    def test_version_is_exported(self):
        self.assertIn("__version__", mwm.__all__)