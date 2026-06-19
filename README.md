# kegao_qq_bot_codex

Workspace for the class desktop communication project.

## Components

- `server/`: raw WebSocket relay service
- `client/`: classroom desktop client
- `Nonebot/kgGao29Robot/`: QQ bot and NoneBot plugin project
- `shared/`: shared configuration and protocol contracts

## Current Status

Step 1 and Step 2 are in place:

- project skeleton exists for `server`, `client`, `shared`, `configs`, `scripts`, `logs`, and `data`
- shared root configuration is defined in `configs/app.example.toml`
- shared raw WebSocket contract is defined in `shared/protocol.py`
- plugin runtime environment can be derived from root config before NoneBot starts

## Local Smoke Tests

```powershell
.\scripts\run_server.ps1 -SmokeTest
.\scripts\run_client.ps1 -SmokeTest
.\scripts\run_plugin.ps1 -SmokeTest
```

Or run them together:

```powershell
.\scripts\smoke_test_all.ps1
```

## Config Notes

- copy `configs/app.example.toml` to `configs/app.toml` for local overrides
- `INTERNAL_TOKEN` is the only internal auth token name
- all internal communication uses raw WebSocket, not Socket.IO
- `docs/PROJECT_CONTRACT.md` records the current shared contract
