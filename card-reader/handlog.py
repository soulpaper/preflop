"""핸드 기록: 약 1초마다 받는 테이블 상태(tablestate.read 결과)에서 핸드 경계와 좌석별 액션을 추론한다.

    tracker = handlog.Tracker(session="20261029_0102")
    for each frame (~1 s):
        done = tracker.update(state, hero_cards, board, names, t, pot=None, shown=None)
        for rec in done: store.observe(rec)          # 끝난 핸드 기록 (dict)
    tracker.flush(t)                                 # 앱 종료 / 다시 보정 때

핸드 경계 (하나라도)
  - 내 카드 두 장이 다른 카드로 바뀜
  - 보드가 있다가 2프레임 연속 비어 있음
  - 딜러 원판이 다른 자리로 2프레임 연속
  - 이번 핸드에서 폴드(흑백)였던 2명 이상이 다시 컬러로 카드를 받음
  - 핸드가 끝난 뒤(남은 사람 1명) 다시 2명 이상이 카드를 받고 블라인드만 깔림
액션 (스트리트별, 프레임 차이로)
  - 바닥 벳 증가 → 현재 최대 벳과 비교해 call / raise / bet (프리플랍 첫 SB 0.5 / BB 1 = 블라인드, VPIP 아님)
  - 폴드: 아바타 흑백(folded) 또는 '폴드' 말풍선     - 올인: allin 또는 '올인' 말풍선
  - 체크: '체크' 말풍선, 또는 스트리트가 끝날 때 벳 없이 남아 있음 (포스트플랍 첫 벳 앞 순서의 사람도 체크)
  - 한 프레임 사이에 여러 액션: 지난번 마지막 행동자 다음부터 행동 순서대로 처리. '콜' 말풍선인데 금액이 지금 최대보다
    크면 같은 프레임의 레이즈 뒤로 미룸. 같은 사람이 벳하고 폴드까지 했으면 폴드는 맨 뒤.
  - 다음 스트리트에 남아 있는데 금액을 못 본 사람 = 콜로 추정 (issues 에 남김)
믿을 수 없는 핸드는 confidence='low' + issues (profiles.Store 는 통계에 안 넣고 JSONL 에만 표시해 저장)

절대 예외를 밖으로 내지 않는다 (update/flush 는 실패하면 [] 반환, 진행 중 핸드는 low 로 표시).
"""
import time
from collections import Counter

STREETS = ("preflop", "flop", "turn", "river")
BOARD_STREET = {0: 0, 3: 1, 4: 2, 5: 3}
EPS = 0.05                 # 같은 금액으로 보는 차이 (BB)
SB_AMOUNT, BB_AMOUNT = 0.5, 1.0
GAP_FATAL = 8.0            # 핸드 중 프레임 간격이 이보다 길면 놓친 액션이 있을 수 있음 → low
STALE_HAND = 90.0          # 이만큼 새 프레임이 없으면 진행 중 핸드를 닫음
PRE_RANK = ("UTG", "U1", "U2", "U3", "MP", "LJ", "HJ", "CO", "BTN", "SB", "BB")
# 이 문제가 하나라도 있으면 confidence='low' (통계에 안 넣음). 나머지(inferred_call, positions_unknown,
# bubble_conflict, short_call)는 참고용
FATAL = {"mid_hand_start", "bet_decreased", "ambiguous_order", "tracker_error", "gap", "allin_unread", "incomplete"}


def _is_card(c):
    return isinstance(c, str) and len(c) == 2 and c[0] in "23456789TJQKA" and c[1] in "shdc"


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


class _Seat:
    __slots__ = ("key", "slot", "pos", "names", "folded", "fold_street", "allin", "inv", "posted", "stale",
                 "bubble", "hero", "acted")

    def __init__(self, key):
        self.key = key
        self.slot = None
        self.pos = Counter()
        self.names = Counter()
        self.folded = False
        self.fold_street = None
        self.allin = False
        self.inv = 0.0          # 이번 스트리트에 낸 금액 (바닥 벳)
        self.posted = False
        self.stale = None       # 지난 스트리트 벳이 아직 화면에 남아 있을 수 있음 (그 값은 무시)
        self.bubble = None      # 직전 프레임 말풍선
        self.hero = key == "hero"
        self.acted = False      # 이번 스트리트에 행동함


class _Hand:
    def __init__(self, hid, session, t):
        self.id = hid
        self.session = session
        self.t0 = self.t1 = t
        self.frames = 0
        self.street = 0
        self.board = []
        self.hero_cards = None
        self.dealer = Counter()
        self.seats = {}
        self.actions = {s: [] for s in STREETS}
        self.cur_max = BB_AMOUNT
        self.raises = 0
        self.last_actor = None
        self.issues = []
        self.notes = []
        self.pot = None
        self.shown = {}
        self.inhand_start = {0: None}
        self.over = False        # 남은 사람 1명 (나머지 폴드) 또는 화면상 핸드 종료
        self.allin_runout = False
        self.blind_keys = {}
        self.pos_players = Counter()

    def issue(self, code, detail=None):
        if not any(i["code"] == code and i.get("detail") == detail for i in self.issues):
            self.issues.append({"code": code, "detail": detail} if detail is not None else {"code": code})

    def live(self):
        return [s for s in self.seats.values() if not s.folded]


class Tracker:
    def __init__(self, session=None, on_hand=None):
        self.session = session or time.strftime("%Y%m%d_%H%M%S")
        self.on_hand = on_hand
        self.hand = None
        self.count = 0
        self.errors = 0
        self.last_error = None
        self.last_t = None
        self.last_ms = 0.0
        self._board_empty = 0
        self._dealer_new = (None, 0)
        self._hero_new = (None, 0)

    def _suspect(self):
        """새 핸드일 수 있는 신호가 1프레임 보임 → 이 프레임의 액션은 처리하지 않음."""
        return bool(self._board_empty or self._dealer_new[1] or self._hero_new[1])

    # --- 공개 API ------------------------------------------------------------------------------------
    def update(self, state, hero_cards=None, board=None, names=None, t=None, pot=None, shown=None):
        """프레임 하나 → 이번에 끝난 핸드 기록 리스트 (대개 빈 리스트)."""
        t0 = time.perf_counter()
        t = time.time() if t is None else float(t)
        try:
            out = self._update(state, hero_cards or [], board or [], names or {}, t, pot, shown)
        except Exception as exc:  # 어떤 경우에도 앱을 멈추지 않음
            self.errors += 1
            self.last_error = repr(exc)
            if self.hand is not None:
                self.hand.issue("tracker_error", repr(exc)[:200])
            out = []
        self.last_ms = (time.perf_counter() - t0) * 1000
        self._emit(out)
        return out

    def flush(self, t=None, reason="flush"):
        """진행 중 핸드를 닫아 기록 반환 (없으면 [])."""
        try:
            out = self._finish(t if t is not None else (self.last_t or time.time()), reason)
        except Exception as exc:
            self.errors += 1
            self.last_error = repr(exc)
            self.hand = None
            out = []
        self._emit(out)
        return out

    def live(self):
        """진행 중 핸드의 좌석별 액션 (handread.estimate 의 seats 입력 모양) → {seat_key: {...}} 또는 {}.
        preflop: blind open limp call 3bet call_3bet 4bet jam call_jam check fold,
        포스트플랍: check bet call raise checkraise allin fold."""
        try:
            h = self.hand
            return to_handread(h.actions, {k: (s.pos.most_common(1)[0][0] if s.pos else None)
                                           for k, s in h.seats.items()}) if h else {}
        except Exception as exc:
            self.errors += 1
            self.last_error = repr(exc)
            return {}

    def new_session(self, session=None, t=None):
        """새 보정/새 테이블: 진행 중 핸드를 닫고 세션 id 를 바꿈 (잘린 닉네임 기억은 세션 단위)."""
        out = self.flush(t, "new_session")
        self.session = session or time.strftime("%Y%m%d_%H%M%S")
        return out

    def _emit(self, recs):
        if self.on_hand is None:
            return
        for r in recs:
            try:
                self.on_hand(r)
            except Exception as exc:
                self.errors += 1
                self.last_error = repr(exc)

    # --- 프레임 처리 ------------------------------------------------------------------------------------
    def _update(self, state, hero_cards, board, names, t, pot, shown):
        out = []
        if self.hand is not None and self.last_t is not None and t - self.last_t > STALE_HAND:
            out += self._finish(self.last_t, "stale")
        if not isinstance(state, dict) or not state.get("seats") or "error" in state or "empty" in state:
            return out
        board = [c for c in board if _is_card(c)]
        hero = [c for c in hero_cards if _is_card(c)][:2]
        seats = {s["key"]: s for s in state["seats"] if isinstance(s, dict) and "key" in s}
        dealt = [k for k, s in seats.items() if s.get("dealt")]

        h = self.hand
        if h is not None:
            reason = self._boundary(h, state, seats, hero, board)
            if reason:
                out += self._finish(self.last_t if self.last_t is not None else t, reason)
                h = None
        if h is None:
            if len(dealt) < 2:
                self.last_t = t
                return out
            h = self._start(state, seats, board, t)
        if t - h.t1 > GAP_FATAL and h.frames:
            h.issue("gap", round(t - h.t1, 1))
        h.t1 = t
        h.frames += 1
        self.last_t = t
        if len(hero) == 2 and h.hero_cards is None:
            h.hero_cards = hero
        if len(board) > len(h.board):
            h.board = board[:5]
        if _num(pot) is not None:
            h.pot = _num(pot)
        if shown:
            for k, cards in shown.items():
                if cards and all(_is_card(c) for c in cards):
                    h.shown[k] = list(cards)
        if state.get("dealer"):
            h.dealer[state["dealer"]] += 1
        if state.get("pos_players"):
            h.pos_players[state["pos_players"]] += 1
        for k, s in seats.items():
            if not s.get("dealt"):
                continue
            seat = h.seats.get(k)
            if seat is None:
                if h.frames > 1 and h.street > 0:
                    continue  # 핸드 중간에 새로 카드를 받은 듯 보이는 자리는 무시 (오판)
                seat = h.seats[k] = _Seat(k)
                seat.bubble = s.get("bubble")
            seat.slot = s.get("slot", seat.slot)
            if s.get("pos"):
                seat.pos[s["pos"]] += 1
            nm = names.get(k)
            if nm and nm[0]:
                seat.names[(nm[0], bool(nm[1]))] += 1
        for kind in ("sb", "bb"):
            k = (state.get("blind_seats") or {}).get(kind)
            if k:
                h.blind_keys[kind] = k

        # 스트리트
        street = BOARD_STREET.get(len(board))
        if street is not None and street > h.street:
            while h.street < street:
                self._close_street(h)
                self._open_street(h, seats)
        if self._suspect():
            h.notes.append("skipped a frame (possible new hand)")
        elif not h.over:
            self._actions(h, seats)
        return out

    def _start(self, state, seats, board, t):
        self.count += 1
        h = _Hand(f"{self.session}-{self.count:04d}", self.session, t)
        street = BOARD_STREET.get(len(board), 0)
        if board:
            h.issue("mid_hand_start", f"board {len(board)}")
            h.street = street
            h.cur_max = 0.0
        levels = sorted({round(_num(s.get("bet")), 2) for s in seats.values()
                         if _num(s.get("bet")) is not None and _num(s.get("bet")) > BB_AMOUNT + EPS})
        if not board and len(levels) >= 2:
            h.issue("mid_hand_start", f"{len(levels)} raises already")
        elif not board and levels:
            h.notes.append("joined after the open")
        h.inhand_start = {h.street: None}
        self.hand = h
        self._board_empty = 0
        self._dealer_new = (None, 0)
        self._hero_new = (None, 0)
        return h

    def _boundary(self, h, state, seats, hero, board):
        """새 핸드가 시작됐으면 이유 문자열."""
        if len(hero) == 2 and h.hero_cards and set(hero) != set(h.hero_cards):
            # 카드 한 장 오인식으로 핸드가 갈라지지 않게 같은 새 카드가 2프레임 연속일 때
            prev, n = self._hero_new
            self._hero_new = (set(hero), n + 1 if prev == set(hero) else 1)
            if self._hero_new[1] >= 2:
                return "hero cards changed"
        else:
            self._hero_new = (None, 0)
        if h.board and not board:
            self._board_empty += 1
            if self._board_empty >= 2:
                return "board cleared"
        else:
            self._board_empty = 0
        d = state.get("dealer")
        cur = h.dealer.most_common(1)[0][0] if h.dealer else None
        if d and cur and d != cur:
            prev, n = self._dealer_new
            self._dealer_new = (d, n + 1 if prev == d else 1)
            if self._dealer_new[1] >= 2:
                return "dealer moved"
        else:
            self._dealer_new = (None, 0)
        unfolded = [k for k, s in seats.items() if k in h.seats and h.seats[k].folded
                    and s.get("dealt") and not s.get("folded") and s.get("in_hand", True)]
        if len(unfolded) >= 2:
            return "folded seats dealt again"
        if h.over:
            in_hand = [s for s in seats.values() if s.get("in_hand")]
            bets = {k: _num(s.get("bet")) for k, s in seats.items() if _num(s.get("bet"))}
            if len(in_hand) >= 2 and bets and max(bets.values()) <= BB_AMOUNT + EPS and not board and \
                    len(in_hand) > len(h.live()):
                return "new blinds after hand over"
        return None

    # --- 행동 순서 -----------------------------------------------------------------------------------
    def _order(self, h, street):
        keys = list(h.seats)
        pos = {k: (h.seats[k].pos.most_common(1)[0][0] if h.seats[k].pos else None) for k in keys}
        n = len(keys)
        if all(pos.values()):
            if n == 2 and set(pos.values()) == {"SB", "BB"}:
                pre = sorted(keys, key=lambda k: pos[k] != "SB")
                return pre if street == 0 else pre[::-1]
            pre = sorted(keys, key=lambda k: PRE_RANK.index(pos[k]) if pos[k] in PRE_RANK else 99)
            if street == 0:
                return pre
            return [k for k in pre if pos[k] in ("SB", "BB")] + [k for k in pre if pos[k] not in ("SB", "BB")]
        # 포지션을 모름: 화면 순서(시계 방향, 내 왼쪽부터) + 딜러
        dealer = h.dealer.most_common(1)[0][0] if h.dealer else None
        if dealer in keys:
            i = keys.index(dealer)
            post = keys[i + 1:] + keys[:i + 1]
        else:
            h.issue("positions_unknown")
            post = keys
        if street == 0 and n > 2:
            return post[2:] + post[:2]
        return post

    def _blind_kind(self, h, k):
        seat = h.seats[k]
        p = seat.pos.most_common(1)[0][0] if seat.pos else None
        if p in ("SB", "BB"):
            return p
        if h.blind_keys.get("sb") == k:
            return "SB"
        if h.blind_keys.get("bb") == k:
            return "BB"
        return None

    # --- 액션 추론 -----------------------------------------------------------------------------------
    def _actions(self, h, seats):
        order = self._order(h, h.street)
        if not order:
            return
        bets, folds, checks = [], [], []
        for k in order:
            seat, s = h.seats[k], seats.get(k)
            if s is None or seat.folded:
                continue
            bubble = s.get("bubble")
            new_bubble = bubble is not None and bubble != seat.bubble
            seat.bubble = bubble
            b = _num(s.get("bet"))
            if b is not None and seat.stale is not None:
                if abs(b - seat.stale) <= EPS:
                    b = None              # 지난 스트리트 칩이 아직 보임
                else:
                    seat.stale = None
            elif b is None and seat.stale is not None and not s.get("bet"):
                seat.stale = None
            allin = bool(s.get("allin")) or bubble == "allin"
            if b is not None and b > seat.inv + EPS:
                bets.append((k, b, allin, bubble))
            elif b is not None and b < seat.inv - EPS and b > 0:
                others = any(_num(o.get("bet")) for kk, o in seats.items() if kk != k)
                if others:
                    h.issue("bet_decreased", f"{k} {seat.inv:g}->{b:g}")
            elif allin and not seat.allin and b is None and new_bubble:
                h.issue("allin_unread", k)
                seat.allin = True
            if s.get("folded") or bubble == "fold":
                folds.append(k)
            elif new_bubble and bubble == "check":
                checks.append(k)

        start = (order.index(h.last_actor) + 1) if h.last_actor in order else 0

        def cyc(k):
            return (order.index(k) - start) % len(order)
        bets.sort(key=lambda e: cyc(e[0]))
        bet_keys = {e[0] for e in bets}
        # 모두 동시에 폴드 = 핸드가 끝나 화면이 정리된 것 (폴드 아님)
        live = h.live()
        if folds and len(live) - len(set(folds)) < 1:
            h.over = True
            folds = []
        events = [("bet", e) for e in bets] + [("fold", k) for k in folds if k not in bet_keys] + \
                 [("check", k) for k in checks]
        events.sort(key=lambda ev: cyc(ev[1][0] if ev[0] == "bet" else ev[1]))
        deferred, this_frame_raisers = [], []
        for kind, e in events:
            if kind == "bet":
                k, b, allin, bubble = e
                if bubble == "call" and b > h.cur_max + EPS and len(bets) > 1:
                    deferred.append(e)
                    continue
                if self._bet(h, k, b, allin, bubble, "bet") in ("raise", "bet"):
                    this_frame_raisers.append(k)
            elif kind == "fold":
                self._fold(h, e)
            else:
                self._check(h, e)
        for k, b, allin, bubble in deferred:
            seat = h.seats[k]
            if b <= h.cur_max + EPS and this_frame_raisers and not seat.acted and \
                    any(cyc(r) > cyc(k) for r in this_frame_raisers):
                # 레이즈보다 앞 순서인데 그 레이즈에 콜 → 이 프레임 사이에 두 번 행동 (첫 행동은 모름)
                h.issue("ambiguous_order", k)
                self._record(h, k, "unknown", None, False, "inferred")
            self._bet(h, k, b, allin, bubble, "bet+bubble")
        for k in folds:
            if k in bet_keys:
                self._fold(h, k)
        if len(h.live()) <= 1:
            h.over = True
        elif sum(1 for s in h.live() if not s.allin) <= 1 and all(
                s.inv >= h.cur_max - EPS or s.allin for s in h.live()):
            h.allin_runout = any(s.allin for s in h.live())

    def _record(self, h, k, act, amount, allin, src, **extra):
        seat = h.seats[k]
        a = {"seat": k, "act": act, "src": src, "facing": h.cur_max > seat.inv + EPS,
             "raises_before": h.raises if h.street == 0 else None}
        if amount is not None:
            a["to"] = round(amount, 2)
        if allin:
            a["allin"] = True
        a.update(extra)
        h.actions[STREETS[h.street]].append(a)
        if act != "post":
            seat.acted = True
            h.last_actor = k
        return a

    def _bet(self, h, k, b, allin, bubble, src):
        seat = h.seats[k]
        if h.street == 0 and not seat.posted and not seat.acted:
            kind = self._blind_kind(h, k)
            if kind:
                amt = SB_AMOUNT if kind == "SB" else BB_AMOUNT
                seat.posted = True
                self._record(h, k, "post", min(b, amt), allin and b <= amt + EPS, "bet", blind=kind)
                seat.inv = min(b, amt)
                if b <= amt + EPS:
                    seat.allin = seat.allin or allin
                    return "post"
        if b > h.cur_max + EPS:
            act = "bet" if h.street > 0 and h.cur_max <= EPS else "raise"
            if bubble == "call":
                h.issue("bubble_conflict", f"{k} call bubble but raised to {b:g}")
            self._record(h, k, act, b, allin, src)
            if h.street == 0:
                h.raises += 1
            h.cur_max = b
        else:
            if b < h.cur_max - EPS and not allin:
                h.issue("short_call", f"{k} {b:g} < {h.cur_max:g}")
            self._record(h, k, "call", b, allin, src)
        seat.inv = b
        seat.allin = seat.allin or allin
        return h.actions[STREETS[h.street]][-1]["act"]

    def _fold(self, h, k):
        seat = h.seats[k]
        if seat.folded:
            return
        self._record(h, k, "fold", None, False, "grey/bubble")
        seat.folded = True
        seat.fold_street = h.street

    def _check(self, h, k):
        seat = h.seats[k]
        if h.cur_max <= seat.inv + EPS:
            self._record(h, k, "check", None, False, "bubble")

    # --- 스트리트 경계 -------------------------------------------------------------------------------
    def _close_street(self, h):
        name = STREETS[h.street]
        acts = h.actions[name]
        order = self._order(h, h.street)
        started = h.inhand_start.get(h.street)
        if started is None:
            started = set(order)
        first_aggr = next((i for i, a in enumerate(acts) if a["act"] in ("bet", "raise")), None)
        inserted = []
        for k in order:
            seat = h.seats[k]
            if k not in started or (seat.folded and seat.fold_street != h.street):
                continue
            mine = [i for i, a in enumerate(acts) if a["seat"] == k and a["act"] != "post"]
            allin_before = seat.allin and not mine
            if allin_before:
                continue
            if not mine:
                if seat.folded or len(h.live()) <= 1:
                    continue  # 폴드했거나, 나머지가 모두 폴드해서 행동할 필요가 없었음
                if h.street == 0 and not seat.posted and self._blind_kind(h, k) == "BB":
                    seat.inv = BB_AMOUNT  # 블라인드 칩을 못 읽었어도 BB 는 1BB 를 냈음
                if h.cur_max <= seat.inv + EPS:
                    inserted.append((len(acts), self._mk(k, "check", "inferred", h, seat)))
                else:
                    a = self._mk(k, "call", "inferred", h, seat)
                    a["to"] = round(h.cur_max, 2)
                    inserted.append((len(acts), a))
                    h.issue("inferred_call", f"{name} {k}")
                    seat.inv = h.cur_max
            elif h.street > 0 and first_aggr is not None and mine[0] > first_aggr:
                bettor = acts[first_aggr]["seat"]
                if bettor in order and order.index(k) < order.index(bettor):
                    a = self._mk(k, "check", "inferred", h, seat)
                    a["facing"] = False
                    inserted.append((first_aggr, a))
            if seat.inv < h.cur_max - EPS and not seat.folded and not seat.allin and mine:
                a = self._mk(k, "call", "inferred", h, seat)
                a["to"] = round(h.cur_max, 2)
                inserted.append((len(acts), a))
                h.issue("inferred_call", f"{name} {k}")
                seat.inv = h.cur_max
        if inserted:  # 추정 액션을 제자리에 (같은 자리면 행동 순서대로)
            idx = {k: i for i, k in enumerate(order)}
            rebuilt = []
            for j in range(len(acts) + 1):
                rebuilt += sorted((a for p, a in inserted if p == j), key=lambda a: idx.get(a["seat"], 99))
                if j < len(acts):
                    rebuilt.append(acts[j])
            acts[:] = rebuilt

    @staticmethod
    def _mk(k, act, src, h, seat):
        return {"seat": k, "act": act, "src": src, "facing": h.cur_max > seat.inv + EPS,
                "raises_before": h.raises if h.street == 0 else None}

    def _open_street(self, h, seats):
        h.street += 1
        h.cur_max = 0.0
        h.last_actor = None
        h.inhand_start[h.street] = {k for k, s in h.seats.items() if not s.folded}
        for k, seat in h.seats.items():
            seat.stale = seat.inv if seat.inv > 0 else None
            seat.inv = 0.0
            seat.acted = False
            s = seats.get(k)
            seat.bubble = s.get("bubble") if s else None  # 지난 스트리트 말풍선은 새 액션이 아님

    # --- 핸드 마감 -----------------------------------------------------------------------------------
    def _finish(self, t, reason):
        h = self.hand
        self.hand = None
        self._board_empty = 0
        self._dealer_new = (None, 0)
        self._hero_new = (None, 0)
        if h is None:
            return []
        if h.frames < 2 and not any(h.actions.values()):
            return []  # 한 프레임만 보고 끝남: 기록할 것 없음
        if reason in ("flush", "stale", "new_session") and not h.over and h.street < 3:
            h.issue("incomplete", reason)
        if "incomplete" not in {i["code"] for i in h.issues}:
            self._close_street(h)  # 끝까지 못 본 핸드는 남은 액션을 추정하지 않음
        live = [k for k, s in h.seats.items() if not s.folded]
        showdown = len(live) >= 2 and (h.street == 3 or h.allin_runout) and "incomplete" not in \
            {i["code"] for i in h.issues}
        rec = {
            "id": h.id, "session": h.session, "start": round(h.t0, 3), "end": round(h.t1, 3),
            "frames": h.frames, "end_reason": reason,
            "players": len(h.seats),
            "pos_players": h.pos_players.most_common(1)[0][0] if h.pos_players else None,
            "dealer": h.dealer.most_common(1)[0][0] if h.dealer else None,
            "hero_cards": h.hero_cards, "board": h.board, "pot": h.pot,
            "streets": STREETS[h.street],
            "actions": {s: a for s, a in h.actions.items() if a},
            "showdown": {"seats": live, "shown": {k: v for k, v in h.shown.items()}} if showdown else None,
            "seats": {},
            "issues": h.issues,
            "notes": h.notes,
        }
        for k, seat in h.seats.items():
            name = seat.names.most_common(1)[0][0] if seat.names else (None, False)
            rec["seats"][k] = {
                "name": name[0], "truncated": bool(name[1]), "hero": seat.hero,
                "pos": seat.pos.most_common(1)[0][0] if seat.pos else None, "slot": seat.slot,
                "folded": STREETS[seat.fold_street] if seat.folded and seat.fold_street is not None else None,
                "allin": seat.allin, "shown": h.shown.get(k),
            }
        derive(rec)
        codes = {i["code"] for i in h.issues}
        rec["confidence"] = "low" if codes & FATAL else "ok"
        return [rec]


# --- 기록 → 좌석별 통계 플래그 ------------------------------------------------------------------------
def derive(rec):
    """rec['seats'][k]['stats'] 를 채움: 이번 핸드에서의 카운터 (profiles.Store 가 더함)."""
    acts = rec.get("actions") or {}
    reached = STREETS.index(rec.get("streets") or "preflop")
    sd = set((rec.get("showdown") or {}).get("seats") or [])
    for k, seat in rec["seats"].items():
        st = {"hands": 1, "vpip": 0, "pfr": 0, "threebet": 0, "threebet_opp": 0, "faced_bet": 0, "fold_to_bet": 0,
              "agg": 0, "calls": 0, "saw_flop": 0, "showdown": 0}
        pre = [a for a in acts.get("preflop", []) if a["seat"] == k and a["act"] != "post"]
        if any(a["act"] in ("call", "raise", "bet", "unknown") for a in pre):
            st["vpip"] = 1
        if any(a["act"] in ("raise", "bet") for a in pre):
            st["pfr"] = 1
        for a in pre:
            if a.get("raises_before") == 1 and a["act"] in ("call", "raise", "fold"):
                opener = next((x["seat"] for x in acts.get("preflop", []) if x["act"] == "raise"), None)
                if opener != k:
                    st["threebet_opp"] = 1
                    if a["act"] == "raise":
                        st["threebet"] = 1
                break
        folded_pre = seat.get("folded") == "preflop"
        st["saw_flop"] = int(reached >= 1 and not folded_pre)
        for street in STREETS[1:]:
            mine = [a for a in acts.get(street, []) if a["seat"] == k]
            first_facing = next((a for a in mine if a.get("facing")), None)
            if first_facing is not None:
                st["faced_bet"] += 1
                st["fold_to_bet"] += int(first_facing["act"] == "fold")
            st["agg"] += sum(1 for a in mine if a["act"] in ("bet", "raise"))
            st["calls"] += sum(1 for a in mine if a["act"] == "call")
        st["showdown"] = int(k in sd)
        seat["stats"] = st
    return rec


def to_handread(actions, pos):
    """액션 목록({street: [action]}) + {seat: pos} → handread.estimate 의 seats 모양."""
    out = {k: {"pos": p, "preflop": [], "flop": [], "turn": [], "river": []} for k, p in pos.items()}
    opener = None
    for a in actions.get("preflop", []):
        k, act, rb = a["seat"], a["act"], a.get("raises_before") or 0
        if k not in out:
            continue
        if act == "post":
            word = "blind"
        elif act == "raise":
            word = "jam" if a.get("allin") else ("open", "3bet", "4bet")[min(rb, 2)]
            if rb == 0:
                opener = pos.get(k)
        elif act == "call":
            word = "call_jam" if a.get("allin") and rb >= 1 else ("limp", "call", "call_3bet")[min(rb, 2)]
        elif act in ("check", "fold"):
            word = act
        else:
            continue
        out[k]["preflop"].append(word)
    for street in STREETS[1:]:
        for a in actions.get(street, []):
            k, act = a["seat"], a["act"]
            if k not in out or act not in ("check", "bet", "call", "raise", "fold"):
                continue
            seq = out[k][street]
            word = "allin" if a.get("allin") and act in ("bet", "raise") else act
            if act == "raise" and "check" in seq:
                word = "checkraise"
            seq.append(word)
    for k in out:
        if opener and out[k]["pos"] != opener:
            out[k]["opener"] = opener
    return out
