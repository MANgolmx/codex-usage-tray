# Codex Usage Tray

Small Windows tray app for viewing your remaining Codex allowance.

The app **does not read browser cookies or store your ChatGPT password**. It starts the local `codex app-server` and calls the official `account/rateLimits/read` method using your existing Codex CLI sign-in.

## What it shows

- remaining allowance for the short window (usually 5 hours);
- remaining allowance for the weekly window;
- next reset time;
- additional-credit balance, when Codex returns it;
- the five-hour remaining percentage directly on the tray icon.

Data normally refreshes every three minutes. When a usage percentage changes, it refreshes every minute until the next successful check shows no change, then returns to the three-minute interval. If a fetch fails, the next automatic or manual refresh starts a new Codex app-server process; a single failure does not stop the updater. The menu includes a manual refresh and a link to the Usage page.
All information and error notifications use the title `Codex usage info`.

## Requirements

1. Windows 10/11.
2. Python 3.10+.
3. Codex CLI installed.
4. Signed in to Codex CLI with ChatGPT.

Check it with:

```powershell
codex --version
```

If Codex asks you to sign in, run:

```powershell
codex login
```

## Quick start

Open PowerShell in this folder:

```powershell
py -m pip install -r requirements.txt
py codex_usage_tray.py
```

After launch, an icon appears in the system tray. Its number is the remaining percentage for the 5-hour window. If Codex does not identify a 5-hour window, it falls back to the lowest available allowance.

## Build an EXE

Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

The output file will be here:

```text
dist\CodexUsageTray.exe
```

Python is not needed to run the EXE. Codex CLI must still be installed and signed in because the app gets usage data through it.

## Start automatically

After building the EXE:

1. Press `Win + R`.
2. Enter `shell:startup`.
3. Create a shortcut to `CodexUsageTray.exe` in the folder that opens.

## If it shows an error

First check:

```powershell
codex --version
codex
```

In interactive Codex you can also run `/status`. If `/status` cannot see the limits, the tray app may not be able to fetch them either.

## Important

This project shows the **Codex / agentic allowance** returned by Codex through app-server. It is not a counter for all regular ChatGPT messages.
