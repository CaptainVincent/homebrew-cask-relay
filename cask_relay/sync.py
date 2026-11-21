"""Allowlisted source sync. Invalid metadata propagates to the CLI; native-write failures roll back."""

import hashlib
import json
import re
import shutil
import subprocess
import urllib.request
from difflib import unified_diff
from pathlib import Path
from urllib.parse import urlparse

from .recipe import VERSION, recipe_shape, routine_recipe_change, version_key

GATEKEEPER = "fails_gatekeeper_check"
# shortcut: only single-line official disable syntax; review the parser if upstream makes it multiline.
DISABLE = re.compile(
    r'(?m)^  disable! date: "[0-9]{4}-[0-9]{2}-[0-9]{2}", because: :fails_gatekeeper_check\n'
)
HEX = re.compile(r"[0-9a-f]{64}")


def parse_json(data, source):
    try:
        return json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f"Invalid JSON from {source}") from error


def digest(data):
    return hashlib.sha256(data).hexdigest()


def tokens(root):
    names = [
        line.split("#", 1)[0].strip()
        for line in (root / "casks.txt").read_text().splitlines()
    ]
    names = [name for name in names if name]
    if not names or len(names) != len(set(names)):
        raise ValueError("casks.txt must contain unique, nonempty tokens")
    if any(
        not re.fullmatch(r"[a-z0-9][a-z0-9+@._-]*", name) or ".." in name
        for name in names
    ):
        raise ValueError("Invalid cask token")
    return names


class NoSourceRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Redirected source requires manual review")


def fetch(url):
    endpoint = urlparse(url)
    if endpoint.scheme != "https" or endpoint.netloc not in {
        "formulae.brew.sh",
        "raw.githubusercontent.com",
    }:
        raise ValueError("Only first-party HTTPS metadata/source endpoints are allowed")
    request = urllib.request.Request(url, headers={"User-Agent": "homebrew-cask-relay"})
    with urllib.request.build_opener(NoSourceRedirect()).open(
        request, timeout=30
    ) as response:
        data = response.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("Unexpectedly large source response")
    return data


def command(args):
    result = subprocess.run(args, check=False, capture_output=True, timeout=900)
    if result.returncode:
        stderr = result.stderr.decode("utf-8", "replace").strip()[-1200:]
        stdout = result.stdout.decode("utf-8", "replace").strip()[-1200:]
        raise RuntimeError(
            f"{args[0]} command failed ({result.returncode}): "
            f"stderr={stderr!r}; stdout={stdout!r}"
        )
    return result.stdout.decode("utf-8")


def validate_recipe(token, text):
    if not re.search(
        r'^cask ["\']' + re.escape(token) + r'["\'] do$', text, re.MULTILINE
    ):
        raise ValueError(f"Wrong cask declaration: {token}")
    if re.search(r"^[ \t]*disable!", text, re.MULTILINE):
        raise ValueError(f"Unexpected disable declaration: {token}")
    if re.search(
        r"com\.apple\.quarantine|remove_quarantine|\bspctl\b|sha256\s+:no_check", text
    ):
        raise ValueError(f"Security-sensitive recipe requires manual review: {token}")


def official(token, download):
    meta = parse_json(
        download(f"https://formulae.brew.sh/api/cask/{token}.json"),
        f"Homebrew API ({token})",
    )
    if meta.get("token") != token or not HEX.fullmatch(meta.get("sha256", "")):
        raise ValueError(f"Missing official token/artifact checksum: {token}")
    for active, reason in [
        ("disabled", "disable_reason"),
        ("deprecated", "deprecation_reason"),
    ]:
        if not isinstance(meta.get(active), bool):
            raise TypeError(f"Missing official {active} state: {token}")
        if meta[active] and meta.get(reason) != GATEKEEPER:
            raise ValueError(
                f"Non-Gatekeeper {active} reason: {token}: {meta.get(reason)}"
            )
    commit = meta["tap_git_head"]
    source_path = meta["ruby_source_path"]
    if (
        not re.fullmatch(r"[0-9a-f]{40}", commit)
        or source_path != f"Casks/{token[0]}/{token}.rb"
    ):
        raise ValueError(f"Unexpected official source identity: {token}")
    url = f"https://raw.githubusercontent.com/Homebrew/homebrew-cask/{commit}/{source_path}"
    raw = download(url)
    source_sha = meta["ruby_source_checksum"]["sha256"]
    if digest(raw) != source_sha:
        raise ValueError(f"Official source checksum mismatch: {token}")
    text, count = DISABLE.subn("", raw.decode("utf-8"))
    if count != (1 if meta.get("disabled") else 0):
        raise ValueError(f"Unexpected Gatekeeper disable format: {token}")
    validate_recipe(token, text)
    endpoint = urlparse(meta["url"])
    if (
        endpoint.scheme != "https"
        or not endpoint.hostname
        or endpoint.username
        or endpoint.password
    ):
        raise ValueError(f"Non-HTTPS artifact URL: {token}")
    return text, {
        "source_url": url,
        "source_sha256": source_sha,
        "artifact_host": endpoint.hostname,
        "official_version": meta["version"],
        "official_recipe_sha256": digest(text.encode()),
    }


def prepare(root, names, download):
    lock_path = root / "source-lock.json"
    if lock_path.is_symlink():
        raise ValueError("source-lock.json must not be a symlink")
    previous = (
        parse_json(lock_path.read_bytes(), "source-lock.json")
        if lock_path.exists()
        else {}
    )
    expected = {root / "Casks" / name[0] / (name + ".rb") for name in names}
    if (root / "Casks").is_symlink() or any(
        p.is_symlink() for p in (root / "Casks").rglob("*")
    ):
        raise ValueError("Casks must not contain symlinks")
    if set((root / "Casks").rglob("*.rb")) - expected or set(previous) - set(names):
        raise ValueError(
            "Unlisted casks remain: migrate their consumers before removing them"
        )
    plan, records = {}, {}
    for name in names:
        path = root / "Casks" / name[0] / (name + ".rb")
        text, record = official(name, download)
        old = previous.get(name)
        if old:
            if record["artifact_host"] != old["artifact_host"]:
                raise ValueError(
                    f"Artifact publisher host changed: {name}; review manually"
                )
            if not path.is_file() or digest(path.read_bytes()) != old["recipe_sha256"]:
                raise ValueError(
                    f"Generated recipe changed outside sync: {name}; review it first"
                )
            if record["source_sha256"] == old["source_sha256"]:
                text, record = path.read_bytes().decode("utf-8"), dict(old)
            elif old["recipe_sha256"] != old["official_recipe_sha256"]:
                current = path.read_bytes().decode("utf-8")
                current_shape, official_shape = (
                    recipe_shape(current),
                    recipe_shape(text),
                )
                if (
                    current_shape is None
                    or official_shape is None
                    or current_shape[0] != official_shape[0]
                ):
                    raise ValueError(
                        f"Official code changed after an upstream-only bump: {name}; reconcile manually"
                    )
                if (
                    version_key(current_shape[1]) == version_key(official_shape[1])
                    and current_shape[2] != official_shape[2]
                ):
                    raise ValueError(
                        f"Same-version artifact replacement requires review: {name}"
                    )
                if version_key(official_shape[1]) <= version_key(current_shape[1]):
                    text = current
        elif path.exists():
            raise ValueError(f"Untracked recipe would be overwritten: {name}")
        validate_recipe(name, text)
        record["recipe_sha256"] = digest(text.encode())
        plan[path], records[name] = text.encode(), record
    return plan, records


def upstream_updates(root, names, records, tap, bump, dry_run, run):
    tap = tap.lower()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9_-]*", tap):
        raise ValueError("Use a fully qualified owner/tap")
    if Path(run(["brew", "--repository", tap]).strip()).resolve() != root.resolve():
        raise ValueError(
            "The named tap is not this checkout; refusing to modify another installation"
        )
    full_names = [f"{tap}/{name}" for name in names]
    checks = parse_json(
        run(["brew", "livecheck", "--cask", "--json", *full_names]), "brew livecheck"
    )
    if not isinstance(checks, list) or len(checks) != len(names):
        raise ValueError("Incomplete livecheck result")
    seen, updates = set(), []
    for check in checks:
        full = check.get("cask", "")
        name = full.rsplit("/", 1)[-1]
        if (
            name not in names
            or name in seen
            or ("/" in full and full != f"{tap}/{name}")
        ):
            raise ValueError("Unexpected/duplicate livecheck result")
        seen.add(name)
        version = check.get("version")
        if check.get("status") or not isinstance(version, dict):
            raise ValueError(f"Livecheck did not verify {name}: {check}")
        if not isinstance(version.get("outdated"), bool) or not isinstance(
            version.get("current"), str
        ):
            raise TypeError(f"Unexpected livecheck version schema: {name}")
        latest = version.get("latest_throttled") or version.get("latest")
        if not isinstance(latest, str) or not VERSION.fullmatch(latest):
            raise ValueError(f"Unexpected upstream version format: {name}")
        path = root / "Casks" / name[0] / (name + ".rb")
        before = path.read_bytes().decode("utf-8")
        shape = recipe_shape(before)
        if shape is None or shape[1] != version["current"]:
            raise ValueError(f"Livecheck result does not match the checkout: {name}")
        if not version["outdated"] or version_key(latest) <= version_key(shape[1]):
            continue
        updates.append({"cask": name, "current": shape[1], "latest": latest})
    # Validate the entire livecheck batch before asking Homebrew to write any files.
    if bump and not dry_run:
        for row in updates:
            name, latest = row["cask"], row["latest"]
            path = root / "Casks" / name[0] / (name + ".rb")
            before = path.read_bytes().decode("utf-8")
            run(
                [
                    "brew",
                    "bump-cask-pr",
                    "--write-only",
                    f"--version={latest}",
                    f"{tap}/{name}",
                ]
            )
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Native bump did not leave a regular recipe: {name}")
            after = path.read_bytes().decode("utf-8")
            shape = recipe_shape(after)
            if shape is None or shape[1] != latest:
                raise ValueError(
                    f"Native bump did not produce the requested version: {name}"
                )
            routine, why = routine_recipe_change(before, after)
            if not routine:
                diff = "".join(
                    unified_diff(
                        before.splitlines(keepends=True),
                        after.splitlines(keepends=True),
                        fromfile="committed recipe",
                        tofile="native candidate",
                        n=2,
                    )
                )
                # Escape newlines/control characters in public workflow logs.
                excerpt = repr(diff[:6000])
                if len(diff) > 6000:
                    excerpt += " (truncated)"
                raise ValueError(
                    f"Native bump requires review: {name} {row['current']} -> {latest}: {why}. "
                    f"Rejected diff: {excerpt}"
                )
            validate_recipe(name, after)
            info = parse_json(
                run(["brew", "info", "--cask", "--json=v2", f"{tap}/{name}"]),
                f"brew info ({name})",
            )["casks"]
            if (
                len(info) != 1
                or info[0].get("token") != name
                or info[0].get("version") != latest
            ):
                raise ValueError(
                    f"Native bump metadata did not match the recipe: {name}"
                )
            if (
                info[0].get("disabled") is not False
                or info[0].get("sha256") not in shape[2]
            ):
                raise ValueError(
                    f"Bumped cask has invalid enabled/checksum state: {name}"
                )
            endpoint = urlparse(info[0]["url"])
            if (
                endpoint.scheme != "https"
                or endpoint.hostname != records[name]["artifact_host"]
                or endpoint.username
                or endpoint.password
            ):
                raise ValueError(f"Native bump changed the artifact source: {name}")
            records[name]["recipe_sha256"] = digest(after.encode())
    return updates


def sync(root, dry_run=False, tap=None, bump=False, download=fetch, run=command):
    root = Path(root).resolve()
    if bump and not tap:
        raise ValueError(
            "--bump requires --tap; it downloads official app archives to compute checksums"
        )
    # shortcut: 2 GiB download allowance covers these five casks; raise it before adding large SDKs.
    if (
        bump
        and not dry_run
        and shutil.disk_usage(root).free < 7 * 1024**3 + 64 * 1024**2
    ):
        raise ValueError(
            "Native bump needs 5 GiB reserve + 2 GiB download allowance + 64 MiB"
        )
    names = tokens(root)
    plan, records = prepare(root, names, download)
    lock_path = root / "source-lock.json"
    paths = [*plan, lock_path]
    snapshot = {p: p.read_bytes() if p.exists() else None for p in paths}
    if tap:
        for path, data in plan.items():
            before = snapshot[path]
            if (
                before is None
                or (dry_run and before != data)
                or not routine_recipe_change(
                    before.decode("utf-8"), data.decode("utf-8")
                )[0]
            ):
                raise ValueError(
                    f"Review and sync {path.name} before invoking Homebrew"
                )
    touched = set()
    try:
        if not dry_run:
            for path, data in plan.items():
                if snapshot[path] != data:
                    touched.add(path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                run(["ruby", "-c", str(path)])
        if bump and not dry_run:
            touched.update(plan)
        updates = (
            upstream_updates(root, names, records, tap, bump, dry_run, run)
            if tap
            else []
        )
        if not dry_run:
            bumped = {row["cask"] for row in updates} if bump else set()
            for path in plan:
                if path.is_symlink() or not path.is_file():
                    raise ValueError(f"Unexpected recipe file type: {path.name}")
                name = path.stem
                if digest(path.read_bytes()) != records[name]["recipe_sha256"]:
                    raise ValueError(f"Unrecorded recipe change: {name}")
                if name in bumped:
                    run(["ruby", "-c", str(path)])
            data = (json.dumps(records, indent=2, sort_keys=True) + "\n").encode()
            if snapshot[lock_path] != data:
                touched.add(lock_path)
                lock_path.write_bytes(data)
        return {"casks": names, "dry_run": dry_run, "upstream_updates": updates}
    except BaseException:
        for path in sorted(touched):
            data = snapshot[path]
            if path.is_symlink():
                path.unlink()
            if data is None:
                path.unlink(missing_ok=True)
            elif not path.is_file() or path.read_bytes() != data:
                path.write_bytes(data)
        raise


def update_policy(root, run=command):
    """Classify already verified sync output; this does not verify/download or publish it."""
    root = Path(root).resolve()
    names = tokens(root)
    expected = {f"Casks/{name[0]}/{name}.rb" for name in names} | {"source-lock.json"}
    changed = set(
        filter(
            None,
            run(["git", "-C", str(root), "diff", "--name-only", "-z", "HEAD"]).split(
                "\0"
            ),
        )
    )
    changed.update(
        filter(
            None,
            run(
                [
                    "git",
                    "-C",
                    str(root),
                    "ls-files",
                    "--others",
                    "--exclude-standard",
                    "-z",
                ]
            ).split("\0"),
        )
    )
    if changed - expected:
        return {
            "automatic": False,
            "publishable": False,
            "reasons": ["Changes outside generated allowlisted files"],
        }

    def old_file(path):
        try:
            return run(["git", "-C", str(root), "show", "HEAD:" + path])
        except RuntimeError:
            return None

    previous = old_file("source-lock.json")
    if previous is None:
        return {
            "automatic": False,
            "publishable": False,
            "reasons": ["Initial publication requires owner review"],
        }
    before = parse_json(previous, "HEAD:source-lock.json")
    after = parse_json((root / "source-lock.json").read_bytes(), "source-lock.json")
    existing = {p.relative_to(root).as_posix() for p in (root / "Casks").rglob("*.rb")}
    if existing != expected - {"source-lock.json"}:
        raise ValueError("Cask checkout does not match the allowlist")
    for relative in expected:
        path = root / relative
        if (
            path.is_symlink()
            or path.resolve() != path
            or not path.is_file()
            or path.stat().st_mode & 0o111
        ):
            raise ValueError(f"Not a regular non-executable generated file: {relative}")
    if set(before) != set(names) or set(after) != set(names):
        return {
            "automatic": False,
            "publishable": False,
            "reasons": ["Selection changes require owner review"],
        }
    fields = {
        "source_url",
        "source_sha256",
        "official_version",
        "official_recipe_sha256",
        "recipe_sha256",
        "artifact_host",
    }
    reasons = []
    for name in names:
        path = f"Casks/{name[0]}/{name}.rb"
        old_text = old_file(path)
        if old_text is None:
            reasons.append(f"{name}: new recipe requires review")
            continue
        new_text = (root / path).read_text()
        for record, text in [(before[name], old_text), (after[name], new_text)]:
            if set(record) != fields or record["recipe_sha256"] != digest(
                text.encode()
            ):
                raise ValueError(f"Unverified source-lock/recipe pair: {name}")
            if not all(
                HEX.fullmatch(record[key])
                for key in ["source_sha256", "official_recipe_sha256", "recipe_sha256"]
            ):
                raise ValueError(f"Invalid provenance digest: {name}")
            source = urlparse(record["source_url"])
            if (
                source.scheme != "https"
                or source.netloc != "raw.githubusercontent.com"
                or not re.fullmatch(
                    r"/Homebrew/homebrew-cask/[0-9a-f]{40}/Casks/[a-z0-9]/"
                    + re.escape(name)
                    + r"\.rb",
                    source.path,
                )
            ):
                raise ValueError(f"Nonofficial source provenance: {name}")
            validate_recipe(name, text)
        if before[name]["artifact_host"] != after[name]["artifact_host"]:
            reasons.append(f"{name}: publisher host changed")
            continue
        allowed, reason = routine_recipe_change(old_text, new_text)
        if not allowed:
            reasons.append(f"{name}: {reason}")
    return {"automatic": not reasons, "publishable": True, "reasons": reasons}
