"""Let Jev play Factorio.

    python main.py              # Jev decides (needs TYPESAFE_API_KEY, env or .env)
    python main.py --random     # random valid actions, no API calls (for testing)
    python main.py --reset      # remove the bot and everything it built
"""

import argparse
import asyncio
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from brain import ACTIONS, Brain, enrich
from factorio import Factorio, show_thought

LOG = Path(__file__).with_name("thoughts.jsonl")


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
    parser.add_argument("--steps", type=int, default=50)
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

    for step in range(1, args.steps + 1):
        for tech in game.check_triggers():
            print(f"  ★ unlocked {tech}")
            game.say(f"unlocked [technology={tech}]")
            history.append(f"unlocked technology {tech}")
        obs = enrich(game, game.observe())
        thought = await brain.think(obs, history)
        print_thought(step, obs, thought, verbose=not args.quiet)
        show_thought(game, step, thought)
        game.say(f"{thought.choice.replace('_', ' ')} ({thought.probabilities[thought.choice]:.0%} sure)")

        if thought.choice == "start_research":
            tech, tech_probs = await brain.pick_research(obs)
            print("  research options: " + ", ".join(f"{k} {v:.0%}" for k, v in sorted(tech_probs.items(), key=lambda kv: -kv[1])))
            ok = game.start_research(tech)
            result = f"started researching {tech}" if ok else f"could not start researching {tech}"
        else:
            result = ACTIONS[thought.choice].run(game, obs)
        print(f"  → {result}")
        show_thought(game, step, thought, result)
        history.append(f"{thought.choice}: {result}")
        with LOG.open("a") as log:
            log.write(json.dumps({"time": time.time(), "step": step, "tick": obs["tick"],
                                  **asdict(thought), "result": result}) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
