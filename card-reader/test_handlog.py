"""handlog.Tracker / profiles.Store 단위 테스트 (가짜 tablestate 스냅샷 순서). 실행: python test_handlog.py"""
import json
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

import handlog
import profiles

# 6인 테이블: 시계 방향 (내 왼쪽부터), 나는 마지막
KEYS = ["seat_BL", "seat_UL", "seat_TC", "seat_UR", "seat_BR", "hero"]


def positions(dealer):
    """딜러 키 → {key: 포지션} (6명)."""
    i = KEYS.index(dealer)
    after = KEYS[i + 1:] + KEYS[:i + 1]           # SB, BB, UTG, MP, CO, BTN
    names = ["SB", "BB", "UTG", "MP", "CO", "BTN"]
    return dict(zip(after, names))


class Table:
    """좌석 상태를 들고 있다가 tablestate.read() 모양의 dict 를 만들어 Tracker 에 넣는다."""

    def __init__(self, tracker, dealer="seat_UR", hero=("Ah", "Kd"), names=None, keys=KEYS):
        self.tr, self.keys = tracker, keys
        self.t = 1000.0
        self.out = []
        self.names = names if names is not None else {k: (f"P_{k}", False) for k in keys}
        self.new_hand(dealer, hero)

    def new_hand(self, dealer, hero=("Ah", "Kd")):
        self.dealer = dealer
        self.pos = positions(dealer)
        self.hero = list(hero)
        self.board = []
        self.folded, self.allin = set(), set()
        self.bubble = {}
        sb = next(k for k, p in self.pos.items() if p == "SB")
        bb = next(k for k, p in self.pos.items() if p == "BB")
        self.bets = {sb: 0.5, bb: 1.0}

    def key(self, pos):
        return next(k for k, p in self.pos.items() if p == pos)

    def snap(self):
        seats = []
        for k in self.keys:
            folded = k in self.folded
            seats.append({"key": k, "slot": k.replace("seat_", "") if k != "hero" else "HERO",
                          "pos": self.pos.get(k), "occupied": True, "dealt": True,
                          "in_hand": not folded, "folded": folded, "allin": k in self.allin,
                          "bet": self.bets.get(k), "stack": 0.0 if k in self.allin else 50.0,
                          "bubble": self.bubble.get(k)})
        live = [s for s in seats if s["in_hand"]]
        return {"players": len(seats), "dealt": len(seats), "in_hand": len(live),
                "opponents_in_hand": sum(1 for s in live if s["key"] != "hero"),
                "hero_folded": "hero" in self.folded, "hero_in_hand": "hero" not in self.folded,
                "dealer": self.dealer, "hero_pos": self.pos.get("hero"), "pos_players": 6,
                "blind_seats": {"sb": self.key("SB"), "bb": self.key("BB")}, "seats": seats,
                "to_act": False, "preaction": False, "call": None, "can_check": False, "can_raise": False,
                "street": None, "facing_allin": False, "preflop": None, "notes": []}

    def step(self, bets=None, fold=(), bubble=None, allin=(), board=None, dt=1.0, collect=False):
        """한 프레임: 바뀐 것만 넣음. collect=True → 바닥 벳이 팟으로 모임 (스트리트 끝)."""
        if collect:
            self.bets = {}
        for k, v in (bets or {}).items():
            self.bets[self.k(k)] = v
        for k in fold:
            self.folded.add(self.k(k))
        for k in allin:
            self.allin.add(self.k(k))
        if bubble is not None:
            self.bubble = {self.k(k): v for k, v in bubble.items()}
        if board is not None:
            self.board = list(board)
        self.t += dt
        hero = ["back", "back"] if "hero" in self.folded else self.hero
        recs = self.tr.update(self.snap(), hero, self.board, self.names, self.t)
        self.out += recs
        return recs

    def k(self, k):
        return self.key(k) if k in ("SB", "BB", "UTG", "MP", "CO", "BTN") else k


def acts(rec, street):
    return [(a["seat"], a["act"]) for a in rec["actions"].get(street, [])]


def st(rec, key):
    return rec["seats"][key]["stats"]


def finish(table):
    """다음 핸드 시작(딜러 이동)으로 지금 핸드를 닫고 기록 반환."""
    i = KEYS.index(table.dealer)
    table.new_hand(KEYS[(i + 1) % len(KEYS)], ("2c", "7d"))
    table.step()
    recs = table.step()
    assert len(table.out) >= 1, "hand not finished"
    return table.out[-1]


# --- 시나리오 ---------------------------------------------------------------------------------------
def test_unopened_fold_around():
    tb = Table(handlog.Tracker("s1"))   # 딜러 UR → SB=BR, BB=hero, UTG=BL, MP=UL, CO=TC, BTN=UR
    tb.step()
    tb.step(fold=["UTG", "MP"], bubble={"UTG": "fold", "MP": "fold"})
    tb.step(fold=["CO", "BTN"])
    tb.step(fold=["SB"])
    rec = finish(tb)
    assert rec["confidence"] == "ok", rec["issues"]
    assert acts(rec, "preflop") == [("seat_BR", "post"), ("hero", "post"), ("seat_BL", "fold"), ("seat_UL", "fold"),
                                     ("seat_TC", "fold"), ("seat_UR", "fold"), ("seat_BR", "fold")], acts(rec, "preflop")
    for k in KEYS:
        assert st(rec, k)["vpip"] == 0 and st(rec, k)["pfr"] == 0, (k, st(rec, k))
    assert rec["showdown"] is None and rec["streets"] == "preflop"
    assert rec["seats"]["hero"]["pos"] == "BB" and rec["hero_cards"] == ["Ah", "Kd"]


def test_open_call_flop_bet_fold():
    tb = Table(handlog.Tracker("s2"))
    tb.step()
    tb.step(bets={"UTG": 2.5}, bubble={"UTG": "raise"})                    # UTG 오픈
    tb.step(fold=["MP", "CO"])
    tb.step(bets={"BTN": 2.5}, bubble={"BTN": "call"})                     # BTN 콜
    tb.step(fold=["SB"])
    tb.step(bets={"BB": 2.5}, bubble={"BB": "call"})                       # BB 콜
    tb.step(board=["Ah", "7c", "2d"], collect=True, bubble={})              # 플랍
    tb.step(bets={"UTG": 3.0}, bubble={"UTG": "raise"})                    # BB 체크(안 보임) → UTG 벳
    tb.step(fold=["BTN"], bubble={"BTN": "fold"})
    tb.step(fold=["BB"])
    rec = finish(tb)
    assert rec["confidence"] == "ok", rec["issues"]
    utg, btn, bb = "seat_BL", "seat_UR", "hero"
    assert ("seat_BL", "raise") in acts(rec, "preflop")
    assert acts(rec, "flop") == [(bb, "check"), (utg, "bet"), (btn, "fold"), (bb, "fold")], acts(rec, "flop")
    assert st(rec, utg)["vpip"] == 1 and st(rec, utg)["pfr"] == 1 and st(rec, utg)["agg"] == 1
    assert st(rec, btn)["vpip"] == 1 and st(rec, btn)["pfr"] == 0
    assert st(rec, bb)["vpip"] == 1, "BB 콜은 VPIP"
    for k in (btn, bb):
        assert st(rec, k)["faced_bet"] == 1 and st(rec, k)["fold_to_bet"] == 1, (k, st(rec, k))
        assert st(rec, k)["saw_flop"] == 1 and st(rec, k)["showdown"] == 0
    assert st(rec, "seat_UL")["threebet_opp"] == 1 and st(rec, "seat_UL")["threebet"] == 0


def test_3bet_pot():
    tb = Table(handlog.Tracker("s3"))
    tb.step()
    tb.step(bets={"UTG": 3.0}, bubble={"UTG": "raise"})
    tb.step(fold=["MP"])
    tb.step(bets={"CO": 9.0}, bubble={"CO": "raise"})                      # CO 3벳
    tb.step(fold=["BTN", "SB", "BB"])
    tb.step(bets={"UTG": 9.0}, bubble={"UTG": "call"})                     # UTG 콜
    tb.step(board=["Kc", "Qd", "3s"], collect=True, bubble={})
    tb.step(bets={"CO": 6.0}, bubble={"CO": "raise"})                      # UTG 체크(추정) → CO c-bet
    tb.step(bets={"UTG": 6.0}, bubble={"UTG": "call"})
    live = tb.tr.live()                                                     # handread 입력 모양
    assert live["seat_BL"]["preflop"] == ["open", "call_3bet"] and live["seat_TC"]["preflop"] == ["3bet"], live
    assert live["seat_TC"]["flop"] == ["bet"] and live["seat_BL"]["flop"] == ["call"], live
    assert live["hero"]["preflop"] == ["blind", "fold"] and live["seat_TC"]["opener"] == "UTG"
    tb.step(board=["Kc", "Qd", "3s", "9h"], collect=True, bubble={})
    tb.step()
    tb.step(board=["Kc", "Qd", "3s", "9h", "2c"])
    tb.step()
    rec = finish(tb)
    assert rec["confidence"] == "ok", rec["issues"]
    utg, co = "seat_BL", "seat_TC"
    assert acts(rec, "preflop")[2:] == [(utg, "raise"), ("seat_UL", "fold"), (co, "raise"), ("seat_UR", "fold"),
                                        ("seat_BR", "fold"), ("hero", "fold"), (utg, "call")], acts(rec, "preflop")
    assert st(rec, co)["threebet"] == 1 and st(rec, co)["threebet_opp"] == 1 and st(rec, co)["pfr"] == 1
    assert st(rec, utg)["threebet_opp"] == 0 and st(rec, utg)["pfr"] == 1
    assert st(rec, "seat_UR")["threebet_opp"] == 0, "BTN 은 3벳 뒤에 행동 → 3벳 기회 아님"
    assert acts(rec, "flop") == [(utg, "check"), (co, "bet"), (utg, "call")], acts(rec, "flop")
    assert acts(rec, "turn") == [(utg, "check"), (co, "check")] and acts(rec, "river") == [(utg, "check"), (co, "check")]
    assert st(rec, utg)["faced_bet"] == 1 and st(rec, utg)["fold_to_bet"] == 0 and st(rec, utg)["calls"] == 1
    assert rec["showdown"] and set(rec["showdown"]["seats"]) == {utg, co}
    assert st(rec, utg)["showdown"] == 1 and st(rec, co)["showdown"] == 1


def test_multiway_limp():
    tb = Table(handlog.Tracker("s4"))
    tb.step()
    tb.step(bets={"UTG": 1.0, "MP": 1.0}, bubble={"UTG": "call", "MP": "call"})
    tb.step(fold=["CO"], bets={"BTN": 1.0}, bubble={"BTN": "call"})
    tb.step(bets={"SB": 1.0}, bubble={"SB": "call"})
    tb.step(board=["5h", "6h", "Jc"], collect=True, bubble={})
    tb.step()
    tb.step(board=["5h", "6h", "Jc", "Td"])
    rec = finish(tb)
    assert rec["confidence"] == "ok", rec["issues"]
    pre = acts(rec, "preflop")
    assert ("hero", "check") in pre, pre
    for k in ("seat_BL", "seat_UL", "seat_UR", "seat_BR"):
        assert st(rec, k)["vpip"] == 1 and st(rec, k)["pfr"] == 0, k
    assert st(rec, "hero")["vpip"] == 0, "BB 체크는 VPIP 아님"
    assert [a for _, a in acts(rec, "flop")] == ["check"] * 5, acts(rec, "flop")
    assert [k for k, _ in acts(rec, "flop")] == ["seat_BR", "hero", "seat_BL", "seat_UL", "seat_UR"]
    assert rec["streets"] == "turn" and rec["showdown"] is None


def test_allin_preflop():
    tb = Table(handlog.Tracker("s5"))
    tb.step()
    tb.step(bets={"UTG": 50.0}, allin=["UTG"], bubble={"UTG": "allin"})
    tb.step(fold=["MP", "CO", "BTN", "SB"])
    tb.step(bets={"BB": 50.0}, allin=["BB"], bubble={"BB": "allin"})
    tb.step(board=["2s", "8d", "Kd"], collect=True, bubble={})
    tb.step(board=["2s", "8d", "Kd", "4c"])
    tb.step(board=["2s", "8d", "Kd", "4c", "Ac"])
    rec = finish(tb)
    assert rec["confidence"] == "ok", rec["issues"]
    pre = [a for a in rec["actions"]["preflop"] if a["act"] != "post"]
    assert (pre[0]["seat"], pre[0]["act"], pre[0].get("allin")) == ("seat_BL", "raise", True)
    assert (pre[-1]["seat"], pre[-1]["act"], pre[-1].get("allin")) == ("hero", "call", True)
    assert "flop" not in rec["actions"], "올인 후 런아웃: 포스트플랍 액션 없음"
    assert set(rec["showdown"]["seats"]) == {"seat_BL", "hero"}
    assert st(rec, "hero")["showdown"] == 1 and st(rec, "hero")["saw_flop"] == 1


def test_two_actions_one_gap():
    # (a) 한 프레임 사이에 UTG 오픈 + CO 3벳 + UTG 폴드: UTG 칩(3)이 남아 있고 흑백
    tb = Table(handlog.Tracker("s6"))
    tb.step()
    tb.step(bets={"UTG": 3.0, "CO": 9.0}, fold=["UTG", "MP", "BTN", "SB", "BB"],
            bubble={"UTG": "fold", "MP": "fold", "CO": "raise"})
    rec = finish(tb)
    pre = acts(rec, "preflop")[2:]
    assert pre == [("seat_BL", "raise"), ("seat_UL", "fold"), ("seat_TC", "raise"), ("seat_UR", "fold"),
                   ("seat_BR", "fold"), ("hero", "fold"), ("seat_BL", "fold")], pre
    assert rec["confidence"] == "ok", rec["issues"]
    assert st(rec, "seat_TC")["threebet"] == 1 and st(rec, "seat_BL")["pfr"] == 1

    # (b) 지난 프레임에 UTG 오픈을 봤고, 그 사이 CO 3벳 + UTG 콜 → 순서대로 풀림
    tb = Table(handlog.Tracker("s7"))
    tb.step()
    tb.step(bets={"UTG": 3.0}, bubble={"UTG": "raise"})
    tb.step(fold=["MP", "BTN", "SB", "BB"], bets={"CO": 9.0, "UTG": 9.0}, bubble={"CO": "raise", "UTG": "call"})
    tb.step(board=["Kc", "Qd", "3s"], collect=True, bubble={})
    rec = finish(tb)
    assert rec["confidence"] == "ok", rec["issues"]
    assert acts(rec, "preflop")[-1] == ("seat_BL", "call") and st(rec, "seat_TC")["threebet"] == 1,         (acts(rec, "preflop"), rec["issues"])

    # (c) 오픈 + 3벳 + 콜이 모두 한 프레임 사이 → 누가 먼저 했는지 알 수 없음 → low
    tb = Table(handlog.Tracker("s8"))
    tb.step()
    tb.step(fold=["MP"], bets={"CO": 9.0, "UTG": 9.0}, bubble={"CO": "raise", "UTG": "call"})
    tb.step(fold=["BTN", "SB", "BB"])
    tb.step(board=["Kc", "Qd", "3s"], collect=True, bubble={})
    rec = finish(tb)
    assert rec["confidence"] == "low" and any(i["code"] == "ambiguous_order" for i in rec["issues"])


def test_hero_folded_early():
    tb = Table(handlog.Tracker("s9"), dealer="seat_BR")   # SB=hero? 딜러 BR → SB=hero, BB=BL, UTG=UL ...
    tb.new_hand("seat_TC", ("9c", "4d"))                  # 딜러 TC → SB=UR, BB=BR, UTG=hero
    tb.step()
    tb.step(fold=["hero"], bubble={"hero": "fold"})
    tb.step(bets={"MP": 2.5}, bubble={"MP": "raise"})
    tb.step(fold=["CO", "BTN", "SB"])
    tb.step(bets={"BB": 2.5}, bubble={"BB": "call"})
    tb.step(board=["Th", "Ts", "6c"], collect=True, bubble={})
    tb.step(bets={"BB": 2.0}, bubble={"BB": "raise"})
    tb.step(bets={"MP": 6.0}, bubble={"MP": "raise"})
    tb.step(fold=["BB"])
    mp, bb = tb.key("MP"), tb.key("BB")
    rec = finish(tb)
    assert rec["confidence"] == "ok", rec["issues"]
    assert rec["hero_cards"] == ["9c", "4d"], "폴드 전에 본 내 카드"
    assert acts(rec, "preflop")[2] == ("hero", "fold")
    assert st(rec, "hero")["vpip"] == 0 and rec["seats"]["hero"]["folded"] == "preflop"
    assert acts(rec, "flop") == [(bb, "bet"), (mp, "raise"), (bb, "fold")], acts(rec, "flop")
    assert st(rec, mp)["agg"] == 1 and st(rec, bb)["agg"] == 1 and st(rec, bb)["fold_to_bet"] == 1


def test_new_hand_detection():
    # 1. 내 카드가 바뀜
    tr = handlog.Tracker("s10")
    tb = Table(tr)
    tb.step()
    tb.step(bets={"UTG": 1.0})
    tb.hero = ["Qs", "Qh"]
    tb.step()
    assert not tb.out, "카드 오인식 한 번으로는 새 핸드 아님"
    tb.step()
    assert len(tb.out) == 1 and tb.out[0]["end_reason"] == "hero cards changed"
    # 2. 보드가 비어짐 (2프레임 연속)
    tr = handlog.Tracker("s11")
    tb = Table(tr)
    tb.step()
    tb.step(board=["Ah", "7c", "2d"], collect=True)
    tb.step()
    tb.new_hand("seat_UR")        # 같은 딜러로 보이고 내 카드도 같게 읽혔다고 가정 → 보드만 신호
    tb.step()
    assert not tb.out, "한 프레임만 비면 깜빡임일 수 있음"
    tb.step()
    assert len(tb.out) == 1 and tb.out[0]["end_reason"] == "board cleared"
    assert "flop" not in tb.out[0]["actions"] or all(a["act"] == "check" for a in tb.out[0]["actions"]["flop"]),         "새 블라인드를 지난 핸드 플랍 벳으로 읽으면 안 됨"
    # 3. 딜러 이동 (2프레임 연속)
    tr = handlog.Tracker("s12")
    tb = Table(tr)
    tb.step()
    tb.step(fold=["UTG"])
    tb.dealer = "seat_BR"
    tb.step()
    assert not tb.out
    tb.step()
    assert len(tb.out) == 1 and tb.out[0]["end_reason"] == "dealer moved"
    # 4. 폴드했던 사람들이 다시 카드를 받음 (딜러/내 카드는 그대로 보인다고 가정)
    tr = handlog.Tracker("s13")
    tb = Table(tr)
    tb.step()
    tb.step(fold=["UTG", "MP", "CO"])
    tb.folded = set()
    tb.step()
    assert len(tb.out) == 1 and tb.out[0]["end_reason"] == "folded seats dealt again"
    # 5. 나쁜 입력에도 예외 없음
    tr = handlog.Tracker("s14")
    for bad in (None, {}, {"error": "x"}, {"seats": [None, {"key": "a", "bet": "zz", "dealt": True}]},
                {"seats": "nope"}):
        assert tr.update(bad, None, None, None, 1.0) == []
    assert tr.update({"seats": [{"key": "a", "dealt": True, "bet": "1BB"}, {"key": "b", "dealt": True}]},
                     ["Ah"], ["Xx"], {"a": None}, 2.0) == []


def test_truncated_names_session():
    tmp = Path(tempfile.mkdtemp())
    try:
        store = profiles.Store(tmp / "p.db", hands_dir=tmp / "hands")
        names = {k: (f"P_{k}", False) for k in KEYS}
        names["seat_TC"] = ("User84", True)          # 잘린 닉네임
        tr = handlog.Tracker("sessA", on_hand=store.observe)
        store.new_session("sessA")
        tb = Table(tr, names=names)
        tb.step()
        tb.step(bets={"CO": 2.5}, fold=["UTG", "MP"], bubble={"CO": "raise"})
        tb.step(fold=["BTN", "SB", "BB"])
        finish(tb)
        rec = tb.out[0]
        assert rec["confidence"] == "ok", rec["issues"]
        db = sqlite3.connect(str(tmp / "p.db"))
        rows = {r[0]: r[1:] for r in db.execute("SELECT name, hands, vpip, pfr FROM players")}
        assert "User84" not in rows and not any("User84" in n for n in rows), rows
        assert rows["P_seat_BL"] == (1, 0, 0), rows
        s = store.stats("User84", True, seat="seat_TC", session="sessA")
        assert s["hands"] == 1 and s["vpip"] == 100.0 and s["pfr"] == 100.0 and not s["reliable"], s
        table = store.get_for_table(names, session="sessA")
        assert table["seat_TC"]["hands"] == 1 and table["seat_BL"]["hands"] == 1
        assert store.stats("User84", True, seat="seat_BL", session="sessA")["hands"] == 0, "다른 좌석과 섞이지 않음"
        # JSONL: 잘린 이름은 자리표시, 전체 이름은 그대로
        line = json.loads(next((tmp / "hands").glob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0])
        assert line["seats"]["seat_TC"]["name"] is None and line["seats"]["seat_TC"]["placeholder"] == "seat-local:seat_TC"
        assert line["seats"]["seat_BL"]["name"] == "P_seat_BL"
        assert "User84" not in json.dumps(line, ensure_ascii=False)
        # 좌석 이름이 바뀌면 그 좌석 기억 버림
        names["seat_TC"] = ("User85", True)
        tb.step()
        tb.step()
        tb.step(fold=["UTG", "MP", "CO", "BTN", "SB"])
        finish(tb)
        assert store.stats("User84", True, seat="seat_TC", session="sessA")["hands"] == 0
        # 새 세션(다시 보정)이면 모두 버림, DB 는 그대로
        before = db.execute("SELECT SUM(hands) FROM players").fetchone()[0]
        store.new_session("sessB")
        assert store.stats("User85", True, seat="seat_TC", session="sessA")["hands"] == 0
        assert db.execute("SELECT SUM(hands) FROM players").fetchone()[0] == before
        assert not any("User8" in r[0] for r in db.execute("SELECT name FROM players"))
        # 같은 기록 두 번은 한 번만
        assert store.observe(rec) is False
        # low confidence 는 통계에 안 들어감
        low = dict(rec, id="x-low", confidence="low")
        assert store.observe(low) is False
        db.close()
        store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_timing():
    tb = Table(handlog.Tracker("s15"))
    t0 = time.perf_counter()
    n = 0
    for _ in range(100):
        tb.new_hand("seat_UR")
        tb.step(); tb.step(bets={"UTG": 2.5}); tb.step(fold=["MP", "CO"]); tb.step(bets={"BTN": 2.5})
        tb.step(fold=["SB", "BB"]); tb.step(board=["Ah", "7c", "2d"], collect=True); tb.step(bets={"UTG": 3.0})
        tb.step(fold=["BTN"])
        n += 8
    ms = (time.perf_counter() - t0) * 1000 / n
    print(f"  tracker update: {ms:.3f} ms/frame")
    assert ms < 5


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as exc:
            failed += 1
            print("FAIL", fn.__name__, exc)
    print(f"{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
