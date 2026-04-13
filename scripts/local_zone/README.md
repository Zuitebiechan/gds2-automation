# Local Zone edge assets

This folder contains the minimum deployment assets for a production-style
customer node where:

- Flask listens only on `127.0.0.1:8080`
- a reverse proxy exposes `443`
- the reverse tunnel still listens on `9000`

Files:

- `Caddyfile.example`: sample Caddy reverse-proxy config for `EDGE_DOMAIN`
- `aws_launch_smoke_test.py`: validates AWS identity and tests one Local Zone EC2 launch request
- `zone_catalog.example.json`: example multi-zone routing catalog for Dallas, Atlanta, and Los Angeles
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

AWS launch smoke test:

```powershell
pip install -r requirements-cloud.txt
$env:DIAGNOSTIC_AWS_REGION = "us-west-2"
$env:DIAGNOSTIC_NODE_LAUNCH_TEMPLATE = "diag-local-zone-worker"
$env:DIAGNOSTIC_NODE_INSTANCE_TYPE = "m6i.xlarge"
$env:DIAGNOSTIC_NODE_SUBNET_ID = "subnet-12345"
$env:DIAGNOSTIC_NODE_DEFAULT_ZONE = "us-west-2-lax-1a"
$env:DIAGNOSTIC_NODE_DEFAULT_METRO = "los-angeles"
$env:DIAGNOSTIC_NODE_DEFAULT_API_BASE = "https://lax-1.diag.example.com"
$env:DIAGNOSTIC_NODE_DEFAULT_TUNNEL_HOST = "lax-1.diag.example.com"
python .\scripts\local_zone\aws_launch_smoke_test.py
```

Default behavior is EC2 `DryRun`.

For multi-zone allocation, prefer a catalog file instead of one global subnet:

```powershell
$env:DIAGNOSTIC_NODE_ZONE_CATALOG_FILE = "$PWD\\scripts\\local_zone\\zone_catalog.example.json"
```

Each zone entry can point at a different:

- `subnet_id`
- `api_base_url`
- `tunnel_host`
- optional `instance_type`
- optional `security_group_ids`
- optional routing hints such as `time_zones` and `cities`

You can now validate route selection before a real launch by passing user hints:

```powershell
$env:DIAGNOSTIC_NODE_ZONE_CATALOG_FILE = "$PWD\\scripts\\local_zone\\zone_catalog.example.json"
python .\scripts\local_zone\aws_launch_smoke_test.py --client-time-zone "America/Chicago"
```

That command will print the resolved metro and zone before the EC2 request.

To do a real launch and automatically terminate the instance right after the launch call succeeds:

```powershell
python .\scripts\local_zone\aws_launch_smoke_test.py --live
```

To keep the launched instance for inspection:

```powershell
python .\scripts\local_zone\aws_launch_smoke_test.py --live --keep-instance
```

You can also combine both modes:

```powershell
python .\scripts\local_zone\aws_launch_smoke_test.py `
  --zone-catalog-file .\scripts\local_zone\zone_catalog.example.json `
  --client-time-zone "America/Chicago" `
  --live
```
