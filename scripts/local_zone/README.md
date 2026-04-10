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
