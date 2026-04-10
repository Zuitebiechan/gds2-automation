# AWS Local Zone customer node

## Scope

This guide describes the simplest production-style deployment for one customer
per Windows cloud node in AWS Local Zone.

## Plain-language summary

Treat each customer node like one dedicated diagnostics workstation in the
cloud:

- the diagnostics GUI reaches the node over `443`
- the local reverse client reaches the node over `9000`
- Flask stays private on `127.0.0.1:8080`
- a reverse proxy such as Caddy exposes HTTPS and forwards to Flask

## Recommended shape

```text
Customer GUI -> https://customer-node.example.com:443 -> Caddy -> 127.0.0.1:8080
Local reverse client -> tls://customer-node.example.com:9000 -> reverse_server
```

## AWS checklist

1. Create one Windows EC2 instance in the target Local Zone.
2. Attach it to a public subnet with Internet Gateway access.
3. Assign a public IP or Elastic IP.
4. Point a customer-specific DNS name at that public IP.
5. Open only these inbound ports in the security group:
   - `443` for the diagnostics GUI
   - `9000` for the reverse tunnel
6. Do not expose `8080` publicly.

## Software on the node

Run these components on the Windows instance:

- Flask API on `127.0.0.1:8080`
- reverse tunnel server on `9000`
- GDS2 and Java agent
- Caddy or IIS as the HTTPS edge proxy

Helpful repository assets:

- `scripts/local_zone/Caddyfile.example`
- `scripts/local_zone/start_edge_proxy.ps1`
- `scripts/local_zone/test_edge_health.ps1`
- `scripts/local_zone/deploy_customer_node.ps1`
- `scripts/local_zone/register_edge_autostart.bat`

## Startup order

1. Start `reverse_server`
2. Start Flask with `python app.py --port 8080`
3. Start GDS2 with the Java agent
4. Start the edge proxy on `443`
5. Run the health check script

## Validation steps

From the node itself:

1. Verify `http://127.0.0.1:8080/api/session/start` returns JSON.
2. Verify `https://<customer-domain>/api/session/start` returns JSON.

From the customer side:

1. Set GUI `api_scheme=https`
2. Set GUI `host=<customer-domain>`
3. Set GUI `api_port=443`
4. Set GUI `api_token` when `DIAGNOSTIC_API_TOKEN` is enabled

## Temporary no-domain debug mode

If you are still proving business behavior and do not yet have a working
customer DNS name plus trusted `443` edge, use a temporary direct-connect mode
first:

1. Start Flask with `python app.py --port 8080 --public`
2. Keep the reverse tunnel on `9000`
3. Set GUI `api_scheme=http`
4. Set GUI `host=<public-ip>`
5. Set GUI `api_port=8080`

This is a debugging shortcut, not the final production shape. It is useful for:

- confirming that session start works end-to-end
- separating API-entrypoint problems from backend or tunnel problems
- validating customer workflows before HTTPS and DNS are finished

After that succeeds, move back to the production model:

- Flask on `127.0.0.1:8080`
- reverse proxy on `443`
- GUI on `https://<customer-domain>:443`

## 443 prerequisites

Testing `443` is intentionally more involved than testing `8080`.

Before the GUI can use `https://<customer-domain-or-ip>:443`, you need:

1. a reverse proxy such as Caddy or IIS actually listening on `443`
2. the GUI configured to match that public entrypoint
3. a certificate chain the client trusts

Practical note:

- `scripts/local_zone/start_edge_proxy.ps1` expects a real `caddy.exe`
  location or a working `caddy` command on `PATH`
- the example value `C:\path\to\caddy.exe` is only a placeholder
- without a domain-backed public certificate, `443` testing may require
  additional certificate-trust setup on the client

## Security notes

- Prefer HTTPS for the public diagnostics API.
- Prefer `DIAGNOSTIC_API_TOKEN` for per-node API access control.
- Prefer TLS on the reverse tunnel.
- Keep Flask loopback-only unless a temporary public bind is required for debugging.
