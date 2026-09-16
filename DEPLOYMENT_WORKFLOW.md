# Git and deployment workflow

## Source of truth
Windows repositories are source development; GitHub stores committed history.
The Pi and AWS checkouts must point at the exact deployed source commit.
Do not edit production source as the routine workflow.

## Before every deployment
1. Read AGENTS.md. Runtime changes need the user's authorization.
2. Review local changes, run appropriate tests, commit and push. Never force-push.
3. Record the full commit ID. Fetch origin on the destination server.
4. Check services, disk space and Git status. Preserve unexpected server edits.
5. Stage the reviewed tools/update_checkout.py outside the checkout, then run its
   dry run against the exact target commit. Examples below use placeholders.
6. Apply only after the dry run is understood. The tool backs up changed files,
   retains retired runtime files, writes source atomically and fast-forwards Git
   metadata. It does not reset working files wholesale, delete files, install
   packages, change the live database or restart services.

Pi:
```
python3 /home/pi/enclosure-deployments/update_checkout.py --repo /home/pi/enclosure --profile pi --target FULL_COMMIT --backup-root /home/pi/enclosure-deployments/reconciliations
```

AWS:
```
python3 /root/enclosure-deployments/update_checkout.py --repo / --profile aws --target FULL_COMMIT --backup-root /root/enclosure-deployments/reconciliations
```

Append --apply only for an authorized update. Do not use the tool concurrently
with another deployment or with production source edits. It refuses unrelated
changes, staged changes, non-fast-forward history, unsupported paths and source
deletions. Handle those cases explicitly; never bypass them with a hard reset.
An interrupted update has backups but is not automatically rolled back; inspect
before retrying. Git metadata moves only after source files verify.

## Activation and verification
The tool aligns SOURCE files, not necessarily the currently running process.
For actual code/dependency changes, follow the release-specific service/dependency
plan and validate/reload nginx if needed. Source updates alone do not activate
already-imported Python code. No restart is necessary for documentation, tracking,
or line-ending-only reconciliation.

Verify Git HEAD equals the target and git status is clean. Verify remote services,
API health, reading freshness and the dashboard. Record the outcome in a
runtime-verification.json next to the generated release.json, including source
commit, timestamp, service PIDs/restart state, checks and any remaining limitations.
Do not label a release verified merely because Git is clean.

## Runtime state and operating-system files
Pi enclosure.db and its journal/WAL files are ignored. Never deploy a database
from Git. Back it up with SQLite's backup API; do not overwrite a live database
during rollback. The older committed database remains in Git history.

AWS fastcgi_params, proxy_params, scgi_params and uwsgi_params are nginx-common
package files. Their installed content is retained outside application Git.
Application nginx.conf and the bruck.gg site remain tracked. Never replace the
sites-enabled symlink with a Windows text placeholder.

Python environments and .env files stay outside Git. Do not add runtime secrets.
Existing legacy application files remain historical reference; this workflow
does not remove them.

## Line endings and rollback
.gitattributes keeps deployed text LF on Windows and Linux. It avoids apparent
whole-file differences after copying Windows files.
Rollback requires reviewing backups and later changes, restoring selected code/
configuration, and activating services as appropriate. Preserve database events.
Prefer a new revert commit in Git for source rollback; do not rewrite shared history.
Never pull blindly over a dirty production checkout, discard all changes, run
git clean, force-push or hard-reset a server.
