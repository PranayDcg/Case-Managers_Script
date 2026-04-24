# CM Weekly Upload Automation

Slack-driven automation for uploading weekly Case Manager lead lists to the Convoso dialer. A bot watches a Slack channel for file drops, parses the spreadsheet, uploads leads to the correct campaigns, and activates new lists on a schedule -- all with interactive confirmation steps.

## How It Works

1. Drop a `.xlsx` or `.csv` file into the `#cm-uploads` Slack channel
2. Bot parses the file, splits leads by Case Manager, and posts a summary
3. Type `proceed` to create inactive lists, clear campaign-specific DNC entries, and upload leads
4. Type `activate` to go live (or let it auto-activate at 5:30 PM)
5. Bot activates new lists, deactivates old ones, and posts a summary to Asana

## CM Campaign Config

| Case Manager        | Campaign ID |
|---------------------|-------------|
| Alondra Alva        | 4135        |
| Alquinton Smith     | 4999        |
| Asmeri Mix          | 4997        |
| Darrell Fair        | 4985        |
| Maximiliano Muller  | 4991        |
| Eli Paredes         | 5003        |
| Patricia Tercier    | 5007        |
| Sammaurae Barr      | 4993        |
| Wanicka Cassagnol   | 4995        |

To add or remove a CM, edit the `CM_CAMPAIGNS` dict in `cm_upload.py`.

## File Format

The input file (xlsx or csv) must have these columns in order:

| Column 1   | Column 2     | Column 3       | Column 4   |
|------------|--------------|----------------|------------|
| File Number | Client Name | Case Manager   | Cell Phone |

The Case Manager column value must match a key in `CM_CAMPAIGNS` (case-sensitive).

## Custom Fields

Each lead is uploaded with three custom fields:

- `client_name` -- client's full name
- `case_manager` -- CM name from the spreadsheet
- `file` -- file/case number

Phone numbers are normalized to 10 digits (leading `1` stripped).

## Setup

### Environment Variables

Create a `.env` file in the `cm/` directory (or the parent `Convoso/` directory):

```
# Slack
CM_SLACK_BOT_TOKEN=xoxb-...       # Bot token with channels:history, chat:write, files:read
CM_CHANNEL_ID=C0...               # Slack channel ID for #cm-uploads

# Convoso
CONVOSO_API_TOKEN=...             # Convoso API auth token
CONVOSO_BASE_URL=https://api.convoso.com   # Optional, defaults to this

# Asana (optional, for post-activation reporting)
ASANA_TOKEN=...                   # Asana personal access token
ASANA_PROJECT_GID=...             # Asana project GID
```

### Python Dependencies

```
pip install requests python-dotenv openpyxl
```

## How to Run

### Manual Start

```bash
python slack_watcher.py
```

### Auto-Restart (Windows)

Double-click `start_watcher.bat` -- it runs the watcher in a loop, restarting automatically after 10 seconds if it crashes.

### Background/Silent Mode (Windows)

Double-click `start_watcher.vbs` -- launches the batch file with no visible console window. Useful for always-on operation.

Only one instance can run at a time (enforced via `watcher.lock` with PID checking).

## Slack Commands

Type these in the `#cm-uploads` channel:

| Command      | When to Use                          | What It Does                                                    |
|--------------|--------------------------------------|-----------------------------------------------------------------|
| *(file drop)* | Anytime (idle state)                | Parses file, shows CM/lead summary                              |
| `proceed`    | After file summary is shown          | Creates inactive lists, clears campaign DNC, uploads all leads  |
| `cancel`     | After file summary or after upload   | Aborts the current session                                      |
| `activate`   | After upload completes               | Activates new lists, deactivates old ones, posts Asana report   |
| `status`     | Anytime                             | Shows current bot state (idle / pending / uploaded)              |

## Weekly Workflow

1. Export the week's CM lead list as `.xlsx` or `.csv`
2. Drop it in `#cm-uploads`
3. Review the summary -- make sure all CMs are matched to campaigns
4. Type `proceed` to upload (lists are created **inactive**)
5. Either type `activate` when ready, or wait for auto-activation at **5:30 PM**
6. Confirm activation summary in Slack and check the Asana report

## Files

| File                | Purpose                                              |
|---------------------|------------------------------------------------------|
| `slack_watcher.py`  | Slack polling bot -- main entry point                |
| `cm_upload.py`      | Convoso API logic: parse, upload, activate, Asana    |
| `start_watcher.bat` | Auto-restart wrapper (visible console)               |
| `start_watcher.vbs` | Silent launcher (no console window)                  |
| `.env`              | Secrets and config (not committed)                   |
| `raw_files/`        | Downloaded files from Slack (auto-created)            |
| `logs/watcher.log`  | Runtime log (auto-created)                           |
| `slack_processed.json` | Tracks processed Slack message timestamps          |
| `watcher.lock`      | PID lock file to prevent duplicate instances         |

## Notes

- The bot polls Slack every 30 seconds (not websocket-based)
- Heartbeat logged every ~10 minutes (every 20 polls)
- On repeated API errors, the bot backs off up to 5 minutes between retries
- DNC clearing is campaign-specific -- only removes DNC entries matching the target campaign, not global DNC
- Duplicate checking is disabled on upload (`duplicate_check: NO`) since each week is a fresh list
