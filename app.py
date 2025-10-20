# app.py
import os
import json
import logging
import re
from datetime import datetime, time, timedelta
from urllib.parse import urljoin

import pytz
import requests
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

# Load env
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TZ_NAME = os.getenv("TIMEZONE", "Asia/Kolkata")
MORNING_TIME = os.getenv("MORNING_TIME", "08:00")
EDIT_WINDOW_MINS = int(os.getenv("EDIT_WINDOW_MINS", "30"))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
HOSTNAME = os.getenv("HOSTNAME", "")  # must be full https:// url for webhook

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise RuntimeError("Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in .env")

TZ = pytz.timezone(TZ_NAME)

# Files
SCHEDULE_FILE = "schedule.json"
USER_STATE_FILE = "user_state.json"

# Initialize state file if missing
if not os.path.exists(USER_STATE_FILE):
    with open(USER_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f)

# Load schedule
with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
    schedule = json.load(f)

# Setup Flask
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Utilities
def load_state():
    with open(USER_STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state):
    with open(USER_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def send_message(text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    r = requests.post(url, data=payload, timeout=10)
    if not r.ok:
        logger.error("Telegram sendMessage failed: %s %s", r.status_code, r.text)
    return r

def format_morning_list(day_key):
    day_tasks = schedule.get(day_key, [])
    lines = []
    # Add header with nutrition targets
    meta = schedule.get("meta", {})
    nutrition = meta.get("protein_g")
    lines.append(f"Good morning — here's today's wellness plan ({day_key.capitalize()}):\n")
    for t in day_tasks:
        typ = t.get("type", "")
        if typ == "interval":
            if t.get("task", "").lower().startswith("hydration"):
                lines.append("💧 Hydration: every 2 hours (aim 2.5–3L/day)")
            elif "eyes" in t.get("task", "").lower():
                lines.append("👁️ Eyes: 20-20-20 breaks (3x)")
            else:
                lines.append(f"⏱️ {t.get('task')}")
        elif typ == "nutrition":
            lines.append(f"🍽️ Nutrition targets: {t.get('details')}")
        elif typ == "window":
            win = t.get("window", "")
            lines.append(f"🟡 Window {win}: {t.get('task')}")
        else:
            tm = t.get("time")
            if tm:
                lines.append(f"🕒 {tm} — {t.get('task')}")
            else:
                lines.append(f"⏱️ {t.get('task')}")
    return "\n".join(lines)

# Morning list send function
scheduler = BackgroundScheduler(timezone=TZ)
lock_jobs = {}  # track lock job ids per day_key

def send_morning_list_manual():
    """Helper to trigger morning list immediately (for testing)."""
    day_key = datetime.now(TZ).strftime("%A").lower()
    send_morning_list_for_day(day_key)

def send_morning_list_for_day(day_key):
    text = format_morning_list(day_key)
    # quick reply buttons as inline choices (note: these are not per-task granular)
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Accept", "callback_data": "accept"},
                {"text": "❌ Skip", "callback_data": "skip"}
            ],
            [
                {"text": "🔁 Swap", "callback_data": "swap"},
                {"text": "➕ Add", "callback_data": "add"}
            ]
        ]
    }
    send_message(text, reply_markup=keyboard)
    # initialize state
    state = load_state()
    state.setdefault(day_key, {})
    state[day_key]["accepted"] = False
    state[day_key]["locked"] = False
    state[day_key].setdefault("skips", [])
    state[day_key].setdefault("swaps", {})
    state[day_key].setdefault("adds", [])
    save_state(state)
    # schedule lock after EDIT_WINDOW_MINS
    lock_id = f"lock_{day_key}_{datetime.now(TZ).isoformat()}"
    def lock_job():
        s = load_state()
        s.setdefault(day_key, {})["locked"] = True
        save_state(s)
        send_message("Edit window closed. Today's schedule locked and reminders will continue.")
    job = scheduler.add_job(lock_job, 'date', run_date=datetime.now(TZ) + timedelta(minutes=EDIT_WINDOW_MINS))
    lock_jobs[day_key] = job

# Interval reminders
def hydration_reminder():
    send_message("💧 Hydration reminder — sip water now (aim 2.5–3L/day).")

def eyes_reminder():
    send_message("👁️ 20-20-20: look 20 ft away for 20 sec — give your eyes a break.")

def posture_reminder():
    send_message("🧍 Posture check — chin tuck & shoulder reset (30s).")

# Fixed-time tasks scheduling: schedule daily jobs for fixed tasks (they will check the weekday)
def schedule_fixed_tasks():
    for day_name, tasks in schedule.items():
        if day_name == "meta":
            continue
        for t in tasks:
            tm = t.get("time")
            typ = t.get("type")
            if tm and typ in ("fixed", "short"):
                try:
                    hh, mm = map(int, tm.split(":"))
                except Exception:
                    continue
                # add daily job at hh:mm that checks current weekday
                def make_sender(task_text, day=day_name):
                    def sender():
                        today = datetime.now(TZ).strftime("%A").lower()
                        if today != day:
                            return
                        # check user_state skips and swaps
                        s = load_state().get(today, {})
                        skips = s.get("skips", [])
                        swaps = s.get("swaps", {})
                        # if any skip substring matches, skip
                        for sk in skips:
                            if sk.lower() in task_text.lower():
                                logger.info("Skipping task due to user skip: %s", task_text)
                                return
                        # apply swap if present
                        for orig, new in swaps.items():
                            if orig.lower() in task_text.lower():
                                send_message(f"Reminder (swapped): {new}")
                                return
                        send_message(f"Reminder: {task_text}")
                    return sender
                scheduler.add_job(make_sender(t.get("task")), trigger=CronTrigger(hour=hh, minute=mm))

# Setup daily jobs
def setup_daily_jobs():
    # Morning list at MORNING_TIME
    hh, mm = map(int, schedule.get("meta", {}).get("morning_time", MORNING_TIME).split(":"))
    scheduler.add_job(lambda: send_morning_list_for_day(datetime.now(TZ).strftime("%A").lower()),
                      trigger=CronTrigger(hour=hh, minute=mm))
    # Hydration: schedule times 9,11,13,15,17,19,21,23 local time
    for h in [9,11,13,15,17,19,21,23]:
        scheduler.add_job(hydration_reminder, trigger=CronTrigger(hour=h, minute=0))
    # Eyes reminders at 10,14,18
    for h in [10,14,18]:
        scheduler.add_job(eyes_reminder, trigger=CronTrigger(hour=h, minute=0))
    # Posture hourly 9-21
    for h in range(9,22):
        scheduler.add_job(posture_reminder, trigger=CronTrigger(hour=h, minute=0))
    # Fixed tasks
    schedule_fixed_tasks()

# Parse user text commands
def handle_user_command(text):
    text = text.strip()
    today_key = datetime.now(TZ).strftime("%A").lower()
    state = load_state()
    state.setdefault(today_key, {})
    # accept
    if text.lower() == "accept":
        state[today_key]["accepted"] = True
        state[today_key]["locked"] = True
        save_state(state)
        return "Accepted. Today's schedule locked."
    # skip <task>
    if text.lower().startswith("skip "):
        task = text[5:].strip()
        state.setdefault(today_key, {}).setdefault("skips", []).append(task)
        save_state(state)
        return f"Marked to skip: {task}"
    # swap <task> with <new task>
    m = re.match(r"swap (.+) with (.+)", text, flags=re.I)
    if m:
        a = m.group(1).strip()
        b = m.group(2).strip()
        state.setdefault(today_key, {}).setdefault("swaps", {})[a] = b
        save_state(state)
        return f"Swapped: {a} → {b}"
    # add <task> at HH:MM
    m2 = re.match(r"add (.+) at ([0-2]?\d:[0-5]\d)", text, flags=re.I)
    if m2:
        task = m2.group(1).strip()
        ttime = m2.group(2).strip()
        state.setdefault(today_key, {}).setdefault("adds", []).append({"task": task, "time": ttime})
        save_state(state)
        return f"Added: {task} at {ttime}"
    return "Sorry, I didn't understand. Use 'accept', 'skip <task>', 'swap <task> with <new task>', or 'add <task> at HH:MM'."

# Flask route for Telegram webhook
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    # handle message
    if "message" in data:
        msg = data["message"]
        chat_id = str(msg.get("chat", {}).get("id", ""))
        # Only react if sender is the authorized chat id (single-user)
        if chat_id != str(CHAT_ID):
            return jsonify(ok=True)
        text = msg.get("text", "")
        if not text:
            return jsonify(ok=True)
        # handle commands via text
        if text.strip().lower() == "/start":
            send_message("Wellness bot active. You'll receive the morning list daily at configured time.")
            return jsonify(ok=True)
        # handle text actions
        response = handle_user_command(text)
        send_message(response)
    # handle callback_query (inline keyboard presses)
    if "callback_query" in data:
        cq = data["callback_query"]
        chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        if chat_id != str(CHAT_ID):
            return jsonify(ok=True)
        data_payload = cq.get("data", "")
        if data_payload == "accept":
            r = handle_user_command("accept")
            # answer callback by editing message text or sending reply
            send_message(r)
        else:
            send_message("Press /help for examples of skip/swap/add commands.")
    return jsonify(ok=True)

# Small endpoint to trigger morning list manually for testing
@app.route("/trigger_morning", methods=["POST","GET"])
def trigger_morning():
    send_morning_list_manual()
    return "Triggered morning list", 200

# Endpoint to set Telegram webhook (call this once after starting with host configured)
@app.route("/set_webhook", methods=["POST"])
def set_webhook():
    if not HOSTNAME:
        return "HOSTNAME not set in .env", 400
    webhook_url = urljoin(HOSTNAME.rstrip("/"), WEBHOOK_PATH.lstrip("/"))
    set_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    r = requests.post(set_url, data={"url": webhook_url}, timeout=10)
    return jsonify(status=r.json())

if __name__ == "__main__":
    # Schedule jobs and start
    setup_daily_jobs()
    scheduler.start()
    # Flask runs on 0.0.0.0:5000 by default
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
