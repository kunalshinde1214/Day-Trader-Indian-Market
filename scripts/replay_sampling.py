"""Same-clock bootstrap policies shared by broker and public research replay."""
from core import Block


def policy(config):
    mode = config.get("replay_sampling", "independent")
    if mode not in ("independent", "block", "session"):
        raise Block("REPLAY: sampling must be independent, block or session")
    if mode != "block" and "replay_block_bars" in config:
        raise Block("REPLAY: block bars require block sampling")
    bars = config.get("replay_block_bars", 6) if mode == "block" else None
    if mode == "block" and (type(bars) is not int or not 1 <= bars <= 375):
        raise Block("REPLAY: block bars must be an integer from 1 to 375")
    if type(config.get("replay_seed")) is not int:
        raise Block("REPLAY: seed must be an integer")
    return {"mode": mode, "block_bars": bars}


def draw_slots(pool, rng, sampling):
    """Pool columns must share historical session order; stop consuming at exit."""
    width = 1 if sampling["mode"] == "independent" else (
        sampling["block_bars"] if sampling["mode"] == "block" else len(pool))
    for k, slot in enumerate(pool):
        if k % width == 0:
            index = rng.randrange(len(slot))
        yield slot[index]


def limitation(sampling):
    if sampling["mode"] == "independent":
        return "Independent same-slot draws lose serial dependence and may miss event regimes."
    if sampling["mode"] == "block":
        return "Same-slot blocks preserve consecutive moves within each block only; dependence breaks at joins."
    return "Whole-session draws preserve historical tail sequences; repeated draws do not create new historical evidence."
