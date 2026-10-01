"""Multi-entity modules the bot can build: what they need and how to place them.

Layouts are lists of entities relative to an origin (1x1 and 3x3 entities sit on tile
centres, x.5; 2x2 on tile corners). Inserter `dir` is the side it picks up from.
"""

import json
import math

from factorio import Factorio, _lua_str

# Lab with its own pole.
LAB = [
    {"name": "lab", "x": 0.5, "y": 0.5},
    {"name": "small-electric-pole", "x": 2.5, "y": 0.5},
]

# Automated red science, fed by plate belts from the smelting columns:
#
#       p c i >>>>e        e = belt entry (a plate belt from a column ends here), > v = belt
#  e     i                 i = inserter, G = gear assembler, R = red science assembler, L = lab
#  v i GGG i RRR i LLL     c = sink chest: plates (and coal) that reach the end of a belt pile up
#  v   GGG   RRR   LLL         here, so a belt never jams; the bot collects from it
#  v   GGG   RRR   LLL     p = pole
#  v p     p     p
#  i
#  c
RED_SCIENCE = [
    {"name": "transport-belt", "x": 0.5, "y": -1.5, "dir": "south", "role": "plates-in:iron-plate"},
    *({"name": "transport-belt", "x": 0.5, "y": y + 0.5, "dir": "south"} for y in range(-1, 3)),
    {"name": "burner-inserter", "x": 0.5, "y": 3.5, "dir": "north"},
    {"name": "iron-chest", "x": 0.5, "y": 4.5, "role": "line:hub"},
    {"name": "inserter", "x": 1.5, "y": 0.5, "dir": "west"},
    {"name": "assembling-machine-1", "x": 3.5, "y": 0.5, "recipe": "iron-gear-wheel", "role": "module:red"},
    {"name": "inserter", "x": 5.5, "y": 0.5, "dir": "west"},
    {"name": "assembling-machine-1", "x": 7.5, "y": 0.5, "recipe": "automation-science-pack"},
    {"name": "inserter", "x": 9.5, "y": 0.5, "dir": "west"},
    {"name": "lab", "x": 11.5, "y": 0.5},
    {"name": "transport-belt", "x": 5.5, "y": -2.5, "dir": "east", "role": "plates-in:copper-plate"},
    *({"name": "transport-belt", "x": x + 0.5, "y": -2.5, "dir": "east"} for x in range(6, 9)),
    {"name": "burner-inserter", "x": 9.5, "y": -2.5, "dir": "west"},
    {"name": "iron-chest", "x": 10.5, "y": -2.5, "role": "line:hub"},
    {"name": "inserter", "x": 7.5, "y": -1.5, "dir": "north"},
    {"name": "small-electric-pole", "x": 1.5, "y": 2.5},
    {"name": "small-electric-pole", "x": 5.5, "y": 2.5},
    {"name": "small-electric-pole", "x": 9.5, "y": 2.5},
    {"name": "small-electric-pole", "x": 4.5, "y": -2.5},
]

# Burner drill drop points relative to its centre (vector_to_place_result, rotated).
DRILL_DROP = {"north": (-0.5, -1.3), "east": (1.3, -0.5), "south": (0.5, 1.3), "west": (-1.3, 0.5)}

# Self-fuelling coal pair: nothing here needs refuelling by hand.
#      BB         B = drill facing south, drops into the chest
#   AA BB         A = drill facing east, drops into B's fuel slot
#   AA ic         i = burner inserter, takes coal from the chest into A (and fuels itself)
# B's surplus piles up in the chest for the bot to collect.
COAL_LINE = [
    {"name": "burner-mining-drill", "x": 0, "y": 0, "dir": "south", "on": "coal", "role": "line:coal"},
    {"name": "burner-mining-drill", "x": -2, "y": 1, "dir": "east", "on": "coal", "role": "line:coal"},
    {"name": "iron-chest", "x": 0.5, "y": 1.5, "role": "line:coal"},
    {"name": "burner-inserter", "x": -0.5, "y": 1.5, "dir": "east", "role": "line:coal"},
]

COLUMN_DRILLS = 4


def smelt_column(ore: str, n: int = COLUMN_DRILLS) -> list[dict]:
    """Drills each dropping straight into its own furnace (0.25 ore/s fits one furnace's 0.31/s),
    with a coal belt looping round so burner inserters keep every drill and furnace fuelled.
    Output inserters put plates on the same belt; its far end is where a belt to the hub starts.
    Built in two stages: drills and furnaces first (cheap), belts and inserters (stage 2) once iron flows.

        e>>>>>>>>v     e = fuel entry: a coal belt from a coal line ends here (see Factorio.connect_fuel)
         i i i i v     i = burner inserter, takes coal off the belt
         DD DD DD v
         DD DD DD v    D = drill, drops ore straight into the furnace below
         FF FF FF v
         FF FF FF v    F = furnace
         ii ii ii v    i = fuel inserter (left) and plate output inserter (right) under each furnace
        o<<<<<<<<<     o = plates out: coal and plates end up on separate lanes
    """
    role = f"line:{ore}"
    right = 2 * n - 0.5
    layout = [e for k in range(n) for e in (
        {"name": "burner-mining-drill", "x": 2 * k, "y": 0, "dir": "south", "on": ore, "role": role},
        {"name": "stone-furnace", "x": 2 * k, "y": 2, "role": role},
        {"name": "burner-inserter", "x": 2 * k - 0.5, "y": -1.5, "dir": "north", "role": role, "stage": 2},
        {"name": "burner-inserter", "x": 2 * k - 0.5, "y": 3.5, "dir": "south", "role": role, "stage": 2},
        {"name": "burner-inserter", "x": 2 * k + 0.5, "y": 3.5, "dir": "north", "role": role, "stage": 2},
    )]
    belt = [(x + 0.5, -2.5, "east") for x in range(-2, 2 * n - 1)]  # along the top, from the entry
    belt += [(right, y + 0.5, "south") for y in range(-3, 4)]  # down the right side
    belt += [(x + 0.5, 4.5, "west") for x in range(2 * n - 1, -3, -1)]  # back along the bottom
    for x, y, d in belt:
        layout.append({"name": "transport-belt", "x": x, "y": y, "dir": d, "role": role, "stage": 2})
    layout[5 * n]["role"] = "fuel-in"  # top-left belt: where the coal arrives
    layout[-1]["role"] = f"plates-out:{ore.replace('-ore', '-plate')}"  # bottom-left belt: where plates leave
    return layout


POWER_PARTS = {"offshore-pump": 1, "boiler": 1, "steam-engine": 1, "small-electric-pole": 2}
SPARE_POLES = 6  # for the pole line from the power plant to a module


def stage(layout: list[dict], n: int) -> list[dict]:
    return [e for e in layout if e.get("stage", 1) == n]


# Module slots: a tidy row of 16-tile blocks around the power plant, nearest first.
SLOTS = sorted(((dx * 16 * i, dy * 16 * i) for i in range(1, 6) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))),
               key=lambda o: abs(o[0]) + abs(o[1]))


def parts(layout: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in layout:
        counts[e["name"]] = counts.get(e["name"], 0) + 1
    return counts


def module_needs(layout: list[dict]) -> dict[str, int]:
    needs = parts(layout)
    needs["small-electric-pole"] = needs.get("small-electric-pole", 0) + SPARE_POLES
    return needs


def missing(f: Factorio, needs: dict[str, int]) -> dict[str, int]:
    """Raw materials still to gather for `needs`, crafting intermediates from what's in the inventory.

    Items whose recipe isn't unlocked yet come back as "<item> (locked)".
    """
    return f.lua(f"""
        local needs = helpers.json_to_table({_lua_str(needs)})
        local have = inv(bot.get_main_inventory())
        local short = {{}}
        local function need(item, n)
          local h = have[item] or 0
          local use = math.min(h, n)
          have[item] = h - use
          n = n - use
          if n <= 0 then return end
          if handcraftable(item) then
            local r = prototypes.recipe[item]
            local outn = 1
            for _, p in pairs(r.products) do if p.name == item then outn = p.amount end end
            local crafts = math.ceil(n / outn)
            for _, ing in pairs(r.ingredients) do need(ing.name, ing.amount * crafts) end
            have[item] = (have[item] or 0) + crafts * outn - n
          else
            local key = (prototypes.recipe[item] and HAND[prototypes.recipe[item].category]) and (item .. " (locked)") or item
            short[key] = (short[key] or 0) + n
          end
        end
        for item, n in pairs(needs) do need(item, n) end
        out(short)
    """) or {}


def craft_all(f: Factorio, needs: dict[str, int]):
    """Hand-craft everything in `needs` that isn't already in the inventory."""
    inv = f.observe()["inventory"]
    per_craft = f.lua(f"""
        local r = {{}}
        for item in pairs(helpers.json_to_table({_lua_str(needs)})) do
          local rec = prototypes.recipe[item]
          r[item] = 1
          if rec then for _, p in pairs(rec.products) do if p.name == item then r[item] = p.amount end end end
        end
        out(r)
    """)
    for item, n in needs.items():
        short = n - inv.get(item, 0)
        if short > 0:
            f.craft_deep(item, math.ceil(short / per_craft[item]))


def build_power(f: Factorio) -> str:
    """Steam power at the nearest water: offshore pump -> boiler -> steam engine, plus a pole."""
    craft_all(f, POWER_PARTS)
    water = f.lua("""
        local tiles = S.find_tiles_filtered{name = {"water", "deepwater"}, position = bot.position, radius = 250, limit = 4000}
        local best, bd
        for _, t in pairs(tiles) do
          local d = (t.position.x - bot.position.x)^2 + (t.position.y - bot.position.y)^2
          if not bd or d < bd then best, bd = t.position, d end
        end
        if best then out(best) end
    """)
    if not water:
        return "no water within 250 tiles"
    f.walk_to(water["x"], water["y"], stop_short=4)
    r = f.lua(f"""
        for name, n in pairs({{["offshore-pump"] = 1, boiler = 1, ["steam-engine"] = 1, ["small-electric-pole"] = 1}}) do
          if bot.get_item_count(name) < n then out({{ok = false, reason = "missing " .. name}}) return end
        end
        local manual = defines.build_check_type.manual
        local dirs = {{"north", "east", "south", "west"}}
        local function linked(a, idx, b)
          for _, fb in pairs(a.fluidbox.get_connections(idx)) do if fb.owner == b then return true end end
          return false
        end
        local function attach(name, near, radius, ok)
          for r = 0, radius do for dx = -r, r do for dy = -r, r do
            if math.max(math.abs(dx), math.abs(dy)) == r then
              for _, d in pairs(dirs) do
                local p = {{near.x + dx, near.y + dy}}
                if S.can_place_entity{{name = name, position = p, direction = defines.direction[d], force = "player", build_check_type = manual}} then
                  local e = S.create_entity{{name = name, position = p, direction = defines.direction[d], force = "player"}}
                  if ok(e) then return e end
                  e.destroy()
                end
              end
            end
          end end end
        end
        local W = {{x = {water['x']}, y = {water['y']}}}
        local plant
        for r = 0, 12 do for dx = -r, r do for dy = -r, r do
          if not plant and math.max(math.abs(dx), math.abs(dy)) == r then
            local here = {{x = W.x + dx + 0.5, y = W.y + dy + 0.5}}
            local pump = attach("offshore-pump", here, 0, function() return true end)
            if pump then
              local out1 = pump.fluidbox.get_pipe_connections(1)[1].target_position
              local boiler = attach("boiler", out1, 4, function(b) return linked(pump, 1, b) end)
              local engine = boiler and attach("steam-engine", boiler.position, 5, function(e) return linked(boiler, 2, e) end)
              if engine then plant = {{pump, boiler, engine}} else
                if boiler then boiler.destroy() end
                pump.destroy()
              end
            end
          end
        end end end
        if not plant then out({{ok = false, reason = "could not fit pump, boiler and engine on this shore"}}) return end
        local engine = plant[3]
        local pole = attach("small-electric-pole", engine.position, 5, function(p)
          return p.electric_network_id ~= nil and engine.is_connected_to_electric_network()
        end)
        if not pole then for _, e in pairs(plant) do e.destroy() end out({{ok = false, reason = "no spot for a pole"}}) return end
        for _, e in pairs({{plant[1], plant[2], plant[3], pole}}) do
          bot.remove_item{{name = e.name, count = 1}}
          track(e)
        end
        J.roles[pole.unit_number] = "power"
        J.base = {{x = engine.position.x, y = engine.position.y}}
        pcall(function() game.forces.player.add_chart_tag(S, {{position = engine.position, text = "Jev: power"}}) end)
        local coal = math.min(10, bot.get_item_count("coal"))
        if coal > 0 then plant[2].get_fuel_inventory().insert{{name = "coal", count = coal}} bot.remove_item{{name = "coal", count = coal}} end
        out({{ok = true, x = engine.position.x, y = engine.position.y, coal = coal}})
    """)
    if not r["ok"]:
        return f"could not build power: {r['reason']}"
    return f"built steam power at ({r['x']}, {r['y']}) with {r['coal']} coal in the boiler"


def feed_boiler(f: Factorio) -> str:
    """A burner inserter into the boiler, picking up from a one-belt fuel entry that a coal belt can reach later."""
    boiler = f.lua('for _, e in pairs(J.built) do if e.valid and e.name == "boiler" then out({x = e.position.x, y = e.position.y}) return end end')
    if not boiler:
        return "no boiler yet"
    craft_all(f, {"burner-inserter": 1, "transport-belt": 1})
    f.walk_to(boiler["x"], boiler["y"], stop_short=3)
    r = f.lua("""
        if bot.get_item_count("burner-inserter") < 1 or bot.get_item_count("transport-belt") < 1 then
          out({ok = false, reason = "missing burner-inserter or transport-belt"}) return
        end
        local boiler
        for _, e in pairs(J.built) do if e.valid and e.name == "boiler" then boiler = e end end
        local east = defines.direction.east
        local function placeable(name, p, d)
          return S.can_place_entity{name = name, position = p, direction = d, force = "player", build_check_type = defines.build_check_type.manual}
        end
        for dx = -3, 3 do for dy = -3, 3 do
          local p = {math.floor(boiler.position.x) + dx + 0.5, math.floor(boiler.position.y) + dy + 0.5}
          for _, d in pairs({"north", "east", "south", "west"}) do
            if placeable("burner-inserter", p, defines.direction[d]) then
              local ins = S.create_entity{name = "burner-inserter", position = p, direction = defines.direction[d], force = "player"}
              local q = ins.pickup_position
              local into_boiler = S.find_entities_filtered{position = ins.drop_position, name = "boiler"}[1] == boiler
              if into_boiler and placeable("transport-belt", q, east) and placeable("transport-belt", {q.x - 1, q.y}, east) then
                local belt = S.create_entity{name = "transport-belt", position = q, direction = east, force = "player"}
                bot.remove_item{name = "burner-inserter", count = 1}
                bot.remove_item{name = "transport-belt", count = 1}
                track(ins) track(belt)
                J.roles[ins.unit_number] = "boiler-feed"
                J.roles[belt.unit_number] = "fuel-in"
                local coal = math.min(2, bot.get_item_count("coal"))
                if coal > 0 then ins.get_fuel_inventory().insert{name = "coal", count = coal} bot.remove_item{name = "coal", count = coal} end
                out({ok = true}) return
              end
              ins.destroy()
            end
          end
        end end
        out({ok = false, reason = "no room next to the boiler for a fuel inserter"})
    """)
    return "set up a coal entry at the boiler, ready for a coal belt" if r["ok"] else f"could not feed the boiler: {r['reason']}"


def build_module(f: Factorio, name: str, layout: list[dict]) -> str:
    """Craft a module's parts, place it near the power plant and wire it up."""
    base = f.lua("if J.base then out(J.base) end")
    if not base:
        return f"cannot build {name}: no power plant yet"
    craft_all(f, module_needs(layout))
    f.walk_to(base["x"], base["y"], stop_short=6)
    bx, by = int(base["x"]), int(base["y"])
    r = f.place_layout(layout, (bx, by), label=name, candidates=[[bx + dx, by + dy] for dx, dy in SLOTS])
    if not r["ok"]:
        return f"could not build {name}: {r['reason']}"
    pole = next(e for e in layout if e["name"] == "small-electric-pole")
    wire = f.connect_power(r["x"] + pole["x"], r["y"] + pole["y"])
    f.mark_power()
    note = "" if wire["ok"] else f", but it has no power: {wire['reason']}"
    return f"built {name} at ({r['x']}, {r['y']}){note}"


def _check_layouts():
    """Nothing overlaps, drills drop into what they feed, every inserter moves between two entities,
    and every belt is reached from an entry."""
    size = {"burner-mining-drill": 2, "stone-furnace": 2, "iron-chest": 1, "burner-inserter": 1, "inserter": 1,
            "transport-belt": 1, "assembling-machine-1": 3, "lab": 3, "small-electric-pole": 1}
    step = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}

    def box(e):
        h = size[e["name"]] / 2
        return e["x"] - h, e["y"] - h, e["x"] + h, e["y"] + h

    def inside(p, e):
        x0, y0, x1, y1 = box(e)
        return x0 < p[0] < x1 and y0 < p[1] < y1

    column = smelt_column("iron-ore")
    drill_feeds = {id(COAL_LINE[0]): COAL_LINE[2], id(COAL_LINE[1]): COAL_LINE[0],
                   **{id(column[5 * k]): column[5 * k + 1] for k in range(COLUMN_DRILLS)}}
    for layout in (COAL_LINE, column, RED_SCIENCE, LAB):
        for i, a in enumerate(layout):
            for b in layout[i + 1:]:
                ax0, ay0, ax1, ay1 = box(a)
                bx0, by0, bx1, by1 = box(b)
                assert ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0, f"{a} overlaps {b}"
            if id(a) in drill_feeds:
                dx, dy = DRILL_DROP[a["dir"]]
                assert inside((a["x"] + dx, a["y"] + dy), drill_feeds[id(a)]), f"{a} misses what it feeds"
            if a["name"].endswith("inserter"):  # dir is the side it picks up from
                dx, dy = step[a["dir"]]
                for p in ((a["x"] + dx, a["y"] + dy), (a["x"] - dx, a["y"] - dy)):
                    assert any(inside(p, b) for b in layout if b is not a), f"{a} has nothing at {p}"
        belts = {(e["x"], e["y"]): e for e in layout if e["name"] == "transport-belt"}
        reached = set()
        for entry in (e for e in layout if (e.get("role") or "").startswith(("fuel-in", "plates-in"))):
            pos = (entry["x"], entry["y"])
            while pos in belts and pos not in reached:
                reached.add(pos)
                dx, dy = step[belts[pos]["dir"]]
                pos = (pos[0] + dx, pos[1] + dy)
        assert reached == set(belts), f"belts not reached from an entry: {set(belts) - reached}"
    print("layouts ok")


if __name__ == "__main__":
    _check_layouts()
