# Support

Toposync is currently an early access alpha project. It is experimental software for testing in contained environments, contained networks, and non-critical Home Assistant setups.

Do not rely on Toposync yet for daily household operation, safety-critical automation, unattended security monitoring, emergency workflows, access control, or any automation where failure could cause harm, property damage, privacy exposure, or loss of essential service.

## Where to get help

Use GitHub issues for public support requests, bug reports, documentation problems, and feature discussions.

Before opening an issue:

- search existing issues;
- check the documentation site;
- verify that you are using the latest published version;
- collect the minimum logs and environment details needed to reproduce the problem;
- remove secrets, tokens, credentials, private URLs, camera URLs, and personal data.

## What to include

For installation and runtime issues, include:

- Toposync version;
- installation path: Python, Docker, Home Assistant add-on, Windows service, processing server, or development checkout;
- operating system and architecture;
- whether the issue involves cameras, Home Assistant, streaming, vision, authentication, files, extensions, or processing servers;
- expected behavior and actual behavior;
- reproduction steps;
- sanitized logs or screenshots.

For Home Assistant add-on issues, include:

- Home Assistant installation type;
- add-on version;
- whether you are using sidebar ingress, direct port `18756`, RTSP, WebRTC, or a remote processing server;
- relevant add-on logs and Supervisor errors with secrets removed.

## Security issues

Do not report vulnerabilities in public issues.

Follow the [Security Policy](SECURITY.md) and use private GitHub vulnerability reporting or a private GitHub security advisory when available.

## Scope of support

Community support can help with:

- installation and upgrade issues;
- documentation problems;
- reproducible bugs;
- local development setup;
- Home Assistant add-on behavior;
- Docker and Python package installation;
- processing server setup;
- first-party extensions.

Community support is not a guarantee of:

- production readiness;
- compatibility with every camera, network, GPU, or Home Assistant installation;
- emergency response;
- custom deployment design;
- private one-on-one troubleshooting;
- support for modified forks or unpublished third-party extensions.

## Experimental usage expectations

If you are testing Toposync with real devices:

- use a private LAN or isolated test network;
- keep Home Assistant and Toposync backups;
- avoid exposing experimental instances to the public internet;
- start with non-critical entities, test cameras, and limited automation scope;
- keep a manual fallback for cameras, alarms, notifications, and automations;
- review logs after updates and configuration changes.

## Feature requests

Feature requests are welcome when they explain:

- the problem being solved;
- the current workaround;
- the desired behavior;
- why it belongs in core, a first-party extension, documentation, or a third-party extension.

Toposync keeps the core generic. Domain-specific behavior usually belongs in extensions.

## Contributions

If you want to contribute a fix or documentation improvement, see [Contributing to Toposync](CONTRIBUTING.md).
