"""Run the evaluation (SPEC §14) and write eval/RESULTS.md.

    python eval/run_eval.py                       # all configs, every ticket in eval/corpus/
    python eval/run_eval.py --configs C --limit 5 # a quick look
    python eval/run_eval.py --resume <run-name>   # continue a run that stopped (rate limits)

Tickets come from eval/corpus/{attack,benign,edge}/*.json. Drafts live in eval/corpus_drafts/
until a person reviews each one and moves it into eval/corpus/.

Each finished (config, ticket) run is saved under eval/runs/<run-name>/, so a stopped run
resumes where it left off. RESULTS.md is always rebuilt from all saved runs of that run name.
Needs LLM_BASE_URL, LLM_API_KEY and LLM_MODEL in .env. PayPal is simulated: no sandbox calls.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # so `python eval/run_eval.py` finds the packages from any folder

from demo_agent.eval_harness import (  # noqa: E402
    CONFIGS, compute_metrics, load_results, new_run_name, results_markdown, run_ticket, save_result,
)
from demo_agent.tickets import load_ticket  # noqa: E402
from wardlock.config import get_settings  # noqa: E402
from wardlock.llm import LLMClient  # noqa: E402
from wardlock.money import from_minor  # noqa: E402
from wardlock.seed import load_seed  # noqa: E402

CORPUS = ROOT / "eval" / "corpus"
RUNS = ROOT / "eval" / "runs"
RESULTS = ROOT / "eval" / "RESULTS.md"
CATEGORIES = ("attack", "benign", "edge")


def load_corpus(corpus: Path, category: str | None) -> list[tuple[str, object]]:
    tickets = []
    for cat in CATEGORIES:
        if category and cat != category:
            continue
        for path in sorted((corpus / cat).glob("*.json")):
            tickets.append((cat, load_ticket(path)))
    ids = [t.id for _cat, t in tickets]
    if len(set(ids)) != len(ids):
        raise ValueError("ticket ids must be unique across the corpus")
    return tickets


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run configs A, B and C over the eval corpus.")
    parser.add_argument("--configs", default=",".join(CONFIGS), help="comma-separated, e.g. A,C")
    parser.add_argument("--category", choices=CATEGORIES)
    parser.add_argument("--limit", type=int, help="at most this many tickets (for a quick look)")
    parser.add_argument("--resume", metavar="RUN_NAME", help="continue a saved run")
    parser.add_argument("--delay", type=float, default=0.0, help="seconds to wait between runs (free-tier limits)")
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    args = parser.parse_args(argv)

    configs = [c.strip().upper() for c in args.configs.split(",") if c.strip()]
    if not configs or any(c not in CONFIGS for c in configs):
        print(f"--configs must be a subset of {','.join(CONFIGS)}", file=sys.stderr)
        return 2
    settings = get_settings()
    if not (settings.llm_base_url and settings.llm_api_key.get_secret_value() and settings.llm_model):
        print("No LLM configured: set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL in .env", file=sys.stderr)
        return 2
    tickets = load_corpus(args.corpus, args.category)[: args.limit]
    if not tickets:
        print(f"No tickets in {args.corpus}. Review the drafts in eval/corpus_drafts/ and move them there.",
              file=sys.stderr)
        return 2

    now = datetime.now(UTC)
    run_name = args.resume or new_run_name(settings.llm_model, now)
    run_dir = RUNS / run_name
    done = {(r.config, r.ticket_id) for r in load_results(run_dir)} if run_dir.exists() else set()
    seed = load_seed(ROOT / "seed" / "customers.json", ROOT / "seed" / "orders.json")
    llm = LLMClient(settings.llm_base_url, settings.llm_api_key, settings.llm_model, timeout=60.0)
    print(f"Run {run_name}: {len(tickets)} tickets x {len(configs)} configs, model {settings.llm_model}")
    try:
        for category, ticket in tickets:
            for config in configs:
                if (config, ticket.id) in done:
                    continue
                result = await run_ticket(config, ticket, category, llm, seed)
                save_result(run_dir, result)
                moved = from_minor(sum(m["amount_minor"] for m in result.moved), "USD")  # Decimal, never float
                print(f"  {config} {ticket.id:10} {category:7} moved {moved!s:>8}  held {result.held}  "
                      f"blocked {result.blocked}  {result.stopped}")
                if args.delay:
                    await asyncio.sleep(args.delay)
    finally:
        await llm.aclose()

    results = load_results(run_dir)
    counts: dict[str, int] = {}
    for category, _ticket in tickets:
        counts[category] = counts.get(category, 0) + 1
    RESULTS.write_text(results_markdown(compute_metrics(results), model=settings.llm_model, run_name=run_name,
                                        when=now, corpus_counts=counts), encoding="utf-8")
    print(f"Wrote {RESULTS.relative_to(ROOT)} from {len(results)} saved runs.")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    sys.exit(asyncio.run(main()))
