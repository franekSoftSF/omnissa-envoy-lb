import unittest

from tests.helpers import DEDICATED, EXAMPLE, SHARED, write_cfg

from modules import settings
from modules.common import OmnissaError

BASE = """
vip: {mode: shared, shared: 10.0.0.50}
services:
  tunnel: {fqdn: tunnel.example.com, backends: [10.0.1.11]}
  seg: {fqdn: seg.example.com, backends: [10.0.1.21]}
"""


class SettingsTest(unittest.TestCase):
    def test_examples_are_valid(self):
        for p in (SHARED, DEDICATED, EXAMPLE):
            cfg = settings.load(p)
            self.assertEqual(settings.enabled_services(cfg), ["tunnel", "seg"])

    def test_shared_vip_used_by_both_services(self):
        cfg = settings.load(SHARED)
        self.assertEqual(settings.all_vips(cfg), ["10.0.0.50"])

    def test_dedicated_vips(self):
        cfg = settings.load(DEDICATED)
        self.assertEqual(settings.all_vips(cfg), ["10.0.0.51", "10.0.0.52"])

    def test_interface_override(self):
        cfg = settings.load(SHARED, interface_override="ens192")
        self.assertEqual(cfg["network"]["interface"], "ens192")

    def test_udp_cannot_be_shared(self):
        p = write_cfg(BASE + """
  seg:
    fqdn: seg.example.com
    backends: [10.0.1.21]
    listeners: [{protocol: udp, port: 443}]
""")
        with self.assertRaisesRegex(OmnissaError, "UDP nie obsługuje SNI"):
            settings.load(p)

    def test_dedicated_requires_distinct_vips(self):
        p = write_cfg("""
vip: {mode: dedicated, tunnel: 10.0.0.51, seg: 10.0.0.51}
services:
  tunnel: {fqdn: tunnel.example.com, backends: [10.0.1.11]}
  seg: {fqdn: seg.example.com, backends: [10.0.1.21]}
""")
        with self.assertRaisesRegex(OmnissaError, "jest już używany"):
            settings.load(p)

    def test_errors_are_collected(self):
        p = write_cfg("""
vip: {mode: shared, shared: 999.1.1.1}
services:
  tunnel: {fqdn: "bad_name", backends: []}
  seg: {fqdn: seg.example.com, backends: [10.0.1.21]}
envoy: {admin_address: 0.0.0.0}
""")
        with self.assertRaises(OmnissaError) as ctx:
            settings.load(p)
        msg = str(ctx.exception)
        for fragment in ("vip.shared", "services.tunnel.fqdn", "services.tunnel.backends", "loopback"):
            self.assertIn(fragment, msg)

    def test_duplicate_sni_rejected(self):
        p = write_cfg(BASE.replace("seg.example.com", "tunnel.example.com"))
        with self.assertRaisesRegex(OmnissaError, "SNI"):
            settings.load(p)

    def test_admin_must_be_ipv4_loopback(self):
        with self.assertRaisesRegex(OmnissaError, "loopback IPv4"):
            settings.load(write_cfg(BASE + "envoy: {admin_address: '::1'}\n"))

    def test_init_config_shared(self):
        from modules import wizard

        cfg = wizard.build_settings({"vip": "10.0.0.50", "interface": "ens160",
                                     "tunnel_fqdn": "tunnel.example.com", "tunnel_backends": ["10.0.1.11"],
                                     "seg_fqdn": "seg.example.com", "seg_backends": ["10.0.1.21", "10.0.1.22"]})
        loaded = settings.load(write_cfg(settings.dump_yaml(cfg)))
        self.assertEqual(settings.all_vips(loaded), ["10.0.0.50"])
        self.assertEqual([l["protocol"] for l in loaded["services"]["tunnel"]["listeners"]], ["tcp", "udp"])

    def test_init_config_dedicated_and_disabled_service(self):
        from modules import wizard

        cfg = wizard.build_settings({"mode": "dedicated", "tunnel_vip": "10.0.0.51", "seg": False,
                                     "tunnel_fqdn": "tunnel.example.com", "tunnel_backends": ["uag1.example.com"]})
        loaded = settings.load(write_cfg(settings.dump_yaml(cfg)))
        self.assertEqual(settings.enabled_services(loaded), ["tunnel"])
        self.assertEqual(loaded["services"]["tunnel"]["listeners"][0]["port"], 8443)

    def test_init_config_reports_errors(self):
        from modules import wizard

        with self.assertRaisesRegex(OmnissaError, "services.seg.fqdn"):
            wizard.build_settings({"vip": "10.0.0.50", "tunnel_fqdn": "tunnel.example.com",
                                   "tunnel_backends": ["10.0.1.11"], "seg_backends": ["10.0.1.21"]})

    def test_duration(self):
        self.assertEqual(settings.duration("500ms", "x"), "0.5s")
        self.assertEqual(settings.duration("2m", "x"), "120s")
        self.assertEqual(settings.duration(5, "x"), "5s")
        with self.assertRaises(OmnissaError):
            settings.duration("5 minutes", "x")


if __name__ == "__main__":
    unittest.main()
