# Smart Enclosure — Raspberry Pi Backend

## Purpose

This repository contains the enclosure-side backend for the Smart Enclosure system.

The source code is edited locally on the Windows development machine.

The actual application runs on the Raspberry Pi attached to the enclosure.

The local Windows machine is NOT a runtime environment for this application.

Do not attempt to validate Raspberry Pi functionality using local Windows services, localhost, local Tailscale, GPIO, I2C, systemd, or other local runtime resources.

## Source Code

All source-code changes should be made in this local Git repository.

Do not edit the production source files on the Raspberry Pi over SSH unless I explicitly request it.

Git changes should be reviewed locally before deployment.

Do not assume that changing local files changes the deployed application.

## Raspberry Pi Runtime

The Raspberry Pi is the real runtime environment for this repository.

It handles enclosure hardware and sensors and exposes the enclosure API.

SSH host:

```
beardapi
```

When runtime information is required, inspect the actual Raspberry Pi using SSH.

Examples:

```
ssh beardapi "tailscale status"
ssh beardapi "systemctl --type=service --state=running"
ssh beardapi "ps aux"
ssh beardapi "journalctl -u <service> -n 100 --no-pager"
```

Do not interpret the local Windows machine's:

* Tailscale status
* localhost
* network interfaces
* system services
* GPIO state
* I2C devices
* USB devices
* sensor state

as representing the Raspberry Pi.

## Hardware

This application interacts with physical enclosure hardware.

Hardware-dependent functionality may not be reproducible on the local Windows development machine.

When diagnosing hardware behavior, use the Raspberry Pi runtime environment over SSH.

Do not make potentially disruptive hardware-control changes without explicit approval.

## Related AWS Application

The corresponding hosted application exists in the other Git repository attached to this Codex project.

The AWS runtime can be reached using:

```
ssh aws-ec2
```

Changes to API contracts may require corresponding changes in the AWS repository.

When modifying:

* API routes
* request formats
* response formats
* authentication behavior
* sensor data structures
* device identifiers

inspect the AWS repository for consumers of that functionality before considering the change complete.

## Remote Access Policy

By default, SSH access should be treated as read-only investigation.

It is acceptable to inspect:

* application logs
* running processes
* service status
* network status
* Tailscale status
* application configuration
* deployed source versions
* Git status
* filesystem layout
* connected hardware/device state

Do NOT without explicit approval:

* modify production files
* deploy code
* restart or stop services
* install or remove packages
* change systemd configuration
* change network configuration
* reboot or shut down the Raspberry Pi
* delete files
* modify hardware configuration
* perform database migrations

## Testing

There is currently no supported local environment that fully reproduces the Raspberry Pi runtime.

Do not attempt to run the complete application locally unless a local development environment is explicitly created in the future.

Local static analysis, unit tests, syntax checks, and tests that do not require Raspberry Pi hardware may still be run when supported by the repository.

For runtime verification requiring Raspberry Pi services or hardware, use the `beardapi` SSH host.

Unless explicitly authorized to deploy, runtime inspection must not modify the deployed application.

## Development Workflow

The expected workflow is:

1. Inspect the local repository.
2. Use SSH to inspect the Raspberry Pi when runtime information is required.
3. Make source changes locally.
4. Review and test local changes where possible.
5. Do not deploy until explicitly instructed.
6. After deployment, verify behavior against the actual Raspberry Pi runtime.

Never assume that a successful local edit means the Raspberry Pi application has changed.

## Git and release consistency
- Commit and push reviewed local changes before deploying an exact commit.
- Server master must fast-forward to that release; never force-push or hard-reset.
- Use tools/update_checkout.py first in dry-run mode, then --apply only for an
  authorized deployment/reconciliation. Follow DEPLOYMENT_WORKFLOW.md.
- Keep the Pi database and distro-owned nginx parameter files outside Git.
- Preserve unexpected server edits; bring intentional source changes back locally.
- A source update does not restart a service. Follow the release-specific activation
  plan, verify runtime behavior, and record successful verification separately.
