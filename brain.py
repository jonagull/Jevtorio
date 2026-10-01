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
    "with drill lines and columns, and prefer building toward automation over doing the same chore by hand again."
)

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
    needs: dict[str, int] | Callable[[dict], dict[str, int]]  # callable when it depends on the map
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


def _collectable(b: dict) -> bool:
    """Not a chest the bot fills (module inputs), keeps full (coal feeds) or stores surplus in."""
    return not (b.get("role") or "").startswith(("input", "feed", "storage"))


def _output_waiting(obs: dict) -> bool:
    return any(
        sum(b.get("output", {}).values()) > 0
        or (b["type"] == "container" and _collectable(b) and b.get("contents"))
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
    per = builds.parts(builds.COAL_LINE)["burner-mining-drill"] if resource == "coal" else builds.COLUMN_DRILLS
    return sum(b["type"] == "mining-drill" for b in _has_role(obs, f"line:{resource}")) // per


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


def _build_on(resource: str, layout: list[dict], site: str | None = None):
    """Build stage 1 of `layout` on the nearest `resource`, grid-aligned; later stages get a reserved `site`."""
    def run(f: Factorio) -> str:
        builds.craft_all(f, builds.parts(builds.stage(layout, 1)))
        ore = f.lua(f'local e = nearest("{resource}", bot.position) if e then out({{x = e.position.x, y = e.position.y}}) end')
        if not ore:
            return f"no {resource} nearby"
        f.walk_to(ore["x"], ore["y"], stop_short=3)
        r = f.place_layout(layout, (ore["x"], ore["y"]), radius=24, label=f"{resource} line", site=site, grid=2)
        return f"built an automated {resource} line at ({r['x']}, {r['y']})" if r["ok"] else f"could not build a {resource} line: {r['reason']}"
    return run


def _columns_to_upgrade(obs: dict) -> list[dict]:
    return [s for s in obs.get("sites") or [] if s["kind"].startswith("column:") and not s["done"]]


def _column_layout(site: dict) -> list[dict]:
    return builds.smelt_column(site["kind"].split(":")[1])


COLUMN_UPGRADE = {**builds.parts(builds.stage(builds.smelt_column("iron-ore"), 2)), "coal": 24}


def _upgrade_column(f: Factorio) -> str:
    sites = _columns_to_upgrade(f.observe())
    if not sites:
        return "no column waiting for its belts"
    site = sites[0]
    builds.craft_all(f, builds.parts(builds.stage(_column_layout(site), 2)))
    f.walk_to(site["x"], site["y"], stop_short=3)
    r = f.place_layout(_column_layout(site), (site["x"], site["y"]), radius=0, stage=2, site=site["kind"],
                       ignore_reserved=True, label=site["kind"].split(":")[1] + " column")
    return f"added the fuel belt and plate output to the {site['kind'].split(':')[1]} column" if r["ok"] \
        else f"could not upgrade the column: {r['reason']}"


@dataclass
class Link:
    """A belt worth laying: from a source (coal line chest, or a column's plate output) to an entry that wants it."""
    kind: str  # "coal" or "plates"
    source: dict
    entry: dict
    start: tuple[float, float]
    feeder: tuple[float, float] | None  # burner inserter that lifts coal out of a chest onto the belt
    done: tuple[str, str]  # roles for source and entry once connected

    @property
    def belts(self) -> int:
        return int(abs(self.start[0] - (self.entry["x"] - 1)) + abs(self.start[1] - self.entry["y"])) + 1


def _dist(a: dict, b: dict) -> float:
    return abs(a["x"] - b["x"]) + abs(a["y"] - b["y"])


def _links(obs: dict, kind: str) -> list[Link]:
    built, links = obs["built"], []
    if kind == "coal":
        chests = [b for b in _has_role(obs, "line:coal") if b["type"] == "container"]
        for entry in (b for b in built if b.get("role") == "fuel-in"):
            if chests:
                c = min(chests, key=lambda c: _dist(c, entry))
                chests.remove(c)
                links.append(Link("coal", c, entry, (c["x"], c["y"] + 2), (c["x"], c["y"] + 1), ("feed:coal", "fuel-in:done")))
    else:
        for out in (b for b in _has_role(obs, "plates-out:") if not b["role"].endswith(":done")):
            plate = out["role"].split(":")[1]
            entries = [b for b in built if b.get("role") == f"plates-in:{plate}"]
            if entries:
                e = min(entries, key=lambda e: _dist(e, out))
                links.append(Link("plates", out, e, (out["x"] - 1, out["y"]), None, (out["role"] + ":done", e["role"] + ":done")))
    return links


def _link_needs(kind: str):
    def needs(obs: dict) -> dict[str, int]:
        links = _links(obs, kind)
        if not links:
            return {}
        return {"transport-belt": links[0].belts, **({"burner-inserter": 1} if links[0].feeder else {})}
    return needs


def _lay_link(kind: str):
    def build(f: Factorio) -> str:
        links = _links(f.observe(), kind)
        if not links:
            return f"nothing to connect with a {kind} belt"
        link = links[0]
        f.walk_to((link.source["x"] + link.entry["x"]) / 2, (link.source["y"] + link.entry["y"]) / 2)
        builds.craft_all(f, {"transport-belt": link.belts})
        r = f.lay_belt(link.start, link.entry, link.feeder, label=f"{kind} belt")
        if not r["ok"]:
            return f"could not lay a {kind} belt: {r['reason']}"
        f.set_role(link.source["x"], link.source["y"], link.done[0])
        f.set_role(link.entry["x"], link.entry["y"], link.done[1])
        return f"laid a {r['belts']}-belt {kind} line"
    return build


PLANS: dict[str, Plan] = {
    "coal_line": Plan(
        "a self-fuelling coal line: 2 burner drills and an inserter that keep each other fuelled and fill a chest with coal, "
        "so fuel no longer has to be mined by hand",
        {**builds.parts(builds.COAL_LINE), "coal": 6},
        _build_on("coal", builds.COAL_LINE),
        # One coal line for the bot, plus one to feed each smelting column's fuel belt.
        lambda o: "coal" in o["ores"] and _lines(o, "coal") < min(6, 1 + len(_has_role(o, "fuel-in"))),
    ),
    "copper_line": Plan(
        "an automated copper smelting column: 4 burner drills, each dropping straight into its own furnace "
        "(belts and inserters are added later with upgrade_column)",
        {**builds.parts(builds.stage(builds.smelt_column("copper-ore"), 1)), "coal": 20},
        _build_on("copper-ore", builds.smelt_column("copper-ore"), site="column:copper-ore"),
        # One staged column (older columns have no plate output to belt to the hub).
        lambda o: "copper-ore" in o["ores"] and _lines(o, "copper-ore") < 2
        and not any(s["kind"] == "column:copper-ore" for s in o.get("sites") or []),
    ),
    "iron_line": Plan(
        "an automated iron smelting column: 4 burner drills, each dropping straight into its own furnace "
        "(belts and inserters are added later with upgrade_column)",
        {**builds.parts(builds.stage(builds.smelt_column("iron-ore"), 1)), "coal": 20},
        _build_on("iron-ore", builds.smelt_column("iron-ore"), site="column:iron-ore"),
        lambda o: "iron-ore" in o["ores"] and _lines(o, "iron-ore") < 2,
    ),
    "column_upgrade": Plan(
        "the second stage of a smelting column: a coal belt loop with burner inserters fuelling every drill and furnace, "
        "and output inserters putting plates on a belt that can run to the red science module",
        COLUMN_UPGRADE,
        _upgrade_column,
        lambda o: bool(_columns_to_upgrade(o)),
    ),
    "coal_belt": Plan(
        "a coal belt from a coal line to a smelting column, so its drills and furnaces are fuelled automatically",
        _link_needs("coal"),
        _lay_link("coal"),
        lambda o: bool(_links(o, "coal")),
    ),
    "plate_belt": Plan(
        "a plate belt from a smelting column to the red science module, so plates arrive without being carried",
        _link_needs("plates"),
        _lay_link("plates"),
        lambda o: bool(_links(o, "plates")),
    ),
    "steam_power": Plan(
        "steam power at the nearest water: offshore pump, boiler and steam engine",
        {**builds.POWER_PARTS, "coal": 5},
        builds.build_power,
        lambda o: _researched(o, "electronics") and not _built(o, "steam-engine"),
    ),
    "boiler_feed": Plan(
        "a coal entry at the boiler (a burner inserter and one belt), so a coal belt can keep the steam power running",
        {"burner-inserter": 1, "transport-belt": 1},
        builds.feed_boiler,
        lambda o: bool(_built(o, "steam-engine")) and not _has_role(o, "boiler-feed"),
    ),
    "lab": Plan(
        "a lab next to the power plant, wired with poles (crafting it unlocks red science)",
        builds.module_needs(builds.LAB),
        lambda f: builds.build_module(f, "a lab", builds.LAB),
        lambda o: bool(_built(o, "steam-engine")) and not _built(o, "lab"),
    ),
    "red_science_module": Plan(
        "the automated red science module next to the power plant: a gear assembler feeds a red science assembler "
        "that inserts straight into a lab, fed by plate belts from the smelting columns",
        builds.module_needs(builds.RED_SCIENCE),
        lambda f: builds.build_module(f, "the red science module", builds.RED_SCIENCE),
        lambda o: _researched(o, "automation") and len(_has_role(o, "module:red")) + len(_has_role(o, "input:iron-plate")) < 2,
    ),
}

# Shortfalls worked out each step: every plan, plus a couple of one-off crafts.
# One-off crafts whose shortfall is also worked out each step.
# Ingredients, not the pack itself: a pack already in the inventory says nothing about crafting more.
EXTRA_SHORTFALLS = {"red_science_by_hand": {"iron-gear-wheel": 1, "copper-plate": 1}, "burner_drill": {"burner-mining-drill": 1}}


def _needs(plan: Plan, obs: dict) -> dict[str, int]:
    return plan.needs(obs) if callable(plan.needs) else plan.needs


def enrich(f: Factorio, obs: dict) -> dict:
    """Add what's still missing for each plan, so availability and descriptions can use it."""
    needs = {**{name: _needs(p, obs) for name, p in PLANS.items()}, **EXTRA_SHORTFALLS}
    obs["short"] = {name: builds.missing(f, n) for name, n in needs.items()}
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
        for b in obs["built"] if _collectable(b)
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
        if not builds.missing(f, _needs(plan, obs)):
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
    "stock_red_science_module": Action(
        "Carry iron and copper plates from the inventory to the red science module's input chests.",
        lambda o: bool(_has_role(o, "input:")) and (_count(o, "iron-plate") >= 10 or _count(o, "copper-plate") >= 10),
        _stock,
    ),
    "wait": Action("Do nothing for 10 seconds and let machines work.", lambda o: True, lambda f, o: time.sleep(10) or "waited 10 seconds"),
}


# ---- what Jev is told --------------------------------------------------------

def research_idle(obs: dict) -> bool:
    """Labs exist but nothing is being researched: main.py then asks Jev for the next technology."""
    return "current" not in obs["research"] and bool(_lab_techs(obs)) and bool(_built(obs, "lab"))


def milestone(obs: dict) -> str:
    if not _researched(obs, "electronics"):
        return "Smelt 10 copper plates (in any furnace) to unlock Electronics: circuits, labs, inserters and poles."
    if not _built(obs, "steam-engine"):
        return "Build steam power at water. It needs iron plates, a stone furnace, some coal and 2 small electric poles."
    if not _built(obs, "lab"):
        return "Build a lab next to the power plant. Crafting it unlocks the red science recipe."
    if not _researched(obs, "automation"):
        return "Research Automation: hand-craft 10 red science and put it in the lab (research starts by itself)."
    if not (_has_role(obs, "input:") or _has_role(obs, "module:red")):
        return "Build the automated red science module."
    return ("Connect smelting columns to the red science module with plate belts and coal lines to every fuel belt, "
            "keep everything fueled and keep research going.")


def _fmt(items: dict) -> str:
    return ", ".join(f"{n} {k}" for k, n in sorted(items.items())) or "nothing"


def describe(obs: dict, history: list[str]) -> str:
    """Plain-English state. Python does the counting so Jev doesn't have to."""
    lines = [GOAL, f"Current milestone: {milestone(obs)}", "", f"Inventory: {_fmt(obs['inventory'])}"]
    if not _count(obs, "coal"):
        lines.append("No coal in the inventory: smelting, refuelling and building burner machines all need coal.")

    for ore, info in obs["ores"].items():
        lines.append(f"Nearest {ore}: {info['distance']} tiles away.")
    if not _lines(obs, "iron-ore"):
        lines.append("No iron smelting column yet: nearly everything needs iron plates, so one should come first.")
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


def _failed(entry: str) -> bool:
    return any(w in entry for w in ("failed", "could not", "error:", "cannot", "no room"))


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
        # An action that just failed twice in a row is left out once, so a broken action can't loop forever.
        last = history[-2:]
        stuck = last[0].split(":")[0] if len(last) == 2 and last[0] == last[1] and _failed(last[0]) else None
        options = {name: a.describe(obs) for name, a in ACTIONS.items() if a.available(obs) and name != stuck}
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
