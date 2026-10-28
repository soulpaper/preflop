"""포지션별 프리플랍 대응 (오픈 / vs 오픈 / vs 3벳 / vs 4벳 / 숏스택 잼·잼콜).

레인지 데이터와 판단 규칙은 이 저장소의 프리플랍 레인지 앱(app.js)과 동일하다.
"""
import re

import evaluator as ev


# app.js 에서 추출한 레인지 데이터 (G: 169핸드 강도 순위, N: 인원별 좌석, Ks: 뒤 인원별 오픈,
# bs/ss: vs 오픈, ys: vs 3벳, os: vs 4벳, ts/hs/xs/Bs: 숏스택 잼·잼콜 %, vs: 숏스택 기준 BB)
DATA = {'G': ['AA', 'KK', 'QQ', 'JJ', 'TT', '99', '88', 'AKs', '77', 'AQs', 'AJs', 'AKo', 'ATs', 'AQo', 'AJo', 'KQs',
       '66', 'A9s', 'ATo', 'KJs', 'KTs', 'A8s', 'KQo', 'A7s', 'A9o', 'KJo', '55', 'A6s', 'K9s', 'QJs', 'A5s',
       'A8o', 'KTo', 'QTs', 'A4s', 'A7o', 'A3s', 'K8s', 'QJo', 'K9o', 'A5o', 'A6o', 'Q9s', 'JTs', 'K7s',
       'QTo', 'A2s', '44', 'A4o', 'K6s', 'Q8s', 'K5s', 'K8o', 'A3o', 'J9s', 'JTo', 'K7o', 'Q9o', 'A2o', 'K4s',
       'Q7s', 'K3s', 'J8s', 'K6o', 'T9s', '33', 'Q6s', 'Q8o', 'J9o', 'K5o', 'K2s', 'Q5s', 'T8s', 'J7s', 'K4o',
       'Q4s', 'J8o', 'Q7o', 'T9o', 'K3o', 'Q3s', 'Q6o', 'T7s', '98s', 'J6s', 'K2o', 'Q5o', '22', 'J5s', 'Q2s',
       'J7o', 'T8o', 'Q4o', 'J4s', '97s', 'T6s', 'Q3o', '98o', 'J3s', 'T7o', '87s', 'J6o', '96s', 'J2s',
       'Q2o', 'T5s', 'J5o', 'T4s', '97o', '86s', 'J4o', 'T6o', 'T3s', '95s', 'J3o', '76s', 'T2s', '87o',
       '96o', '85s', 'J2o', 'T5o', '75s', '94s', 'T4o', '86o', '93s', '65s', '95o', '84s', 'T3o', '92s',
       '76o', '74s', 'T2o', '54s', '85o', '64s', '83s', '94o', '75o', '82s', '73s', '93o', '65o', '53s',
       '63s', '84o', '92o', '43s', '74o', '64o', '72s', '54o', '52s', '62s', '83o', '42s', '82o', '73o',
       '53o', '63o', '32s', '43o', '72o', '52o', '62o', '42o', '32o'],
 'N': {'6': ['UTG', 'MP', 'CO', 'BTN', 'SB', 'BB'],
       '7': ['UTG', 'LJ', 'HJ', 'CO', 'BTN', 'SB', 'BB'],
       '8': ['UTG', 'U1', 'LJ', 'HJ', 'CO', 'BTN', 'SB', 'BB'],
       '9': ['UTG', 'U1', 'U2', 'LJ', 'HJ', 'CO', 'BTN', 'SB', 'BB'],
       '10': ['UTG', 'U1', 'U2', 'U3', 'LJ', 'HJ', 'CO', 'BTN', 'SB', 'BB']},
 'Ks': {'1': '22+, A2s+, K5s+, Q6s+, J7s+, T7s+, 96s+, 85s+, 74s+, 64s+, 53s+, A4o+, K8o+, Q9o+, J9o+, T9o',
        '2': '22+, A2s+, K2s+, Q4s+, J6s+, T6s+, 95s+, 84s+, 74s+, 63s+, 53s+, 43s, A2o+, K7o+, Q8o+, J8o+, '
             'T8o+, 97o+, 86o+, 76o, 65o',
        '3': '22+, A2s+, K7s+, Q8s+, J8s+, T7s+, 96s+, 85s+, 75s+, 64s+, 54s, 43s, A8o+, A5o, KTo+, QTo+, '
             'JTo, T9o',
        '4': '22+, A9s+, A5s-A2s, K9s+, Q9s+, J9s+, T8s+, 97s+, 86s+, 76s, 65s, 54s, ATo+, KJo+, QJo',
        '5': '22+, ATs+, A5s, A4s, KTs+, QTs+, JTs, T9s, 98s, 87s, 76s, 65s, AJo+, KQo',
        '6': '22+, ATs+, A5s, KTs+, QTs+, JTs, T9s, 98s, 87s, 76s, AJo+, KQo',
        '7': '22+, AJs+, ATs, A5s, KTs+, QJs, JTs, T9s, 98s, 87s, 76s, AQo+, KQo',
        '8': '22+, AJs+, ATs, A5s, KJs+, QJs, JTs, T9s, 98s, 76s, AQo+, KQo',
        '9': '22+, AJs+, ATs, KJs+, QJs, JTs, T9s, 98s, AQo+, KQo'},
 'bs': {'UTG': {'tb': 'QQ+, AKs, AKo, AQs, A5s',
                'call': 'JJ-22, AJs-ATs, KQs-KTs, QJs-QTs, JTs, T9s, 98s, 87s, 76s, 65s, AQo, KQo'},
        'MP': {'tb': 'QQ+, AKs, AKo, AQs, AJs, A5s, A4s',
               'call': 'JJ-22, ATs-A9s, KQs-K9s, QJs-Q9s, JTs-J9s, T9s, 98s, 87s, 76s, 65s, 54s, AQo, AJo, '
                       'KQo'},
        'CO': {'tb': 'TT+, AJs+, KJs+, A5s-A4s, AQo+',
               'call': '99-22, ATs-A2s, KTs-K9s, QTs-Q9s, JTs-J9s, T9s-T8s, 98s, 87s, 76s, 65s, 54s, '
                       'AJo-ATo, KQo-KJo, QJo'},
        'BTN': {'tb': 'TT+, ATs+, KTs+, QJs, JTs, A5s-A2s, AJo+, KJo+',
                'call': '99-22, A9s-A2s, K9s-K5s, QTs-Q6s, J9s-J7s, T9s-T7s, 98s-96s, 87s-85s, 76s-74s, '
                        '65s-64s, 54s-53s, ATo-A8o, KQo-K9o, QJo-Q9o, JTo-J9o, T9o'},
        'SB': {'tb': '77+, A8s+, KTs+, QTs+, JTs, A5s-A2s, ATo+, KJo+',
               'call': '66-22, A7s-A2s, K9s-K5s, Q9s-Q6s, J9s-J7s, T9s-T7s, 98s-96s, 87s-85s, 76s-74s, '
                       '65s-64s, 54s-53s, A9o-A4o, KTo-K8o, QTo-Q9o, JTo-J9o, T9o'}},
 'ss': {'UTG': {'ip': {'tb': 'QQ+, AKs, AKo, AJs, A5s, KQs',
                       'call': 'JJ-55, AQs, ATs, KJs, QJs, JTs, T9s, 98s, AQo'},
                'oop': {'tb': 'QQ+, AKs, AKo, A5s', 'call': 'JJ-88, AQs, AJs, KQs'}},
        'MP': {'ip': {'tb': 'QQ+, AKs, AKo, AJs, A5s, A4s, KQs',
                      'call': 'JJ-44, AQs, ATs, KJs, QJs, JTs, T9s, 98s, 87s, AQo, KQo'},
               'oop': {'tb': 'QQ+, AKs, AKo, AJs, A5s', 'call': 'JJ-77, AQs, KQs, QJs'}},
        'CO': {'ip': {'tb': 'TT+, AJs+, KQs, KJs, A5s-A4s, AQo+',
                      'call': '99-22, ATs-A8s, KTs-K9s, QTs-Q9s, J9s+, T8s+, 98s, 87s, 76s, 65s, AJo-ATo, '
                              'KJo, QJo'},
               'oop': {'tb': 'TT+, AJs+, A5s-A4s, AQo+', 'call': '99-66, AQs, AJs, KQs, KJs, QJs'}},
        'BTN': {'oop': {'tb': '99+, ATs+, KTs+, QTs+, JTs, A5s-A2s, AJo+, KJo+, QJo',
                        'call': '88-55, A9s-A7s, KJs-K9s, QJs-QTs, J9s, T9s'}}},
 'ys': {'UTG': {'oop': {'fb': 'KK+, AKs, A5s', 'call': 'QQ-TT, AQs, AKo, AJs, KQs'},
                'ip': {'fb': 'KK+, AKs, A5s, A4s', 'call': 'QQ-99, AQs, AKo, AJs, ATs, KQs, KJs, QJs, JTs'}},
        'MP': {'oop': {'fb': 'KK+, AKs, A5s', 'call': 'QQ-99, AQs, AKo, AJs, KQs, KJs'},
               'ip': {'fb': 'QQ+, AKs, AKo, A5s, A4s',
                      'call': 'JJ-88, AQs, AJs, ATs, KQs, KJs, QJs, JTs, T9s'}},
        'CO': {'oop': {'fb': 'QQ+, AKs, A5s, A4s', 'call': 'JJ-99, AQs, AKo, AJs, ATs, KQs, KJs, QJs'},
               'ip': {'fb': 'QQ+, AKs, AKo, A5s-A4s, KQs',
                      'call': 'JJ-66, AQs, AJs, ATs, KQs, KJs, QJs, JTs, T9s, 98s, AQo'}},
        'BTN': {'ip': {'fb': 'QQ+, AKs, AKo, A5s-A4s, KQs, KJs',
                       'call': 'JJ-55, AQs, AJs, ATs, A9s, KQs, KJs, QJs, JTs, T9s, 98s, 87s, AQo, KQo'},
                'oop': {'fb': 'QQ+, AKs, AKo, A5s', 'call': 'JJ-88, AQs, AJs, KQs, KJs, QJs'}},
        'SB': {'oop': {'fb': 'QQ+, AKs, AKo, A5s, A4s', 'call': 'JJ-88, AQs, AJs, KQs, KJs, QJs, AQo'},
               'ip': {'fb': 'QQ+, AKs, AKo, A5s, A4s', 'call': 'JJ-88, AQs, AJs, KQs, KJs, QJs, AQo'}}},
 'os': {'jam': 'KK+, AKs, A5s', 'call': 'QQ, AKo'},
 'ts': [5, 8, 12, 16, 20],
 'hs': {'1': [72, 60, 50, 42, 38],
        '2': [58, 44, 35, 28, 23],
        '3': [45, 34, 26, 20, 16],
        '4': [38, 29, 22, 17, 14],
        '5': [30, 23, 18, 14, 11],
        '6': [26, 20, 15, 12, 10],
        '7': [22, 17, 13, 11, 9],
        '8': [19, 15, 12, 10, 8],
        '9': [17, 13, 11, 9, 7]},
 'xs': [52, 46, 38, 32, 28],
 'Bs': {'1': 1, '2': 0.9, '3': 0.82, '4': 0.75, '5': 0.7, '6': 0.66, '7': 0.63, '8': 0.6, '9': 0.58},
 'vs': 20}

RANKS = "AKQJT98765432"  # app.js 와 같은 순서 (A가 0)
SHORT_STACK_BB = DATA["vs"]

SITUATIONS = [
    ("open", "첫 진입"),
    ("vsopen", "상대 오픈"),
    ("vs3bet", "3벳 당함"),
    ("vs4bet", "4벳 당함"),
]
SHORT_SITUATIONS = [
    ("jam", "첫 진입"),
    ("jamcall", "상대 잼"),
]
# 상황별로 골라야 하는 상대 포지션 설명 (None이면 필요 없음)
VILLAIN_LABEL = {"vsopen": "오픈 상대", "vs3bet": "3벳 상대", "jamcall": "잼 상대"}


def _idx(rank):
    return RANKS.index(rank)


def _expand(token):
    token = token.strip()
    if not token:
        return []
    m = re.fullmatch(r"([AKQJT98765432])\1-([AKQJT98765432])\2", token)
    if m:
        a, b = sorted((_idx(m.group(1)), _idx(m.group(2))))
        return [RANKS[i] * 2 for i in range(a, b + 1)]
    m = re.fullmatch(r"([AKQJT98765432])\1\+", token)
    if m:
        return [RANKS[i] * 2 for i in range(_idx(m.group(1)), -1, -1)]
    m = re.fullmatch(r"([AKQJT98765432])\1", token)
    if m:
        return [token]
    m = re.fullmatch(r"([AKQJT98765432])([AKQJT98765432])([so])-([AKQJT98765432])([AKQJT98765432])\3", token)
    if m:
        hi, suffix = m.group(1), m.group(3)
        a, b = sorted((_idx(m.group(2)), _idx(m.group(5))))
        return [hi + RANKS[i] + suffix for i in range(a, b + 1)]
    m = re.fullmatch(r"([AKQJT98765432])([AKQJT98765432])([so])\+", token)
    if m:
        hi, lo, suffix = _idx(m.group(1)), _idx(m.group(2)), m.group(3)
        return [RANKS[hi] + RANKS[i] + suffix for i in range(lo, hi, -1)]
    if re.fullmatch(r"([AKQJT98765432])([AKQJT98765432])([so])", token):
        return [token]
    return []


def parse_range(text):
    hands = set()
    for token in text.split(","):
        hands.update(_expand(token))
    return hands


def combos(hand):
    return 6 if len(hand) == 2 else 4 if hand.endswith("s") else 12


def range_pct(hands):
    return sum(combos(h) for h in hands) / 1326 * 100


def top_pct(pct):
    """핸드 순위 상위 pct% (콤보 기준)."""
    hands, total, limit = set(), 0, pct / 100 * 1326
    for h in DATA["G"]:
        if total >= limit:
            break
        hands.add(h)
        total += combos(h)
    return hands


def _interp(x, xs, ys):
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(len(xs) - 1):
        if xs[i] <= x <= xs[i + 1]:
            t = (x - xs[i]) / (xs[i + 1] - xs[i])
            return ys[i] + t * (ys[i + 1] - ys[i])
    return ys[-1]


# --- 포지션 -------------------------------------------------------------------
def seats(players):
    return DATA["N"][str(players)]


def behind(pos, players):
    """내 뒤에 남은 인원 (블라인드 포함)."""
    return players - 1 - seats(players).index(pos)


def postflop_order(pos, players):
    """포스트플랍 행동 순서 (클수록 늦게 = IP)."""
    return 0 if pos == "SB" else 1 if pos == "BB" else 2 + seats(players).index(pos)


def group(pos, players):
    if pos in ("CO", "BTN", "SB", "BB"):
        return pos
    return "MP" if behind(pos, players) == 4 else "UTG"


def after(pos, players):
    s = seats(players)
    return [p for p in s if s.index(p) > s.index(pos)]


def before(pos, players):
    s = seats(players)
    return [p for p in s if s.index(p) < s.index(pos)]


def hand_code(cards):
    """카드 2장(evaluator 정수) → 'AKs' / 'T9o' / '77'."""
    a, b = sorted(cards, key=lambda c: -(c >> 2))
    ra, rb = ev.RANKS[a >> 2], ev.RANKS[b >> 2]
    if ra == rb:
        return ra + rb
    return ra + rb + ("s" if (a & 3) == (b & 3) else "o")


# --- 상황별 레인지 --------------------------------------------------------------
def valid_villains(situation, hero, players):
    """상황에서 선택 가능한 상대 포지션 목록."""
    if situation == "vsopen":
        return [p for p in before(hero, players)]
    if situation == "vs3bet":
        return after(hero, players)
    if situation == "jamcall":
        return [p for p in before(hero, players)]
    return []


def valid_heroes(situation, players):
    s = seats(players)
    if situation in ("open", "vs3bet", "jam"):
        return [p for p in s if p != "BB"]
    if situation in ("vsopen", "jamcall"):
        return s[1:]
    return s


def ranges(situation, hero, villain, players, stack_bb=100.0, ante=False):
    """[(액션 이름, 핸드 집합, 색 키)] — 앞에 있는 액션이 우선. 나머지는 폴드."""
    if situation == "open":
        return [("오픈 레이즈", parse_range(DATA["Ks"][str(behind(hero, players))]), "raise")]
    if situation == "vsopen":
        g = group(villain, players)
        if hero == "BB":
            r = DATA["bs"][g]
        else:
            x = DATA["ss"].get(g) or DATA["ss"]["CO"]
            ip = postflop_order(hero, players) > postflop_order(villain, players)
            r = x.get("ip" if ip else "oop") or x.get("oop") or x.get("ip")
        return [("3벳", parse_range(r["tb"]), "aggro"), ("콜", parse_range(r["call"]), "call")]
    if situation == "vs3bet":
        ip = postflop_order(hero, players) > postflop_order(villain, players)
        table = DATA["ys"][group(hero, players)]
        r = table.get("ip" if ip else "oop") or table.get("oop") or table.get("ip")
        return [("4벳", parse_range(r["fb"]), "aggro"), ("콜", parse_range(r["call"]), "call")]
    if situation == "vs4bet":
        return [("5벳 잼", parse_range(DATA["os"]["jam"]), "jam"), ("콜", parse_range(DATA["os"]["call"]), "call")]
    if situation == "jam":
        pct = min(_interp(stack_bb, DATA["ts"], DATA["hs"][str(behind(hero, players))]) * (1.2 if ante else 1), 92)
        return [("잼 (올인)", top_pct(pct), "jam")]
    if situation == "jamcall":
        pct = _interp(stack_bb, DATA["ts"], DATA["xs"]) * DATA["Bs"][str(behind(villain, players))]
        pct = min(pct * (1.15 if ante else 1), 90)
        return [("잼 콜", top_pct(pct), "call")]
    return []


def decide(hand, action_ranges):
    """(액션 이름, 색 키). 어느 레인지에도 없으면 폴드."""
    for name, hands, color in action_ranges:
        if hand in hands:
            return name, color
    return "폴드", "fold"


def grid():
    """13x13 핸드 격자 (행 i, 열 j): i==j 페어, i<j 수딧, i>j 오프수딧."""
    out = []
    for i, r1 in enumerate(RANKS):
        row = []
        for j, r2 in enumerate(RANKS):
            if i == j:
                row.append(r1 + r2)
            elif i < j:
                row.append(r1 + r2 + "s")
            else:
                row.append(r2 + r1 + "o")
        out.append(row)
    return out
