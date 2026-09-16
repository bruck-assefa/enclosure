from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("updater", Path(__file__).resolve().parents[1] / "tools/update_checkout.py")
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.git("init", "-b", "master")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "core.autocrlf", "false")
        (self.root / "main.py").write_bytes(b"old\n")
        with closing(sqlite3.connect(self.root / "enclosure.db")) as db:
            db.execute("create table events (value text)")
            db.execute("insert into events values ('preserve me')")
            db.commit()
        self.git("add", ".")
        self.git("commit", "-m", "old")
        self.old = self.git("rev-parse", "HEAD").strip()
        self.git("rm", "--cached", "enclosure.db")
        (self.root / ".gitignore").write_bytes(b"/enclosure.db\n")
        (self.root / "main.py").write_bytes(b"new\n")
        self.git("add", ".")
        self.git("commit", "-m", "new")
        self.target = self.git("rev-parse", "HEAD").strip()
        self.git("update-ref", "refs/remotes/origin/master", self.target)
        # Reproduce an SCP deployment: files new, Git metadata old.
        self.git("reset", "--mixed", self.old)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], stderr=subprocess.PIPE).decode()

    def test_reconciles_deployed_files_and_preserves_database(self):
        original = (self.root / "enclosure.db").read_bytes()
        release = updater.apply(self.root, self.target, "pi", Path(self.temp.name) / "backups")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.target)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual((self.root / "enclosure.db").read_bytes(), original)
        with closing(sqlite3.connect(release / "enclosure-backup.db")) as db:
            self.assertEqual(db.execute("select value from events").fetchone()[0], "preserve me")
        self.assertEqual(json.loads((release / "release.json").read_text())["runtime_verification"], "pending")
        self.git("merge-base", "--is-ancestor", self.old, self.target)

    def test_normalizes_only_equivalent_line_endings(self):
        (self.root / "main.py").write_bytes(b"new\r\n")
        updater.apply(self.root, self.target, "pi", Path(self.temp.name) / "backups")
        self.assertEqual((self.root / "main.py").read_bytes(), b"new\n")

    def test_refuses_unexpected_server_edit(self):
        (self.root / "main.py").write_bytes(b"unexpected edit\n")
        with self.assertRaises(ValueError):
            updater.plan(self.root, self.target, "pi")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.old)

    def test_dry_run_changes_nothing(self):
        before = (self.root / "enclosure.db").read_bytes()
        updater.plan(self.root, self.target, "pi")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.old)
        self.assertEqual((self.root / "enclosure.db").read_bytes(), before)

    def test_refuses_non_fast_forward(self):
        (self.root / "main.py").write_bytes(b"other change\n")
        self.git("add", "main.py")
        self.git("commit", "-m", "diverged")
        with self.assertRaises(subprocess.CalledProcessError):
            updater.plan(self.root, self.target, "pi")

    def test_refuses_unrelated_untracked_file(self):
        (self.root / "unreviewed.txt").write_text("keep")
        with self.assertRaises(ValueError):
            updater.plan(self.root, self.target, "pi")


if __name__ == "__main__":
    unittest.main()
