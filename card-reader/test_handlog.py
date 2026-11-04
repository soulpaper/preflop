"""handlog.Tracker / profiles.Store 단위 테스트 (가짜 tablestate 스냅샷 순서). 실행: python test_handlog.py"""
import json
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

import numpy as np

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

    def step(self, bets=None, fold=(), bubble=None, allin=(), board=None, dt=1.0, collect=False, shown=None, pot=None):
        """한 프레임: 바뀐 것만 넣음. collect=True → 바닥 벳이 팟으로 모임 (스트리트 끝). shown: 쇼다운 공개 카드."""
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
        recs = self.tr.update(self.snap(), hero, self.board, self.names, self.t, shown=shown, pot=pot)
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
    # 포스트플랍 벳/콜에는 기록 금액으로 계산한 팟 대비 크기가 붙는다: 6 into 19.5 (0.5+1+9+9) ≈ 0.31
    assert [w["a"] for w in live["seat_TC"]["flop"]] == ["bet"] and [w["a"] for w in live["seat_BL"]["flop"]] == ["call"], live
    assert abs(live["seat_TC"]["flop"][0]["size"] - 6.0 / 19.5) < 0.01, live["seat_TC"]["flop"]
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


def test_no_names_on_disk_session_stats_in_memory():
    # 닉네임은 기록·JSONL·DB 파일 어디에도 없다. 통계는 메모리에서만 (세션·좌석 단위 잘린 이름 규칙은 그대로)
    tmp = Path(tempfile.mkdtemp())
    try:
        store = profiles.Store(tmp / "p.db", hands_dir=tmp / "hands")
        names = {k: (f"P_{k}", False) for k in KEYS}
        names["seat_TC"] = ("User84", True)          # 잘린 닉네임
        tr = handlog.Tracker("sessA")
        tr.on_hand = lambda r: store.observe(r, names=tr.names_for(r["id"]))
        store.new_session("sessA")
        tb = Table(tr, names=names)
        tb.step()
        tb.step(bets={"CO": 2.5}, fold=["UTG", "MP"], bubble={"CO": "raise"})
        tb.step(fold=["BTN", "SB", "BB"])
        finish(tb)
        rec = tb.out[0]
        assert rec["confidence"] == "ok", rec["issues"]
        assert not (tmp / "p.db").exists(), "profiles.db 를 만들지 않음 (통계 DB 는 메모리)"
        for k, seat in rec["seats"].items():
            assert not set(seat) & set(profiles.NAME_FIELDS), (k, seat)
        assert "P_seat" not in json.dumps(rec, ensure_ascii=False) and "User84" not in json.dumps(rec, ensure_ascii=False)
        # 메모리 통계는 그대로 쌓임
        assert store.stats("P_seat_BL")["hands"] == 1
        s = store.stats("User84", True, seat="seat_TC", session="sessA")
        assert s["hands"] == 1 and s["vpip"] == 100.0 and s["pfr"] == 100.0 and not s["reliable"], s
        table = store.get_for_table(names, session="sessA")
        assert table["seat_TC"]["hands"] == 1 and table["seat_BL"]["hands"] == 1
        assert store.stats("User84", True, seat="seat_BL", session="sessA")["hands"] == 0, "다른 좌석과 섞이지 않음"
        # JSONL: 닉네임 없음, 좌석 키·포지션·pid·타입 칸만
        text = next((tmp / "hands").glob("*.jsonl")).read_text(encoding="utf-8")
        assert "P_seat" not in text and "User84" not in text, text[:300]
        line = json.loads(text.splitlines()[0])
        assert set(line["seats"]["seat_BL"]) >= {"pos", "pid", "type", "type_src"}
        assert line["seats"]["seat_TC"]["pid"] is None, "잘린 이름은 pid 없음"
        assert re.fullmatch(r"p_[0-9a-f]{8}", line["seats"]["seat_BL"]["pid"]), line["seats"]["seat_BL"]
        assert "pid" not in line["seats"]["hero"] or line["seats"]["hero"]["pid"] is None
        # 좌석 이름이 바뀌면 그 좌석 기억 버림
        names["seat_TC"] = ("User85", True)
        tb.step()
        tb.step()
        tb.step(fold=["UTG", "MP", "CO", "BTN", "SB"])
        finish(tb)
        assert store.stats("User84", True, seat="seat_TC", session="sessA")["hands"] == 0
        store.new_session("sessB")
        assert store.stats("User85", True, seat="seat_TC", session="sessA")["hands"] == 0
        assert list(tmp.iterdir()) == [tmp / "hands"], list(tmp.iterdir())
        store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pid_session_pseudonym():
    # 같은 세션: 자리를 옮겨도 같은 pid, 다른 사람은 다른 pid. 새 세션: 같은 사람도 다른 pid (소금이 새로)
    tr = handlog.Tracker("pidA")
    names = {k: (f"P_{k}", False) for k in KEYS}
    tb = Table(tr, names=names)
    tb.step()
    tb.step(fold=["UTG", "MP", "CO", "BTN", "SB"])
    # 다음 핸드: 같은 사람이 다른 자리로 (seat_BL 사람이 seat_UL 로)
    tb.names = {k: (f"P_{k}", False) for k in KEYS}
    tb.names["seat_UL"] = ("P_seat_BL", False)
    tb.names["seat_BL"] = ("Someone", False)
    rec1 = finish(tb)
    pids1 = {k: s["pid"] for k, s in rec1["seats"].items() if k != "hero"}
    assert all(re.fullmatch(r"p_[0-9a-f]{8}", p) for p in pids1.values()), pids1
    assert len(set(pids1.values())) == len(pids1), "다른 사람은 다른 pid"
    tb.step()
    tb.step(fold=["UTG", "MP", "CO", "BTN", "SB"])
    rec2 = finish(tb)
    assert rec2["seats"]["seat_UL"]["pid"] == pids1["seat_BL"], (rec2["seats"]["seat_UL"], pids1)
    # 닉네임 정규화 (공백·대소문자)
    assert handlog.make_pid(b"k", "Ab C") == handlog.make_pid(b"k", "abc") and handlog.make_pid(b"k", "") is None
    # 이름이 엇갈리면(한 핸드에 두 이름) pid 없음
    tb.step()
    tb.names["seat_BR"] = ("Other", False)
    tb.step()
    tb.step(fold=["UTG", "MP", "CO", "BTN", "SB"])
    rec3 = finish(tb)
    assert rec3["seats"]["seat_BR"]["pid"] is None, rec3["seats"]["seat_BR"]
    # 새 세션
    tr.new_session("pidB")
    tb.names = {k: (f"P_{k}", False) for k in KEYS}
    tb.step()
    tb.step(fold=["UTG", "MP", "CO", "BTN", "SB"])
    rec4 = finish(tb)
    assert rec4["session"] == "pidB"
    assert rec4["seats"]["seat_BL"]["pid"] != pids1["seat_BL"], "세션이 다르면 연결되지 않음"
    assert "P_seat" not in json.dumps([rec1, rec2, rec3, rec4], ensure_ascii=False)
    assert tr.names_for(rec4["id"])["seat_BL"] == ("P_seat_BL", False), "메모리에서만 닉네임"


def test_types_recorded_per_seat():
    # 앱이 넘긴 좌석 타입(팝업)이 기록에 seat['type'] / type_src / type_stats 로 남음
    tr = handlog.Tracker("ty")
    tb = Table(tr)
    types = {"seat_BL": {"type": "LP", "type_src": "popup", "type_stats": {"vpip": 48.0, "pfr": 6.0}}}
    s = tb.snap()
    tb.t += 1
    tr.update(s, tb.hero, [], tb.names, tb.t, types=types)
    tb.step(fold=["UTG", "MP", "CO", "BTN", "SB"])
    rec = finish(tb)
    bl = rec["seats"]["seat_BL"]
    assert bl["type"] == "LP" and bl["type_src"] == "popup" and bl["type_stats"]["vpip"] == 48.0, bl
    assert rec["seats"]["seat_UL"]["type"] is None and rec["seats"]["seat_UL"]["type_src"] is None
    assert "type" not in rec["seats"]["hero"]


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


# --- 회귀 테스트 (감사에서 찾은 버그) -------------------------------------------------------------------
def test_phantom_seat_one_frame():
    # 핸드에 안 낀 자리가 한 프레임만 '카드 받음'으로 보임 (아이콘/흑백 오판) → 콜 추정(VPIP)도, 남은 사람도 아님
    tb = Table(handlog.Tracker("r1"))
    real, ghost = tb.snap, {"on": False}

    def snap():
        s = real()
        s["seats"].insert(4, {"key": "seat_R", "slot": "R", "pos": None, "occupied": True, "dealt": ghost["on"],
                              "in_hand": ghost["on"], "folded": False, "allin": False, "bet": None, "stack": None,
                              "bubble": None})
        return s
    tb.snap = snap
    tb.step()
    ghost["on"] = True
    tb.step(bets={"UTG": 2.5}, bubble={"UTG": "raise"})
    ghost["on"] = False
    tb.step(fold=["MP", "CO", "BTN", "SB"])
    tb.step(bets={"BB": 2.5}, bubble={"BB": "call"})
    tb.step(board=["Ah", "7c", "2d"], collect=True, bubble={})
    tb.step(fold=["BB"])
    assert tb.tr.hand.over, "BB 폴드로 UTG 혼자 남음 → 핸드 끝"
    rec = finish(tb)
    assert "seat_R" not in rec["seats"], rec["seats"].keys()
    assert rec["confidence"] == "ok" and ("seat_R", "call") not in acts(rec, "preflop"), (acts(rec, "preflop"), rec["issues"])


def test_preflop_end_with_two_left_not_inferred():
    # 프리플랍에 2명 이상 남은 채 다른 핸드 신호 (마지막 폴드를 놓침/잘못된 경계) → 행동 안 한 사람을 콜(VPIP)로 만들지 않음
    tb = Table(handlog.Tracker("r2"))
    tb.step()
    tb.step(bets={"UTG": 2.5}, bubble={"UTG": "raise"})
    tb.dealer = "seat_BR"
    tb.step()
    tb.step()
    rec = tb.out[0]
    assert rec["end_reason"] == "dealer moved" and rec["confidence"] == "low", (rec["end_reason"], rec["issues"])
    assert all(a != "call" for _, a in acts(rec, "preflop")), acts(rec, "preflop")
    assert all(st(rec, k)["vpip"] == 0 for k in KEYS if k != "seat_BL")


def test_truncated_if_ever_seen_truncated():
    # 점('…')을 놓친 프레임이 더 많아도 한 번이라도 잘린 이름으로 읽혔으면 pid 를 붙이지 않음 (다른 사람과 겹칠 수 있음)
    tmp = Path(tempfile.mkdtemp())
    try:
        store = profiles.Store(hands_dir=tmp / "hands")
        tr = handlog.Tracker("r3")
        tr.on_hand = lambda r: store.observe(r, names=tr.names_for(r["id"]))
        tb = Table(tr)
        store.new_session("r3")
        tb.names["seat_TC"] = ("User84", True)
        tb.step()
        tb.names["seat_TC"] = ("User84", False)
        tb.step(bets={"CO": 2.5}, fold=["UTG", "MP"], bubble={"CO": "raise"})
        tb.step()
        tb.step(fold=["BTN", "SB", "BB"])
        rec = finish(tb)
        assert rec["seats"]["seat_TC"]["pid"] is None and "truncated" not in rec["seats"]["seat_TC"]
        assert tr.names_for(rec["id"])["seat_TC"] == ("User84", True)
        assert store.stats("User84")["hands"] == 0, "잘린 이름은 (메모리) 전체 이름 통계에도 안 들어감"
        store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_allin_showdown_counts_saw_flop():
    # 프리플랍 올인 쇼다운인데 보드를 못 읽음 → showdown 이면 saw_flop 도 1 (WTSD 가 100% 를 넘지 않게)
    tb = Table(handlog.Tracker("r4"))
    tb.step()
    tb.step(bets={"UTG": 50.0}, allin=["UTG"], bubble={"UTG": "allin"})
    tb.step(fold=["MP", "CO", "BTN", "SB"])
    tb.step(bets={"BB": 50.0}, allin=["BB"], bubble={"BB": "allin"})
    tb.step()
    rec = finish(tb)
    for k in ("seat_BL", "hero"):
        assert st(rec, k)["showdown"] <= st(rec, k)["saw_flop"], (k, st(rec, k))


def test_layout_mismatch_frame_ignored():
    # tablestate 가 화면이 보정과 어긋났다고 하면 (layout_ok False) 그 프레임의 좌석 값으로 액션을 만들지 않음
    tb = Table(handlog.Tracker("r5"))
    tb.step()
    s = tb.snap()
    s["layout_ok"] = False
    for seat in s["seats"]:
        seat["folded"], seat["in_hand"] = True, False
    tb.t += 1
    tb.tr.update(s, tb.hero, [], tb.names, tb.t)
    assert not any(a["act"] == "fold" for a in tb.tr.hand.actions["preflop"]), tb.tr.hand.actions


def test_store_robustness():
    tmp = Path(tempfile.mkdtemp())
    try:
        # JSONL 을 못 써도 (폴더 자리에 파일) 통계는 쌓임
        (tmp / "hands").write_text("x")
        store = profiles.Store(tmp / "p.db", hands_dir=tmp / "hands")
        assert store.observe({"id": "j1", "session": "S", "confidence": "ok", "start": 1, "end": 2, "board": [],
                              "seats": {"seat_A": {"name": "Bob", "truncated": False, "stats": {"hands": 1, "vpip": 1}}}})
        assert store.stats("Bob")["hands"] == 1
        # 글자 지문이 닮아도 숫자가 다른 닉네임은 다른 사람
        fp = np.zeros((16, 120), np.uint8)
        fp[3:13, ::7] = 255
        store.learn_fingerprint("seat_A", "User854834280", False, fp)
        assert store.resolve_name("User854839280", fp) == "User854839280"
        assert store.resolve_name("User854834280", fp) == "User854834280"
        store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_recorder_recalib_same_second():
    import recorder
    tmp = Path(tempfile.mkdtemp())
    try:
        r = recorder.Recorder(tmp)
        r.start({"v": 1})
        r.set_calib({"v": 2})
        r.set_calib({"v": 3})   # 1초 안에 다시 보정해도 최신 보정이 저장됨
        assert json.loads((r.folder / "calib.json").read_text(encoding="utf-8")) == {"v": 3} and r.errors == 0, r.last_error
        r.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_tablestate_positions_headsup():
    import tablestate
    order = [{"key": "seat_UR"}, {"key": "hero"}]
    want = {"hero": "SB", "seat_UR": "BB"}   # 헤즈업: 딜러 = SB
    for dealer, discs in (("hero", {}), (None, {"SB": "hero", "BB": "seat_UR"}), (None, {"BB": "seat_UR"}),
                          (None, {"SB": "hero"})):
        got = tablestate.TableReader._positions(order, order, dealer, discs, [])
        assert got == want, (dealer, discs, got)


def _break_calib(img, pcx, pcy, pw):
    """samples/live_break_false_cards 의 게임 창에 맞춘 보정 (휴식 화면엔 알약이 없어 자리 좌표로 만듦)."""
    import layout
    H, W = img.shape[:2]
    res = {"geom": {"pill_center": [pcx, pcy], "pw": pw, "ring": [pcx, pcy + 0.5 * pw, 2.1 * pw, 1.2 * pw], "size": [W, H]},
           "unit": "BB", "pill": [int(pcx - pw / 2), int(pcy - pw / 10), int(pw), int(pw / 5)]}

    def stack(slot):
        x, y = layout.SLOTS[slot]
        return [int(pcx + x * pw - 0.3 * pw), int(pcy + y * pw - 0.048 * pw), int(0.6 * pw), int(0.096 * pw)]
    res["my_stack"] = stack("HERO")
    for i, s in enumerate(("BL", "L", "UL", "TL", "TR", "UR", "R", "BR"), 1):
        res[f"opp{i}"] = stack(s)
    x, y, w, h = layout.HERO_FROM_STACK
    hx, hy = pcx + layout.SLOTS["HERO"][0] * pw, pcy + layout.SLOTS["HERO"][1] * pw
    res["hero"] = layout._clip((hx + x * pw, hy + y * pw, w * pw, h * pw), img.shape)
    res.update(layout.anchors(res))
    return res


def test_tablestate_break_screen():
    import cv2
    import ocr
    import tablestate
    if not ocr.available():
        print("  OCR 불가: 건너뜀")
        return
    full = cv2.imread(str(Path(__file__).resolve().parent / "samples" / "live_break_false_cards.webp"))
    img = full[109:836, 1:960]      # 데스크톱 캡처 안의 게임 창 (청록 테두리 안쪽)
    s = tablestate.TableReader().read(img, _break_calib(img, 472.4, 289.0, 186.0))
    assert s["layout_ok"] and s["no_hand"] and s["players"] == 8 and s["dealt"] == 0 and s["in_hand"] == 0, \
        (s["players"], s["dealt"], s["notes"])
    assert s["hero_pos"] is None and s["preflop"] is None and not s["to_act"]
    # 창이 움직였는데 예전 보정 그대로 (20px 아래로 어긋남) → '1명 중 0명' 대신 화면 불일치 신호
    s = tablestate.TableReader().read(img, _break_calib(img, 472.4, 309.0, 186.0))
    assert s["layout_ok"] is False and s["players"] is None and s["notes"], s["notes"]
    # 다른 크기 창의 보정 (내 카드 칸이 이미지 밖) → 예외 없이
    s = tablestate.TableReader().read(img[:400, :500], _break_calib(img, 472.4, 289.0, 186.0))
    assert s["layout_ok"] is False


def test_call_amount_reread_is_not_a_new_action():
    """2026-10-30 라이브 KK 핸드: MP 림프 1BB 를 0.9 로 읽었다가 내 레이즈 뒤 1.0 으로 다시 읽음 → '콜 0.9' + '콜 1.0'
    두 액션이 되어 라인이 꼬였다. 다시 읽은 금액은 직전 콜을 고친다."""
    tb = Table(handlog.Tracker("s_wobble"), dealer="seat_BL", hero=("Kh", "Kc"))   # SB=UL BB=TC UTG=UR MP=BR CO=hero
    tb.step()
    tb.step(bets={"UTG": 1.0}, bubble={"UTG": "call"})
    tb.step(bets={"MP": 0.9}, bubble={"MP": "call"})          # OCR: 1BB → 0.9
    tb.step(bets={"hero": 5.0}, bubble={"hero": "raise"})
    tb.step(fold=["BTN"], bets={"MP": 1.0})                   # 같은 칩을 이제 1.0 으로 읽음
    tb.step(bets={"SB": 5.0, "BB": 5.0}, bubble={"SB": "call", "BB": "call"})
    tb.step(bets={"UTG": 5.0, "MP": 5.0}, bubble={"UTG": "call", "MP": "call"})
    live = tb.tr.live()
    assert live["seat_BR"]["preflop"] == ["limp", "call"], live["seat_BR"]
    tb.step(board=["Ah", "9d", "4c"], collect=True, bubble={})
    tb.step()
    rec = finish(tb)
    mp = [(a["act"], a.get("to"), a.get("raises_before")) for a in rec["actions"]["preflop"] if a["seat"] == "seat_BR"]
    assert mp == [("call", 1.0, 0), ("call", 5.0, 1)], mp
    # 진짜 새 콜(림프 1 → 레이즈 5 에 콜)은 그대로 두 액션
    utg = [a["act"] for a in rec["actions"]["preflop"] if a["seat"] == "seat_UR"]
    assert utg == ["call", "call"], utg


# --- 쇼다운 공개 카드 (showdown.Watcher → Tracker.update(shown=...)) ------------------------------------------
def _to_river(tb):
    """UTG(seat_BL) 오픈, BB(hero) 콜, 나머지 폴드 → 리버까지 체크."""
    tb.step()
    tb.step(bets={"UTG": 2.5}, bubble={"UTG": "raise"})
    tb.step(fold=["MP", "CO", "BTN", "SB"])
    tb.step(bets={"BB": 2.5}, bubble={"BB": "call"})
    tb.step(board=["8c", "4d", "Jh"], collect=True, bubble={})
    tb.step(bubble={"BB": "check", "UTG": "check"})
    tb.step(board=["8c", "4d", "Jh", "3c"], bubble={})
    tb.step(bubble={"BB": "check", "UTG": "check"})
    tb.step(board=["8c", "4d", "Jh", "3c", "7s"], bubble={})
    tb.step(bubble={"BB": "check", "UTG": "check"})


def test_shown_cards_in_record():
    tb = Table(handlog.Tracker("sd1"))
    _to_river(tb)
    assert tb.tr.showdown_watch() == (tb.tr.hand.id, ["seat_BL"]), tb.tr.showdown_watch()
    # 공개 카드가 아바타를 덮어 흑백(폴드)으로 보이는 프레임에 카드도 읽힘 → 폴드 아님
    tb.step(fold=["UTG"], shown={"seat_BL": ["9c", "7c"]})
    tb.step(fold=["UTG"], shown={"seat_BL": ["9c", "7c"]})
    rec = finish(tb)
    assert rec["seats"]["seat_BL"]["shown"] == ["9c", "7c"], rec["seats"]["seat_BL"]
    assert rec["seats"]["seat_BL"]["folded"] is None, rec["seats"]["seat_BL"]
    assert rec["showdown"] is not None and rec["showdown"]["shown"] == {"seat_BL": ["9c", "7c"]}, rec["showdown"]
    assert set(rec["showdown"]["seats"]) == {"seat_BL", "hero"}, rec["showdown"]
    assert not any(a["act"] == "fold" for a in rec["actions"].get("river", [])), rec["actions"].get("river")
    assert st(rec, "seat_BL")["showdown"] == 1 and rec["seats"]["hero"]["shown"] is None


def test_street_pot_recorded_from_screen_pot():
    """화면 팟(앤티 포함)을 스트리트 시작(벳이 보이기 전)에 기록 -> rec["street_pot"], bet_sizes 가 그 팟 기준으로 계산."""
    tb = Table(handlog.Tracker("s2p"))
    tb.step(pot=1.5)
    tb.step(bets={"UTG": 2.5}, bubble={"UTG": "raise"}, pot=4.0)
    tb.step(fold=["MP", "CO"], pot=4.0)
    tb.step(bets={"BTN": 2.5}, bubble={"BTN": "call"}, pot=6.5)
    tb.step(fold=["SB"], pot=6.5)
    tb.step(bets={"BB": 2.5}, bubble={"BB": "call"}, pot=9.0)
    tb.step(board=["Ah", "7c", "2d"], collect=True, bubble={}, pot=11.0)    # 플랍: 팟 11 (앤티 3 포함)
    tb.step(pot=11.0)
    tb.step(bets={"UTG": 3.0}, bubble={"UTG": "raise"}, pot=14.0)          # 벳이 보인 뒤의 팟은 기록 안 함
    tb.step(fold=["BTN"], bubble={"BTN": "fold"}, pot=14.0)
    tb.step(fold=["BB"], pot=14.0)
    rec = finish(tb)
    assert rec.get("street_pot") == {"flop": 11.0}, rec.get("street_pot")
    sz = handlog.bet_sizes(rec["actions"], street_pot=rec["street_pot"])
    bet = [v for (s, i), v in sz.items() if s == "flop" and rec["actions"]["flop"][i]["act"] == "bet"]
    assert bet and abs(bet[0] - 3.0 / 11.0) < 0.01, sz


def test_street_pot_absent_without_pot():
    tb = Table(handlog.Tracker("s2q"))
    tb.step()
    tb.step(bets={"UTG": 2.5}, bubble={"UTG": "raise"})
    tb.step(fold=["MP", "CO", "BTN", "SB"])
    tb.step(fold=["BB"])
    rec = finish(tb)
    assert "street_pot" not in rec


def test_shown_late_read_undoes_grey_fold():
    # 카드가 뜬 첫 프레임은 못 읽어서 흑백 폴드가 기록됨 → 다음 프레임에 읽히면 그 폴드를 되돌림
    tb = Table(handlog.Tracker("sd2"))
    _to_river(tb)
    tb.step(fold=["UTG"])
    assert tb.tr.hand.seats["seat_BL"].folded
    assert tb.tr.showdown_watch() == (tb.tr.hand.id, ["seat_BL"]), "이번 스트리트에 흑백이 된 좌석도 본다"
    tb.step(fold=["UTG"], shown={"seat_BL": ["Ad", "5d"]})
    rec = finish(tb)
    assert rec["seats"]["seat_BL"]["folded"] is None and rec["seats"]["seat_BL"]["shown"] == ["Ad", "5d"]
    assert rec["showdown"] and rec["showdown"]["shown"] == {"seat_BL": ["Ad", "5d"]}
    assert any("fold undone" in n for n in rec["notes"]), rec["notes"]


def test_shown_none_drops_and_bad_values_ignored():
    tb = Table(handlog.Tracker("sd3"))
    _to_river(tb)
    tb.step(shown={"seat_BL": ["9c", "7c"]})
    tb.step(shown={"seat_BL": None})                                   # Watcher: 다른 값이 읽혀 어긋남 → 지움
    tb.step(shown={"seat_UL": ["9c", "9c"], "seat_TC": ["Xx", "7c"], "seat_UR": ["9c"]})   # 잘못된 값은 무시
    rec = finish(tb)
    assert rec["showdown"]["shown"] == {}, rec["showdown"]
    assert all(s["shown"] is None for s in rec["seats"].values())


def test_shown_on_boundary_frame_goes_to_finished_hand():
    # 새 핸드로 넘어가는 프레임에 아직 보이는 공개 카드는 끝나는 핸드 것
    tb = Table(handlog.Tracker("sd4"))
    _to_river(tb)
    first = tb.tr.hand.id
    tb.new_hand("seat_BR", ("2c", "7d"))
    recs = tb.step(shown={"seat_BL": ["Ks", "Kd"]})      # 이 프레임에서 'folded seats dealt again' 으로 핸드가 끝남
    assert [r["id"] for r in recs] == [first], recs
    assert recs[0]["seats"]["seat_BL"]["shown"] == ["Ks", "Kd"], recs[0]["seats"]["seat_BL"]
    assert tb.tr.hand is not None and not tb.tr.hand.shown


def test_showdown_watch_gating():
    tb = Table(handlog.Tracker("sd5"))
    assert tb.tr.showdown_watch() is None                                # 핸드 없음
    tb.step()
    tb.step(bets={"UTG": 2.5}, bubble={"UTG": "raise"})
    assert tb.tr.showdown_watch() is None                                # 프리플랍, 올인 없음
    tb.step(fold=["MP", "CO", "BTN", "SB"])
    tb.step(bets={"BB": 2.5}, bubble={"BB": "call"})
    tb.step(board=["8c", "4d", "Jh"], collect=True, bubble={})
    assert tb.tr.showdown_watch() is None                                # 플랍, 아무도 흑백 아님
    tb.step(bets={"UTG": 47.5}, allin=["UTG"], bubble={"UTG": "allin"})
    assert tb.tr.showdown_watch() == (tb.tr.hand.id, ["seat_BL"])       # 올인
    tb2 = Table(handlog.Tracker("sd6"))
    tb2.step()
    tb2.step(fold=["UTG", "MP", "CO", "BTN"])
    tb2.step(fold=["SB"])
    assert tb2.tr.showdown_watch() is None                               # 남은 사람 1명


# --- to_handread 입력 정리 (2026-11-05 쇼다운 채점에서 찾은 읽기 오류 모양, 가짜 기록) ------------------------------
def _pa(seat, act, to=None, allin=False):
    a = {"seat": seat, "act": act}
    if to is not None:
        a["to"] = to
    if allin:
        a["allin"] = True
    return a


_POS = {"S": "SB", "B": "BB", "U": "UTG", "M": "MP", "C": "CO", "D": "BTN"}
_BLINDS = [_pa("S", "post", 0.5), _pa("B", "post", 1.0)]


def _pre(seats, k):
    return seats[k]["preflop"]


def _rb(acts):
    """Tracker 처럼 raises_before = 앞의 '레이즈' 기록 수 (정리 전 값)."""
    n = 0
    for a in acts["preflop"]:
        a["raises_before"] = n
        n += a["act"] == "raise"
    return acts


def test_to_handread_same_raise_read_twice():
    """오픈 1.3 → 2.0 을 두 번 읽음: 오픈 하나. 뒤 사람의 레이즈는 4벳이 아니라 3벳."""
    acts = _rb({"preflop": _BLINDS + [_pa("U", "raise", 1.3), _pa("U", "raise", 2.0), _pa("C", "raise", 8.3),
                                      _pa("D", "call", 8.3)]})
    out = handlog.to_handread(acts, _POS)
    assert _pre(out, "U") == ["open"] and _pre(out, "C") == ["3bet"], out
    assert _pre(out, "D") == ["call_3bet"], out
    raw = handlog.to_handread(acts, _POS, clean=False)
    assert _pre(raw, "U") == ["open", "3bet"] and _pre(raw, "C") == ["4bet"], raw     # 예전 입력 (비교)
    cleaned, fixed = handlog.clean_preflop(acts["preflop"])
    assert fixed >= 1 and [a["act"] for a in cleaned].count("raise") == 2, cleaned


def test_to_handread_allin_reread_and_call_is_call_jam():
    """올인 114.7 → 115 다시 읽음 = 올인 하나. 그 올인에 콜(콜한 사람은 올인 아님) = 올인콜, 스택 = 올인 금액."""
    acts = _rb({"preflop": _BLINDS + [_pa("U", "call", 1.0), _pa("C", "raise", 114.7, True), _pa("C", "raise", 115.0, True),
                                      _pa("B", "call", 115.0)]})
    out = handlog.to_handread(acts, _POS)
    assert _pre(out, "C") == ["jam"] and out["C"]["stack_bb"] == 115.0, out["C"]
    assert _pre(out, "B") == ["blind", "call_jam"] and out["B"]["stack_bb"] == 115.0, out["B"]
    assert "stack_bb" not in out["U"]


def test_to_handread_short_jam_call():
    """8.5BB 오픈 올인에 BB 콜 → BB 는 올인콜 (예전엔 '오픈 콜'), 두 사람 모두 스택 8.5."""
    acts = _rb({"preflop": _BLINDS + [_pa("D", "raise", 8.5, True), _pa("B", "call", 8.5)]})
    out = handlog.to_handread(acts, _POS)
    assert _pre(out, "D") == ["jam"] and _pre(out, "B") == ["blind", "call_jam"], out
    assert out["D"]["stack_bb"] == 8.5 and out["B"]["stack_bb"] == 8.5
    assert _pre(handlog.to_handread(acts, _POS, clean=False), "B") == ["blind", "call"]


def test_to_handread_tiny_allin_is_not_a_3bet():
    """2.0 오픈에 2.1 올인(최소 레이즈 미만): 레이즈로 안 센다 → 뒤 콜은 3벳콜이 아니고, 오프너의 0.1 콜은 새 결정이 아님."""
    acts = _rb({"preflop": _BLINDS + [_pa("U", "call", 1.0), _pa("C", "raise", 2.0), _pa("S", "raise", 2.1, True),
                                      _pa("B", "call", 2.1), _pa("U", "call", 2.1), _pa("C", "call", 2.1)]})
    out = handlog.to_handread(acts, _POS)
    assert _pre(out, "C") == ["open"], out["C"]                       # 예전: ['open', 'call_3bet']
    assert _pre(out, "S") == ["blind", "jam"] and out["S"]["stack_bb"] == 2.1, out["S"]
    assert _pre(out, "B") == ["blind", "call"], out["B"]              # 예전: 3벳콜
    assert _pre(out, "U") == ["limp", "call"], out["U"]
    raw = handlog.to_handread(acts, _POS, clean=False)
    assert _pre(raw, "C") == ["open", "call_3bet"], raw["C"]


def _derive_stats(pre, seats):
    rec = {"actions": _rb({"preflop": pre}), "seats": {k: {} for k in seats}, "streets": "preflop"}
    return {k: s["stats"] for k, s in handlog.derive(rec)["seats"].items()}


def test_derive_threebet_uses_clean_preflop():
    """derive 는 정리된 프리플랍으로 vpip/pfr/3벳을 센다 (짧은 올인·콜 금액 재읽기·오픈 두 번 읽음)."""
    # ① C 오픈 2.0, B 가 2.1 '레이즈'(올인 아님) = 콜 금액을 다시 읽음: 3벳 기회 1, 3벳 0, pfr 0
    d = _derive_stats(_BLINDS + [_pa("C", "raise", 2.0), _pa("B", "raise", 2.1)], "SBC")
    assert (d["B"]["threebet_opp"], d["B"]["threebet"], d["B"]["pfr"], d["B"]["vpip"]) == (1, 0, 0, 1), d["B"]
    # ② 짧은 올인 S 2.1: 기회는 있되 3벳 아님(pfr 은 공격 행동이라 1), 뒤의 B 콜도 기회 1
    d = _derive_stats(_BLINDS + [_pa("C", "raise", 2.0), _pa("S", "raise", 2.1, True), _pa("B", "call", 2.1)], "SBC")
    assert (d["S"]["threebet_opp"], d["S"]["threebet"], d["S"]["pfr"]) == (1, 0, 1), d["S"]
    assert (d["B"]["threebet_opp"], d["B"]["threebet"]) == (1, 0), d["B"]
    # ③ 진짜 3벳
    d = _derive_stats(_BLINDS + [_pa("C", "raise", 2.0), _pa("B", "raise", 6.0)], "SBC")
    assert (d["B"]["threebet"], d["B"]["threebet_opp"]) == (1, 1), d["B"]
    assert (d["C"]["threebet_opp"], d["C"]["pfr"]) == (0, 1), d["C"]
    # ④ 오픈 1.3 → 2.0 두 번 읽음: BTN 6.0 은 3벳, C 는 한 번만 센다
    d = _derive_stats(_BLINDS + [_pa("C", "raise", 1.3), _pa("C", "raise", 2.0), _pa("D", "raise", 6.0)], "SBCD")
    assert (d["D"]["threebet"], d["D"]["threebet_opp"]) == (1, 1), d["D"]
    assert (d["C"]["threebet_opp"], d["C"]["pfr"]) == (0, 1), d["C"]


def test_to_handread_tiny_raise_not_allin_is_a_call():
    """올인 아닌 2.0 → 2.1 '레이즈' = 콜 금액을 다시 읽은 것."""
    acts = _rb({"preflop": _BLINDS + [_pa("U", "raise", 2.0), _pa("C", "raise", 2.1), _pa("D", "call", 2.1)]})
    out = handlog.to_handread(acts, _POS)
    assert _pre(out, "C") == ["call"] and _pre(out, "D") == ["call"], out


def test_to_handread_limp_reread_merged():
    """림프 0.6 → 1.0 (같은 칩을 두 번 읽음) = 림프 하나."""
    acts = _rb({"preflop": _BLINDS + [_pa("U", "call", 0.6), _pa("U", "call", 1.0), _pa("B", "check")]})
    assert _pre(handlog.to_handread(acts, _POS), "U") == ["limp"]


def test_to_handread_absurd_sizes():
    """700 / 1400BB 레이즈(올인 아님) = 잘못 읽은 금액 → 레이즈 순서는 남기고 금액은 버림 (팟·벳 크기를 망치지 않게)."""
    acts = _rb({"preflop": _BLINDS + [_pa("U", "call", 1.0), _pa("S", "raise", 700.0), _pa("B", "raise", 1400.0),
                                      _pa("U", "call", 1400.0), _pa("S", "call", 1400.0)],
                "flop": [_pa("S", "check"), _pa("B", "bet", 4.0), _pa("U", "call", 4.0), _pa("S", "fold")]})
    cleaned, fixed = handlog.clean_preflop(acts["preflop"])
    assert fixed >= 3 and all(a.get("to") is None for a in cleaned if a["act"] in ("raise", "call") and a["seat"] != "U"
                              or a.get("size_bad")), cleaned
    out = handlog.to_handread(acts, _POS)
    assert _pre(out, "S") == ["blind", "open", "call_3bet"] and _pre(out, "B") == ["blind", "3bet"], out
    bet = out["B"]["flop"][0]
    assert isinstance(bet, dict) and bet["size"] > 0.3, bet           # 2800BB 팟이면 0.001 (작은 벳)로 잘못 나옴
    assert "stack_bb" not in out["S"] and "stack_bb" not in out["B"]


def test_to_handread_postflop_sizes():
    """포스트플랍 벳/콜/레이즈에 팟 대비 크기: 3BB 오픈·콜(팟 6.5) → 플랍 벳 4.5 = 0.69, 체크레이즈 크기."""
    acts = _rb({"preflop": _BLINDS + [_pa("C", "raise", 3.0), _pa("S", "fold"), _pa("B", "call", 3.0)],
                "flop": [_pa("B", "check"), _pa("C", "bet", 4.5), _pa("B", "raise", 15.0), _pa("C", "call", 15.0)]})
    out = handlog.to_handread(acts, _POS)
    c, b = out["C"]["flop"], out["B"]["flop"]
    assert c[0]["a"] == "bet" and abs(c[0]["size"] - 4.5 / 6.5) < 0.01, c
    assert b[0] == "check" and b[1]["a"] == "checkraise" and abs(b[1]["size"] - 10.5 / 15.5) < 0.01, b
    assert c[1]["a"] == "call" and abs(c[1]["size"] - b[1]["size"]) < 1e-9
    assert out["C"]["bet_role"] == {"flop": "cbet"}
    plain = handlog.to_handread(acts, _POS, sizes=False)
    assert plain["C"]["flop"] == ["bet", "call"], plain["C"]


def test_jsonl_append_after_crash():
    # 쓰다 죽어 줄바꿈 없이 끝난 핸드 기록 파일: 다음 기록은 새 줄에 써서 읽을 수 있어야 함
    tmp = Path(tempfile.mkdtemp())
    try:
        store = profiles.Store(hands_dir=tmp / "hands")
        rec = {"id": "c1", "session": "S", "confidence": "ok", "start": 1000.0, "end": 1001.0, "board": [], "seats": {}}
        store.observe(rec)
        f = next((tmp / "hands").glob("*.jsonl"))
        with open(f, "a", encoding="utf-8") as fh:
            fh.write('{"id": "c2", "session": "S", "sea')            # 반쪽 줄
        store.observe(dict(rec, id="c3"))
        good = []
        for ln in f.read_text(encoding="utf-8").splitlines():
            try:
                good.append(json.loads(ln)["id"])
            except ValueError:
                pass
        assert good == ["c1", "c3"], f.read_text(encoding="utf-8")
        store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
