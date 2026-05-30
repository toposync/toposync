# Security Policy

Toposync is a local-first platform that can handle authentication, user-managed files, cameras, Home Assistant access, streaming transports, processing servers, and extension packages. Security reports must be handled privately so users have time to update before details are public.

Toposync is currently an early access alpha project. It is experimental software and should be tested in contained environments, contained networks, and non-critical Home Assistant setups. Do not rely on it yet for daily household operation, safety-critical automation, unattended security monitoring, life-safety workflows, emergency response, access control, or any automation where a failure could cause harm, property damage, privacy exposure, or loss of essential service.

If you test Toposync with real cameras, Home Assistant entities, notifications, streaming, or processing servers, assume the system can fail open, fail closed, lose state, expose incorrect diagnostics, miss events, duplicate events, or require manual recovery. Keep rollback paths available and avoid exposing experimental instances to the public internet.

Do not report vulnerabilities in public issues, discussions, pull requests, chat logs, or social channels.

## Supported versions

Toposync is currently pre-1.0 alpha. Security fixes are provided for the latest published version only.

| Version | Security support |
| --- | --- |
| Latest published version | Supported |
| Older versions | Not supported |
| Unreleased local branches | Not supported |

If you are not on the latest published version, upgrade before requesting a security backport. Backports may be considered only in exceptional cases and are not guaranteed during the alpha phase.

## Experimental use guidance

Use early access builds with conservative boundaries:

- run Toposync on a private LAN or isolated test network;
- avoid public internet exposure unless you fully understand the reverse proxy, TLS, authentication, and firewall configuration;
- do not connect safety-critical automations, locks, alarms, medical devices, or emergency workflows;
- do not use it as the only monitoring path for cameras or sensors;
- avoid granting broader Home Assistant access than needed for testing;
- use test cameras, test areas, or non-critical entities when possible;
- keep Home Assistant backups and Toposync data backups before upgrades;
- monitor logs after installing, updating, enabling streaming, or adding processing servers.

Treat alpha builds as evaluation software. Validate behavior in your own environment before trusting any automation, camera, notification, or processing result.

## Reporting a vulnerability

Use GitHub private vulnerability reporting or a private GitHub Security Advisory for this repository.

Do not open a public issue for suspected vulnerabilities. If private vulnerability reporting is unavailable, contact the maintainer privately without including exploit details in a public channel.

## What to include

Include enough information to reproduce and triage the issue without exposing secrets:

- affected Toposync version;
- installation shape: Python, Docker, Home Assistant add-on, Windows service, processing server, or development checkout;
- operating system and architecture;
- whether the issue involves authentication, files, cameras, Home Assistant, streaming, extensions, plugin API, processing servers, packaging, or update flow;
- expected impact;
- minimal reproduction steps;
- sanitized logs, tracebacks, request paths, or screenshots;
- whether the issue affects a default install or requires non-default configuration.

## What not to include publicly

Do not publish or attach:

- passwords, tokens, cookies, API keys, or Home Assistant long-lived access tokens;
- private URLs, camera URLs, RTSP credentials, or full internal network maps;
- personal data, images, video frames, or Home Assistant entity data from real users;
- exploit scripts, weaponized payloads, or step-by-step public exploitation instructions;
- private add-on data, `config.json`, database files, or environment files unless they have been sanitized.

If a file is useful for triage, remove secrets and private details before sharing it privately.

## Response expectations

The maintainer will try to acknowledge private reports in a reasonable timeframe, triage the issue, and coordinate a fix when the report is valid.

Depending on severity and scope, the resolution may include:

- a patch release;
- package or dependency guidance;
- Home Assistant add-on update guidance;
- documentation updates;
- coordinated disclosure after users have had time to update.

The project is maintained as an early-stage open source project, so response times can vary. Please avoid public disclosure until a fix or mitigation is available.

## Scope

This policy covers:

- Toposync core backend and frontend host;
- first-party extensions in this repository;
- `@toposync/plugin-api`;
- Python packaging, bundle packages, and distribution metadata;
- Docker runtime files in this repository;
- Home Assistant add-on documentation and runtime contract maintained by the Toposync project;
- processing server runtime and registration behavior.

Third-party extensions are primarily the responsibility of their maintainers. Report third-party issues to the relevant extension maintainer unless the issue exposes a vulnerability in Toposync core, the plugin API, or the extension loading model.

## Safe harbor

Good-faith security research is welcome when it stays within reasonable boundaries:

- use only systems and data you own or are authorized to test;
- avoid denial of service, persistence, lateral movement, or access to third-party data;
- do not exfiltrate secrets, camera media, Home Assistant data, or user files;
- do not publicly disclose details before a fix or mitigation is available;
- stop testing and report privately if you find a real vulnerability.

Reports that follow these guidelines will be treated as good-faith research.
