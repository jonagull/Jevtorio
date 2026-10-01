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

# Automated red science, fed by the bot (or later belts) through two input chests:
#
#          c           c = copper plate chest,  i = inserter,  p = pole
#       p  i
#  c i GGG i RRR i LLL    G = gear assembler, R = red science assembler, L = lab
#      GGG   RRR   LLL
#      GGG   RRR   LLL
#    p     p     p
RED_SCIENCE = [
    {"name": "iron-chest", "x": 0.5, "y": 0.5, "role": "input:iron-plate"},
    {"name": "inserter", "x": 1.5, "y": 0.5, "dir": "west"},
    {"name": "assembling-machine-1", "x": 3.5, "y": 0.5, "recipe": "iron-gear-wheel"},
    {"name": "inserter", "x": 5.5, "y": 0.5, "dir": "west"},
    {"name": "assembling-machine-1", "x": 7.5, "y": 0.5, "recipe": "automation-science-pack"},
    {"name": "inserter", "x": 9.5, "y": 0.5, "dir": "west"},
    {"name": "lab", "x": 11.5, "y": 0.5},
    {"name": "iron-chest", "x": 7.5, "y": -2.5, "role": "input:copper-plate"},
    {"name": "inserter", "x": 7.5, "y": -1.5, "dir": "north"},
    {"name": "small-electric-pole", "x": 1.5, "y": 2.5},
    {"name": "small-electric-pole", "x": 5.5, "y": 2.5},
    {"name": "small-electric-pole", "x": 9.5, "y": 2.5},
    {"name": "small-electric-pole", "x": 5.5, "y": -1.5},
]

# Two burner drills on coal facing each other, both dropping into one chest:
#   DDD      east-facing drill at (0, 0) drops at (1.5, -0.5)
#   DcDD     west-facing drill at (3, -1) drops at (1.5, -0.5) too
#    DD
COAL_LINE = [
    {"name": "burner-mining-drill", "x": 0, "y": 0, "dir": "east", "on": "coal", "role": "line:coal"},
    {"name": "burner-mining-drill", "x": 3, "y": -1, "dir": "west", "on": "coal", "role": "line:coal"},
    {"name": "iron-chest", "x": 1.5, "y": -0.5, "role": "line:coal"},
]

POWER_PARTS = {"offshore-pump": 1, "boiler": 1, "steam-engine": 1, "small-electric-pole": 2}
SPARE_POLES = 6  # for the pole line from the power plant to a module


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
        local coal = math.min(10, bot.get_item_count("coal"))
        if coal > 0 then plant[2].get_fuel_inventory().insert{{name = "coal", count = coal}} bot.remove_item{{name = "coal", count = coal}} end
        out({{ok = true, x = engine.position.x, y = engine.position.y, coal = coal}})
    """)
    if not r["ok"]:
        return f"could not build power: {r['reason']}"
    return f"built steam power at ({r['x']}, {r['y']}) with {r['coal']} coal in the boiler"


def build_module(f: Factorio, name: str, layout: list[dict]) -> str:
    """Craft a module's parts, place it near the power plant and wire it up."""
    base = f.lua("if J.base then out(J.base) end")
    if not base:
        return f"cannot build {name}: no power plant yet"
    craft_all(f, module_needs(layout))
    f.walk_to(base["x"], base["y"], stop_short=6)
    r = f.place_layout(layout, (base["x"], base["y"]), radius=40, min_radius=7)
    if not r["ok"]:
        return f"could not build {name}: {r['reason']}"
    pole = next(e for e in layout if e["name"] == "small-electric-pole")
    wire = f.connect_power(r["x"] + pole["x"], r["y"] + pole["y"])
    f.mark_power()
    note = "" if wire["ok"] else f", but it has no power: {wire['reason']}"
    return f"built {name} at ({r['x']}, {r['y']}){note}"
