"""leadsmith command line.

    leadsmith               open the dashboard (same as `leadsmith dashboard`)
    leadsmith dashboard     http://127.0.0.1:4600  [--port 4700] [--no-open]
    leadsmith serve         run the MCP server over stdio (what Claude Code launches)
    leadsmith init          create ~/.leadsmith with templates
    leadsmith doctor        check Claude Code, free sources, Gmail and the business profile
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def _init() -> int:
    from . import config

    created = config.ensure_home()
    print(f"Workspace: {config.HOME}")
    for path in created:
        print(f"  created {path}")
    if not created:
        print("  (already set up, nothing overwritten)")
    print("\nNext: run `uv run leadsmith` to open the dashboard.")
    return 0


async def _probe(coro) -> str:
    try:
        result = await coro
        return "ok" if result else "ok (no results)"
    except Exception as exc:  # noqa: BLE001 - report anything to the user
        return f"FAILED: {exc.__class__.__name__}: {str(exc)[:160]}"


def _doctor() -> int:
    from . import config, decks, engines, mailer
    from .brain import read_brain
    from .providers import emails, hn, news, website, yc

    config.load_env()
    config.ensure_home()
    print(f"Python {sys.version.split()[0]}  |  workspace {config.HOME}\n")

    chosen = engines.current().key
    print("Agent engines (change in Settings):")
    for st in engines.all_status():
        state = "signed in" if st["logged_in"] else "signed out" if st["installed"] else "not installed"
        mark = "*" if st["key"] == chosen else " "
        print(f" {mark}{st['label']:<15} {state}" + (f"   → {st['help']}" if st.get("help") and st["key"] == chosen else ""))

    print("\nFree data sources:")
    checks = {
        "website scan": website.scan("example.com"),
        "job boards": website.open_jobs("linear.app"),
        "google news": news.google_news("startup funding", 7, 1),
        "yc directory": yc.search("sales", limit=1, max_pages=1),
        "hn who's hiring": hn.who_is_hiring(["remote"], 1, 1),
        "email/MX check": emails.mx_records("gmail.com"),
    }
    for name, coro in checks.items():
        print(f"  {name:<16} {asyncio.run(_probe(coro))}")

    print("\nDecks:")
    print(f"  PDF renderer     {decks.find_browser() or 'not found (decks saved as HTML)'}")

    print("\nGmail:")
    status = mailer.mail_status()
    print(f"  address          {status['gmail_address'] or 'not set'}")
    print(f"  login            {mailer.test_login()}")
    print(f"  sending enabled  {status['send_enabled']}  (daily limit {status['daily_limit']})")

    print("\nBusiness profile:")
    for name, info in read_brain().items():
        state = "ready" if info["complete"] else f"{info['todos_left']} TODOs left"
        print(f"  {name:<10} {state}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="leadsmith", description="Find buyers and write outreach they reply to, on your own Claude Code")
    parser.add_argument("command", nargs="?", default="dashboard", choices=["dashboard", "serve", "init", "doctor"])
    parser.add_argument("--port", type=int, default=4600, help="dashboard port (default 4600)")
    parser.add_argument("--no-open", action="store_true", help="don't open the browser")
    args = parser.parse_args(argv)
    if args.command != "serve":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(errors="replace")  # Windows consoles can't print every character
            except (AttributeError, ValueError):
                pass
    if args.command == "init":
        return _init()
    if args.command == "doctor":
        return _doctor()
    if args.command == "serve":
        from .server import main as serve

        serve()
        return 0
    from .dashboard import run

    run(args.port, not args.no_open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
