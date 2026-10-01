"""Turns game state into a question for Jev and runs the chosen action.

Jev only picks; it can't count well or write code. So Python does the arithmetic
(e.g. "what's still missing for a lab?") and only offers actions that are possible right now.
"""

import random
import time
from dataclasses import dataclass, field
from typing import Callable

from pydantic_ai.models.decision import ChoiceQuestion, DecisionRequest, ScoreQuestion
from pydantic_ai.models.typesafe import TypeSafeModel

import builds
from factorio import Factorio

GOAL = (
    "Goal: automate red science (automation science packs) and research with it. Every burner machine "
    "(drills, furnaces, burner inserters, the boiler) needs coal. Assemblers and labs need electricity from steam power. "
    "Play efficiently: hand-mining is slow, so automate any resource you keep needing (coal and copper especially) "
    "with drill lines, and prefer building toward automation over doing the same chore by hand again."
)

# What one smelting line needs (see Factorio.build_smelting_line).
LINE_PARTS = {"burner-mining-drill": 2, "transport-belt": 5, "burner-inserter": 2, "stone-furnace": 1, "iron-chest": 1}
RED_PACK = "automation-science-pack"
GATHERABLE = {"coal", "stone", "wood", "iron-plate", "copper-plate", "iron-ore", "copper-ore"}


@dataclass
class Action:
    description: str | Callable[[dict], str]
    available: Callable[[dict], bool]
    run: Callable[[Factorio, dict], str]

    def describe(self, obs: dict) -> str:
        return self.description(obs) if callable(self.description) else self.description


@dataclass
class Plan:
    """Something worth building. Jev can pursue it before it's affordable; Python does the gathering."""
    description: str
    needs: dict[str, int]
    build: Callable[[Factorio], str]
    wanted: Callable[[dict], bool]  # prerequisites met and not built enough yet


def _count(obs: dict, item: str) -> int:
    return obs["inventory"].get(item, 0)


def _affordable(obs: dict, plan: str) -> bool:
    return not obs["short"][plan]


def _built(obs: dict, name: str) -> list[dict]:
    return [b for b in obs["built"] if b["name"] == name]


def _has_role(obs: dict, prefix: str) -> list[dict]:
    return [b for b in obs["built"] if (b.get("role") or "").startswith(prefix)]


def _researched(obs: dict, tech: str) -> bool:
    return tech in obs["research"]["researched"]


def _lab_techs(obs: dict) -> dict[str, dict]:
    """Technologies that can be researched in a lab right now (not trigger techs)."""
    return {n: t for n, t in obs["research"]["available"].items() if not t.get("trigger")}


def _low_fuel(obs: dict) -> bool:
    return any(b.get("burner") and b["fuel"] < 3 for b in obs["built"])


def _output_waiting(obs: dict) -> bool:
    return any(
        sum(b.get("output", {}).values()) > 0
        or (b["type"] == "container" and not (b.get("role") or "").startswith("input") and b.get("contents"))
        for b in obs["built"]
    )


def _dead_drills(obs: dict) -> bool:
    return any(b["type"] == "mining-drill" and b["status"] == "no_minable_resources" for b in obs["built"])


def _loose_furnace_for(obs: dict, ore: str) -> bool:
    """Have ore and coal, and either a furnace outside a smelting line that can take this ore, or the means to make one."""
    free = any(
        b["name"] == "stone-furnace" and not (b.get("role") or "").startswith("line") and set(b.get("input", {})) <= {ore}
        for b in obs["built"]
    )
    return _count(obs, ore) >= 1 and _count(obs, "coal") >= 1 and (
        free or _count(obs, "stone-furnace") >= 1 or _count(obs, "stone") >= 5
    )


def _lines(obs: dict, resource: str) -> int:
    return sum(b["type"] == "mining-drill" for b in _has_role(obs, f"line:{resource}")) // 2


# ---- action implementations ----------------------------------------------------

def _mine(resource: str, n: int):
    def run(f: Factorio, obs: dict) -> str:
        return f"got {f.mine(resource, n)} {resource}"
    return run


def _craft(item: str, n: int = 1):
    def run(f: Factorio, obs: dict) -> str:
        made = f.craft_deep(item, n)
        return f"crafted {made} {item}" if made else f"failed to craft {item}"
    return run


def _place_drill(f: Factorio, obs: dict) -> str:
    r = f.place_drill_on("iron-ore")
    if not r["ok"]:
        return f"could not place drill: {r['reason']}"
    return "placed a drill on iron" + (" with a furnace at its output" if r["furnace"] else " (no furnace, ore drops on the ground)")


def _smelt(ore: str):
    def run(f: Factorio, obs: dict) -> str:
        loaded = f.load_furnaces(ore)
        if not loaded:  # no free furnace: put down a new one
            if _count(obs, "stone-furnace") < 1:
                f.craft("stone-furnace")
            f.place_furnace_here()
            loaded = f.load_furnaces(ore)
        fuel = f.refuel_all()
        return f"loaded {loaded} {ore} into furnaces (added {fuel} coal)"
    return run


def _feed_labs(f: Factorio, obs: dict) -> str:
    return f"put {f.feed_labs(RED_PACK)} red science into labs"


def _stock(f: Factorio, obs: dict) -> str:
    moved = f.stock_inputs()
    return "stocked " + (", ".join(f"{n} {k}" for k, n in moved.items() if n) or "nothing (no matching plates)")


def _build_line(resource: str):
    def run(f: Factorio) -> str:
        builds.craft_all(f, LINE_PARTS)
        r = f.build_smelting_line(resource, drills=2)
        if not r["ok"]:
            return f"could not build a {resource} line: {r['reason']}"
        return f"built an automated {resource} smelting line at ({r['x']}, {r['y']})"
    return run


def _build_coal_line(f: Factorio) -> str:
    builds.craft_all(f, builds.parts(builds.COAL_LINE))
    coal = f.lua('local e = nearest("coal", bot.position) if e then out({x = e.position.x, y = e.position.y}) end')
    if not coal:
        return "no coal nearby"
    f.walk_to(coal["x"], coal["y"], stop_short=3)
    r = f.place_layout(builds.COAL_LINE, (coal["x"], coal["y"]), radius=24)
    return f"built an automated coal line at ({r['x']}, {r['y']})" if r["ok"] else f"could not build a coal line: {r['reason']}"


PLANS: dict[str, Plan] = {
    "coal_line": Plan(
        "an automated coal line: 2 burner drills on coal that fill a chest with coal, so fuel no longer has to be mined by hand",
        {"burner-mining-drill": 2, "iron-chest": 1, "coal": 4},
        _build_coal_line,
        lambda o: "coal" in o["ores"] and _lines(o, "coal") < 2,
    ),
    "copper_line": Plan(
        "an automated copper smelting line: 2 burner drills put copper ore on a belt into a furnace, plates collect in a chest",
        {**LINE_PARTS, "coal": 10},
        _build_line("copper-ore"),
        lambda o: "copper-ore" in o["ores"] and _lines(o, "copper-ore") < 2,
    ),
    "iron_line": Plan(
        "another automated iron smelting line: 2 burner drills put iron ore on a belt into a furnace, plates collect in a chest",
        {**LINE_PARTS, "coal": 10},
        _build_line("iron-ore"),
        lambda o: "iron-ore" in o["ores"] and _lines(o, "iron-ore") < 3,
    ),
    "steam_power": Plan(
        "steam power at the nearest water: offshore pump, boiler and steam engine",
        {**builds.POWER_PARTS, "coal": 5},
        builds.build_power,
        lambda o: _researched(o, "electronics") and not _built(o, "steam-engine"),
    ),
    "lab": Plan(
        "a lab next to the power plant, wired with poles (crafting it unlocks red science)",
        builds.module_needs(builds.LAB),
        lambda f: builds.build_module(f, "a lab", builds.LAB),
        lambda o: bool(_built(o, "steam-engine")) and not _built(o, "lab"),
    ),
    "red_science_module": Plan(
        "the automated red science module next to the power plant: a gear assembler feeds a red science assembler "
        "that inserts straight into a lab, fed with plates through two chests",
        builds.module_needs(builds.RED_SCIENCE),
        lambda f: builds.build_module(f, "the red science module", builds.RED_SCIENCE),
        lambda o: _researched(o, "automation") and not _has_role(o, "input:"),
    ),
}

# Shortfalls worked out each step: every plan, plus a couple of one-off crafts.
SHORTFALLS = {**{name: p.needs for name, p in PLANS.items()}, "red_science_by_hand": {RED_PACK: 1}, "burner_drill": {"burner-mining-drill": 1}}


def enrich(f: Factorio, obs: dict) -> dict:
    """Add what's still missing for each plan, so availability and descriptions can use it."""
    obs["short"] = {name: builds.missing(f, needs) for name, needs in SHORTFALLS.items()}
    return obs


GATHER_ORDER = ["coal", "stone", "wood", "copper-plate", "iron-plate", "copper-ore", "iron-ore"]


def _gather_step(f: Factorio, obs: dict, item: str, amount: int) -> str:
    """One step toward having `amount` more of `item`."""
    n = max(10, min(amount, 30))
    if item in ("coal", "stone", "iron-ore", "copper-ore"):
        coal_chest = [b for b in _has_role(obs, "line:coal") if b["type"] == "container" and b.get("contents", {}).get("coal")]
        if item == "coal" and coal_chest:
            return f"collected {f.collect_output()} items (coal from the coal line)"
        return f"mined {f.mine(item, n)} {item}"
    if item == "wood":
        return f"got {f.mine('wood', n)} wood"
    plate, ore = item, item.replace("-plate", "-ore")
    ready = any(
        b.get("output", {}).get(plate) or b.get("contents", {}).get(plate)
        for b in obs["built"] if not (b.get("role") or "").startswith("input")
    )
    if ready:
        return f"collected {f.collect_output()} items"
    if _count(obs, ore) >= 5 and _loose_furnace_for(obs, ore):
        return _smelt(ore)(f, obs)
    return f"mined {f.mine(ore, n)} {ore} to smelt"


def _pursue(name: str):
    plan = PLANS[name]

    def run(f: Factorio, obs: dict) -> str:
        short = obs["short"][name]
        if not short:
            return plan.build(f)
        item = next(i for i in GATHER_ORDER if i in short)
        step = _gather_step(f, obs, item, short[item])
        if not builds.missing(f, plan.needs):
            return f"{step}, then {plan.build(f)}"
        return f"{step} (toward {name.replace('_', ' ')})"
    return run


def _pursue_description(name: str):
    plan = PLANS[name]

    def describe(obs: dict) -> str:
        short = obs["short"][name]
        if not short:
            return f"Build {plan.description}. Everything needed is available now."
        return f"Work toward {plan.description}. Gathers what's missing ({_fmt(short)}) and builds it once complete."
    return describe


def _pursuable(name: str):
    plan = PLANS[name]
    return lambda o: plan.wanted(o) and set(o["short"][name]) <= GATHERABLE


ACTIONS: dict[str, Action] = {
    **{
        f"automate_{name}" if name.endswith("line") else f"build_{name}":
            Action(_pursue_description(name), _pursuable(name), _pursue(name))
        for name in PLANS
    },
    "mine_iron_ore": Action("Walk to iron ore and mine 10 by hand.", lambda o: "iron-ore" in o["ores"], _mine("iron-ore", 10)),
    "mine_copper_ore": Action("Walk to copper ore and mine 10 by hand.", lambda o: "copper-ore" in o["ores"], _mine("copper-ore", 10)),
    "mine_coal": Action("Walk to coal and mine 10 by hand. Coal fuels every burner machine.", lambda o: "coal" in o["ores"], _mine("coal", 10)),
    "mine_stone": Action("Walk to stone and mine 10 by hand. Stone makes furnaces.", lambda o: "stone" in o["ores"], _mine("stone", 10)),
    "chop_wood": Action("Cut down trees for about 12 wood. Wood makes small electric poles.", lambda o: True, _mine("wood", 12)),
    "smelt_iron_ore": Action(
        "Put iron ore from the inventory into a furnace so it becomes iron plates.",
        lambda o: _loose_furnace_for(o, "iron-ore"),
        _smelt("iron-ore"),
    ),
    "smelt_copper_ore": Action(
        "Put copper ore from the inventory into a furnace so it becomes copper plates.",
        lambda o: _loose_furnace_for(o, "copper-ore"),
        _smelt("copper-ore"),
    ),
    "relocate_dead_drills": Action(
        "Pick up drills whose ore has run out so they can be placed on fresh ore.", _dead_drills,
        lambda f, o: f"picked up {f.pick_up_dead_drills()} dead drills",
    ),
    "refuel_machines": Action(
        "Walk around and add coal to every burner machine (drills, furnaces, inserters, boiler) that is low on fuel.",
        lambda o: _count(o, "coal") >= 1 and _low_fuel(o),
        lambda f, o: f"added {f.refuel_all()} coal to machines",
    ),
    "collect_plates": Action(
        "Collect finished plates and coal from furnaces and automated line chests.", _output_waiting,
        lambda f, o: f"collected {f.collect_output()} items",
    ),
    "craft_red_science": Action(
        "Hand-craft 10 red science packs (each is 1 copper plate and 1 iron gear).",
        # Only to bootstrap Automation; after that the module makes them and hand-crafting eats its copper.
        lambda o: _affordable(o, "red_science_by_hand") and not _researched(o, "automation"),
        _craft(RED_PACK, 10),
    ),
    "feed_labs": Action(
        "Put red science packs from the inventory into the labs so they can research.",
        lambda o: _count(o, RED_PACK) >= 1 and bool(_built(o, "lab")),
        _feed_labs,
    ),
    "start_research": Action(
        "Choose a technology for the labs to research.",
        lambda o: "current" not in o["research"] and bool(_lab_techs(o)) and bool(_built(o, "lab")),
        lambda f, o: "",  # filled in by Brain, which asks Jev which technology
    ),
    "stock_red_science_module": Action(
        "Carry iron and copper plates from the inventory to the red science module's input chests.",
        lambda o: bool(_has_role(o, "input:")) and (_count(o, "iron-plate") >= 10 or _count(o, "copper-plate") >= 10),
        _stock,
    ),
    "wait": Action("Do nothing for 10 seconds and let machines work.", lambda o: True, lambda f, o: time.sleep(10) or "waited 10 seconds"),
}


# ---- what Jev is told --------------------------------------------------------

def milestone(obs: dict) -> str:
    if not _researched(obs, "electronics"):
        return "Smelt 10 copper plates (in any furnace) to unlock Electronics: circuits, labs, inserters and poles."
    if not _built(obs, "steam-engine"):
        return "Build steam power at water. It needs iron plates, a stone furnace, some coal and 2 small electric poles."
    if not _built(obs, "lab"):
        return "Build a lab next to the power plant. Crafting it unlocks the red science recipe."
    if not _researched(obs, "automation"):
        return "Research Automation: hand-craft 10 red science, put it in the lab and start the research."
    if not _has_role(obs, "input:"):
        return "Build the automated red science module."
    return "Keep the red science module stocked with iron and copper plates, keep everything fueled and keep research going."


def _fmt(items: dict) -> str:
    return ", ".join(f"{n} {k}" for k, n in sorted(items.items())) or "nothing"


def describe(obs: dict, history: list[str]) -> str:
    """Plain-English state. Python does the counting so Jev doesn't have to."""
    lines = [GOAL, f"Current milestone: {milestone(obs)}", "", f"Inventory: {_fmt(obs['inventory'])}"]
    if not _count(obs, "coal"):
        lines.append("No coal in the inventory: smelting, refuelling and building burner machines all need coal.")

    for ore, info in obs["ores"].items():
        lines.append(f"Nearest {ore}: {info['distance']} tiles away.")
    lines.append("Automated lines: " + ", ".join(f"{_lines(obs, r)} {r.replace('-ore', '')}" for r in ("iron-ore", "copper-ore", "coal")) + ".")

    research = obs["research"]
    if "current" in research:
        lines.append(f"Researching {research['current']['name']}: {research['current']['progress']:.0%} done.")
    else:
        lines.append("Nothing is being researched.")
    for name, t in research["available"].items():
        if t.get("trigger"):
            trig = t["trigger"]
            item = trig.get("item", {}).get("name", "?") if isinstance(trig.get("item"), dict) else trig.get("item")
            lines.append(f"Technology {name} unlocks by itself after producing {trig.get('count', 1)} {item}.")

    for plan, short in obs["short"].items():
        lines.append(f"Still missing for {plan.replace('_', ' ')}: {_fmt(short) if short else 'nothing, it can be made now'}.")

    groups: dict[tuple[str, str], int] = {}
    for b in obs["built"]:
        if b["type"] not in ("transport-belt", "electric-pole", "container"):
            groups[(b["name"], b["status"])] = groups.get((b["name"], b["status"]), 0) + 1
    for (name, status), n in sorted(groups.items()):
        lines.append(f"{n} {name}: {status.replace('_', ' ')}.")
    for b in obs["built"]:
        if b["type"] in ("container", "lab"):
            role = b.get("role") or ""
            label = role.replace("input:", "input chest for ") if role.startswith("input:") else "output chest"
            lines.append(f"{b['name'] if b['type'] == 'lab' else label} holds: {_fmt(b.get('contents', {}))}.")
    if _low_fuel(obs):
        lines.append("Some burner machines are low on fuel.")

    if history:
        lines += ["", "Recent actions (oldest first):"] + [f"- {h}" for h in history[-6:]]
    return "\n".join(lines)


BOTTLENECKS = {
    "fuel": "Not enough coal for the burner machines.",
    "iron_plates": "Not enough iron plates.",
    "copper_plates": "Not enough copper plates.",
    "stone": "Not enough stone for furnaces.",
    "wood": "Not enough wood for power poles.",
    "power": "No electricity, or not enough.",
    "research": "Research is stalled: no research running or no science packs in the labs.",
    "none": "Nothing is blocking progress right now.",
}

PROGRESS_LEVELS = [
    "No copper plates yet; Electronics is locked.",
    "Electronics unlocked, but no power plant.",
    "Power and a lab are built.",
    "Automation is researched (assemblers unlocked).",
    "The automated red science module is running and researching.",
]


@dataclass
class Thought:
    """Everything Jev said about one moment in the game."""
    state: str
    choice: str
    probabilities: dict[str, float]
    bottleneck: str = "?"
    bottleneck_probabilities: dict[str, float] = field(default_factory=dict)
    progress: float = 0.0


class Brain:
    def __init__(self, use_jev: bool = True):
        self.model = TypeSafeModel("jev-latest") if use_jev else None

    async def think(self, obs: dict, history: list[str]) -> Thought:
        state = describe(obs, history)
        options = {name: a.describe(obs) for name, a in ACTIONS.items() if a.available(obs)}
        if self.model is None:
            pick = random.choice(list(options))
            return Thought(state, pick, {name: 1.0 if name == pick else 0.0 for name in options})
        # One request, several questions: the action, plus diagnostics that show how Jev reads the situation.
        request = DecisionRequest(
            state=state,
            questions={
                "action": ChoiceQuestion(
                    instructions="You are playing Factorio. Which single action makes the most progress toward the current milestone right now?",
                    criteria=options,
                ),
                "bottleneck": ChoiceQuestion(
                    instructions="What is the biggest thing holding back progress right now?",
                    criteria=BOTTLENECKS,
                ),
                "progress": ScoreQuestion(
                    instructions="How far along is the base toward automated red science?",
                    criteria=PROGRESS_LEVELS,
                ),
            },
        )
        answers = (await self.model.decide(request, {})).answers
        action, bottleneck = answers["action"], answers["bottleneck"]
        return Thought(
            state=state,
            choice=action.choice,
            probabilities=action.probabilities,
            bottleneck=bottleneck.choice,
            bottleneck_probabilities=bottleneck.probabilities,
            progress=answers["progress"].score,
        )

    async def pick_research(self, obs: dict) -> tuple[str, dict[str, float]]:
        techs = {
            name: f"Unlocks {', '.join(t['unlocks']) or 'bonuses'}; costs {t['units']} x {', '.join(t['packs'])}."
            for name, t in _lab_techs(obs).items()
        }
        if self.model is None:
            pick = random.choice(list(techs))
            return pick, {pick: 1.0}
        request = DecisionRequest(
            state=describe(obs, []),
            questions={"tech": ChoiceQuestion(
                instructions="Which technology should the labs research next to best reach the goal?", criteria=techs,
            )},
        )
        answer = (await self.model.decide(request, {})).answers["tech"]
        return answer.choice, answer.probabilities
