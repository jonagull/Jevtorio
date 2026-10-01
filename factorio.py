"""Talks to Factorio over RCON and gives the bot a body, eyes and hands.

Every call sends a self-contained Lua snippet via /silent-command. The bot is a
plain `character` entity (not a connected player), kept in `storage.jev`.
"""

import json
import math
import time

import factorio_rcon

PRELUDE = """
local S = game.surfaces[1]
storage.jev = storage.jev or {built = {}}
local J = storage.jev
J.roles = J.roles or {}
J.crafted = J.crafted or {}
local bot = J.char
if bot and not bot.valid then bot = nil end
local function track(e) J.built[e.unit_number] = e end
local HAND = prototypes.entity["character"].crafting_categories
local function handcraftable(name)
  local r = prototypes.recipe[name]
  return r ~= nil and HAND[r.category] == true and game.forces.player.recipes[name].enabled
end
local function out(t) rcon.print(helpers.table_to_json(t)) end
local function inv(e)
  local c = {}
  for _, it in pairs(e.get_contents()) do c[it.name] = (c[it.name] or 0) + it.count end
  return c
end
local function nearest(name, pos)
  for _, r in pairs({16, 48, 128, 256}) do
    local found = name == "tree" and S.find_entities_filtered{type = "tree", position = pos, radius = r}
                  or S.find_entities_filtered{name = name, position = pos, radius = r}
    if #found > 0 then
      local best, bd = nil, math.huge
      for _, e in pairs(found) do
        local d = (e.position.x - pos.x)^2 + (e.position.y - pos.y)^2
        if d < bd then best, bd = e, d end
      end
      return best
    end
  end
end
"""

ORES = ["iron-ore", "copper-ore", "coal", "stone"]
STARTING_ITEMS = {"burner-mining-drill": 1, "stone-furnace": 1, "iron-plate": 8, "wood": 1}
WALK_SPEED = 9.0  # tiles/second, close to a real character
MINE_SECONDS = 1.0  # per ore; real hand mining is ~2s, sped up so it's less dull


class LuaError(RuntimeError):
    pass


class Factorio:
    def __init__(self, host="127.0.0.1", port=27015, password="secret"):
        self.rcon = factorio_rcon.RCONClient(host, port, password)

    def lua(self, body: str):
        """Run Lua (with PRELUDE) and return whatever it passed to out(), parsed from JSON."""
        code = PRELUDE + f"local ok, err = pcall(function() {body} end) if not ok then out({{error = tostring(err)}}) end"
        # Factorio commands are one line; the Lua snippets here contain no `--` comments.
        raw = self.rcon.send_command("/silent-command " + " ".join(code.split()))
        if not raw:
            return None
        result = json.loads(raw)
        if isinstance(result, dict) and "error" in result:
            raise LuaError(result["error"])
        return result

    def say(self, text: str):
        self.lua(f"game.print({json.dumps('[color=yellow][Jev][/color] ' + text)})")

    # ---- body -------------------------------------------------------------

    def spawn(self):
        """Create the bot character near spawn if it doesn't exist yet."""
        items = "{" + ", ".join(f'["{k}"] = {v}' for k, v in STARTING_ITEMS.items()) + "}"
        return self.lua(f"""
            if bot then out({{spawned = false}}) return end
            local p = S.find_non_colliding_position("character", {{4, 4}}, 20, 0.5)
            bot = S.create_entity{{name = "character", position = p, force = "player"}}
            J.char = bot
            for name, count in pairs({items}) do bot.insert{{name = name, count = count}} end
            rendering.draw_text{{text = "Jev", surface = S, target = {{entity = bot, offset = {{0, -2.6}}}},
                                 color = {{1, 0.85, 0.2}}, scale = 1.4, alignment = "center"}}
            out({{spawned = true}})
        """)

    def reset(self):
        """Remove the bot and everything it built."""
        self.lua("""
            if bot then bot.destroy() end
            for _, e in pairs(J.built) do if e.valid then e.destroy() end end
            storage.jev = nil
            out({})
        """)

    # ---- eyes -------------------------------------------------------------

    def observe(self) -> dict:
        ores = json.dumps(ORES).replace("[", "{").replace("]", "}")
        return self.lua(f"""
            local status_names = {{}}
            for k, v in pairs(defines.entity_status) do status_names[v] = k end
            local ores = {{}}
            for _, name in pairs({ores}) do
              local e = nearest(name, bot.position)
              if e then
                local dx, dy = e.position.x - bot.position.x, e.position.y - bot.position.y
                ores[name] = {{x = e.position.x, y = e.position.y, distance = math.floor(math.sqrt(dx*dx + dy*dy))}}
              end
            end
            local built = {{}}
            for _, e in pairs(J.built) do
              if e.valid then
                local b = {{name = e.name, x = e.position.x, y = e.position.y,
                           status = status_names[e.status] or "unknown", fuel = 0}}
                b.type = e.type
                b.role = J.roles[e.unit_number]
                local fuel = e.get_fuel_inventory()
                if fuel then b.fuel = fuel.get_item_count() b.burner = true end
                if e.type == "container" then b.contents = inv(e.get_inventory(defines.inventory.chest)) end
                if e.type == "lab" then b.contents = inv(e.get_inventory(defines.inventory.lab_input)) end
                if e.type == "furnace" then
                  b.input = inv(e.get_inventory(defines.inventory.furnace_source))
                  b.output = inv(e.get_inventory(defines.inventory.furnace_result))
                end
                table.insert(built, b)
              end
            end
            local force = game.forces.player
            local research = {{researched = {{}}, available = {{}}}}
            for name, t in pairs(force.technologies) do
              if t.researched then
                table.insert(research.researched, name)
              else
                local ready = true
                for _, pre in pairs(t.prerequisites) do if not pre.researched then ready = false end end
                if ready then
                  local unlocks = {{}}
                  for _, eff in pairs(t.prototype.effects) do if eff.recipe then table.insert(unlocks, eff.recipe) end end
                  local trig = t.prototype.research_trigger
                  local cost = {{}}
                  if not trig then for _, ing in pairs(t.research_unit_ingredients) do table.insert(cost, ing.name) end end
                  research.available[name] = {{unlocks = unlocks, trigger = trig, units = t.research_unit_count, packs = cost}}
                end
              end
            end
            if force.current_research then
              research.current = {{name = force.current_research.name, progress = force.research_progress}}
            end
            out({{tick = game.tick, x = bot.position.x, y = bot.position.y,
                 inventory = inv(bot.get_main_inventory()), ores = ores, built = built, research = research}})
        """)

    # ---- legs -------------------------------------------------------------

    def walk_to(self, x: float, y: float, stop_short: float = 1.5):
        """Glide toward (x, y) in small teleports so it looks like walking."""
        pos = self.lua("out({x = bot.position.x, y = bot.position.y})")
        dx, dy = x - pos["x"], y - pos["y"]
        dist = math.hypot(dx, dy) - stop_short
        if dist <= 0:
            return
        ux, uy = dx / (dist + stop_short), dy / (dist + stop_short)
        tick = 0.05
        steps = max(1, int(dist / (WALK_SPEED * tick)))
        direction = _direction(ux, uy)
        for i in range(1, steps + 1):
            d = dist * i / steps
            self.lua(f"bot.teleport({{{pos['x'] + ux * d}, {pos['y'] + uy * d}}}) bot.direction = defines.direction.{direction}")
            time.sleep(tick)

    # ---- hands ------------------------------------------------------------

    def mine(self, resource: str, count: int) -> int:
        """Walk to the nearest `resource` tile and mine `count` of it by hand."""
        if resource == "wood":
            return self.chop_trees(count)
        mined = 0
        while mined < count:
            ore = self.lua(f'local e = nearest("{resource}", bot.position) if e then out({{x = e.position.x, y = e.position.y}}) end')
            if not ore:
                break
            self.walk_to(ore["x"], ore["y"])
            got = self.lua(f"""
                local e = S.find_entity("{resource}", {{{ore['x']}, {ore['y']}}})
                if not e then out({{got = 0}}) return end
                if e.amount <= 1 then e.destroy() else e.amount = e.amount - 1 end
                bot.insert{{name = "{resource}", count = 1}}
                out({{got = 1}})
            """)
            mined += got["got"]
            time.sleep(MINE_SECONDS)
        return mined

    def chop_trees(self, wood: int) -> int:
        """Cut down trees until we have gained `wood` wood."""
        got = 0
        while got < wood:
            tree = self.lua('local e = nearest("tree", bot.position) if e then out({x = e.position.x, y = e.position.y}) end')
            if not tree:
                break
            self.walk_to(tree["x"], tree["y"])
            time.sleep(MINE_SECONDS * 2)
            got += self.lua(f"""
                local e = S.find_entities_filtered{{type = "tree", position = {{{tree['x']}, {tree['y']}}}, radius = 0.5, limit = 1}}[1]
                if not e then out({{n = 0}}) return end
                local n = 0
                for _, p in pairs(e.prototype.mineable_properties.products or {{}}) do
                  if p.name == "wood" then n = n + (p.amount or p.amount_max or 1) end
                end
                e.destroy()
                if n > 0 then bot.insert{{name = "wood", count = n}} end
                out({{n = n}})
            """)["n"]
        return got

    def craft(self, recipe: str, count: int = 1) -> int:
        """Craft by paying the real ingredient cost. Returns how many were made."""
        made = 0
        for _ in range(count):
            r = self.lua(f"""
                if not handcraftable("{recipe}") then out({{ok = false, missing = "unlocked recipe"}}) return end
                local r = prototypes.recipe["{recipe}"]
                for _, ing in pairs(r.ingredients) do
                  if bot.get_item_count(ing.name) < ing.amount then out({{ok = false, missing = ing.name}}) return end
                end
                for _, ing in pairs(r.ingredients) do bot.remove_item{{name = ing.name, count = ing.amount}} end
                for _, p in pairs(r.products) do
                  bot.insert{{name = p.name, count = p.amount}}
                  J.crafted[p.name] = (J.crafted[p.name] or 0) + p.amount
                end
                out({{ok = true, seconds = r.energy}})
            """)
            if not r["ok"]:
                break
            time.sleep(r["seconds"])
            made += 1
        return made

    def place_furnace_here(self) -> bool:
        """Put down a stone furnace next to the bot."""
        r = self.lua("""
            if bot.get_item_count("stone-furnace") < 1 then out({ok = false}) return end
            local p = S.find_non_colliding_position("stone-furnace", {bot.position.x + 2.5, bot.position.y}, 10, 1)
            local f = p and S.create_entity{name = "stone-furnace", position = p, force = "player"}
            if not f then out({ok = false}) return end
            bot.remove_item{name = "stone-furnace", count = 1}
            track(f)
            out({ok = true})
        """)
        return r["ok"]

    def place_drill_on(self, resource: str) -> dict:
        """Place a burner drill on `resource`, fuel it, and put a furnace at its output if we have one."""
        ore = self.lua(f'local e = nearest("{resource}", bot.position) if e then out({{x = e.position.x, y = e.position.y}}) end')
        if not ore:
            return {"ok": False, "reason": f"no {resource} nearby"}
        self.walk_to(ore["x"], ore["y"], stop_short=3)
        return self.lua(f"""
            if bot.get_item_count("burner-mining-drill") < 1 then out({{ok = false, reason = "no drill"}}) return end
            local spot
            for r = 0, 12 do
              for dx = -r, r do for dy = -r, r do
                local p = {{{ore['x']} + dx, {ore['y']} + dy}}
                if not spot and S.can_place_entity{{name = "burner-mining-drill", position = p, direction = defines.direction.south, force = "player"}}
                   and S.can_place_entity{{name = "stone-furnace", position = {{p[1], p[2] + 2}}, force = "player"}} then
                  spot = p
                end
              end end
              if spot then break end
            end
            if not spot then out({{ok = false, reason = "no room on the patch"}}) return end
            local d = S.create_entity{{name = "burner-mining-drill", position = spot, direction = defines.direction.south, force = "player"}}
            bot.remove_item{{name = "burner-mining-drill", count = 1}}
            track(d)
            local coal = math.min(5, bot.get_item_count("coal"))
            if coal > 0 then d.get_fuel_inventory().insert{{name = "coal", count = coal}} bot.remove_item{{name = "coal", count = coal}} end
            local furnace = false
            if bot.get_item_count("stone-furnace") > 0 then
              local f = S.create_entity{{name = "stone-furnace", position = {{spot[1], spot[2] + 2}}, force = "player"}}
              if f then
                bot.remove_item{{name = "stone-furnace", count = 1}}
                track(f)
                local c = math.min(3, bot.get_item_count("coal"))
                if c > 0 then f.get_fuel_inventory().insert{{name = "coal", count = c}} bot.remove_item{{name = "coal", count = c}} end
                furnace = true
              end
            end
            out({{ok = true, furnace = furnace, fueled = coal > 0}})
        """)

    def _visit_built(self, lua_filter: str) -> list[dict]:
        """Positions of built entities matching a Lua condition on `e`."""
        return self.lua(f"""
            local r = {{}}
            for i, e in pairs(J.built) do if e.valid and ({lua_filter}) then table.insert(r, {{i = i, x = e.position.x, y = e.position.y}}) end end
            out(r)
        """) or []

    def refuel_all(self) -> int:
        """Walk to each machine that is low on fuel and give it coal."""
        given = 0
        targets = self._visit_built("e.get_fuel_inventory() and e.get_fuel_inventory().get_item_count() < 3")
        coal = self.lua('out({n = bot.get_item_count("coal")})')["n"]
        share = max(1, min(10, coal // max(1, len(targets))))  # spread the coal so nothing is left empty
        for t in targets:
            self.walk_to(t["x"], t["y"], stop_short=2)
            r = self.lua(f"""
                local e = J.built[{t['i']}]
                local n = math.min({share}, bot.get_item_count("coal"))
                if n > 0 then n = e.get_fuel_inventory().insert{{name = "coal", count = n}} bot.remove_item{{name = "coal", count = n}} end
                out({{n = n}})
            """)
            given += r["n"]
        return given

    def load_furnaces(self, ore: str = "iron-ore") -> int:
        """Walk to furnaces and put `ore` from the inventory into them."""
        loaded = 0
        for t in self._visit_built(
            f'e.type == "furnace" and not (J.roles[e.unit_number] or ""):find("^line")'
            f' and e.get_inventory(defines.inventory.furnace_source).can_insert("{ore}")'
        ):
            self.walk_to(t["x"], t["y"], stop_short=2)
            r = self.lua(f"""
                local e = J.built[{t['i']}]
                local n = math.min(20, bot.get_item_count("{ore}"))
                if n > 0 then n = e.get_inventory(defines.inventory.furnace_source).insert{{name = "{ore}", count = n}} end
                if n > 0 then bot.remove_item{{name = "{ore}", count = n}} end
                local fuel = e.get_fuel_inventory()
                local coal = math.min(5 - fuel.get_item_count("coal"), bot.get_item_count("coal"))
                if coal > 0 then fuel.insert{{name = "coal", count = coal}} bot.remove_item{{name = "coal", count = coal}} end
                out({{n = n}})
            """)
            loaded += r["n"]
        return loaded

    def collect_output(self) -> int:
        """Walk to furnaces and take out finished plates."""
        taken = 0
        for t in self._visit_built(
            '(e.type == "furnace" and not e.get_inventory(defines.inventory.furnace_result).is_empty())'
            ' or (e.type == "container" and not (J.roles[e.unit_number] or ""):find("^input")'
            ' and not e.get_inventory(defines.inventory.chest).is_empty())'
        ):
            self.walk_to(t["x"], t["y"], stop_short=2)
            r = self.lua(f"""
                local e = J.built[{t['i']}]
                local out_inv = e.get_inventory(e.type == "furnace" and defines.inventory.furnace_result or defines.inventory.chest)
                local n = 0
                for _, it in pairs(out_inv.get_contents()) do
                  local moved = bot.insert{{name = it.name, count = it.count}}
                  out_inv.remove{{name = it.name, count = moved}}
                  n = n + moved
                end
                out({{n = n}})
            """)
            taken += r["n"]
        return taken

    def craft_deep(self, recipe: str, count: int = 1) -> int:
        """Craft `recipe`, first hand-crafting any missing intermediates (e.g. gears). Returns how many were made."""
        made = 0
        for _ in range(count):
            # Re-check after each pass: crafting one ingredient can eat another (belts use gears).
            for _attempt in range(4):
                ingredients = self.lua(f"""
                    local r = {{}}
                    for _, ing in pairs(prototypes.recipe["{recipe}"].ingredients) do
                      local outn = 1
                      if handcraftable(ing.name) then
                        for _, p in pairs(prototypes.recipe[ing.name].products) do if p.name == ing.name then outn = p.amount end end
                      end
                      table.insert(r, {{name = ing.name, need = ing.amount - bot.get_item_count(ing.name),
                                       handcraft = handcraftable(ing.name), out = outn}})
                    end
                    out(r)
                """)
                short = [i for i in ingredients if i["need"] > 0 and i["handcraft"]]
                if not short:
                    break
                for ing in short:
                    self.craft_deep(ing["name"], math.ceil(ing["need"] / ing["out"]))
            if not self.craft(recipe):
                break
            made += 1
        return made

    def pick_up_dead_drills(self) -> int:
        """Pick up drills with no ore left (and the furnace they fed), contents and all."""
        picked = 0
        for t in self._visit_built('e.type == "mining-drill" and e.status == defines.entity_status.no_minable_resources'):
            self.walk_to(t["x"], t["y"], stop_short=2)
            r = self.lua(f"""
                local function pickup(e)
                  for i = 1, e.get_max_inventory_index() do
                    local ei = e.get_inventory(i)
                    if ei then for _, it in pairs(ei.get_contents()) do bot.insert{{name = it.name, count = it.count}} end end
                  end
                  bot.insert{{name = e.name, count = 1}}
                  J.built[e.unit_number] = nil
                  e.destroy()
                end
                local d = J.built[{t['i']}]
                local f = S.find_entities_filtered{{type = "furnace", position = d.drop_position, limit = 1}}[1]
                if f and not f.is_crafting() and f.get_inventory(defines.inventory.furnace_source).is_empty() then pickup(f) end
                pickup(d)
                out({{}})
            """)
            picked += 1
        return picked

    def build_smelting_line(self, resource: str = "iron-ore", drills: int = 2) -> dict:
        """Build: drills (facing east) -> belt (south) -> inserter -> furnace -> inserter -> chest.

        Layout, with the drills at (ox, oy + 2k):
            DD b        D = drill, b = belt, i = burner inserter
            DD b        F = stone furnace, c = iron chest
            DD b
            DD b
               b
               i
               FF
               FF
               i
               c
        """
        ore = self.lua(f'local e = nearest("{resource}", bot.position) if e then out({{x = e.position.x, y = e.position.y}}) end')
        if not ore:
            return {"ok": False, "reason": f"no {resource} nearby"}
        self.walk_to(ore["x"], ore["y"], stop_short=3)
        return self.lua(f"""
            local N, ore = {drills}, "{resource}"
            local function layout(ox, oy)
              local L = {{}}
              for k = 0, N - 1 do
                table.insert(L, {{name = "burner-mining-drill", position = {{ox, oy + 2 * k}}, direction = defines.direction.east}})
              end
              for y = oy - 0.5, oy + 2 * N - 0.5 do
                table.insert(L, {{name = "transport-belt", position = {{ox + 1.5, y}}, direction = defines.direction.south}})
              end
              local base = oy + 2 * N
              table.insert(L, {{name = "burner-inserter", position = {{ox + 1.5, base + 0.5}}, direction = defines.direction.north}})
              table.insert(L, {{name = "stone-furnace", position = {{ox + 2, base + 2}}}})
              table.insert(L, {{name = "burner-inserter", position = {{ox + 1.5, base + 3.5}}, direction = defines.direction.north}})
              table.insert(L, {{name = "iron-chest", position = {{ox + 1.5, base + 4.5}}}})
              return L
            end
            local function fits(L)
              for _, spec in pairs(L) do
                if not S.can_place_entity{{name = spec.name, position = spec.position, direction = spec.direction, force = "player"}} then return false end
                if spec.name == "burner-mining-drill" then
                  local p = spec.position
                  if S.count_entities_filtered{{name = ore, area = {{{{p[1] - 1, p[2] - 1}}, {{p[1] + 1, p[2] + 1}}}}}} < 3 then return false end
                end
              end
              return true
            end
            local needed = {{}}
            for _, spec in pairs(layout(0, 0)) do needed[spec.name] = (needed[spec.name] or 0) + 1 end
            for name, n in pairs(needed) do
              if bot.get_item_count(name) < n then out({{ok = false, reason = "missing " .. name}}) return end
            end
            local cx, cy = math.floor({ore['x']}), math.floor({ore['y']})
            local chosen
            for r = 0, 24 do
              for dx = -r, r do for dy = -r, r do
                if not chosen and math.max(math.abs(dx), math.abs(dy)) == r then
                  local L = layout(cx + dx, cy + dy)
                  if fits(L) then chosen = L end
                end
              end end
              if chosen then break end
            end
            if not chosen then out({{ok = false, reason = "no room for a line on the patch"}}) return end
            local coal_each = math.floor(bot.get_item_count("coal") / (N + 3))
            for _, spec in pairs(chosen) do
              spec.force = "player"
              local e = S.create_entity(spec)
              bot.remove_item{{name = spec.name, count = 1}}
              track(e)
              J.roles[e.unit_number] = "line:" .. ore
              local fuel = e.get_fuel_inventory()
              if fuel and coal_each > 0 then fuel.insert{{name = "coal", count = coal_each}} bot.remove_item{{name = "coal", count = coal_each}} end
            end
            J.lines = (J.lines or 0) + 1
            out({{ok = true, x = chosen[1].position[1], y = chosen[1].position[2], coal_each = coal_each}})
        """)

    # ---- research ---------------------------------------------------------

    def check_triggers(self) -> list[str]:
        """Complete trigger technologies whose condition the bot has really met.

        The game fires these for machine-made items, but not for the bot's scripted crafting, so we count those too.
        """
        return self.lua("""
            local force, done = game.forces.player, {}
            local stats = force.get_item_production_statistics(S)
            for name, t in pairs(force.technologies) do
              local trig = t.prototype.research_trigger
              local ready = not t.researched and trig and trig.type == "craft-item"
              if ready then for _, pre in pairs(t.prerequisites) do if not pre.researched then ready = false end end end
              if ready then
                local item = trig.item.name or trig.item
                if stats.get_input_count(item) + (J.crafted[item] or 0) >= (trig.count or 1) then
                  t.researched = true
                  table.insert(done, name)
                end
              end
            end
            out(done)
        """) or []

    def start_research(self, tech: str) -> bool:
        return self.lua(f'out({{ok = game.forces.player.add_research("{tech}")}})')["ok"]

    def feed_labs(self, pack: str = "automation-science-pack") -> int:
        """Walk to labs and put science packs from the inventory in them."""
        fed = 0
        for t in self._visit_built('e.type == "lab"'):
            self.walk_to(t["x"], t["y"], stop_short=2)
            fed += self.lua(f"""
                local n = bot.get_item_count("{pack}")
                if n > 0 then n = J.built[{t['i']}].get_inventory(defines.inventory.lab_input).insert{{name = "{pack}", count = n}} end
                if n > 0 then bot.remove_item{{name = "{pack}", count = n}} end
                out({{n = n}})
            """)["n"]
        return fed

    def stock_inputs(self) -> dict[str, int]:
        """Courier run: fill input chests (role "input:<item>") from the inventory, keeping a little back."""
        moved: dict[str, int] = {}
        for t in self._visit_built('(J.roles[e.unit_number] or ""):find("^input:")'):
            self.walk_to(t["x"], t["y"], stop_short=2)
            r = self.lua(f"""
                local e = J.built[{t['i']}]
                local item = J.roles[e.unit_number]:sub(7)
                local n = bot.get_item_count(item)
                if n > 0 then n = e.insert{{name = item, count = n}} end
                if n > 0 then bot.remove_item{{name = item, count = n}} end
                out({{item = item, n = n}})
            """)
            moved[r["item"]] = moved.get(r["item"], 0) + r["n"]
        return moved

    # ---- building from layouts ---------------------------------------------

    def place_layout(self, layout: list[dict], center: tuple[float, float], radius: int = 30, min_radius: int = 0) -> dict:
        """Find room for `layout` near `center` and build it, paying for every entity from the inventory.

        Each layout entry: {name, x, y, dir?, recipe?, role?, on?}. `on` requires that resource under a drill.
        Poles in the layout connect to each other on their own; returns the placed origin.
        """
        return self.lua(f"""
            local L = helpers.json_to_table({_lua_str(layout)})
            local cx, cy = math.floor({center[0]}), math.floor({center[1]})
            local needed = {{}}
            for _, s in pairs(L) do needed[s.name] = (needed[s.name] or 0) + 1 end
            for name, n in pairs(needed) do
              if bot.get_item_count(name) < n then out({{ok = false, reason = "missing " .. name}}) return end
            end
            local function spec(s, ox, oy)
              return {{name = s.name, position = {{ox + s.x, oy + s.y}}, direction = s.dir and defines.direction[s.dir],
                      force = "player", build_check_type = defines.build_check_type.manual}}
            end
            local function fits(ox, oy)
              for _, s in pairs(L) do
                local sp = spec(s, ox, oy)
                if not S.can_place_entity(sp) then return false end
                if s.on then
                  local p = sp.position
                  if S.count_entities_filtered{{name = s.on, area = {{{{p[1] - 1, p[2] - 1}}, {{p[1] + 1, p[2] + 1}}}}}} < 3 then return false end
                end
              end
              return true
            end
            local ox, oy
            for r = {min_radius}, {radius} do
              for dx = -r, r do for dy = -r, r do
                if not ox and math.max(math.abs(dx), math.abs(dy)) == r and fits(cx + dx, cy + dy) then ox, oy = cx + dx, cy + dy end
              end end
              if ox then break end
            end
            if not ox then out({{ok = false, reason = "no room nearby"}}) return end
            local burners = 0
            for _, s in pairs(L) do
              local proto = prototypes.entity[s.name]
              if proto.burner_prototype then burners = burners + 1 end
            end
            local coal_each = burners > 0 and math.min(5, math.floor(bot.get_item_count("coal") / burners)) or 0
            for _, s in pairs(L) do
              local sp = spec(s, ox, oy)
              sp.build_check_type = nil
              local e = S.create_entity(sp)
              bot.remove_item{{name = s.name, count = 1}}
              track(e)
              if s.role then J.roles[e.unit_number] = s.role end
              if s.recipe then e.set_recipe(s.recipe) end
              local fuel = e.get_fuel_inventory()
              if fuel and coal_each > 0 then fuel.insert{{name = "coal", count = coal_each}} bot.remove_item{{name = "coal", count = coal_each}} end
            end
            out({{ok = true, x = ox, y = oy}})
        """)

    def connect_power(self, x: float, y: float) -> dict:
        """Run a line of small poles from the nearest powered pole to (x, y). Poles come from the inventory."""
        return self.lua(f"""
            local target = {{x = {x}, y = {y}}}
            local best, bd
            for _, e in pairs(J.built) do
              if e.valid and e.type == "electric-pole" and J.roles[e.unit_number] == "power" then
                local d = (e.position.x - target.x)^2 + (e.position.y - target.y)^2
                if not bd or d < bd then best, bd = e, d end
              end
            end
            if not best then out({{ok = false, reason = "no power plant"}}) return end
            local here = best.position
            local placed = 0
            while (here.x - target.x)^2 + (here.y - target.y)^2 > 36 do
              local dx, dy = target.x - here.x, target.y - here.y
              local len = math.sqrt(dx * dx + dy * dy)
              local want = {{here.x + dx / len * 6, here.y + dy / len * 6}}
              local p = S.find_non_colliding_position("small-electric-pole", want, 1.5, 0.5, true)
              if not p then out({{ok = false, reason = "pole line blocked"}}) return end
              if bot.get_item_count("small-electric-pole") < 1 then out({{ok = false, reason = "missing small-electric-pole"}}) return end
              local pole = S.create_entity{{name = "small-electric-pole", position = p, force = "player"}}
              bot.remove_item{{name = "small-electric-pole", count = 1}}
              track(pole)
              J.roles[pole.unit_number] = "power"
              here = pole.position
              placed = placed + 1
            end
            out({{ok = true, poles = placed}})
        """)

    def mark_power(self):
        """Tag every pole on the plant's network as part of the power grid, so later modules can extend from them."""
        self.lua("""
            local net
            for _, e in pairs(J.built) do if e.valid and e.name == "steam-engine" then
              for _, p in pairs(S.find_entities_filtered{type = "electric-pole", position = e.position, radius = 6}) do net = p.electric_network_id end
            end end
            for _, e in pairs(J.built) do
              if e.valid and e.type == "electric-pole" and e.electric_network_id == net then J.roles[e.unit_number] = "power" end
            end
            out({})
        """)


def _direction(ux: float, uy: float) -> str:
    names = ["east", "southeast", "south", "southwest", "west", "northwest", "north", "northeast"]
    return names[round(math.atan2(uy, ux) / (math.pi / 4)) % 8]


def _lua_str(value) -> str:
    """A Lua string literal holding `value` as JSON (Factorio's Lua has no \\u escapes, so keep it raw UTF-8)."""
    return json.dumps(json.dumps(value, ensure_ascii=False), ensure_ascii=False)


def show_thought(game: Factorio, step: int, thought, result: str | None = None):
    """Draw Jev's current thinking: a speech bubble over the bot and a panel for every connected player."""
    options = sorted(thought.probabilities.items(), key=lambda kv: -kv[1])
    data = {
        "step": step,
        "choice": thought.choice.replace("_", " "),
        "options": [[name.replace("_", " "), p] for name, p in options],
        "bottleneck": thought.bottleneck.replace("_", " "),
        "progress": thought.progress,
        "result": result or "working on it...",
    }
    game.lua(f"""
        local d = helpers.json_to_table({_lua_str(data)})
        if J.bubble and J.bubble.valid then J.bubble.destroy() end
        J.bubble = rendering.draw_text{{text = "→ " .. d.choice, surface = S, target = {{entity = bot, offset = {{0, -3.4}}}},
                                       color = {{0.6, 0.9, 1}}, scale = 1.1, alignment = "center"}}
        for _, player in pairs(game.connected_players) do
          local screen = player.gui.screen
          local location = screen.jev_brain and screen.jev_brain.location
          if screen.jev_brain then screen.jev_brain.destroy() end
          local f = screen.add{{type = "frame", name = "jev_brain", direction = "vertical", caption = "Jev's brain - step " .. d.step}}
          f.location = location or {{player.display_resolution.width - 460, 120}}
          f.style.width = 400
          f.add{{type = "label", caption = "[font=default-bold]Doing:[/font] " .. d.choice}}
          f.add{{type = "label", caption = "[font=default-bold]Result:[/font] " .. d.result}}.style.single_line = false
          f.add{{type = "label", caption = "[font=default-bold]Bottleneck:[/font] " .. d.bottleneck}}
          f.add{{type = "label", caption = string.format("[font=default-bold]Progress:[/font] %.1f / 4", d.progress)}}
          f.add{{type = "line"}}
          f.add{{type = "label", caption = "[font=default-bold]How Jev weighed its options[/font]"}}
          for _, o in pairs(d.options) do
            local row = f.add{{type = "flow", direction = "horizontal"}}
            local l = row.add{{type = "label", caption = o[1]}}
            l.style.width = 150
            local bar = row.add{{type = "progressbar", value = o[2]}}
            bar.style.width = 170
            row.add{{type = "label", caption = string.format("%3d%%", math.floor(o[2] * 100 + 0.5))}}
          end
        end
        out({{}})
    """)
