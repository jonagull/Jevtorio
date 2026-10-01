"""Let Jev play Factorio.

    python main.py              # Jev decides (needs TYPESAFE_API_KEY, env or .env)
    python main.py --steps 0    # run forever
    python main.py --random     # random valid actions, no API calls (for testing)
    python main.py --reset      # remove the bot and everything it built
"""

import argparse
import asyncio
import itertools
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from brain import ACTIONS, Brain, enrich, milestone, research_idle
from factorio import Factorio, LuaError, show_thought

LOG = Path(__file__).with_name("thoughts.jsonl")
TEXT_LOG = Path(__file__).with_name("jev.log")  # one line per step: tail -f jev.log


def load_dotenv(path: Path = Path(__file__).with_name(".env")):
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def print_thought(step: int, obs: dict, thought, verbose: bool):
    print(f"\n\033[1m━━ step {step} ━━ tick {obs['tick']}\033[0m")
    if verbose:
        print("\033[2m" + "\n".join("  │ " + line for line in thought.state.splitlines()) + "\033[0m")
    print(f"  bottleneck: {thought.bottleneck}   progress: {thought.progress:.1f}/4")
    for name, p in sorted(thought.probabilities.items(), key=lambda kv: -kv[1]):
        mark = "▶" if name == thought.choice else " "
        print(f"  {mark} {name:<22} {'█' * round(p * 30):<30} {p:4.0%}")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--steps", type=int, default=50, help="0 = run forever")
    parser.add_argument("--random", action="store_true", help="pick random valid actions instead of asking Jev")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="don't print what Jev sees each step")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=27015)
    parser.add_argument("--password", default="secret")
    args = parser.parse_args()

    load_dotenv()
    game = Factorio(args.host, args.port, args.password)
    if args.reset:
        game.reset()
        print("Bot and its buildings removed.")
        return

    if game.spawn()["spawned"]:
        game.say("Hello! I'm Jev. Time to automate.")
    brain = Brain(use_jev=not args.random)
    history: list[str] = []
    goal = None

    for step in itertools.count(1) if args.steps == 0 else range(1, args.steps + 1):
        for tech in game.check_triggers():
            print(f"  ★ unlocked {tech}")
            game.say(f"unlocked [technology={tech}]")
            history.append(f"unlocked technology {tech}")
        if stashed := game.stash_excess():
            print(f"  inventory nearly full: put {stashed} items in storage chests")
        obs = enrich(game, game.observe())
        if milestone(obs) != goal:
            if goal:
                print(f"  ★ milestone done: {goal}")
                game.say(f"milestone done: {goal}")
                history.append(f"milestone done: {goal}")
            goal = milestone(obs)
        if research_idle(obs):
            tech, tech_probs = await brain.pick_research(obs)
            print("  research options: " + ", ".join(f"{k} {v:.0%}" for k, v in sorted(tech_probs.items(), key=lambda kv: -kv[1])))
            if game.start_research(tech):
                game.say(f"researching [technology={tech}]")
                history.append(f"started researching {tech}")
        thought = await brain.think(obs, history)
        print_thought(step, obs, thought, verbose=not args.quiet)
        show_thought(game, step, thought)
        game.say(f"{thought.choice.replace('_', ' ')} ({thought.probabilities[thought.choice]:.0%} sure) [gps={obs['x']:.0f},{obs['y']:.0f}]")

        try:
            result = ACTIONS[thought.choice].run(game, obs)
        except LuaError as e:  # one failed action shouldn't end a long run
            result = f"error: {e}"
        print(f"  → {result}")
        show_thought(game, step, thought, result)
        history.append(f"{thought.choice}: {result}")
        made = obs["made"]
        with LOG.open("a") as log:
            log.write(json.dumps({"time": time.time(), "step": step, "tick": obs["tick"], "milestone": goal, "made": made,
                                  **asdict(thought), "result": result}) + "\n")
        with TEXT_LOG.open("a") as log:
            log.write(f"{time.strftime('%H:%M:%S')} step {step} | {thought.choice} | {result} | made: "
                      f"{made['iron-plate']} iron, {made['copper-plate']} copper, {made['automation-science-pack']} red\n")


if __name__ == "__main__":
    asyncio.run(main())
