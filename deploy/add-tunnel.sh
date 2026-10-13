#!/usr/bin/env bash
# Expone Cliente Fantasma en fantasma.eriktaveras.com por el túnel del homelab.
# Necesita sudo para el ingress en /etc/cloudflared: correr con `sudo bash deploy/add-tunnel.sh`.
set -euo pipefail

TUNEL=99458af3-46ef-4c70-9f74-36a49c81530c
HOST=fantasma.eriktaveras.com
PUERTO=${HTTP_PORT:-8134}
CFG=/etc/cloudflared/config.yml

# La ruta DNS usa ~/.cloudflared/cert.pem del usuario, no el de root.
USUARIO=${SUDO_USER:-$USER}
sudo -u "$USUARIO" cloudflared tunnel route dns "$TUNEL" "$HOST" || echo "La ruta DNS ya existía o falló; revisa arriba."

if ! grep -q "$HOST" "$CFG"; then
  cp "$CFG" "$CFG.bak.$(date +%s)"
  sed -i "/- service: http_status:404/i\  - hostname: $HOST\n    service: http://localhost:$PUERTO" "$CFG"
fi
cloudflared tunnel ingress validate --config "$CFG"
systemctl restart cloudflared && systemctl is-active cloudflared
curl -s -o /dev/null -w "https://$HOST -> %{http_code}\n" -L "https://$HOST/" || true
