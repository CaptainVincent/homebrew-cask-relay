import json
import unittest
from pathlib import Path

from cask_relay.sync import digest, tokens, validate_recipe


class SnapshotTests(unittest.TestCase):
    def test_allowlist_and_lock_match_committed_casks(self):
        root = Path(__file__).resolve().parents[1]
        names = set(tokens(root))
        lock = json.loads((root / "source-lock.json").read_bytes())
        paths = set((root / "Casks").rglob("*.rb"))

        self.assertEqual(set(lock), names)
        self.assertEqual(
            paths, {root / "Casks" / name[0] / f"{name}.rb" for name in names}
        )
        for path in sorted(paths):
            with self.subTest(cask=path.stem):
                data = path.read_bytes()
                validate_recipe(path.stem, data.decode("utf-8"))
                self.assertEqual(digest(data), lock[path.stem]["recipe_sha256"])
