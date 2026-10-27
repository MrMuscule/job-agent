"""CLI: python -m jobagent collect | bot | top | stats"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from rich.console import Console
from rich.table import Table

from .config import load_profile
from .pipeline import collect
from .store import Store

console = Console()


def cmd_collect(top: int) -> None:
    profile = load_profile()
    store = Store()
    try:
        res = asyncio.run(collect(profile, store, log=lambda m: console.print(f"[yellow]{m}[/yellow]")))
        console.print(
            f"Adzuna {res.raw_adzuna} · JustJoin {res.raw_justjoin} · unique {res.unique} · "
            f"[green]kept {res.kept}[/green] (new {res.new}) · excluded {res.excluded} · "
            f"описаний JJ загружено {res.bodies_fetched} · closed {res.closed}")
        print_top(store, top)
    finally:
        store.close()


def print_top(store: Store, n: int) -> None:
    rows = store.ranked(n)
    table = Table(title=f"Top {len(rows)} из {store.count_ranked()}")
    for col in ("#", "Score", "Src", "Title", "Company", "Loc", "Salary", "Почему"):
        table.add_column(col, overflow="fold")
    for i, r in enumerate(rows, 1):
        why = "; ".join(f"{p:+d} {t}" if p else t
                        for p, t in sorted(json.loads(r["reasons_json"]),
                                           key=lambda x: abs(x[0]), reverse=True)[:3])
        table.add_row(str(i), str(r["score"]), r["source"][:3], r["title"][:45],
                      (r["company"] or "")[:22], (r["location"] or "")[:20],
                      r["salary_label"] or "—", why)
    console.print(table)


def main() -> None:
    p = argparse.ArgumentParser(prog="jobagent")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="собрать вакансии и показать топ")
    c.add_argument("--top", type=int, default=10)
    sub.add_parser("bot", help="запустить Telegram-бота")
    t = sub.add_parser("top", help="показать топ из базы")
    t.add_argument("--n", type=int, default=10)
    sub.add_parser("stats", help="статистика")
    args = p.parse_args()

    if args.cmd == "bot":
        from .bot import run_bot
        run_bot()
        return
    if args.cmd == "collect":
        cmd_collect(args.top)
        return
    store = Store()
    try:
        if args.cmd == "top":
            print_top(store, args.n)
        elif args.cmd == "stats":
            console.print(store.stats())
    finally:
        store.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
