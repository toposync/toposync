# Toposync Home Assistant Add-on

This add-on runs the published `toposync` application behind Home Assistant ingress.

- UI and API are served by the same `toposync serve` process on port `8000`
- Home Assistant access is managed through Supervisor ingress headers
- The Home Assistant extension uses the internal Supervisor Core API automatically
- Persistent data lives in `/data`

The image installs the published Python package instead of rebuilding the application inside the add-on. That keeps the runtime DRY with the normal `pip install toposync` distribution.
