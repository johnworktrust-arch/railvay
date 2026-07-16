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
        happ_block = re.search(
            r'location ~ "\^/happ/.*?\n    }', config, flags=re.DOTALL
        )
        self.assertIsNotNone(happ_block)
        assert happ_block is not None
        bridge = happ_block.group(0)
        self.assertIn("[A-Za-z0-9._~-]{1,160}", bridge)
        self.assertIn(
            "happ://add/https://sub.79-137-197-51.sslip.io:8443/sub/$1",
            bridge,
        )
        self.assertIn("access_log off;", bridge)
        self.assertIn('add_header Cache-Control "no-store" always;', bridge)
        self.assertIn('add_header Referrer-Policy "no-referrer" always;', bridge)
        self.assertIn('add_header X-Robots-Tag "noindex, nofollow, noarchive" always;', bridge)
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
