# displayshow — Ubuntu screen service

LEVETOP front-panel serial display as a systemd service.

| Docs | |
|---|---|
| 中文部署手册 | [部署手册.md](./部署手册.md) |
| English deployment guide | [DEPLOYMENT.en.md](./DEPLOYMENT.en.md) |

**Version:** 1.1.1 · **Install path:** `/opt/displayshow` · **Unit:** `display-screen.service`

## Quick install

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip
cd ubuntu-screen-service
sudo ./INSTALL.sh
```

```bash
systemctl status display-screen.service
journalctl -u display-screen.service -f
```
