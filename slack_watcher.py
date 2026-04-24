"""
CM Weekly Upload — Slack Watcher Bot
Polls a Slack channel for file drops and commands.
Flow: file drop → summary → proceed → upload → activate
"""

import os, sys, re, json, time, logging, signal
from pathlib import Path
from datetime import datetime

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── Encoding fix for Windows ────────────────────────────────────────────────
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ── Config ──────────────────────────────────────────────────────────────────

CM_DIR = Path(__file__).resolve().parent
load_dotenv(CM_DIR / '.env')
load_dotenv(CM_DIR.parent / '.env')

SLACK_TOKEN     = os.getenv('CM_SLACK_BOT_TOKEN')
CHANNEL_ID      = os.getenv('CM_CHANNEL_ID')
POLL_INTERVAL   = 30
PROCESSED_FILE  = CM_DIR / 'slack_processed.json'
LOG_FILE        = CM_DIR / 'logs' / 'watcher.log'
LOCK_FILE       = CM_DIR / 'watcher.lock'
RAW_DIR         = CM_DIR / 'raw_files'
HEARTBEAT_INTERVAL = 20
BOT_USER_ID     = None

# ── Logging ─────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(str(LOG_FILE), encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger('cm_watcher')

# ── Session with retry + proxy bypass ───────────────────────────────────────

session = requests.Session()
retry = Retry(total=3, backoff_factor=2, status_forcelist=[500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retry))
session.trust_env = False
session.proxies = {'http': None, 'https': None}

# ── State ───────────────────────────────────────────────────────────────────
# Tracks the current session state:
#   None          = idle, waiting for file
#   'pending'     = file parsed, summary shown, waiting for proceed/cancel
#   'uploaded'    = leads uploaded, waiting for activate
pending_summary = None
session_state = None
auto_activate_time = None  # datetime when auto-activation should fire

AUTO_ACTIVATE_HOUR = 17    # 5:30 PM
AUTO_ACTIVATE_MINUTE = 30


# ── Processed tracking ──────────────────────────────────────────────────────

def load_processed():
    if PROCESSED_FILE.exists():
        try:
            return set(json.loads(PROCESSED_FILE.read_text()).get('processed', []))
        except Exception:
            pass
    return set()

def save_processed(processed):
    PROCESSED_FILE.write_text(json.dumps({'processed': list(processed)}))


# ── Lock file (singleton) ──────────────────────────────────────────────────

def check_lock():
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text().strip())
            # Check if PID is alive (Windows)
            import subprocess
            result = subprocess.run(
                ['tasklist', '/FI', f'PID eq {old_pid}'],
                capture_output=True, text=True, timeout=5
            )
            if str(old_pid) in result.stdout:
                log.error(f"Another watcher is running (PID {old_pid}). Exiting.")
                sys.exit(1)
        except (ValueError, subprocess.TimeoutExpired):
            pass
    # Write our PID with double-check
    LOCK_FILE.write_text(str(os.getpid()))
    time.sleep(1)
    written_pid = int(LOCK_FILE.read_text().strip())
    if written_pid != os.getpid():
        log.error(f"Lock race lost to PID {written_pid}. Exiting.")
        sys.exit(1)
    log.info(f"Lock acquired (PID {os.getpid()})")

def cleanup_lock():
    try:
        if LOCK_FILE.exists() and int(LOCK_FILE.read_text().strip()) == os.getpid():
            LOCK_FILE.unlink()
    except Exception:
        pass


# ── Slack helpers ───────────────────────────────────────────────────────────

def slack_get(method, params=None):
    resp = session.get(
        f'https://slack.com/api/{method}',
        headers={'Authorization': f'Bearer {SLACK_TOKEN}'},
        params=params or {},
        timeout=30
    )
    return resp.json()

def slack_post(method, payload):
    resp = session.post(
        f'https://slack.com/api/{method}',
        headers={'Authorization': f'Bearer {SLACK_TOKEN}'},
        json=payload,
        timeout=30
    )
    return resp.json()

def post_message(text):
    return slack_post('chat.postMessage', {'channel': CHANNEL_ID, 'text': text})

def download_file(url, dest):
    resp = session.get(url, headers={'Authorization': f'Bearer {SLACK_TOKEN}'}, timeout=60)
    resp.raise_for_status()
    with open(dest, 'wb') as f:
        f.write(resp.content)
    return dest


# ── Format helpers ──────────────────────────────────────────────────────────

def format_summary(summary):
    lines = [
        f"*CM List {summary['filename']}* — {len(summary['cms'])} Case Managers, {summary['total_leads']} leads",
        "",
    ]
    for cm_name, info in sorted(summary['cms'].items()):
        if info['campaign_id']:
            lines.append(f"  {cm_name:25s} — {info['count']:3d} leads   Campaign {info['campaign_id']}")
        else:
            lines.append(f"  {cm_name:25s} — {info['count']:3d} leads   *NO CAMPAIGN*")
    lines.append("")
    if summary['unmatched_cms']:
        lines.append(f"*{len(summary['unmatched_cms'])} CM(s) have no campaign.* Create campaigns first, then re-upload.")
        lines.append("")
    lines.append('Reply `proceed` to upload, or `cancel` to abort.')
    return '\n'.join(lines)


def format_upload_result(summary):
    lines = ["*Upload Complete*", ""]
    uploaded_count = 0
    total_leads = 0
    for r in summary.get('results', []):
        if r['status'] == 'uploaded':
            uploaded_count += 1
            total_leads += r['uploaded']
            dnc_note = f", {r.get('dnc_cleared', 0)} DNC cleared" if r.get('dnc_cleared') else ""
            fail_note = f" ({r['failed']} failed)" if r['failed'] else ""
            lines.append(f"  {r['cm']:25s} — {r['uploaded']} uploaded to list {r['list_id']} (inactive){dnc_note}{fail_note}")
        elif r['status'] == 'skipped':
            lines.append(f"  {r['cm']:25s} — skipped ({r['reason']})")
        elif r['status'] == 'error':
            lines.append(f"  {r['cm']:25s} — ERROR: {r['reason']}")
    lines.insert(1, f"{uploaded_count} campaigns, {total_leads} leads")
    lines.append("")
    if auto_activate_time:
        lines.append(f'Reply `activate` to go live now, or auto-activates at {auto_activate_time.strftime("%I:%M %p")}.')
    else:
        lines.append('Reply `activate` when ready to go live.')
    return '\n'.join(lines)


def format_activation(summary):
    lines = ["*Activation Complete*", ""]
    for a in summary.get('activation', []):
        lines.append(f"  {a['cm']:25s} — list {a['list_id']} ACTIVE, {a['deactivated']} old lists deactivated")
    lines.append("")
    lines.append("All done. Dialer traffic switched to new lists.")
    return '\n'.join(lines)


# ── Command handlers ────────────────────────────────────────────────────────

def handle_file(file_info):
    global pending_summary, session_state

    url = file_info.get('url_private_download') or file_info.get('url_private')
    name = file_info.get('name', 'unknown')
    ext = Path(name).suffix.lower()

    if ext not in ('.xlsx', '.csv'):
        post_message(f"Unsupported file type `{ext}`. Please upload `.xlsx` or `.csv`.")
        return

    post_message(f"Received `{name}` — parsing...")

    # Download
    dest = RAW_DIR / name
    download_file(url, dest)

    # Parse
    import cm_upload
    try:
        summary = cm_upload.parse_file(dest)
    except Exception as e:
        post_message(f"Error parsing file: {e}")
        log.error(f"Parse error: {e}")
        return

    pending_summary = summary
    session_state = 'pending'
    post_message(format_summary(summary))


def handle_proceed():
    global pending_summary, session_state, auto_activate_time

    if session_state != 'pending' or not pending_summary:
        post_message("Nothing to proceed with. Drop a file first.")
        return

    post_message("Uploading leads to Convoso... this may take a minute.")

    import cm_upload
    try:
        result = cm_upload.upload_all(pending_summary)
    except Exception as e:
        post_message(f"Upload error: {e}")
        log.error(f"Upload error: {e}")
        return

    pending_summary = result
    session_state = 'uploaded'

    # Set auto-activation at 5:30 PM today (or now+5min if already past 5:30)
    now = datetime.now()
    target = now.replace(hour=AUTO_ACTIVATE_HOUR, minute=AUTO_ACTIVATE_MINUTE, second=0, microsecond=0)
    if target <= now:
        # Already past 5:30 — activate in 5 minutes as safety buffer
        from datetime import timedelta
        target = now + timedelta(minutes=5)
    auto_activate_time = target
    log.info(f"Auto-activation scheduled for {target.strftime('%I:%M %p')}")

    post_message(format_upload_result(result))


def handle_cancel():
    global pending_summary, session_state, auto_activate_time

    if session_state in ('pending', 'uploaded'):
        pending_summary = None
        session_state = None
        auto_activate_time = None
        post_message("Cancelled.")
    else:
        post_message("Nothing to cancel.")


def handle_activate(auto=False):
    global pending_summary, session_state, auto_activate_time

    if session_state != 'uploaded' or not pending_summary:
        if not auto:
            post_message("Nothing to activate. Upload leads first with `proceed`.")
        return

    source = "Auto-activating" if auto else "Activating"
    post_message(f"{source} new lists and deactivating old ones...")

    import cm_upload
    try:
        result = cm_upload.activate(pending_summary)
    except Exception as e:
        post_message(f"Activation error: {e}")
        log.error(f"Activation error: {e}")
        return

    # Post to Asana
    asana_url = None
    try:
        asana_url = cm_upload.post_to_asana(result)
    except Exception as e:
        log.error(f"Asana error: {e}")

    pending_summary = None
    session_state = None
    auto_activate_time = None

    msg = format_activation(result)
    if asana_url:
        msg += f"\nAsana report: {asana_url}"
    post_message(msg)


def handle_status():
    if session_state is None:
        post_message("Idle — waiting for a file drop.")
    elif session_state == 'pending':
        cms = len(pending_summary.get('cms', {}))
        leads = pending_summary.get('total_leads', 0)
        post_message(f"File parsed: {cms} CMs, {leads} leads. Waiting for `proceed` or `cancel`.")
    elif session_state == 'uploaded':
        uploaded = sum(1 for r in pending_summary.get('results', []) if r['status'] == 'uploaded')
        timer = f" Auto-activates at {auto_activate_time.strftime('%I:%M %p')}." if auto_activate_time else ""
        post_message(f"Uploaded to {uploaded} campaigns. Waiting for `activate`.{timer}")


# ── Main loop ───────────────────────────────────────────────────────────────

def main():
    global BOT_USER_ID

    log.info("=" * 60)
    log.info("CM Slack Watcher starting")
    log.info(f"  Channel: {CHANNEL_ID}")
    log.info(f"  PID: {os.getpid()}")
    log.info("=" * 60)

    check_lock()

    # Get bot user ID to ignore own messages
    auth = slack_get('auth.test')
    if auth.get('ok'):
        BOT_USER_ID = auth.get('user_id')
        log.info(f"Bot user ID: {BOT_USER_ID}")
    else:
        log.error(f"auth.test failed: {auth}")
        sys.exit(1)

    processed = load_processed()
    poll_count = 0
    consecutive_errors = 0

    def shutdown(sig, frame):
        log.info("Shutting down...")
        cleanup_lock()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while True:
        try:
            result = slack_get('conversations.history', {'channel': CHANNEL_ID, 'limit': '10'})

            if not result.get('ok'):
                log.warning(f"Slack API error: {result.get('error')}")
            else:
                consecutive_errors = 0
                messages = result.get('messages', [])

                for msg in messages:
                    ts = msg.get('ts', '')
                    if ts in processed:
                        continue

                    # Skip bot's own messages
                    if msg.get('user') == BOT_USER_ID:
                        processed.add(ts)
                        save_processed(processed)
                        continue

                    files = msg.get('files', [])
                    text = (msg.get('text') or '').strip()

                    # Handle file uploads
                    if files:
                        for f in files:
                            try:
                                handle_file(f)
                            except Exception as e:
                                log.error(f"Error processing file: {e}")
                                post_message(f"Error processing file: {e}")
                        processed.add(ts)
                        save_processed(processed)

                    # Handle text commands
                    elif text:
                        lower = text.lower().strip()
                        if lower == 'proceed':
                            try:
                                handle_proceed()
                            except Exception as e:
                                log.error(f"Proceed error: {e}")
                                post_message(f"Error: {e}")
                        elif lower == 'cancel':
                            handle_cancel()
                        elif lower == 'activate':
                            try:
                                handle_activate()
                            except Exception as e:
                                log.error(f"Activate error: {e}")
                                post_message(f"Error: {e}")
                        elif lower == 'status':
                            handle_status()

                        processed.add(ts)
                        save_processed(processed)

        except Exception as e:
            consecutive_errors += 1
            log.error(f"Poll error ({consecutive_errors}): {e}")

        # Auto-activation check
        if auto_activate_time and session_state == 'uploaded' and datetime.now() >= auto_activate_time:
            log.info("Auto-activation triggered")
            try:
                handle_activate(auto=True)
            except Exception as e:
                log.error(f"Auto-activate error: {e}")
                post_message(f"Auto-activation failed: {e}")

        # Heartbeat
        poll_count += 1
        if poll_count % HEARTBEAT_INTERVAL == 0:
            log.info(f"Heartbeat: {poll_count} polls, {len(processed)} processed, state={session_state}")

        # Backoff on errors
        if consecutive_errors > 5:
            wait = min(consecutive_errors * 30, 300)
            log.warning(f"Too many errors, backing off {wait}s")
            time.sleep(wait)
        else:
            time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        cleanup_lock()
    except Exception as e:
        log.critical(f"Fatal error: {e}")
        cleanup_lock()
        raise
