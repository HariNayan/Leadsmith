"""Where Leadsmith keeps its data, and the settings the dashboard edits.

Everything lives in one folder (default ``~/.leadsmith``, override with
``LEADSMITH_HOME``) so it survives plugin updates and is easy to back up:

    ~/.leadsmith/
        .env          Gmail + sending settings (edited from the dashboard)
        brain/*.md    your company, ICP, personas and voice
        leadsmith.db  accounts, signals, contacts, drafts, decks, jobs
        decks/        generated PDF decks
        exports/      CSV exports

The app used to be called GTM Brain. A ``~/.gtm-brain`` folder is moved here on first start, and ``GTM_HOME``
still works.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path


def _home() -> Path:
    chosen = os.environ.get("LEADSMITH_HOME") or os.environ.get("GTM_HOME")
    if chosen:
        home = Path(chosen).expanduser()
    else:
        home = Path.home() / ".leadsmith"
        old = Path.home() / ".gtm-brain"
        if not home.exists() and old.is_dir():
            try:
                old.rename(home)
            except OSError:  # in use by an old dashboard: keep using it until the next start
                return old
    for suffix in ("", "-wal", "-shm"):
        old_db, new_db = home / f"gtm.db{suffix}", home / f"leadsmith.db{suffix}"
        if old_db.exists() and not new_db.exists():
            try:
                old_db.rename(new_db)
            except OSError:
                pass
    return home


HOME = _home()
ENV_FILE = HOME / ".env"
BRAIN_DIR = HOME / "brain"
# Never start an empty database next to an old one that couldn't be renamed.
DB_PATH = HOME / "leadsmith.db" if (HOME / "leadsmith.db").exists() or not (HOME / "gtm.db").exists() else HOME / "gtm.db"
EXPORT_DIR = HOME / "exports"
DECK_DIR = HOME / "decks"
TEMPLATES = Path(__file__).parent / "templates"

# key -> (label, kind, default, help). kind: text | secret | bool | int | textarea
SETTINGS: dict[str, tuple[str, str, str, str]] = {
    "GMAIL_ADDRESS": ("Gmail address", "text", "",
                      ""),
    "GMAIL_APP_PASSWORD": ("Gmail app password", "secret", "",
                           "Create one at myaccount.google.com/apppasswords (needs 2-Step Verification)."),
    "SENDER_NAME": ("Sender name", "text", "", ""),
    "SENDER_SIGNATURE": ("Signature", "textarea", "", ""),
    "POSTAL_ADDRESS": ("Postal address", "text", "", ""),
    "OPT_OUT_LINE": ("Opt-out line", "text", "If this isn't relevant, just reply \"no\" and I won't follow up.",
                     ""),
    "TEST_RECIPIENT": ("Test mode: send everything to", "text", "",
                       "While set, every email goes here instead, marked [TEST]. Clear it to go live."),
    "SEND_HOURS": ("Sending hours (prospect's time)", "text", "9-17",
                   ""),
    "SEND_WEEKDAYS_ONLY": ("Weekdays only", "bool", "true", ""),
    "PROSPECT_TIMEZONE": ("Default time zone", "text", "",
                          "Used when a prospect's location is unknown. Empty = your time zone."),
    "SEND_ENABLED": ("Sending enabled", "bool", "false", "Nothing is sent while this is off."),
    "DAILY_SEND_LIMIT": ("Daily send limit (maximum)", "int", "25", ""),
    "WARMUP": ("Warm-up", "bool", "true",
               "Start low and raise the daily limit each day."),
    "WARMUP_START": ("Warm-up: first-day limit", "int", "5", ""),
    "WARMUP_STEP": ("Warm-up: added per day", "int", "3", ""),
    "SEND_GAP_SECONDS": ("Gap between emails (seconds)", "int", "60",
                         ""),
    "AUTO_FOLLOWUPS": ("Automatic follow-ups", "bool", "true",
                       "Sends due follow-ups while the dashboard is open; stops on reply."),
    "EMAIL_STYLE": ("Email style", "text", "idea_menu",
                    "idea_menu: three ideas they pick from with one letter. personal_pitch: one specific compliment, "
                    "what you do for buyers like theirs, a personal deck attached, and a short call ask."),
    "AGENT_ENGINE": ("Agent engine", "engine", "claude",
                     "The coding agent that runs the four agents: claude, codex, opencode or gemini."),
    "AGENT_MODEL": ("Model", "text", "", "Optional. Empty = the engine's own default model."),
    "DECK_COLOR": ("Deck accent colour", "text", "#4f46e5", ""),
    "BETTERCONTACT_API_KEY": ("BetterContact API key", "secret", "",
                              "Optional, paid. Adds a Verify button for guessed emails."),
}


MODEL_FAMILIES = ("sonnet", "opus", "haiku", "fable")


def normalize_model(value: str | None) -> str:
    """Turn what people type ("Sonnet 5.5", "claude opus 5.5", "opus") into a name Claude Code accepts."""
    value = (value or "").strip()
    if not value:
        return ""
    m = re.fullmatch(r"(?i)(?:claude[\s_-]*)?(" + "|".join(MODEL_FAMILIES) + r")(?:[\s_-]*(\d+)(?:[.\s_-]+(\d+))?)?", value)
    if m:
        family, major, minor = m.group(1).lower(), m.group(2), m.group(3)
        if not major:
            return family
        return f"claude-{family}-{major}" + (f"-{minor}" if minor else "")
    if re.fullmatch(r"[A-Za-z0-9._\-\[\]]+", value):
        return value
    raise ValueError(f'"{value}" isn\'t a model name. Use sonnet, opus, haiku, or a full ID like claude-sonnet-5-5.')


def load_env() -> None:
    """Load ~/.leadsmith/.env into os.environ. Real env vars win."""
    if not ENV_FILE.exists():
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and not os.environ.get(key):
            os.environ[key] = value


def save_env(updates: dict[str, str]) -> None:
    """Write settings to .env, keeping comments and unrelated lines."""
    HOME.mkdir(parents=True, exist_ok=True)
    if not ENV_FILE.exists():
        ensure_home()  # start from the commented template, not a bare file
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    pending = dict(updates)
    for i, line in enumerate(lines):
        m = re.match(r"\s*([A-Z0-9_]+)\s*=", line)
        if m and m.group(1) in pending:
            key = m.group(1)
            lines[i] = f"{key}={_quote(pending.pop(key))}"
    lines += [f"{k}={_quote(v)}" for k, v in pending.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for key, value in updates.items():
        os.environ[key] = value


def _quote(value: str) -> str:
    value = value.replace("\r", "").replace("\n", "\\n")
    return f'"{value}"' if value and (value[0] == " " or value[-1] == " " or "#" in value) else value


def get(key: str, default: str | None = None) -> str | None:
    value = os.environ.get(key, "").strip()
    if value:
        return value
    if default is None and key in SETTINGS:
        return SETTINGS[key][2] or None
    return default


def get_int(key: str, default: int) -> int:
    try:
        return int(get(key, str(default)))  # type: ignore[arg-type]
    except ValueError:
        return default


def get_bool(key: str, default: bool = False) -> bool:
    value = get(key)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def ensure_home() -> list[str]:
    """Create the workspace from templates. Never overwrites user files."""
    created: list[str] = []
    for directory in (HOME, BRAIN_DIR, EXPORT_DIR, DECK_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    for template in sorted((TEMPLATES / "brain").glob("*.md")):
        target = BRAIN_DIR / template.name
        if not target.exists():
            shutil.copyfile(template, target)
            created.append(str(target))
    if not ENV_FILE.exists():
        shutil.copyfile(TEMPLATES / "env.example", ENV_FILE)
        created.append(str(ENV_FILE))
    return created


def public_settings() -> dict[str, dict[str, object]]:
    """Settings for the dashboard. Secrets are never sent back, only whether they're set."""
    out: dict[str, dict[str, object]] = {}
    for key, (label, kind, default, help_text) in SETTINGS.items():
        value = os.environ.get(key, "").strip() or default
        out[key] = {
            "label": label, "kind": kind, "help": help_text,
            "value": "" if kind == "secret" else value.replace("\\n", "\n"),
            "is_set": bool(os.environ.get(key, "").strip()),
        }
    return out
