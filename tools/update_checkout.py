#!/usr/bin/env python3
"""Guarded source update. No services, package installs, SQL migrations or file deletions."""
from contextlib import closing
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import tarfile
import tempfile

RETIRED = {
    "pi": {"enclosure.db"},
    "aws": {"etc/nginx/fastcgi_params", "etc/nginx/proxy_params",
            "etc/nginx/scgi_params", "etc/nginx/uwsgi_params"},
}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


def paths(data):
    return [x.decode() for x in data.split(b"\0") if x]


def same(a, b):
    return a == b or (b"\0" not in a and b"\0" not in b and a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n"))


def allowed(name, profile):
    common = {".gitignore", ".gitattributes", "AGENTS.md", "DEPLOYMENT_WORKFLOW.md",
              "SENSOR_UPDATES.md", "requirements.txt"}
    if name in common or name.startswith(("tools/", "tests/", "docs/")):
        return True
    if profile == "pi":
        return name in {"main.py", "relay_settings.py", "timing_config.py", "sensor_collector.py", "sensor_worker.py",
                        "sensor_contract.py", "sensor_simulator.py", "beardapi.service",
                        "docker-compose.yml", "mediamtx.yml"}
    return (name.startswith("home/admin/smart-enclosure-api/") and
            "/venv/" not in name and "/.env" not in name) or name in {
                "etc/nginx/sites-available/bruck.gg",
                "etc/systemd/system/smart-enclosure-api.service",
                "var/www/bruck.gg/debug.html", "var/www/bruck.gg/js/enclosure.js"}


def blob(root, commit, name):
    try:
        return git(root, "show", f"{commit}:{name}")
    except subprocess.CalledProcessError:
        return None


def safe_path(root, name):
    if Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError(f"Unsafe repository path: {name}")
    p = root / name
    current = p
    while current != root:
        if current.is_symlink():
            raise ValueError(f"Refusing to replace/follow symlink: {name}")
        current = current.parent
    return p


def plan(root, target, profile):
    if not re.fullmatch(r"[0-9a-f]{40}", target):
        raise ValueError("Use the full, reviewed 40-character commit ID")
    if git(root, "symbolic-ref", "--short", "HEAD").strip() != b"master":
        raise ValueError("Expected checked-out master")
    head = git(root, "rev-parse", "HEAD").decode().strip()
    git(root, "merge-base", "--is-ancestor", head, target)
    git(root, "merge-base", "--is-ancestor", target, "origin/master")
    if git(root, "diff", "--cached", "--name-only").strip():
        raise ValueError("Staged server changes must be reviewed separately")
    changed = set(paths(git(root, "diff", "--name-only", "-z", head, target)))
    dirty = set(paths(git(root, "diff", "--name-only", "-z", "HEAD")))
    unknown = set(paths(git(root, "ls-files", "--others", "--exclude-standard", "-z")))
    extra = (dirty | unknown) - changed - RETIRED[profile]
    if extra:
        raise ValueError("Unrelated server changes: " + ", ".join(sorted(extra)))
    operations = []
    for name in sorted(changed):
        p = safe_path(root, name)
        desired = blob(root, target, name)
        if desired is None:
            if name not in RETIRED[profile]:
                raise ValueError(f"Deletion requires a separate reviewed procedure: {name}")
            operations.append((name, None, "preserve-untracked"))
            continue
        if not allowed(name, profile):
            raise ValueError(f"Path outside deployment allowlist: {name}")
        mode = git(root, "ls-tree", target, "--", name).split()[0]
        if mode not in (b"100644", b"100755"):
            raise ValueError(f"Unsupported target mode: {name}")
        if p.exists():
            current = p.read_bytes()
            previous = blob(root, head, name)
            if not same(current, desired) and (previous is None or not same(current, previous)):
                raise ValueError(f"Unexpected server edit; preserve and reconcile locally: {name}")
            action = "unchanged" if current == desired else "normalize" if same(current, desired) else "update"
        else:
            if blob(root, head, name) is not None:
                raise ValueError(f"Unexpected server deletion: {name}")
            action = "create"
        operations.append((name, desired, action))
    return head, operations


def apply(root, target, profile, backup_root):
    head, operations = plan(root, target, profile)
    backup_root = backup_root.resolve()
    if backup_root == root or (root != Path("/") and root in backup_root.parents):
        raise ValueError("Backups must be outside the checkout (AWS / uses an explicit /root backup directory)")
    release = backup_root / (datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + target[:12])
    release.mkdir(parents=True, mode=0o700)
    (release / "before.patch").write_bytes(git(root, "diff", "--binary", "HEAD"))
    (release / "status-before.txt").write_bytes(git(root, "status", "--short", "--untracked-files=all"))
    if profile == "pi" and (root / "enclosure.db").exists():
        with closing(sqlite3.connect((root / "enclosure.db").as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(release / "enclosure-backup.db")) as destination:
                source.backup(destination)
    metadata = {}
    with tarfile.open(release / "before.tar.gz", "w:gz") as archive:
        for name, _, _ in operations:
            p = root / name
            metadata[name] = p.exists()
            if p.exists():
                # The database has a consistent SQLite backup above.
                if not (profile == "pi" and name == "enclosure.db"):
                    archive.add(p, arcname=name, recursive=False)
    (release / "before.json").write_text(json.dumps({"head": head, "target": target,
        "paths_existed": metadata}, indent=2))
    for name, content, action in operations:
        if action in ("preserve-untracked", "unchanged"):
            continue
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        old = p.stat() if p.exists() else None
        fd, temp = tempfile.mkstemp(prefix=".release-", dir=p.parent)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temp, stat.S_IMODE(old.st_mode) if old else 0o644)
            if old and hasattr(os, "chown"):
                os.chown(temp, old.st_uid, old.st_gid)
            os.replace(temp, p)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    # All content is verified before moving metadata. No working file is removed.
    for name, content, _ in operations:
        if content is not None and (root / name).read_bytes() != content:
            raise ValueError(f"Post-write verification failed: {name}")
    git(root, "reset", "--mixed", target)
    status = git(root, "status", "--porcelain", "--untracked-files=all").decode()
    record = {"previous_commit": head, "source_commit": target,
              "runtime_verification": "pending", "git_status": status,
              "sha256": {name: hashlib.sha256(content).hexdigest()
                         for name, content, _ in operations if content is not None}}
    (release / "release.json").write_text(json.dumps(record, indent=2))
    print("Source commit aligned; services were not restarted. Record:", release / "release.json")
    if status.strip():
        raise ValueError("Source update completed, but checkout is not clean; inspect " + str(release))
    return release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--profile", choices=("pi", "aws"), required=True)
    parser.add_argument("--backup-root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.repo.resolve()
    head, operations = plan(root, args.target, args.profile)
    print(head, "->", args.target)
    for name, _, action in operations:
        print(action, name)
    if args.apply:
        apply(root, args.target, args.profile, args.backup_root)
    else:
        print("Dry run only. Review before --apply.")


if __name__ == "__main__":
    main()
