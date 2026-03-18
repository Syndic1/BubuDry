"""
BubuDry - Smart Tumble Dryer Dashboard
Full-featured web dashboard for a Hoover HLE C10TG via the hOn ecosystem.

Features:
  - Periodic polling of dryer state via hOn cloud API (every 30s by default)
  - Progress bar with time remaining
  - Programme name, dry level, cycle phase
  - Door / water tank / filter alerts
  - Remote controls: start, stop, pause, dry level
  - Cycle history and energy statistics
  - Accessible dark-mode UI for elderly users (large text, high contrast)

Usage:
    HON_EMAIL=your@email.com HON_PASSWORD=yourpassword python app.py
"""

import asyncio
import logging
import logging.handlers
import os
import threading
import time
from datetime import datetime
from pathlib import Path

# Load .env file if present (keeps credentials out of the environment/shell history)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed — fall back to env vars set manually

from flask import Flask, jsonify, render_template_string, request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HON_EMAIL = os.environ.get("HON_EMAIL", "")
HON_PASSWORD = os.environ.get("HON_PASSWORD", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "120"))
FLASK_PORT = int(os.environ.get("FLASK_PORT", "5000"))
STATIC_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# Logging — two outputs:
#   Console : friendly, human-readable, only meaningful events
#   File    : full diagnostic detail (bubudry.log), everything including HTTP
# ---------------------------------------------------------------------------

LOG_FILE = Path(__file__).parent / "bubudry.log"

# Console handler — clean, friendly format
_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))

# File handler — full detail for diagnostics
_file = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
)
_file.setLevel(logging.DEBUG)
_file.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(threadName)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))

# Root logger — send everything to file, INFO+ to console
logging.basicConfig(level=logging.DEBUG, handlers=[_console, _file])
log = logging.getLogger(__name__)

# Silence Werkzeug's per-request log lines on the console (still written to file)
logging.getLogger("werkzeug").addHandler(_file)
logging.getLogger("werkzeug").propagate = False

# ---------------------------------------------------------------------------
# Shared state — written by the polling/MQTT thread, read by Flask
# ---------------------------------------------------------------------------

state = {
    # Core status
    "status": "unknown",
    "status_label": "Starting up...",
    "programme": None,
    "dry_level": None,
    "phase": None,
    # Time
    "remaining_minutes": None,
    "total_minutes": None,
    "cycle_started": None,
    # Hardware alerts
    "door_open": False,
    "water_tank_full": False,
    "filter_dirty": False,
    "error": None,
    # Connection
    "connected": False,
    "dryer_online": False,
    "last_updated": None,
    # Tumbling
    "tumbling": False,
    "paused": False,
    # Anti-crease
    "anti_crease": False,
    # History & stats (populated on first load)
    "history": [],
    "statistics": {},
    # Available programmes for remote start
    "programmes": [],
}
state_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Machine state mapping
# ---------------------------------------------------------------------------

MACH_MODE_MAP = {
    0: ("off",      "Off"),
    1: ("running",  "Running"),
    2: ("running",  "Drying"),
    3: ("paused",   "Paused"),
    4: ("finished", "Finished"),
    5: ("finished", "Cycle Complete"),
    6: ("running",  "Delayed Start"),
    7: ("running",  "Running"),
}

PHASE_MAP = {
    0: "Idle",
    1: "Heating",
    2: "Drying",
    3: "Cooling Down",
    4: "Anti-crease Tumbling",
}

DRY_LEVEL_MAP = {
    1: "Iron Dry",
    2: "Hang Dry",
    3: "Cupboard Dry",
    4: "Extra Dry",
    11: "Timed",
}

PROGRAMME_MAP = {
    "all_in_one": "All in One",
    "cotton": "Cotton",
    "synthetics": "Synthetics",
    "mixed": "Mixed",
    "shirts": "Shirts",
    "wool": "Wool",
    "sport": "Sport",
    "duvet": "Duvet",
    "delicates": "Delicates",
    "rapid": "Rapid 30'",
    "refresh": "Refresh",
    "baby": "Baby Care",
    "hygiene": "Hygiene+",
    "xpress_dry_30": "Express Dry 30'",
    "xpress_dry_45": "Express Dry 45'",
    "xpress_dry_59": "Express Dry 59'",
}

# ---------------------------------------------------------------------------
# Attribute helpers
# ---------------------------------------------------------------------------

def _get_attr(appliance, key, default=None):
    """Read a parameter value from a pyhOn appliance object."""
    attr = appliance.attributes.get("parameters", {}).get(key)
    if attr is None:
        return default
    return getattr(attr, "value", attr)


def _safe_int(val, default=0):
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def parse_dryer_state(appliance) -> dict:
    """Extract all useful status from a pyhOn appliance into a flat dict."""
    mode_int = _safe_int(_get_attr(appliance, "machMode", 0))

    # Phase tells us what the machine is *actually* doing right now
    phase_int = _safe_int(_get_attr(appliance, "prPhase", 0))
    phase_label = PHASE_MAP.get(phase_int, f"Phase {phase_int}")

    # Connection status — needed early to determine "active" vs "off"
    last_conn = appliance.attributes.get("lastConnEvent", {})
    dryer_online = False
    if isinstance(last_conn, dict):
        dryer_online = last_conn.get("category", "").upper() == "CONNECTED"

    # Determine status from machMode + prPhase combined:
    #   "running"  = machine is mid-cycle and actively doing something (heating/drying/cooling)
    #   "active"   = machine is reachable/on but not currently processing ("Ready")
    #   "off"      = machine is not reachable at all
    #   "paused", "finished", "error" as before
    raw_key, raw_label = MACH_MODE_MAP.get(mode_int, ("unknown", f"Mode {mode_int}"))

    if raw_key == "running":
        if phase_int > 0:
            # Actually doing something — use the phase as the label
            status_key = "running"
            status_label = phase_label  # e.g. "Heating", "Drying", "Cooling Down"
        else:
            # machMode says running but nothing is happening yet (pre-start / idle)
            status_key = "active"
            status_label = "Ready"
    elif raw_key == "off":
        if dryer_online:
            # Wi-Fi module alive, machine in standby
            status_key = "active"
            status_label = "Ready"
        else:
            status_key = "off"
            status_label = "Off"
    else:
        status_key = raw_key
        status_label = raw_label

    # Programme name — try commandHistory first (has the human-readable prStr),
    # then fall back to the programName attribute
    prog_display = None
    cmd_hist = appliance.attributes.get("commandHistory", {})
    if isinstance(cmd_hist, dict):
        cmd_data = cmd_hist.get("command", {})
        pr_str = cmd_data.get("attributes", {}).get("prStr", "")
        if pr_str:
            prog_display = pr_str
    if not prog_display:
        prog_name = appliance.attributes.get("programName", "") or ""
        if prog_name and prog_name.lower() != "no program":
            prog_display = PROGRAMME_MAP.get(prog_name.lower().replace(" ", "_"),
                                             prog_name.replace("_", " ").title())

    # Dry level
    dry_level_int = _safe_int(_get_attr(appliance, "dryLevel", 0))
    dry_level_label = DRY_LEVEL_MAP.get(dry_level_int, f"Level {dry_level_int}")

    # Remaining & total time
    remaining = _safe_int(_get_attr(appliance, "remainingTimeMM", 0))
    activity = appliance.attributes.get("activity", {})
    activity_attrs = activity.get("attributes", {}) if isinstance(activity, dict) else {}
    total = _safe_int(activity_attrs.get("dryTimeMM", 0))
    cycle_started = None
    if isinstance(activity, dict):
        cycle_started = activity.get("activityExecutionStarted", None)

    # Hardware alerts
    # doorStatus may live under parameters or at the top-level of attributes
    door_raw = _get_attr(appliance, "doorStatus")
    if door_raw is None:
        door_raw = appliance.attributes.get("doorStatus", "0")
    door_open = str(door_raw) not in ("0", "", "None", "false", "False")
    log.debug("doorStatus raw=%r -> open=%s", door_raw, door_open)
    water_tank = str(_get_attr(appliance, "waterTankStatus", "0")) != "0"
    dry_filter = str(_get_attr(appliance, "dryFilterStatus", "0")) != "0"
    tumbling = str(_get_attr(appliance, "tumblingStatus", "0")) != "0"
    anti_crease = str(_get_attr(appliance, "anticrease", "0")) != "0"
    paused = getattr(appliance, "pause", False)

    # Generic errors
    error_msg = None
    err_val = _get_attr(appliance, "errors", "0")
    if str(err_val) not in ("0", "", "false", "False"):
        error_msg = f"Error code: {err_val}"
        status_key = "error"
        status_label = "Error"

    # Priority alert override — collect all, don't let one hide the other
    if water_tank or dry_filter:
        status_key = "error"
        status_label = "Attention Needed"
        alerts = []
        if water_tank:
            alerts.append("Water tank is full — please empty it")
        if dry_filter:
            alerts.append("Filter needs cleaning")
        error_msg = " | ".join(alerts)

    # If paused, override
    if paused and status_key in ("running", "active"):
        status_key = "paused"
        status_label = "Paused"

    return {
        "status": status_key,
        "status_label": status_label,
        "programme": prog_display if (prog_display and status_key in ("running", "paused", "error")) else None,
        "dry_level": dry_level_label if (dry_level_int > 0 and status_key in ("running", "paused", "error")) else None,
        "phase": phase_label if status_key in ("running", "paused") else None,
        # Only show time when the dryer is actively doing something — otherwise it's stale
        "remaining_minutes": remaining if (remaining > 0 and status_key in ("running", "paused")) else None,
        "total_minutes": total if (total > 0 and status_key in ("running", "paused")) else None,
        "cycle_started": cycle_started,
        "door_open": door_open,
        "water_tank_full": water_tank,
        "filter_dirty": dry_filter,
        "error": error_msg,
        "connected": True,
        "dryer_online": dryer_online,
        "tumbling": tumbling,
        "paused": paused,
        "anti_crease": anti_crease,
        "last_updated": datetime.now().strftime("%H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# hOn connection — persistent, with MQTT real-time updates
# ---------------------------------------------------------------------------

# We keep the Hon instance alive globally so MQTT stays connected
_hon = None
_hon_lock = threading.Lock()


async def _get_dryer():
    """Return (Hon instance, dryer appliance) — creating/reconnecting as needed.

    Uses _hon_lock to prevent concurrent access from the polling thread and
    Flask command requests stepping on each other.
    """
    global _hon
    from pyhon import Hon

    with _hon_lock:
        if _hon is None:
            log.info("Connecting to hOn...")
            _hon = Hon(HON_EMAIL, HON_PASSWORD)
            await _hon.create()
            await _hon.setup()
            appliance_names = [
                f"{a.nick_name or a.model_name or a.appliance_type}"
                for a in _hon.appliances
            ]
            log.info("Connected to hOn. Appliances: %s", ", ".join(appliance_names))
        # No token refresh here — pyhOn handles it internally when needed.
        # Calling auth.refresh() on every poll was an unnecessary extra API call.

        dryer = next(
            (a for a in _hon.appliances if a.appliance_type == "TD"),
            None,
        )
        return _hon, dryer


async def fetch_quick_state() -> dict | None:
    """Fast poll — just refresh attributes (1 API call)."""
    hon, dryer = await _get_dryer()

    if dryer is None:
        types = [a.appliance_type for a in hon.appliances]
        log.warning("No tumble dryer found. Appliances: %s", types)
        return None

    await dryer.load_attributes()
    return parse_dryer_state(dryer)


async def fetch_slow_extras() -> dict:
    """Slow poll — stats, history, programmes (3 API calls). Called less often."""
    hon, dryer = await _get_dryer()
    extras = {}

    if dryer is None:
        return extras

    # Statistics & maintenance
    try:
        stats_raw = await hon.api.load_statistics(dryer)
        if isinstance(stats_raw, dict):
            extras["statistics"] = {
                "total_cycles": _safe_int(stats_raw.get("programsCounter", 0)),
                "most_used": stats_raw.get("mostUsedPrograms", []),
            }
        maint_raw = await hon.api.load_maintenance(dryer)
        if isinstance(maint_raw, dict):
            extras.setdefault("statistics", {})
            extras["statistics"]["filter_cleaning"] = maint_raw.get("filterCleaning", {})
            extras["statistics"]["last_checkup"] = maint_raw.get("lastCheckup", {})
    except Exception as e:
        log.warning("Could not load statistics: %s", e)

    # Command history (last 10 cycles)
    try:
        hist_raw = await hon.api.load_command_history(dryer)
        history = []
        entries = hist_raw if isinstance(hist_raw, list) else []
        for entry in entries[:10]:
            cmd = entry.get("command", {})
            params = cmd.get("parameters", {})
            ts = entry.get("timestampAccepted", "") or cmd.get("timestamp", "")
            history.append({
                "programme": cmd.get("programName", "").split(".")[-1].replace("_", " ").title(),
                "date": ts[:16].replace("T", " "),
                "duration": _safe_int(params.get("dryTimeMM", 0)),
                "dry_level": DRY_LEVEL_MAP.get(_safe_int(params.get("dryLevel", 0)), ""),
            })
        extras["history"] = history
    except Exception as e:
        log.warning("Could not load history: %s", e)

    # Available programmes for remote start
    try:
        if "startProgram" in dryer.commands:
            start_cmd = dryer.commands["startProgram"]
            prog_names = []
            if hasattr(start_cmd, "categories") and start_cmd.categories:
                for cat_name in start_cmd.categories:
                    display = PROGRAMME_MAP.get(cat_name.lower(), cat_name.replace("_", " ").title())
                    prog_names.append({"id": cat_name, "name": display})
            extras["programmes"] = prog_names
    except Exception as e:
        log.warning("Could not load programmes: %s", e)

    return extras


async def send_dryer_command(command_name: str, programme: str = None, dry_level: int = None) -> dict:
    """Send a command (stopProgram, pauseProgram, startProgram) to the dryer."""
    hon, dryer = await _get_dryer()
    if dryer is None:
        return {"ok": False, "error": "No dryer found"}

    if command_name not in dryer.commands:
        available = list(dryer.commands.keys())
        return {"ok": False, "error": f"Command '{command_name}' not available. Available: {available}"}

    cmd = dryer.commands[command_name]

    if command_name == "startProgram":
        if programme:
            if hasattr(cmd, "categories") and cmd.categories:
                if programme in cmd.categories:
                    cmd.category = programme
                    log.debug("Set programme category: %s", programme)
                else:
                    log.warning("Programme '%s' not in available categories: %s", programme, list(cmd.categories.keys()) if isinstance(cmd.categories, dict) else list(cmd.categories))
            else:
                log.warning("startProgram has no categories — programme '%s' ignored", programme)
        else:
            log.warning("startProgram called with no programme — dryer may ignore it")

    # Set dry level if provided (1=Iron, 2=Hang, 3=Cupboard, 4=Extra)
    if dry_level is not None and command_name == "startProgram":
        try:
            params = cmd.parameters or {}
            if "dryLevel" in params:
                params["dryLevel"].value = str(dry_level)
        except Exception as e:
            log.warning("Could not set dry level: %s", e)

    try:
        await cmd.send()
        extras = []
        if programme: extras.append(programme)
        if dry_level: extras.append(DRY_LEVEL_MAP.get(dry_level, f"level {dry_level}"))
        log.info("Command sent: %s%s", command_name, f" ({', '.join(extras)})" if extras else "")
        return {"ok": True}
    except Exception as e:
        log.error("Command failed (%s): %s", command_name, e)
        return {"ok": False, "error": str(e)}


SLOW_POLL_INTERVAL = int(os.environ.get("SLOW_POLL_INTERVAL", "600"))   # 10 min
# Heartbeat poll — just confirms we're still talking to hOn, not fetching all data
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "300"))  # 5 min

# The persistent event loop — created once, never destroyed.
_event_loop: asyncio.AbstractEventLoop | None = None


def _run(coro):
    """Run a coroutine on the persistent event loop from any thread."""
    return asyncio.run_coroutine_threadsafe(coro, _event_loop).result()


def _apply_state_update(result: dict, last_reported_status: str | None) -> str | None:
    """Write result into shared state and log if status changed. Returns new last_reported_status."""
    with state_lock:
        state.update(result)
    new_status = result["status_label"]
    if new_status != last_reported_status:
        prog = result.get("programme")
        mins = result.get("remaining_minutes")
        time_str = f" — {mins} min remaining" if mins else ""
        prog_str = f" ({prog})" if prog else ""
        if result.get("error"):
            log.warning("Dryer: %s — %s", new_status, result["error"])
        else:
            log.info("Dryer: %s%s%s", new_status, prog_str, time_str)
    return new_status


def _setup_push_updates(hon, dryer) -> bool:
    """Register for real-time MQTT push updates from the dryer.

    pyhOn's MQTT client automatically subscribes to haier/things/{mac}/event/appliancestatus/update.
    When a message arrives, pyhOn updates dryer.attributes in-place, then calls hon.notify().
    We hook into that via hon.subscribe_updates(fn).  The callback is called from the MQTT
    thread with a single None argument, so it must be synchronous (no await).
    """
    if not hasattr(hon, "subscribe_updates"):
        log.info("Polling mode active (push updates not supported by this pyhOn version)")
        return False

    def on_push(_):
        """Called by pyhOn when the dryer pushes a status change over MQTT."""
        result = parse_dryer_state(dryer)
        with state_lock:
            # Preserve slow-poll data (history/stats) that parse_dryer_state doesn't touch
            result.setdefault("history", state.get("history", []))
            result.setdefault("statistics", state.get("statistics", {}))
            result.setdefault("programmes", state.get("programmes", []))
            state.update(result)
        log.info("Dryer: %s (push update)", result["status_label"])

    hon.subscribe_updates(on_push)
    log.info("Live push updates active -- polling reduced to heartbeat")
    return True




async def _polling_async():
    """Async polling loop — runs forever inside the persistent event loop.

    On first connect, we try to register for MQTT push updates.
    If that works, polling drops to a heartbeat (just checking we're still alive).
    If push updates aren't available, we fall back to polling as before.
    """
    global _hon
    consecutive_failures = 0
    last_slow_poll = 0
    last_reported_status = None
    push_active = False  # True once MQTT callbacks are registered

    while True:
        try:
            do_slow = (time.time() - last_slow_poll >= SLOW_POLL_INTERVAL)

            result = await fetch_quick_state()
            if result:
                consecutive_failures = 0

                # First successful connect — register for MQTT push updates
                if not push_active:
                    hon, dryer = await _get_dryer()
                    if dryer:
                        push_active = _setup_push_updates(hon, dryer)

                if do_slow:
                    try:
                        extras = await fetch_slow_extras()
                        result.update(extras)
                        last_slow_poll = time.time()
                    except Exception as e:
                        log.debug("Slow poll failed (non-critical): %s", e)

                last_reported_status = _apply_state_update(result, last_reported_status)

            else:
                if last_reported_status != "not found":
                    log.warning("Dryer not found on hOn account")
                    last_reported_status = "not found"
                with state_lock:
                    state["connected"] = False
                    state["status"] = "unknown"
                    state["status_label"] = "Dryer Not Found"
                    state["last_updated"] = datetime.now().strftime("%H:%M:%S")

            # If push updates are active, we only need a heartbeat to confirm
            # we're still connected — the dryer tells us everything else itself
            sleep_for = HEARTBEAT_INTERVAL if push_active else POLL_INTERVAL
            await asyncio.sleep(sleep_for)

        except Exception as exc:
            consecutive_failures += 1
            push_active = False  # re-register callbacks after reconnect

            if consecutive_failures == 1:
                log.warning("Lost connection to hOn — retrying...")
            elif consecutive_failures == 2:
                with _hon_lock:
                    _hon = None
                log.warning("Still offline — reconnecting from scratch")
            else:
                log.debug("Reconnect attempt %d failed: %s", consecutive_failures, exc)

            last_reported_status = None
            with state_lock:
                state["connected"] = False
                state["status"] = "unknown"
                state["status_label"] = "Reconnecting..."
                state["error"] = "Connection lost — retrying..."
                state["last_updated"] = datetime.now().strftime("%H:%M:%S")

            retry_wait = min(30, POLL_INTERVAL // 2) if consecutive_failures == 1 else POLL_INTERVAL
            await asyncio.sleep(retry_wait)


def polling_loop():
    """Spin up the persistent event loop and run the async poller inside it forever."""
    global _event_loop
    log.info("BubuDry starting up")
    _event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_event_loop)
    _event_loop.run_until_complete(_polling_async())


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")


@app.route("/api/state")
def api_state():
    """JSON endpoint for AJAX polling from the dashboard."""
    with state_lock:
        return jsonify(dict(state))


@app.route("/api/command", methods=["POST"])
def api_command():
    """Send a command to the dryer. Body: {"command": "stopProgram"} etc."""
    data = request.get_json(force=True)
    cmd = data.get("command", "")
    prog = data.get("programme", None)
    dry_level = data.get("dryLevel", None)
    if not cmd:
        return jsonify({"ok": False, "error": "No command specified"}), 400
    result = _run(send_dryer_command(cmd, prog, dry_level))
    return jsonify(result)


# ---------------------------------------------------------------------------
# HTML Dashboard — single-page, accessible, auto-updating via fetch()
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BubuDry - Laundry Status</title>
  <style>
    :root {
      --green: #2ecc71; --blue: #3498db; --red: #e74c3c;
      --orange: #f39c12; --grey: #7f8c8d;
      --bg: #1a1a2e; --bg2: #16213e; --card: #1e2a47;
      --card-hover: #253356; --text: #e8e8e8; --text-dim: #8892a8;
      --accent: #4fc3f7; --border: #2a3a5c;
      --radius: 20px; --shadow: 0 8px 32px rgba(0,0,0,0.3);
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      padding: 20px;
    }

    /* ---- Layout ---- */
    .container { max-width: 900px; margin: 0 auto; }

    h1 {
      text-align: center;
      font-size: 2.6rem;
      margin-bottom: 8px;
      color: var(--accent);
    }
    .subtitle {
      text-align: center;
      font-size: 1.15rem;
      color: var(--text-dim);
      margin-bottom: 28px;
    }

    /* ---- Main hero card ---- */
    .hero {
      background: var(--card);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      border: 1px solid var(--border);
      padding: 40px 30px 30px;
      text-align: center;
      margin-bottom: 20px;
    }

    /* Bubu & Dudu flanking the dryer */
    .mascots {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 24px;
      margin-bottom: 24px;
    }
    .mascots img {
      width: 110px; height: 110px;
      border-radius: 50%;
      border: 3px solid var(--border);
    }

    /* ---- SVG Dryer ---- */
    .dryer-wrap {
      width: 180px; height: 180px;
      position: relative; flex-shrink: 0;
    }
    .dryer-wrap svg { width: 100%; height: 100%; }
    @keyframes tumble {
      0%   { transform: rotate(0deg); }
      100% { transform: rotate(360deg); }
    }
    .drum-spin {
      transform-origin: 90px 100px;
      animation: tumble 3s linear infinite;
      animation-play-state: paused;
    }
    .is-running .drum-spin { animation-play-state: running; }
    .is-paused .drum-spin  { animation-play-state: paused; }

    /* ---- Status badge ---- */
    .status-badge {
      display: inline-block;
      font-size: 2.2rem; font-weight: 700;
      padding: 14px 44px;
      border-radius: 60px;
      color: white;
      margin: 16px 0 8px;
      letter-spacing: 0.02em;
      transition: background 0.4s;
    }
    .status-running  { background: var(--green); }
    .status-active   { background: #0097a7; }  /* teal — "Ready" */
    .status-finished { background: var(--blue); }
    .status-off      { background: #5a6270; }
    .status-error    { background: var(--red); }
    .status-paused   { background: var(--orange); }
    .status-unknown  { background: #4a5568; }

    /* ---- Programme / phase info ---- */
    .info-line { font-size: 1.3rem; color: var(--text-dim); margin: 6px 0; }
    .info-line strong { color: var(--text); }

    /* ---- Progress bar ---- */
    .progress-section { margin: 24px 0 16px; }
    .progress-bar-bg {
      background: #2a3a5c;
      border-radius: 14px; height: 28px;
      width: 100%; overflow: hidden;
    }
    .progress-bar-fill {
      height: 100%; border-radius: 14px;
      background: linear-gradient(90deg, var(--green), var(--accent));
      transition: width 1s ease;
    }
    .progress-text { font-size: 1.15rem; color: var(--text-dim); margin-top: 8px; }
    .time-big {
      font-size: 3.2rem; font-weight: 700;
      color: var(--accent); line-height: 1.1; margin: 8px 0 2px;
    }
    .time-label { font-size: 1.1rem; color: var(--text-dim); }

    /* ---- Alert banners ---- */
    .alerts { margin: 16px 0; }
    .alert {
      font-size: 1.25rem; padding: 14px 20px;
      border-radius: 14px; margin-bottom: 10px;
      display: flex; align-items: center; gap: 12px; font-weight: 600;
    }
    .alert-icon { font-size: 1.6rem; }
    .alert-error   { background: rgba(231,76,60,0.15); color: #ff6b6b; border: 2px solid rgba(231,76,60,0.4); }
    .alert-warning  { background: rgba(243,156,18,0.15); color: #ffc048; border: 2px solid rgba(243,156,18,0.4); }
    .alert-info     { background: rgba(52,152,219,0.15); color: var(--accent); border: 2px solid rgba(52,152,219,0.4); }

    /* ---- Controls ---- */
    .controls {
      display: flex; justify-content: center;
      gap: 14px; flex-wrap: wrap; margin: 20px 0 10px;
    }
    .ctrl-btn {
      font-size: 1.2rem; font-weight: 700;
      padding: 14px 32px; border: none;
      border-radius: 50px; cursor: pointer; color: white;
      transition: transform 0.15s, opacity 0.2s, box-shadow 0.2s;
      min-width: 130px;
    }
    .ctrl-btn:hover { transform: scale(1.04); box-shadow: 0 4px 20px rgba(0,0,0,0.4); }
    .ctrl-btn:active { transform: scale(0.97); }
    .ctrl-btn:disabled { opacity: 0.35; cursor: not-allowed; transform: none; box-shadow: none; }
    .btn-stop   { background: var(--red); }
    .btn-pause  { background: var(--orange); }
    .btn-start  { background: var(--green); }
    .btn-dry    { background: #7c4dff; min-width: auto; padding: 14px 24px; font-size: 1.05rem; }
    .btn-prog   { background: #00838f; min-width: auto; padding: 14px 24px; font-size: 1.05rem; }

    /* ---- Dry level picker popover ---- */
    .dry-picker {
      position: relative; display: inline-block;
    }
    .dry-menu {
      display: none; position: absolute; bottom: 110%; left: 50%;
      transform: translateX(-50%);
      background: var(--card); border: 1px solid var(--border);
      border-radius: 14px; padding: 8px; min-width: 180px;
      box-shadow: 0 8px 30px rgba(0,0,0,0.5); z-index: 10;
    }
    .dry-menu.open { display: block; }
    .dry-option {
      display: block; width: 100%; text-align: left;
      padding: 12px 16px; background: none; border: none;
      color: var(--text); font-size: 1.1rem; cursor: pointer;
      border-radius: 10px; font-weight: 600;
    }
    .dry-option:hover { background: var(--card-hover); }
    .dry-option.active { color: var(--accent); background: rgba(79,195,247,0.1); }

    /* ---- Tabs ---- */
    .tabs-wrap {
      background: var(--card);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      border: 1px solid var(--border);
      overflow: hidden;
      margin-bottom: 20px;
    }
    .tab-bar {
      display: flex; border-bottom: 2px solid var(--border);
    }
    .tab-btn {
      flex: 1; padding: 16px 12px;
      background: none; border: none; color: var(--text-dim);
      font-size: 1.15rem; font-weight: 600; cursor: pointer;
      transition: color 0.2s, background 0.2s;
      border-bottom: 3px solid transparent;
      margin-bottom: -2px;
    }
    .tab-btn:hover { color: var(--text); background: rgba(255,255,255,0.03); }
    .tab-btn.active {
      color: var(--accent);
      border-bottom-color: var(--accent);
      background: rgba(79,195,247,0.05);
    }
    .tab-panel { display: none; padding: 24px; }
    .tab-panel.active { display: block; }

    /* ---- Stat rows inside tabs ---- */
    .stat-row {
      display: flex; justify-content: space-between;
      padding: 10px 0; font-size: 1.15rem;
      border-bottom: 1px solid var(--border);
    }
    .stat-row:last-child { border-bottom: none; }
    .stat-label { color: var(--text-dim); }
    .stat-value { font-weight: 600; color: var(--text); }

    /* ---- History table ---- */
    .history-table { width: 100%; border-collapse: collapse; font-size: 1.1rem; }
    .history-table th {
      text-align: left; padding: 10px 8px;
      border-bottom: 2px solid var(--border);
      color: var(--text-dim); font-weight: 600;
    }
    .history-table td {
      padding: 10px 8px;
      border-bottom: 1px solid var(--border);
      color: var(--text);
    }
    .history-table tr:last-child td { border-bottom: none; }

    /* ---- Section headers inside tabs ---- */
    .tab-section-title {
      font-size: 1.25rem; font-weight: 700;
      color: var(--accent); margin: 0 0 16px 0;
      padding-bottom: 8px;
      border-bottom: 2px solid var(--border);
    }
    .tab-section-title:not(:first-child) { margin-top: 24px; }

    /* ---- Footer ---- */
    .footer {
      text-align: center; color: #556;
      font-size: 0.95rem; margin-top: 20px; padding-bottom: 20px;
    }

    /* ---- Connection indicator ---- */
    .conn-dot {
      display: inline-block; width: 12px; height: 12px;
      border-radius: 50%; margin-right: 6px; vertical-align: middle;
    }
    .conn-dot.online  { background: var(--green); box-shadow: 0 0 8px var(--green); }
    .conn-dot.offline { background: var(--red); box-shadow: 0 0 8px var(--red); }

    .hidden { display: none !important; }

    /* ---- Responsive ---- */
    @media (max-width: 600px) {
      h1 { font-size: 2rem; }
      .mascots img { width: 80px; height: 80px; }
      .dryer-wrap { width: 140px; height: 140px; }
      .status-badge { font-size: 1.6rem; padding: 12px 28px; }
      .time-big { font-size: 2.4rem; }
      .tab-btn { font-size: 1rem; padding: 12px 8px; }
    }
  </style>
</head>
<body>

<div class="container">
  <h1>BubuDry</h1>
  <p class="subtitle">
    <span class="conn-dot" id="connDot"></span>
    <span id="connText">Connecting...</span>
  </p>

  <!-- ============ HERO CARD ============ -->
  <div class="hero">
    <div class="mascots">
      <img src="/static/BubuWine.gif" alt="Bubu" title="Bubu">
      <!-- Inline SVG tumble dryer -->
      <div class="dryer-wrap" id="dryerWrap">
        <svg viewBox="0 0 180 200" xmlns="http://www.w3.org/2000/svg">
          <rect x="10" y="10" width="160" height="180" rx="16" ry="16"
                fill="#2a3a5c" stroke="#3d5278" stroke-width="3"/>
          <rect x="10" y="10" width="160" height="36" rx="16" ry="16"
                fill="#344868" stroke="#3d5278" stroke-width="2"/>
          <rect x="10" y="30" width="160" height="16" fill="#344868"/>
          <circle cx="40" cy="28" r="8" fill="#1e2a47" stroke="#4fc3f7" stroke-width="2"/>
          <line x1="40" y1="21" x2="40" y2="26" stroke="#e74c3c" stroke-width="2" stroke-linecap="round"/>
          <circle cx="140" cy="28" r="10" fill="#1e2a47" stroke="#4fc3f7" stroke-width="2"/>
          <line x1="140" y1="28" x2="140" y2="20" stroke="#8892a8" stroke-width="2" stroke-linecap="round"/>
          <circle cx="90" cy="120" r="58" fill="#1a2240" stroke="#3d5278" stroke-width="3"/>
          <circle cx="90" cy="120" r="48" fill="#1e3a5f" stroke="#4fc3f7" stroke-width="2" opacity="0.5"/>
          <g class="drum-spin">
            <circle cx="75" cy="108" r="8" fill="#4fc3f7" opacity="0.5"/>
            <circle cx="105" cy="108" r="7" fill="#81d4fa" opacity="0.4"/>
            <circle cx="90" cy="130" r="9" fill="#29b6f6" opacity="0.4"/>
            <circle cx="78" cy="128" r="6" fill="#4dd0e1" opacity="0.5"/>
            <circle cx="102" cy="122" r="7" fill="#4fc3f7" opacity="0.3"/>
            <circle cx="85" cy="112" r="5" fill="#80deea" opacity="0.4"/>
            <circle cx="96" cy="115" r="6" fill="#26c6da" opacity="0.4"/>
          </g>
          <rect x="130" y="115" width="12" height="5" rx="2" fill="#3d5278"/>
          <rect x="22" y="188" width="16" height="6" rx="3" fill="#3d5278"/>
          <rect x="142" y="188" width="16" height="6" rx="3" fill="#3d5278"/>
        </svg>
      </div>
      <img src="/static/DuduWine.gif" alt="Dudu" title="Dudu">
    </div>

    <!-- Status badge -->
    <div class="status-badge status-unknown" id="statusBadge">Starting...</div>

    <!-- Programme & phase -->
    <div id="infoLines">
      <div class="info-line hidden" id="progLine">Programme: <strong id="progName"></strong></div>
      <div class="info-line hidden" id="phaseLine">Phase: <strong id="phaseName"></strong></div>
      <div class="info-line hidden" id="dryLevelLine">Dry level: <strong id="dryLevelName"></strong></div>
    </div>

    <!-- Time remaining -->
    <div id="timeSection" class="hidden">
      <div class="time-big" id="timeRemaining"></div>
      <div class="time-label">remaining</div>
    </div>

    <!-- Progress bar -->
    <div class="progress-section hidden" id="progressSection">
      <div class="progress-bar-bg">
        <div class="progress-bar-fill" id="progressFill" style="width: 0%"></div>
      </div>
      <div class="progress-text" id="progressText"></div>
    </div>

    <!-- Alerts -->
    <div class="alerts" id="alerts"></div>

    <!-- Controls: Start / Pause / Stop / Dry Level -->
    <div class="controls" id="controls">
      <div class="dry-picker">
        <button class="ctrl-btn btn-prog" id="btnProg" onclick="toggleProgMenu()">Programme: -</button>
        <div class="dry-menu" id="progMenu"></div>
      </div>
      <button class="ctrl-btn btn-start hidden" id="btnStart" onclick="doStart()">Start</button>
      <button class="ctrl-btn btn-pause hidden" id="btnPause" onclick="togglePause()">Pause</button>
      <button class="ctrl-btn btn-stop hidden"  id="btnStop"  onclick="confirmStop()">Stop</button>
      <div class="dry-picker">
        <button class="ctrl-btn btn-dry" id="btnDry" onclick="toggleDryMenu()">Dryness: -</button>
        <div class="dry-menu" id="dryMenu">
          <button class="dry-option" data-level="1" onclick="setDryLevel(1)">Iron Dry</button>
          <button class="dry-option" data-level="2" onclick="setDryLevel(2)">Hang Dry</button>
          <button class="dry-option" data-level="3" onclick="setDryLevel(3)">Cupboard Dry</button>
          <button class="dry-option" data-level="4" onclick="setDryLevel(4)">Extra Dry</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ============ TABBED SECTION ============ -->
  <div class="tabs-wrap">
    <div class="tab-bar">
      <button class="tab-btn active" onclick="switchTab('status')">Status</button>
      <button class="tab-btn" onclick="switchTab('history')">History</button>
      <button class="tab-btn" onclick="switchTab('maintenance')">Maintenance</button>
    </div>

    <!-- Tab: Status -->
    <div class="tab-panel active" id="tab-status">
      <div class="tab-section-title">Dryer Status</div>
      <div class="stat-row"><span class="stat-label">Machine State</span><span class="stat-value" id="statStatus">-</span></div>
      <div class="stat-row"><span class="stat-label">Door</span><span class="stat-value" id="statDoor">-</span></div>
      <div class="stat-row"><span class="stat-label">Water Tank</span><span class="stat-value" id="statTank">-</span></div>
      <div class="stat-row"><span class="stat-label">Filter</span><span class="stat-value" id="statFilter">-</span></div>
      <div class="stat-row"><span class="stat-label">Anti-crease</span><span class="stat-value" id="statAnticrease">-</span></div>
      <div class="stat-row"><span class="stat-label">Dryer Online</span><span class="stat-value" id="statOnline">-</span></div>
      <div class="stat-row"><span class="stat-label">Cycle Started</span><span class="stat-value" id="statStarted">-</span></div>
    </div>

    <!-- Tab: History -->
    <div class="tab-panel" id="tab-history">
      <div class="tab-section-title">Recent Cycles</div>
      <table class="history-table" id="historyTable">
        <thead>
          <tr><th>Programme</th><th>Date</th><th>Duration</th><th>Dryness</th></tr>
        </thead>
        <tbody id="historyBody">
          <tr><td colspan="4" style="color:var(--text-dim)">Loading...</td></tr>
        </tbody>
      </table>
    </div>

    <!-- Tab: Maintenance -->
    <div class="tab-panel" id="tab-maintenance">
      <div class="tab-section-title">Maintenance &amp; Stats</div>
      <div class="stat-row"><span class="stat-label">Total Cycles</span><span class="stat-value" id="statCycles">-</span></div>
      <div class="stat-row"><span class="stat-label">Filter Status</span><span class="stat-value" id="maintFilter">-</span></div>
      <div class="stat-row"><span class="stat-label">Last Checkup</span><span class="stat-value" id="maintCheckup">-</span></div>
      <div id="mostUsedSection"></div>
    </div>
  </div>
</div>

<div class="footer">
  Last updated: <span id="lastUpdated">-</span> &middot; BubuDry
</div>

<!-- ============ JAVASCRIPT ============ -->
<script>
const $ = id => document.getElementById(id);

// ---- Tab switching ----
function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach((btn, i) => {
    btn.classList.toggle('active', btn.textContent.toLowerCase().replace(/\s/g,'') === name);
  });
  document.querySelectorAll('.tab-panel').forEach(p => {
    p.classList.toggle('active', p.id === 'tab-' + name);
  });
}

// ---- Dry level picker ----
let selectedDryLevel = 3; // default: Cupboard Dry
const DRY_LABELS = {1: 'Iron Dry', 2: 'Hang Dry', 3: 'Cupboard Dry', 4: 'Extra Dry'};

function toggleDryMenu() {
  $('dryMenu').classList.toggle('open');
}
function setDryLevel(level) {
  selectedDryLevel = level;
  $('btnDry').textContent = 'Dryness: ' + DRY_LABELS[level];
  $('dryMenu').classList.remove('open');
  // Highlight active option
  document.querySelectorAll('.dry-option').forEach(o => {
    o.classList.toggle('active', parseInt(o.dataset.level) === level);
  });
}
// Close menu on outside click
document.addEventListener('click', function(e) {
  if (!e.target.closest('.dry-picker')) {
    $('dryMenu').classList.remove('open');
  }
});
// Init default
setDryLevel(3);

// ---- Programme picker ----
let selectedProgramme = null;

function toggleProgMenu() {
  $('progMenu').classList.toggle('open');
}
function setProgramme(id, name) {
  selectedProgramme = id;
  $('btnProg').textContent = 'Programme: ' + name;
  $('progMenu').classList.remove('open');
  document.querySelectorAll('#progMenu .dry-option').forEach(o => {
    o.classList.toggle('active', o.dataset.prog === id);
  });
}
function buildProgMenu(programmes) {
  const menu = $('progMenu');
  if (!programmes || programmes.length === 0) return;
  menu.innerHTML = programmes.map(p =>
    '<button class="dry-option" data-prog="' + p.id + '" onclick="setProgramme(\'' + p.id + '\', \'' + p.name + '\')">' + p.name + '</button>'
  ).join('');
  // Auto-select first programme if none selected
  if (!selectedProgramme) {
    // Prefer cotton, else first available
    const cotton = programmes.find(p => p.id.toLowerCase().includes('cotton'));
    const pick = cotton || programmes[0];
    setProgramme(pick.id, pick.name);
  }
}
document.addEventListener('click', function(e) {
  if (!e.target.closest('#btnProg') && !e.target.closest('#progMenu')) {
    $('progMenu').classList.remove('open');
  }
});

function doStart() {
  if (!selectedProgramme) {
    alert('Please select a programme first');
    return;
  }
  sendCommand('startProgram', selectedProgramme);
}

// ---- Main UI update ----
function updateUI(s) {
  // Connection indicator — reflects whether our backend can reach hOn (reliable),
  // not dryer_online which is a stale MQTT event and often lags behind reality.
  const dot = $('connDot');
  if (s.connected) {
    dot.className = 'conn-dot online';
    $('connText').textContent = 'Connected';
  } else {
    dot.className = 'conn-dot offline';
    $('connText').textContent = 'Reconnecting...';
  }

  // Status badge
  const badge = $('statusBadge');
  badge.textContent = s.status_label || 'Unknown';
  badge.className = 'status-badge status-' + (s.status || 'unknown');

  // Dryer animation
  const wrap = $('dryerWrap');
  wrap.classList.remove('is-running', 'is-paused');
  if (s.status === 'running') wrap.classList.add('is-running');
  if (s.status === 'paused') wrap.classList.add('is-paused');

  // Programme / phase / dry level
  setInfoLine('progLine', 'progName', s.programme);
  setInfoLine('phaseLine', 'phaseName', s.phase);
  setInfoLine('dryLevelLine', 'dryLevelName', s.dry_level);

  // Time remaining
  if (s.remaining_minutes && s.remaining_minutes > 0) {
    const hrs = Math.floor(s.remaining_minutes / 60);
    const mins = s.remaining_minutes % 60;
    $('timeRemaining').textContent = hrs > 0
      ? hrs + 'h ' + String(mins).padStart(2, '0') + 'm'
      : mins + ' min';
    $('timeSection').classList.remove('hidden');
  } else {
    $('timeSection').classList.add('hidden');
  }

  // Progress bar
  if (s.total_minutes && s.total_minutes > 0 && s.remaining_minutes != null) {
    const elapsed = s.total_minutes - (s.remaining_minutes || 0);
    const pct = Math.min(100, Math.max(0, (elapsed / s.total_minutes) * 100));
    $('progressFill').style.width = pct.toFixed(1) + '%';
    $('progressText').textContent = Math.round(pct) + '% complete';
    $('progressSection').classList.remove('hidden');
  } else {
    $('progressSection').classList.add('hidden');
  }

  // Alerts
  let alertsHtml = '';
  if (s.water_tank_full) alertsHtml += alertBox('error', '&#128167;', 'Water tank is full — please empty it');
  if (s.filter_dirty) alertsHtml += alertBox('warning', '&#9881;', 'Filter needs cleaning');
  if (s.door_open) alertsHtml += alertBox('warning', '&#128682;', 'Door is open');
  if (s.error && !s.water_tank_full && !s.filter_dirty) alertsHtml += alertBox('error', '&#9888;', s.error);
  if (!s.connected) alertsHtml += alertBox('warning', '&#128268;', 'Cannot reach dryer service — will retry automatically');
  $('alerts').innerHTML = alertsHtml;

  // Controls visibility
  const isRunning = s.status === 'running';
  const isPaused = s.status === 'paused' || s.paused;
  const isActive = isRunning || isPaused;
  const isOff = s.status === 'off' || s.status === 'active' || s.status === 'finished' || s.status === 'unknown';

  // Start button: show when dryer is off/finished and connected
  $('btnStart').classList.toggle('hidden', !isOff || !s.connected);
  // Pause: show when running or paused and connected
  $('btnPause').classList.toggle('hidden', !(isActive && s.connected));
  // Stop: show when active and connected
  $('btnStop').classList.toggle('hidden', !(isActive && s.connected));
  // Dry level: only useful when dryer is off (pre-start setting)
  $('btnDry').parentElement.classList.toggle('hidden', !isOff || !s.connected);
  $('btnProg').parentElement.classList.toggle('hidden', !isOff || !s.connected);

  if (isPaused) {
    $('btnPause').textContent = 'Resume';
    $('btnPause').className = 'ctrl-btn btn-start';
  } else {
    $('btnPause').textContent = 'Pause';
    $('btnPause').className = 'ctrl-btn btn-pause';
  }

  // Status tab
  $('statStatus').textContent = s.status_label || '-';
  $('statDoor').textContent = s.door_open ? 'Open' : 'Closed';
  $('statDoor').style.color = s.door_open ? 'var(--orange)' : 'var(--green)';
  $('statTank').textContent = s.water_tank_full ? 'FULL — empty it!' : 'OK';
  $('statTank').style.color = s.water_tank_full ? 'var(--red)' : 'var(--green)';
  $('statFilter').textContent = s.filter_dirty ? 'Needs cleaning' : 'OK';
  $('statFilter').style.color = s.filter_dirty ? 'var(--orange)' : 'var(--green)';
  $('statAnticrease').textContent = s.anti_crease ? 'On' : 'Off';
  $('statOnline').textContent = s.dryer_online ? 'Yes' : 'No';
  $('statOnline').style.color = s.dryer_online ? 'var(--green)' : 'var(--red)';
  $('statStarted').textContent = s.cycle_started ? s.cycle_started.replace('T', ' ').substring(11, 16) : '-';

  // Maintenance tab
  const stats = s.statistics || {};
  $('statCycles').textContent = stats.total_cycles || '-';

  const fc = stats.filter_cleaning || {};
  if (fc.remainingWashes != null) {
    $('maintFilter').textContent = fc.remainingWashes + ' washes until cleaning';
    $('maintFilter').style.color = fc.remainingWashes < 5 ? 'var(--orange)' : 'var(--green)';
  } else {
    $('maintFilter').textContent = s.filter_dirty ? 'Needs cleaning' : 'OK';
    $('maintFilter').style.color = s.filter_dirty ? 'var(--orange)' : 'var(--green)';
  }

  const lc = stats.last_checkup || {};
  $('maintCheckup').textContent = lc.date || '-';

  // Most-used programmes
  if (stats.most_used && stats.most_used.length > 0) {
    let muHtml = '<div class="tab-section-title">Most Used Programmes</div>';
    stats.most_used.slice(0, 5).forEach(p => {
      const name = (p.programName || p.name || '-').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
      muHtml += '<div class="stat-row"><span class="stat-label">' + esc(name)
              + '</span><span class="stat-value">' + (p.counter || p.count || '-') + ' cycles</span></div>';
    });
    $('mostUsedSection').innerHTML = muHtml;
  }

  // Programme picker
  if (s.programmes && s.programmes.length > 0) buildProgMenu(s.programmes);

  // History tab
  if (s.history && s.history.length > 0) {
    let rows = '';
    s.history.forEach(h => {
      rows += '<tr><td>' + esc(h.programme || '-') + '</td>'
            + '<td>' + esc(h.date || '-') + '</td>'
            + '<td>' + (h.duration ? h.duration + ' min' : '-') + '</td>'
            + '<td>' + esc(h.dry_level || '-') + '</td></tr>';
    });
    $('historyBody').innerHTML = rows;
  }

  // Footer
  $('lastUpdated').textContent = s.last_updated || '-';
}

function setInfoLine(lineId, valueId, value) {
  if (value) { $(lineId).classList.remove('hidden'); $(valueId).textContent = value; }
  else { $(lineId).classList.add('hidden'); }
}

function alertBox(type, icon, msg) {
  return '<div class="alert alert-' + type + '"><span class="alert-icon">' + icon + '</span><span>' + msg + '</span></div>';
}

function esc(s) {
  const d = document.createElement('div'); d.textContent = s; return d.innerHTML;
}

// ---- State & commands ----
let _lastState = {};

function togglePause() {
  const isPaused = (_lastState.paused || _lastState.status === 'paused');
  sendCommand(isPaused ? 'resumeProgram' : 'pauseProgram');
}

function confirmStop() {
  if (confirm('Are you sure you want to stop the dryer?')) sendCommand('stopProgram');
}

async function sendCommand(cmd, programme) {
  const body = {command: cmd};
  if (programme) body.programme = programme;
  if (cmd === 'startProgram') body.dryLevel = selectedDryLevel;
  try {
    const res = await fetch('/api/command', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    const data = await res.json();
    if (!data.ok) alert('Command failed: ' + (data.error || 'Unknown error'));
    setTimeout(fetchState, 1500);
  } catch (e) {
    alert('Could not send command: ' + e.message);
  }
}

async function fetchState() {
  try {
    const res = await fetch('/api/state');
    const data = await res.json();
    _lastState = data;
    updateUI(data);
  } catch (e) {
    console.error('Fetch failed:', e);
  }
}

// Poll every 30 seconds as a safety net — live state arrives via MQTT push
fetchState();
setInterval(fetchState, 2000);
</script>

</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not HON_EMAIL or not HON_PASSWORD:
        log.error(
            "HON_EMAIL and HON_PASSWORD environment variables must be set.\n"
            "Example:  HON_EMAIL=me@example.com HON_PASSWORD=secret python app.py"
        )
        raise SystemExit(1)

    poller = threading.Thread(target=polling_loop, name="dryer-poller", daemon=True)
    poller.start()

    # Silence noisy third-party loggers on console (still go to file)
    for noisy in ("werkzeug", "awscrt", "awsiot", "pyhon"):
        lg = logging.getLogger(noisy)
        lg.setLevel(logging.WARNING)
        lg.addHandler(_file)
        lg.propagate = False
    log.info("Dashboard ready at http://0.0.0.0:%d", FLASK_PORT)
    app.run(host="0.0.0.0", port=FLASK_PORT, use_reloader=False, threaded=True)
