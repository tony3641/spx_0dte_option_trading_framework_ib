"""Render benchmark runner: drives tests/e2e/render_bench.js in Chromium against the hermetic server.

CLI (baseline / ad-hoc): python -m tests.e2e.render_bench --protocol v1 --seconds 60 --out <file.json>
Protocol v1 is only valid against the baseline commit 577db68 (its stream-scope partial chain_quotes are a
full replace for the current client); v2 is the protocol for this branch.
"""
import argparse
import asyncio
import json
import os
import sys

from tests.e2e.hermetic_server import hermetic_server

BENCH_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render_bench.js")


def _proactor_policy():
    """Playwright's sync API needs a Proactor loop on Windows; server.py may have set Selector."""
    prev = None
    if os.name == "nt":
        prev = asyncio.get_event_loop_policy()
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    return prev


def open_page(p, base_url, tab="dashboard"):
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    # Page exceptions (e.g. a handler throwing on an injected message) go to stderr, not the result.
    page.on("pageerror", lambda exc: print(f"[render_bench:{tab}] page error: {exc}", file=sys.stderr))
    page.goto(f"{base_url}/#{tab}")
    page.wait_for_function("() => typeof state !== 'undefined' && state.wsConnected === true", timeout=30000)
    page.wait_for_timeout(1500)            # let the server's own init land before we inject ours
    page.add_script_tag(path=BENCH_JS)
    return browser, page


def run_bench(base_url: str, protocol: str, seconds: float, tabs=("dashboard", "chain")) -> dict:
    from playwright.sync_api import sync_playwright
    prev = _proactor_policy()
    try:
        results = {}
        with sync_playwright() as p:
            for tab in tabs:
                browser, page = open_page(p, base_url, tab)
                try:
                    results[tab] = page.evaluate(
                        "opts => window.__renderBench(opts)",
                        {"protocol": protocol, "seconds": seconds, "tab": tab})
                finally:
                    browser.close()
        return results
    finally:
        if prev is not None:
            asyncio.set_event_loop_policy(prev)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--protocol", choices=("v1", "v2"), required=True)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    with hermetic_server() as url:
        res = run_bench(url, args.protocol, args.seconds)
    text = json.dumps(res, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
