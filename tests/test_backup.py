import unittest

from tests.helpers import TMP

from modules import backup
from modules.common import Context, write_managed_file


class BackupTest(unittest.TestCase):
    def test_transaction_restores_files(self):
        ctx = Context()
        ctx.transaction = backup.Transaction.open("tx-test-1", ctx)
        existing = TMP / "etc" / "existing.conf"
        existing.write_text("old\n")
        new = TMP / "etc" / "new.conf"
        self.assertTrue(write_managed_file(ctx, existing, "new\n"))
        self.assertTrue(write_managed_file(ctx, new, "created\n"))
        self.assertFalse(write_managed_file(ctx, new, "created\n"))  # idempotencja
        self.assertTrue(write_managed_file(ctx, existing, "newer\n"))  # backup tylko pierwszej wersji
        ctx.transaction.restore_files(Context())
        self.assertEqual(existing.read_text(), "old\n")
        self.assertFalse(new.exists())

    def test_manifest_format_shared_with_bash(self):
        ctx = Context()
        tx = backup.Transaction.open("tx-test-2", ctx)
        target = TMP / "etc" / "bash-managed.conf"
        target.write_text("a")
        # wpis w formacie zapisywanym przez install.sh (tx_backup)
        (tx.dir / "files" / str(target).lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
        (tx.dir / ("files" + str(target))).write_text("a")
        with tx.manifest.open("a") as fh:
            fh.write(f"file\t{target}\t1\tfiles{target}\n")
        target.write_text("b")
        tx.restore_files(Context())
        self.assertEqual(target.read_text(), "a")

    def test_dry_run_writes_nothing(self):
        ctx = Context(dry_run=True)
        p = TMP / "etc" / "dry.conf"
        self.assertTrue(write_managed_file(ctx, p, "x"))
        self.assertFalse(p.exists())
        self.assertEqual(ctx.planned[0]["action"], "write")


if __name__ == "__main__":
    unittest.main()
