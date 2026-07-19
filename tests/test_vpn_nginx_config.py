from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


class VpnNginxConfigTest(unittest.TestCase):
    def test_ws_tls_profile_precedes_reality_in_xray_config(self) -> None:
        config = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "deploy"
                / "vpn"
                / "xray_config.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(
            [inbound["tag"] for inbound in config["inbounds"]],
            ["VLESS WS TLS FALLBACK", "VLESS TCP REALITY"],
        )

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

        v2box_block = re.search(
            r'location ~ "\^/v2box/.*?\n    }', config, flags=re.DOTALL
        )
        self.assertIsNotNone(v2box_block)
        assert v2box_block is not None
        v2box_bridge = v2box_block.group(0)
        self.assertIn("[A-Za-z0-9._~-]{1,160}", v2box_bridge)
        self.assertIn(
            "v2box://install-sub?url=https%3A%2F%2Fsub.79-137-197-51.sslip.io%3A8443%2Fsub%2F$1&name=CEA%20VPN",
            v2box_bridge,
        )
        self.assertIn("access_log off;", v2box_bridge)
        self.assertIn('add_header Cache-Control "no-store" always;', v2box_bridge)
        self.assertIn('add_header Referrer-Policy "no-referrer" always;', v2box_bridge)
        self.assertIn(
            'add_header X-Robots-Tag "noindex, nofollow, noarchive" always;',
            v2box_bridge,
        )
        self.assertIn('add_header routing-enable "0" always;', config)
        self.assertNotIn('add_header routing "', config)
        self.assertNotIn("happ://routing/off", config)
        self.assertNotIn("happ://routing/onadd/", config)

    def test_happ_publishes_only_the_named_netherlands_ws_profile(self) -> None:
        root = Path(__file__).resolve().parents[1]
        hosts_script = (
            root / "deploy" / "vpn" / "configure-marzban-hosts.sh"
        ).read_text(encoding="utf-8")

        reality = re.search(
            r"reality_tag: \[\{(?P<body>.*?)\}\],\n\s*fallback_tag:",
            hosts_script,
            flags=re.DOTALL,
        )
        fallback = re.search(
            r"fallback_tag: \[\{(?P<body>.*?)\}\],\n\}",
            hosts_script,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(reality)
        self.assertIsNotNone(fallback)
        assert reality is not None and fallback is not None
        self.assertIn('"is_disabled": True', reality.group("body"))
        self.assertIn('"is_disabled": False', fallback.group("body"))
        self.assertIn(
            '"remark": "🇳🇱 Нидерланды · Амстердам"',
            fallback.group("body"),
        )

        smoke_script = (
            root / "deploy" / "vpn" / "smoke-test.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("expected_vless_profiles=1", smoke_script)
        self.assertIn(
            'profiles[0]["remark"] == "🇳🇱 Нидерланды · Амстердам"',
            smoke_script,
        )
        self.assertIn('require(kinds == ["ws-tls"]', smoke_script)


if __name__ == "__main__":
    unittest.main()
