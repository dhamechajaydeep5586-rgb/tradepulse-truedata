#!/usr/bin/env bash
# One-time provisioning script for the TradePulse AI backend on an Oracle Cloud
# Always Free Ubuntu VM. Run this ON THE VM as the `ubuntu` user:
#
#   scp -i your-key.pem backend/deploy/oracle/setup.sh ubuntu@<VM_IP>:~/
#   ssh -i your-key.pem ubuntu@<VM_IP>
#   chmod +x setup.sh && ./setup.sh
#
# After it finishes you still need to:
#   1. Fill in /home/ubuntu/tradepulse-ai/backend/.env (copy from Render's env vars)
#   2. sudo systemctl start tradepulse-backend
#   3. Point nginx server_name at your domain/IP and run certbot for HTTPS
set -euo pipefail

REPO_URL="https://github.com/dhamechajaydeep5586-rgb/tradepulse-ai.git"
APP_DIR="$HOME/tradepulse-ai"

echo "==> Opening OS-level firewall for HTTP/HTTPS (Oracle images default-deny)"
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save 2>/dev/null || true

echo "==> Installing system packages"
sudo apt update
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.13 python3.13-venv git nginx certbot python3-certbot-nginx

echo "==> Cloning repo"
if [ ! -d "$APP_DIR" ]; then
    git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR/backend"

echo "==> Creating virtualenv"
python3.13 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

mkdir -p logs

if [ ! -f .env ]; then
    echo "==> No .env found — copy Render's environment variables into $APP_DIR/backend/.env before starting the service."
    touch .env
fi

echo "==> Installing systemd unit"
sudo cp deploy/oracle/tradepulse-backend.service /etc/systemd/system/tradepulse-backend.service
sudo systemctl daemon-reload
sudo systemctl enable tradepulse-backend

echo "==> Installing nginx site"
sudo cp deploy/oracle/nginx.conf /etc/nginx/sites-available/tradepulse-backend
sudo ln -sf /etc/nginx/sites-available/tradepulse-backend /etc/nginx/sites-enabled/tradepulse-backend
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx

echo "==> Done."
echo "Next: fill in .env, then: sudo systemctl start tradepulse-backend"
echo "Check status with: sudo systemctl status tradepulse-backend"
echo "Then edit /etc/nginx/sites-available/tradepulse-backend to set server_name, and run:"
echo "  sudo certbot --nginx -d api.yourdomain.com"
