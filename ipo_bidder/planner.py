"""Scoring and the fund-aware planner (same logic as the backtest)."""
import itertools
from datetime import date, timedelta


def bday_add(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def chance_from_sub(retail_sub, default: float) -> float:
    if retail_sub is None or retail_sub <= 0:
        return default
    return min(1.0, 1.0 / retail_sub)


def score(gmp_pct: float, chance: float, is_today: bool, s: dict) -> float:
    v = gmp_pct * chance
    if gmp_pct < s["penalty_below_pct"]:
        v *= s["penalty"]
    if not is_today:
        v *= s["future_discount"]
    return v


def plan(today: date, candidates: list, locked_on, budget: float) -> list:
    """candidates: dicts with keys day (close date), refund (date), amount, score.
    locked_on(d) -> rupees already committed on day d (funds usable only from the day after release).
    Returns the subset of today's candidates to apply for.
    """
    cands = sorted(candidates, key=lambda c: -c["score"])[:12]
    if not cands:
        return []
    days = sorted({c["day"] for c in cands})
    best, best_s = [], -1.0
    for r in range(1, len(cands) + 1):
        for combo in itertools.combinations(cands, r):
            ok = True
            for d in days:
                need = sum(c["amount"] for c in combo if c["day"] <= d and c["refund"] >= d)
                if need + locked_on(d) > budget + 1e-6:
                    ok = False
                    break
            if ok:
                tot = sum(c["score"] for c in combo)
                if tot > best_s:
                    best, best_s = list(combo), tot
    return [c for c in best if c["day"] == today]
