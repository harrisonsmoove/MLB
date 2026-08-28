"""Retrosheet play-string parser.

Retrosheet encodes a plate appearance as ``basic[/modifiers][.advances]``::

    S8/L               single to centre, line drive
    64(1)3/GDP         6-4-3 double play, runner from first forced
    D7/F.2-H;1-3       double to left, runner from second scores, first to third
    K.1X2(26)          strikeout, runner thrown out stealing second

The base-out transition and baserunner advancement matrices that the simulator
needs are estimated from these strings, so the parser has to be real rather than
approximate: "probability a runner on first scores on a double with one out" is
a number the simulator uses tens of thousands of times per game, and getting it
from a rough guess would put a systematic error into every price.

Every parse records ``parse_ok``. Coverage is reported rather than assumed --
see ``mlb-edge verify``. A parser that silently mislabels 3% of plays would bias
the advancement rates in a direction nobody would ever notice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Retrosheet event codes, as used in the canonical EVENT_CD field.
EVENT_UNKNOWN = 0
EVENT_NO_PLAY = 1
EVENT_GENERIC_OUT = 2
EVENT_STRIKEOUT = 3
EVENT_STOLEN_BASE = 4
EVENT_DEFENSIVE_INDIFF = 5
EVENT_CAUGHT_STEALING = 6
EVENT_PICKOFF = 8
EVENT_WILD_PITCH = 9
EVENT_PASSED_BALL = 10
EVENT_BALK = 11
EVENT_OTHER_ADVANCE = 12
EVENT_FOUL_ERROR = 13
EVENT_WALK = 14
EVENT_IBB = 15
EVENT_HBP = 16
EVENT_INTERFERENCE = 17
EVENT_ERROR = 18
EVENT_FIELDERS_CHOICE = 19
EVENT_SINGLE = 20
EVENT_DOUBLE = 21
EVENT_TRIPLE = 22
EVENT_HOME_RUN = 23

# Destination codes: 0 = out, 1/2/3 = base occupied, 4 = scored.
OUT = 0
SCORED = 4

_ADVANCE_RE = re.compile(r"^([B123])([-X])([123H])(.*)$")
_ERROR_IN_PAREN_RE = re.compile(r"\((?:[^)]*?)E\d(?:[^)]*?)\)")
_PUTOUT_IN_BASIC_RE = re.compile(r"\(([B123])\)")


@dataclass
class HalfInningState:
    """Runners and outs within one half-inning."""

    #: base number (1/2/3) -> runner identifier
    runners: dict[int, str] = field(default_factory=dict)
    outs: int = 0

    def base_state(self) -> int:
        """Bitmask: 1 = runner on first, 2 = second, 4 = third."""
        return sum(1 << (base - 1) for base in self.runners)

    def reset(self) -> None:
        self.runners.clear()
        self.outs = 0


@dataclass
class PlayResult:
    event_code: int
    runs: int
    outs: int
    batter_dest: int
    run1_dest: int | None
    run2_dest: int | None
    run3_dest: int | None
    start_base_state: int
    end_base_state: int
    outs_before: int
    is_batter_event: bool
    parse_ok: bool
    raw: str


def _strip_annotations(text: str) -> str:
    """Drop parenthetical annotations that are not runner putouts."""
    return re.sub(r"\((?![B123]\))[^)]*\)", "", text)


def classify_basic(basic: str) -> tuple[int, int | None, bool]:
    """Classify the basic play.

    Returns ``(event_code, batter_destination, is_batter_event)``. The
    destination is ``None`` when the play does not involve the batter reaching
    (a stolen base, a wild pitch), in which case the batter is still at the
    plate and the plate appearance continues.
    """
    text = basic.strip()
    if not text:
        return EVENT_UNKNOWN, None, False

    head = _strip_annotations(text)

    # Non-batter events: the batter stays at the plate.
    if head.startswith("NP"):
        return EVENT_NO_PLAY, None, False
    if head.startswith(("SB",)):
        return EVENT_STOLEN_BASE, None, False
    if head.startswith(("CS",)):
        return EVENT_CAUGHT_STEALING, None, False
    if head.startswith("POCS"):
        return EVENT_CAUGHT_STEALING, None, False
    if head.startswith("PO"):
        return EVENT_PICKOFF, None, False
    if head.startswith("WP"):
        return EVENT_WILD_PITCH, None, False
    if head.startswith("PB"):
        return EVENT_PASSED_BALL, None, False
    if head.startswith("BK"):
        return EVENT_BALK, None, False
    if head.startswith("DI"):
        return EVENT_DEFENSIVE_INDIFF, None, False
    if head.startswith("OA"):
        return EVENT_OTHER_ADVANCE, None, False
    if head.startswith("FLE"):
        return EVENT_FOUL_ERROR, None, False

    # Strikeout, possibly compounded ("K+WP", "K+SB2").
    if head.startswith("K"):
        return EVENT_STRIKEOUT, OUT, True

    # Walks, including the compound forms.
    if head.startswith(("IW", "I")) and not head.startswith("IF"):
        return EVENT_IBB, 1, True
    if head.startswith("W"):
        return EVENT_WALK, 1, True
    if head.startswith("HP"):
        return EVENT_HBP, 1, True

    # Hits. HR before H so that "HR" is not read as a hit-by-pitch variant.
    if head.startswith(("HR", "H")) and not head.startswith("HP"):
        return EVENT_HOME_RUN, SCORED, True
    if head.startswith("DGR"):
        return EVENT_DOUBLE, 2, True
    if head.startswith("S"):
        return EVENT_SINGLE, 1, True
    if head.startswith("D"):
        return EVENT_DOUBLE, 2, True
    if head.startswith("T"):
        return EVENT_TRIPLE, 3, True

    if head.startswith("C") and not head.startswith("CS"):
        return EVENT_INTERFERENCE, 1, True
    if head.startswith("FC"):
        return EVENT_FIELDERS_CHOICE, 1, True
    if head.startswith("E"):
        return EVENT_ERROR, 1, True

    # A bare fielder sequence ("63", "8", "543") is an out in play.
    if head and head[0].isdigit():
        return EVENT_GENERIC_OUT, OUT, True

    return EVENT_UNKNOWN, None, True


def parse_advances(advance_text: str) -> tuple[dict[str, int], bool]:
    """Parse the advancement clause into ``{runner: destination}``.

    ``B`` is the batter. ``X`` marks an out, *unless* the annotation carries an
    error, in which case the runner is safe -- ``2X3(E6)`` is a runner who
    should have been out at third and was not. Getting this backwards would
    inflate the out rate on exactly the plays where extra bases are taken.
    """
    destinations: dict[str, int] = {}
    ok = True
    if not advance_text:
        return destinations, ok

    for token in _split_advances(advance_text):
        match = _ADVANCE_RE.match(token.strip())
        if not match:
            ok = False
            continue
        runner, marker, target, annotation = match.groups()
        dest = SCORED if target == "H" else int(target)
        if marker == "X" and not _ERROR_IN_PAREN_RE.search(annotation):
            dest = OUT
        # Retrosheet may list a runner twice (an out overturned by an error).
        # The later entry wins, which matches how the file is written.
        destinations[runner] = dest
    return destinations, ok


def _split_advances(text: str) -> list[str]:
    """Split on ';' at paren depth zero."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        if char == ";" and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current))
    return parts


def parse_play(event: str, state: HalfInningState) -> PlayResult:
    """Apply one play string to ``state`` and return the transition."""
    raw = event.strip()
    start_state = state.base_state()
    outs_before = state.outs

    basic, _, remainder = raw.partition(".")
    advance_text = remainder
    basic_no_mods = basic.split("/")[0]

    event_code, batter_dest, is_batter_event = classify_basic(basic_no_mods)
    advances, advances_ok = parse_advances(advance_text)
    parse_ok = advances_ok and event_code != EVENT_UNKNOWN

    # Runners explicitly put out inside the basic play, e.g. "64(1)3" forcing
    # the runner from first. These are outs the advancement clause never
    # mentions, and missing them under-counts double plays badly.
    forced_out_runners = {
        marker for marker in _PUTOUT_IN_BASIC_RE.findall(basic_no_mods) if marker != "B"
    }

    # Start from the current occupancy, then apply what the play says.
    resolved: dict[str, int] = {}
    for base in (1, 2, 3):
        if base in state.runners:
            resolved[str(base)] = base  # stay put unless told otherwise

    for runner in forced_out_runners:
        resolved[runner] = OUT
    for runner, dest in advances.items():
        resolved[runner] = dest

    if is_batter_event:
        if "B" in advances:
            batter_dest = advances["B"]
        elif batter_dest is None:
            batter_dest = OUT
        # A home run scores every runner regardless of what the clause says,
        # and Retrosheet often omits the obvious advances.
        if event_code == EVENT_HOME_RUN:
            for base in (1, 2, 3):
                if base in state.runners:
                    resolved.setdefault(str(base), SCORED)
                    if resolved[str(base)] != SCORED:
                        resolved[str(base)] = SCORED
            batter_dest = SCORED
        # A walk forces the runner from first only when first is occupied.
        elif event_code in (EVENT_WALK, EVENT_IBB, EVENT_HBP) and 1 in state.runners:
            if "1" not in advances:
                resolved["1"] = 2
                if 2 in state.runners and "2" not in advances:
                    resolved["2"] = 3
                    if 3 in state.runners and "3" not in advances:
                        resolved["3"] = SCORED
    else:
        batter_dest = None

    runs = sum(1 for dest in resolved.values() if dest == SCORED)
    outs_on_play = sum(1 for dest in resolved.values() if dest == OUT)
    if is_batter_event and batter_dest == SCORED:
        runs += 1
    if is_batter_event and batter_dest == OUT:
        outs_on_play += 1

    # Rebuild occupancy.
    new_runners: dict[int, str] = {}
    for runner, dest in resolved.items():
        if dest in (1, 2, 3):
            new_runners[dest] = state.runners.get(int(runner), runner)
    if is_batter_event and batter_dest in (1, 2, 3):
        new_runners[batter_dest] = "B"

    run_dests = {base: resolved.get(str(base)) for base in (1, 2, 3)}

    result = PlayResult(
        event_code=event_code,
        runs=runs,
        outs=outs_on_play,
        batter_dest=batter_dest if batter_dest is not None else -1,
        run1_dest=run_dests[1] if 1 in state.runners else None,
        run2_dest=run_dests[2] if 2 in state.runners else None,
        run3_dest=run_dests[3] if 3 in state.runners else None,
        start_base_state=start_state,
        end_base_state=sum(1 << (base - 1) for base in new_runners),
        outs_before=outs_before,
        is_batter_event=is_batter_event,
        parse_ok=parse_ok,
        raw=raw,
    )

    state.runners = new_runners
    state.outs = outs_before + outs_on_play
    if state.outs >= 3:
        state.reset()
    return result
