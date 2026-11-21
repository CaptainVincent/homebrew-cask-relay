import copy
import json
import subprocess
import tempfile
import unittest
from http.client import HTTPMessage
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import Request

from cask_relay import sync as cask_sync

digest, sync, tokens = cask_sync.digest, cask_sync.sync, cask_sync.tokens


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "Casks").mkdir()
        (self.root / "casks.txt").write_text("min\n")
        self.source = (
            'cask "min" do\n  version "1.0"\n  sha256 "'
            + "e" * 64
            + '"\n  url "https://vendor.example/min.zip"\n'
            '  disable! date: "2026-09-01", because: :fails_gatekeeper_check\nend\n'
        )
        self.meta = {
            "token": "min",
            "version": "1.0",
            "sha256": "e" * 64,
            "url": "https://vendor.example/min.zip",
            "disabled": True,
            "disable_reason": "fails_gatekeeper_check",
            "deprecated": False,
            "tap_git_head": "a" * 40,
            "ruby_source_path": "Casks/m/min.rb",
        }
        space = patch(
            "cask_relay.sync.shutil.disk_usage", return_value=Mock(free=20 * 1024**3)
        )
        space.start()
        self.addCleanup(space.stop)
        self.calls = []
        self.latest = "1.0"
        self.fail_bump = False
        self.fail_ruby = False
        self.no_write = False

    def fetch(self, url):
        if url.startswith("https://formulae.brew.sh/"):
            meta = copy.deepcopy(self.meta)
            meta["ruby_source_checksum"] = {"sha256": digest(self.source.encode())}
            return json.dumps(meta).encode()
        return self.source.encode()

    def runner(self, args):
        self.calls.append(args)
        if args[:2] == ["brew", "--repository"]:
            return str(self.root)
        if args[:2] == ["brew", "livecheck"]:
            path = self.root / "Casks/m/min.rb"
            current = (
                "2.0"
                if path.exists() and 'version "2.0"' in path.read_text()
                else "1.0"
            )
            return json.dumps(
                [
                    {
                        "cask": "owner/tools/min",
                        "version": {
                            "current": current,
                            "latest": self.latest,
                            "outdated": current != self.latest,
                        },
                    }
                ]
            )
        if args[:2] == ["brew", "bump-cask-pr"]:
            path = self.root / "Casks/m/min.rb"
            if not self.no_write:
                path.write_text(
                    path.read_text().replace('version "1.0"', 'version "2.0"')
                )
            if self.fail_bump:
                raise RuntimeError("native audit failed")
        if args[:2] == ["brew", "info"]:
            return json.dumps(
                {
                    "casks": [
                        {
                            "token": "min",
                            "version": self.latest,
                            "disabled": False,
                            "sha256": "e" * 64,
                            "url": self.meta["url"],
                        }
                    ]
                }
            )
        if args[0] == "ruby" and self.fail_ruby:
            raise RuntimeError("syntax failure")
        return "Syntax OK"

    def do_sync(self, **kwargs):
        return sync(self.root, download=self.fetch, run=self.runner, **kwargs)

    def files(self):
        return {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }

    def test_only_gatekeeper_line_removed_and_second_sync_is_noop(self):
        self.do_sync()
        text = (self.root / "Casks/m/min.rb").read_text()
        self.assertEqual(
            text,
            self.source.replace(
                '  disable! date: "2026-09-01", because: :fails_gatekeeper_check\n', ""
            ),
        )
        before = self.files()
        # Unrelated official repository commits must not churn the source lock.
        self.meta["tap_git_head"] = "b" * 40
        self.do_sync()
        self.assertEqual(before, self.files())

    def test_bad_token_and_duplicate_rejected(self):
        for text in ["../min\n", "min\nmin\n", "\n", "min;rm\n"]:
            (self.root / "casks.txt").write_text(text)
            with self.assertRaises(ValueError):
                tokens(self.root)

    def test_other_disable_reasons_are_not_overridden(self):
        self.meta["disable_reason"] = "malware"
        before = self.files()
        with self.assertRaisesRegex(ValueError, "Non-Gatekeeper"):
            self.do_sync()
        self.assertEqual(before, self.files())

    def test_source_hash_and_identity_rejected(self):
        def corrupt(url):
            data = self.fetch(url)
            return data + b"# tampered" if "raw.githubusercontent" in url else data

        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            sync(self.root, download=corrupt, run=self.runner)
        self.meta["ruby_source_path"] = "Casks/../min.rb"
        with self.assertRaisesRegex(ValueError, "source identity"):
            self.do_sync()

    def test_quarantine_hook_and_no_check_require_review(self):
        for addition in [
            '  run "xattr", "com.apple.quarantine"\n',
            "  sha256 :no_check\n",
        ]:
            old = self.source
            self.source += addition
            with self.assertRaisesRegex(ValueError, "Security-sensitive"):
                self.do_sync()
            self.source = old

    def test_all_sources_validate_before_any_write(self):
        (self.root / "casks.txt").write_text("min\nxld\n")
        before = self.files()
        with self.assertRaises(ValueError):
            self.do_sync()
        self.assertEqual(before, self.files())

    def test_dry_run_does_not_write_or_install(self):
        before = self.files()
        self.do_sync(dry_run=True)
        self.assertEqual(before, self.files())
        self.assertEqual(self.calls, [])

    def test_syntax_failure_rolls_back_all_generated_files(self):
        before = self.files()
        self.fail_ruby = True
        with self.assertRaisesRegex(RuntimeError, "syntax"):
            self.do_sync()
        self.assertEqual(before, self.files())

    def test_unlisted_and_locally_modified_recipes_are_not_destroyed(self):
        self.do_sync()
        path = self.root / "Casks/m/min.rb"
        path.write_text(path.read_text() + "# local edit\n")
        with self.assertRaisesRegex(ValueError, "outside sync"):
            self.do_sync()
        self.assertTrue(path.read_text().endswith("# local edit\n"))
        (self.root / "casks.txt").write_text("xld\n")
        with self.assertRaisesRegex(ValueError, "Unlisted"):
            self.do_sync()

    def test_publisher_host_change_stops_sync(self):
        self.do_sync()
        before = self.files()
        self.meta["url"] = "https://unreviewed.example/min.zip"
        with self.assertRaisesRegex(ValueError, "publisher host changed"):
            self.do_sync()
        self.assertEqual(before, self.files())

    def test_native_bump_is_write_only_and_preserved_across_sync(self):
        self.do_sync()
        self.latest = "2.0"
        self.do_sync(tap="owner/tools", bump=True)
        self.assertIn(
            [
                "brew",
                "bump-cask-pr",
                "--write-only",
                "--version=2.0",
                "owner/tools/min",
            ],
            self.calls,
        )
        self.assertFalse(
            any("install" in c or "upgrade" in c or "trust" in c for c in self.calls)
        )
        before = self.files()
        self.do_sync()
        self.assertEqual(before, self.files())
        self.source = self.source.replace('version "1.0"', 'version "1.1"')
        self.do_sync()
        self.assertIn('version "2.0"', (self.root / "Casks/m/min.rb").read_text())
        self.source = self.source.replace('version "1.1"', 'version "3.0"')
        self.do_sync()
        self.assertIn('version "3.0"', (self.root / "Casks/m/min.rb").read_text())

    def test_native_bump_failure_and_silent_noop_are_rolled_back(self):
        self.do_sync()
        self.latest = "2.0"
        before = self.files()
        self.fail_bump = True
        with self.assertRaisesRegex(RuntimeError, "audit"):
            self.do_sync(tap="owner/tools", bump=True)
        self.assertEqual(before, self.files())
        self.fail_bump, self.no_write = False, True
        with self.assertRaisesRegex(ValueError, "requested version"):
            self.do_sync(tap="owner/tools", bump=True)
        self.assertEqual(before, self.files())

    def test_livecheck_errors_and_wrong_checkout_fail_closed(self):
        self.do_sync()
        before = self.files()

        def bad(args):
            if args[:2] == ["brew", "livecheck"]:
                return json.dumps(
                    [{"cask": "min", "status": "error", "messages": ["offline"]}]
                )
            return self.runner(args)

        with self.assertRaisesRegex(ValueError, "Livecheck"):
            sync(self.root, tap="owner/tools", download=self.fetch, run=bad)
        self.assertEqual(before, self.files())
        with self.assertRaisesRegex(ValueError, "not this checkout"):
            sync(
                self.root,
                tap="owner/tools",
                download=self.fetch,
                run=lambda args: "/wrong/path",
            )

    def test_upstream_preview_never_bumps(self):
        self.do_sync()
        self.latest = "2.0"
        before = self.files()
        report = self.do_sync(dry_run=True, tap="owner/tools", bump=True)
        self.assertEqual(report["upstream_updates"][0]["latest"], "2.0")
        self.assertFalse(any(c[:2] == ["brew", "bump-cask-pr"] for c in self.calls))
        self.assertEqual(before, self.files())

    def test_reenabled_official_recipe_is_preserved_without_new_exception(self):
        self.meta["disabled"] = False
        self.source = self.source.replace(
            '  disable! date: "2026-09-01", because: :fails_gatekeeper_check\n', ""
        )
        self.do_sync()
        self.assertEqual((self.root / "Casks/m/min.rb").read_text(), self.source)

    def test_multiline_disable_is_not_partially_removed(self):
        self.source = self.source.replace(", because:", ",\n    because:")
        before = self.files()
        with self.assertRaisesRegex(ValueError, "disable format"):
            self.do_sync()
        self.assertEqual(before, self.files())

    def test_symlink_output_and_unverified_metadata_are_rejected(self):
        outside = self.root / "owner-data.rb"
        outside.write_text("do not overwrite")
        (self.root / "Casks/m").mkdir()
        (self.root / "Casks/m/min.rb").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.do_sync()
        self.assertEqual(outside.read_text(), "do not overwrite")
        (self.root / "Casks/m/min.rb").unlink()
        self.meta["sha256"] = "no_check"
        with self.assertRaisesRegex(ValueError, "artifact checksum"):
            self.do_sync()

    def test_native_bump_storage_guard_precedes_any_update(self):
        before = self.files()
        with (
            patch("cask_relay.sync.shutil.disk_usage", return_value=Mock(free=1024)),
            self.assertRaisesRegex(ValueError, "reserve"),
        ):
            self.do_sync(tap="owner/tools", bump=True)
        self.assertEqual(before, self.files())
        self.assertEqual(self.calls, [])

    def test_malformed_json_aborts_without_writes(self):
        before = self.files()
        with self.assertRaisesRegex(ValueError, "Invalid JSON from Homebrew API"):
            sync(self.root, download=lambda url: b"{broken", run=self.runner)
        self.assertEqual(before, self.files())

    def test_fetch_rejects_non_source_endpoints_without_network(self):
        with patch("cask_relay.sync.urllib.request.build_opener") as opener:
            for url in [
                "file:///etc/passwd",
                "http://formulae.brew.sh/cask/min",
                "https://other.example/cask",
                "https://user@formulae.brew.sh/cask/min",
            ]:
                with self.assertRaisesRegex(ValueError, "first-party HTTPS"):
                    cask_sync.fetch(url)
            opener.assert_not_called()

    def test_command_failure_reports_both_stream_tails(self):
        result = subprocess.CompletedProcess(
            ["brew", "livecheck"], 1, b"JSON status", b"." * 1400 + b"last error"
        )
        with (
            patch("cask_relay.sync.subprocess.run", return_value=result),
            self.assertRaisesRegex(RuntimeError, "last error.*JSON status"),
        ):
            cask_sync.command(["brew", "livecheck"])

    def test_source_redirect_is_not_followed(self):
        with self.assertRaisesRegex(ValueError, "Redirected"):
            cask_sync.NoSourceRedirect().redirect_request(
                Request("https://formulae.brew.sh/"),
                BytesIO(),
                302,
                "moved",
                HTTPMessage(),
                "https://other.example/",
            )

    def policy_runner(self, baseline):
        def run(args):
            current = self.files()
            if args[3] == "show":
                name = args[4].removeprefix("HEAD:")
                if name not in baseline:
                    raise RuntimeError("not in baseline")
                return baseline[name].decode()
            if args[3] == "diff":
                return "\0".join(
                    name for name in baseline if baseline[name] != current.get(name)
                )
            if args[3] == "ls-files":
                return "\0".join(set(current) - set(baseline))
            self.fail("Unexpected git operation")

        return run

    def candidate(self, text):
        (self.root / "Casks/m/min.rb").write_text(text)
        path = self.root / "source-lock.json"
        data = json.loads(path.read_text())
        data["min"]["recipe_sha256"] = digest(text.encode())
        path.write_text(json.dumps(data))

    def test_routine_policy_accepts_only_newer_version_and_hash_data(self):
        self.do_sync()
        baseline = self.files()
        text = (self.root / "Casks/m/min.rb").read_text()
        self.candidate(
            text.replace('version "1.0"', 'version "2.0"').replace("e" * 64, "f" * 64)
        )
        self.assertTrue(
            cask_sync.update_policy(self.root, self.policy_runner(baseline))[
                "automatic"
            ]
        )

    def test_routine_policy_requires_review_for_same_version_repack_and_downgrade(self):
        self.do_sync()
        baseline = self.files()
        text = (self.root / "Casks/m/min.rb").read_text()
        for changed in [
            text.replace("e" * 64, "f" * 64),
            text.replace('version "1.0"', 'version "0.9"'),
        ]:
            self.candidate(changed)
            self.assertFalse(
                cask_sync.update_policy(self.root, self.policy_runner(baseline))[
                    "automatic"
                ]
            )

    def test_routine_policy_rejects_url_hooks_flags_and_comments(self):
        self.do_sync()
        baseline = self.files()
        text = (self.root / "Casks/m/min.rb").read_text()
        for changed in [
            text.replace("vendor.example/min.zip", "vendor.example/other.zip"),
            text + 'postflight_steps do\n  run "unexpected"\nend\n',
            text + "auto_updates true\n",
            text + "# unrelated change\n",
        ]:
            self.candidate(changed.replace('version "1.0"', 'version "2.0"'))
            self.assertFalse(
                cask_sync.update_policy(self.root, self.policy_runner(baseline))[
                    "automatic"
                ]
            )

    def test_routine_policy_preserves_architecture_checksum_structure(self):
        old = (
            'cask "min" do\n  version "1.9"\n  sha256 arm: "'
            + "e" * 64
            + '",\n         intel: "'
            + "f" * 64
            + '"\nend\n'
        )
        new = (
            old.replace('version "1.9"', 'version "1.10"')
            .replace("e" * 64, "a" * 64)
            .replace("f" * 64, "b" * 64)
        )
        self.assertTrue(cask_sync.routine_recipe_change(old, new)[0])
        self.assertFalse(
            cask_sync.routine_recipe_change(old, new.replace("intel:", "arm:"))[0]
        )

    def test_routine_policy_requires_review_for_new_selection_or_code_files(self):
        self.do_sync()
        baseline = self.files()
        (self.root / "casks.txt").write_text("min\n# changed selection policy\n")
        self.assertFalse(
            cask_sync.update_policy(self.root, self.policy_runner(baseline))[
                "automatic"
            ]
        )
        (self.root / "casks.txt").write_bytes(baseline["casks.txt"])
        (self.root / "new-code.py").write_text("unexpected")
        self.assertFalse(
            cask_sync.update_policy(self.root, self.policy_runner(baseline))[
                "automatic"
            ]
        )

    def test_routine_policy_verifies_provenance_and_recorded_bytes(self):
        self.do_sync()
        baseline = self.files()
        path = self.root / "source-lock.json"
        data = json.loads(path.read_text())
        data["min"]["source_url"] = "https://untrusted.example/min.rb"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "Nonofficial"):
            cask_sync.update_policy(self.root, self.policy_runner(baseline))
        path.write_bytes(baseline["source-lock.json"])
        (self.root / "Casks/m/min.rb").write_text("unrecorded edit")
        with self.assertRaisesRegex(ValueError, "Unverified"):
            cask_sync.update_policy(self.root, self.policy_runner(baseline))

    def test_routine_policy_accepts_unchanged_recipes_and_initial_setup_is_manual(self):
        self.do_sync()
        baseline = self.files()
        self.assertTrue(
            cask_sync.update_policy(self.root, self.policy_runner(baseline))[
                "automatic"
            ]
        )
        initial = {"casks.txt": baseline["casks.txt"]}
        self.assertFalse(
            cask_sync.update_policy(self.root, self.policy_runner(initial))["automatic"]
        )

    def test_source_code_drift_after_bump_still_needs_manual_reconciliation(self):
        self.do_sync()
        self.latest = "2.0"
        self.do_sync(tap="owner/tools", bump=True)
        before = self.files()
        self.source += "depends_on :macos\n"
        with self.assertRaisesRegex(ValueError, "Official code changed"):
            self.do_sync()
        self.assertEqual(before, self.files())

    def test_non_numeric_upstream_version_is_not_passed_to_native_bump(self):
        self.do_sync()
        self.latest = '2.0";unexpected'
        with self.assertRaisesRegex(ValueError, "version format"):
            self.do_sync(tap="owner/tools", bump=True)
        self.assertFalse(any(c[:2] == ["brew", "bump-cask-pr"] for c in self.calls))

    def test_policy_with_real_local_git_baseline(self):
        self.do_sync()
        commands = [
            ["git", "init", "-q"],
            ["git", "add", "--", "casks.txt", "Casks", "source-lock.json"],
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "reviewed baseline",
            ],
        ]
        for args in commands:
            subprocess.run(
                args, cwd=self.root, check=True, capture_output=True, text=True
            )
        text = (self.root / "Casks/m/min.rb").read_text()
        self.candidate(text.replace('version "1.0"', 'version "2.0"'))
        self.assertTrue(cask_sync.update_policy(self.root)["automatic"])
        self.candidate(
            text.replace('version "1.0"', 'version "2.0"') + "auto_updates true\n"
        )
        self.assertFalse(cask_sync.update_policy(self.root)["automatic"])

    def test_preview_refuses_stale_recipe_before_native_check(self):
        self.do_sync()
        self.calls.clear()
        self.source = self.source.replace('version "1.0"', 'version "1.1"')
        before = self.files()
        with self.assertRaisesRegex(ValueError, "before invoking Homebrew"):
            self.do_sync(tap="owner/tools", dry_run=True)
        self.assertFalse(self.calls)
        self.assertEqual(before, self.files())

    def test_native_candidate_requires_routine_diff_before_metadata_read(self):
        self.do_sync()
        self.latest = "2.0"
        before = self.files()

        def run(args):
            result = self.runner(args)
            if args[:2] == ["brew", "bump-cask-pr"]:
                path = self.root / "Casks/m/min.rb"
                path.write_text(path.read_text() + "# requires review\n")
            return result

        with self.assertRaisesRegex(ValueError, "Native bump requires review"):
            sync(self.root, tap="owner/tools", bump=True, download=self.fetch, run=run)
        self.assertFalse(any(args[:2] == ["brew", "info"] for args in self.calls))
        self.assertEqual(before, self.files())

    def test_trailing_zero_is_not_an_upgrade(self):
        self.do_sync()
        self.latest = "1.0.0"
        self.assertEqual(
            self.do_sync(tap="owner/tools", bump=True)["upstream_updates"], []
        )
        self.assertFalse(
            any(args[:2] == ["brew", "bump-cask-pr"] for args in self.calls)
        )

    def test_generated_metadata_cannot_be_executable(self):
        self.do_sync()
        before = self.files()
        (self.root / "source-lock.json").chmod(0o755)
        with self.assertRaisesRegex(ValueError, "non-executable"):
            cask_sync.update_policy(self.root, self.policy_runner(before))

    def test_new_disable_shape_requires_review(self):
        self.source = self.source.replace(
            "because: :fails_gatekeeper_check",
            "because: :fails_gatekeeper_check # changed",
        )
        before = self.files()
        with self.assertRaisesRegex(ValueError, "disable format"):
            self.do_sync()
        self.assertEqual(before, self.files())


if __name__ == "__main__":
    unittest.main()
