"""The installer writes LOOM_ROLL_PIN_EXEMPT into the station plist."""

import unittest

import test_install_sh

_Base = test_install_sh.InstallScriptTest


class RollPinExemptInstallTest(unittest.TestCase):
    # Reuse the throwaway-HOME fixture without re-running the inherited tests.
    setUp = _Base.setUp
    run_install = _Base.run_install

    def plist(self):
        return (self.home / "Library" / "LaunchAgents" / "com.loom.station.plist").read_text()

    def test_roll_pin_exempt_is_written_to_station_plist(self):
        exempt = " ".join(["example-owner/project" + "#" + "12", "example-owner/other" + "#" + "34"])
        result = self.run_install(LOOM_ROLL_PIN_EXEMPT=exempt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("<key>LOOM_ROLL_PIN_EXEMPT</key><string>%s</string>" % exempt, self.plist())

    def test_roll_pin_exempt_is_empty_when_unset(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("<key>LOOM_ROLL_PIN_EXEMPT</key><string></string>", self.plist())


del _Base


if __name__ == "__main__":
    unittest.main()
