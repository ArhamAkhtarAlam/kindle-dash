"""Touch games on the Kindle: against the Pi, two players on one Kindle, or solo.

Games: Tic-tac-toe, Connect 4, Reversi, Checkers, Chess, Battleship (with player PINs for 2 players),
a daily Sudoku and Minesweeper.
The picker has a vs Pi / 2 players switch and an Easy / Normal / Hard switch. The Pi is
never perfect: at every level it sometimes plays a weaker move on purpose.
Player 1 is always the filled/black pieces, player 2 (or the Pi) the outlined ones, and
who goes first alternates between games.

State lives in out/games.json; tap targets in out/games_layout.json.
"""

import functools
import json
import os
import random
import subprocess
import time

from PIL import Image, ImageDraw, ImageFont

import render

STATE_FILE = os.path.join(render.OUT_DIR, "games.json")
LAYOUT_FILE = os.path.join(render.OUT_DIR, "games_layout.json")
P1, P2 = 1, 2
M = 50

GAMES = [  # key, name, modes it supports
    ("ttt", "Tic-tac-toe", ("pi", "2p")),
    ("c4", "Connect 4", ("pi", "2p")),
    ("reversi", "Reversi", ("pi", "2p")),
    ("checkers", "Checkers", ("pi", "2p")),
    ("chess", "Chess", ("pi", "2p")),
    ("battleship", "Battleship", ("pi", "2p")),
    ("sudoku", "Sudoku", ("solo",)),
    ("mines", "Minesweeper", ("solo",)),
]
NAMES = {k: n for k, n, _ in GAMES}
MODES = {k: m for k, _, m in GAMES}

# How the Pi plays at each level. "slip" = chance of deliberately playing a random (legal)
# move instead of its best one; the other numbers are how hard it thinks.
LEVELS = {
    "easy":   {"slip": 0.45, "c4_depth": 1, "rev_depth": 0, "ck_depth": 1, "chess_depth": 1},
    "normal": {"slip": 0.22, "c4_depth": 3, "rev_depth": 1, "ck_depth": 3, "chess_depth": 2},
    "hard":   {"slip": 0.10, "c4_depth": 4, "rev_depth": 2, "ck_depth": 4, "chess_depth": 4},
    # never slips; searches as deep as it can in THINK_SECONDS (chess: Stockfish)
    "impossible": {"slip": 0.0, "c4_depth": 4, "rev_depth": 2, "ck_depth": 4, "chess_depth": 4},
}


# ------------------------------ state -------------------------------

def load():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st.setdefault("game", None)
    st.setdefault("mode", "pi")
    st.setdefault("level", "normal")
    st.setdefault("score", {})
    return st


def save(st):
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(st, f)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


def _mode(st):
    modes = MODES[st["game"]]
    return st["mode"] if st["mode"] in modes else modes[0]


def _lvl(st):
    return LEVELS.get(st.get("level"), LEVELS["normal"])


def finished(st):
    return st.get("winner") is not None


def pi_turn(st):
    return st.get("game") and _mode(st) == "pi" and st["turn"] == P2 and not finished(st)


def new_game(st, game):
    first = P2 if st.get("last_first") == P1 else P1
    if MODES[game] == ("solo",):
        first = P1
    st.update(game=game, turn=first, last_first=first, winner=None, line=[], msg="")
    for k in ("board", "mines", "open", "flags", "flag_mode", "sel", "quiet", "puzzle", "solution",
              "entries", "check", "title", "fleets", "shots", "pins", "typed", "phase", "who", "after",
              "white", "side", "castling", "ep", "half", "last", "reps"):
        st.pop(k, None)
    INIT[game](st)
    run_pi(st)
    return st


def end(st, winner, line=()):
    """Record the result: winner P1, P2, or -1 for a draw."""
    st["winner"], st["line"] = winner, list(line)
    mode = _mode(st)
    key = "%s:%s" % (st["game"], mode)
    names = {"pi": ("you", "pi"), "2p": ("p1", "p2"), "solo": ("won", "lost")}[mode]
    sc = st["score"].setdefault(key, {names[0]: 0, names[1]: 0, "draws": 0})
    sc[{P1: names[0], P2: names[1], -1: "draws"}[winner]] += 1


def run_pi(st):
    guard = 0
    while pi_turn(st) and guard < 50:
        PI_MOVE[st["game"]](st)
        guard += 1


def play(st, move):
    """A tap on the board by whoever's turn it is. Returns False if it wasn't allowed."""
    if finished(st):
        return False
    if not HUMAN_MOVE[st["game"]](st, move):
        return False
    if not _impossible(st):                                # (impossible: think() runs after the redraw)
        run_pi(st)
    return True


def _slip(st):
    return random.random() < _lvl(st)["slip"]


# ------------------------- impossible: the best play ------------------------
# Deep searches against the clock (iterative deepening: the deepest finished search wins).

THINK_SECONDS = 2.5
STOCKFISH = os.path.join(render.BASE_DIR, "engines", "stockfish")
STOCKFISH_MS = 1500


def _impossible(st):
    return st.get("level") == "impossible"


class _Timeout(Exception):
    pass


_deadline = [None, 0]                                     # (time limit, node counter)


def _tick():
    if _deadline[0] is not None:
        _deadline[1] += 1
        if _deadline[1] & 255 == 0 and time.time() > _deadline[0]:
            raise _Timeout


def _deepen(search, first=1, last=60, seconds=THINK_SECONDS):
    """search(depth) -> (score, move) for deeper and deeper searches until time's up."""
    best = None
    _deadline[:] = [time.time() + seconds, 0]
    try:
        for depth in range(first, last + 1):
            best = search(depth)
            if abs(best[0]) >= 10 ** 5:                    # a forced win or loss: no need to look deeper
                break
    except _Timeout:
        pass
    finally:
        _deadline[0] = None
    return best


def think():
    """Impossible: the Pi's reply, run after your move is already on screen. True if it moved."""
    st = load()
    if not pi_turn(st):
        return False
    run_pi(st)
    save(st)
    return True


# --------------------------- tic-tac-toe ----------------------------

TTT_LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]


def _ttt_win(b):
    for l in TTT_LINES:
        if b[l[0]] and b[l[0]] == b[l[1]] == b[l[2]]:
            return b[l[0]], l
    return (0, ()) if 0 in b else (-1, ())


def _ttt_minimax(b, p):
    return _ttt_search(tuple(b), p)


@functools.lru_cache(maxsize=None)            # only ~6,000 positions exist: remember them
def _ttt_search(b, p):
    w, _ = _ttt_win(b)
    if w:
        return {P2: 1, P1: -1, -1: 0}[w], None
    best = (-2, None) if p == P2 else (2, None)
    for i in range(9):
        if b[i] == 0:
            score, _ = _ttt_search(b[:i] + (p,) + b[i + 1:], 3 - p)
            if (p == P2 and score > best[0]) or (p == P1 and score < best[0]):
                best = (score, i)
    return best


def _ttt_init(st):
    st["board"] = [0] * 9


def _ttt_place(st, i):
    b = st["board"]
    if not 0 <= i < 9 or b[i]:
        return False
    b[i] = st["turn"]
    w, line = _ttt_win(b)
    if w:
        end(st, w, line)
    else:
        st["turn"] = 3 - st["turn"]
    return True


def _ttt_pi(st):
    b = st["board"]
    free = [i for i in range(9) if b[i] == 0]
    _ttt_place(st, random.choice(free) if _slip(st) else _ttt_minimax(b, P2)[1])


# ----------------------------- connect 4 ----------------------------

def _c4_drop(board, col, p):
    for row in range(6):
        if board[col][row] == 0:
            board[col][row] = p
            return row
    return None


def _c4_win(board):
    for c in range(7):
        for r in range(6):
            p = board[c][r]
            if not p:
                continue
            for dc, dr in ((1, 0), (0, 1), (1, 1), (1, -1)):
                cells = [(c + k * dc, r + k * dr) for k in range(4)]
                if all(0 <= x < 7 and 0 <= y < 6 and board[x][y] == p for x, y in cells):
                    return p, cells
    return (0, ()) if any(0 in col for col in board) else (-1, ())


def _c4_score(board):
    score = 3 * (board[3].count(P2) - board[3].count(P1))
    for c in range(7):
        for r in range(6):
            for dc, dr in ((1, 0), (0, 1), (1, 1), (1, -1)):
                cells = [(c + k * dc, r + k * dr) for k in range(4)]
                if not all(0 <= x < 7 and 0 <= y < 6 for x, y in cells):
                    continue
                vals = [board[x][y] for x, y in cells]
                for who, sign in ((P2, 1), (P1, -1)):
                    if 3 - who not in vals:
                        score += sign * {2: 2, 3: 10}.get(vals.count(who), 0)
    return score


def _c4_search(board, depth, alpha, beta, p):
    w, _ = _c4_win(board)
    if w == P2:
        return 100000 + depth, None
    if w == P1:
        return -100000 - depth, None
    if w == -1 or depth == 0:
        return _c4_score(board), None
    order = [c for c in (3, 2, 4, 1, 5, 0, 6) if board[c][5] == 0]
    best = order[0]
    value = -10 ** 9 if p == P2 else 10 ** 9
    for c in order:
        r = _c4_drop(board, c, p)
        score, _ = _c4_search(board, depth - 1, alpha, beta, 3 - p)
        board[c][r] = 0
        if (p == P2 and score > value) or (p == P1 and score < value):
            value, best = score, c
        if p == P2:
            alpha = max(alpha, value)
        else:
            beta = min(beta, value)
        if alpha >= beta:
            break
    return value, best


def _c4_init(st):
    st["board"] = [[0] * 6 for _ in range(7)]


def _c4_place(st, col):
    if not 0 <= col < 7 or _c4_drop(st["board"], col, st["turn"]) is None:
        return False
    w, line = _c4_win(st["board"])
    if w:
        end(st, w, line)
    else:
        st["turn"] = 3 - st["turn"]
    return True


# Impossible Connect 4: bitboards (7 bits per column: 6 cells + a spare), negamax with a
# transposition table, and threat counting at the horizon.
C4_BOTTOM = sum(1 << (c * 7) for c in range(7))
C4_FULL = C4_BOTTOM * 63
C4_ORDER = (3, 2, 4, 1, 5, 0, 6)
C4_WIN = 10 ** 5


def _c4_threats(pos, mask):
    """Empty cells that would complete four for the player whose pieces are pos."""
    r = (pos << 1) & (pos << 2) & (pos << 3)
    for s in (7, 6, 8):
        p = (pos << s) & (pos << 2 * s)
        r |= p & (pos << 3 * s)
        r |= p & (pos >> s)
        p = (pos >> s) & (pos >> 2 * s)
        r |= p & (pos << s)
        r |= p & (pos >> 3 * s)
    return r & (C4_FULL ^ mask)


def _c4_negamax(cur, mask, n, alpha, beta, depth, tt):
    _tick()
    if n >= 42:
        return 0
    poss = (mask + C4_BOTTOM) & C4_FULL
    if _c4_threats(cur, mask) & poss:
        return C4_WIN + (43 - n)                           # we win next move (sooner = better)
    opp = cur ^ mask
    opp_threats = _c4_threats(opp, mask)
    forced = poss & opp_threats
    if forced:
        if forced & (forced - 1):
            return -C4_WIN - (42 - n)                      # two threats to stop: lost
        poss = forced
    poss &= ~(opp_threats >> 1)                            # don't play under their winning cell
    if not poss:
        return -C4_WIN - (42 - n)
    if depth == 0:
        mine, theirs = _c4_threats(cur, mask), opp_threats
        center = (cur >> 21 & 63).bit_count() - (opp >> 21 & 63).bit_count()
        return 10 * (mine.bit_count() - theirs.bit_count()) + 3 * center
    key = (cur, mask)
    hit = tt.get(key)
    if hit and hit[0] >= depth:
        if hit[2] == 0:
            return hit[1]
        if hit[2] > 0:
            alpha = max(alpha, hit[1])
        else:
            beta = min(beta, hit[1])
        if alpha >= beta:
            return hit[1]
    moves = []
    for c in C4_ORDER:
        m = poss & (63 << (c * 7))
        if m:
            moves.append(((_c4_threats(cur | m, mask | m)).bit_count(), m))
    moves.sort(key=lambda x: -x[0])
    a0, best = alpha, -10 ** 9
    for _, m in moves:
        v = -_c4_negamax(cur ^ mask, mask | m, n + 1, -beta, -alpha, depth - 1, tt)
        if v > best:
            best = v
        alpha = max(alpha, v)
        if alpha >= beta:
            break
    tt[key] = (depth, best, 1 if best >= beta else -1 if best <= a0 else 0)
    return best


def _c4_best(board, p):
    cur = mask = 0
    n = 0
    for c in range(7):
        for r in range(6):
            if board[c][r]:
                bit = 1 << (c * 7 + r)
                mask |= bit
                n += 1
                if board[c][r] == p:
                    cur |= bit
    poss = (mask + C4_BOTTOM) & C4_FULL
    cols = [c for c in C4_ORDER if poss & (63 << (c * 7))]
    tt = {}

    def search(depth):
        best, best_col = -10 ** 9, cols[0]
        for c in cols:
            m = poss & (63 << (c * 7))
            if _c4_threats(cur, mask) & m:
                return C4_WIN + 50, c                      # win right now
            v = -_c4_negamax(cur ^ mask, mask | m, n + 1, -10 ** 9, -best, depth - 1, tt)
            if v > best:
                best, best_col = v, c
        return best, best_col
    return _deepen(search, 1, 42 - n)[1]


def _c4_pi(st):
    b = st["board"]
    free = [c for c in range(7) if b[c][5] == 0]
    if _impossible(st):
        _c4_place(st, _c4_best(b, P2))
        return
    if _slip(st):
        col = random.choice(free)
    else:
        col = _c4_search(b, _lvl(st)["c4_depth"], -10 ** 9, 10 ** 9, P2)[1]
    _c4_place(st, col)


# ------------------------------ reversi -----------------------------

DIRS8 = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
# Corners are gold, the squares next to them are traps: the usual Reversi wisdom.
REV_WEIGHTS = [100, -20, 10, 5, 5, 10, -20, 100,
               -20, -40, -2, -2, -2, -2, -40, -20,
               10, -2, 1, 1, 1, 1, -2, 10,
               5, -2, 1, 0, 0, 1, -2, 5,
               5, -2, 1, 0, 0, 1, -2, 5,
               10, -2, 1, 1, 1, 1, -2, 10,
               -20, -40, -2, -2, -2, -2, -40, -20,
               100, -20, 10, 5, 5, 10, -20, 100]


def _rev_flips(b, i, p):
    if b[i]:
        return []
    r0, c0 = divmod(i, 8)
    out = []
    for dr, dc in DIRS8:
        r, c, run = r0 + dr, c0 + dc, []
        while 0 <= r < 8 and 0 <= c < 8 and b[r * 8 + c] == 3 - p:
            run.append(r * 8 + c)
            r, c = r + dr, c + dc
        if run and 0 <= r < 8 and 0 <= c < 8 and b[r * 8 + c] == p:
            out += run
    return out


def _rev_moves(b, p):
    return [i for i in range(64) if _rev_flips(b, i, p)]


def _rev_init(st):
    b = [0] * 64
    b[27], b[36], b[28], b[35] = P2, P2, P1, P1
    st["board"] = b


def _rev_eval(b, p):
    pos = sum(REV_WEIGHTS[i] * (1 if v == p else -1) for i, v in enumerate(b) if v)
    return pos + 5 * (len(_rev_moves(b, p)) - len(_rev_moves(b, 3 - p)))


def _rev_best(b, p, depth):
    best, best_score = None, -10 ** 9
    for i in _rev_moves(b, p):
        nb = b[:]
        nb[i] = p
        for f in _rev_flips(b, i, p):
            nb[f] = p
        if depth > 0 and _rev_moves(nb, 3 - p):
            # assume the opponent replies with its own best move
            reply = _rev_best(nb, 3 - p, depth - 1)[0]
            nb2 = nb[:]
            nb2[reply] = 3 - p
            for f in _rev_flips(nb, reply, 3 - p):
                nb2[f] = 3 - p
            score = _rev_eval(nb2, p)
        else:
            score = _rev_eval(nb, p)
        if score > best_score:
            best, best_score = i, score
    return best, best_score


def _rev_place(st, i):
    b, p = st["board"], st["turn"]
    flips = _rev_flips(b, i, p) if 0 <= i < 64 else []
    if not flips:
        return False
    b[i] = p
    for f in flips:
        b[f] = p
    st["msg"] = ""
    if _rev_moves(b, 3 - p):
        st["turn"] = 3 - p
    elif _rev_moves(b, p):
        st["msg"] = "%s has no move, so %s goes again" % (_who(st, 3 - p), _who(st, p).lower())
    else:
        n1, n2 = b.count(P1), b.count(P2)
        end(st, P1 if n1 > n2 else P2 if n2 > n1 else -1)
    return True


def _rev_apply(b, i, p):
    nb = b[:]
    nb[i] = p
    for f in _rev_flips(b, i, p):
        nb[f] = p
    return nb


def _rev_negamax(b, p, depth, alpha, beta, passed=False):
    """Score for p to move: positions, mobility and corners; exact disc count at the end."""
    _tick()
    moves = _rev_moves(b, p)
    if not moves:
        if passed or not _rev_moves(b, 3 - p):            # nobody can move: game over
            diff = b.count(p) - b.count(3 - p)
            return (10 ** 5 + diff) if diff > 0 else (-10 ** 5 + diff) if diff < 0 else 0
        return -_rev_negamax(b, 3 - p, depth, -beta, -alpha, True)
    if depth == 0:
        return _rev_eval(b, p)
    moves.sort(key=lambda i: -REV_WEIGHTS[i])              # corners first
    best = -10 ** 9
    for i in moves:
        v = -_rev_negamax(_rev_apply(b, i, p), 3 - p, depth - 1, -beta, -alpha)
        best = max(best, v)
        alpha = max(alpha, v)
        if alpha >= beta:
            break
    return best


def _rev_deep(b, p):
    moves = sorted(_rev_moves(b, p), key=lambda i: -REV_WEIGHTS[i])
    empties = b.count(0)

    def search(depth):
        best, best_i = -10 ** 9, moves[0]
        for i in moves:
            v = -_rev_negamax(_rev_apply(b, i, p), 3 - p, depth - 1, -10 ** 9, -best)
            if v > best:
                best, best_i = v, i
        return best, best_i
    return _deepen(search, 1, empties)[1]


def _rev_pi(st):
    b = st["board"]
    moves = _rev_moves(b, P2)
    if _impossible(st):
        _rev_place(st, _rev_deep(b, P2))
        return
    if _slip(st):
        _rev_place(st, random.choice(moves))
    else:
        _rev_place(st, _rev_best(b, P2, _lvl(st)["rev_depth"])[0])


# ---------------------------- minesweeper ---------------------------

MS_COLS, MS_ROWS, MS_MINES = 16, 10, 25


def _ms_init(st):
    st.update(mines=[], open=[], flags=[], flag_mode=False)


def _ms_neighbours(i):
    r0, c0 = divmod(i, MS_COLS)
    return [r * MS_COLS + c for r in range(r0 - 1, r0 + 2) for c in range(c0 - 1, c0 + 2)
            if 0 <= r < MS_ROWS and 0 <= c < MS_COLS and (r, c) != (r0, c0)]


def _ms_count(st, i):
    mines = set(st["mines"])
    return sum(1 for n in _ms_neighbours(i) if n in mines)


def _ms_tap(st, i):
    if not 0 <= i < MS_COLS * MS_ROWS or i in st["open"]:
        return False
    if st["flag_mode"]:
        st["flags"].remove(i) if i in st["flags"] else st["flags"].append(i)
        return True
    if i in st["flags"]:
        return False
    if not st["mines"]:                                # first tap is always safe (and opens up)
        safe = set(_ms_neighbours(i) + [i])
        st["mines"] = random.sample([k for k in range(MS_COLS * MS_ROWS) if k not in safe], MS_MINES)
    if i in st["mines"]:
        st["open"].append(i)
        end(st, P2, [i])
        return True
    todo, opened = [i], set(st["open"])
    while todo:                                        # open the empty area around a 0
        k = todo.pop()
        if k in opened or k in st["flags"]:
            continue
        opened.add(k)
        if _ms_count(st, k) == 0:
            todo += [n for n in _ms_neighbours(k) if n not in opened]
    st["open"] = sorted(opened)
    if len(st["open"]) == MS_COLS * MS_ROWS - MS_MINES:
        end(st, P1)
    return True



# ------------------------------ checkers ----------------------------
# 1/2 = men, 3/4 = kings of player 1/2. Player 1 starts at the bottom and moves up.

def _ck_owner(v):
    return 0 if not v else (P1 if v in (1, 3) else P2)


def _ck_init(st):
    b = [0] * 64
    for r in range(8):
        for c in range(8):
            if (r + c) % 2 == 1:
                if r < 3:
                    b[r * 8 + c] = 2
                elif r > 4:
                    b[r * 8 + c] = 1
    st.update(board=b, sel=[], quiet=0)


def _ck_dirs(v):
    if v in (3, 4):
        return [(-1, -1), (-1, 1), (1, -1), (1, 1)]
    return [(-1, -1), (-1, 1)] if v == 1 else [(1, -1), (1, 1)]


def _ck_jumps(b, i, v, path, taken):
    """All capture sequences from square i (multi-jumps; a man that gets crowned stops)."""
    r0, c0 = divmod(i, 8)
    out = []
    for dr, dc in _ck_dirs(v):
        r1, c1, r2, c2 = r0 + dr, c0 + dc, r0 + 2 * dr, c0 + 2 * dc
        if 0 <= r2 < 8 and 0 <= c2 < 8:
            mid, dest = r1 * 8 + c1, r2 * 8 + c2
            if _ck_owner(b[mid]) == 3 - _ck_owner(v) and mid not in taken and b[dest] == 0:
                crowned = v in (1, 2) and r2 in (0, 7)
                more = [] if crowned else _ck_jumps(b, dest, v, path + [dest], taken + [mid])
                out += more or [(path + [dest], taken + [mid])]
    return out


def _ck_moves(b, p):
    """Legal moves for p as (path, captured). Captures are compulsory."""
    jumps, steps = [], []
    for i, v in enumerate(b):
        if _ck_owner(v) != p:
            continue
        saved, b[i] = b[i], 0                              # the moving piece leaves its square
        jumps += _ck_jumps(b, i, v, [i], [])
        b[i] = saved
        r0, c0 = divmod(i, 8)
        for dr, dc in _ck_dirs(v):
            r, c = r0 + dr, c0 + dc
            if 0 <= r < 8 and 0 <= c < 8 and b[r * 8 + c] == 0:
                steps.append(([i, r * 8 + c], []))
    return jumps or steps


def _ck_apply(b, move):
    path, taken = move
    v = b[path[0]]
    b[path[0]] = 0
    for t in taken:
        b[t] = 0
    end_r = path[-1] // 8
    if v == 1 and end_r == 0:
        v = 3
    elif v == 2 and end_r == 7:
        v = 4
    b[path[-1]] = v


def _ck_eval(b):
    score = 0
    for i, v in enumerate(b):
        if v:
            r = i // 8
            val = {1: 100 + (7 - r) * 3, 2: 100 + r * 3, 3: 170, 4: 170}[v]
            score += val if _ck_owner(v) == P2 else -val
    return score


def _ck_search(b, depth, alpha, beta, p):
    _tick()
    moves = _ck_moves(b, p)
    if not moves:
        return (-10 ** 6 if p == P2 else 10 ** 6), None
    if depth == 0:
        return _ck_eval(b), None
    best = moves[0]
    value = -10 ** 9 if p == P2 else 10 ** 9
    for m in moves:
        nb = b[:]
        _ck_apply(nb, m)
        score, _ = _ck_search(nb, depth - 1, alpha, beta, 3 - p)
        if (p == P2 and score > value) or (p == P1 and score < value):
            value, best = score, m
        if p == P2:
            alpha = max(alpha, value)
        else:
            beta = min(beta, value)
        if alpha >= beta:
            break
    return value, best


def _ck_play(st, move):
    _ck_apply(st["board"], move)
    st["sel"] = []
    st["quiet"] = 0 if move[1] else st["quiet"] + 1
    p = st["turn"]
    if not _ck_moves(st["board"], 3 - p):
        end(st, p)                                         # the other side can't move: p wins
    elif st["quiet"] >= 80:
        end(st, -1)                                        # 40 moves each without a capture
    else:
        st["turn"] = 3 - p


def _ck_tap(st, i):
    """Tap a piece, then where it goes (each landing square in turn for a multi-jump)."""
    b, p = st["board"], st["turn"]
    if not 0 <= i < 64:
        return False
    moves = _ck_moves(b, p)
    sel = st.get("sel") or []
    if sel:
        n = len(sel)
        cands = [m for m in moves if m[0][:n] == sel and len(m[0]) > n and m[0][n] == i]
        if cands:
            done = [m for m in cands if len(m[0]) == n + 1]
            if done and len(done) == len(cands):
                _ck_play(st, done[0])
            else:
                st["sel"] = sel + [i]
            return True
    if any(m[0][0] == i for m in moves):
        st["sel"] = [i]
        return True
    if sel:
        st["sel"] = []
        return True
    return False


def _ck_pi(st):
    moves = _ck_moves(st["board"], P2)
    if _impossible(st):
        if len(moves) == 1:
            _ck_play(st, moves[0])
            return
        found = _deepen(lambda depth: _ck_search(st["board"], depth, -10 ** 9, 10 ** 9, P2), 2, 40)
        _ck_play(st, found[1] if found and found[1] else moves[0])
        return
    move = random.choice(moves) if _slip(st) else \
        _ck_search(st["board"], _lvl(st)["ck_depth"], -10 ** 9, 10 ** 9, P2)[1]
    _ck_play(st, move)


# ------------------------------- sudoku -----------------------------
# A puzzle a day per difficulty (same date + level = same puzzle), or a random one.

SU_CLUES = {"easy": 40, "normal": 32, "hard": 27, "impossible": 22}


def _su_ok(g, i, v):
    r, c = divmod(i, 9)
    br, bc = r // 3 * 3, c // 3 * 3
    for k in range(9):
        if g[r * 9 + k] == v or g[k * 9 + c] == v or g[(br + k // 3) * 9 + bc + k % 3] == v:
            return False
    return True


def _su_solve(g, rng=None, limit=2):
    """Fill g in place (randomly if rng); returns how many solutions were found (up to limit)."""
    try:
        i = g.index(0)
    except ValueError:
        return 1
    digits = list(range(1, 10))
    if rng:
        rng.shuffle(digits)
    count = 0
    for v in digits:
        if _su_ok(g, i, v):
            g[i] = v
            count += _su_solve(g, rng, limit - count)
            if count >= limit or (rng and count):
                if rng:
                    return count                           # keep the filled grid
                g[i] = 0
                return count
            g[i] = 0
    return count


def _su_make(seed, clues):
    rng = random.Random(seed)
    sol = [0] * 81
    _su_solve(sol, rng)
    puzzle = sol[:]
    cells = list(range(81))
    rng.shuffle(cells)
    for i in cells:                                        # remove clues while it stays unique
        if sum(1 for v in puzzle if v) <= clues:
            break
        keep, puzzle[i] = puzzle[i], 0
        if _su_solve(puzzle[:], None, 2) != 1:
            puzzle[i] = keep
    return puzzle, sol


def _su_init(st, seed=None):
    level = st.get("level", "normal")
    daily = seed is None
    seed = seed or "%s-%s" % (time.strftime("%Y-%m-%d"), level)
    puzzle, sol = _su_make(seed, SU_CLUES[level])
    st.update(puzzle=puzzle, solution=sol, entries=[0] * 81, sel=None, check=False,
              title="Daily Sudoku" if daily else "Sudoku", turn=P1)


def _su_tap(st, i):
    if 0 <= i < 81 and not st["puzzle"][i]:
        st["sel"] = i
        return True
    return False


def _su_number(st, n):
    i = st.get("sel")
    if i is None or st["puzzle"][i] or finished(st):
        return False
    st["entries"][i] = n
    if all((st["puzzle"][k] or st["entries"][k]) == st["solution"][k] for k in range(81)):
        end(st, P1)
    return True


# ----------------------------- battleship ---------------------------
# vs the Pi, or two players taking turns on one Kindle with a PIN each, so neither sees the
# other's fleet. Phases: pin_set -> place -> (handoff -> pin) -> play -> fired -> handoff ...

BS_N = 10
BS_SHIPS = [5, 4, 3, 3, 2]


def _bs_random_fleet(rng=random):
    taken, fleet = set(), []
    for size in BS_SHIPS:
        while True:
            horiz = rng.random() < 0.5
            r = rng.randrange(BS_N if horiz else BS_N - size + 1)
            c = rng.randrange(BS_N - size + 1 if horiz else BS_N)
            cells = [r * BS_N + c + (k if horiz else k * BS_N) for k in range(size)]
            if not any(x in taken for x in cells):
                taken.update(cells)
                fleet.append(cells)
                break
    return fleet


def _bs_init(st):
    st.update(fleets={"1": _bs_random_fleet(), "2": _bs_random_fleet()}, shots={"1": [], "2": []},
              pins={}, typed="", turn=P1, msg="")
    if _mode(st) == "2p":
        st.update(phase="pin_set", who=P1)
    else:
        st.update(phase="place", who=P1)


def _bs_other(p):
    return str(3 - int(p))


def _bs_sunk(fleet, shots):
    return [ship for ship in fleet if all(c in shots for c in ship)]


def _bs_fire(st, p, cell):
    """Player p fires at cell on the opponent's board. Returns a short result."""
    shots = st["shots"][str(p)]
    if cell in shots:
        return None
    shots.append(cell)
    fleet = st["fleets"][_bs_other(p)]
    hit = any(cell in ship for ship in fleet)
    if not hit:
        return "miss"
    ship = next(s for s in fleet if cell in s)
    if all(c in shots for c in ship):
        if len(_bs_sunk(fleet, shots)) == len(fleet):
            end(st, p)
            return "sank the last ship"
        return "sank a %d-long ship" % len(ship)
    return "hit"


def _bs_density(st):
    """Impossible: the square most likely to hold a ship, from everything the Pi has seen
    (its hits, misses and which ships are sunk; never your actual fleet)."""
    shots = set(st["shots"]["2"])
    fleet = st["fleets"]["1"]
    sunk_ships = _bs_sunk(fleet, list(shots))
    sunk = {c for s in sunk_ships for c in s}
    hits = {c for c in shots if c not in sunk and any(c in s for s in fleet)}
    misses = shots - sunk - hits
    left = list(BS_SHIPS)
    for s_ in sunk_ships:
        left.remove(len(s_))
    heat = [0] * (BS_N * BS_N)
    for size in left:
        for r in range(BS_N):
            for c in range(BS_N):
                for horiz in (True, False):
                    if (c + size > BS_N) if horiz else (r + size > BS_N):
                        continue
                    cells = [r * BS_N + c + (k if horiz else k * BS_N) for k in range(size)]
                    if any(x in misses or x in sunk for x in cells):
                        continue
                    weight = 1 + 50 * sum(x in hits for x in cells)   # lines through a hit: far likelier
                    for x in cells:
                        if x not in shots:
                            heat[x] += weight
    best = max(heat)
    return random.choice([i for i, h in enumerate(heat) if h == best and i not in shots])


def _bs_pi(st):
    """Hunt and target: finish off hit ships first, else search on a checkerboard pattern."""
    if _impossible(st):
        res = _bs_fire(st, P2, _bs_density(st))
        st["msg"] = (st.get("msg", "") + "  ·  The Pi: %s" % res).strip(" ·")
        if not finished(st):
            st["turn"] = P1
        return
    shots = st["shots"]["2"]
    fleet = st["fleets"]["1"]
    sunk = {c for s in _bs_sunk(fleet, shots) for c in s}
    open_hits = [c for c in shots if c not in sunk and any(c in s for s in fleet)]
    targets = []
    for h in open_hits:
        r, c = divmod(h, BS_N)
        for dr, dc in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            rr, cc = r + dr, c + dc
            if 0 <= rr < BS_N and 0 <= cc < BS_N and rr * BS_N + cc not in shots:
                targets.append(rr * BS_N + cc)
    free = [i for i in range(BS_N * BS_N) if i not in shots]
    if targets and not _slip(st):
        cell = random.choice(targets)
    else:
        parity = [i for i in free if (i // BS_N + i % BS_N) % 2 == 0]
        cell = random.choice(parity or free)
    res = _bs_fire(st, P2, cell)
    st["msg"] = (st.get("msg", "") + "  ·  The Pi: %s" % res).strip(" ·")
    if not finished(st):
        st["turn"] = P1


def _bs_ship_cells(start, size, horiz):
    """Cells of a ship starting at start (slid back onto the board if it would hang off)."""
    r, c = divmod(start, BS_N)
    if horiz:
        c = min(c, BS_N - size)
    else:
        r = min(r, BS_N - size)
    return [r * BS_N + c + (k if horiz else k * BS_N) for k in range(size)]


def _bs_horiz(ship):
    return len(ship) == 1 or ship[1] - ship[0] == 1


def _bs_move(st, k, cells):
    """Put ship k of the placing player's fleet on cells, if nothing else is there."""
    fleet = st["fleets"][str(st["who"])]
    others = {c for j, s in enumerate(fleet) if j != k for c in s}
    if any(c in others for c in cells):
        st["msg"] = "It doesn't fit there (another ship is in the way)"
        return True
    fleet[k] = cells
    st["msg"] = ""
    return True


def _bs_place_tap(st, i):
    """Placing: tap a ship to pick it up, then a square to move it there."""
    fleet = st["fleets"][str(st["who"])]
    hit = next((k for k, ship in enumerate(fleet) if i in ship), None)
    sel = st.get("sel")
    if hit is not None and hit != sel:
        st.update(sel=hit, msg="")                         # pick up (or switch to) that ship
        return True
    if sel is None:
        st["msg"] = "Tap a ship first, then where it should go"
        return True
    if hit == sel:
        st["sel"] = None                                   # tapped it again: put it down
        return True
    ship = fleet[sel]
    return _bs_move(st, sel, _bs_ship_cells(i, len(ship), _bs_horiz(ship)))


def _bs_tap(st, i):
    """Board taps: move ships while placing, fire while playing."""
    if not 0 <= i < BS_N * BS_N:
        return False
    if st["phase"] == "place":
        return _bs_place_tap(st, i)
    if st["phase"] != "play":
        return False
    p = st["who"]
    res = _bs_fire(st, p, i)
    if res is None:
        return False
    st["msg"] = "%s: %s" % ("You" if _mode(st) == "pi" else "Player %d" % p, res)
    if finished(st):
        return True
    if _mode(st) == "pi":
        st["turn"] = P2                                    # run_pi() fires back
    else:
        st["phase"] = "fired"                              # show the result, then pass the Kindle
    return True


def _bs_button(st, action):
    """Keypad, Shuffle/Ready, Done and handoff taps. Returns True if something changed."""
    ph, who = st["phase"], st["who"]
    if action.startswith("key:") and ph in ("pin_set", "pin"):
        key = action[4:]
        if key == "del":
            st["typed"] = st["typed"][:-1]
        elif key == "ok":
            if ph == "pin_set":
                if len(st["typed"]) != 4:
                    st["msg"] = "PINs are 4 digits"
                    return True
                st["pins"][str(who)] = st["typed"]
                st.update(phase="place", typed="", msg="")
            elif st["typed"] == st["pins"].get(str(who)):
                st.update(phase="play", typed="", msg="")
            else:
                st.update(typed="", msg="Wrong PIN")
        elif len(st["typed"]) < 4:
            st["typed"] += key
        return True
    if action == "shuffle" and ph == "place":
        st["fleets"][str(who)] = _bs_random_fleet()
        st.update(sel=None, msg="")
        return True
    if action == "rotate" and ph == "place":
        k = st.get("sel")
        if k is None:
            st["msg"] = "Tap a ship first, then Rotate"
            return True
        ship = st["fleets"][str(who)][k]
        return _bs_move(st, k, _bs_ship_cells(ship[0], len(ship), not _bs_horiz(ship)))
    if action == "ready" and ph == "place":
        st.update(sel=None, msg="")
        if _mode(st) == "pi":
            st.update(phase="play", who=P1, turn=P1, msg="Your turn: tap a square to fire")
        elif who == P1:
            st.update(phase="handoff", who=P2, after="pin_set")
        else:
            first = st.get("last_first", P1)
            st.update(phase="handoff", who=first, after="pin", turn=first)
        return True
    if action == "done" and ph == "fired":
        nxt = 3 - who
        st.update(phase="handoff", who=nxt, after="pin", turn=nxt)
        return True
    if action == "continue" and ph == "handoff":
        st.update(phase=st.get("after", "pin"), typed="", msg="")
        return True
    return False


# -------------------------------- chess -----------------------------
# Board: 64 chars, index r*8+c with r=0 the black back rank ("8"); "PNBRQK" white,
# "pnbrqk" black, "." empty. st["white"] = which player (P1/P2) plays white this game.

CH_START = "rnbqkbnrpppppppp" + "." * 32 + "PPPPPPPPRNBQKBNR"
CH_VALUE = {"p": 100, "n": 320, "b": 330, "r": 500, "q": 900, "k": 0}
CH_KNIGHT = [(-2, -1), (-2, 1), (-1, -2), (-1, 2), (1, -2), (1, 2), (2, -1), (2, 1)]
CH_KING = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
CH_ROOK = [(-1, 0), (1, 0), (0, -1), (0, 1)]
CH_BISHOP = [(-1, -1), (-1, 1), (1, -1), (1, 1)]
# Positional bonus for white pieces (mirrored for black): advance pawns, centralise knights.
CH_PAWN_BONUS = [0, 0, 0, 0, 0, 0, 0, 0, 50, 50, 50, 50, 50, 50, 50, 50, 10, 10, 20, 30, 30, 20, 10, 10,
                 5, 5, 10, 25, 25, 10, 5, 5, 0, 0, 0, 20, 20, 0, 0, 0, 5, -5, -10, 0, 0, -10, -5, 5,
                 5, 10, 10, -20, -20, 10, 10, 5, 0, 0, 0, 0, 0, 0, 0, 0]
CH_CENTRE = [-((abs(3.5 - i // 8) + abs(3.5 - i % 8)) * 6) for i in range(64)]
CH_GLYPH = {"k": ("\u265a", "\u2654"), "q": ("\u265b", "\u2655"), "r": ("\u265c", "\u2656"),
            "b": ("\u265d", "\u2657"), "n": ("\u265e", "\u2658"), "p": ("\u265f", "\u2659")}
CH_FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial Unicode.ttf",
            "/System/Library/Fonts/Apple Symbols.ttf"]


@functools.lru_cache(maxsize=8)
def _ch_font(size):
    """A font with the chess symbols (DejaVu Sans on the Pi)."""
    for path in CH_FONTS:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return render.ui_font(size)


def _ch_white(p):
    return p.isupper()


def _ch_attacked(b, sq, by_white):
    """Is square sq attacked by the side given (white if by_white)?"""
    r0, c0 = divmod(sq, 8)
    pawn, knight, king = ("P", "N", "K") if by_white else ("p", "n", "k")
    rq = ("R", "Q", "B") if by_white else ("r", "q", "b")
    dr = 1 if by_white else -1                    # white pawns attack upwards (from r+1)
    for dc in (-1, 1):
        r, c = r0 + dr, c0 + dc
        if 0 <= r < 8 and 0 <= c < 8 and b[r * 8 + c] == pawn:
            return True
    for dr_, dc in CH_KNIGHT:
        r, c = r0 + dr_, c0 + dc
        if 0 <= r < 8 and 0 <= c < 8 and b[r * 8 + c] == knight:
            return True
    for dr_, dc in CH_KING:
        r, c = r0 + dr_, c0 + dc
        if 0 <= r < 8 and 0 <= c < 8 and b[r * 8 + c] == king:
            return True
    for dirs, sliders in ((CH_ROOK, (rq[0], rq[1])), (CH_BISHOP, (rq[2], rq[1]))):
        for dr_, dc in dirs:
            r, c = r0 + dr_, c0 + dc
            while 0 <= r < 8 and 0 <= c < 8:
                p = b[r * 8 + c]
                if p != ".":
                    if p in sliders:
                        return True
                    break
                r, c = r + dr_, c + dc
    return False


def _ch_pseudo(b, white, castling, ep):
    """Moves ignoring whether they leave the own king in check: (from, to)."""
    out = []
    own = str.isupper if white else str.islower
    for i, p in enumerate(b):
        if p == "." or not own(p):
            continue
        r0, c0 = divmod(i, 8)
        k = p.lower()
        if k == "p":
            d = -1 if white else 1
            r = r0 + d
            if 0 <= r < 8 and b[r * 8 + c0] == ".":
                out.append((i, r * 8 + c0))
                if r0 == (6 if white else 1) and b[(r0 + 2 * d) * 8 + c0] == ".":
                    out.append((i, (r0 + 2 * d) * 8 + c0))
            for dc in (-1, 1):
                c = c0 + dc
                if 0 <= r < 8 and 0 <= c < 8:
                    t = r * 8 + c
                    if (b[t] != "." and not own(b[t])) or t == ep:
                        out.append((i, t))
        elif k in "nk":
            for dr, dc in (CH_KNIGHT if k == "n" else CH_KING):
                r, c = r0 + dr, c0 + dc
                if 0 <= r < 8 and 0 <= c < 8 and (b[r * 8 + c] == "." or not own(b[r * 8 + c])):
                    out.append((i, r * 8 + c))
        else:
            dirs = CH_ROOK if k == "r" else CH_BISHOP if k == "b" else CH_ROOK + CH_BISHOP
            for dr, dc in dirs:
                r, c = r0 + dr, c0 + dc
                while 0 <= r < 8 and 0 <= c < 8:
                    t = r * 8 + c
                    if b[t] == ".":
                        out.append((i, t))
                    else:
                        if not own(b[t]):
                            out.append((i, t))
                        break
                    r, c = r + dr, c + dc
    # castling: king and rook unmoved (rights), squares between empty, not through check
    if white and b[60] == "K":
        if "K" in castling and b[61] == b[62] == "." and b[63] == "R" and \
                not any(_ch_attacked(b, s, False) for s in (60, 61, 62)):
            out.append((60, 62))
        if "Q" in castling and b[59] == b[58] == b[57] == "." and b[56] == "R" and \
                not any(_ch_attacked(b, s, False) for s in (60, 59, 58)):
            out.append((60, 58))
    if not white and b[4] == "k":
        if "k" in castling and b[5] == b[6] == "." and b[7] == "r" and \
                not any(_ch_attacked(b, s, True) for s in (4, 5, 6)):
            out.append((4, 6))
        if "q" in castling and b[3] == b[2] == b[1] == "." and b[0] == "r" and \
                not any(_ch_attacked(b, s, True) for s in (4, 3, 2)):
            out.append((4, 2))
    return out


def _ch_apply(b, castling, ep, half, move):
    """Play move; returns (board, castling, ep, halfmove). Pawns promote to a queen."""
    f, t = move
    b = list(b)
    p = b[f]
    capture = b[t] != "." or (p in "Pp" and t == ep)
    if p in "Pp" and t == ep:                       # en passant: remove the passed pawn
        b[t + (8 if p == "P" else -8)] = "."
    if p in "Kk" and abs(t - f) == 2:               # castling: move the rook too
        rf, rt = (f + 3, f + 1) if t > f else (f - 4, f - 1)
        b[rt], b[rf] = b[rf], "."
    b[t], b[f] = p, "."
    if p == "P" and t < 8:
        b[t] = "Q"
    elif p == "p" and t >= 56:
        b[t] = "q"
    for sq, rights in ((60, "KQ"), (4, "kq"), (63, "K"), (56, "Q"), (7, "k"), (0, "q")):
        if f == sq or t == sq:                      # king/rook moved or a rook was taken
            castling = "".join(x for x in castling if x not in rights)
    new_ep = (f + t) // 2 if p in "Pp" and abs(t - f) == 16 else -1
    return "".join(b), castling, new_ep, 0 if (capture or p in "Pp") else half + 1


def _ch_legal(b, white, castling, ep):
    out = []
    for m in _ch_pseudo(b, white, castling, ep):
        nb = _ch_apply(b, castling, ep, 0, m)[0]
        king = nb.find("K" if white else "k")
        if king >= 0 and not _ch_attacked(nb, king, not white):
            out.append(m)
    return out


def _ch_eval(b):
    """Score from white's point of view: material plus a little position."""
    s = 0
    for i, p in enumerate(b):
        if p == ".":
            continue
        k = p.lower()
        mirror = i if p.isupper() else 63 - i
        v = CH_VALUE[k] + (CH_PAWN_BONUS[mirror] if k == "p" else CH_CENTRE[i] if k in "nb" else 0)
        s += v if p.isupper() else -v
    return s


def _ch_search(b, white, castling, ep, depth, alpha, beta):
    """Negamax over pseudo-legal moves (taking the king ends it). Returns (score, move)."""
    if "K" not in b:                                  # scores are for the side to move
        return (-10 ** 6 if white else 10 ** 6), None
    if "k" not in b:
        return (10 ** 6 if white else -10 ** 6), None
    if depth == 0:
        return _ch_eval(b) * (1 if white else -1), None
    moves = _ch_pseudo(b, white, castling, ep)
    moves.sort(key=lambda m: -CH_VALUE.get(b[m[1]].lower(), 0))     # captures first
    best, best_move = -10 ** 9, None
    for m in moves:
        nb, nc, ne, _ = _ch_apply(b, castling, ep, 0, m)
        score = -_ch_search(nb, not white, nc, ne, depth - 1, -beta, -alpha)[0]
        if score > best:
            best, best_move = score, m
        alpha = max(alpha, score)
        if alpha >= beta:
            break
    return best, best_move


def _ch_init(st):
    white = st.get("turn", P1)                      # whoever starts plays white
    st.update(board=CH_START, white=white, side="w", castling="KQkq", ep=-1, half=0,
              sel=None, last=None, reps={}, msg="")


def _ch_side_player(st, side):
    return st["white"] if side == "w" else 3 - st["white"]


def _ch_play(st, move):
    b, castling, ep, half = _ch_apply(st["board"], st["castling"], st["ep"], st["half"], move)
    st.update(board=b, castling=castling, ep=ep, half=half, last=list(move), sel=None)
    st["side"] = "b" if st["side"] == "w" else "w"
    white = st["side"] == "w"
    key = "%s %s %s %d" % (b, st["side"], castling, ep)
    st["reps"][key] = st["reps"].get(key, 0) + 1
    st["turn"] = _ch_side_player(st, st["side"])
    if not _ch_legal(b, white, castling, ep):
        king = b.find("K" if white else "k")
        if _ch_attacked(b, king, not white):
            end(st, _ch_side_player(st, "b" if white else "w"))   # checkmate
            st["msg"] = "Checkmate"
        else:
            end(st, -1)
            st["msg"] = "Stalemate"
    elif half >= 100:
        end(st, -1)
        st["msg"] = "Draw: 50 moves without a capture or pawn move"
    elif st["reps"][key] >= 3:
        end(st, -1)
        st["msg"] = "Draw: the same position three times"
    elif all(p in ".KkNnBb" for p in b) and sum(p in "NnBb" for p in b) <= 1:
        end(st, -1)
        st["msg"] = "Draw: not enough pieces left to checkmate"
    else:
        king = b.find("K" if white else "k")
        st["msg"] = "Check!" if _ch_attacked(b, king, not white) else ""


def _ch_tap(st, i):
    """Tap one of your pieces, then a highlighted square."""
    if not 0 <= i < 64:
        return False
    white = st["side"] == "w"
    moves = _ch_legal(st["board"], white, st["castling"], st["ep"])
    sel = st.get("sel")
    if sel is not None and (sel, i) in [tuple(m) for m in moves]:
        _ch_play(st, (sel, i))
        return True
    if any(m[0] == i for m in moves):
        st["sel"] = i
        return True
    if sel is not None:
        st["sel"] = None
        return True
    return False


def _ch_fen(st):
    rows = []
    for r in range(8):
        row, empty = "", 0
        for p in st["board"][r * 8:(r + 1) * 8]:
            if p == ".":
                empty += 1
            else:
                row += (str(empty) if empty else "") + p
                empty = 0
        rows.append(row + (str(empty) if empty else ""))
    ep = "-" if st["ep"] < 0 else "abcdefgh"[st["ep"] % 8] + str(8 - st["ep"] // 8)
    return "%s %s %s %s %d 1" % ("/".join(rows), st["side"], st["castling"] or "-", ep, st["half"])


def _stockfish(st):
    """Stockfish's move as (from, to), or None if it isn't there."""
    if not os.path.exists(STOCKFISH):
        return None
    cmds = "uci\nsetoption name Threads value 4\nsetoption name Hash value 64\nisready\n" \
           "position fen %s\ngo movetime %d\n" % (_ch_fen(st), STOCKFISH_MS)
    p = subprocess.Popen([STOCKFISH], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    p.stdin.write(cmds)
    p.stdin.flush()
    move = None
    deadline = time.time() + STOCKFISH_MS / 1000 + 10
    for line in p.stdout:
        if line.startswith("bestmove"):
            move = line.split()[1]
            break
        if time.time() > deadline:
            break
    p.kill()
    p.wait()
    if not move or move == "(none)":
        return None
    sq = lambda t: (8 - int(t[1])) * 8 + "abcdefgh".index(t[0])
    return sq(move[:2]), sq(move[2:4])


def _ch_pi(st):
    white = st["side"] == "w"
    b = st["board"]
    legal = _ch_legal(b, white, st["castling"], st["ep"])
    if _impossible(st):
        move = _stockfish(st)
        if move in legal:
            _ch_play(st, move)
            return
        render.log("games: Stockfish unavailable, using the built-in search")
    if _slip(st):
        _ch_play(st, random.choice(legal))
        return
    depth = _lvl(st)["chess_depth"]
    random.shuffle(legal)                              # ties broken differently each game
    best, move = -10 ** 9, legal[0]
    for m in sorted(legal, key=lambda m: -CH_VALUE.get(b[m[1]].lower(), 0)):
        nb, nc, ne, _ = _ch_apply(b, st["castling"], st["ep"], 0, m)
        if not _ch_legal(nb, not white, nc, ne):
            king = nb.find("k" if white else "K")
            score = 10 ** 6 if _ch_attacked(nb, king, white) else 0       # mate, or stalemate = draw
        else:
            score = -_ch_search(nb, not white, nc, ne, depth - 1, -10 ** 9, -best)[0]
        if score > best:
            best, move = score, m
    _ch_play(st, move)

INIT = {"ttt": _ttt_init, "c4": _c4_init, "reversi": _rev_init, "mines": _ms_init,
        "checkers": _ck_init, "sudoku": _su_init, "battleship": _bs_init, "chess": _ch_init}
HUMAN_MOVE = {"ttt": _ttt_place, "c4": _c4_place, "reversi": _rev_place, "mines": _ms_tap,
              "checkers": _ck_tap, "sudoku": _su_tap, "battleship": _bs_tap, "chess": _ch_tap}
PI_MOVE = {"ttt": _ttt_pi, "c4": _c4_pi, "reversi": _rev_pi, "checkers": _ck_pi, "battleship": _bs_pi,
           "chess": _ch_pi}


def _who(st, p):
    if _mode(st) == "pi":
        return "You" if p == P1 else "The Pi"
    return "Player %d" % p


# ------------------------------ drawing -----------------------------

def _button(d, box, text, font, filled=False):
    d.rounded_rectangle(box, radius=18, outline=0, width=4, fill=0 if filled else 255)
    d.text(((box[0] + box[2] - d.textlength(text, font=font)) / 2,
            (box[1] + box[3]) / 2 - font.size * 0.62), text, font=font, fill=255 if filled else 0)


def _piece(d, cx, cy, rad, p, ring_w=8):
    if p == P1:
        d.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), fill=0)
    elif p == P2:
        d.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), fill=255, outline=0, width=ring_w)


def draw(w, h, st):
    """Picker or board. Returns (image, targets) with targets = [{"action", "box"}]."""
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    title = render.ui_font(54, "bold")
    btn = render.ui_font(34, "semibold")
    small = render.ui_font(28)
    targets = []

    if not st.get("game"):                             # ---------- picker
        d.text((M, 34), "Games", font=title, fill=0)
        x = w - M
        for key, label in (("impossible", "Impossible"), ("hard", "Hard"), ("normal", "Normal"), ("easy", "Easy")):
            bw = 200 if key == "impossible" else 150
            box = (x - bw, 36, x, 100)
            _button(d, box, label, btn, filled=st["level"] == key)
            targets.append({"action": "level:" + key, "box": box})
            x -= bw + 12
        x -= 30
        for key, label in (("2p", "2 players"), ("pi", "vs Pi")):
            bw = 210 if key == "2p" else 170
            box = (x - bw, 36, x, 100)
            _button(d, box, label, btn, filled=st["mode"] == key)
            targets.append({"action": "mode:" + key, "box": box})
            x -= bw + 15
        cols, rows = 4, 2
        tw, th = (w - (cols + 1) * M + 20) / cols, (h - 170 - 40 - (rows - 1) * 30) / rows
        for n, (key, name, modes) in enumerate(GAMES):
            r, c = divmod(n, cols)
            x0, y0 = M + c * (tw + 30), 150 + r * (th + 30)
            box = (x0, y0, x0 + tw, y0 + th)
            _button(d, box, name, render.ui_font(44, "bold"))
            mode = st["mode"] if st["mode"] in modes else modes[0]
            sc = st["score"].get("%s:%s" % (key, mode))
            sub = {"pi": "vs Pi", "2p": "2 players", "solo": "solo"}[mode]
            if sc:
                vals = list(sc.values())
                sub += "  ·  %d–%d" % (vals[0], vals[1]) + ("  (%d draws)" % sc["draws"] if sc.get("draws") else "")
            d.text((x0 + (tw - d.textlength(sub, font=small)) / 2, y0 + th - 60), sub, font=small, fill=0)
            targets.append({"action": "game:" + key, "box": box})
        return img, targets

    game, mode = st["game"], _mode(st)                 # ---------- a game
    back = (M, 30, M + 200, 100)
    _button(d, back, "‹ Games", btn)
    targets.append({"action": "back", "box": back})
    new = (w - M - 240, 30, w - M, 100)
    _button(d, new, "New game", btn)
    targets.append({"action": "new", "box": new})
    name = st.get("title") if game == "sudoku" else NAMES[game]
    d.text(((w - d.textlength(name, font=title)) / 2, 32), name, font=title, fill=0)

    w_ = st.get("winner")
    if game == "sudoku":
        status = "Solved! Tap New for another" if w_ else (
            "Tap a square, then a number" if st.get("sel") is None else "Now tap a number (or Erase)")
    elif game == "battleship" and st.get("phase") in ("pin_set", "pin", "handoff", "place"):
        status = st.get("msg") or ""
    elif game == "battleship" and w_ is not None:
        status = "%s %s! Every ship sunk" % (_who(st, w_), "win" if _who(st, w_) == "You" else "wins")
    elif game == "battleship":
        status = st.get("msg") or ("%s: tap a square to fire" % ("Your turn" if _mode(st) == "pi" else "Player %d" % st["who"]))
    elif game == "chess":
        colour = {st["white"]: "white", 3 - st["white"]: "black"}
        if w_ is None:
            status = ("Your turn (%s)" % colour[P1] if st["turn"] == P1 else "The Pi is thinking") \
                if mode == "pi" else "Player %d (%s) to move" % (st["turn"], colour[st["turn"]])
        elif w_ == -1:
            status = st.get("msg") or "Draw"
        else:
            status = "Checkmate. %s %s" % (_who(st, w_), "win!" if _who(st, w_) == "You" else "wins")
    elif game == "mines":
        status = {None: "Flag mode: tap to place flags" if st["flag_mode"] else "Tap a square",
                  P1: "Cleared! You win", P2: "Boom. Tap the board to try again"}[w_]
    elif w_ is None:
        status = "%s's turn" % _who(st, st["turn"]) if mode == "2p" else \
            ("Your turn" if st["turn"] == P1 else "The Pi is thinking")
        status = status.replace("Player 1's turn", "Player 1's turn (black)").replace(
            "Player 2's turn", "Player 2's turn (white)")
    elif w_ == -1:
        status = "Draw"
    else:
        status = "%s %s!" % (_who(st, w_), "win" if _who(st, w_) == "You" else "wins")
    if w_ is not None and game not in ("mines", "sudoku", "battleship"):
        status += "  ·  tap the board to play again"
    if st.get("msg") and w_ is None and game != "battleship":
        status = st["msg"] + "  ·  " + status if "Tap to continue" not in st["msg"] else st["msg"]
    sf = render.ui_font(36, "semibold")
    d.text(((w - d.textlength(status, font=sf)) / 2, h - 70), status, font=sf, fill=0)
    top, bottom = 125, h - 90
    line = {tuple(x) if isinstance(x, list) else x for x in st.get("line", [])}

    if game == "ttt":
        cell = (bottom - top) // 3
        x0 = (w - 3 * cell) // 2
        for i in (1, 2):
            d.line((x0 + i * cell, top, x0 + i * cell, top + 3 * cell), fill=0, width=8)
            d.line((x0, top + i * cell, x0 + 3 * cell, top + i * cell), fill=0, width=8)
        pad = cell * 0.22
        for i, v in enumerate(st["board"]):
            cx0, cy0 = x0 + (i % 3) * cell, top + (i // 3) * cell
            if i in line:
                d.rectangle((cx0 + 10, cy0 + 10, cx0 + cell - 10, cy0 + cell - 10), fill=205)
            if v == P1:
                d.line((cx0 + pad, cy0 + pad, cx0 + cell - pad, cy0 + cell - pad), fill=0, width=18)
                d.line((cx0 + cell - pad, cy0 + pad, cx0 + pad, cy0 + cell - pad), fill=0, width=18)
            elif v == P2:
                d.ellipse((cx0 + pad, cy0 + pad, cx0 + cell - pad, cy0 + cell - pad), outline=0, width=14)
            targets.append({"action": "m:%d" % i, "box": (cx0, cy0, cx0 + cell, cy0 + cell)})

    elif game == "c4":
        cell = min((w - 2 * M) // 7, (bottom - top - 20) // 6)
        x0, y0 = (w - 7 * cell) // 2, top + 10
        d.rounded_rectangle((x0 - 10, y0 - 10, x0 + 7 * cell + 10, y0 + 6 * cell + 10), radius=24, outline=0, width=6)
        for c in range(7):
            for r in range(6):
                cx, cy, rad = x0 + c * cell + cell / 2, y0 + (5 - r) * cell + cell / 2, cell * 0.40
                v = st["board"][c][r]
                if v:
                    _piece(d, cx, cy, rad, v, 10)
                else:
                    d.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), outline=190, width=3)
                if (c, r) in line:
                    d.ellipse((cx - rad * .35, cy - rad * .35, cx + rad * .35, cy + rad * .35),
                              fill=255 if v == P1 else 0)
            targets.append({"action": "m:%d" % c, "box": (x0 + c * cell, y0, x0 + (c + 1) * cell, y0 + 6 * cell)})

    elif game == "reversi":
        n = 8
        cell = (bottom - top) // n
        x0, y0 = (w - n * cell) // 2, top
        b = st["board"]
        hints = set(_rev_moves(b, st["turn"])) if w_ is None and not pi_turn(st) else set()
        for i in range(n + 1):
            d.line((x0 + i * cell, y0, x0 + i * cell, y0 + n * cell), fill=0, width=3)
            d.line((x0, y0 + i * cell, x0 + n * cell, y0 + i * cell), fill=0, width=3)
        for i, v in enumerate(b):
            r, c = divmod(i, n)
            cx, cy = x0 + c * cell + cell / 2, y0 + r * cell + cell / 2
            if v:
                _piece(d, cx, cy, cell * 0.40, v, 7)
                if i in line:
                    d.ellipse((cx - 7, cy - 7, cx + 7, cy + 7), fill=255 if v == P1 else 0)
            elif i in hints:
                d.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill=0)
            targets.append({"action": "m:%d" % i, "box": (x0 + c * cell, y0 + r * cell, x0 + (c + 1) * cell, y0 + (r + 1) * cell)})
        score = "Black %d  ·  White %d" % (b.count(P1), b.count(P2))
        d.text((M, h - 120), score, font=small, fill=0)

    elif game == "checkers":
        n = 8
        cell = (bottom - top) // n
        x0, y0 = (w - n * cell) // 2, top
        b = st["board"]
        sel = st.get("sel") or []
        moves = _ck_moves(b, st["turn"]) if w_ is None and not pi_turn(st) else []
        dests = {m[0][len(sel)] for m in moves if sel and m[0][:len(sel)] == sel and len(m[0]) > len(sel)}
        movable = {m[0][0] for m in moves} if not sel else set()
        d.rectangle((x0 - 4, y0 - 4, x0 + n * cell + 4, y0 + n * cell + 4), outline=0, width=4)
        for i in range(64):
            r, c = divmod(i, n)
            box = (x0 + c * cell, y0 + r * cell, x0 + (c + 1) * cell, y0 + (r + 1) * cell)
            if (r + c) % 2 == 1:
                d.rectangle(box, fill=170)
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            v = b[i]
            if v:
                _piece(d, cx, cy, cell * 0.38, _ck_owner(v), 7)
                if v in (3, 4):                           # king: a ring inside
                    rr = cell * 0.18
                    d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=255 if _ck_owner(v) == P1 else 0, width=5)
            if sel and i == sel[0]:
                d.rectangle((box[0] + 4, box[1] + 4, box[2] - 4, box[3] - 4), outline=0, width=6)
            elif i in sel:
                d.ellipse((cx - 12, cy - 12, cx + 12, cy + 12), outline=0, width=4)
            if i in dests:
                d.ellipse((cx - 14, cy - 14, cx + 14, cy + 14), fill=0)
            elif i in movable and not pi_turn(st):
                d.rectangle((box[0] + 3, box[1] + 3, box[2] - 3, box[3] - 3), outline=255, width=2)
            targets.append({"action": "m:%d" % i, "box": box})

    elif game == "chess":
        cell = (bottom - top - 34) // 8                       # room for the a-h labels
        x0, y0 = (w - 8 * cell) // 2, top
        b = st["board"]
        flip = st["white"] != P1 if mode == "pi" else False      # your colour at the bottom
        white = st["side"] == "w"
        sel = st.get("sel")
        moves = _ch_legal(b, white, st["castling"], st["ep"]) if w_ is None and not pi_turn(st) else []
        dests = {m[1] for m in moves if m[0] == sel}
        last = set(st.get("last") or [])
        king = b.find("K" if white else "k")
        checked = king if w_ is None and king >= 0 and _ch_attacked(b, king, not white) else None
        fill_font, line_font = _ch_font(int(cell * 0.82)), _ch_font(int(cell * 0.82))
        coord = render.ui_font(24, "semibold")
        d.rectangle((x0 - 4, y0 - 4, x0 + 8 * cell + 4, y0 + 8 * cell + 4), outline=0, width=4)
        for k in range(64):
            dr, dc = divmod(k, 8)
            i = 63 - k if flip else k
            box = (x0 + dc * cell, y0 + dr * cell, x0 + (dc + 1) * cell, y0 + (dr + 1) * cell)
            dark = (dr + dc) % 2 == 1
            d.rectangle(box, fill=(140 if dark else 215) if i in last else (170 if dark else 255))
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            p = b[i]
            if p != ".":
                k_ = p.lower()
                solid, outline_ = CH_GLYPH[k_]
                if p.isupper():                              # white: white body, black outline
                    d.text((cx, cy), solid, font=fill_font, fill=255, anchor="mm")
                    d.text((cx, cy), outline_, font=line_font, fill=0, anchor="mm")
                else:
                    d.text((cx, cy), solid, font=fill_font, fill=0, anchor="mm")
            if i == sel:
                d.rectangle((box[0] + 3, box[1] + 3, box[2] - 3, box[3] - 3), outline=0, width=7)
            elif i == checked:
                d.rectangle((box[0] + 3, box[1] + 3, box[2] - 3, box[3] - 3), outline=0, width=4)
                d.rectangle((box[0] + 11, box[1] + 11, box[2] - 11, box[3] - 11), outline=0, width=4)
            if i in dests:
                if p != "." or (b[sel] in "Pp" and i == st["ep"]):     # capture: a ring
                    rr = cell * 0.44
                    d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=0, width=6)
                else:
                    d.ellipse((cx - 15, cy - 15, cx + 15, cy + 15), fill=0)
            if dc == 0:
                d.text((x0 - 14, cy), "87654321"[i // 8], font=coord,
                       fill=0, anchor="rm")
            if dr == 7:
                d.text((cx, y0 + 8 * cell + 8), "abcdefgh"[i % 8], font=coord, fill=0, anchor="mt")
            targets.append({"action": "m:%d" % i, "box": box})
        # captured pieces either side of the board
        start = {p: CH_START.count(p) for p in "PNBRQpnbrq"}
        gone = {p: start[p] - b.count(p) for p in start}
        cap = _ch_font(52)
        for side_white, xpos in ((False, x0 + 8 * cell + 30), (True, x0 - 30 - 52 * 2)):
            # left column: white pieces taken; right column: black pieces taken
            if flip:
                xpos = x0 + 8 * cell + 30 if side_white else x0 - 30 - 52 * 2
            n = 0
            for k_ in "qrbnp":
                p = k_.upper() if side_white else k_
                for _ in range(max(0, gone[p])):
                    r_, c_ = divmod(n, 2)
                    px, py = xpos + c_ * 52 + 26, y0 + 30 + r_ * 56
                    solid, outline_ = CH_GLYPH[k_]
                    if side_white:
                        d.text((px, py), solid, font=cap, fill=255, anchor="mm")
                        d.text((px, py), outline_, font=cap, fill=0, anchor="mm")
                    else:
                        d.text((px, py), solid, font=cap, fill=0, anchor="mm")
                    n += 1

    elif game == "sudoku":
        size = bottom - top
        cell = size // 9
        x0, y0 = M + 40, top
        num = render.ui_font(int(cell * 0.6), "bold")
        num_user = render.ui_font(int(cell * 0.6))
        for i in range(81):
            r, c = divmod(i, 9)
            box = (x0 + c * cell, y0 + r * cell, x0 + (c + 1) * cell, y0 + (r + 1) * cell)
            given, entry = st["puzzle"][i], st["entries"][i]
            if i == st.get("sel"):
                d.rectangle(box, fill=200)
            v = given or entry
            if v:
                font = num if given else num_user
                d.text(((box[0] + box[2] - d.textlength(str(v), font=font)) / 2, box[1] + cell * 0.15),
                       str(v), font=font, fill=0)
                if not given and st.get("check") and entry != st["solution"][i]:
                    d.line((box[0] + 10, box[3] - 10, box[2] - 10, box[1] + 10), fill=0, width=4)
            targets.append({"action": "m:%d" % i, "box": box})
        for k in range(10):
            wd = 6 if k % 3 == 0 else 2
            d.line((x0 + k * cell, y0, x0 + k * cell, y0 + 9 * cell), fill=0, width=wd)
            d.line((x0, y0 + k * cell, x0 + 9 * cell, y0 + k * cell), fill=0, width=wd)
        px0 = x0 + 9 * cell + 60
        pw = w - M - px0
        kw = (pw - 2 * 16) / 3
        kh = min(kw, (size - 3 * 16 - 100) / 4)
        kf = render.ui_font(56, "bold")
        for k in range(9):
            r, c = divmod(k, 3)
            box = (px0 + c * (kw + 16), top + r * (kh + 16), px0 + c * (kw + 16) + kw, top + r * (kh + 16) + kh)
            _button(d, box, str(k + 1), kf)
            targets.append({"action": "su:%d" % (k + 1), "box": box})
        y = top + 3 * (kh + 16)
        for j, (act, label) in enumerate((("su:0", "Erase"), ("su:check", "Check: on" if st.get("check") else "Check"))):
            box = (px0 + j * (pw / 2 + 8), y, px0 + j * (pw / 2 + 8) + pw / 2 - 8, y + kh * 0.8)
            _button(d, box, label, btn, filled=(act == "su:check" and st.get("check")))
            targets.append({"action": act, "box": box})
        d.text((px0, y + kh * 0.8 + 24), "%s · %s" % (st.get("title", "Sudoku"), st.get("level", "normal").title()),
               font=small, fill=0)

    elif game == "battleship":
        ph, who = st["phase"], st["who"]
        two = mode == "2p"
        pname = "Player %d" % who if two else "You"
        big = render.ui_font(60, "bold")

        def keypad(label):
            d.text(((w - d.textlength(label, font=big)) / 2, top + 10), label, font=big, fill=0)
            for k in range(4):                       # PIN dots, drawn (filled = typed)
                cx = w / 2 + (k - 1.5) * 70
                d.ellipse((cx - 20, top + 110, cx + 20, top + 150), outline=0, width=4,
                          fill=0 if k < len(st["typed"]) else 255)
            keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "del", "0", "ok"]
            kw, kh, gap = 190, 112, 16
            kx0 = (w - (3 * kw + 2 * gap)) / 2
            ky0 = top + 190
            for k, key in enumerate(keys):
                r, c = divmod(k, 3)
                box = (kx0 + c * (kw + gap), ky0 + r * (kh + gap), kx0 + c * (kw + gap) + kw, ky0 + r * (kh + gap) + kh)
                _button(d, box, {"del": "Del", "ok": "OK"}.get(key, key), render.ui_font(54, "bold"), filled=key == "ok")
                targets.append({"action": "bs:key:" + key, "box": box})

        if ph == "pin_set":
            keypad("Player %d: choose a 4-digit PIN" % who)
        elif ph == "pin":
            keypad("Player %d: enter your PIN" % who)
        elif ph == "handoff":
            msg = "Pass the Kindle to Player %d" % who
            d.text(((w - d.textlength(msg, font=big)) / 2, top + 200), msg, font=big, fill=0)
            sub2 = "Player %d: tap the button when only you can see the screen" % who
            d.text(((w - d.textlength(sub2, font=small)) / 2, top + 290), sub2, font=small, fill=0)
            box = (w / 2 - 220, top + 380, w / 2 + 220, top + 500)
            _button(d, box, "I'm Player %d" % who, render.ui_font(48, "bold"), filled=True)
            targets.append({"action": "bs:continue", "box": box})
        else:
            # boards: in play, the opponent's waters big on the left and your fleet small on the right
            mine, theirs = str(who), _bs_other(who)
            show_fleet = ph == "place" or w_ is not None

            def board(x, y, cell, fleet, shots, reveal, tappable, picked=()):
                sunk = {c for s in _bs_sunk(fleet, shots) for c in s}
                ships = {c for s in fleet for c in s}
                for i in range(BS_N * BS_N):
                    r, c = divmod(i, BS_N)
                    box = (x + c * cell, y + r * cell, x + (c + 1) * cell, y + (r + 1) * cell)
                    d.rectangle(box, outline=150, width=1)
                    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
                    if i in ships and (reveal or i in sunk):
                        d.rectangle((box[0] + 3, box[1] + 3, box[2] - 3, box[3] - 3),
                                    fill=0 if (i in sunk or i in picked) else 110)
                    if i in shots:
                        if i in ships:
                            rr = cell * 0.32
                            d.line((cx - rr, cy - rr, cx + rr, cy + rr), fill=255 if (reveal or i in sunk) else 0, width=5)
                            d.line((cx - rr, cy + rr, cx + rr, cy - rr), fill=255 if (reveal or i in sunk) else 0, width=5)
                        else:
                            d.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill=0)
                    if tappable:
                        targets.append({"action": "m:%d" % i, "box": box})
                d.rectangle((x, y, x + BS_N * cell, y + BS_N * cell), outline=0, width=4)

            if ph == "place":
                cell = (bottom - top - 20) // BS_N
                fleet = st["fleets"][mine]
                sel = st.get("sel")
                picked = set(fleet[sel]) if sel is not None and sel < len(fleet) else set()
                board(M + 20, top + 10, cell, fleet, [], True, True, picked)
                px = M + 20 + BS_N * cell + 60
                title2 = "%s: place your ships" % pname
                d.text((px, top + 20), title2, font=render.ui_font(44, "bold"), fill=0)
                hint = ["Tap a ship to pick it up (it turns black),", "then tap where it should go.",
                        "Rotate turns it. Shuffle = random."] if sel is None else \
                       ["Picked: the %d-long ship." % len(fleet[sel]), "Tap a square to move it there,",
                        "Rotate to turn it, or tap it to put it down."]
                for n, line in enumerate(hint):
                    d.text((px, top + 84 + n * 38), line, font=small, fill=0)
                for j, (act, label) in enumerate((("bs:rotate", "Rotate"), ("bs:shuffle", "Shuffle"),
                                                  ("bs:ready", "Ready"))):
                    box = (px, top + 220 + j * 135, w - M, top + 330 + j * 135)
                    _button(d, box, label, render.ui_font(50, "bold"), filled=act == "bs:ready")
                    targets.append({"action": act, "box": box})
            else:
                cell = (bottom - top - 20) // BS_N
                board(M, top + 10, cell, st["fleets"][theirs], st["shots"][mine], w_ is not None,
                      ph == "play" and w_ is None)
                sx = M + BS_N * cell + 50
                small_cell = (w - M - sx) // BS_N
                d.text((sx, top + 4), "Your fleet", font=render.ui_font(30, "semibold"), fill=0)
                board(sx, top + 48, small_cell, st["fleets"][mine], st["shots"][theirs], True, False)
                y = top + 60 + BS_N * small_cell
                left = len(st["fleets"][theirs]) - len(_bs_sunk(st["fleets"][theirs], st["shots"][mine]))
                d.text((sx, y), "Enemy ships left: %d" % left, font=small, fill=0)
                if ph == "fired" and w_ is None:
                    box = (sx, y + 60, w - M, y + 170)
                    _button(d, box, "Done, pass it on", render.ui_font(40, "bold"), filled=True)
                    targets.append({"action": "bs:done", "box": box})
    elif game == "mines":
        cell = min((w - 2 * M) // MS_COLS, (bottom - top - 75) // MS_ROWS)   # room for the Flag row
        x0, y0 = (w - MS_COLS * cell) // 2, top + 5
        num = render.ui_font(int(cell * 0.55), "bold")
        opened, flags, mines = set(st["open"]), set(st["flags"]), set(st["mines"])
        for i in range(MS_COLS * MS_ROWS):
            r, c = divmod(i, MS_COLS)
            box = (x0 + c * cell, y0 + r * cell, x0 + (c + 1) * cell, y0 + (r + 1) * cell)
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            show_mine = w_ is not None and i in mines
            if i in opened or show_mine:
                d.rectangle(box, fill=255, outline=170)
                if i in mines:
                    d.ellipse((cx - cell * .25, cy - cell * .25, cx + cell * .25, cy + cell * .25), fill=0)
                    if i in line:
                        d.rectangle(box, outline=0, width=5)
                else:
                    n = _ms_count(st, i)
                    if n:
                        d.text((cx - d.textlength(str(n), font=num) / 2, cy - num.size * 0.62), str(n), font=num, fill=0)
            else:
                d.rectangle(box, fill=150, outline=255, width=2)
                if i in flags:
                    d.polygon([(cx - cell * .15, cy - cell * .3), (cx + cell * .3, cy - cell * .12),
                               (cx - cell * .15, cy + cell * .05)], fill=0)
                    d.line((cx - cell * .15, cy - cell * .3, cx - cell * .15, cy + cell * .3), fill=0, width=4)
            targets.append({"action": "m:%d" % i, "box": box})
        fbox = (M, h - 140, M + 230, h - 85)
        _button(d, fbox, "Flag: on" if st["flag_mode"] else "Flag: off", btn, filled=st["flag_mode"])
        targets.append({"action": "flag", "box": fbox})
        left = MS_MINES - len(flags)
        d.text((w - M - d.textlength("%d mines left" % left, font=small), h - 130), "%d mines left" % left,
               font=small, fill=0)
    return img, targets


def save_layout(targets):
    with open(LAYOUT_FILE, "w") as f:
        json.dump(targets, f)


def hit(cx, cy):
    try:
        with open(LAYOUT_FILE) as f:
            targets = json.load(f)
    except (OSError, ValueError):
        return None
    for t in targets:
        x0, y0, x1, y1 = t["box"]
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return t["action"]
    return None


def tap(action):
    """Apply a tapped action. Returns it (for the log), or None if nothing happened."""
    st = load()
    if action.startswith("game:"):
        new_game(st, action[5:])
    elif action.startswith("mode:"):
        st["mode"] = action[5:]
    elif action.startswith("level:"):
        st["level"] = action[6:]
    elif action == "back":
        st["game"] = None
    elif action == "new":
        new_game(st, st["game"])
        if st["game"] == "sudoku":                    # New = a random puzzle (the picker gives the daily)
            _su_init(st, seed=random.random())
    elif action.startswith("su:") and st.get("game") == "sudoku":
        arg = action[3:]
        if arg == "check":
            st["check"] = not st.get("check")
        elif not _su_number(st, int(arg)):
            return None
    elif action.startswith("bs:") and st.get("game") == "battleship":
        if not _bs_button(st, action[3:]):
            return None
    elif action == "flag" and st.get("game") == "mines":
        st["flag_mode"] = not st["flag_mode"]
    elif action.startswith("m:"):
        if finished(st) and st["game"] in ("sudoku", "battleship"):
            return None                               # these restart with New game
        if finished(st):
            new_game(st, st["game"])                  # tap the board after a game: rematch
        elif not play(st, int(action[2:])):
            return None
    else:
        return None
    save(st)
    return action
