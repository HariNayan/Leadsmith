"""The coding agents that can power Leadsmith: Claude Code, Codex, OpenCode and Gemini CLI.

Every engine gets the same Leadsmith MCP server and the same safety rules: it may use the Leadsmith tools and the
web, but never shell commands, file edits, or the two tools that approve and send email. Those stay a human click
in the dashboard. Each adapter knows how to start its CLI headless and turn its JSON output into log lines.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
BLOCKED_MCP = ("review_drafts", "send_approved_emails")
# Tool names arrive prefixed differently per engine: mcp__leadsmith__x, leadsmith_x, mcp_leadsmith_x.
_PREFIX_RE = re.compile(r"^(mcp__leadsmith__|mcp_leadsmith_|leadsmith_)")


def tool_name(raw: str) -> str:
    name = _PREFIX_RE.sub("", raw or "", count=1)
    return name if name else raw


def short(value: Any, limit: int = 90) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tool_line(raw: str, args: Any, indent: str = "") -> str:
    args = args if isinstance(args, dict) else {}
    detail = args.get("domain") or args.get("query") or args.get("url") or args.get("website") or args.get("command") or args
    return f"{indent}→ {tool_name(raw)} {short(detail)}".rstrip()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _pretty(name: str) -> str:
    known = {"github-models": "GitHub Models", "github-copilot": "GitHub Copilot", "openai": "OpenAI",
             "anthropic": "Anthropic", "openrouter": "OpenRouter", "google": "Google", "groq": "Groq",
             "cloudflare-workers-ai": "Cloudflare Workers AI", "deepseek": "DeepSeek", "xai": "xAI", "ollama": "Ollama"}
    return known.get(name, name.replace("-", " ").title())


def mcp_server() -> dict[str, Any]:
    """The Leadsmith MCP server, started from this same Python so every engine sees the dashboard's code and data."""
    return {"command": sys.executable, "args": ["-m", "leadsmith.cli", "serve"],
            "env": {"LEADSMITH_HOME": str(config.HOME)}}


# On Windows, never flash a console window when an engine is checked or run.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_quiet(cmd: list[str], timeout: int = 20) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                              creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None


@dataclass
class Parsed:
    """What one line of engine output means."""
    log: str = ""
    result: str | None = None        # final answer text (replaces any earlier one)
    result_part: str = ""            # streamed piece of the final answer (appended)
    cost: float = 0.0                # USD to add, when the engine reports it
    error: str | None = None


@dataclass
class Engine:
    key: str
    label: str
    binary: str
    install: str
    sign_in: str
    plan: str                        # whose plan or account pays, for the UI ("your Claude plan")
    subagents: bool = False          # can it dispatch the agents in agents/ itself?
    model_hint: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def path(self) -> str | None:
        return shutil.which(self.binary)

    def info(self) -> dict[str, Any]:
        """What Leadsmith knows without starting the engine: whether it's installed, and the plan it signs in with."""
        installed = bool(self.path())
        return {"key": self.key, "label": self.label, "plan": self.plan, "model_hint": self.model_hint,
                "installed": installed, "install": self.install, "account": self.account() if installed else None}

    def account(self) -> dict[str, Any] | None:
        """The user's plan, read from the CLI's own files on this computer (nothing is started or sent).
        {"plan": "Claude Pro", "detail": "..."} or None when it can't be told."""
        return None

    def status(self) -> dict[str, Any]:
        """Starts the CLI once to check sign-in. Only called when the user clicks Connect."""
        path = self.path()
        base = {"key": self.key, "label": self.label, "plan": self.plan, "model_hint": self.model_hint,
                "account": self.account()}
        if not path:
            return {**base, "installed": False, "logged_in": False, "help": f"Install {self.label}: {self.install}"}
        logged_in, method = self.signed_in(path)
        return {**base, "installed": True, "logged_in": logged_in, "auth_method": method,
                "help": None if logged_in else self.sign_in}

    def signed_in(self, path: str) -> tuple[bool, str | None]:
        return True, None

    def models(self) -> list[dict[str, str]]:
        """Models for the Settings dropdown: [{"id", "label"}]. Aliases where the CLI has them, so they stay current."""
        return []

    def command(self, model: str, session: str | None = None) -> tuple[list[str], dict[str, str]]:
        """The headless command. With `session`, it continues that conversation instead of starting a new one."""
        raise NotImplementedError

    def session_of(self, event: dict[str, Any]) -> str | None:
        """The session id, if this output event carries it."""
        return None

    def parse(self, event: dict[str, Any]) -> Parsed:
        raise NotImplementedError


class Claude(Engine):
    TOOL_PREFIX = "mcp__leadsmith__"

    def models(self) -> list[dict[str, str]]:
        # Aliases always point at the newest model of each family.
        return [{"id": "sonnet", "label": "Sonnet: balanced, good default"},
                {"id": "opus", "label": "Opus: most capable, uses more of your plan"},
                {"id": "haiku", "label": "Haiku: fastest, lightest"},
                {"id": "fable", "label": "Fable"}]

    PLANS = {"claude_pro": "Claude Pro", "claude_max": "Claude Max", "claude_team": "Claude Team",
             "claude_enterprise": "Claude Enterprise", "claude_free": "Claude Free"}

    def account(self) -> dict[str, Any] | None:
        oauth = _read_json(Path.home() / ".claude.json").get("oauthAccount") or {}
        kind = oauth.get("organizationType") or ""
        if kind:
            return {"plan": self.PLANS.get(kind, kind.replace("_", " ").title()),
                    "detail": "Extra usage on" if oauth.get("hasExtraUsageEnabled") else None}
        if os.environ.get("ANTHROPIC_API_KEY"):
            return {"plan": "Anthropic API key", "detail": "Pay per use"}
        return None

    def session_of(self, event: dict[str, Any]) -> str | None:
        return event.get("session_id") if event.get("type") == "system" else None

    def signed_in(self, path: str) -> tuple[bool, str | None]:
        out = _run_quiet([path, "auth", "status", "--json"])
        try:
            info = json.loads(out.stdout or "{}") if out else {}
        except ValueError:
            info = {}
        return bool(info.get("loggedIn")), info.get("authMethod")

    def command(self, model: str, session: str | None = None) -> tuple[list[str], dict[str, str]]:
        mcp_config = config.HOME / ".dashboard-mcp.json"
        mcp_config.write_text(json.dumps({"mcpServers": {"leadsmith": mcp_server()}}), encoding="utf-8")
        allowed = [f"{self.TOOL_PREFIX}*", "WebSearch", "WebFetch", "Agent", "Task", "Skill", "TodoWrite"]
        blocked = [f"{self.TOOL_PREFIX}{name}" for name in BLOCKED_MCP] + ["Bash", "Edit", "Write", "NotebookEdit"]
        # Agents and skills load from the plugin folder; --strict-mcp-config drops the plugin's own uv-launched server.
        cmd = [self.path() or self.binary, "-p", "--plugin-dir", str(PLUGIN_ROOT), "--mcp-config", str(mcp_config),
               "--strict-mcp-config", "--output-format", "stream-json", "--verbose", "--permission-mode", "dontAsk",
               "--allowedTools", ",".join(allowed), "--disallowedTools", ",".join(blocked)]
        try:
            model = config.normalize_model(model)
        except ValueError:
            model = ""  # an unusable value falls back to the Claude Code default instead of failing the run
        return cmd + (["--model", model] if model else []) + (["--resume", session] if session else []), {}

    def parse(self, event: dict[str, Any]) -> Parsed:
        kind = event.get("type")
        if kind == "result":
            return Parsed(result=event.get("result"), cost=event.get("total_cost_usd") or 0.0,
                          error=(event.get("result") or "Claude Code reported an error") if event.get("is_error") else None)
        if kind != "assistant":
            return Parsed()
        indent = "    " if event.get("parent_tool_use_id") else ""
        lines = []
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                lines.append(f"{indent}{block['text'].strip()}")
            elif block.get("type") == "tool_use":
                args = block.get("input") or {}
                if block.get("name") in ("Agent", "Task"):
                    lines.append(f"{indent}▶ agent {args.get('subagent_type', '')}: {short(args.get('description', ''))}")
                else:
                    lines.append(tool_line(block.get("name", ""), args, indent))
        return Parsed(log="\n".join(lines) + "\n" if lines else "")


class Codex(Engine):
    PLANS = {"free": "ChatGPT Free", "go": "ChatGPT Go", "plus": "ChatGPT Plus", "pro": "ChatGPT Pro",
             "team": "ChatGPT Team", "business": "ChatGPT Business", "enterprise": "ChatGPT Enterprise", "edu": "ChatGPT Edu"}

    def account(self) -> dict[str, Any] | None:
        auth = _read_json(Path.home() / ".codex" / "auth.json")
        if auth.get("auth_mode") == "apikey" or (auth.get("OPENAI_API_KEY") and not auth.get("tokens")):
            return {"plan": "OpenAI API key", "detail": "Pay per use"}
        token = (auth.get("tokens") or {}).get("id_token") or ""
        if token.count(".") != 2:
            return None
        try:  # only the plan claims are read from the sign-in token; the token itself is never used or shown
            payload = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (ValueError, TypeError):
            return None
        info = claims.get("https://api.openai.com/auth") or {}
        kind = info.get("chatgpt_plan_type") or ""
        until = (info.get("chatgpt_subscription_active_until") or "")[:10]
        # The plan comes from Codex's last sign-in token, so it can be out of date; only show a date still ahead.
        detail = "As of your last Codex sign-in"
        try:
            end = datetime.fromisoformat(until) if until else None
        except ValueError:
            end = None
        if end and end.date() >= datetime.now().date():
            detail = f"Active until {end.day} {end:%b %Y}"
        return {"plan": self.PLANS.get(kind, f"ChatGPT {kind.title()}" if kind else "ChatGPT"), "detail": detail}

    def session_of(self, event: dict[str, Any]) -> str | None:
        return event.get("thread_id") if event.get("type") == "thread.started" else None

    def models(self) -> list[dict[str, str]]:
        # Codex keeps the models available to your account in a cache file; read it without starting Codex.
        try:
            cache = json.loads((Path.home() / ".codex" / "models_cache.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        found = [m for m in cache.get("models") or [] if m.get("slug") and m.get("visibility", "list") == "list"]
        found.sort(key=lambda m: m.get("priority", 99))
        return [{"id": m["slug"], "label": f"{m.get('display_name') or m['slug']}"
                 + (f": {m['description'].rstrip('.')}" if m.get("description") else "")} for m in found]

    def signed_in(self, path: str) -> tuple[bool, str | None]:
        out = _run_quiet([path, "login", "status"])
        text = ((out.stdout or "") + (out.stderr or "")).strip() if out else ""
        ok = bool(out) and out.returncode == 0 and "not logged in" not in text.lower()
        return ok, text.replace("Logged in using ", "") or None

    def command(self, model: str, session: str | None = None) -> tuple[list[str], dict[str, str]]:
        server = mcp_server()
        env_toml = "{" + ", ".join(f"{k}={json.dumps(v)}" for k, v in server["env"].items()) + "}"
        # Sessions are kept (no --ephemeral) so a later run can continue them. `exec resume` has no --sandbox
        # flag, so the read-only sandbox is set as config, the same way for new and continued sessions.
        cmd = [self.path() or self.binary, "exec", *(["resume"] if session else []), "--json", "--skip-git-repo-check",
               "-c", 'sandbox_mode="read-only"',  # shell commands can read, never write
               "-c", f"mcp_servers.leadsmith.command={json.dumps(server['command'])}",
               "-c", f"mcp_servers.leadsmith.args={json.dumps(server['args'])}",
               "-c", f"mcp_servers.leadsmith.env={env_toml}",
               "-c", f"mcp_servers.leadsmith.disabled_tools={json.dumps(list(BLOCKED_MCP))}",
               "-c", 'mcp_servers.leadsmith.default_tools_approval_mode="approve"',
               "-c", "mcp_servers.leadsmith.startup_timeout_sec=60",
               "-c", 'web_search="live"']
        return cmd + (["--model", model] if model else []) + ([session] if session else []) + ["-"], {}

    def parse(self, event: dict[str, Any]) -> Parsed:
        kind = event.get("type")
        if kind in ("error", "turn.failed"):
            err = event.get("message") or (event.get("error") or {}).get("message") or "Codex reported an error"
            return Parsed(error=err)
        item = event.get("item") or {}
        itype = item.get("type")
        if kind == "item.started" and itype == "mcp_tool_call":
            return Parsed(log=tool_line(item.get("tool", ""), item.get("arguments")) + "\n")
        if kind != "item.completed":
            return Parsed()
        if itype == "agent_message" and (item.get("text") or "").strip():
            text = item["text"].strip()
            return Parsed(log=text + "\n", result=text)
        if itype == "mcp_tool_call" and item.get("status") == "failed":
            return Parsed(log=f"  ✗ {tool_name(item.get('tool', ''))}: {short((item.get('error') or {}).get('message', ''))}\n")
        if itype == "web_search":
            return Parsed(log=f"→ web search {short(item.get('query') or '')}\n")
        if itype == "command_execution":
            return Parsed(log=f"→ shell {short(item.get('command') or '')}\n")
        return Parsed()


class OpenCode(Engine):
    def account(self) -> dict[str, Any] | None:
        auth = _read_json(Path.home() / ".local" / "share" / "opencode" / "auth.json")
        if not auth:
            return {"plan": "OpenCode free models", "detail": "No providers added yet"}
        kinds = {name: (entry.get("type") if isinstance(entry, dict) else None) for name, entry in auth.items()}
        word = {"oauth": "subscription", "api": "API key"}
        if len(set(kinds.values())) == 1:  # all the same kind: say it once
            how = word.get(next(iter(kinds.values())) or "", "")
            return {"plan": ", ".join(_pretty(n) for n in kinds),
                    "detail": f"OpenCode providers · {how}{'s' if how and len(kinds) > 1 else ''}" if how else "OpenCode providers"}
        return {"plan": ", ".join(f"{_pretty(n)} ({word.get(k or '', 'other')})" for n, k in kinds.items()),
                "detail": "OpenCode providers"}

    def session_of(self, event: dict[str, Any]) -> str | None:
        return event.get("sessionID")

    def models(self) -> list[dict[str, str]]:
        # The providers you've added decide the list; `opencode models` prints one provider/model per line.
        out = _run_quiet([self.path() or self.binary, "models"], timeout=60)
        lines = [ln.strip() for ln in (out.stdout if out else "").splitlines()]
        return [{"id": ln, "label": ln} for ln in lines if re.fullmatch(r"[\w.@-]+/\S+", ln)]

    def signed_in(self, path: str) -> tuple[bool, str | None]:
        out = _run_quiet([path, "auth", "list"])
        text = re.sub(r"\x1b\[[0-9;]*m", "", (out.stdout or "") if out else "")
        counts = [int(n) for n in re.findall(r"(\d+)\s+(?:credentials?|environment variables?)", text)]
        return sum(counts) > 0, f"{sum(counts)} provider(s)" if counts else None

    def command(self, model: str, session: str | None = None) -> tuple[list[str], dict[str, str]]:
        server = mcp_server()
        cfg = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {"leadsmith": {"type": "local", "command": [server["command"], *server["args"]],
                                  "environment": server["env"], "enabled": True}},
            "permission": {"edit": "deny", "bash": "deny", "webfetch": "allow"},
            "tools": {f"leadsmith_{name}": False for name in BLOCKED_MCP},
        }
        cmd = [self.path() or self.binary, "run", "--format", "json"] + (["--model", model] if model else []) \
            + (["--session", session] if session else [])
        return cmd, {"OPENCODE_CONFIG_CONTENT": json.dumps(cfg)}

    def parse(self, event: dict[str, Any]) -> Parsed:
        kind = event.get("type")
        part = event.get("part") or {}
        if kind == "tool_use":
            state = part.get("state") or {}
            line = tool_line(part.get("tool", ""), state.get("input"))
            if state.get("status") == "error":
                line += f"\n  ✗ {short(state.get('error') or '')}"
            return Parsed(log=line + "\n")
        if kind == "text" and (part.get("text") or "").strip():
            text = part["text"].strip()
            return Parsed(log=text + "\n", result=text)
        if kind == "step_finish":
            return Parsed(cost=float(part.get("cost") or 0))
        if kind == "error":
            err = event.get("error") or {}
            return Parsed(error=(err.get("data") or {}).get("message") or err.get("message") or str(err) or "OpenCode reported an error")
        return Parsed()


class Gemini(Engine):
    def account(self) -> dict[str, Any] | None:
        home = Path.home() / ".gemini"
        kind = ((_read_json(home / "settings.json").get("security") or {}).get("auth") or {}).get("selectedType") or ""
        if kind == "oauth-personal" or (not kind and (home / "oauth_creds.json").exists()):
            return {"plan": "Google account", "detail": "Gemini Code Assist"}
        if kind == "vertex-ai" or os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"):
            return {"plan": "Vertex AI", "detail": "Your Google Cloud project"}
        if kind == "gemini-api-key" or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            return {"plan": "Gemini API key", "detail": "Free or paid, depending on the key"}
        return None

    def session_of(self, event: dict[str, Any]) -> str | None:
        return event.get("session_id") if event.get("type") == "init" else None

    def models(self) -> list[dict[str, str]]:
        # Gemini CLI's own aliases resolve to its current models.
        return [{"id": "auto", "label": "Auto: Gemini picks per task"},
                {"id": "pro", "label": "Pro: most capable"},
                {"id": "flash", "label": "Flash: fast, good default"},
                {"id": "flash-lite", "label": "Flash-Lite: fastest, lightest"}]

    def signed_in(self, path: str) -> tuple[bool, str | None]:
        home = Path.home() / ".gemini"
        if any(os.environ.get(k) for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI")):
            return True, "API key"
        if (home / "oauth_creds.json").exists():
            return True, "Google account"
        try:
            selected = json.loads((home / "settings.json").read_text(encoding="utf-8")).get("security", {}).get("auth", {})
        except (OSError, ValueError, AttributeError):
            selected = {}
        return bool(selected.get("selectedType")), selected.get("selectedType")

    def command(self, model: str, session: str | None = None) -> tuple[list[str], dict[str, str]]:
        # Project settings in the workspace: the Leadsmith server, the blocked tools, and no shell or file edits.
        settings_dir = config.HOME / ".gemini"
        settings_dir.mkdir(parents=True, exist_ok=True)
        (settings_dir / "settings.json").write_text(json.dumps({
            "mcpServers": {"leadsmith": {**mcp_server(), "excludeTools": list(BLOCKED_MCP), "timeout": 120000}},
            "tools": {"exclude": ["run_shell_command", "write_file", "replace"]},
        }, indent=2), encoding="utf-8")
        cmd = [self.path() or self.binary, "-o", "stream-json", "--approval-mode", "yolo", "--skip-trust",
               "--allowed-mcp-server-names", "leadsmith"]
        return cmd + (["--model", model] if model else []) + (["--resume", session] if session else []) + ["-p", " "], {}

    def parse(self, event: dict[str, Any]) -> Parsed:
        kind = event.get("type")
        if kind == "tool_use":  # text streamed before a tool call isn't the final answer
            return Parsed(log="\n" + tool_line(event.get("tool_name", ""), event.get("parameters")) + "\n", result="")
        if kind == "tool_result" and event.get("status") not in (None, "success"):
            return Parsed(log=f"  ✗ {short(event.get('output') or event.get('error') or '')}\n")
        if kind == "message" and event.get("role") == "assistant":
            return Parsed(log=event.get("content") or "", result_part=event.get("content") or "")
        if kind == "result" and event.get("status") not in (None, "success"):
            return Parsed(error=short(event.get("error") or event, 300))
        return Parsed()


ENGINES: dict[str, Engine] = {e.key: e for e in (
    Claude("claude", "Claude Code", "claude", "https://claude.com/claude-code",
           "Open a terminal, run `claude`, and sign in with /login.", "your Claude plan", subagents=True,
           model_hint='sonnet, opus, haiku, or a version like "Sonnet 5.5"'),
    Codex("codex", "Codex", "codex", "https://developers.openai.com/codex (npm install -g @openai/codex)",
          "Open a terminal and run `codex login`.", "your ChatGPT plan", model_hint="e.g. gpt-5.5"),
    OpenCode("opencode", "OpenCode", "opencode", "https://opencode.ai (npm install -g opencode-ai)",
             "Open a terminal and run `opencode auth login` to add a model provider.", "your OpenCode providers",
             model_hint="provider/model, e.g. anthropic/claude-sonnet-5-5"),
    Gemini("gemini", "Gemini CLI", "gemini", "https://geminicli.com (npm install -g @google/gemini-cli)",
           "Open a terminal, run `gemini`, and sign in with Google (or set GEMINI_API_KEY).", "your Google account",
           model_hint="e.g. gemini-3.6-flash"),
)}
DEFAULT = "claude"


def current() -> Engine:
    return ENGINES.get((config.get("AGENT_ENGINE") or DEFAULT).strip().lower(), ENGINES[DEFAULT])


def current_model() -> str:
    """The model for the chosen engine. CLAUDE_MODEL is the setting's old name, still read for Claude Code."""
    model = (config.get("AGENT_MODEL") or "").strip()
    if not model and current().key == "claude":
        model = (config.get("CLAUDE_MODEL") or "").strip()
    return model


def all_status() -> list[dict[str, Any]]:
    return [e.status() for e in ENGINES.values()]
