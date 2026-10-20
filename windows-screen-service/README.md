# displayshow — Windows screen service

LEVETOP front-panel serial display as a Windows service (`DisplayScreen`).

| Docs | |
|---|---|
| 中文部署手册 | [部署手册.md](./部署手册.md) |
| English deployment guide | [DEPLOYMENT.en.md](./DEPLOYMENT.en.md) |

**Version:** 1.1.1 · **Default install:** `%ProgramData%\displayshow` · **Service:** `DisplayScreen`

## Quick install (Administrator PowerShell)

```powershell
cd windows-screen-service
.\install.ps1
```

```powershell
Get-Service DisplayScreen
.\run_screen_updater.bat --dry-run --once
```
