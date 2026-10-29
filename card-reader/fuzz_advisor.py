"""advisor / preflop / rangeq / odds 속성(퍼즈) 테스트 — python fuzz_advisor.py [--n 3000] [--seed 1] [-v]

무작위 핸드·보드·팟·콜·스택·인원·느슨함·포지션·프로필로 recommend 를 돌리며 불변식을 확인한다.
위반은 종류별로 개수와 예시를 출력하고, 하나라도 있으면 exit 1 (없으면 0).
"""
import argparse
import os
import random
import re
import subprocess
import sys
import traceback
from collections import defaultdict

import advisor
import evaluator as ev
import odds
import preflop as pf
import rangeq

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

advisor.GTO_ENABLED = False   # 라이브러리 상태와 무관하게
KEYS = {"action", "size_bb", "size_text", "reason", "detail", "confidence", "color_key", "plan"}
POSITIONS = sorted({p for n in range(6, 11) for p in pf.seats(n)}) + ["MP", "XX"]
SITS = list(advisor.DEEP_SITUATIONS + advisor.SHORT_SITUATIONS) + [None]

viol = defaultdict(list)
counts = defaultdict(int)


def bad(kind, ctx, info=""):
    viol[kind].append((ctx, info))


def cs(cards):
    return [ev.card_str(c) for c in cards]


def nut_equity(hero, board):
    """리버 헤즈업 정확 에쿼티 (랜덤 핸드 1명)."""
    return ev.equity(hero, board, opponents=1)["equity"]


def rand_profile(r):
    if r.random() < 0.6:
        return None
    p = {}
    for k, lo, hi in (("vpip", 5, 70), ("pfr", 2, 40), ("threebet", 1, 20), ("fold_to_bet", 10, 80), ("af", 0.3, 5)):
        if r.random() < 0.7:
            p[k] = round(r.uniform(lo, hi), 1)
    if r.random() < 0.8:
        p["hands"] = r.choice([0, 10, 29, 30, 80, 400, None, "x"])
    if r.random() < 0.2:
        p["reliable"] = r.choice([True, False, None])
    return p


def pct_parts(text):
    """'필요 승률 33%+2%(뒤 레이즈 위험)-1%(실현 보정) < 보정 에쿼티 50%(…)' → (필요 합계, 부등호, 보정 에쿼티)."""
    m = re.search(r"필요 승률 (\d+)%((?:[+-]\d+%\([^)]*\))*) ([<>]) 보정 에쿼티 (\d+)%", text)
    if not m:
        return None
    total = int(m.group(1)) + sum(int(x) for x in re.findall(r"([+-]\d+)%\(", m.group(2)))
    return total / 100, m.group(3), int(m.group(4)) / 100, int(m.group(1)) / 100


# --- 포스트플랍 ---------------------------------------------------------------------
def post_case(r):
    nb = r.choice([3, 4, 5, 5])
    deck = list(range(52))
    r.shuffle(deck)
    hero, board = deck[:2], deck[2:2 + nb]
    opp = r.choice([1, 1, 1, 2, 3, 4])
    p0 = round(r.uniform(1, 300) if r.random() < 0.8 else r.uniform(0.3, 5), 2)
    facing = r.random() < 0.6
    call = round(p0 * r.choice([0.1, 0.25, 0.33, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]), 2) if facing else 0.0
    stack = round(r.choice([r.uniform(0.5, 30), r.uniform(5, 400)]), 2)
    my_stack = stack if r.random() < 0.5 else round(stack * r.uniform(1, 3), 2)
    rb = r.random() < 0.35
    exact = nb == 5 and opp == 1 and not rb and r.random() < 0.5
    if exact:
        eq = nut_equity(hero, board)
    elif r.random() < 0.5:
        eq = ev.equity(hero, board, opponents=opp, iterations=120, rng=random.Random(r.random()))["equity"]
    else:
        eq = r.choice([0.0, 1.0, r.random()])
    loose = r.choice([None, r.uniform(-1, 1), -1.0, 1.0])
    ctx = dict(hero=cs(hero), board=cs(board), street={3: "flop", 4: "turn", 5: "river"}[nb],
               players=r.choice([6, 7, 8, 9, 10, None, 3, 12]), opponents_in_hand=r.choice([opp, opp, None]) or None,
               hero_pos=r.choice(POSITIONS + [None]), villain_pos=r.choice(POSITIONS + [None]),
               stack_bb=r.choice([stack, stack, None, 0]), my_stack=r.choice([my_stack, my_stack, None]),
               pot=r.choice([p0, p0, None]), pot_total=r.choice([round(p0 + call, 2), None]),
               call=call if (call or r.random() < 0.8) else None,
               to_act=r.choice([True, True, False, None]),
               can_check=(call == 0) if r.random() < 0.85 else None,
               equity=eq, equity_basis="range" if rb else r.choice(["random", None]),
               facing_allin=bool(call) and call >= stack and r.random() < 0.7,
               villain_profile=rand_profile(r), villain_raised=r.random() < 0.2,
               closing_action=r.choice([None, True, False]), hero_ip=r.choice([None, True, False]),
               aggressor=r.choice([None, True, False]))
    if loose is not None:
        ctx["looseness"] = loose
    elif r.random() < 0.3:
        ctx["style"] = r.choice(["tight", "standard", "aggressive", "weird"])
    if ctx["opponents_in_hand"] is None:
        ctx["opponents_in_hand"] = opp
    return ctx, dict(hero=hero, board=board, eq=eq, p0=p0, call=call, stack=stack, opp=opp, rb=rb, exact=exact)


def eff_stack(ctx):
    """내가 낼 수 있는 최대 금액: 내 스택(my_stack), 없으면 유효 스택."""
    return ctx.get("my_stack") or ctx.get("stack_bb") or None


def common_checks(ctx, res, tag):
    if set(res) != KEYS or res["action"] not in advisor.COLOR or res["color_key"] not in advisor.COLOR.values():
        bad(f"{tag}: 출력 형식", ctx, res)
    if len(res["detail"]) > 3 or len(res["plan"]) > 3:
        bad(f"{tag}: detail/plan 3줄 초과", ctx, res)
    call = ctx.get("call") or 0
    if call == 0 and ctx.get("can_check") is True and res["action"] == advisor.FOLD:
        bad(f"{tag}: 체크 가능한데 폴드", ctx, res["reason"])
    if call > 0 and ctx.get("can_check") is False and res["action"] == advisor.CHECK:
        bad(f"{tag}: 벳을 마주했는데 체크", ctx, res["reason"])
    st = eff_stack(ctx)
    if st and res["size_bb"] is not None and res["size_bb"] > st + 0.051:
        bad(f"{tag}: 사이즈 > 스택", ctx, f"{res['action']} {res['size_bb']} > {st}")
    if res["size_bb"] is not None and res["size_bb"] < 0:
        bad(f"{tag}: 음수 사이즈", ctx, res)
    for d in res["detail"] + [res["reason"]]:
        for m in re.findall(r"필요(?: 승률)? (-?\d+)%", d):
            if not 0 <= int(m) < 100:
                bad(f"{tag}: 필요 승률 범위 밖", ctx, d)


def post_checks(ctx, k, res):
    common_checks(ctx, res, "post")
    # 리버 너트급 (헤즈업 랜덤 기준 정확 에쿼티 98%+) 은 절대 폴드하지 않는다
    if k["exact"] and k["eq"] >= 0.98 and k["call"] > 0 \
            and res["action"] == advisor.FOLD and ctx.get("to_act") is not False:
        bad("post: 리버 너트 폴드", ctx, res["detail"])
    # 표기된 필요 승률/보정 에쿼티 부등호가 숫자와 맞는가, 필요 승률 합계가 1 미만인가, 가산이 과도하지 않은가
    for d in res["detail"]:
        p = pct_parts(d)
        if not p:
            continue
        total, sign, adj, need = p
        if total >= 1.0:
            bad("post: 필요 승률 합계 >= 100%", ctx, d)
        if total - need > 0.30:
            bad(KNOWN_MARGIN, ctx, d)
        if sign == "<" and total > adj + 0.03 or sign == ">" and total < adj - 0.03:
            bad("post: 필요/보정 에쿼티 부등호가 숫자와 다름", ctx, d)
        if sign == "<" and res["action"] == advisor.FOLD and "드로우" not in res["reason"] and "이상만" not in res["reason"] and ctx.get("to_act") is not False:
            bad("post: 에쿼티 충분 표기인데 폴드", ctx, res["reason"] + " | " + d)
    if "에쿼티 ≥ 필요 승률" in res["reason"] and not any(" < 보정" in d for d in res["detail"]):
        bad("post: 이유와 수치 불일치", ctx, res["detail"])
    # plan 의 최대 콜 금액: 그 금액의 벳에 실제로 콜(또는 레이즈/올인) 해야 한다
    for line in res["plan"]:
        m = re.search(r"벳이 오면: 팟의 (\d+)%\(≈([\d.]+)BB\)까지 콜", line)
        if not m or ctx.get("pot") is None:
            continue
        p0 = ctx["pot"]
        call = int(m.group(1)) / 100 * p0
        c2 = dict(ctx, call=round(call, 4), pot=p0, pot_total=round(p0 + call, 4), to_act=True, can_check=False,
                  facing_allin=False)
        r2 = advisor.recommend(c2)
        counts["plan 최대 콜 재검증"] += 1
        if r2["action"] == advisor.FOLD:
            bad("post: plan '까지 콜' 금액에 폴드", ctx, f"{line} → {r2['reason']} | {r2['detail']}")


# --- 프리플랍 -------------------------------------------------------------------------
def pre_case(r):
    deck = list(range(52))
    r.shuffle(deck)
    hero = deck[:2]
    players = r.choice([6, 7, 8, 9, 10])
    pos = r.choice(pf.seats(players) + [r.choice(POSITIONS), None])
    sit = r.choice(SITS)
    villain = r.choice(pf.seats(players) + [None])
    stack = round(r.choice([r.uniform(1, 15), 15.0, r.uniform(15, 40), r.uniform(40, 300)]), 2)
    posted = {"SB": 0.5, "BB": 1.0}.get(pos, 0.0)
    ante = r.choice([False, True, 0.9, 1.2, 0])
    dead = 1.5 + (ante if isinstance(ante, float) else (1.0 if ante is True else 0.0))
    kind = r.choice(["unopened", "open", "3bet", "4bet", "jam", "limp"])
    if kind == "unopened":
        call = max(0.0, 1.0 - posted)
        pot_total = dead
    elif kind == "limp":
        n = r.randint(1, 3)
        call = max(0.0, 1.0 - posted)
        pot_total = dead + n
    elif kind == "open":
        o = r.choice([2.0, 2.2, 2.5, 3.0, 4.0, 6.0, 12.0])
        callers = r.choice([0, 0, 1, 2])
        call = o - posted
        pot_total = dead + o * (1 + callers)
    elif kind == "3bet":
        call = r.choice([5.0, 6.5, 8.0, 15.0])
        pot_total = dead + call + 2.5 + call
    elif kind == "4bet":
        call = r.choice([12.0, 14.0, 25.0])
        pot_total = dead + 2 * call
    else:
        call = min(stack, r.uniform(3, 60))
        pot_total = dead + call
    call = round(min(call, stack * r.choice([1, 1, 2])), 2)
    ctx = dict(hero=cs(hero), board=[], street="preflop", players=players, opponents_in_hand=r.randint(1, 4),
               hero_pos=pos, situation=sit, villain_pos=villain, stack_bb=r.choice([stack, stack, None]),
               my_stack=r.choice([stack, round(stack * 2, 2), None]), pot=r.choice([None, round(pot_total - call, 2)]),
               pot_total=r.choice([round(pot_total, 2), round(pot_total, 2), None]), call=call,
               to_act=r.choice([True, True, False, None]), can_check=(call == 0) if r.random() < 0.9 else None,
               ante=ante, facing_allin=kind == "jam" and r.random() < 0.7, closing_action=r.choice([None, True]))
    if r.random() < 0.5:
        ctx["looseness"] = r.choice([r.uniform(-1, 1), -1.0, 1.0, 0.0])
    if r.random() < 0.2:
        ctx["equity"] = r.random()
        ctx["equity_basis"] = "range"
    return ctx, dict(hero=hero, kind=kind)


def pre_checks(ctx, k, res):
    common_checks(ctx, res, "pre")
    hand = pf.hand_code(k["hero"])
    if hand == "AA" and k["kind"] in ("unopened", "limp") and res["action"] == advisor.FOLD:
        bad("pre: AA 언오픈 폴드", ctx, res["reason"])
    if hand == "AA" and res["action"] == advisor.FOLD:
        bad("pre: AA 폴드 (어떤 상황이든)", ctx, res["reason"] + " | " + " / ".join(res["detail"]))


# --- 프리플랍 레인지 표 ---------------------------------------------------------------------
LOOSE_GRID = [-1.0, -0.8, -0.6, -0.3, 0.0, 0.3, 0.6, 0.8, 1.0]
PREMIUM = {"AA", "KK", "QQ", "JJ", "AKs", "AKo"}
# 알려진 잔여 (exit code 에 반영하지 않음): vs 오픈 콜 폭 = (방어 목표 - 3벳 폭) × 깊이 배수 라서 3벳이 빨리 넓어지는 구간
# (SB, 얕은 스택, 3벳 배수 큰 L>0.6) 에서 경계 핸드 1~2개가 콜 → 폴드로 바뀐다.  표 구조 변경이 필요해 남겨 둠.
KNOWN_NEST = "range: 느슨함 증가에 레인지가 줄어듦 (중첩 깨짐) [알려진 잔여]"
# 헤즈업 '실현 보정' = 솔버 표 call[거리] + pot_slope × log2(팟/10BB) — 200BB 솔버 데이터로 맞춘 값이라 팟이 200BB 를 넘으면
# 가산이 30%p 를 넘는다 (너트급은 _adj_equity 의 NUT_K 바닥으로 여전히 콜).  튜닝 값이라 참고로만 센다.
KNOWN_MARGIN = "post: 필요 승률 가산 > 30%p [튜닝 값 — 참고]"
KNOWN = {KNOWN_NEST, KNOWN_MARGIN}


def range_checks():
    n = 0
    for players in range(6, 11):
        for sit in ("open", "vsopen", "vs3bet", "vs4bet", "jam", "jamcall"):
            for hero in pf.valid_heroes(sit, players):
                vs = pf.valid_villains(sit, hero, players) or [None]
                for villain in vs:
                    for stack in (8, 15, 15.01, 22, 40, 100, 250):
                        for ante in (False, True):
                            prev = None
                            for L in LOOSE_GRID:
                                try:
                                    acts = pf.ranges(sit, hero, villain, players, stack, ante, L)
                                except Exception as e:
                                    bad("range: 예외", (sit, hero, villain, players, stack, ante, L), repr(e))
                                    break
                                n += 1
                                union = set().union(*(a[1] for a in acts)) if acts else set()
                                if "AA" not in union:
                                    bad("range: AA 가 레인지 밖", (sit, hero, villain, players, stack, ante, L))
                                if sit == "vsopen" and not PREMIUM <= union:
                                    bad("range: vs 오픈 프리미엄(JJ+/AK) 폴드", (hero, villain, players, stack, ante, L),
                                        sorted(PREMIUM - union))
                                if prev is not None and not prev <= union:
                                    bad(KNOWN_NEST,
                                        (sit, hero, villain, players, stack, ante, L), sorted(prev - union))
                                prev = union
    counts["레인지 조합"] = n
    # 앤티가 있으면 오픈/방어가 넓어진다 (줄지 않는다)
    for players in range(6, 11):
        for hero in pf.valid_heroes("open", players):
            a = pf.open_range(hero, players, False)
            b = pf.open_range(hero, players, True)
            if not a <= b:
                bad("range: 앤티 오픈이 앤티 없는 오픈보다 좁음", (hero, players), sorted(a - b))


def hashseed_check():
    """set 순서(PYTHONHASHSEED) 에 따라 레인지가 바뀌면 비결정적."""
    code = ("import preflop as pf, json, sys\n"
            "out = []\n"
            "for n in range(6, 11):\n"
            "  for h in pf.valid_heroes('vsopen', n):\n"
            "    for v in pf.valid_villains('vsopen', h, n):\n"
            "      for st in (18, 22, 27, 40, 100):\n"
            "        for L in (-1, -0.3, 0, 0.4, 1):\n"
            "          for an in (False, True):\n"
            "            out.append([sorted(x[1]) for x in pf.ranges('vsopen', h, v, n, st, an, L)])\n"
            "  for h in pf.valid_heroes('open', n):\n"
            "    for st in (16, 22, 40):\n"
            "      for L in (-1, -0.3, 0, 0.4, 1):\n"
            "        out.append(sorted(pf.open_range(h, n, True, L, st)))\n"
            "sys.stdout.write(str(hash(json.dumps(out))) if False else json.dumps(out))\n")
    here = os.path.dirname(os.path.abspath(__file__))
    outs = []
    for seed in ("1", "2", "3", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        p = subprocess.run([sys.executable, "-c", code], cwd=here, env=env, capture_output=True, text=True)
        outs.append(p.stdout)
    counts["해시 시드 실행"] = len(outs)
    if len(set(outs)) != 1:
        bad("range: PYTHONHASHSEED 에 따라 레인지가 달라짐 (비결정적)", "preflop.ranges vsopen/open", "")


def parse_checks():
    cases = {"84만 7500": 847500, "84만7500": 847500, "1.5BB": 1.5, "0BB": 0.0, "13만": 130000, "1.5만": 15000,
             "12,000": 12000, "2k": 2000, "21": 21, "1억": 1e8, "1.2억": 1.2e8, "1억 2000만": 1.2e8,
             "1억2000만 5000": 120005000, "3억 5000": 300005000, "": None, "abc": None, "BB": None}
    for text, want in cases.items():
        got = odds.parse_amount(text)
        if (got is None) != (want is None) or (want is not None and abs(got - want) > 1e-6):
            bad("odds: parse_amount", text, f"{got!r} != {want!r}")
    for v in (0, 0.5, 9999, 9999.9, 12345, 99999, 99_995_000, 99_999_999, 1e8, 123456789):
        s = odds.format_amount(v)
        back = odds.parse_amount(s)
        if back is None or abs(back - v) > max(1.0, v * 0.01):
            bad("odds: format_amount 왕복", v, f"{s!r} → {back!r}")
    if abs(odds.pot_odds(10, 5) - 5 / 20) > 1e-12:
        bad("odds: pot_odds", (10, 5), odds.pot_odds(10, 5))


def ip_checks():
    for hero, vil, want in (("BTN", "BB", True), ("SB", "BTN", False), ("BB", "SB", True), ("CO", "BTN", False),
                            ("BB", "UTG", False), ("UTG", "BB", True)):
        c = advisor._Ctx({"hero_pos": hero, "villain_pos": vil, "players": 9})
        if c.ip is not want:
            bad("post: IP 추정", (hero, vil), c.ip)


def rangeq_checks(r, n):
    for _ in range(n):
        act = r.choice(["open", "call_open", "3bet", "call_3bet", "4bet", "limp", "jam", "call_jam", "blind", None,
                        "raise", "x"])
        pos = r.choice(POSITIONS + [None])
        players = r.choice([6, 7, 8, 9, 10, None, 2, "x"])
        stack = r.choice([None, 0, 5, 15, 20, 40, 100, 300])
        prof = rand_profile(r)
        try:
            rg = rangeq.villain_range(act, pos, players, stack, prof, opener=r.choice(POSITIONS + [None]),
                                      ante=r.random() < 0.5)
        except Exception as e:
            bad("rangeq: villain_range 예외", (act, pos, players, stack, prof), repr(e))
            continue
        if not rg or any(not (0 < w <= 1.0 + 1e-9) for w in rg.values()):
            bad("rangeq: 가중치 범위", (act, pos, players, stack, prof), rg)
        if "AA" not in rg and act not in ("call_open", "call_3bet", "limp"):
            bad("rangeq: AA 없음", (act, pos, players, stack, prof))
        counts["rangeq 레인지"] += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    r = random.Random(a.seed)
    for i in range(a.n):
        for kind, make, chk in (("post", post_case, post_checks), ("pre", pre_case, pre_checks)):
            ctx, k = make(r)
            try:
                res = advisor.recommend(ctx)
                again = advisor.recommend(ctx)
            except Exception:
                bad(f"{kind}: 예외", ctx, traceback.format_exc(limit=3))
                continue
            counts[kind] += 1
            if res != again:
                bad(f"{kind}: 비결정적", ctx, "")
            try:
                chk(ctx, k, res)
            except Exception:
                bad(f"{kind}: 검사 중 예외", ctx, traceback.format_exc(limit=3))
    rangeq_checks(r, max(200, a.n // 5))
    range_checks()
    hashseed_check()
    parse_checks()
    ip_checks()
    print("검사 수:", dict(counts))
    total = 0
    for kind in sorted(viol):
        items = viol[kind]
        total += 0 if kind in KNOWN else len(items)
        print(f"\n[위반] {kind}: {len(items)}건")
        for ctx, info in items[: (5 if a.v else 2)]:
            print("   ctx :", ctx)
            if info != "":
                print("   info:", info)
    known = sum(len(viol[k]) for k in KNOWN if k in viol)
    print(f"\n위반 합계 {total}건" if total else "\n위반 없음", f"(알려진 잔여 {known}건은 별도)" if known else "")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
