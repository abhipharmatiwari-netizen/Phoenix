# Vultr Reverse Proxy For Local Phoenix

This runbook exposes the local Windows Phoenix LIVE stack through a small Vultr
Ubuntu proxy. Phoenix and Postgres remain on the Windows machine; Vultr only
terminates HTTPS and proxies traffic through a reverse SSH tunnel.

Use this for the current 1 vCPU / 1 GB Vultr instance. Do not expose Postgres
to the internet.

## Topology

```text
app.phoenixtechnosolutions.in
  -> Vultr 65.20.69.50 nginx :443
  -> 127.0.0.1:18080 on Vultr
  -> reverse SSH tunnel from Docker sidecar phoenix-v9-vultr-tunnel
  -> Docker service nginx:80
  -> local Phoenix backend as needed
```

Validated state on 2026-06-29 IST:

- GoDaddy DNS resolves `app.phoenixtechnosolutions.in` to `65.20.69.50`;
- nginx HTTPS is active for `app.phoenixtechnosolutions.in`;
- Let's Encrypt certificate expires on 2026-09-26 18:16:46 UTC;
- `https://app.phoenixtechnosolutions.in/readyz` returns HTTP 200 with
  `ready=true`;
- `https://app.phoenixtechnosolutions.in/login` returns HTTP 200;
- `/auth/login` reaches backend authentication and does not return
  `Invalid Host header`;
- plain HTTP redirects to HTTPS;
- the active reverse tunnel is owned by Docker container
  `phoenix-v9-vultr-tunnel`; the Windows Scheduled Task is installed but
  disabled as fallback.

## DNS

Create or verify this GoDaddy DNS record:

```text
Type: A
Name: app
Value: 65.20.69.50
TTL: 600 seconds
```

## Vultr Network Requirements

The Vultr instance must allow inbound TCP:

- `22` for SSH
- `80` for Let's Encrypt HTTP validation and redirect
- `443` for HTTPS

If `ssh phoenixproxy@65.20.69.50` times out, use the Vultr web console or Vultr
firewall page to restore access. Do not paste the root password or Phoenix
secrets into chat, docs, screenshots, or tickets.

## Add SSH Key

Use a non-root tunnel user. The active proxy uses `phoenixproxy` with
passwordless sudo for proxy maintenance only. Add the Windows public key to
`/home/phoenixproxy/.ssh/authorized_keys` on Vultr:

```text
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOS+zcgUMS9Nxz9YQTbjCFCRKviLjGLjeNcAaHZ43APG phoenix-vultr-workspace-20260629
```

Keep the private key on Windows at
`C:\Users\abhis\.ssh\phoenix_vultr_proxy_workspace_ed25519` with ACL access
restricted to `FREEDOM\abhis` and `SYSTEM`.

## Bootstrap Vultr

After SSH works, copy and run:

```bash
scp -i C:\Users\abhis\.ssh\phoenix_vultr_proxy_workspace_ed25519 scripts/ops/vultr_proxy_bootstrap.sh phoenixproxy@65.20.69.50:/tmp/
ssh -i C:\Users\abhis\.ssh\phoenix_vultr_proxy_workspace_ed25519 phoenixproxy@65.20.69.50 'sudo bash /tmp/vultr_proxy_bootstrap.sh app.phoenixtechnosolutions.in'
```

The bootstrap script installs nginx/certbot/OpenSSH, enables reverse SSH
forwarding bound to Vultr localhost only, configures nginx to proxy
`127.0.0.1:18080`, opens `22/80/443` with UFW, and requests a Let's Encrypt
certificate once DNS resolves. Before TLS is issued, public HTTP is for
`/readyz` validation only; do not use the login or admin UI over plain HTTP.

The Vultr nginx upstream deliberately sends `Host: localhost` to the local
Phoenix stack and preserves the public hostname in `X-Forwarded-Host`. The
current local Docker Desktop backend was started without `PHOENIX_DOMAIN`, so
passing `Host: app.phoenixtechnosolutions.in` through the tunnel causes the
backend Host guard to reject login with `Invalid Host header`.

## Preferred Tunnel Owner: Docker Sidecar

The local Docker Desktop stack owns the tunnel through the
`vultr-tunnel` service in `docker-compose.live.single.yml`.

```text
phoenix-v9-vultr-tunnel
  -> ssh -R 127.0.0.1:18080:nginx:80 phoenixproxy@65.20.69.50
```

The sidecar:

- starts after `phoenix-v9-web` is healthy;
- waits for `http://nginx/readyz`;
- copies the mounted SSH key to container-private `/tmp` with mode `0600`;
- reconnects if SSH exits;
- uses `restart: unless-stopped`.

The sidecar uses this local secret path by default:

```text
C:\Users\abhis\.ssh\phoenix_vultr_proxy_workspace_ed25519
```

If the key is stored elsewhere, set `VULTR_REVERSE_TUNNEL_SSH_KEY` before
running Compose.

Useful checks:

```powershell
docker ps --filter "name=phoenix-v9-vultr-tunnel"
docker logs --tail 50 phoenix-v9-vultr-tunnel
```

## Manual Windows Tunnel

From the repo root on Windows:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\ops\start_vultr_reverse_tunnel.ps1
```

The tunnel maps:

```text
Vultr 127.0.0.1:18080 -> Windows 127.0.0.1:80
```

Use this only as an operator fallback when the Docker sidecar is unavailable.
Keep this process running while using the domain. If it exits, nginx on Vultr
will return an upstream error even though Phoenix may still be running locally.
The tunnel script is single-instance and waits for local Phoenix `/readyz`
before connecting, so it can be started before Docker Desktop has fully brought
the Phoenix containers back.

## Windows Scheduled Task Fallback

The fallback Windows Scheduled Task can be installed from the repo root:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\ops\install_vultr_reverse_tunnel_task.ps1
```

The task is named `Phoenix Vultr Reverse Tunnel`. It runs hidden for the
logged-in Windows user, starts two minutes after logon, and writes output to:

```text
.artifacts\vultr_proxy\reverse_tunnel.task.log
```

Keep the task disabled while the Docker sidecar is active so both tunnel owners
do not race for Vultr `127.0.0.1:18080`. To use the fallback:

```powershell
docker stop phoenix-v9-vultr-tunnel
Enable-ScheduledTask -TaskName "Phoenix Vultr Reverse Tunnel"
Start-ScheduledTask -TaskName "Phoenix Vultr Reverse Tunnel"
```

After the sidecar is repaired and verified, disable the fallback again:

```powershell
Disable-ScheduledTask -TaskName "Phoenix Vultr Reverse Tunnel"
```

Because Docker Desktop and the SSH key are user-profile resources, the fallback
task is registered as a logon task, not a pre-login machine startup service.
After a reboot, Docker Desktop remains the dependency that must start before the
sidecar can reconnect.

Useful checks:

```powershell
Get-ScheduledTask -TaskName "Phoenix Vultr Reverse Tunnel"
Get-ScheduledTaskInfo -TaskName "Phoenix Vultr Reverse Tunnel"
Start-ScheduledTask -TaskName "Phoenix Vultr Reverse Tunnel"
```

## Verification

If DNS or TLS needs to be revalidated during recovery:

```powershell
curl.exe -I http://65.20.69.50/readyz
```

Do not enter credentials or perform live operations through `http://65.20.69.50`.
Use the public UI only after HTTPS is active on the domain.

After GoDaddy DNS resolves for `app.phoenixtechnosolutions.in`, issue or renew
the certificate:

```powershell
ssh -i C:\Users\abhis\.ssh\phoenix_vultr_proxy_workspace_ed25519 phoenixproxy@65.20.69.50 `
  'sudo certbot --nginx -d app.phoenixtechnosolutions.in --redirect --non-interactive --agree-tos --register-unsafely-without-email'
```

Then verify HTTPS:

```powershell
curl.exe -I https://app.phoenixtechnosolutions.in/readyz
curl.exe https://app.phoenixtechnosolutions.in/health
```

Expected:

- `/readyz` returns HTTP 200
- `/health` reports `ready:true`
- Phoenix UI loads at `https://app.phoenixtechnosolutions.in`
- dummy `/auth/login` with a wrong password returns HTTP 401, not
  `Invalid Host header`
- HTTP requests redirect to HTTPS

Check certificate state from the proxy:

```powershell
ssh -i C:\Users\abhis\.ssh\phoenix_vultr_proxy_workspace_ed25519 phoenixproxy@65.20.69.50 `
  'sudo certbot certificates -d app.phoenixtechnosolutions.in'
```
