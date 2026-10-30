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


if __name__ == "__main__":
    unittest.main()
