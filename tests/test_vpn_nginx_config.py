from __future__ import annotations

import json
import re
import unittest
import uuid
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
        self.assertIn("server 127.0.0.1:8010", config)
        self.assertIn("server 127.0.0.1:8000 backup", config)
        self.assertIn("proxy_pass http://ceavpn_subscription_backend;", config)
        connect_block = re.search(
            r'location ~ "\^/connect/.*?\n    }', config, flags=re.DOTALL
        )
        self.assertIsNotNone(connect_block)
        assert connect_block is not None
        setup_guide = connect_block.group(0)
        self.assertIn("[A-Za-z0-9._~-]{1,160}", setup_guide)
        self.assertIn("try_files /connect.html =404;", setup_guide)
        self.assertIn('add_header Cache-Control "no-store" always;', setup_guide)
        self.assertIn('add_header X-Frame-Options "DENY" always;', setup_guide)

        setup_html = (
            Path(__file__).resolve().parents[1]
            / "deploy"
            / "vpn"
            / "connect.html"
        ).read_text(encoding="utf-8")
        self.assertIn("Подключите VPN", setup_html)
        self.assertIn("https://www.happ.su/main", setup_html)
        self.assertIn("happ://add/", setup_html)
        self.assertIn("Добавить подписку", setup_html)
        self.assertIn("Копировать", setup_html)
        self.assertIn("location.pathname", setup_html)
        self.assertIn("overflow-x: hidden", setup_html)
        self.assertIn("grid-template-columns: 34px minmax(0, 1fr)", setup_html)
        self.assertIn(".content {", setup_html)
        self.assertIn("min-width: 0", setup_html)
        self.assertIn('<details class="fallback">', setup_html)

        happ_block = re.search(
            r'location ~ "\^/happ/.*?\n    }', config, flags=re.DOTALL
        )
        self.assertIsNotNone(happ_block)
        assert happ_block is not None
        bridge = happ_block.group(0)
        self.assertIn("[A-Za-z0-9._~-]{1,160}", bridge)
        self.assertIn(
            "happ://add/https://__SUB_DOMAIN__:8443/sub/$1",
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
            "v2box://install-sub?url=https%3A%2F%2F__SUB_DOMAIN__%3A8443%2Fsub%2F$1&name=CEA%20VPN",
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
        self.assertIn(
            "include /etc/nginx/snippets/ceavpn-relays.conf;",
            config,
        )

        apply_script = (
            Path(__file__).resolve().parents[1]
            / "deploy"
            / "vpn"
            / "apply-reality-config.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "/etc/nginx/snippets/ceavpn-relays.conf",
            apply_script,
        )
        provision_script = (
            Path(__file__).resolve().parents[1]
            / "deploy"
            / "vpn"
            / "provision-node.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            '"$bundle_dir/connect.html" /opt/marzban/connect.html',
            provision_script,
        )
        self.assertIn(
            '"$bundle_dir/subscription_proxy.py"',
            provision_script,
        )
        self.assertIn(
            "systemctl enable --now ceavpn-subscription-proxy.service",
            provision_script,
        )

    def test_happ_can_publish_multiple_named_region_ws_profiles(self) -> None:
        root = Path(__file__).resolve().parents[1]
        hosts_script = (
            root / "deploy" / "vpn" / "configure-marzban-hosts.sh"
        ).read_text(encoding="utf-8")

        self.assertIn('"is_disabled": True', hosts_script)
        self.assertIn("fallback_tag: normalized_fallback_hosts", hosts_script)
        self.assertIn(
            'published_hosts_file.is_file()',
            hosts_script,
        )
        self.assertIn('"remark": remark', hosts_script)
        self.assertIn('"is_disabled": False', hosts_script)

        smoke_script = (
            root / "deploy" / "vpn" / "smoke-test.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'expected_vless_profiles="${VPN_SMOKE_EXPECTED_PROFILE_COUNT:-1}"',
            smoke_script,
        )
        self.assertIn(
            'expected_profile_remark in {profile["remark"] for profile in profiles}',
            smoke_script,
        )
        self.assertIn(
            'require(kinds == ["ws-tls"] * expected_profile_count',
            smoke_script,
        )

    def test_lte_gateway_forces_both_public_inbounds_through_foreign_exit(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        template = (
            root / "deploy" / "vpn" / "xray_config_lte.json"
        ).read_text(encoding="utf-8")
        relay_uuid = str(uuid.uuid4())
        rendered = template
        replacements = {
            "__FALLBACK_WS_PATH__": f"/ws-{'1' * 48}",
            "__COVER_DOMAIN__": "cover.example.test",
            "__REALITY_PRIVATE_KEY__": "private-key",
            "__REALITY_PUBLIC_KEY__": "public-key",
            "__REALITY_SHORT_ID__": "0123456789abcdef",
            "__LTE_EXIT_ADDRESS__": "sub-exit.example.test",
            "__LTE_EXIT_PORT__": "8443",
            "__LTE_EXIT_UUID__": relay_uuid,
            "__LTE_EXIT_SNI__": "sub-exit.example.test",
            "__LTE_EXIT_HOST__": "sub-exit.example.test",
            "__LTE_EXIT_PATH__": f"/ws-{'2' * 48}",
        }
        for placeholder, value in replacements.items():
            self.assertEqual(rendered.count(placeholder), 1)
            rendered = rendered.replace(placeholder, value)
        self.assertIsNone(re.search(r"__[A-Z0-9_]+__", rendered))

        config = json.loads(rendered)
        exit_outbound = next(
            outbound
            for outbound in config["outbounds"]
            if outbound["tag"] == "LTE EXIT"
        )
        self.assertEqual(exit_outbound["protocol"], "vless")
        self.assertEqual(
            exit_outbound["settings"]["vnext"][0]["users"][0]["id"],
            relay_uuid,
        )
        self.assertEqual(
            exit_outbound["streamSettings"]["security"],
            "tls",
        )

        exit_rules = [
            rule
            for rule in config["routing"]["rules"]
            if rule.get("outboundTag") == "LTE EXIT"
        ]
        self.assertEqual(len(exit_rules), 1)
        self.assertEqual(
            set(exit_rules[0]["inboundTag"]),
            {"VLESS WS TLS FALLBACK", "VLESS TCP REALITY"},
        )

        provision_script = (
            root / "deploy" / "vpn" / "provision-node.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('CEAVPN_NODE_MODE:-direct', provision_script)
        self.assertIn(
            'xray_template_source="$bundle_dir/xray_config_lte.json"',
            provision_script,
        )
        self.assertIn("/root/ceavpn-lte-exit.env", provision_script)


if __name__ == "__main__":
    unittest.main()
