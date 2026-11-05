"""덜 잃기 — '이기게 해 줘'가 아니라 '최대한 작게 지기'.

  1) 실시간 경고 (포스트플랍, 메이드 패):  beat_info(hero, board, ranges) → guard_lines(info, ctx, advice)
     남은 상대마다 handread 가 추정한 가중 범위로 '지금 나를 이기는 패를 가졌을 확률'을 잰다 (범위와 무관한 ev.beaters 개수와 다름).
     상대가 여럿이면 '적어도 한 명이 이기는 패' = 1 - Π(1 - p_i) (상대끼리 카드 겹침은 무시 — handread 와 같은 가정).
     화면 2줄:  "나를 이기는 패 45%: 더 높은 플러시 (A♥, K♥ 가진 사람) 35% · 풀하우스 (33, QT) 10%"
                "→ 접는 게 덜 잃어요 (이길 확률 55% < 필요 60%)"  또는  "→ 이 판은 앞으로 최대 14BB까지만 (이길 확률 70%)"
     이길 확률 w = 1 - (이기는 패 확률) - 비김/2 (지금 보드 기준: 플랍·턴에서 앞으로 나올 카드는 보지 않는다 — 단순하고 정직하게).
     접기 기준: 벳을 받았을 때 w < 팟 오즈(콜 / (팟 + 콜)) — 즉 '이기는 패 확률 > 1 - 팟 오즈'.
     '앞으로 최대 N BB': 지금부터 내가 더 넣는 돈 A 를 상대가 맞춰 받을 때 본전 이상인 가장 큰 A.
        EV(A) = w × (팟 + 2A - 콜) - A >= 0  →  A <= w × (팟 - 콜) / (1 - 2w)   (w >= 50% 이면 끝까지 가도 됨).
        이미 낸 돈(매몰 비용)은 넣지 않는다 — 앞으로의 판단에만 쓰이는 숫자라서.
     추천(advisor)이 결정하고, 이 경고는 그 아래 손해 쪽을 설명한다: 추천과 반대로 말하지 않는다
        (계산상 접기인데 추천이 콜이면 '추천은 콜 (다음 카드 N장으로 좋아질 수 있음) — 더 커지면 접기').
     쇼다운 채점(2026-10-29~11-06 로그, 내 결정 187지점): 예측 평균 52% / 실제로 진 비율 65% (쇼다운 편향: 접은 약한 패는
     안 보임). 벳을 받고 '접는 게 덜 잃어요' 쪽이었던 9번은 9번 모두 실제로 지고 있었다.

  2) 핸드 뒤 복기:  review_hand(rec, ...) → {"lines": [최대 3줄], ... 숫자 기록}
     rec = handlog 기록 (actions/board/hero_cards/seats/showdown). 닉네임은 쓰지 않는다 (좌석 키·포지션·숫자만).
       - 쇼다운 카드를 읽었으면: 무엇이 나를 이겼나 + 잃은 칩이 어디서 나갔나 + 그때 상대 범위에서 내가 이기는 경우 → 접었으면 덜 잃은 칩.
       - 폴드 에쿼티를 놓친 곳 (추정): 체크/콜한 스트리트에서 내 패가 약한 페어·드로우·노페어였으면, 그 시점 상대 범위가
         팟 2/3 · 팟 크기 벳(레이즈)에 접을 몫을 handread 의 보드 묶음별 단순 폴드 표(FOLD)로 재고, 실제로 한 체크/콜을 같은 모델로
         본 값과의 차이(평균 BB)가 MISS_MIN_BB 이상이고 접을 확률이 MISS_MIN_FOLD 이상일 때만 보여 준다 (실제 결과와 바로 비교하면
         뒤 스트리트의 실수·운까지 '벳 안 한 탓'이 되어 부풀려진다 — 그 값은 기록 fe_spots.ev_vs_actual 에만).
         상대 카드를 봤는데 그 패가 그 벳에 접지 않았을 패면 보여 주지 않는다. 진 판에서만.
       - 쇼다운 카드 없이 크게 잃었으면: 가장 많이 잃은 결정 하나 (기록이 일부 빠진 판은 금액을 지어내지 않고 그렇다고만).
     돈 계산은 기록된 금액(BB)으로 하는 근사 (사이드 팟 무시, 앤티는 ante_bb 를 주면 팟에 더함).

순수 함수 (파일·화면 I/O 없음). 몬테카를로 에쿼티는 고정 시드라 같은 입력이면 같은 결과.
오프라인:  python lossreview.py [hands 폴더]  → 모든 핸드 복기, 가장 크게 진 5판과 합계.
"""
import json
import random
import sys
from collections import Counter
from pathlib import Path

import evaluator as ev
import handlog as hl
import handread as hr
import rangeq as rq

STREETS = ("preflop", "flop", "turn", "river")
NBOARD = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}
STREET_KO = {"preflop": "프리플랍", "flop": "플랍", "turn": "턴", "river": "리버"}
ACT_KO = {"call": "콜", "bet": "벳", "raise": "레이즈", "allin": "올인", "check": "체크", "fold": "폴드"}
_R = ev.RANKS

# --- 폴드 모델 (추정) -----------------------------------------------------------------------
# 상대 패를 handread 의 보드 기준 묶음으로 5종으로 나누고, 벳 크기별 '접을 확률'을 곱한다. 리버에서는 드로우가 없으므로
# 놓친 드로우는 '노페어'로 들어간다. 숫자는 보수적인 상식값 (쇼다운 데이터로 검증되지 않음 → 화면에 늘 '추정').
#                 팟 2/3  팟 크기
FOLD = {"air": (0.80, 0.88),      # 노페어 / 놓친 드로우
        "draw": (0.40, 0.50),     # 플러시·스트레이트 드로우 (플랍·턴)
        "weak": (0.50, 0.65),     # 중간·약한 페어, 보드 아래 포켓 페어
        "top": (0.12, 0.20),      # 탑페어 · 오버페어
        "strong": (0.02, 0.04)}   # 투페어 이상
SIZES = ((2 / 3, "팟 2/3", 0), (1.0, "팟 크기", 1))
RAISE_FOLD = 0.8          # 이미 벳한 상대에게 레이즈: 돈을 넣었으니 덜 접는다
MISS_MIN_BB = 2.0         # 폴드 에쿼티 줄은 평균 차이가 이 이상일 때만
MISS_MIN_FOLD = 0.30      # ... 그리고 (모두) 접을 확률이 이 이상일 때만 ('접게 해서 이긴다'는 말이 맞을 때)
# 이 문제가 있는 기록은 금액 줄을 쓰지 않는다 (내 금액·순서를 믿을 수 없음). gap·allin_unread 는 내 금액엔 영향이 적어 허용
MONEY_FATAL = {"ambiguous_order", "tracker_error", "bet_decreased", "incomplete", "mid_hand_start"}
CHECK_BETTER = 0.45       # 내 포스트플랍 벳/레이즈를 받아 준 범위에서 이 미만이면 '체크가 덜 잃음'
BIG_LOSS_BB = 10.0        # 쇼다운 카드 없이 이만큼 이상 잃었으면 가장 큰 결정 한 줄
REVIEW_MIN_BB = 3.0       # 이보다 적게 넣은 판은 복기 줄 없음 (블라인드 폴드 등)
EQ_ITERS = 1200           # 플랍·턴 에쿼티 몬테카를로 표본 (리버 헤즈업은 정확 계산)
GUARD_MIN = 0.05          # 이기는 패 확률이 이보다 작으면 '거의 없음'


def _num(v):
    try:
        f = None if v is None or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None
    return f if f is not None and f == f else None


def _cards(seq):
    out = []
    for c in seq or []:
        try:
            x = c if isinstance(c, int) else ev.parse_card(str(c))
        except (ValueError, TypeError, IndexError):
            continue
        if 0 <= x < 52 and x not in out:
            out.append(x)
    return out


def _pct(x):
    """화면용 확률 글자: 10% 이상은 5% 단위, 그 아래는 1% 단위 (0 이 아닌 값은 최소 1%)."""
    p = 100.0 * x
    if p >= 10:
        return f"{int(5 * round(p / 5))}%"
    return f"{max(1, int(round(p)))}%" if p > 0 else "0%"


def _bb(x):
    return f"{x:.0f}BB" if x >= 10 else f"{x:.1f}BB".replace(".0BB", "BB")


def _prod(xs):
    out = 1.0
    for x in xs:
        out *= x
    return out


# --- 족보 설명 ---------------------------------------------------------------------------
def hand_desc(score, other=None):
    """족보 점수 → 짧은 한국어 ('J 플러시', 'Q 페어', 'K·7 투페어'). other(비교 상대 점수)와 족보·주 랭크가 같으면 키커를 붙인다."""
    cat = score[0]
    r = _R[score[1]] if len(score) > 1 and score[1] >= 0 else "?"
    if cat == 0:
        text = f"{r} 하이"
    elif cat == 1:
        text = f"{r} 페어"
    elif cat == 2:
        text = f"{r}·{_R[score[2]]} 투페어"
    elif cat == 3:
        text = f"{r} 트리플"
    elif cat == 4:
        text = f"{r} 스트레이트"
    elif cat == 5:
        text = f"{r} 플러시"
    elif cat == 6:
        text = f"{r} 풀하우스"
    elif cat == 7:
        text = f"{r} 포카드"
    else:
        text = f"{r} 스트레이트 플러시"
    if other is not None and other[0] == cat and cat != 5:
        main = {0: 1, 1: 2, 2: 3, 3: 2, 7: 2}.get(cat, len(score))
        if tuple(score[:main]) == tuple(other[:main]):
            diff = next((i for i in range(main, min(len(score), len(other))) if score[i] != other[i]), None)
            if diff is not None and score[diff] >= 0:
                text += f" ({_R[score[diff]]} 키커)"
    if other is not None and other[0] == cat == 5 and score[1] == other[1]:
        diff = next((i for i in range(2, min(len(score), len(other))) if score[i] != other[i]), None)
        if diff is not None:
            text += f" (다음 카드 {_R[score[diff]]})"
    return text


_BASE = {2: "투페어", 4: "스트레이트", 5: "플러시", 6: "풀하우스", 7: "포카드", 8: "스트레이트 플러시"}


def _beat_group(me, sc, board, c1, c2):
    """나를 이기는 콤보 → (묶음 이름, 예시 표기). 같은 족보면 '더 높은 …', 플러시는 가진 무늬 카드('A♥')."""
    cat = sc[0]
    if cat == me[0]:
        if cat == 1:
            name = "같은 페어, 더 높은 키커" if sc[1] == me[1] else "더 높은 페어"
        elif cat == 3:
            name = "같은 트리플, 더 높은 키커" if sc[1] == me[1] else "더 높은 트리플"
        elif cat == 2 and sc[1] == me[1] and sc[2] == me[2]:
            name = "같은 투페어, 더 높은 키커"
        else:
            name = "더 높은 " + _BASE.get(cat, ev.CATEGORY_NAMES[cat])
    elif cat == 3:
        name = "셋" if c1 >> 2 == c2 >> 2 else "트립스"
    else:
        name = _BASE.get(cat, ev.CATEGORY_NAMES[cat])
    if cat in (5, 8):
        cnt = Counter(c & 3 for c in list(board) + [c1, c2])
        s = max(cnt, key=cnt.get)
        mine = sorted((c for c in (c1, c2) if c & 3 == s), reverse=True)
        if mine:
            return name, ev.card_str(mine[0], symbols=True)
    hi, lo = sorted((c1 >> 2, c2 >> 2), reverse=True)
    return name, _R[hi] + _R[lo]


# --- 1) 실시간: 이기는 패 확률 ---------------------------------------------------------------
def beat_info(hero, board, ranges):
    """hero 2장, board 3~5장, ranges = 남은 상대별 범위 리스트 ({'AhKd': 가중치} 또는 {'AKs': 가중치}).
    메이드 패(원페어 이상)가 아니면 None.
    → {p(적어도 한 명이 이기는 패), tie, per[상대별 p], groups[{name, pct, ex[], cat}], hand(내 족보 글), cat, n}"""
    h, b = _cards(hero), _cards(board)
    if len(h) != 2 or len(b) < 3 or not ranges:
        return None
    me = ev.evaluate(h + b)
    if me[0] < 1:
        return None
    dead = set(h) | set(b)
    per, ties, gper = [], [], []
    for rng in ranges:
        tot = beat = tie = 0.0
        g = {}
        for c1, c2, w in rq.expand(rng or {}):
            if c1 in dead or c2 in dead or c1 == c2:
                continue
            tot += w
            sc = ev.evaluate_fast(b + [c1, c2])
            if sc > me:
                beat += w
                name, ex = _beat_group(me, sc, b, c1, c2)
                d = g.setdefault(name, [0.0, {}, sc[0]])
                d[0] += w
                d[1][ex] = d[1].get(ex, 0.0) + w
            elif sc == me:
                tie += w
        if tot <= 0:
            continue
        per.append(beat / tot)
        ties.append(tie / tot)
        gper.append({k: (v[0] / tot, {e: x / tot for e, x in v[1].items()}, v[2]) for k, v in g.items()})
    if not per:
        return None
    none_beat = _prod(1 - p for p in per)
    tie = max(0.0, none_beat - _prod(1 - p - t for p, t in zip(per, ties)))
    groups = []
    for name in {n for gp in gper for n in gp}:
        q = 1 - _prod(1 - gp[name][0] for gp in gper if name in gp)
        ex = Counter()
        for gp in gper:
            if name in gp:
                ex.update(gp[name][1])
        cat = next(gp[name][2] for gp in gper if name in gp)
        groups.append({"name": name, "pct": round(q, 4), "ex": [e for e, _ in ex.most_common(2)], "cat": cat})
    groups.sort(key=lambda g: (-g["pct"], -g["cat"]))
    return {"p": round(1 - none_beat, 4), "tie": round(tie, 4), "per": [round(p, 4) for p in per], "groups": groups,
            "hand": hand_desc(me), "cat": me[0], "n": len(per)}


def beat_text(info, n=2):
    """'나를 이기는 패 45%: 더 높은 플러시 (A♥, K♥ 가진 사람) 35% · 풀하우스 (33, QT) 10%'."""
    if not info:
        return ""
    if info["p"] < GUARD_MIN:
        return "나를 이기는 패: 거의 없음 (5% 미만)"
    parts = []
    for g in info["groups"]:
        if len(parts) >= n or g["pct"] < 0.03:
            break
        ex = ", ".join(g["ex"])
        if g["cat"] in (5, 8) and ex:
            ex += " 가진 사람"
        parts.append(f"{g['name']} ({ex}) {_pct(g['pct'])}" if ex else f"{g['name']} {_pct(g['pct'])}")
    who = "나를 이기는 패" if info.get("n", 1) <= 1 else f"상대 {info['n']}명 중 누군가 나를 이길 패"
    return f"{who} {_pct(info['p'])}" + (": " + " · ".join(parts) if parts else "")


def max_more(w, pot, call=0.0):
    """앞으로 더 넣어도 본전 이상인 최대 금액 (BB). w >= 0.5 면 None (제한 없음). pot = 지금 팟(상대 벳 포함), call = 콜 금액."""
    call = max(0.0, call or 0.0)
    if w >= 0.5:
        return None
    return max(0.0, w * max(0.0, pot - call) / (1 - 2 * w))


def guard_lines(info, ctx, advice=None):
    """beat_info 결과 + 추천 ctx(pot, pot_total, call, my_stack, street) + 추천(action) → 화면 줄.
    → {"lines": [이기는 패 줄, 손해 한도 줄], "fold": 접기 쪽인가, "w": 이길 확률, "need": 필요 승률, "max_bb": 최대 BB 또는 None}
    추천과 반대로 말하지 않는다: 계산상 접기인데 추천이 콜/레이즈면 그 사실과 '더 커지면 접기'만 말한다."""
    if not info:
        return None
    ctx = ctx or {}
    call = _num(ctx.get("call")) or 0.0
    total = _num(ctx.get("pot_total"))
    if not total:
        pot = _num(ctx.get("pot"))
        total = pot + call if pot is not None else None
    w = max(0.0, 1.0 - info["p"] - info.get("tie", 0.0) / 2)
    act = (advice or {}).get("action")
    act_ko = (advice or {}).get("action_ko") or ACT_KO.get(act, act or "")
    out = {"lines": [beat_text(info)], "fold": False, "p": info["p"], "w": round(w, 3), "need": None, "max_bb": None}
    if total is None or total <= 0:
        return out
    need = call / (total + call) if call > 0 else 0.0
    out["need"] = round(need, 3)
    street = ctx.get("street") or {3: "flop", 4: "turn", 5: "river"}.get(len(ctx.get("board") or []))
    if call > 0 and w < need:
        out["fold"] = True
        if act in (None, "fold", "wait") or advice is None:
            out["lines"].append(f"→ 접는 게 덜 잃어요 (이길 확률 {_pct(w)} < 필요 {_pct(need)})")
        else:
            outs = _num((ctx.get("outs") or {}).get("count")) if isinstance(ctx.get("outs"), dict) else None
            why = f" (다음 카드 {outs:.0f}장으로 좋아질 수 있음)" if street in ("flop", "turn") and outs else ""
            out["lines"].append(f"→ 지금은 이길 확률 {_pct(w)} < 필요 {_pct(need)} · 추천은 {act_ko}{why} — 더 커지면 접기")
        return out
    more = max_more(w, total, call)
    stack = _num(ctx.get("my_stack"))
    if act == "fold":
        out["lines"].append(f"→ 계산상 콜도 되지만 추천은 폴드 — 이 판에 더 넣지 않기")
        return out
    if more is None or (stack and more >= stack):
        out["lines"].append(f"→ 이길 확률 {_pct(w)} — 이 판은 끝까지 가도 괜찮아요")
        return out
    more = max(more, call)
    out["max_bb"] = round(more, 1)
    out["lines"].append(f"→ 이 판은 앞으로 최대 {_bb(more)}까지만 (이길 확률 {_pct(w)})")
    return out


# --- 2) 핸드 뒤 복기 -----------------------------------------------------------------------
def _hero_key(rec):
    return next((k for k, s in (rec.get("seats") or {}).items() if isinstance(s, dict) and s.get("hero")), None)


def timeline(rec, ante_bb=0.0):
    """기록의 액션을 순서대로 다시 돌려 돈 흐름을 잰다 (프리플랍은 handlog.clean_preflop 정리본).
    → (rows, total{seat: 낸 합계}, final_pot). row = {street, i(그 스트리트 정리본 인덱스), seat, act, to, added,
       pot_before(액션 직전 팟, 이번 스트리트 벳 포함), facing(콜하려면 더 낼 금액), allin, level}"""
    acts = rec.get("actions") or {}
    pot = max(0.0, _num(ante_bb) or 0.0)
    total = Counter()
    rows = []
    for st in STREETS:
        seq = hl.clean_preflop(acts.get("preflop"))[0] if st == "preflop" else list(acts.get(st) or [])
        inv = Counter()
        level = 0.0
        if st == "preflop" and not any(a.get("act") == "post" for a in seq):
            for k, s in (rec.get("seats") or {}).items():   # 블라인드 칩을 못 읽은 기록: 포지션으로
                p = (s or {}).get("pos")
                if p in ("SB", "BB"):
                    inv[k] = 0.5 if p == "SB" else 1.0
                    level = max(level, inv[k])
        for i, a in enumerate(seq):
            if not isinstance(a, dict):
                continue
            k, act, to = a.get("seat"), a.get("act"), _num(a.get("to"))
            if to is not None and (to > hl.MAX_BB or (st != "preflop" and not a.get("allin")
                                                       and to > hl.MAX_JUMP * max(pot, level, 1.0))):
                to = None      # 말이 안 되는 금액 (OCR 오류)
            before = pot + sum(inv.values())
            prev = inv[k]
            if act == "post":
                if to is not None:
                    inv[k] = max(prev, to)
                    level = max(level, inv[k])
                continue
            if act == "call" and to is None:
                to = level
            last = next((x for x in reversed(rows) if x["street"] == st and x["act"] in ("bet", "raise")), None)
            if (st != "preflop" and act in ("bet", "raise") and to is not None and last is not None
                    and last["seat"] == k and last["to"] is not None and to > last["to"]):
                # 같은 사람이 그 사이 다른 사람의 벳/레이즈 없이 또 올림 = 칩이 쌓이는 중에 읽은 금액 (166.4 → 243 올인) → 한 액션
                # (그 사이 폴드·콜만 있었으면 다시 올릴 차례가 오지 않는다)
                last["to"] = to
                last["added"] = round(last["added"] + to - prev, 2)
                last["allin"] = last["allin"] or bool(a.get("allin"))
                inv[k] = to
                level = max(level, to)
                last["level"] = level
                continue
            facing = max(0.0, level - prev)
            added = max(0.0, to - prev) if act in ("call", "bet", "raise") and to is not None else 0.0
            rows.append({"street": st, "i": i, "seat": k, "act": act, "to": to, "added": round(added, 2),
                         "pot_before": round(before, 2), "facing": round(facing, 2), "allin": bool(a.get("allin")),
                         "level": level})
            if to is not None and act in ("call", "bet", "raise"):
                inv[k] = max(prev, to)
                level = max(level, inv[k])
        pot += sum(inv.values())
        total.update(inv)
    return rows, dict(total), round(pot, 2)


def outcome(rec, total, hero_key):
    """→ (내 손익 BB 또는 None, 근거 'folded'/'uncontested'/'showdown'/'unknown', 이긴 상대 좌석 또는 None)."""
    seats = rec.get("seats") or {}
    hero = seats.get(hero_key) or {}
    inv_h = total.get(hero_key, 0.0)
    others = sum(v for k, v in total.items() if k != hero_key)
    live = [k for k, s in seats.items() if not (s or {}).get("folded")]
    if hero.get("folded"):
        return -inv_h, "folded", None
    if live == [hero_key]:
        return others, "uncontested", None
    board = _cards(rec.get("board"))
    hc = _cards(rec.get("hero_cards"))
    if len(board) == 5 and len(hc) == 2:
        me = ev.evaluate(hc + board)
        scores = {}
        for k in live:
            if k == hero_key:
                continue
            sh = _cards((seats.get(k) or {}).get("shown"))
            if len(sh) == 2:
                scores[k] = ev.evaluate(sh + board)
        best = max(scores.items(), key=lambda kv: kv[1]) if scores else None
        if best and best[1] > me:
            return -inv_h, "showdown", best[0]          # 보인 상대 하나라도 이기면 진 판
        if scores and len(scores) == len(live) - 1:
            if best[1] == me:
                return 0.0, "showdown", None
            return others, "showdown", None
    return None, "unknown", None


def _folded_before(rows, upto):
    out = set()
    for r in rows[:upto]:
        if r["act"] == "fold":
            out.add(r["seat"])
    return out


def _truncate(rec, rows, upto):
    """rows[:upto] 까지의 액션만 남긴 actions (handread 입력용; 프리플랍은 정리본)."""
    acts = rec.get("actions") or {}
    pre = hl.clean_preflop(acts.get("preflop"))[0]
    if upto >= len(rows):
        return {"preflop": pre, **{s: list(acts.get(s) or []) for s in STREETS[1:]}}
    r = rows[upto]
    si = STREETS.index(r["street"])
    out = {}
    for j, s in enumerate(STREETS):
        seq = pre if s == "preflop" else list(acts.get(s) or [])
        if j < si:
            out[s] = seq
        elif j == si:
            out[s] = seq[:r["i"]]
    return out


def ranges_at(rec, rows, upto, opp_keys, hero, board_n, players=None, stack_bb=None, ante_bb=0.0):
    """rows[upto] 직전까지의 액션으로 opp_keys 의 범위 → {seat: handread 결과}."""
    seats = rec.get("seats") or {}
    pos = {k: (s or {}).get("pos") for k, s in seats.items()}
    hr_seats = hl.to_handread(_truncate(rec, rows, upto), pos, street_pot=rec.get("street_pot"), ante=ante_bb or 0.0)
    pick = {}
    for k in opp_keys:
        s = dict(hr_seats.get(k) or {"pos": pos.get(k)})
        ty = (seats.get(k) or {}).get("type")
        if ty:
            s["type"] = ty
        pick[k] = s
    players = int(_num(players) or _num(rec.get("pos_players")) or _num(rec.get("players")) or 9)
    return hr.estimate(pick, board_n, players, dead=hero, stack_bb=stack_bb or 100, ante=bool(ante_bb))


def equity(hero, board, ranges, iters=EQ_ITERS, seed=11):
    """내 에쿼티 (0~1). 리버 + 상대 1명이면 범위 전체로 정확히, 아니면 고정 시드 몬테카를로."""
    h, b = _cards(hero), _cards(board)
    ranges = [r for r in ranges if r]
    if not ranges:
        return None
    if len(b) == 5 and len(ranges) == 1:
        me = ev.evaluate(h + b)
        dead = set(h) | set(b)
        tot = share = 0.0
        for c1, c2, w in rq.expand(ranges[0]):
            if c1 in dead or c2 in dead:
                continue
            sc = ev.evaluate_fast(b + [c1, c2])
            tot += w
            share += w if me > sc else w / 2 if me == sc else 0.0
        return share / tot if tot > 0 else None
    try:
        return rq.equity_vs_ranges(h, b, ranges, iters, rng=random.Random(seed), cache=False)["equity"]
    except ValueError:
        return None


def _fold_cat(group):
    if group == "노페어":
        return "air"
    if group in ("플러시 드로우", "스트레이트 드로우"):
        return "draw"
    if group == "오버페어" or group.endswith("탑페어"):
        return "top"
    if group == "포켓페어" or group.endswith(" 페어"):
        return "weak"
    return "strong"


def fold_split(rng, board, size_idx, raise_=False):
    """범위 → (접을 확률, 계속하는 범위 {combo: 가중치}). 묶음별 FOLD 표 (레이즈면 RAISE_FOLD 배)."""
    b = _cards(board)
    tot = fold = 0.0
    cont = {}
    k = RAISE_FOLD if raise_ else 1.0
    for c1, c2, w in rq.expand(rng or {}):
        if c1 in b or c2 in b:
            continue
        f = FOLD[_fold_cat(hr.combo_group(b, c1, c2))][size_idx] * k
        tot += w
        fold += w * f
        if w * (1 - f) > 0:
            key = ev.card_str(max(c1, c2)) + ev.card_str(min(c1, c2))
            cont[key] = cont.get(key, 0.0) + w * (1 - f)
    return (fold / tot if tot > 0 else 0.0), cont


def _shown_cat(rec, k, board_n):
    sh = _cards(((rec.get("seats") or {}).get(k) or {}).get("shown"))
    if len(sh) != 2:
        return None, None
    return _fold_cat(hr.combo_group(_cards(board_n), sh[0], sh[1])), sh


def _faced_word(rows, idx):
    """rows[idx] (내 콜) 이 받은 상대 액션 이름: 벳/레이즈/올인."""
    r = rows[idx]
    for x in reversed(rows[:idx]):
        if x["street"] != r["street"]:
            break
        if x["act"] in ("bet", "raise") and x["seat"] != r["seat"]:
            if x.get("allin"):
                return "올인"
            return ACT_KO[x["act"]]
    return "벳"


def review_hand(rec, ante_bb=0.0, stack_bb=None, players=None, iters=EQ_ITERS):
    """핸드 기록 하나 → 복기 dict (기록에 그대로 넣을 수 있는 숫자 + 화면 줄 'lines' 최대 3개). 실패해도 예외 없이 빈 결과."""
    try:
        return _review(rec, ante_bb, stack_bb, players, iters)
    except Exception as exc:   # 복기는 부가 기능: 앱을 멈추지 않는다
        return {"v": 1, "lines": [], "error": repr(exc)[:200]}


def _review(rec, ante_bb, stack_bb, players, iters):
    out = {"v": 1, "lines": []}
    hk = _hero_key(rec)
    hero = _cards(rec.get("hero_cards"))
    if not isinstance(rec, dict) or hk is None or len(hero) != 2:
        return out
    board = _cards(rec.get("board"))
    rows, total, _pot = timeline(rec, ante_bb)
    inv_h = round(total.get(hk, 0.0), 2)
    delta, src, winner = outcome(rec, total, hk)
    codes = {i.get("code") for i in rec.get("issues") or [] if isinstance(i, dict)}
    reliable = not (codes & MONEY_FATAL)
    out.update(result_bb=None if delta is None else round(delta, 2), result_src=src, invested_bb=inv_h,
               reliable=reliable)
    seats = rec.get("seats") or {}
    pos = {k: (s or {}).get("pos") for k, s in seats.items()}
    mine = [i for i, r in enumerate(rows) if r["seat"] == hk]

    def opps_at(i):
        gone = _folded_before(rows, i)
        return [k for k in seats if k != hk and k not in gone]

    def contesting(i):
        """rows[i] 때 이 팟을 다투는 상대: 살아 있고, 그 스트리트에서 나중에 접지 않은 사람 (뒤에서 접을 사람은 빼고)."""
        st_ = rows[i]["street"]
        later = {x["seat"] for x in rows[i + 1:] if x["street"] == st_ and x["act"] == "fold"}
        keys = opps_at(i)
        return [k for k in keys if k not in later] or keys

    def rng_list(i, keys, n):
        est = ranges_at(rec, rows, i, keys, hero, board[:n], players, stack_bb, ante_bb)
        return [est[k]["range"] for k in keys if k in est]

    lines = []
    # (a) 쇼다운: 무엇이 나를 이겼나
    if winner is not None and len(board) == 5:
        sh = _cards((seats.get(winner) or {}).get("shown"))
        me, op = ev.evaluate(hero + board), ev.evaluate(sh + board)
        if op > me:   # 늘 참 (outcome 이 그렇게 골랐다) — 그래도 다시 확인하고만 말한다
            cards = "".join(ev.card_str(c, symbols=True) for c in sh)
            out["lost_to"] = {"seat": winner, "pos": pos.get(winner), "cards": [ev.card_str(c) for c in sh],
                              "hand": hand_desc(op, me), "hero_hand": hand_desc(me, op)}
            lines.append(f"상대({pos.get(winner) or '?'}) {cards} {hand_desc(op, me)} — 내 {hand_desc(me, op)}보다 높음")
            paid = 0.0       # 내 메이드 패가 이미 그 상대 패에 지고 있던 스트리트에 넣은 돈 = 이기는 패에 갚아 준 돈
            for i in mine:
                r = rows[i]
                n = NBOARD[r["street"]]
                mine_sc = ev.evaluate(hero + board[:n]) if n >= 3 else None
                if mine_sc and mine_sc[0] >= 1 and r["added"] > 0 and ev.evaluate(sh + board[:n]) > mine_sc:
                    paid += r["added"]
            out["paid_beaters_bb"] = round(paid, 2)

    # (b) 가장 크게 잃은 결정 (진 판: 쇼다운에서 졌거나 폴드)
    lost = delta is not None and delta < 0
    if lost and inv_h >= REVIEW_MIN_BB and reliable:
        spend = [i for i in mine if rows[i]["added"] > 0 and rows[i]["act"] in ("call", "bet", "raise")]
        if spend and (winner is not None or -delta >= BIG_LOSS_BB):
            i = max(spend, key=lambda j: (rows[j]["added"], j))
            r = rows[i]
            n = NBOARD[r["street"]]
            keys = contesting(i)
            saved = round(sum(rows[j]["added"] for j in mine if j >= i), 2)
            big = {"street": r["street"], "act": r["act"], "bb": r["added"], "saved_bb": saved}
            head = ("잃은 칩 대부분은 " if r["added"] >= 0.5 * inv_h else "가장 큰 돈은 ") + \
                f"{STREET_KO[r['street']]} {ACT_KO['allin'] if r['allin'] else ACT_KO[r['act']]} {_bb(r['added'])}"
            tail = ""
            if keys and r["act"] == "call":
                eq = equity(hero, board[:n], rng_list(i, keys, n), iters)
                need = r["added"] / (r["pot_before"] + r["added"]) if r["pot_before"] + r["added"] > 0 else None
                if eq is not None and need is not None:
                    big.update(win_pct=round(100 * eq, 1), need_pct=round(100 * need, 1))
                    word = f"그 {_faced_word(rows, i)} 범위" if len(keys) == 1 else f"상대 {len(keys)}명 범위"
                    if eq < need - 0.02:
                        tail = f" · {word}에서 내가 이기는 경우 {_pct(eq)} → 접었으면 {_bb(saved)} 덜 잃음"
                        big["fold_better"] = True
                    elif eq >= need:
                        tail = (f" · {word}에서 이기는 경우 {_pct(eq)} ≥ 필요 {_pct(need)} → 맞는 콜"
                                + (" (운이 나빴음)" if src == "showdown" else ""))
                        big["fold_better"] = False
            elif keys and r["act"] in ("bet", "raise"):
                # 내 벳/레이즈를 받아 준 범위: 같은 스트리트에서 내 액션 뒤 상대 응답까지 포함
                j = i + 1
                while j < len(rows) and rows[j]["street"] == r["street"] and rows[j]["seat"] != hk:
                    j += 1
                callers = [k for k in opps_at(j) if k in keys]
                if callers:
                    eq = equity(hero, board[:n], rng_list(j, callers, n), iters)
                    if eq is not None:
                        big["win_pct"] = round(100 * eq, 1)
                        if n == 0:
                            # 프리플랍 레이즈/올인: 접게 만든 몫이 따로 있어 '안 했으면 덜 잃음'은 말할 수 없다 → 사실만
                            if eq < 0.5:
                                tail = f" · 받아 준 범위에서 내 이길 확률 {_pct(eq)}"
                        elif eq < CHECK_BETTER:
                            tail = f" · 받아 준 범위엔 내가 이기는 경우 {_pct(eq)} → 체크했으면 최대 {_bb(r['added'])} 덜 잃음"
                            big["check_better"] = True
                        if not tail and eq >= 0.6 and src == "showdown":
                            tail = f" · 받아 준 범위에서 내 이길 확률 {_pct(eq)} → 괜찮은 {ACT_KO['allin'] if r['allin'] else ACT_KO[r['act']]} (운이 나빴음)"
            out["biggest"] = big
            lines.append(head + tail)

    # (c) 폴드 에쿼티를 놓친 곳 (이기지 못한 판에서만)
    if reliable and (delta is None or delta <= 0) and inv_h >= REVIEW_MIN_BB:
        best = shown = None
        spots = []
        first_seen = set()
        for i in mine:
            r = rows[i]
            st = r["street"]
            if st == "preflop" or st in first_seen:
                continue
            first_seen.add(st)
            n = NBOARD[st]
            if len(board) < n or r["act"] not in ("check", "call") or r["allin"]:
                continue
            if any(x["allin"] for x in rows[:i] if x["street"] == st and x["seat"] != hk):
                continue   # 올인을 받음: 레이즈로 접게 할 수 없음
            bn = board[:n]
            if _fold_cat(hr.combo_group(bn, hero[0], hero[1])) in ("top", "strong"):
                continue   # 탑페어 이상은 접게 하려는 벳이 아님
            allin_opp = {x["seat"] for x in rows[:i] if x["allin"] and x["seat"] != hk}
            keys = [k for k in opps_at(i) if k not in allin_opp]   # 올인한 상대는 접을 수 없다
            if not keys or len(keys) < len(opps_at(i)):
                continue
            rngs = rng_list(i, keys, n)
            if len(rngs) != len(keys):
                continue
            T, b = r["pot_before"], r["facing"] if r["act"] == "call" else 0.0
            raise_ = b > 0
            eq_now = equity(hero, bn, rngs, iters)
            if eq_now is None:
                continue
            # 비교 대상 = 실제로 한 체크/콜을 같은 모델로 본 값 (지금 에쿼티만큼 팟을 가져간다고 후하게 봄 → 벳 쪽엔 보수적).
            # 실제 결과(그 뒤에 팟을 잃음)와 비교하면 나중 스트리트의 실수·운까지 '벳 안 한 탓'이 되어 부풀려진다 → 기록에만 남김.
            taken = eq_now * (T + b) - b
            here = sum(rows[j]["added"] for j in mine if j >= i and rows[j]["street"] == st)
            realized = -here if (src in ("folded", "showdown") and delta is not None and delta < 0) else None
            for frac, label, si in SIZES:
                add = b + frac * (T + b)
                fs, conts = [], []
                for rg in rngs:
                    f, cont = fold_split(rg, bn, si, raise_)
                    fs.append(f)
                    conts.append(cont)
                f_all = _prod(fs)
                if f_all >= 0.999:
                    eq_c = 0.0
                else:
                    eq_c = equity(hero, bn, conts, iters)
                    if eq_c is None:
                        continue
                ev_alt = f_all * T + (1 - f_all) * (eq_c * (T + 2 * add - b) - add)
                diff = ev_alt - taken
                shown_ok = None
                for k in keys:   # 카드를 본 상대가 그 벳에 접지 않았을 패였으면 말하지 않는다
                    cat, _ = _shown_cat(rec, k, bn)
                    if cat is not None:
                        shown_ok = (shown_ok is not False) and FOLD[cat][si] * (RAISE_FOLD if raise_ else 1.0) >= 0.5
                if shown_ok is False:
                    continue
                cand = {"street": st, "size": label, "frac": round(frac, 3), "raise": raise_,
                        "fold_pct": round(100 * f_all, 1), "ev_bb": round(diff, 2), "eq_called": round(eq_c, 3),
                        "n_opp": len(keys), "shown_check": shown_ok,
                        "ev_vs_actual": None if realized is None else round(ev_alt - realized, 2)}
                spots.append({"street": st, "frac": cand["frac"], "raise": raise_, "pot": round(T, 1),
                              "fold_pct": cand["fold_pct"], "ev_bb": cand["ev_bb"], "ev_vs_actual": cand["ev_vs_actual"]})
                if best is None or cand["ev_bb"] > best["ev_bb"]:
                    best = cand
                if cand["ev_bb"] >= MISS_MIN_BB and cand["fold_pct"] >= 100 * MISS_MIN_FOLD and (
                        shown is None or cand["ev_bb"] > shown["ev_bb"]):
                    shown = cand
        if spots:
            out["fe_spots"] = spots      # 기준 미달이어도 기록 (분석용): 스트리트·크기별 접을 확률과 평균 차이
        if best is not None:
            out["miss_best"] = best
        if shown is not None:
            out["miss"] = shown
            who = "상대가" if shown["n_opp"] == 1 else "상대들이 모두"
            act = "레이즈" if shown["raise"] else "벳"
            lines.append(f"{STREET_KO[shown['street']]}에 {shown['size']} {act} 했으면 {who} 접을 확률 약 "
                         f"{_pct(shown['fold_pct'] / 100)} → 평균 +{_bb(shown['ev_bb'])} (추정)")

    # (d) 크게 잃었는데 기록이 일부 빠짐 (금액을 믿을 수 없음) → 지어내지 않고 그렇다고만
    if not lines and lost and -delta >= BIG_LOSS_BB and not reliable:
        lines.append(f"{_bb(-delta)} 잃은 판 — 기록이 일부 빠져 금액 복기는 생략")
    out["lines"] = lines[:3]
    return out


def log_fields(rv):
    """복기 결과 → 핸드 기록에 넣을 dict (좌석 키·포지션·숫자·줄만, 닉네임 없음)."""
    if not isinstance(rv, dict):
        return None
    keep = ("v", "result_bb", "result_src", "invested_bb", "reliable", "lost_to", "paid_beaters_bb", "biggest",
            "miss", "miss_best", "fe_spots", "lines", "error")
    return {k: rv[k] for k in keep if k in rv and rv[k] is not None}


# --- 오프라인 실행 --------------------------------------------------------------------------
def _load(path):
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                try:
                    x = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(x, dict):
                    out.append(x)
    except OSError:
        pass
    return out


def run_dir(hands_dir, iters=EQ_ITERS):
    """hands 폴더의 YYYY-MM-DD.jsonl 전부 → [(기록, 복기)] (결정 기록이 있으면 그 핸드의 앤티·스택을 씀). 파일은 읽기만."""
    d = Path(hands_dir)
    ctx = {}
    for f in sorted(d.glob("decisions_*.jsonl")):
        for x in _load(f):
            if x.get("type") == "decision" and x.get("hand_id") and x["hand_id"] not in ctx:
                ctx[x["hand_id"]] = x
    out = []
    for f in sorted(d.glob("20*.jsonl")):
        for rec in _load(f):
            c = ctx.get(rec.get("id")) or {}
            ante = _num(c.get("ante")) or 0.0
            rv = review_hand(rec, ante_bb=ante, stack_bb=_num(c.get("stack_bb")), players=_num(c.get("players")),
                             iters=iters)
            out.append((rec, rv))
    return out


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    argv = sys.argv[1:] if argv is None else argv
    d = Path(argv[0]) if argv else Path(__file__).resolve().parent / "dist" / "hands"
    res = run_dir(d)
    losers = sorted((x for x in res if (x[1].get("result_bb") or 0) < 0), key=lambda x: x[1]["result_bb"])
    print(f"hands {len(res)} · reviewed with lines {sum(1 for _, rv in res if rv.get('lines'))}")
    for rec, rv in losers[:5]:
        print(f"\n{rec.get('id')}  hero {' '.join(rec.get('hero_cards') or [])}  board {' '.join(rec.get('board') or [])}"
              f"  result {rv['result_bb']:+.1f}BB ({rv['result_src']}{'' if rv.get('reliable') else ', low'})")
        for ln in rv.get("lines") or ["(no lines)"]:
            print("   ", ln)
    paid = sum(rv.get("paid_beaters_bb") or 0 for _, rv in res)
    lost_sd = [rv for _, rv in res if rv.get("lost_to")]
    fold_better = [rv for _, rv in res if (rv.get("biggest") or {}).get("fold_better")]
    misses = [rv for _, rv in res if rv.get("miss")]
    print(f"\nlost at showdown to shown cards: {len(lost_sd)} hands, chips put in while already behind: {paid:.1f}BB")
    print(f"biggest call was a losing call vs the range: {len(fold_better)} hands, "
          f"{sum(rv['biggest']['saved_bb'] for rv in fold_better):.1f}BB would have been saved by folding")
    print(f"bigger bet plausibly better (>= {MISS_MIN_BB}BB): {len(misses)} hands, "
          f"+{sum(rv['miss']['ev_bb'] for rv in misses):.1f}BB estimated")


if __name__ == "__main__":
    main()
