# Local Zone edge assets

This folder contains the minimum deployment assets for a production-style
customer node where:

- Flask listens only on `127.0.0.1:8080`
- a reverse proxy exposes `443`
- the reverse tunnel still listens on `9000`

Files:

- `Caddyfile.example`: sample Caddy reverse-proxy config for `EDGE_DOMAIN`
- `start_edge_proxy.ps1`: starts Caddy with the sample config
- `test_edge_health.ps1`: checks both loopback Flask and the public HTTPS edge
- `deploy_customer_node.ps1`: starts cloud services, starts the edge proxy, and runs health checks
- `register_edge_autostart.bat`: registers the edge proxy as an on-boot task
- `unregister_edge_autostart.bat`: removes the edge proxy startup task
- `customer_node.env.example`: sample environment values for one customer node

Typical flow:

1. Start Flask locally with `python app.py --port 8080`
2. Set `EDGE_DOMAIN` to the customer-facing DNS name
3. Start Caddy through `start_edge_proxy.ps1`
4. Verify `https://<edge-domain>/api/session/start` through `test_edge_health.ps1`

Prerequisites:

- `start_edge_proxy.ps1` expects a working `caddy` command on `PATH`, or a real
  `caddy.exe` path passed through `-CaddyExe` or `CADDY_EXE`
- the sample string `C:\path\to\caddy.exe` is only a placeholder
- the GUI must be configured to match the real public API entrypoint
  (`https` + `443` for the edge proxy, or `http` + `8080` for temporary
  direct-connect debugging)

Practical testing advice:

- if DNS, certificates, or `443` are not ready yet, first validate the
  business flow with `python app.py --port 8080 --public`
- in that temporary mode, set the GUI to `api_scheme=http` and `api_port=8080`
- once direct-connect testing succeeds, switch back to the intended production
  shape with Flask on loopback and the reverse proxy on `443`

Or use the combined deploy helper:

```powershell
$env:EDGE_DOMAIN = "cust001.diag.example.com"
.\scripts\local_zone\deploy_customer_node.ps1 -NoGds2
```

Example:

```powershell
$env:EDGE_DOMAIN = "cust001.diag.example.com"
.\scripts\local_zone\start_edge_proxy.ps1 -Background
.\scripts\local_zone\test_edge_health.ps1
```
