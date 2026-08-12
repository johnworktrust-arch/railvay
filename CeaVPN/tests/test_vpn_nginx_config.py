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
        self.assertIn("Открыть Happ", setup_html)
        self.assertIn("Копировать", setup_html)
        self.assertIn("location.pathname", setup_html)
        self.assertIn("background: #fff", setup_html)
        self.assertIn("Happ откроется только после вашего нажатия", setup_html)
        self.assertNotIn("window.location", setup_html)
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
            'kinds == [expected_profile_kind] * expected_profile_count',
            smoke_script,
        )
        self.assertIn('"xhttp-reality"', smoke_script)
        self.assertIn(
            'chained nodes require VPN_SMOKE_EXPECTED_EGRESS_IPV4',
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

    def test_whitelist_gateway_uses_xhttp_reality_without_vision_flow(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        template = (
            root / "deploy" / "vpn" / "xray_config_whitelist.json"
        ).read_text(encoding="utf-8")
        relay_uuid = str(uuid.uuid4())
        rendered = template
        replacements = {
            "__XHTTP_PATH__": f"/xhttp-{'1' * 48}",
            "__COVER_DOMAIN__": "cover.example.test",
            "__REALITY_TARGET__": "cover.example.test:443",
            "__REALITY_PRIVATE_KEY__": "private-key",
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
        self.assertEqual(len(config["inbounds"]), 1)
        inbound = config["inbounds"][0]
        self.assertEqual(inbound["tag"], "VLESS XHTTP REALITY")
        self.assertEqual(inbound["settings"]["clients"], [])
        self.assertNotIn("flow", json.dumps(inbound))
        self.assertEqual(inbound["streamSettings"]["network"], "xhttp")
        self.assertEqual(
            inbound["streamSettings"]["xhttpSettings"],
            {"path": f"/xhttp-{'1' * 48}"},
        )
        self.assertEqual(inbound["streamSettings"]["security"], "reality")
        reality = inbound["streamSettings"]["realitySettings"]
        self.assertEqual(reality["target"], "cover.example.test:443")
        self.assertNotIn("publicKey", reality)
        self.assertEqual(
            set(reality),
            {
                "show",
                "target",
                "xver",
                "serverNames",
                "privateKey",
                "shortIds",
            },
        )

        exit_outbound = next(
            outbound
            for outbound in config["outbounds"]
            if outbound["tag"] == "WHITELIST EXIT"
        )
        self.assertEqual(exit_outbound["protocol"], "vless")
        self.assertEqual(
            exit_outbound["settings"]["vnext"][0]["users"],
            [{"id": relay_uuid, "encryption": "none"}],
        )
        self.assertEqual(
            exit_outbound["streamSettings"]["network"],
            "ws",
        )
        self.assertEqual(
            exit_outbound["streamSettings"]["security"],
            "tls",
        )
        exit_rules = [
            rule
            for rule in config["routing"]["rules"]
            if rule.get("outboundTag") == "WHITELIST EXIT"
        ]
        self.assertEqual(
            exit_rules,
            [{
                "type": "field",
                "inboundTag": ["VLESS XHTTP REALITY"],
                "outboundTag": "WHITELIST EXIT",
            }],
        )

    def test_whitelist_provisioning_is_pinned_and_publication_is_gated(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        provision = (
            root / "deploy" / "vpn" / "provision-node.sh"
        ).read_text(encoding="utf-8")
        apply_script = (
            root / "deploy" / "vpn" / "apply-reality-config.sh"
        ).read_text(encoding="utf-8")
        qualification = (
            root / "deploy" / "vpn" / "qualify-whitelist-ingress.sh"
        ).read_text(encoding="utf-8")
        whitelist_hosts = (
            root / "deploy" / "vpn" / "configure-whitelist-host.sh"
        ).read_text(encoding="utf-8")
        relay_helper = (
            root / "deploy" / "vpn" / "provision-whitelist-relay.sh"
        ).read_text(encoding="utf-8")
        pins = (
            root / "deploy" / "vpn" / "xray-pins.env"
        ).read_text(encoding="utf-8")
        gate_service = (
            root / "deploy" / "vpn" / "ceavpn-whitelist-gate.service"
        ).read_text(encoding="utf-8")
        boot_close_service = (
            root
            / "deploy"
            / "vpn"
            / "ceavpn-whitelist-boot-close.service"
        ).read_text(encoding="utf-8")
        gate_timer = (
            root / "deploy" / "vpn" / "ceavpn-whitelist-gate.timer"
        ).read_text(encoding="utf-8")
        worker_script = (
            root / "deploy" / "vpn" / "worker.py"
        ).read_text(encoding="utf-8")
        worker_service = (
            root / "deploy" / "vpn" / "ceavpn-worker.service"
        ).read_text(encoding="utf-8")
        worker_installer = (
            root / "deploy" / "vpn" / "install-worker.sh"
        ).read_text(encoding="utf-8")
        nginx_template = (
            root / "deploy" / "vpn" / "nginx.conf"
        ).read_text(encoding="utf-8")

        self.assertIn('xray_config_whitelist.json', provision)
        self.assertIn('sha256sum "$bundle_dir/xray-core/xray"', provision)
        self.assertIn('dpkg --print-architecture', provision)
        self.assertIn('mv "$xray_candidate"', provision)
        self.assertIn("backup_managed_files()", provision)
        self.assertIn(
            "/opt/marzban/docker-compose.yml",
            provision[provision.index("managed_paths=(") :],
        )
        self.assertIn(
            "/etc/systemd/system/ceavpn-whitelist-gate.timer",
            provision[provision.index("managed_paths=(") :],
        )
        self.assertIn(
            'cp -a "$backup_path" "$path"',
            provision,
        )
        self.assertIn(
            '! -name xray -exec cp -a -- {} /var/lib/marzban/xray-core/',
            provision,
        )
        self.assertRegex(
            pins,
            r"CEAVPN_XRAY_REQUIRED_VERSION=26\.3\.27",
        )
        self.assertRegex(pins, r"CEAVPN_XRAY_SHA256_AMD64=[0-9a-f]{64}")
        self.assertRegex(pins, r"CEAVPN_XRAY_SHA256_ARM64=[0-9a-f]{64}")

        self.assertIn('/root/ceavpn-xhttp.env', apply_script)
        self.assertIn('/xhttp-$(openssl rand -hex 24)', apply_script)
        self.assertIn('chmod 0600 "$xhttp_file"', apply_script)
        self.assertIn('restricted-sim-xhttp-tunnel-worked', qualification)
        self.assertIn("remote_cover_is_healthy()", qualification)
        self.assertIn("-tls1_3 -alpn h2", qualification)
        self.assertIn("--http2 --tlsv1.3 --max-redirs 0", qualification)
        self.assertIn("xray_inbound_is_healthy()", qualification)
        self.assertIn('"$xray" run -test -c "$active_config"', qualification)
        self.assertIn(
            '!= f"{os.environ[\'CEAVPN_COVER_DOMAIN\']}:443"',
            apply_script,
        )
        subscription_server = nginx_template[
            nginx_template.index("listen 8443 ssl;") :
            nginx_template.index("listen 127.0.0.1:9443 ssl;")
        ]
        loopback_cover_server = nginx_template[
            nginx_template.index("listen 127.0.0.1:9443 ssl;") :
        ]
        self.assertIn("__WHITELIST_PROBE_LOCATION__", subscription_server)
        self.assertNotIn(
            "__WHITELIST_PROBE_LOCATION__",
            loopback_cover_server,
        )
        self.assertNotIn('restricted-sim-reached-probe', qualification)
        self.assertIn('canary-create', qualification)
        self.assertIn('canary-status', qualification)
        self.assertIn('canary-revoke', qualification)
        self.assertIn('used_traffic"] <= minimum_usage', qualification)
        self.assertIn('/root/ceavpn-whitelist-canary.txt', qualification)
        self.assertIn('"transfer_over_1mib"', qualification)
        self.assertIn('"status": state', qualification)
        self.assertNotIn('"XHTTP_PATH"', qualification)
        self.assertNotIn('"REALITY_PUBLIC_KEY"', qualification)
        self.assertIn(
            'gate_lock_file="/run/lock/ceavpn-whitelist-gate.lock"',
            qualification,
        )
        self.assertIn('flock -x "$gate_lock_fd"', qualification)
        self.assertIn("force_gate_closed()", qualification)
        self.assertIn(
            'docker compose -f "$compose_file" restart marzban',
            qualification,
        )
        self.assertIn(
            "conntrack -D -p tcp --dport 443",
            qualification,
        )
        self.assertIn("public_ingress_is_closed", qualification)
        self.assertIn('recorded_status={raw_status}', qualification)
        self.assertIn("timedelta(hours=24)", qualification)
        self.assertIn('"is_disabled": not qualified', whitelist_hosts)
        self.assertIn('"VLESS XHTTP REALITY"', whitelist_hosts)
        self.assertIn(
            'gate_lock_file="/run/lock/ceavpn-whitelist-gate.lock"',
            whitelist_hosts,
        )
        self.assertIn("acquire_gate_lock", whitelist_hosts)
        self.assertIn(
            '.[0].is_disabled == false',
            whitelist_hosts,
        )
        self.assertIn("conntrack curl docker-compose-v2", provision)
        self.assertIn(
            'if [[ "$node_mode" == "whitelist" ]]; then\n'
            '  ufw --force delete allow 443/tcp',
            provision,
        )
        self.assertIn(
            'CEAVPN_SERVER_CODE:?CEAVPN_SERVER_CODE is required',
            provision,
        )
        self.assertIn(
            '"server_code": os.environ["CEAVPN_FINGERPRINT_SERVER_CODE"]',
            qualification,
        )
        enforce_index = provision.index(
            "/opt/ceavpn/qualify-whitelist-ingress.sh enforce"
        )
        timer_index = provision.index(
            "systemctl enable --now ceavpn-whitelist-gate.timer"
        )
        self.assertLess(enforce_index, timer_index)
        self.assertIn(
            "/var/www/ceavpn-whitelist",
            gate_service,
        )
        self.assertIn("Before=network-pre.target network.target docker.service", boot_close_service)
        self.assertIn("boot-firewall-close", boot_close_service)
        self.assertIn("OnBootSec=5s", gate_timer)
        self.assertIn(
            "systemctl enable ceavpn-whitelist-boot-close.service",
            provision,
        )
        self.assertIn(
            'isolation_state_file="/root/ceavpn-whitelist-user-isolation.json"',
            qualification,
        )
        self.assertIn("isolate_non_canary_users()", qualification)
        self.assertIn('systemctl stop "$worker_service"', qualification)
        self.assertIn('"/api/users?offset=${offset}&limit=${limit}"', qualification)
        self.assertIn('"disabled" "$index"', qualification)
        self.assertIn("verify_canary_isolation()", qualification)
        self.assertIn(
            'length == 1 and\n      .[0].username == $username',
            qualification,
        )
        self.assertIn("restore_isolated_users()", qualification)
        self.assertIn(".expire > 0 and .expire <= $now", qualification)
        self.assertIn("restart_marzban_runtime()", qualification)
        self.assertIn("complete_restoration_behind_closed_ingress", qualification)
        self.assertIn("relay_e2e_is_healthy()", qualification)
        self.assertIn("--socks5-hostname", qualification)
        self.assertIn(
            'RECONCILED_MARKER = "/run/ceavpn-worker/reconciled"',
            worker_script,
        )
        self.assertIn(
            'getattr(self.railway, "claim_reconciled", False) is True',
            worker_script,
        )
        self.assertIn("_mark_reconciled()", worker_script)
        self.assertIn("_clear_reconciled()", worker_script)
        self.assertIn('claim_payload["worker_epoch"]', worker_script)
        self.assertIn(
            'worker_reconciliation_is_fresh()',
            qualification,
        )
        self.assertIn(
            'on-hold XHTTP users cannot be safely isolated',
            qualification,
        )
        self.assertLess(
            qualification.index(
                'if [[ "${1:-}" == "boot-firewall-close" ]]'
            ),
            qualification.index('if [[ ! -s "$node_file" ]]'),
        )
        canary_case = qualification[qualification.index("  canary-create)") :]
        self.assertLess(
            canary_case.index("relay_e2e_is_healthy"),
            canary_case.index("open_public_ingress"),
        )
        self.assertIn(
            'epoch_mode="${3:-preserve}"',
            worker_installer,
        )
        self.assertIn(
            'printf \'VPN_WORKER_EPOCH=%s\\n\' "$worker_epoch"',
            worker_installer,
        )
        self.assertIn("--rotate-epoch", worker_installer)
        self.assertIn("RuntimeDirectory=ceavpn-worker", worker_service)
        self.assertIn('proxies: {vless: {id: $uuid, flow: ""}}', relay_helper)
        self.assertIn('rollback_mode="delete"', relay_helper)
        self.assertIn('rollback_mode="restore"', relay_helper)
        self.assertIn('CEAVPN_RELAY_STATUS=%q', relay_helper)
        self.assertIn('short-lived root SSH key', relay_helper)
        self.assertNotIn('sudo -n sh -s --', relay_helper)
        self.assertIn('/root/ceavpn-lte-exit.env', relay_helper)
        canary_revoke_case = qualification[
            qualification.index("  canary-revoke)") :
            qualification.index("  pass)")
        ]
        canary_revoke_entry = canary_revoke_case[
            : canary_revoke_case.index(
                'if [[ ! -s "$canary_state_file" ]]'
            )
        ]
        self.assertIn("force_gate_closed", canary_revoke_entry)
        self.assertLess(
            canary_revoke_case.index("force_gate_closed"),
            canary_revoke_case.index('write_canary_audit "revoked"'),
        )
        self.assertLess(
            canary_revoke_case.index('write_canary_audit "revoked"'),
            canary_revoke_case.rindex("prepare_canary_api"),
        )

    def test_whitelist_public_gate_status_is_sanitized_and_atomic(self) -> None:
        root = Path(__file__).resolve().parents[1]
        apply_script = (
            root / "deploy" / "vpn" / "apply-reality-config.sh"
        ).read_text(encoding="utf-8")
        qualification = (
            root / "deploy" / "vpn" / "qualify-whitelist-ingress.sh"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "location = /.well-known/ceavpn-whitelist-status",
            apply_script,
        )
        self.assertIn(
            "alias /var/www/ceavpn-whitelist/status.json;",
            apply_script,
        )
        self.assertIn(
            'add_header Cache-Control \\"no-store\\" always;',
            apply_script,
        )

        public_payload_match = re.search(
            r'payload = \{\n'
            r'    "service": "ceavpn-whitelist-gate-v1",\n'
            r'    "status": "passed",\n'
            r'    "config_fingerprint": '
            r'os\.environ\["CEAVPN_PUBLIC_PROFILE_FINGERPRINT"\],\n'
            r'    "valid_until": .*?\n'
            r'\}',
            qualification,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(public_payload_match)
        assert public_payload_match is not None
        public_payload = public_payload_match.group(0)
        self.assertEqual(
            re.findall(r'^\s+"([^"]+)":', public_payload, flags=re.MULTILINE),
            ["service", "status", "config_fingerprint", "valid_until"],
        )
        for forbidden in (
            "uuid",
            "path",
            "public_key",
            "operator",
            "evidence",
            "exit_",
        ):
            self.assertNotIn(forbidden, public_payload.lower())

        public_profile_function = qualification[
            qualification.index("compute_public_profile_fingerprint()")
            : qualification.index("relay_e2e_is_healthy()")
        ]
        for field in (
            '"server_code"',
            '"address"',
            '"port"',
            '"transport"',
            '"security"',
            '"path"',
            '"sni"',
            '"pbk"',
            '"sid"',
            '"fingerprint"',
            '"mode"',
            '"extra"',
            '"qualification_url"',
        ):
            self.assertIn(field, public_profile_function)
        for setting in (
            '"scMaxEachPostBytes": 1000000',
            '"scMaxConcurrentPosts": 100',
            '"scMinPostsIntervalMs": 30',
            '"xPaddingBytes": "100-1000"',
            '"noGRPCHeader": False',
        ):
            self.assertIn(setting, public_profile_function)
        self.assertNotIn("CEAVPN_LTE_EXIT_UUID", public_profile_function)
        self.assertNotIn("CEAVPN_QUALIFICATION_OPERATOR", public_profile_function)
        self.assertIn('separators=(",", ":")', public_profile_function)
        self.assertIn("sort_keys=True", public_profile_function)

        self.assertIn(
            'public_status_tmp="$(mktemp "${public_status_file}.new.XXXXXX")"',
            qualification,
        )
        self.assertIn('chmod 0644 "$public_status_tmp"', qualification)
        self.assertIn(
            'mv "$public_status_tmp" "$public_status_file"',
            qualification,
        )
        force_closed = qualification[
            qualification.index("force_gate_closed()")
            : qualification.index("publish_public_status()")
        ]
        self.assertLess(
            force_closed.index("close_public_ingress"),
            force_closed.index("remove_public_status"),
        )
        self.assertIn('rm -f -- "$public_status_file"', qualification)
        self.assertIn("canary_window_is_active", qualification)

        canary_uri_function = qualification[
            qualification.index("CEAVPN_CANARY_USERNAME=")
            : qualification.index("canary_status()")
        ]
        self.assertIn('"headerType": ""', canary_uri_function)
        self.assertIn('"mode": "auto"', canary_uri_function)
        self.assertIn('"extra": json.dumps(', canary_uri_function)
        self.assertIn("sort_keys=True", canary_uri_function)
        self.assertNotIn('"flow":', canary_uri_function)
        for setting in (
            '"scMaxEachPostBytes": 1000000',
            '"scMaxConcurrentPosts": 100',
            '"scMinPostsIntervalMs": 30',
            '"xPaddingBytes": "100-1000"',
            '"noGRPCHeader": False',
        ):
            self.assertIn(setting, canary_uri_function)


if __name__ == "__main__":
    unittest.main()
