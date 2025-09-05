# Temperature Threshold Monitor → Microsoft Teams

This Python tool monitors a temperature reading from a PostgreSQL database and sends an alert to a Microsoft Teams channel (via an Azure Logic App workflow URL) whenever the temperature exceeds a configured threshold.  

It supports hysteresis to avoid repeated alerts while the value is fluctuating around the threshold, and includes a **test command** to verify that Teams notifications work.

---

## Features

- Polls a PostgreSQL table at regular intervals for the latest `(temperature, timestamp)` reading.
- Sends an **alert message** to Microsoft Teams when temperature crosses **above** the threshold.
- Optionally sends a **recovery message** when temperature goes back below threshold.
- Configurable poll frequency, SQL query, hysteresis, and payload format.
- **Auto-loads `.env`** for configuration (no manual `export` required).
- **Test command** to send a sample notification without touching the database.
- Works well in **Conda-based Python environments**.

---

## Requirements

- Python 3.8+ (Conda recommended)
- PostgreSQL access
- A Microsoft Teams Logic App workflow URL (manual trigger)

---

## Setup (Conda)

### 1️⃣ Clone or download the files
```bash
git clone <your_repo_url>
cd temperature_monitor
```

### 2️⃣ Create and activate a Conda environment
```bash
conda create -n temp_monitor python=3.11
conda activate temp_monitor
```

### 3️⃣ Install dependencies
```bash
pip install -r requirements.txt
```

### 4️⃣ Configure environment variables
Copy the example file and edit:
```bash
cp .env.example .env
nano .env
```

**Required:**
- `DATABASE_URL` → PostgreSQL connection string  
  Format: `postgresql://user:password@host:5432/dbname`
- `LOGIC_APP_URL` → Your Teams workflow (manual trigger) URL
- `THRESHOLD` → Temperature limit in °C (float)

**Optional:**
- `SQL_QUERY` → Must return `(temperature, timestamp)` in that order
- `POLL_SECONDS` → How often to check (default: 10s)
- `HYSTERESIS` → Margin below threshold before recovery alert is sent (default: 0.3°C)
- `ON_ABOVE_ONLY` → `true` to suppress recovery messages
- `SENSOR_NAME` → Name/label for the sensor in messages
- `TIMEZONE` → e.g. `Asia/Singapore` for timestamp formatting

---

## Running

**Normal monitoring loop** (polls DB, sends alerts):
```bash
python monitor.py
# or explicitly
python monitor.py run
```

**Send a test notification** (no DB access, good for verifying MS Teams integration):
```bash
python monitor.py test
# With custom message:
python monitor.py test --message "Hello from the lab monitor!"
```

**Use a custom `.env` file path**:
```bash
ENV_PATH=/path/to/config.env python monitor.py test --message "Using custom env"
```

---

## Example Output
```
[2025-08-11 14:00:00] INFO: Starting monitor (threshold=30.000°C, hysteresis=0.300°C, poll=10.0s)
[2025-08-11 14:05:10] INFO: Initial state: BELOW (temp=29.50°C at 2025-08-11T14:05:10+08:00)
[2025-08-11 14:07:15] INFO: Sent to Logic App: 🚨 Lab Sensor temperature is ABOVE threshold: 30.20°C (threshold 30.00°C) at 2025-08-11T14:07:15+08:00.
```

---

## Stopping
Press `CTRL + C` to stop the monitor.

---

## Notes

- Keep your **Logic App URL secret** — anyone with it can trigger your Teams message.
- If you run this on a server, consider using **systemd**, **pm2**, or **screen/tmux** to keep it running.
- Ensure your database query is optimized and returns only the latest reading.

---
