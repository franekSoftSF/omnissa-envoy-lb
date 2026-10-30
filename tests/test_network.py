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
        with mock.patch.object(net, "interface_cidrs", return_value={"10.0.0.5": 24, "10.0.0.50": 24}), \
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
        with mock.patch.object(net, "interface_cidrs", return_value={"10.0.0.5": 24}), \
             mock.patch.object(net, "primary_address", return_value={"address": "10.0.0.5", "prefix": 24, "dynamic": False}), \
             mock.patch.object(net, "address_owner", return_value=None), \
             mock.patch.object(vip_manager, "duplicate_in_use", return_value=True):
            with self.assertRaisesRegex(OmnissaError, "odpowiada w sieci"):
                vip_manager.reconcile(ctx, self.cfg, "ens160")

    def test_reconcile_plans_add(self):
        ctx = Context(dry_run=True)
        with mock.patch.object(net, "interface_cidrs", return_value={"10.0.0.5": 24}), \
             mock.patch.object(net, "primary_address", return_value={"address": "10.0.0.5", "prefix": 24, "dynamic": False}), \
             mock.patch.object(net, "address_owner", return_value=None), \
             mock.patch.object(net, "persist", return_value=True), \
             mock.patch.object(vip_manager, "duplicate_in_use", return_value=False):
            rep = vip_manager.reconcile(ctx, self.cfg, "ens160")
        self.assertEqual(rep["added"], ["10.0.0.50/24"])
        self.assertEqual(ctx.planned[0]["category"], "vip")

    def _patches(self, cidrs, managed=None, preexisting=None):
        from modules.common import State

        st = State()
        st.set("managed_vips", managed or [])
        st.set("preexisting_vips", preexisting or [])
        st.save(Context())
        return [mock.patch.object(net, "interface_cidrs", return_value=cidrs),
                mock.patch.object(net, "interface_addresses", return_value=list(cidrs)),
                mock.patch.object(net, "primary_address", return_value={"address": "10.0.0.5", "prefix": 24, "dynamic": False}),
                mock.patch.object(net, "address_owner", return_value="ens160"),
                mock.patch.object(net, "persist", return_value=False),
                mock.patch.object(vip_manager, "duplicate_in_use", return_value=False)]

    def _reconcile(self, cfg, patches):
        from contextlib import ExitStack

        ctx = Context(dry_run=True)
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return vip_manager.reconcile(ctx, cfg, "ens160"), ctx

    def test_prefix_change_of_managed_vip_is_applied(self):
        cfg = settings.load(SHARED)
        cfg["vip"]["prefix"] = 32
        managed = [{"address": "10.0.0.50", "cidr": "10.0.0.50/24", "interface": "ens160"}]
        rep, ctx = self._reconcile(cfg, self._patches({"10.0.0.5": 24, "10.0.0.50": 24}, managed))
        self.assertEqual(rep["removed"], ["10.0.0.50/24"])
        self.assertEqual(rep["added"], ["10.0.0.50/32"])

    def test_prefix_mismatch_of_foreign_address_is_refused(self):
        cfg = settings.load(SHARED)
        cfg["vip"]["prefix"] = 32
        with self.assertRaisesRegex(OmnissaError, "nie został dodany przez instalator"):
            self._reconcile(cfg, self._patches({"10.0.0.5": 24, "10.0.0.50": 24}))

    def test_preexisting_vip_becomes_managed_when_installer_adds_it(self):
        from modules.common import State

        cfg = settings.load(SHARED)
        patches = self._patches({"10.0.0.5": 24}, preexisting=["10.0.0.50"])
        ctx = Context()  # zapis stanu (bez dry-run); polecenia systemowe zamockowane
        ctx.transaction = mock.Mock()
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(mock.patch.object(vip_manager, "run"))
            stack.enter_context(mock.patch.object(vip_manager, "announce"))
            vip_manager.reconcile(ctx, cfg, "ens160")
        st = State()
        self.assertEqual([m["address"] for m in st.get("managed_vips")], ["10.0.0.50"])
        self.assertEqual(st.get("preexisting_vips"), [])


if __name__ == "__main__":
    unittest.main()
