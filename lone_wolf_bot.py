"""Fresh Mech Royale bot built around placement, safe pressure, and clean actions."""
from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Optional, Tuple

import mechfight as mf


def clamp(x: float, low: float, high: float) -> float:
    return max(low, min(high, x))


def wrap(a: float) -> float:
    return (a + math.pi) % (2.0 * math.pi) - math.pi


def d2(a: List[float], b: List[float]) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))


def unit(x: float, y: float) -> Tuple[float, float]:
    n = math.hypot(x, y)
    return (x / n, y / n) if n > 1e-8 else (0.0, 0.0)


class Bot:
    name = "lone-wolf-scratch"

    def __init__(self, hello: mf.Hello) -> None:
        self.rng = random.Random(int(hello.get("seed", 1)))
        self.pickup_radius = float(hello.get("config", {}).get("loot", {}).get("pickup_radius", 4.0))
        self.upgrades = hello.get("config", {}).get("upgrades", [])
        self.last_pos: Optional[Tuple[float, float]] = None
        self.stuck = 0
        self.strafe = 1.0
        self.strafe_until = 0.0
        self.last_target = None
        self.last_target_until = 0.0
        self.laser_holding = [False, False]
        self.skip_pickups: Dict[int, float] = {}
        self.last_damage_t = -999.0
        self.last_kill_t = -999.0

    def _zone_goal(self, obs: mf.Observation, x: float, y: float) -> Tuple[float, float]:
        z = obs["zone"]
        t = float(obs.get("t", 0.0))
        c = z.get("center", [0.0, 0.0])
        nc = z.get("next_center", c)
        r = float(z.get("radius", 500.0))
        nr = float(z.get("next_radius", r))
        start = float(z.get("shrink_start", t))
        end = float(z.get("shrink_end", t))
        if t >= start and end > start:
            q = clamp((t - start) / (end - start), 0.0, 1.0)
            cx = float(c[0]) + (float(nc[0]) - float(c[0])) * q
            cy = float(c[1]) + (float(nc[1]) - float(c[1])) * q
            radius = r + (nr - r) * q
        else:
            cx, cy, radius = float(c[0]), float(c[1]), r
        # Aim for a wide safe ring instead of crossing the exposed center.
        dx, dy = x - cx, y - cy
        ux, uy = unit(dx, dy)
        if ux == 0.0 and uy == 0.0:
            ux, uy = 1.0, 0.0
        safe_r = max(8.0, radius * 0.72)
        if math.hypot(dx, dy) > safe_r * 0.92:
            return cx + ux * safe_r, cy + uy * safe_r
        # The next circle matters before the shrink starts.
        nd = math.hypot(x - float(nc[0]), y - float(nc[1]))
        if nd > nr * 0.76:
            nx, ny = unit(float(nc[0]) - x, float(nc[1]) - y)
            return float(nc[0]) - nx * max(8.0, nr * 0.62), float(nc[1]) - ny * max(8.0, nr * 0.62)
        return x, y

    def _upgrade(self, offer: Dict[str, Any]) -> int:
        choices = offer.get("choices", [])
        best_i, best_s = 0, -1e9
        for i, ch in enumerate(choices):
            desc = str(ch.get("desc", "")).lower()
            uid = str(ch.get("id", "")).lower()
            s = 0.0
            for key, val in (("damage", 8), ("cooldown", 7), ("rate", 7), ("armor", 6),
                             ("max_hp", 6), ("regen", 5), ("speed", 5), ("energy", 4),
                             ("range", 4), ("sensor", 2), ("vision", 2)):
                if key in desc or key in uid:
                    s += val
            if s > best_s:
                best_i, best_s = i, s
        return best_i

    def _aim(self, me: Dict[str, Any], target: Dict[str, Any], weapons: List[Any]) -> Tuple[float, float, float]:
        p = target.get("pos", [0.0, 0.0, 0.0])
        v = target.get("vel", [0.0, 0.0, 0.0])
        mx, my, mz = (float(q) for q in me["pos"][:3])
        muzzle = mz + float(me.get("stats", {}).get("muzzle_height", 7.0))
        dx, dy = float(p[0]) - mx, float(p[1]) - my
        dist = max(1.0, math.hypot(dx, dy))
        kinds = {w.get("kind") for w in weapons if isinstance(w, dict)}
        if "gun" in kinds:
            gun = next(w for w in weapons if isinstance(w, dict) and w.get("kind") == "gun")
            speed = max(1.0, float(gun.get("stats", {}).get("speed", 252.0)))
            lead = min(1.1, dist / speed)
        else:
            lead = min(0.18, dist / 1200.0)
        tx = float(p[0]) + float(v[0]) * lead
        ty = float(p[1]) + float(v[1]) * lead
        tz = float(p[2]) + float(v[2]) * lead + 4.5
        yaw = math.atan2(ty - my, tx - mx)
        pitch = math.atan2(tz - muzzle, dist)

        # Mortar uses a ballistic arc; choose a low arc when it can reach.
        mortar = next((w for w in weapons if isinstance(w, dict) and w.get("kind") == "mortar"), None)
        if mortar is not None and dist > 18.0:
            stats = mortar.get("stats", {})
            speed = float(stats.get("speed", 72.0))
            gravity = float(stats.get("gravity", 22.0))
            dz = tz - muzzle
            disc = speed ** 4 - gravity * (gravity * dist ** 2 + 2.0 * dz * speed ** 2)
            if disc >= 0.0:
                pitch = math.atan((speed ** 2 - math.sqrt(disc)) / (gravity * dist))
        return yaw, clamp(pitch, -0.85, 0.85), dist

    def _can_fire(self, w: Any, dist: float, yaw_err: float, pitch_err: float,
                  energy: float, target_has_mortar: bool, slot: int) -> bool:
        if not isinstance(w, dict):
            self.laser_holding[slot] = False
            return False
        kind = w.get("kind")
        stats = w.get("stats", {})
        rng = float(stats.get("range", 200.0))
        cd = float(w.get("cooldown", 0.0))
        if kind == "gun":
            return (float(w.get("ammo", 0.0)) > 0.0 and float(w.get("reload", 0.0)) <= 0.0
                    and dist <= rng and yaw_err < 0.07 and pitch_err < 0.08)
        if kind == "lightning":
            return (energy >= 3.0 and cd <= 0.05 and dist <= min(rng, 45.0)
                    and yaw_err < math.radians(14.0))
        if kind == "mortar":
            return (cd <= 0.05 and 18.0 <= dist <= rng and yaw_err < 0.10 and pitch_err < 0.12)
        if kind == "laser":
            charge = float(w.get("charge", 0.0))
            full = float(stats.get("charge", 0.75))
            goal = full * (0.76 if target_has_mortar else 0.90)
            aligned = yaw_err < 0.045 and pitch_err < 0.06
            if charge <= 0.0 and not self.laser_holding[slot]:
                self.laser_holding[slot] = cd <= 0.05 and energy > 12.0
                return self.laser_holding[slot]
            if self.laser_holding[slot] and charge >= goal and aligned:
                self.laser_holding[slot] = False
                return False
            return self.laser_holding[slot] and energy > 3.0
        return False

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        pos = me.get("pos", [0.0, 0.0, 0.0])
        x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
        hp = float(me.get("hp", 1400.0))
        max_hp = max(1.0, float(me.get("max_hp", 1400.0)))
        hp_ratio = hp / max_hp
        energy = float(me.get("energy", 0.0))
        t = float(obs.get("t", 0.0))
        in_zone = bool(me.get("in_zone", True))
        weapons = me.get("weapons", [None, None])
        if len(weapons) < 2:
            weapons = list(weapons) + [None] * (2 - len(weapons))
        action: mf.Action = {"tick": obs["tick"]}
        if obs.get("offer"):
            action["pick"] = self._upgrade(obs["offer"])

        for ev in obs.get("events", []):
            if ev.get("type") == "damaged":
                self.last_damage_t = t
            elif ev.get("type") == "kill":
                self.last_kill_t = t
            elif ev.get("type") == "take_failed":
                self.skip_pickups[int(ev.get("id", -1))] = t + 15.0

        if self.last_pos is not None and math.hypot(x - self.last_pos[0], y - self.last_pos[1]) < 0.12:
            self.stuck += 1
        else:
            self.stuck = 0
        self.last_pos = (x, y)

        mechs = obs.get("mechs", [])
        enemies = [m for m in mechs if m.get("kind") == "mech" and m.get("id") != me.get("id")]
        zone = obs["zone"]
        center = zone.get("center", [0.0, 0.0])
        radius = float(zone.get("radius", 500.0))
        nearest = min(enemies, key=lambda m: d2(m["pos"], [x, y]), default=None)
        nearest_d = d2(nearest["pos"], [x, y]) if nearest else 9999.0

        def target_score(m: Dict[str, Any]) -> float:
            hp_m = float(m.get("hp", 1400.0))
            hp_max = max(1.0, float(m.get("max_hp", 1400.0)))
            d = math.dist(m.get("pos", [0, 0, 0]), [x, y, z])
            score = d + (hp_m / hp_max) * 55.0 - int(m.get("cubes", 0)) * 6.0
            try:
                bearing = math.atan2(y - float(m["pos"][1]), x - float(m["pos"][0]))
                if abs(wrap(float(m.get("aim", [bearing, 0.0])[0]) - bearing)) > 1.15:
                    score -= 18.0
            except (TypeError, ValueError, IndexError, KeyError):
                pass
            if hp_m <= 400.0:
                score -= 28.0
            return score

        target = min(enemies, key=target_score, default=None)
        if target is not None:
            if self.last_target is None or t > self.last_target_until or not any(e.get("id") == self.last_target for e in enemies):
                self.last_target = target.get("id")
                self.last_target_until = t + 1.4
            target = next((e for e in enemies if e.get("id") == self.last_target), target)

        can_engage = False
        distracted = False
        near_edge = False
        finisher = False
        if target is not None:
            ep = target.get("pos", [0.0, 0.0, 0.0])
            ehp = float(target.get("hp", 1400.0))
            emax = max(1.0, float(target.get("max_hp", 1400.0)))
            efrac = ehp / emax
            try:
                bearing = math.atan2(y - float(ep[1]), x - float(ep[0]))
                distracted = abs(wrap(float(target.get("aim", [bearing, 0.0])[0]) - bearing)) > 1.0
            except (TypeError, ValueError, IndexError, KeyError):
                distracted = False
            near_edge = math.hypot(float(ep[0]) - float(center[0]), float(ep[1]) - float(center[1])) > radius * 0.80
            finisher = ehp <= 400.0 and d2(ep, [x, y]) <= 70.0 and hp_ratio >= 0.08
            can_engage = efrac < 0.60 or distracted or near_edge or finisher
            # Back off from a close fight when low; still finish a nearly dead mech.
            if hp_ratio < 0.24 and nearest_d < 55.0 and not finisher:
                can_engage = False

        # Keep the base gun loaded.
        gun = weapons[0] if weapons[0] and weapons[0].get("kind") == "gun" else None
        if gun is not None:
            mag = float(gun.get("stats", {}).get("mag", 12.0))
            if float(gun.get("reload", 0.0)) <= 0.0 and float(gun.get("ammo", 0.0)) <= max(1.0, mag * 0.20):
                action["reload"] = True

        # Select a useful second weapon, but abandon the trip when danger is close.
        pickups = obs.get("pickups", [])
        if len(weapons) > 1 and weapons[1] is None and in_zone and nearest_d > 85.0:
            held = {w.get("kind") for w in weapons if isinstance(w, dict)}
            values = {"laser": 3.0, "lightning": 2.0, "mortar": 1.5}
            candidates = [p for p in pickups if p.get("kind") in values and p.get("kind") not in held
                          and self.skip_pickups.get(int(p.get("id", -1)), -1.0) <= t]
            pickup = min(candidates, key=lambda p: d2(p["pos"], [x, y]) / values[p["kind"]], default=None)
            if pickup is not None:
                pd = d2(pickup["pos"], [x, y])
                if pd <= self.pickup_radius and abs(float(pickup["pos"][2]) - z) <= 4.0:
                    action["take"] = pickup["id"]

        zone_x, zone_y = self._zone_goal(obs, x, y)
        move_x, move_y = zone_x - x, zone_y - y
        mode = "zone"
        hold_still = False
        safe_to_loot = nearest_d > 90.0 and (t - self.last_damage_t) > 2.0

        # Safe weapon pickup pursuit.
        if len(weapons) > 1 and weapons[1] is None and in_zone and safe_to_loot:
            held = {w.get("kind") for w in weapons if isinstance(w, dict)}
            vals = {"laser": 3.0, "lightning": 2.0, "mortar": 1.5}
            candidates = [p for p in pickups if p.get("kind") in vals and p.get("kind") not in held
                          and self.skip_pickups.get(int(p.get("id", -1)), -1.0) <= t]
            pickup = min(candidates, key=lambda p: d2(p["pos"], [x, y]) / vals[p["kind"]], default=None)
            if pickup is not None and d2(pickup["pos"], [x, y]) > self.pickup_radius:
                move_x, move_y = float(pickup["pos"][0]) - x, float(pickup["pos"][1]) - y
                mode = "weapon"

        # Collect visible cubes only when nobody is close enough to punish the detour.
        if safe_to_loot and mode == "zone":
            cubes = obs.get("cubes", [])
            cube = min(cubes, key=lambda c: d2(c["pos"], [x, y]), default=None)
            if cube is not None and d2(cube["pos"], [x, y]) < 28.0:
                move_x, move_y = float(cube["pos"][0]) - x, float(cube["pos"][1]) - y
                mode = "cube"

        # Fight only with a clean advantage. Otherwise move away from the nearest mech.
        inside_safe_margin = in_zone and math.hypot(x - float(center[0]), y - float(center[1])) < radius * 0.92
        fight_active = target is not None and can_engage and inside_safe_margin
        if fight_active:
            tx, ty = float(target["pos"][0]), float(target["pos"][1])
            dx, dy = tx - x, ty - y
            dist = math.hypot(dx, dy)
            bearing = math.atan2(dy, dx)
            if t >= self.strafe_until:
                self.strafe = self.rng.choice([-1.0, 1.0])
                self.strafe_until = t + self.rng.uniform(1.0, 2.0)
            kinds = {w.get("kind") for w in weapons if isinstance(w, dict)}
            desired = 48.0 if hp_ratio > 0.50 else 68.0
            if "lightning" in kinds and hp_ratio > 0.40:
                desired = 34.0
            if "mortar" in kinds:
                desired = max(desired, 88.0)
            if finisher:
                desired = min(desired, 38.0)
            radial = 0.0
            if dist < desired * 0.72:
                radial = -0.45
            elif dist > desired * 1.15:
                radial = 0.85
            sx, sy = math.cos(bearing + self.strafe * math.pi / 2), math.sin(bearing + self.strafe * math.pi / 2)
            rx, ry = unit(dx, dy)
            move_x, move_y = rx * radial * 60.0 + sx * 38.0 + (zone_x - x) * 0.25, ry * radial * 60.0 + sy * 38.0 + (zone_y - y) * 0.25
            mode = "fight"
            if dist > 70.0 and energy > 35.0:
                action["boost"] = True
        elif target is not None and nearest is not None and nearest_d < 150.0 and inside_safe_margin:
            away_x, away_y = unit(x - float(nearest["pos"][0]), y - float(nearest["pos"][1]))
            # If target is near the boundary, drift along the safe interior rather than out.
            edge_dir = unit(x - float(center[0]), y - float(center[1]))
            move_x = away_x * 70.0 + edge_dir[0] * 28.0 + (zone_x - x) * 0.25
            move_y = away_y * 70.0 + edge_dir[1] * 28.0 + (zone_y - y) * 0.25
            mode = "disengage"

        # Projected projectile sidestep, but never use a dodge that leaves the current safe circle.
        dodge = None
        for pr in obs.get("projectiles", []):
            pp, pv = pr.get("pos", [0.0, 0.0, 0.0]), pr.get("vel", [0.0, 0.0, 0.0])
            vx, vy = float(pv[0]), float(pv[1])
            vv = vx * vx + vy * vy
            if vv < 1.0:
                continue
            rx, ry = x - float(pp[0]), y - float(pp[1])
            tc = (rx * vx + ry * vy) / vv
            if tc < 0.0 or tc > 1.2:
                continue
            miss = math.hypot(rx - vx * tc, ry - vy * tc)
            if miss < (6.0 if pr.get("kind") == "mortar" else 4.0):
                side = 1.0 if rx * vy - ry * vx >= 0.0 else -1.0
                dodge = (-vy / math.sqrt(vv) * side, vx / math.sqrt(vv) * side)
                break
        if dodge is not None and in_zone:
            test_x = x + dodge[0] * 7.0
            test_y = y + dodge[1] * 7.0
            if math.hypot(test_x - float(center[0]), test_y - float(center[1])) < radius * 0.88:
                move_x, move_y = dodge[0] * 1.2 + move_x * 0.35, dodge[1] * 1.2 + move_y * 0.35
                mode += "_dodge"

        if self.stuck >= 7:
            move_x, move_y = -move_y + 0.15, move_x - 0.15
        move_len = math.hypot(move_x, move_y)
        action["move"] = [move_x / move_len, move_y / move_len] if move_len > 1e-8 else [0.0, 0.0]
        action.setdefault("boost", bool(not in_zone and energy > 15.0))
        action["jump"] = bool(me.get("grounded", False) and self.stuck >= 7)

        # Aim and fire at a vulnerable target; hold laser charge until its aim is ready.
        if fight_active:
            yaw, pitch, dist = self._aim(me, target, weapons)
            action["aim"] = [yaw, pitch]
            actual = me.get("aim", [yaw, pitch])
            yaw_err = abs(wrap(float(actual[0]) - yaw))
            pitch_err = abs(float(actual[1]) - pitch)
            target_has_mortar = "mortar" in target.get("weapons", [])
            action["fire"] = [
                self._can_fire(weapons[i], dist, yaw_err, pitch_err, energy, target_has_mortar, i)
                for i in range(2)
            ]
        else:
            self.laser_holding = [False, False]
            action["fire"] = [False, False]
            action["aim"] = [math.atan2(zone_y - y, zone_x - x), 0.0] if mode == "zone" else me.get("aim", [0.0, 0.0])

        mf.draw.label(f"SCRATCH | {mode} | hp {hp:.0f} | enemies {len(enemies)}")
        return action


mf.run(Bot)
