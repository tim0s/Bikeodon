# Deploying Bikeodon

Target: Oracle Cloud free tier (Ampere A1, Oracle Linux), nginx reverse proxy, gunicorn WSGI server.

## 1. Server prerequisites

```bash
sudo dnf update -y
sudo dnf install -y git python3 python3-pip nginx
sudo dnf install -y liberation-fonts dejavu-sans-fonts google-noto-emoji-color-fonts
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
STRAVA_WEBHOOK_VERIFY_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(16))")
EOF
chmod 600 /opt/Bikeodon/.env
```

## 4. Initialise the database

```bash
cd /opt/Bikeodon
source .venv/bin/activate
python3 -c "from database import init_db; init_db('bikeodon.db')"
mkdir -p output
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

Enable and start:

```bash
sudo mkdir -p /var/log/bikeodon
sudo chown opc:opc /var/log/bikeodon

sudo systemctl daemon-reload
sudo systemctl enable bikeodon-web
sudo systemctl start bikeodon-web

sudo systemctl status bikeodon-web
```

## 6. nginx reverse proxy

Create `/etc/nginx/conf.d/bikeodon.conf`:

```nginx
server {
    listen 80;
    server_name bikeodon.org www.bikeodon.org;

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

```bash
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl start nginx
```

## 7. SSL with Cloudflare (recommended)

Set your Cloudflare SSL/TLS mode to **Flexible** — Cloudflare handles HTTPS to the browser; the origin serves plain HTTP on port 80.

Alternatively, use Let's Encrypt:

```bash
sudo dnf install -y certbot python3-certbot-nginx
sudo certbot --nginx -d bikeodon.org -d www.bikeodon.org
sudo certbot renew --dry-run
```

## 8. Register the Strava webhook

The web service must be running and reachable before doing this.

```bash
cd /opt/Bikeodon
source .venv/bin/activate
python main.py webhook subscribe https://bikeodon.org/strava/webhook

# Verify
python main.py webhook status
```

Strava allows only one subscription per app. To change the callback URL:

```bash
python main.py webhook unsubscribe
python main.py webhook subscribe https://bikeodon.org/strava/webhook
```

In your Strava API app settings at strava.com/settings/api, update the **Authorization Callback Domain** to `bikeodon.org`.

## 9. Verify everything is running

```bash
sudo systemctl status bikeodon-web nginx
sudo journalctl -u bikeodon-web -f    # live web logs
```

## Updating to a new version

```bash
cd /opt/Bikeodon
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart bikeodon-web
```

---

## Administration

All admin commands run on the server with the virtualenv active:

```bash
cd /opt/Bikeodon
source .venv/bin/activate
```

### Grant admin access

```bash
python main.py admin <username>
```

Admins can see the admin dashboard at `/admin`, trigger a full Strava history sync, and view any render or post errors across all users.

### Invite code

Registration is open to anyone unless an invite code is set:

```bash
python main.py invite-code              # show current code
python main.py invite-code <code>       # set a new code
python main.py invite-code ""           # clear (open registration)
```

### Strava webhook

```bash
python main.py webhook status           # show active subscription
python main.py webhook subscribe <url>  # register
python main.py webhook unsubscribe      # remove
```

### Import activities from the CLI

Useful for the initial import or bulk backfill. The web UI "Sync from Strava" button fetches the 10 most recent; for a full history use:

```bash
python main.py sync --full              # all activities for the first Strava-connected user
python main.py sync --count 50          # fetch the 50 most recent
python main.py sync --user <username>   # specific user
```

### Render a specific activity

```bash
python main.py render <activity_id>     # re-render the map image
python main.py charts <activity_id>     # re-generate HR/power charts
```

Failed renders are also visible and re-triggerable from the admin dashboard at `/admin`.

### View or edit settings

```bash
python main.py config list              # all settings for the default user
python main.py config list --user <u>  # specific user
python main.py config get mastodon token
python main.py config set charts ftp 280
```
