#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${1:-app.phoenixtechnosolutions.in}"
TUNNEL_PORT="${TUNNEL_PORT:-18080}"

if [[ -z "${DOMAIN}" ]]; then
  echo "usage: $0 app.phoenixtechnosolutions.in" >&2
  exit 2
fi

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y nginx certbot python3-certbot-nginx openssh-server ufw

cat >/etc/ssh/sshd_config.d/90-phoenix-reverse-tunnel.conf <<'SSHCONF'
AllowTcpForwarding yes
GatewayPorts no
PermitTunnel no
ClientAliveInterval 30
ClientAliveCountMax 3
SSHCONF
systemctl restart ssh

install -d -m 0755 /var/www/letsencrypt

cat >/etc/nginx/conf.d/phoenix_proxy_upgrade_map.conf <<'NGINXMAP'
map $http_upgrade $connection_upgrade {
    default upgrade;
    '' close;
}
NGINXMAP

cat >/etc/nginx/sites-available/phoenix-proxy <<NGINX
server {
    listen 80;
    server_name ${DOMAIN};

    client_max_body_size 10m;

    location /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
    }

    location = /readyz {
        proxy_pass http://127.0.0.1:${TUNNEL_PORT};
        proxy_http_version 1.1;
        # Local Docker Desktop Phoenix may not have PHOENIX_DOMAIN set; keep the
        # upstream Host inside the backend allow-list and preserve the public
        # hostname separately for diagnostics.
        proxy_set_header Host localhost;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    location / {
        if (\$scheme = http) {
            return 426;
        }

        proxy_pass http://127.0.0.1:${TUNNEL_PORT};
        proxy_http_version 1.1;
        # Local Docker Desktop Phoenix may not have PHOENIX_DOMAIN set; keep the
        # upstream Host inside the backend allow-list and preserve the public
        # hostname separately for diagnostics.
        proxy_set_header Host localhost;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
NGINX

rm -f /etc/nginx/sites-enabled/default
ln -sfn /etc/nginx/sites-available/phoenix-proxy /etc/nginx/sites-enabled/phoenix-proxy
nginx -t
systemctl enable --now nginx
systemctl reload nginx

ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable

if getent ahostsv4 "${DOMAIN}" >/dev/null 2>&1; then
  certbot --nginx \
    --non-interactive \
    --agree-tos \
    --register-unsafely-without-email \
    --redirect \
    -d "${DOMAIN}" || {
      echo "certbot failed; HTTP proxy is configured. Retry after DNS points ${DOMAIN} to this server." >&2
    }
else
  echo "DNS for ${DOMAIN} is not resolvable yet; skipping certificate issuance." >&2
fi

systemctl status nginx --no-pager
