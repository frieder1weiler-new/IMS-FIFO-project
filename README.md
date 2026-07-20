# IMS FIFO Project

This Flask application manages FIFO pallet inventory for production and assembly workflows.

## Deployment notes

### Gunicorn
Run the app with Gunicorn:

```bash
gunicorn --workers 4 --bind 0.0.0.0:5000 wsgi:app
```

### Nginx
A simple reverse proxy block can forward requests to Gunicorn:

```nginx
server {
    listen 80;
    server_name ims.example.com;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### systemd
Example service unit:

```ini
[Unit]
Description=IMS FIFO Flask service
After=network.target

[Service]
WorkingDirectory=/opt/ims-fifo
Environment=PATH=/opt/ims-fifo/venv/bin
ExecStart=/opt/ims-fifo/venv/bin/gunicorn --workers 4 --bind 0.0.0.0:5000 wsgi:app
Restart=always

[Install]
WantedBy=multi-user.target
```
