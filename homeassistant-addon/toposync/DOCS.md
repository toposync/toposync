# Toposync

This add-on runs Toposync inside Home Assistant with:

- sidebar entry through ingress
- supervised execution
- internal access to the Home Assistant Core API
- frontend and API served on the same internal port

## Notes

- Access is restricted to Home Assistant ingress requests.
- The add-on uses the Supervisor token automatically; no manual Home Assistant host or API key is required inside Toposync.
- Persistent application data is stored in `/data`.
