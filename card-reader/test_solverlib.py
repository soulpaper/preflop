"""solverlib 테스트 — python test_solverlib.py
합성 덤프로 압축/조회 경로를 확인하고, gto_lib/ 에 실제 솔브가 있으면 그것으로 상식 검사를 한다 (없으면 SKIP)."""
import gzip
import itertools
import json
import os
import random
import sys
import tempfile

import preflop as pf
import solverlib as sl

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

results = []


def test(name, ok, info=""):
    results.append(("PASS" if ok else "FAIL", name, info))


def skip(name, info=""):
    results.append(("SKIP", name, info))


# --- 레인지 변환 -------------------------------------------------------------------------
for spot in sl.SPOTS:
    r = sl.spot_ranges(spot, 40)
    for side in ("ip", "oop"):
        txt = sl.to_ts_range(r[side])
        back = sl.parse_ts_range(txt)
        test(f"레인지 왕복 {spot} {side}", back == r[side], f"{len(back)} 핸드")
w = {"AA": 1.0, "AKs": 0.5, "72o": 0.125, "T9s": 0.333}
test("가중치 왕복 (소수 3자리)", sl.parse_ts_range(sl.to_ts_range(w)) == w, sl.to_ts_range(w))
test("TexasSolver 파일 형식 파싱", sl.parse_ts_range("AA,KK:0.5,AKs:0.25") == {"AA": 1.0, "KK": 0.5, "AKs": 0.25})
test("축약 표기 (QQ+, A5s-A2s) 도 펼친다", set(sl.parse_ts_range("QQ+,A5s-A2s:0.5")) == {"AA", "KK", "QQ", "A5s", "A4s", "A3s", "A2s"})
r40 = sl.spot_ranges("BTN_BB", 40)
test("BTN_BB: IP=BTN 오픈 레인지 = preflop.ranges('open')",
     set(r40["ip"]) == set(pf.ranges("open", "BTN", None, 9, 40, ante=True)[0][1]))
test("BTN_BB: OOP=BB 콜 부분 (3벳 제외)",
     set(r40["oop"]) == set(next(h for _n, h, c in pf.ranges("vsopen", "BB", "BTN", 9, 40, ante=True) if c == "call")))
info = sl.spot_info("BTN_BB", 40)
test("팟: 2.2×2 + 0.5 + 앤티 1.125 = 6.025BB, 남은 스택 37.8", abs(info["pot"] - 6.025) < 1e-9 and abs(info["behind"] - 37.8) < 1e-9)
test("SB_BB: SB 가 OOP, BB 가 IP", sl.spot_info("SB_BB", 40)["oop"] == "SB" and sl.spot_info("SB_BB", 40)["ip"] == "BB")
inp = sl.solver_input("BTN_BB", 40, "AsJh7d", "x.json", 4, r40)
test("솔버 입력: 팟/스택/보드/벳 크기", "set_pot 602.50" in inp and "set_effective_stack 3780.00" in inp
     and "set_board As,Jh,7d" in inp and "set_bet_sizes ip,flop,bet,33,75" in inp and "set_bet_sizes oop,river,allin" in inp)

# --- 플랍 정규화 --------------------------------------------------------------------------
rng = random.Random(3)
ok = True
for _ in range(300):
    b = rng.sample(range(52), 3)
    perm = list(range(4))
    rng.shuffle(perm)
    b2 = [(c >> 2) * 4 + perm[c & 3] for c in b]
    rng.shuffle(b2)
    ok &= sl.canon_flop(b)[0] == sl.canon_flop(b2)[0]
test("수트 동형 플랍 300쌍 → 같은 정규 키", ok)
keys = {sl.canon_flop(list(c))[0] for c in itertools.combinations(range(52), 3)}
test("전략적으로 다른 플랍 1755 클래스", len(keys) == 1755, str(len(keys)))
test("SUBSET 200개, 모두 정규 키, 중복 없음", len(sl.SUBSET) == 200 and len(set(sl.SUBSET)) == 200 and set(sl.SUBSET) <= keys)
test("isomorphic: AhKh2c 와 AsKs2d 의 Qh/Qs 매핑이 같다",
     sl.combo_key(*sl.map_hand(["Ah", "Qh"], "AhKh2c", "AsKs2h")[0]) == sl.combo_key(*sl.map_hand(["As", "Qs"], "AsKs2d", "AsKs2h")[0]))
ok = True
for _ in range(200):     # 같은 클래스로 매핑하면 랭크는 그대로, 수트는 일관된 순열
    b = rng.sample(range(52), 3)
    tkey = sl.canon_flop(b)[0]
    rest = [c for c in range(52) if c not in b]
    h = rng.sample(rest, 2)
    m, _n = sl.map_hand(h, b, tkey)
    tb = sl.parse_board(tkey)
    same_rank = [x >> 2 for x in m] == [x >> 2 for x in h]
    same_suit_rel = ((h[0] & 3) == (h[1] & 3)) == ((m[0] & 3) == (m[1] & 3))
    flush_rel = all(sum(1 for c in b if c & 3 == x & 3) == sum(1 for c in tb if c & 3 == y & 3) for x, y in zip(h, m))
    ok &= same_rank and same_suit_rel and flush_rel and not (set(m) & set(tb))
test("같은 클래스 매핑: 랭크 유지 + 수트 순열 일관 (플러시 드로우/블로커 보존)", ok)
m, _n = sl.map_hand(["Ah", "5h"], "Kh9h2c", "QsTs4h")   # 넛 플러시 드로우 → 넛 플러시 드로우
test("다른 투톤 플랍: 플러시 드로우 무늬가 대상 플러시 무늬로", all((c & 3) == 0 for c in m), sl.combo_str(*m))
m, _n = sl.map_hand(["Kd", "Qc"], "Kh9s2c", "As8d3h")   # 탑페어 → 탑페어
test("랭크 보간: 탑페어는 탑페어로", (max(m) >> 2) == 12, sl.combo_str(*m))
m, _n = sl.map_hand(["Ac", "Ad"], "Kh9s2c", "Qs8d3h")    # 오버페어 → 오버페어
test("랭크 보간: 오버페어는 오버페어로", (m[0] >> 2) == (m[1] >> 2) and (m[0] >> 2) > 10, sl.combo_str(*m))
test("거리: 같은 클래스 0, 페어 여부 다르면 금지",
     sl.flop_distance("AsKd2c", "AhKs2d") == 0 and sl.flop_distance("AsAd2c", "AhKs2d") >= 900)
test("가장 가까운 플랍: K72r → K63r 보다 A72r? (하이카드 우선)",
     sl.nearest_flop(sl.parse_board("Kd7h2c"), ["Ks6h3d", "AsJh7d", "Qs8h4d"])[0] == "Ks6h3d")


# --- 합성 덤프로 압축 / 조회 -----------------------------------------------------------------
def fake_dump(board, ip_hands, oop_hands, f):
    b = sl.parse_board(board)

    def combos(hands):
        out = []
        for h in hands:
            for c in sl.hand_combos(h):
                if not set(c) & set(b):
                    out.append(sl.combo_str(*c))
        return out
    oc, ic = combos(oop_hands), combos(ip_hands)

    def node(player, acts, cs, children=None):
        st = {k: f(k, acts) for k in cs}
        return {"node_type": "action_node", "player": player, "actions": acts,
                "strategy": {"actions": acts, "strategy": st}, "childrens": children or {}}
    P, B = 602.5, 3780.0
    b1, b2 = round(P * 0.33), round(P * 0.75)
    vs_ip = node(1, ["CALL", f"RAISE {b1 + 500}", "FOLD"], oc)
    chk = node(0, ["CHECK", f"BET {b1}", f"BET {b2}"], ic,
               {f"BET {b1}": vs_ip, f"BET {b2}": node(1, ["CALL", "RAISE 1205", "FOLD"], oc)})
    root = node(1, ["CHECK", f"BET {b1}", f"BET {b2}"], oc,
                {"CHECK": chk, f"BET {b1}": node(0, ["CALL", "RAISE 699", "FOLD"], ic),
                 f"BET {b2}": node(0, ["CALL", "RAISE 1205", "FOLD"], ic)})
    return root


def strat(k, acts):
    x = (hash(k) % 97) / 96.0
    v = [x, (1 - x) * 0.25, (1 - x) * 0.75][:len(acts)]
    return v


tmp = tempfile.mkdtemp(prefix="gtolib_test_")
os.environ["GTO_LIB_DIR"] = tmp
REAL_LIB_DIR, sl.LIB_DIR = sl.LIB_DIR, tmp   # 합성 테스트 동안 실제 gto_lib 를 가린다
dump = fake_dump("AsJh7d", ["AA", "KQs", "T9s"], ["A5s", "QJo", "77"], strat)
c = sl.compact_dump(dump, "BTN_BB", 40, "AsJh7d")
test("압축: 노드 이름", {"oop_first", "ip_vs_check", "oop_vs_bet:bet33", "oop_vs_bet:bet75", "ip_vs_bet:bet33",
                     "ip_vs_bet:bet75"} <= set(c["nodes"]), ", ".join(sorted(c["nodes"])))
lib = {"version": 1, "spot": "BTN_BB", "stack": 40, "pot": 6.025, "behind": 37.8, "range_hash": "x",
       "flops": {"AsJh7d": {"nodes": c["nodes"], "exploit": 0.5, "rh": "x"}}}
with gzip.open(os.path.join(tmp, "BTN_BB_40bb.json.gz"), "wt", encoding="utf-8") as fh:
    json.dump(lib, fh)
sl.available(refresh=True)
r = sl.lookup("BTN", "BB", 40, ["Ac", "Ad"], "AsJh7d", "ip_vs_check")
exp = strat(sl.combo_str(sl.card("Ac"), sl.card("Ad")), ["CHECK", "BET", "BET"])
test("조회: 정확한 플랍/콤보, 빈도 합 1", r and abs(sum(r["actions"].values()) - 1) < 1e-6 and r["exact_flop"],
     str(r and {k: round(v, 3) for k, v in r["actions"].items()}))
test("조회: 64단계 양자화 오차 < 2%", r and abs(r["actions"]["check"] - exp[0]) < 0.02, f"{exp[0]:.3f}")
r2 = sl.lookup("BTN", "BB", 40, ["Ah", "Ac"], "AdJs7c", "ip_vs_check")
test("동형 보드 + 동형 콤보 → 같은 답", r2 and r2["actions"] == r["actions"] and r2["exact_flop"])
r3 = sl.lookup("BB", "BTN", 40, ["5d", "Ad"], "AsJh7d", ("oop_vs_bet", 0.75))
test("oop_vs_bet(0.75) → bet75 노드", r3 and r3["node"] == "oop_vs_bet:bet75", str(r3 and r3["node"]))
r4 = sl.lookup("BB", "BTN", 40, ["5d", "Ad"], "AsJh7d", "oop_vs_bet(40)")
test("oop_vs_bet(40) → 가장 가까운 bet33", r4 and r4["node"] == "oop_vs_bet:bet33")
r5 = sl.lookup("BB", "LJ", 33, ["Qd", "Jc"], "KhTd6c", "oop_first")
test("근사 스팟 (LJ→HJ→… 없으면 BTN_BB) + 근사 플랍 표기", r5 and not r5["exact_spot"] and not r5["exact_flop"]
     and r5["spot"] == "BTN_BB", str(r5 and (r5["notes"], r5["mapped_flop"])))
test("IP 노드를 OOP 가 물으면 None", sl.lookup("BB", "BTN", 40, ["5d", "Ad"], "AsJh7d", "ip_vs_check") is None)
test("멀티 핸드 아닌 위치 쌍 (CO vs BTN) → None", sl.lookup("CO", "BTN", 40, ["5d", "Ad"], "AsJh7d", "ip_vs_check") is None)

ctx = {"hero": ["Ac", "Ad"], "board": ["As", "Jh", "7d"], "street": "flop", "players": 9, "opponents_in_hand": 1,
       "hero_pos": "BTN", "villain_pos": "BB", "situation": "open", "stack_bb": 37.8, "pot": 6.0, "pot_total": 6.0,
       "call": 0, "to_act": True, "can_check": True}
a = sl.gto_advice(ctx)
test("gto_advice: IP 체크 받음 → advisor 형식", a and a["action"] in ("체크", "벳") and a["reason"].startswith("GTO: ")
     and a["source"] == "gto" and len(a["detail"]) <= 3, str(a and (a["action"], a["size_text"], a["reason"])))
a = sl.gto_advice(dict(ctx, hero=["5d", "Ad"], hero_pos="BB", villain_pos="BTN", situation="vsopen", call=2.0,
                       pot=6.0, pot_total=8.0, can_check=False))
test("gto_advice: OOP 벳 받음 (2/6 ≈ 33%) → 폴드/콜/레이즈", a and a["action"] in ("폴드", "콜", "레이즈", "올인")
     and a["gto"]["node"] == "oop_vs_bet:bet33", str(a and (a["action"], a["reason"], a["gto"]["node"])))
test("gto_advice: 멀티웨이 → None", sl.gto_advice(dict(ctx, opponents_in_hand=2)) is None)
test("gto_advice: 3벳 팟(팟 20BB) → None", sl.gto_advice(dict(ctx, pot=20.0, pot_total=20.0)) is None)
test("gto_advice: 프리플랍 → None", sl.gto_advice(dict(ctx, board=[], street="preflop")) is None)
test("gto_advice: 내 차례 아님 → None", sl.gto_advice(dict(ctx, to_act=False)) is None)
test("gto_advice: 포지션 모름 → None", sl.gto_advice(dict(ctx, villain_pos=None)) is None)
test("gto_advice: 턴 (턴 파일 없음) → None", sl.gto_advice(dict(ctx, board=["As", "Jh", "7d", "2c"], street="turn")) is None)
os.environ.pop("GTO_LIB_DIR", None)
sl.LIB_DIR = REAL_LIB_DIR
sl.available(refresh=True)

# --- 실제 솔브 상식 검사 (gto_lib 에 있을 때만) ----------------------------------------------------
avail = sl.available(refresh=True)
real = sl.load_lib(avail["BTN_BB"][40]) if 40 in avail.get("BTN_BB", {}) else None
dry = [k for k in (real or {}).get("flops", {}) if k[0] == "A" and sl.flop_features(k)[4] == "r" and sl.flop_features(k)[3] == 0]
if real and dry:
    k = dry[0]
    bd = sl.parse_board(k)
    nd = real["flops"][k]["nodes"]
    agg = dict(zip(nd["ip_vs_check"]["a"], nd["ip_vs_check"]["agg"]))
    test(f"실제 {k} BTN vs BB 40BB: IP 가 체크 받으면 작게 자주 c-bet (33% > 75%, 33% ≥ 45%)",
         agg.get("bet33", 0) > agg.get("bet75", 0) and agg.get("bet33", 0) >= 0.45, str({a: round(f, 2) for a, f in agg.items()}))
    sums = []
    for nk, n in nd.items():
        for combo in sl.combo_order(bd)[1][:400]:
            v = sl.decode(n, combo, bd)
            if v is not None:
                sums.append(sum(v))
    test("실제 덤프: 디코드한 빈도 합 ≈ 1", sums and all(abs(s - 1) < 1e-6 for s in sums), f"{len(sums)} 콤보")
    # 넛: 보드의 A 와 셋이 되는 AA (BTN 오픈 레인지) 는 벳/레이즈 받아도 폴드하지 않는다
    aces = [c for c in range(48, 52) if c not in bd]
    folds = []
    for nk in [n for n in nd if n.startswith("ip_vs_bet") or n.startswith("ip_vs_raise")]:
        v = sl.decode(nd[nk], (aces[0], aces[1]), bd)
        if v is not None:
            folds.append(dict(zip(nd[nk]["a"], v)).get("fold", 0.0))
    test(f"실제 {k}: 셋 에이스(넛)는 절대 폴드 안 함", folds and max(folds) < 0.01, f"{len(folds)} 노드, 최대 {max(folds or [0]):.3f}")
    r = sl.lookup("BTN", "BB", 40, [sl.cstr(aces[0]), sl.cstr(aces[1])], k, "ip_vs_check")
    test("실제 조회 빈도 합 ≈ 1", r and abs(sum(r["actions"].values()) - 1) < 1e-6, str(r and r["actions"]))
    a = sl.gto_advice(dict(ctx, hero=["Kc", "Qd"], board=[sl.cstr(c) for c in bd], pot=6.0, pot_total=6.0))
    test("실제 gto_advice (KQ, IP 체크 받음)", a is not None, str(a and (a["action"], a["reason"], a["detail"])))
    if sl.turn_path("BTN", "BB", k) or sl.turn_path("BTN_BB", 40, k):
        tcard = next(sl.cstr(c) for c in range(52) if c not in bd and c >> 2 == 0)
        r = sl.lookup("BB", "BTN", 40, ["Kc", "Qd"], k + tcard, "oop_first", line="x_x")
        test("턴 조회 (x_x, OOP 먼저) 빈도 합 ≈ 1", r and r["street"] == "turn" and abs(sum(r["actions"].values()) - 1) < 1e-6,
             str(r and (r["mapped_turn"], {a: round(f, 2) for a, f in r["actions"].items()})))
        r = sl.lookup("BB", "BTN", 40, ["Kc", "Qd"], k + tcard, "oop_first", line="x_bet33_c")
        test("턴: 콜한 OOP 는 동크 없음 → 체크 100%", r and r["actions"] == {"check": 1.0})
        a = sl.gto_advice(dict(ctx, hero=["Kc", "Qd"], board=[sl.cstr(c) for c in bd] + [tcard], street="turn",
                               pot=10.0, pot_total=10.0, stack_bb=33.8))
        test("턴 gto_advice (팟 10BB → 33% 벳-콜 라인 추정)", a is not None and a["gto"]["line"] == "x_bet33_c",
             str(a and (a["action"], a["reason"], a["gto"]["line"])))
else:
    skip("실제 솔브 상식 검사", "gto_lib/BTN_BB_40bb.json.gz 에 A 하이 레인보우 플랍이 아직 없음")

# --- 회귀: 대안 콤보 / 수트 동형 불변 ------------------------------------------------------------
_tb = sl.parse_board("Qs8h4s")
_alts = sl._alt_combos(sl.parse_board("Qs7d"), _tb, "Qs8h4s")   # mapped 가 보드 Qs 와 겹침
test("대안 콤보: 보드와 겹친 카드는 같은 랭크(페어 유지)부터", sl.combo_str(*_alts[0]) == "Qh7d",
     str([sl.combo_str(*a) for a in _alts[:3]]))
_nd = {"a": ["call", "fold"], "s": ""}
_b = sl.parse_board("9s5s3s")
_idx, _lst = sl.combo_order(_b)
_s = ["A"] * len(_lst)   # 전부 콜 0 → 폴드 100%
_s[_idx[sl.combo_key(sl.card("Jh"), sl.card("3h"))]] = sl.ALPH[sl.QMAX]   # Jh3h 만 콜 100%
_nd["s"] = "".join(_s)
_f = [sl.decode_sym(_nd, (sl.card(a), sl.card(b)), _b) for a, b in (("Jh", "3h"), ("Jd", "3d"), ("Jc", "3c"))]
test("decode_sym: 동형 콤보(Jh3h/Jd3d/Jc3c on 9s5s3s)는 같은 빈도 (평균)",
     all(abs(f[0] - 1 / 3) < 1e-9 for f in _f), str(_f))
if avail:
    import random as _random
    _rng = _random.Random(5)
    _bad = _n = 0
    _sp = [("BTN", "BB"), ("BB", "BTN"), ("SB", "BB"), ("BB", "SB"), ("CO", "BB"), ("BB", "CO")]
    _nodes = ["oop_first", "ip_vs_check", ("oop_vs_bet", 75), ("ip_vs_bet", 33), "oop_vs_raise", "ip_vs_raise"]
    for _ in range(400):
        hp, vp = _rng.choice(_sp)
        nd_ = _rng.choice(_nodes)
        cs = _rng.sample(range(52), 5)
        r0 = sl.lookup(hp, vp, 40, cs[:2], cs[2:], nd_)
        if not r0:
            continue
        p = _rng.choice(sl._PERMS)
        f = lambda c: (c >> 2) * 4 + p[c & 3]
        r1 = sl.lookup(hp, vp, 40, [f(c) for c in cs[:2]], [f(c) for c in cs[2:]], nd_)
        _n += 1
        if not r1 or max(abs(r0["actions"].get(a, 0) - r1["actions"].get(a, 0)) for a in set(r0["actions"]) | set(r1["actions"])) > 1e-9:
            _bad += 1
    test("조회: 보드+핸드 무늬를 똑같이 바꿔도 (플랍) 같은 빈도", _n > 50 and _bad == 0, f"{_bad}/{_n} 다름")

# --- 결과 -------------------------------------------------------------------------------
failed = 0
for st, name, info in results:
    failed += st == "FAIL"
    print(f"[{st}] {name}" + (f"  — {info}" if info else ""))
print(f"\n{sum(1 for s, *_ in results if s == 'PASS')}/{len(results)} passed, {failed} failed")
assert failed == 0, f"{failed} failed"
