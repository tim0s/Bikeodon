# Deploying Bikeodon

Target: Oracle Cloud free tier (Ampere A1, Oracle Linux), nginx reverse proxy, gunicorn WSGI server, systemd services.

## 1. Server prerequisites

```bash
sudo dnf update -y
sudo dnf install -y git python3 python3-pip nginx
pip3 install gunicorn
```

Open firewall ports (if not already done):

```bash
sudo firewall-cmd --permanent --add-service=http
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --reload
```

## 2. Clone the repo

```bash
cd /opt
sudo git clone https://github.com/tim0s/Bikeodon.git
sudo chown -R opc:opc /opt/Bikeodon
cd /opt/Bikeodon
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Create the .env file

```bash
cat > /opt/Bikeodon/.env <<EOF
STRAVA_CLIENT_ID=your_client_id
STRAVA_CLIENT_SECRET=your_client_secret
FLASK_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
EOF
chmod 600 /opt/Bikeodon/.env
```

## 4. Initialise the database

```bash
cd /opt/Bikeodon
source .venv/bin/activate
python3 -c "from database import init_db; init_db('bikeodon.db')"
```

Create the output directory:

```bash
mkdir -p /opt/Bikeodon/output
```

## 5. systemd — web app (gunicorn)

Create `/etc/systemd/system/bikeodon-web.service`:

```ini
[Unit]
Description=Bikeodon web app
After=network.target

[Service]
User=opc
WorkingDirectory=/opt/Bikeodon
EnvironmentFile=/opt/Bikeodon/.env
ExecStart=/opt/Bikeodon/.venv/bin/gunicorn \
    --workers 2 \
    --bind 127.0.0.1:5000 \
    --access-logfile /var/log/bikeodon/access.log \
    --error-logfile /var/log/bikeodon/error.log \
    app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## 6. systemd — daemon (Strava sync + auto-post)

Create `/etc/systemd/system/bikeodon-daemon.service`:

```ini
[Unit]
Description=Bikeodon sync daemon
After=network.target bikeodon-web.service

[Service]
User=opc
WorkingDirectory=/opt/Bikeodon
EnvironmentFile=/opt/Bikeodon/.env
ExecStart=/opt/Bikeodon/.venv/bin/python main.py daemon
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Enable and start both:

```bash
sudo mkdir -p /var/log/bikeodon
sudo chown opc:opc /var/log/bikeodon

sudo systemctl daemon-reload
sudo systemctl enable bikeodon-web bikeodon-daemon
sudo systemctl start bikeodon-web bikeodon-daemon

# Check status
sudo systemctl status bikeodon-web
sudo systemctl status bikeodon-daemon
```

## 7. nginx reverse proxy

Create `/etc/nginx/conf.d/bikeodon.conf`:

```nginx
server {
    listen 80;
    server_name bikeodon.org www.bikeodon.org;

    # Redirect www → bare domain
    if ($host = www.bikeodon.org) {
        return 301 https://bikeodon.org$request_uri;
    }

    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

Test and reload nginx:

```bash
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl start nginx
```

At this point `http://bikeodon.org` should serve the app.

## 8. SSL with Let's Encrypt

```bash
sudo dnf install -y certbot python3-certbot-nginx
sudo certbot --nginx -d bikeodon.org -d www.bikeodon.org
```

Certbot will rewrite the nginx config to add HTTPS and auto-redirect HTTP → HTTPS. It installs a cron job to renew the certificate automatically.

Verify renewal works:

```bash
sudo certbot renew --dry-run
```

## 9. Update Strava callback URL

In your Strava API app settings at strava.com/settings/api, update the **Authorization Callback Domain** from `localhost` to `bikeodon.org`.

## 10. Verify everything is running

```bash
sudo systemctl status bikeodon-web bikeodon-daemon nginx
sudo journalctl -u bikeodon-web -f       # live web logs
sudo journalctl -u bikeodon-daemon -f    # live daemon logs
```

## Updating to a new version

```bash
cd /opt/Bikeodon
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart bikeodon-web bikeodon-daemon
```
