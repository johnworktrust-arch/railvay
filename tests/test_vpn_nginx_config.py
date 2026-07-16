from __future__ import annotations

import base64
import json
import re
import unittest
from pathlib import Path


class VpnNginxConfigTest(unittest.TestCase):
    def test_browser_requests_still_receive_a_raw_happ_subscription(self) -> None:
        config = (
            Path(__file__).resolve().parents[1] / "deploy" / "vpn" / "nginx.conf"
        ).read_text(encoding="utf-8")

        self.assertIn('proxy_set_header Accept "text/plain";', config)
        encoded = re.search(
            r'add_header routing "happ://routing/onadd/([^\"]+)"', config
        )
        self.assertIsNotNone(encoded)
        assert encoded is not None
        decoded = base64.b64decode(encoded.group(1)).decode("utf-8")
        routing = json.loads(decoded)

        self.assertEqual(decoded.count('"RemoteDNSDomain"'), 1)
        self.assertEqual(routing["Name"], "CEA VPN")
        self.assertEqual(routing["GlobalProxy"], "true")
        self.assertEqual(routing["LastUpdated"], "")
        self.assertEqual(routing["FakeDNS"], "false")


if __name__ == "__main__":
    unittest.main()
