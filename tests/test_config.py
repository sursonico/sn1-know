import unittest

import config


class ConfigTests(unittest.TestCase):
    def test_share_password_setting_is_available(self):
        self.assertTrue(hasattr(config, "SHARE_PASSWORD"))
        self.assertIsInstance(config.SHARE_PASSWORD, str)


if __name__ == "__main__":
    unittest.main()
