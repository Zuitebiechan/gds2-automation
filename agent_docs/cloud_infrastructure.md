# Cloud Infrastructure — AWS Dynamic VM Provisioning

## Current State (2026-03-18)

Manual operation: RDP into cloud VM → open two terminals → start services manually.

Solved with `scripts/cloud_start_services.bat` + `scripts/cloud_register_autostart.bat` for auto-start on boot.

## Future Architecture: Per-User VM Sessions

Each mechanic session gets a dedicated Windows VM, created on demand, destroyed after use.

### Flow

```
Client App → Backend API → AWS EC2 RunInstances → VM boots
                                                    ↓
                                              EC2 User Data runs
                                              (install deps, start services)
                                                    ↓
                                              SSM confirms ready
                                                    ↓
                          Client connects ← Backend returns connection info
                                                    ↓
                                              Session ends → TerminateInstances
```

### AWS Services

| Need | Service | API / Feature |
|---|---|---|
| Create/start VM | EC2 | `RunInstances`, `StartInstances`, `TerminateInstances` |
| Boot-time setup | EC2 User Data + EC2Launch v2 | `executeScript` task in User Data |
| Post-launch commands | Systems Manager (SSM) | `SendCommand` (Run Command) |
| Health check | SSM | `DescribeInstanceInformation` |
| Remote desktop | NICE DCV | Browser-based, free on EC2 |
| Virtual display | NICE DCV | Provides display context for JavaFX |

### EC2 User Data Example

```powershell
<powershell>
# Runs automatically on first boot via EC2Launch v2

# Clone/pull project
cd C:\
git clone https://github.com/Zuitebiechan/gds2-automation.git
cd gds2-automation

# Install Python dependencies
python -m venv venv32
venv32\Scripts\pip install -r requirements-cloud.txt

# Register auto-start
scripts\cloud_register_autostart.bat

# Start services immediately
scripts\cloud_start_services.bat
</powershell>
```

### SSM Run Command (post-launch health check)

```python
import boto3

ssm = boto3.client('ssm')

# Verify services are running
response = ssm.send_command(
    InstanceIds=['i-0abc123def456'],
    DocumentName='AWS-RunPowerShellScript',
    Parameters={
        'commands': [
            'Test-NetConnection -ComputerName localhost -Port 8080',
            'Test-NetConnection -ComputerName localhost -Port 9000',
        ]
    }
)
```

### Instance Types

| Type | Specs | Price (us-east-1) | Use Case |
|---|---|---|---|
| `t3.medium` | 2 vCPU, 4 GB | ~$0.07/hr | Dev/test, software rendering |
| `t3.large` | 2 vCPU, 8 GB | ~$0.13/hr | Production, single user |
| `g4dn.xlarge` | 4 vCPU, 16 GB, T4 GPU | ~$0.71/hr | GPU rendering, NICE DCV |

### Key Constraints

- **JavaFX needs a display context**: NICE DCV provides a virtual display even without physical monitor. Without DCV or GPU, JavaFX falls back to software rendering (slower but functional).
- **Windows Server with Desktop Experience**: Required for GDS2 GUI. Use AMI `Windows_Server-2022-English-Full-Base`.
- **One GDS2 per VM**: GDS2 is single-instance, so each user needs their own VM.
- **Security groups**: Open ports 8080 (Flask API), 9000-9001 (VCI Proxy) to the client's IP only.
- **Cost optimization**: Use Spot Instances for non-critical sessions (~60-70% cheaper). Use `StopInstances` instead of `TerminateInstances` if VM image is pre-configured (faster restart).

### Pre-baked AMI Strategy

Instead of running User Data on every boot:

1. Set up one VM manually (install GDS2, Python, dependencies, agent)
2. Create an AMI snapshot (`CreateImage`)
3. Launch new instances from this AMI — boots in ~2 min with everything pre-installed
4. User Data only needs to start services, not install anything

This reduces cold start from ~10 min to ~2 min.

### Implementation Priority

1. ~~Auto-start on boot~~ — Done (`cloud_start_services.bat`)
2. Pre-baked AMI creation — Next step when scaling
3. Backend API for VM lifecycle (`boto3` + EC2) — When multi-user is needed
4. NICE DCV integration — When headless operation is required
