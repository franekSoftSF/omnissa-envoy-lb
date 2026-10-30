import unittest
from unittest import mock

import yaml

from tests.helpers import SHARED

from modules import network_manager as net
from modules import settings, vip_manager
from modules.common import Context, OmnissaError


class NetworkTest(unittest.TestCase):
    def setUp(self):
        self.cfg = settings.load(SHARED)

    def test_netplan_render_uses_device_id(self):
        with mock.patch.object(net, "netplan_device_id", return_value=("ethernets", "eth0")):
            text = net.render_netplan(self.cfg, "ens160", [("10.0.0.50", 24)])
        doc = yaml.safe_load(text)
        self.assertEqual(doc["network"]["ethernets"]["eth0"]["addresses"], ["10.0.0.50/24"])

    def test_prefix_from_primary_subnet(self):
        self.assertEqual(net.compute_prefix(self.cfg, "10.0.0.50", {"address": "10.0.0.5", "prefix": 24}), 24)
        with self.assertRaises(OmnissaError):
            net.compute_prefix(self.cfg, "10.9.0.50", {"address": "10.0.0.5", "prefix": 24})
        with self.assertRaises(OmnissaError):
            net.compute_prefix(self.cfg, "10.0.0.255", {"address": "10.0.0.5", "prefix": 24})

    def test_reconcile_is_idempotent_when_vip_present(self):
        ctx = Context(dry_run=True)
        with mock.patch.object(net, "interface_addresses", return_value=["10.0.0.5", "10.0.0.50"]), \
             mock.patch.object(net, "primary_address", return_value={"address": "10.0.0.5", "prefix": 24, "dynamic": False}), \
             mock.patch.object(net, "address_owner", return_value="ens160"), \
             mock.patch.object(net, "persist", return_value=False), \
             mock.patch.object(vip_manager, "run") as run:
            rep = vip_manager.reconcile(ctx, self.cfg, "ens160")
        self.assertEqual(rep["added"], [])
        self.assertEqual(rep["present"], ["10.0.0.50"])
        run.assert_not_called()

    def test_reconcile_refuses_duplicate_ip_in_network(self):
        ctx = Context(dry_run=True)
        with mock.patch.object(net, "interface_addresses", return_value=["10.0.0.5"]), \
             mock.patch.object(net, "primary_address", return_value={"address": "10.0.0.5", "prefix": 24, "dynamic": False}), \
             mock.patch.object(net, "address_owner", return_value=None), \
             mock.patch.object(vip_manager, "duplicate_in_use", return_value=True):
            with self.assertRaisesRegex(OmnissaError, "odpowiada w sieci"):
                vip_manager.reconcile(ctx, self.cfg, "ens160")

    def test_reconcile_plans_add(self):
        ctx = Context(dry_run=True)
        with mock.patch.object(net, "interface_addresses", return_value=["10.0.0.5"]), \
             mock.patch.object(net, "primary_address", return_value={"address": "10.0.0.5", "prefix": 24, "dynamic": False}), \
             mock.patch.object(net, "address_owner", return_value=None), \
             mock.patch.object(net, "persist", return_value=True), \
             mock.patch.object(vip_manager, "duplicate_in_use", return_value=False):
            rep = vip_manager.reconcile(ctx, self.cfg, "ens160")
        self.assertEqual(rep["added"], ["10.0.0.50/24"])
        self.assertEqual(ctx.planned[0]["category"], "vip")


if __name__ == "__main__":
    unittest.main()
