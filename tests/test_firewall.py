import unittest

from tests.helpers import DEDICATED, SHARED, write_cfg

from modules import firewall, settings


class FirewallTest(unittest.TestCase):
    def test_shared_vip_rules_deduplicated(self):
        rules = firewall.rules(settings.load(SHARED))
        keys = [(r["vip"], r["protocol"], r["port"]) for r in rules]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn({"service": "tunnel,seg", "vip": "10.0.0.50", "protocol": "tcp", "port": 443}, rules)

    def test_nft_is_atomic_and_scoped_to_vips(self):
        text = firewall.render_nft(settings.load(DEDICATED))
        self.assertIn("delete table inet omnissa_lb", text)
        self.assertIn("ip daddr != @vips return", text)
        self.assertIn("elements = { 10.0.0.51, 10.0.0.52 }", text)
        self.assertIn("ip daddr 10.0.0.51 udp dport 8443 accept", text)
        self.assertNotIn("flush ruleset", text)

    def test_allowed_sources(self):
        text = SHARED.read_text() + "\nfirewall: {manage: true, allowed_sources: [192.168.0.0/16, 10.1.0.0/24]}\n"
        cfg = settings.load(write_cfg(text))
        self.assertIn("ip saddr { 192.168.0.0/16, 10.1.0.0/24 }", firewall.render_nft(cfg))
        specs = firewall._ufw_specs(cfg)
        self.assertIn("allow proto tcp from 192.168.0.0/16 to 10.0.0.50 port 443", specs)

    def test_ipv6_traffic_is_not_dropped(self):
        lines = firewall.render_nft(settings.load(SHARED)).splitlines()
        idx = [i for i, l in enumerate(lines) if "meta nfproto != ipv4 return" in l]
        drop = [i for i, l in enumerate(lines) if "counter drop" in l]
        self.assertTrue(idx and drop and idx[0] < drop[0])

    def test_ipv6_sources_rejected_by_validation(self):
        from modules.common import OmnissaError

        text = SHARED.read_text() + "\nfirewall: {manage: true, allowed_sources: [2001:db8::/32]}\n"
        with self.assertRaisesRegex(OmnissaError, "IPv4"):
            settings.load(write_cfg(text))

    def test_backend_switch_does_not_restore_stale_ufw_state(self):
        from unittest import mock

        from modules.common import Context, State

        st = State()
        st.set("firewall_backend", "ufw")
        st.set("ufw_rules", ["allow proto tcp from any to 10.0.0.50 port 443"])
        st.save(Context())
        cfg = settings.load(SHARED)
        cfg["firewall"]["backend"] = "nftables"
        ctx = Context()
        ctx.transaction = mock.Mock()
        with mock.patch.object(firewall, "run") as run, mock.patch.object(firewall, "which", return_value="/usr/sbin/nft"), \
             mock.patch.object(firewall, "write_managed_file", return_value=True):
            run.return_value = mock.Mock(ok=True, stdout="", stderr="")
            firewall.apply(ctx, cfg)
        self.assertEqual(State().get("ufw_rules"), [])
        self.assertEqual(State().get("firewall_backend"), "nftables")


if __name__ == "__main__":
    unittest.main()
