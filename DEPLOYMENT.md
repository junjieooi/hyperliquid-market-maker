# Production Deployment Guide

This guide covers deploying the market maker bot to a VPS server with security best practices.

## 🎯 Recommended Setup

- **VPS Provider**: DigitalOcean, AWS EC2, Vultr, or Linode
- **OS**: Ubuntu 22.04 LTS (most tested and stable)
- **RAM**: Minimum 2GB (4GB recommended)
- **Storage**: 20GB SSD

**Cost**: $10-20/month for reliable hosting

---

## 📋 Step-by-Step Deployment

### 1. Provision a VPS

**DigitalOcean Example** (easiest):
```bash
# Create a $12/month droplet:
# - Ubuntu 22.04 LTS
# - 2GB RAM / 50GB SSD
# - Choose datacenter close to you for lower latency
```

### 2. Initial Server Setup (Security First!)

SSH into your server:
```bash
ssh root@YOUR_SERVER_IP
```

**Create a non-root user**:
```bash
# Create user
adduser trader
usermod -aG sudo trader

# Copy SSH keys to new user
rsync --archive --chown=trader:trader ~/.ssh /home/trader
```

**Disable root login** (IMPORTANT for security):
```bash
# Edit SSH config
sudo nano /etc/ssh/sshd_config

# Set these values:
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes

# Restart SSH
sudo systemctl restart sshd
```

**Set up firewall**:
```bash
# Allow SSH only
sudo ufw allow OpenSSH
sudo ufw enable
```

**Logout and reconnect as trader**:
```bash
exit
ssh trader@YOUR_SERVER_IP
```

### 3. Install Dependencies

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python and tools
sudo apt install -y python3.10 python3.10-venv python3-pip git htop tmux

# Install fail2ban (protects against brute force)
sudo apt install -y fail2ban
sudo systemctl enable fail2ban
sudo systemctl start fail2ban
```

### 4. Deploy Your Bot

**Clone or upload your code**:

**Option A: Using Git** (recommended if you have a private repo)
```bash
cd ~
git clone YOUR_PRIVATE_REPO_URL tradexyz
cd tradexyz
```

**Option B: Using SCP** (if no git repo)
```bash
# On your LOCAL machine:
scp -r /path/to/tradexyz trader@YOUR_SERVER_IP:~/
```

### 5. Secure Configuration with Environment Variables

**NEVER store private keys in config.json on the server!**

Create environment file:
```bash
cd ~/tradexyz
nano .env
```

Add your secrets:
```bash
# .env file (this file is NOT committed to git)
PRIVATE_KEY=
ACCOUNT_ADDRESS=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=-
```

**Secure the .env file**:
```bash
chmod 600 .env  # Only you can read it
```

**Update config.json to use environment variables**:
```json
{
  "private_key_env": "PRIVATE_KEY",
  "account_address_env": "ACCOUNT_ADDRESS",
  "telegram": {
    "bot_token_env": "TELEGRAM_BOT_TOKEN",
    "chat_id_env": "TELEGRAM_CHAT_ID"
  }
}
```

### 6. Install Python Dependencies

```bash
cd ~/tradexyz
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 7. Test the Bot

```bash
# Load environment variables and test
source .env
python market_maker.py
```

Watch the logs for a few minutes. Press `Ctrl+C` to stop.

### 8. Set Up as a System Service (24/7 Operation)

Create a systemd service file:
```bash
sudo nano /etc/systemd/system/market-maker.service
```

Paste this configuration:
```ini
[Unit]
Description=Hyperliquid Market Maker Bot
After=network.target

[Service]
Type=simple
User=trader
WorkingDirectory=/home/trader/tradexyz
Environment="PATH=/home/trader/tradexyz/venv/bin"
EnvironmentFile=/home/trader/tradexyz/.env
ExecStart=/home/trader/tradexyz/venv/bin/python market_maker.py
Restart=always
RestartSec=10
StandardOutput=append:/home/trader/tradexyz/logs/systemd.log
StandardError=append:/home/trader/tradexyz/logs/systemd-error.log

# Security settings
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/trader/tradexyz/logs

[Install]
WantedBy=multi-user.target
```

**Enable and start the service**:
```bash
# Create logs directory if it doesn't exist
mkdir -p ~/tradexyz/logs

# Reload systemd
sudo systemctl daemon-reload

# Enable auto-start on boot
sudo systemctl enable market-maker

# Start the bot
sudo systemctl start market-maker

# Check status
sudo systemctl status market-maker
```

### 9. Monitor Your Bot

**View logs in real-time**:
```bash
# System logs
sudo journalctl -u market-maker -f

# Application logs
tail -f ~/tradexyz/logs/main.log
tail -f ~/tradexyz/logs/fills.log
```

**Check status**:
```bash
sudo systemctl status market-maker
```

**Restart bot**:
```bash
sudo systemctl restart market-maker
```

**Stop bot**:
```bash
sudo systemctl stop market-maker
```

---

## 🔐 Security Best Practices

### ✅ DO:

1. **Use environment variables** for all secrets
2. **Set proper file permissions**:
   ```bash
   chmod 600 .env                    # Only you can read
   chmod 700 ~/tradexyz              # Only you can access directory
   ```
3. **Enable firewall** (only allow SSH):
   ```bash
   sudo ufw status
   ```
4. **Use SSH keys** (never passwords)
5. **Regular updates**:
   ```bash
   sudo apt update && sudo apt upgrade -y
   ```
6. **Monitor logs** regularly via Telegram notifications
7. **Set up automatic backups** of config and logs

### ❌ DON'T:

1. **Don't store private keys in config.json**
2. **Don't commit .env to git**:
   ```bash
   echo ".env" >> .gitignore
   ```
3. **Don't use root user** to run the bot
4. **Don't open unnecessary ports**
5. **Don't share server IP publicly**

---

## 📊 Monitoring & Alerts

### Set Up Monitoring

**1. System Resource Monitoring**:
```bash
# Install htop for real-time monitoring
htop
```

**2. Log Monitoring Script**:

Create `~/check_bot.sh`:
```bash
#!/bin/bash
# Quick bot health check

echo "=== Bot Status ==="
sudo systemctl status market-maker --no-pager

echo ""
echo "=== Recent Logs (last 20 lines) ==="
tail -20 ~/tradexyz/logs/main.log

echo ""
echo "=== Recent Fills ==="
tail -10 ~/tradexyz/logs/fills.log

echo ""
echo "=== System Resources ==="
free -h
df -h | grep -v loop
```

Make it executable:
```bash
chmod +x ~/check_bot.sh
```

Run it:
```bash
./check_bot.sh
```

### Telegram Alerts

The bot already sends updates every 15 minutes. To get immediate alerts on errors:

Add to your Telegram handler in `market_maker.py` (optional):
```python
# Send critical errors to Telegram immediately
if "ERROR" in log_message or "FAILED" in log_message:
    await self.send_telegram_message(f"⚠️ ALERT: {log_message}")
```

---

## 🔄 Update & Maintenance

### Updating the Bot

```bash
# Stop the bot
sudo systemctl stop market-maker

# Backup current version
cp -r ~/tradexyz ~/tradexyz_backup_$(date +%Y%m%d)

# Pull updates (if using git)
cd ~/tradexyz
git pull

# Or upload new files via SCP
# scp -r /path/to/new/files trader@YOUR_SERVER_IP:~/tradexyz/

# Restart the bot
sudo systemctl start market-maker

# Monitor logs
sudo journalctl -u market-maker -f
```

### Rolling Back

```bash
sudo systemctl stop market-maker
rm -rf ~/tradexyz
mv ~/tradexyz_backup_YYYYMMDD ~/tradexyz
sudo systemctl start market-maker
```

---

## 💾 Backup Strategy

### Automated Daily Backups

Create `~/backup.sh`:
```bash
#!/bin/bash
BACKUP_DIR=~/backups
DATE=$(date +%Y%m%d)

mkdir -p $BACKUP_DIR

# Backup config and logs
tar -czf $BACKUP_DIR/tradexyz_$DATE.tar.gz \
  ~/tradexyz/config.json \
  ~/tradexyz/.env \
  ~/tradexyz/logs/

# Keep only last 7 days
find $BACKUP_DIR -name "tradexyz_*.tar.gz" -mtime +7 -delete

echo "Backup completed: tradexyz_$DATE.tar.gz"
```

**Schedule with cron**:
```bash
chmod +x ~/backup.sh
crontab -e

# Add this line (runs daily at 3 AM):
0 3 * * * /home/trader/backup.sh >> /home/trader/backup.log 2>&1
```

### Download Backups Locally

```bash
# On your LOCAL machine:
scp trader@YOUR_SERVER_IP:~/backups/tradexyz_YYYYMMDD.tar.gz ~/Downloads/
```

---

## 🆘 Troubleshooting

### Bot Won't Start

```bash
# Check service status
sudo systemctl status market-maker

# Check logs
sudo journalctl -u market-maker -n 50

# Check permissions
ls -la ~/tradexyz/.env
ls -la ~/tradexyz/logs/
```

### High CPU Usage

```bash
# Check if bot is running multiple instances
ps aux | grep market_maker

# If yes, kill old instances:
sudo systemctl restart market-maker
```

### Network Issues

```bash
# Test API connectivity
curl https://api.hyperliquid.xyz/info

# Check DNS
ping api.hyperliquid.xyz

# Restart networking (if needed)
sudo systemctl restart networking
```

### Disk Space Full

```bash
# Check disk usage
df -h

# Clean old logs (older than 30 days)
find ~/tradexyz/logs -name "*.log" -mtime +30 -delete

# Or compress old logs
gzip ~/tradexyz/logs/*.log.1
```

---

## 💰 Cost Optimization

### Minimum Viable Setup: ~$6/month

**Vultr/DigitalOcean Basic**:
- 1GB RAM / 25GB SSD: $6/month
- Good for single market maker with low activity

### Recommended Setup: ~$12/month

**DigitalOcean/Vultr Regular**:
- 2GB RAM / 50GB SSD: $12/month
- Handles multiple layers, high volume, logs

### Enterprise Setup: ~$40/month

**AWS EC2 t3.small**:
- 2GB RAM / auto-scaling
- Better uptime SLA
- Advanced monitoring

---

## 🎯 Quick Reference Commands

```bash
# Start bot
sudo systemctl start market-maker

# Stop bot
sudo systemctl stop market-maker

# Restart bot
sudo systemctl restart market-maker

# View status
sudo systemctl status market-maker

# View live logs
sudo journalctl -u market-maker -f

# Check bot health
./check_bot.sh

# Update bot
sudo systemctl stop market-maker && cd ~/tradexyz && git pull && sudo systemctl start market-maker

# Emergency stop
sudo systemctl stop market-maker && pkill -f market_maker.py
```

---

## 📱 Remote Access (Optional)

### Access Server from Phone

**Using Termius** (iOS/Android):
1. Download Termius app
2. Add your server (SSH with key authentication)
3. Can view logs and restart bot from anywhere

**Using Telegram**:
- Already set up! You get updates every 15 minutes
- Can add commands to manually trigger actions (advanced)

---

## ✅ Production Checklist

Before going live on server:

- [ ] VPS provisioned with Ubuntu 22.04
- [ ] Non-root user created
- [ ] SSH key authentication enabled
- [ ] Root login disabled
- [ ] Firewall configured (only SSH)
- [ ] fail2ban installed and running
- [ ] Environment variables in .env (not config.json)
- [ ] .env has correct permissions (chmod 600)
- [ ] Bot tested manually (works for 5+ minutes)
- [ ] systemd service created and enabled
- [ ] Bot starts automatically on server reboot
- [ ] Logs are being written
- [ ] Telegram notifications working
- [ ] Backup script scheduled
- [ ] Monitoring script ready (check_bot.sh)
- [ ] Recovery plan documented

---

**You're now running a professional, secure market maker! 🚀**

*Remember to monitor regularly and start with small position sizes.*
