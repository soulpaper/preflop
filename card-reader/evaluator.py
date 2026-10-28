"""7장 핸드 평가, 에쿼티(승률), 아웃츠 계산.

카드는 정수 0..51 (rank * 4 + suit). rank 0=2 ... 12=A, suit 0..3 = s h d c.
"""
import random
from itertools import combinations
from math import comb

RANKS = "23456789TJQKA"
SUITS = "shdc"
SUIT_SYMBOLS = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}
CATEGORY_NAMES = [
    "하이카드", "원페어", "투페어", "트리플", "스트레이트",
    "플러시", "풀하우스", "포카드", "스트레이트 플러시",
]


def parse_card(text):
    text = text.strip()
    if text[:2] == "10":
        text = "T" + text[2:]
    if len(text) != 2:
        raise ValueError(f"잘못된 카드: {text!r}")
    rank, suit = text[0].upper(), text[1].lower()
    if rank not in RANKS or suit not in SUITS:
        raise ValueError(f"잘못된 카드: {text!r}")
    return RANKS.index(rank) * 4 + SUITS.index(suit)


def is_card(text):
    return len(text) == 2 and text[0] in RANKS and text[1] in SUITS


def card_str(card, symbols=False):
    suit = SUITS[card & 3]
    return RANKS[card >> 2] + (SUIT_SYMBOLS[suit] if symbols else suit)


def _straight_high(rank_set):
    for hi in range(12, 3, -1):
        if all(hi - i in rank_set for i in range(5)):
            return hi
    if {12, 0, 1, 2, 3} <= rank_set:  # A-2-3-4-5 휠
        return 3
    return None


def evaluate(cards):
    """비교 가능한 튜플 반환 (클수록 강함). 첫 원소가 족보 카테고리."""
    counts = [0] * 13
    suit_counts = [0] * 4
    for c in cards:
        counts[c >> 2] += 1
        suit_counts[c & 3] += 1

    flush_suit = next((s for s in range(4) if suit_counts[s] >= 5), -1)
    if flush_suit >= 0:
        flush_ranks = sorted({c >> 2 for c in cards if c & 3 == flush_suit}, reverse=True)
        sf = _straight_high(set(flush_ranks))
        if sf is not None:
            return (8, sf)

    present = [r for r in range(12, -1, -1) if counts[r]]
    quads = [r for r in present if counts[r] == 4]
    trips = [r for r in present if counts[r] == 3]
    pairs = [r for r in present if counts[r] == 2]
    singles = [r for r in present if counts[r] == 1]

    if quads:
        return (7, quads[0], max((r for r in present if r != quads[0]), default=-1))  # 4장만 볼 때(드로우 계산)는 키커 없음
    if trips and (len(trips) > 1 or pairs):
        return (6, trips[0], max(trips[1:] + pairs))
    if flush_suit >= 0:
        return (5, *flush_ranks[:5])
    st = _straight_high(set(present))
    if st is not None:
        return (4, st)
    if trips:
        return (3, trips[0], *singles[:2])
    if len(pairs) >= 2:
        return (2, pairs[0], pairs[1], max((r for r in present if r not in pairs[:2]), default=-1))
    if pairs:
        return (1, pairs[0], *singles[:3])
    return (0, *singles[:5])


def hand_name(score):
    cat = score[0]
    name = CATEGORY_NAMES[cat]
    if cat in (1, 3, 7):
        return f"{name} ({RANKS[score[1]]})"
    if cat == 2:
        return f"{name} ({RANKS[score[1]]}, {RANKS[score[2]]})"
    if cat == 6:
        return f"{name} ({RANKS[score[1]]} / {RANKS[score[2]]})"
    if cat in (4, 8):
        return f"{name} ({RANKS[score[1]]} 하이)"
    return f"{name} ({RANKS[score[1]]} 하이)"


def _showdown(hero, opps, board):
    """hero 기준 획득 지분 (승=1, k명 무승부=1/k, 패=0)과 결과 종류."""
    h = evaluate(hero + board)
    best_opp = max(evaluate(o + board) for o in opps)
    if h > best_opp:
        return 1.0, "win"
    if h < best_opp:
        return 0.0, "lose"
    tied = 1 + sum(1 for o in opps if evaluate(o + board) == h)
    return 1.0 / tied, "tie"


def equity(hero, board, opponents=1, iterations=10000, exact_limit=50000, rng=random):
    """랜덤 핸드를 가진 상대 opponents명 대비 에쿼티.

    1:1 이고 경우의 수가 exact_limit 이하면 전수 계산, 아니면 몬테카를로.
    """
    dead = set(hero) | set(board)
    deck = [c for c in range(52) if c not in dead]
    need = 5 - len(board)
    tally = {"win": 0, "tie": 0, "lose": 0}
    share = 0.0
    total = 0

    n = len(deck)
    if opponents == 1 and comb(n, need) * comb(n - need, 2) <= exact_limit:
        for runout in combinations(deck, need):
            full = board + list(runout)
            rest = [c for c in deck if c not in runout]
            for opp in combinations(rest, 2):
                s, kind = _showdown(hero, [list(opp)], full)
                share += s
                tally[kind] += 1
                total += 1
        exact = True
    else:
        k = need + 2 * opponents
        for _ in range(iterations):
            drawn = rng.sample(deck, k)
            full = board + drawn[:need]
            opps = [drawn[need + 2 * i: need + 2 * i + 2] for i in range(opponents)]
            s, kind = _showdown(hero, opps, full)
            share += s
            tally[kind] += 1
        total = iterations
        exact = False

    return {
        "equity": share / total,
        "win": tally["win"] / total,
        "tie": tally["tie"] / total,
        "lose": tally["lose"] / total,
        "samples": total,
        "exact": exact,
    }


def _has_flush(cards):
    counts = [0] * 4
    for c in cards:
        counts[c & 3] += 1
    return max(counts) >= 5


def _has_straight(cards):
    return _straight_high({c >> 2 for c in cards}) is not None


def draws(hero, board):
    """드로우 종류별 아웃츠.

    반환: {"groups": [(이름, [카드, ...]), ...], "outs": 전체 아웃츠(합집합) 리스트, "deck": 남은 장수}
    페어 계열은 실전 기준으로만 센다: 이미 내 카드로 페어면 투페어/셋 카드,
    아니면 보드보다 높은 오버카드가 페어 되는 카드 (보드 페어로 모두가 같이 좋아지는 카드 제외).
    """
    if len(board) not in (3, 4):
        return {"groups": [], "outs": [], "deck": 0}
    dead = set(hero) | set(board)
    deck = [c for c in range(52) if c not in dead]
    cards = hero + board
    current = evaluate(cards)[0]
    hero_ranks = {c >> 2 for c in hero}
    board_ranks = {c >> 2 for c in board}
    hero_paired = len(hero_ranks) == 1 or bool(hero_ranks & board_ranks)
    top_board = max(board_ranks)

    flush, straight, pair, boat = [], [], [], []
    for c in deck:
        r = c >> 2
        if not _has_flush(cards) and _has_flush(cards + [c]) and not _has_flush(board + [c]):
            flush.append(c)
        if (current < 4 and not _has_straight(cards) and _has_straight(cards + [c])
                and not _has_straight(board + [c])):
            straight.append(c)
        new = evaluate(cards + [c])[0]
        if current in (2, 3) and new >= 6 and evaluate(board + [c])[0] < 6:
            boat.append(c)
        elif current <= 1 and r in hero_ranks and new > current:
            if (current == 1 and hero_paired) or r > top_board:
                pair.append(c)

    groups = []
    straight_ranks = {c >> 2 for c in straight}
    if flush:
        groups.append(("포쁠 (플러시 드로우)", flush))
    if straight:
        groups.append(("양차 (오픈엔디드)" if len(straight_ranks) >= 2 else "빵꾸 (것샷)", straight))
    if pair:
        groups.append(("원페어 → 투페어/셋" if current == 1 and hero_paired else "오버카드", pair))
    if boat:
        groups.append(("풀하우스/포카드", boat))
    total = sorted(set(flush) | set(straight) | set(pair) | set(boat))
    return {"groups": groups, "outs": total, "deck": len(deck), "combo": bool(flush and straight)}


def hit_probability(num_outs, deck_size, cards_to_come):
    """cards_to_come 장 안에 아웃츠가 한 장 이상 나올 확률."""
    if num_outs <= 0:
        return 0.0
    return 1 - comb(deck_size - num_outs, cards_to_come) / comb(deck_size, cards_to_come)
