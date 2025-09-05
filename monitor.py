#!/usr/bin/env python3
"""
Temperature threshold monitor for PostgreSQL -> Microsoft Teams (via Logic App/Workflow URL).

- Polls a PostgreSQL table for the latest temperature.
- When it crosses the configured threshold (with optional hysteresis), sends a POST to your Logic App URL.
- Keeps state in-memory to avoid spamming while the value remains above threshold.
- Auto-loads environment variables from a local .env file (python-dotenv).

Usage:
  # Normal run (polling loop)
  python monitor.py

  # Explicit subcommands (equivalent to the above)
  python monitor.py run

  # Send a test notification without checking the database
  python monitor.py test --message "Hello from the monitor"

Environment variables (see .env.example):
  DATABASE_URL    : e.g. postgresql://user:pass@host:5432/dbname
  LOGIC_APP_URL   : Your Logic App manual trigger URL (keep secret)
  THRESHOLD       : Float, degrees. Example: 28.5
  HYSTERESIS      : Float, degrees below threshold to reset alert (default: 0.3). Set 0 to disable.
  POLL_SECONDS    : Integer/float, how often to check (default: 10)
  SQL_QUERY       : SQL returning exactly two columns: temperature (float), reading_ts (timestamp)
                    Default: SELECT temperature, reading_ts FROM sensor_readings ORDER BY reading_ts DESC LIMIT 1;
  SENSOR_NAME     : Optional label for messages
  VERIFY_TLS      : true/false for HTTPS verification on outgoing POST (default: true)
  TIMEZONE        : Optional tz name for formatting timestamps (default: system local)
  PAYLOAD_KEY     : JSON key for the message text sent to Logic App (default: "text")
  ON_ABOVE_ONLY   : If "true", only alert on crossing above threshold (no recovery message).
                    If "false" (default), send a recovery message when it goes back below (or under threshold - hysteresis).
  ENV_PATH        : Optional path to a .env file to load (overrides auto-detection)
"""
import os
import time
import json
import logging
import argparse
from typing import Tuple, Optional

# --- .env autoload ---
try:
    from dotenv import load_dotenv, find_dotenv
    def _load_env():
        env_path = os.getenv("ENV_PATH")
        if env_path and os.path.exists(env_path):
            load_dotenv(env_path, override=False)
        else:
            # find .env upward from cwd
            load_dotenv(find_dotenv(usecwd=True), override=False)
except Exception:
    def _load_env():
        pass  # python-dotenv not installed; continue without .env autoload

import psycopg2
import psycopg2.extras
import requests
from requests.exceptions import RequestException

try:
    import pytz  # optional
except Exception:
    pytz = None
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s"
)

def getenv_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")

def getenv_float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val is not None else default

def getenv_str(name: str, default: str) -> str:
    val = os.getenv(name)
    return val if val is not None else default

def get_tz(tzname: Optional[str]):
    if not tzname:
        return None
    if pytz is None:
        logging.warning("pytz not installed; TIMEZONE will be ignored.")
        return None
    try:
        return pytz.timezone(tzname)
    except Exception as e:
        logging.warning("Invalid TIMEZONE %r: %s", tzname, e)
        return None

def fetch_latest_temp(conn, sql_query: str) -> Optional[Tuple[float, Optional[datetime]]]:
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(sql_query)
        row = cur.fetchone()
        if not row:
            return None
        # Accept either index or column names
        try:
            temp = float(row[0])
        except Exception:
            temp = float(row["temperature"])
        try:
            ts = row[1]
        except Exception:
            ts = row.get("reading_ts") if hasattr(row, "get") else None
        return temp, ts

def format_ts(ts: Optional[datetime], tz) -> str:
    if ts is None:
        return "(no timestamp)"
    if tz and ts.tzinfo is None and pytz is not None:
        # Assume naive timestamp is UTC before converting
        ts = pytz.utc.localize(ts).astimezone(tz)
    elif tz:
        ts = ts.astimezone(tz)
    return ts.isoformat()

def build_message(sname: str, temp: float, threshold: float, ts_str: str, status: str) -> str:
    if status == "ALERT":
        return f"🚨 {sname or 'Sensor'} temperature is ABOVE threshold: {temp:.2f}°C (threshold {threshold:.2f}°C) at {ts_str}."
    else:
        return f"✅ {sname or 'Sensor'} temperature has RECOVERED: {temp:.2f}°C (threshold {threshold:.2f}°C) at {ts_str}."

def send_logic_app(url: str, message: str, payload_key: str, verify_tls: bool = True):
    # If your Logic App expects attachments for an adaptive card:
    payload = {
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "size": "Large", "weight": "Bolder", "text": "Temperature Monitor"},
                    {"type": "TextBlock", "text": message}
                ]
            }
        }]
    }
    requests.post(url, json=payload, timeout=15, verify=verify_tls).raise_for_status()


def run_monitor():
    _load_env()

    # Some variables are optional for test mode, but monitor requires DB URL
    db_url = os.environ["DATABASE_URL"]
    logic_app_url = os.environ["LOGIC_APP_URL"]
    threshold = getenv_float("THRESHOLD", 30.0)
    hysteresis = getenv_float("HYSTERESIS", 0.3)
    poll_seconds = getenv_float("POLL_SECONDS", 10.0)
    sql_query = getenv_str("SQL_QUERY", "SELECT temperature, reading_ts FROM sensor_readings ORDER BY reading_ts DESC LIMIT 1;")
    sensor_name = getenv_str("SENSOR_NAME", "")
    verify_tls = getenv_bool("VERIFY_TLS", True)
    payload_key = getenv_str("PAYLOAD_KEY", "text")
    on_above_only = getenv_bool("ON_ABOVE_ONLY", False)
    tz = get_tz(getenv_str("TIMEZONE", ""))

    logging.info("Starting monitor (threshold=%.3f°C, hysteresis=%.3f°C, poll=%.1fs)", threshold, hysteresis, poll_seconds)

    # State: None = unknown, False = normal/below, True = alert/above
    is_above = None

    # persistent connection with reconnect loop
    while True:
        try:
            with psycopg2.connect(db_url) as conn:
                conn.autocommit = True
                while True:
                    latest = fetch_latest_temp(conn, sql_query)
                    if latest is None:
                        logging.warning("No rows returned by SQL. Will retry.")
                    else:
                        temp, ts = latest
                        ts_str = format_ts(ts, tz)
                        currently_above = temp > threshold

                        if is_above is None:
                            is_above = currently_above
                            logging.info("Initial state: %s (temp=%.2f°C at %s)",
                                         "ABOVE" if is_above else "BELOW", temp, ts_str)

                        elif not is_above and currently_above:
                            # Crossed upward -> alert
                            msg = build_message(sensor_name, temp, threshold, ts_str, "ALERT")
                            send_logic_app(logic_app_url, msg, payload_key, verify_tls)
                            is_above = True

                        elif is_above and not currently_above:
                            # Require going below threshold - hysteresis to recover (if enabled)
                            if hysteresis > 0 and temp > (threshold - hysteresis):
                                pass  # in band; keep alerting state
                            else:
                                if not on_above_only:
                                    msg = build_message(sensor_name, temp, threshold, ts_str, "RECOVERED")
                                    send_logic_app(logic_app_url, msg, payload_key, verify_tls)
                                is_above = False

                    time.sleep(poll_seconds)
        except KeyboardInterrupt:
            logging.info("Shutting down by user request.")
            break
        except Exception as e:
            logging.error("DB connection loop error: %s", e)
            logging.info("Retrying DB connection in 5 seconds...")
            time.sleep(5)

def run_test(message: Optional[str] = None):
    """
    Sends a one-off test message to the Logic App without hitting the database.
    """
    _load_env()
    logic_app_url = os.environ["LOGIC_APP_URL"]
    verify_tls = getenv_bool("VERIFY_TLS", True)
    payload_key = getenv_str("PAYLOAD_KEY", "text")
    sensor_name = getenv_str("SENSOR_NAME", "")

    text = message or f"🔧 Test notification from {sensor_name or 'Temperature Monitor'} at {datetime.now().isoformat()}."
    send_logic_app(logic_app_url, text, payload_key, verify_tls)

def main():
    parser = argparse.ArgumentParser(description="Temperature threshold monitor to Microsoft Teams via Logic App")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("run", help="Run the monitoring loop (default)")
    tp = sub.add_parser("test", help="Send a test notification (no database access)")
    tp.add_argument("--message", "-m", type=str, help="Custom message text to send")

    args = parser.parse_args()

    if args.cmd in (None, "run"):
        run_monitor()
    elif args.cmd == "test":
        run_test(args.message)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
