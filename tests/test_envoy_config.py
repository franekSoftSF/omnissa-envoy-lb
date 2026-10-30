import unittest

import yaml

from tests.helpers import DEDICATED, SHARED, write_cfg

from modules import envoy_config, settings


def listeners(cfg):
    return yaml.safe_load(envoy_config.render(cfg))["static_resources"]["listeners"]


class EnvoyConfigTest(unittest.TestCase):
    def test_shared_vip_single_tcp_listener_with_sni(self):
        cfg = settings.load(SHARED)
        ls = listeners(cfg)
        tcp = [l for l in ls if l["address"]["socket_address"]["protocol"] == "TCP"]
        self.assertEqual(len(tcp), 1)
        chains = {fc["filter_chain_match"]["server_names"][0]: fc["filters"][0]["typed_config"]["cluster"]
                  for fc in tcp[0]["filter_chains"]}
        self.assertEqual(chains, {"tunnel.example.com": "tunnel_tcp_443", "seg.example.com": "seg_tcp_443"})
        self.assertEqual(tcp[0]["listener_filters"][0]["name"], "envoy.filters.listener.tls_inspector")
        self.assertTrue(tcp[0]["freebind"])

    def test_shared_vip_udp_goes_to_tunnel(self):
        cfg = settings.load(SHARED)
        udp = [l for l in listeners(cfg) if l["address"]["socket_address"]["protocol"] == "UDP"]
        self.assertEqual(len(udp), 1)
        route = udp[0]["listener_filters"][0]["typed_config"]["matcher"]["on_no_match"]["action"]["typed_config"]
        self.assertEqual(route["cluster"], "tunnel_udp_443")

    def test_dedicated_vips_separate_listeners(self):
        cfg = settings.load(DEDICATED)
        addrs = {(l["address"]["socket_address"]["address"], l["address"]["socket_address"]["port_value"],
                  l["address"]["socket_address"]["protocol"]) for l in listeners(cfg)}
        self.assertEqual(addrs, {("10.0.0.51", 8443, "TCP"), ("10.0.0.51", 8443, "UDP"), ("10.0.0.52", 443, "TCP")})

    def test_https_health_check_uses_transport_socket_match(self):
        cfg = settings.load(DEDICATED)
        doc = yaml.safe_load(envoy_config.render(cfg))
        seg = next(c for c in doc["static_resources"]["clusters"] if c["name"] == "seg_tcp_443")
        hc = seg["health_checks"][0]
        self.assertEqual(hc["http_health_check"]["path"], "/favicon.ico")
        self.assertEqual(hc["transport_socket_match_criteria"], {"omnissa_hc": "tls"})
        self.assertEqual(seg["transport_socket_matches"][-1]["match"], {})

    def test_udp_cluster_health_checked_via_tcp_port(self):
        cfg = settings.load(SHARED)
        doc = yaml.safe_load(envoy_config.render(cfg))
        udp = next(c for c in doc["static_resources"]["clusters"] if c["name"] == "tunnel_udp_443")
        ep = udp["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]
        self.assertEqual(ep["health_check_config"]["port_value"], 443)
        self.assertIn("tcp_health_check", udp["health_checks"][0])

    def test_source_ip_affinity_forces_ring_hash(self):
        cfg = settings.load(SHARED)
        model = envoy_config.build_model(cfg)
        pol = {c["name"]: c["lb_policy"] for c in model["clusters"]}
        self.assertEqual(pol["tunnel_tcp_443"], "RING_HASH")
        self.assertEqual(pol["seg_tcp_443"], "LEAST_REQUEST")

    def test_unknown_sni_default_chain(self):
        text = SHARED.read_text() + "\nsni: {unknown_sni_action: seg}\n"
        cfg = settings.load(write_cfg(text))
        tcp = next(l for l in listeners(cfg) if l["address"]["socket_address"]["protocol"] == "TCP")
        default = [fc for fc in tcp["filter_chains"] if "filter_chain_match" not in fc]
        self.assertEqual(len(default), 1)
        self.assertEqual(default[0]["filters"][0]["typed_config"]["cluster"], "seg_tcp_443")

    def test_admin_on_loopback(self):
        doc = yaml.safe_load(envoy_config.render(settings.load(SHARED)))
        self.assertEqual(doc["admin"]["address"]["socket_address"]["address"], "127.0.0.1")

    def test_dns_backends_use_strict_dns(self):
        text = SHARED.read_text().replace("[10.0.1.21, 10.0.1.22]", "[uag1.example.com, uag2.example.com]")
        model = envoy_config.build_model(settings.load(write_cfg(text)))
        seg = next(c for c in model["clusters"] if c["name"] == "seg_tcp_443")
        self.assertEqual(seg["type"], "STRICT_DNS")

    def test_prometheus_listener(self):
        text = SHARED.read_text() + "\nmonitoring: {prometheus: {enabled: true, address: 127.0.0.1, port: 9902}}\n"
        doc = yaml.safe_load(envoy_config.render(settings.load(write_cfg(text))))
        names = [l["name"] for l in doc["static_resources"]["listeners"]]
        self.assertIn("prometheus", names)
        self.assertIn("envoy_admin", [c["name"] for c in doc["static_resources"]["clusters"]])


if __name__ == "__main__":
    unittest.main()
