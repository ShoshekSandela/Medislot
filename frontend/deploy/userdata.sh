#!/bin/bash
set -euo pipefail
# Required user-data parameters: S3_BUCKET, S3_KEY, API_BASE_URL, SECRET_KEY.
exec > >(tee /var/log/medislot-userdata.log|logger -t user-data -s 2>/dev/console) 2>&1
yum update -y
yum install -y python3.12 python3.12-pip nginx unzip awscli
: "${S3_BUCKET:?Set S3_BUCKET}"; : "${S3_KEY:?Set S3_KEY}"; : "${API_BASE_URL:?Set API_BASE_URL}"; : "${SECRET_KEY:?Set SECRET_KEY}"
mkdir -p /opt/medislot /etc/medislot
aws s3 cp "s3://${S3_BUCKET}/${S3_KEY}" /tmp/frontend.zip
rm -rf /opt/medislot/frontend
unzip -q /tmp/frontend.zip -d /opt/medislot
# Archive should contain frontend/ at its root.
chown -R ec2-user:nginx /opt/medislot
python3.12 -m venv /opt/medislot/frontend/.venv
/opt/medislot/frontend/.venv/bin/pip install --upgrade pip
/opt/medislot/frontend/.venv/bin/pip install -r /opt/medislot/frontend/requirements.txt
cat > /etc/medislot/web.env <<EOF
API_BASE_URL=${API_BASE_URL}
SECRET_KEY=${SECRET_KEY}
APP_ENV=production
SESSION_COOKIE_SECURE=0
SESSION_DIR=/var/lib/medislot/sessions
STATIC_BASE_URL=/static
REQUEST_TIMEOUT_SECONDS=10
EOF
mkdir -p /var/lib/medislot/sessions; chown -R ec2-user:nginx /var/lib/medislot
cp /opt/medislot/frontend/deploy/medislot-web.service /etc/systemd/system/medislot-web.service
cp /opt/medislot/frontend/deploy/nginx.conf /etc/nginx/conf.d/medislot.conf
systemctl daemon-reload; systemctl enable --now medislot-web; systemctl enable --now nginx
