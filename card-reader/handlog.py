"""핸드 기록: 약 1초마다 받는 테이블 상태(tablestate.read 결과)에서 핸드 경계와 좌석별 액션을 추론한다.

    tracker = handlog.Tracker(session="20261029_0102")
    for each frame (~1 s):
        done = tracker.update(state, hero_cards, board, names, t, pot=None, shown=None, types=None)
        # shown: 쇼다운 공개 카드 {seat_key: ['9c', '7c']} (showdown.Watcher). 진행 중 핸드에 넣음, None 이면 그 좌석 값을 지움
        # types: 좌석별 상대 타입 {seat_key: {"type": "LP", "type_src": "popup", "type_stats": {...}}} (seattypes.SeatTypes)
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
차례 고리 (state['acting_seat'], 있으면)
  - rec['turns']: 스트리트별 차례 [{street, seat, t0, t1, p(본 최대 진행도)}] — 결정 시간(텔) 분석용
  - 고리가 다음 사람으로 넘어갔는데 그 사람의 새 액션을 RING_GRACE 프레임 안에 못 읽음 → issue 'ring_no_action' (참고용,
    액션을 지어내지 않음)
믿을 수 없는 핸드는 confidence='low' + issues (profiles.Store 는 통계에 안 넣고 JSONL 에만 표시해 저장)

닉네임은 기록에 넣지 않는다 (디스크에 남기지 않음). 좌석 기록에는 자리 키·포지션, 상대 '타입'(게임 유저 정보 팝업의
LAG/LP/TAG/TP, seat['type'] + seat['type_src']='popup'), 세션 가명 seat['pid'] 만.
  pid = "p_" + HMAC-SHA256(세션 소금, 정규화한 닉네임) 앞 8글자 (16진수). 소금은 Tracker 세션마다(앱 시작/새 [자동 지정]/
  테이블 이동) 새로 만든 난수이고 메모리에만 있음 → 같은 세션 안에서는 자리를 옮겨도 같은 pid, 세션이 다르면 연결 불가,
  닉네임을 되살릴 수 없음. 이 핸드에서 잘리지 않은 같은 닉네임이 PID_MIN_FRAMES 프레임 이상, 다른 닉네임 없이 읽혔을 때만
  (잘린 이름·한 번이라도 잘려 읽힌 이름·엇갈린 이름이면 None). 앱은 팝업으로 확인한 좌석의 잘린 이름을 전체 이름으로 바꿔 넘긴다.
이번 프레임들에서 읽은 닉네임은 메모리에만 두고 names_for(hand_id) 로 (앱 실행 중에만) 꺼내 쓸 수 있다.

절대 예외를 밖으로 내지 않는다 (update/flush 는 실패하면 [] 반환, 진행 중 핸드는 low 로 표시).
"""
import hashlib
import hmac
import os
import time
import unicodedata
from collections import Counter, OrderedDict

STREETS = ("preflop", "flop", "turn", "river")
BOARD_STREET = {0: 0, 3: 1, 4: 2, 5: 3}
EPS = 0.05                 # 같은 금액으로 보는 차이 (BB)
WOBBLE_BB, WOBBLE_FRAC = 0.15, 0.10   # 직전 콜보다 이만큼(BB 또는 비율) 안에서 늘어난 벳 = 같은 콜을 다시 읽음 (최소 레이즈는 1BB 이상)
SB_AMOUNT, BB_AMOUNT = 0.5, 1.0
GAP_FATAL = 8.0            # 핸드 중 프레임 간격이 이보다 길면 놓친 액션이 있을 수 있음 → low
STALE_HAND = 90.0          # 이만큼 새 프레임이 없으면 진행 중 핸드를 닫음
TURN_GAP = 3.5             # 같은 자리의 차례 고리가 이 간격 안에 다시 보이면 같은 차례
PID_MIN_FRAMES = 2         # pid 를 붙이려면 같은 (잘리지 않은) 닉네임이 이 프레임 수 이상
NAMES_KEEP = 16           # names_for() 로 기억해 두는 최근 핸드 수 (메모리 전용)
RING_GRACE = 2             # 고리가 다음 사람으로 넘어간 뒤 이 프레임 수 안에 액션이 안 읽히면 ring_no_action
PRE_RANK = ("UTG", "U1", "U2", "U3", "MP", "LJ", "HJ", "CO", "BTN", "SB", "BB")
# 이 문제가 하나라도 있으면 confidence='low' (통계에 안 넣음). 나머지(inferred_call, positions_unknown,
# bubble_conflict, short_call, ring_no_action)는 참고용
FATAL = {"mid_hand_start", "bet_decreased", "ambiguous_order", "tracker_error", "gap", "allin_unread", "incomplete"}


def _is_card(c):
    return isinstance(c, str) and len(c) == 2 and c[0] in "23456789TJQKA" and c[1] in "shdc"


def norm_name(name):
    """pid 용 닉네임 정규화: 유니코드 NFC, 공백 제거, 소문자."""
    return "".join(unicodedata.normalize("NFC", str(name or "")).split()).lower()


def make_pid(salt, name):
    """세션 가명: 'p_' + HMAC-SHA256(salt, 정규화 닉네임) 16진 앞 8글자. 이름이 비면 None."""
    n = norm_name(name)
    if not n or not salt:
        return None
    return "p_" + hmac.new(salt, n.encode("utf-8"), hashlib.sha256).hexdigest()[:8]


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


class _Seat:
    __slots__ = ("key", "slot", "pos", "names", "folded", "fold_street", "allin", "inv", "posted", "stale",
                 "bubble", "hero", "acted", "type")

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
        self.type = None        # 상대 타입 {"type", "type_src", "type_stats"} (이 핸드에서 마지막으로 본 값)


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
        self.street_pot = {}     # 플랍/턴/리버 시작 때 화면 팟 (BB, 앤티 포함; 앞 스트리트 벳이 모인 뒤 ~ 이번 스트리트 첫 벳 전)
        self._sp_frozen = set()
        self.shown = {}
        self.inhand_start = {0: None}
        self.over = False        # 남은 사람 1명 (나머지 폴드) 또는 화면상 핸드 종료
        self.allin_runout = False
        self.blind_keys = {}
        self.pos_players = Counter()
        self.late = {}           # 핸드 중간에 처음 카드를 받은 듯 보인 자리 -> 연속 프레임 수 (2프레임째에 추가)
        self.turns = []          # 차례 고리 (tablestate acting_seat): {street, seat, t0, t1, p} — 결정 시간 분석용
        self.ring_wait = {}      # 고리가 지나간 자리 -> (스트리트, 남은 프레임, 차례 시작 때 액션 수): 새 액션이 안 읽히면 issue
        self.turn_acts = 0
        self.dropped = set()     # 핸드에 없다고 판단해 뺀 좌석 (OrderGuard: 버튼과 SB 사이의 흑백 좌석) — 다시 넣지 않음

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
        self._names = OrderedDict()   # hand_id -> {seat: (name, truncated)} — 메모리 전용, 파일에 안 씀
        self._salt = os.urandom(16)   # pid 소금: 세션마다 새로, 메모리에만 (절대 파일에 쓰지 않음)

    def seat_pid(self, seat):
        """_Seat 의 세션 가명 (닉네임이 안정적으로 읽혔을 때만, 아니면 None)."""
        full = Counter()
        for (n, tr), c in seat.names.items():
            if n and not tr:
                full[norm_name(n)] += c
        if len(full) != 1:
            return None   # 못 읽음 / 잘린 이름뿐 / 서로 다른 이름이 섞임
        n, c = full.most_common(1)[0]
        if any(tr and norm_name(m) and n.startswith(norm_name(m)) for (m, tr) in seat.names):
            return None   # 같은 이름이 잘려('…') 읽힌 적 있음 = 점을 놓친 잘린 이름일 수 있음 (다른 사람과 겹칠 수 있음)
        return make_pid(self._salt, n) if c >= PID_MIN_FRAMES else None

    def names_for(self, hand_id):
        """끝난 핸드의 좌석별 닉네임 {seat: (name, truncated)} (메모리에만 있음, 최근 NAMES_KEEP 핸드). 없으면 {}."""
        return dict(self._names.get(hand_id) or {})

    def _suspect(self):
        """새 핸드일 수 있는 신호가 1프레임 보임 → 이 프레임의 액션은 처리하지 않음."""
        return bool(self._board_empty or self._dealer_new[1] or self._hero_new[1])

    # --- 공개 API ------------------------------------------------------------------------------------
    def update(self, state, hero_cards=None, board=None, names=None, t=None, pot=None, shown=None, types=None):
        """프레임 하나 → 이번에 끝난 핸드 기록 리스트 (대개 빈 리스트)."""
        t0 = time.perf_counter()
        t = time.time() if t is None else float(t)
        try:
            out = self._update(state, hero_cards or [], board or [], names or {}, t, pot, shown, types or {})
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
        preflop: blind open limp call 3bet call_3bet 4bet jam call_jam check fold (to_handread 가 읽기 오류를 정리),
        포스트플랍: check bet call raise checkraise allin fold (금액을 알면 {"a": "bet", "size": 팟 대비}).
        올인/올인콜 좌석에는 'stack_bb' (실제 올인 금액)."""
        try:
            h = self.hand
            return to_handread(h.actions, {k: (s.pos.most_common(1)[0][0] if s.pos else None)
                                           for k, s in h.seats.items()},
                               street_pot=street_pot_named(h.street_pot)) if h else {}
        except Exception as exc:
            self.errors += 1
            self.last_error = repr(exc)
            return {}

    def showdown_watch(self):
        """쇼다운 공개 카드를 찾아볼 만한가 → (핸드 id, 남은 상대 좌석 키 목록) / None.
        리버까지 왔거나 남은 사람 중 올인이 있고, 남은 사람이 2명 이상(상대 1명 이상)일 때만 (평소 프레임은 비용 0).
        남은 사람 = 폴드 안 함 + 이번 스트리트에 '폴드'로 잡힘 (공개 카드가 아바타를 덮어 흑백으로 보였을 수 있음)."""
        try:
            h = self.hand
            if h is None:
                return None
            live = [s for s in h.seats.values() if not s.folded or s.fold_street == h.street]
            # 포스트플랍에서 이번 스트리트에 흑백이 된 좌석 = 공개 카드일 수 있음 (올인을 못 읽은 턴 올인 쇼다운 등)
            greyed = h.street > 0 and any(s.folded for s in live)
            if len(live) < 2 or not (h.street == 3 or h.allin_runout or any(s.allin for s in live) or greyed):
                return None
            opps = [s.key for s in live if not s.hero]
            return (h.id, opps) if opps else None
        except Exception as exc:
            self.errors += 1
            self.last_error = repr(exc)
            return None

    def new_session(self, session=None, t=None):
        """새 보정/새 테이블: 진행 중 핸드를 닫고 세션 id 를 바꿈 (잘린 닉네임 기억은 세션 단위)."""
        out = self.flush(t, "new_session")
        self.session = session or time.strftime("%Y%m%d_%H%M%S")
        self._salt = os.urandom(16)   # 새 세션: 이전 세션 pid 와 연결되지 않게
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
    def _update(self, state, hero_cards, board, names, t, pot, shown, types):
        out = []
        if self.hand is not None and self.last_t is not None and t - self.last_t > STALE_HAND:
            out += self._finish(self.last_t, "stale")
        if not isinstance(state, dict) or not state.get("seats") or "error" in state or "empty" in state:
            return out
        if state.get("layout_ok") is False:
            return out  # 화면이 보정과 어긋남 (tablestate): 좌석 판정이 엉터리라 이 프레임은 버림
        board = [c for c in board if _is_card(c)]
        hero = [c for c in hero_cards if _is_card(c)][:2]
        seats = {s["key"]: s for s in state["seats"] if isinstance(s, dict) and "key" in s}
        dealt = [k for k, s in seats.items() if s.get("dealt")]

        h = self.hand
        if h is not None and shown:
            # 공개 카드는 화면에 보이는 동안의 핸드 것: 이 프레임에서 새 핸드로 넘어가더라도 끝나는 핸드에 넣는다
            for k, cards in shown.items():
                if cards is None:
                    h.shown.pop(k, None)   # 앞서 읽은 값이 다른 프레임과 어긋남 → 버림 (showdown.Watcher)
                elif len(cards) == 2 and all(_is_card(c) for c in cards) and cards[0] != cards[1]:
                    h.shown[k] = list(cards)
                    self._unfold_shown(h, k)
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
        if state.get("dealer"):
            h.dealer[state["dealer"]] += 1
        if state.get("pos_players"):
            h.pos_players[state["pos_players"]] += 1
        late = {}
        for k, s in seats.items():
            if not s.get("dealt"):
                continue
            seat = h.seats.get(k)
            if seat is None:
                if k in h.dropped:
                    continue
                if h.frames > 1 and h.street > 0:
                    continue  # 핸드 중간에 새로 카드를 받은 듯 보이는 자리는 무시 (오판)
                if h.frames > 1:
                    # 프리플랍 중간에 처음 보인 자리 (딜링 애니메이션): 2프레임 연속일 때만. 한 프레임짜리 오판(아이콘/흑백)을
                    # 넣으면 행동 없이 남아서 스트리트 끝에 콜로 추정되고(VPIP), 남은 사람으로 세여 핸드가 안 끝남
                    late[k] = h.late.get(k, 0) + 1
                    if late[k] < 2:
                        continue
                seat = h.seats[k] = _Seat(k)
                seat.bubble = s.get("bubble")
            seat.slot = s.get("slot", seat.slot)
            if s.get("pos"):
                seat.pos[s["pos"]] += 1
            nm = names.get(k)
            if nm and nm[0]:
                seat.names[(nm[0], bool(nm[1]))] += 1
            ty = types.get(k)
            if isinstance(ty, dict) and ty.get("type_src"):
                seat.type = ty
        h.late = late
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
        if _num(pot) is not None:
            self._street_pot(h, seats, _num(pot))   # 스트리트 전환 뒤에 (새 스트리트 팟을 앞 스트리트에 넣지 않게)
        if self._suspect():
            h.notes.append("skipped a frame (possible new hand)")
        elif not h.over:
            self._actions(h, seats)
            self._turns(h, state, t)
        return out

    @staticmethod
    def _street_pot(h, seats, pot):
        """포스트플랍 스트리트 시작 팟: 좌석 앞에 벳이 하나도 안 보이는 프레임의 화면 팟 중 마지막 값 (그 스트리트 첫 벳이
        보이면 확정). 화면 팟은 앤티를 포함하므로 handlog.bet_sizes 가 앱 어드바이저와 같은 팟 기준을 쓸 수 있다."""
        st = h.street
        if st < 1 or st in h._sp_frozen or pot <= 0:
            return
        if any((_num(s.get("bet")) or 0) > EPS for s in seats.values()):
            if st in h.street_pot:
                h._sp_frozen.add(st)
        else:
            h.street_pot[st] = pot

    def _turns(self, h, state, t):
        """차례 고리 기록 + 고리가 지나간 자리인데 RING_GRACE 프레임 안에 액션(벳/폴드/체크 말풍선)을 못 읽었으면
        issue 'ring_no_action' (참고용 — 액션을 지어내지 않음. 스트리트가 끝나면 _close_street 가 하던 대로 추정)."""
        def n_acts(key):
            return sum(1 for a in h.actions[STREETS[h.street]] if a["seat"] == key and a["act"] != "post")
        for k, (street, left, n0) in list(h.ring_wait.items()):
            seat = h.seats.get(k)
            if street != h.street or seat is None or seat.folded or n_acts(k) > n0:
                del h.ring_wait[k]
            elif left <= 1:
                h.issue("ring_no_action", f"{k}@{STREETS[street]}")
                del h.ring_wait[k]
            else:
                h.ring_wait[k] = (street, left - 1, n0)
        k = state.get("acting_seat")
        if not k or k not in h.seats:
            return
        p = _num(state.get("acting_progress"))
        cur = h.turns[-1] if h.turns else None
        if cur and cur["seat"] == k and cur["street"] == STREETS[h.street] and t - cur["t1"] <= TURN_GAP:
            cur["t1"] = round(t, 3)
            if p is not None:
                cur["p"] = max(cur["p"] or 0.0, p)
            return
        if cur and cur["seat"] != k and cur["street"] == STREETS[h.street] and cur["seat"] in h.seats:
            h.ring_wait[cur["seat"]] = (h.street, RING_GRACE, h.turn_acts)
        h.turn_acts = n_acts(k)  # 이 차례가 시작될 때까지 그 사람이 이번 스트리트에 한 액션 수
        h.turns.append({"street": STREETS[h.street], "seat": k, "t0": round(t, 3), "t1": round(t, 3), "p": p})

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
    def upcoming(self, after=None, n=2):
        """이번 스트리트에서 다음에 행동할 좌석 키 (행동 순서대로 최대 n개): after(없으면 마지막으로 행동한 사람) 다음부터
        폴드·올인 안 한 사람. 진행 중 핸드가 없거나 끝났으면 []. 앱이 기다리는 동안 이 좌석만 지켜본다 (기록은 안 바꿈)."""
        try:
            h = self.hand
            if h is None or h.over:
                return []
            order = self._order(h, h.street, quiet=True)
            last = after if after in order else h.last_actor
            start = order.index(last) + 1 if last in order else 0
            out = []
            for i in range(len(order)):
                k = order[(start + i) % len(order)]
                s = h.seats[k]
                if k == last or s.folded or s.allin:
                    continue
                out.append(k)
                if len(out) >= n:
                    break
            return out
        except Exception as exc:
            self.errors += 1
            self.last_error = repr(exc)
            return []

    def _order(self, h, street, quiet=False):
        keys = list(h.seats)
        dealer = h.dealer.most_common(1)[0][0] if h.dealer else None
        out, known = order_keys(keys, self.labels(h), dealer, street)
        if not known and not quiet:
            h.issue("positions_unknown")
        return out

    @staticmethod
    def labels(h):
        """핸드의 좌석별 포지션 (이번 핸드에서 가장 많이 본 값) {좌석: 포지션/None}."""
        return {k: (s.pos.most_common(1)[0][0] if s.pos else None) for k, s in h.seats.items()}

    def relabel(self, pos, dealer=None):
        """OrderGuard 가 다시 정한 포지션으로 이번 핸드를 고침: 좌석 포지션·딜러·블라인드 좌석을 바꾸고, 잘못된 포지션으로
        기록된 블라인드만 바로잡는다 (액션을 지어내지 않음):
          - 'SB 0.5 블라인드 → 콜 1' 로 기록된 진짜 BB = 'BB 1 블라인드' (가짜 콜 삭제)
          - 블라인드가 아닌 사람의 '콜 0.5'(SB 칩) / 첫 '콜 1'(BB 칩) = 그 블라인드
          - 블라인드 종류만 바뀐 것 (SB ↔ BB) 은 이름만, 블라인드가 아닌 사람의 '블라인드' 는 뺌
        → 고친 개수."""
        h = self.hand
        if h is None or not pos:
            return 0
        w = h.frames + 1
        for k, seat in h.seats.items():
            seat.pos = Counter({pos[k]: w}) if pos.get(k) else Counter()
        if dealer:
            h.dealer = Counter({dealer: w})
        self._dealer_new = (None, 0)
        n = len(pos)
        h.pos_players = Counter({min(10, max(6, n)): w})
        h.blind_keys = {kind.lower(): k for k, p in pos.items() for kind in ("SB", "BB") if p == kind}
        fixed = 0
        acts = h.actions["preflop"]
        keep = []
        for i, a in enumerate(acts):
            k, act = a.get("seat"), a.get("act")
            if a.get("_drop"):
                continue
            kind = pos.get(k) if pos.get(k) in ("SB", "BB") else None
            amt = SB_AMOUNT if kind == "SB" else BB_AMOUNT
            nxt = next((b for b in acts[i + 1:] if b.get("seat") == k), None)
            if act == "post":
                fixed += int(kind != a.get("blind"))
                if kind is None:
                    # 블라인드가 아닌 사람의 '블라인드' (잘못된 포지션이 만든 것): 바로 뒤에 같은 사람의 벳이 있으면 그 벳에 포함
                    # (첫 칩이 곧 레이즈 금액), 아니면 그 칩은 림프 = 콜
                    if k in h.seats:
                        h.seats[k].posted = False
                    if nxt is not None and nxt.get("act") in ("call", "raise"):
                        continue
                    a.pop("blind", None)
                    a.update(act="call", src="bet")
                    if k in h.seats:
                        h.seats[k].acted = True
                elif a.get("blind") != kind:
                    a["blind"] = kind
                    if kind == "BB" and nxt is not None and nxt.get("act") == "call" and \
                            abs((_num(nxt.get("to")) or 0) - BB_AMOUNT) <= EPS:
                        a["to"] = BB_AMOUNT       # 진짜 BB 를 SB 로 봐서 '0.5 블라인드 + 콜 1' 로 쪼갬
                        nxt["_drop"] = True
                keep.append(a)
                continue
            to = _num(a.get("to"))
            if kind and act == "call" and not any(b.get("seat") == k for b in keep) and to is not None and \
                    abs(to - amt) <= EPS:
                a.clear()   # 블라인드 칩을 콜로 기록함 → 블라인드
                a.update({"seat": k, "act": "post", "src": "bet", "facing": False, "raises_before": 0, "to": amt,
                          "blind": kind})
                if k in h.seats:
                    h.seats[k].acted = any(b.get("seat") == k and b.get("act") != "post" for b in acts[i + 1:])
                fixed += 1
            keep.append(a)
        acts[:] = keep
        for k, seat in h.seats.items():
            if any(a.get("seat") == k and a.get("act") == "post" for a in acts):
                seat.posted = True
        if h.street == 0:
            h.last_actor = next((a["seat"] for a in reversed(acts) if a.get("act") != "post"), None)
        return fixed

    def drop_seats(self, keys):
        """이번 핸드에 실제로는 없던 좌석을 뺌 (자리 비움·대기 중인 흑백 아바타가 '카드 받고 폴드' 로 보임 → 포지션이 한 칸 밀림).
        흑백/말풍선 폴드 말고 다른 액션이 있는 좌석은 빼지 않는다 (진짜 낀 사람). → 뺀 좌석 목록."""
        h = self.hand
        out = []
        for k in keys or ():
            seat = h.seats.get(k) if h is not None else None
            if seat is None or seat.hero:
                continue
            mine = [a for st in STREETS for a in h.actions[st] if a.get("seat") == k]
            if any(not (a.get("act") == "fold" and a.get("src") == "grey/bubble") for a in mine):
                continue
            for st in STREETS:
                h.actions[st][:] = [a for a in h.actions[st] if a.get("seat") != k]
            del h.seats[k]
            h.dropped.add(k)
            h.notes.append(f"{k}: not in this hand (sitting out?)")
            out.append(k)
        if out and h.last_actor in out:
            h.last_actor = next((a["seat"] for a in reversed(h.actions[STREETS[h.street]]) if a.get("act") != "post"), None)
        return out

    def unfold(self, k):
        """폴드로 기록된 좌석이 다시 행동함 (흑백 오판 등): 그 좌석의 마지막 액션이 이번 스트리트의 흑백/말풍선 폴드면 지움.
        → 지웠으면 True."""
        h = self.hand
        seat = h.seats.get(k) if h is not None else None
        if seat is None or not seat.folded or seat.fold_street != h.street or k in h.shown:
            return False
        acts = h.actions[STREETS[h.street]]
        mine = [i for i, a in enumerate(acts) if a.get("seat") == k]
        if not mine or acts[mine[-1]].get("act") != "fold" or acts[mine[-1]].get("src") != "grey/bubble":
            return False
        del acts[mine[-1]]
        seat.folded, seat.fold_street = False, None
        h.over = False
        h.notes.append(f"{k}: fold undone (acted again)")
        return True

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
            if (s.get("folded") or bubble == "fold") and k not in h.shown:
                # 공개 카드가 아바타를 덮으면 흑백(폴드)처럼 보인다: 카드를 보여 준 좌석은 폴드가 아님
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
        prev = next((a for a in reversed(h.actions[STREETS[h.street]]) if a["seat"] == k), None)
        if (b <= h.cur_max + EPS and prev is not None and prev["act"] == "call" and prev.get("to") is not None
                and prev["to"] < b <= prev["to"] + max(WOBBLE_BB, WOBBLE_FRAC * b)):
            # 같은 콜 금액을 다시 읽은 것 (OCR 0.9 → 1.0, 35.1 → 38): 새 액션이 아니다. 예전엔 '콜 0.9' 뒤에 레이즈가 나오면
            # '콜 1.0' 이 레이즈 뒤 두 번째 콜로 기록돼 림프가 '림프→콜' 등 엉뚱한 라인이 됐다 (2026-10-30 라이브 KK 핸드)
            prev["to"] = round(b, 2)
            seat.inv = b
            seat.allin = seat.allin or allin
            return "call"
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

    def _unfold_shown(self, h, k):
        """쇼다운에서 카드를 보여 준 좌석의 폴드(흑백/말풍선으로 추정, 그 좌석의 마지막 액션)를 되돌림.
        공개 카드가 아바타를 덮어 흑백으로 보여서 생긴 가짜 폴드 (카드를 읽기 전 프레임에 기록된 경우)."""
        seat = h.seats.get(k)
        if seat is None or not seat.folded or seat.fold_street is None:
            return
        name = STREETS[seat.fold_street]
        if any(a.get("seat") == k for s in STREETS[seat.fold_street + 1:] for a in h.actions[s]):
            return
        acts = h.actions[name]
        mine = [i for i, a in enumerate(acts) if a.get("seat") == k]
        if mine and acts[mine[-1]].get("act") == "fold" and acts[mine[-1]].get("src") == "grey/bubble":
            del acts[mine[-1]]
            seat.folded = False
            seat.fold_street = None
            h.notes.append(f"{k}: fold undone (cards shown)")

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
        elif h.street == 0 and not h.over and not h.allin_runout and len(h.live()) >= 2:
            # 보드 없이 2명 이상 남은 채 끝남 = 실제로는 없는 일 (마지막 폴드를 놓쳤거나 잘못된 핸드 경계).
            # 남은 사람을 콜로 추정하면 아직 행동 안 한 사람이 모두 VPIP 로 쌓임 → 통계에 넣지 않음
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
            "turns": h.turns,
        }
        if h.street_pot:   # 선택 키: 스트리트 시작 화면 팟(BB). 없으면 bet_sizes 는 앤티 없는 팟으로 계산 (예전 기록)
            rec["street_pot"] = street_pot_named(h.street_pot)
        names = {}
        for k, seat in h.seats.items():
            if seat.names:  # 닉네임은 메모리에만 (names_for). 같은 이름이 한 번이라도 잘려 읽혔으면 잘린 이름
                name = seat.names.most_common(1)[0][0][0]
                names[k] = (name, any(t for (n, t) in seat.names if n == name))
            ty = seat.type or {}
            rec["seats"][k] = {
                "hero": seat.hero, "pid": None if seat.hero else self.seat_pid(seat),
                "pos": seat.pos.most_common(1)[0][0] if seat.pos else None, "slot": seat.slot,
                "folded": STREETS[seat.fold_street] if seat.folded and seat.fold_street is not None else None,
                "allin": seat.allin, "shown": h.shown.get(k),
            }
            if not seat.hero:
                rec["seats"][k]["type"] = ty.get("type")          # 'LAG'/'LP'/'TAG'/'TP' 또는 None (모름)
                rec["seats"][k]["type_src"] = ty.get("type_src")  # 'popup' (게임 유저 정보 팝업) 또는 None
                if ty.get("type_stats"):
                    rec["seats"][k]["type_stats"] = dict(ty["type_stats"])
        self._names[h.id] = names
        while len(self._names) > NAMES_KEEP:
            self._names.popitem(last=False)
        derive(rec)
        codes = {i["code"] for i in h.issues}
        rec["confidence"] = "low" if codes & FATAL else "ok"
        return [rec]


def order_keys(keys, pos, dealer, street):
    """좌석 키(화면 순서 = 시계 방향, 내 왼쪽부터) + {좌석: 포지션} → (이번 스트리트 행동 순서, 포지션을 다 알았나).
    포지션을 하나라도 모르면 화면 순서 + 딜러 (딜러도 모르면 화면 순서 그대로)."""
    keys = list(keys)
    n = len(keys)
    pos = {k: pos.get(k) for k in keys}
    if keys and all(pos.values()):
        if n == 2 and set(pos.values()) == {"SB", "BB"}:
            pre = sorted(keys, key=lambda k: pos[k] != "SB")
            return (pre if street == 0 else pre[::-1]), True
        pre = sorted(keys, key=lambda k: PRE_RANK.index(pos[k]) if pos[k] in PRE_RANK else 99)
        if street == 0:
            return pre, True
        return [k for k in pre if pos[k] in ("SB", "BB")] + [k for k in pre if pos[k] not in ("SB", "BB")], True
    if dealer in keys:
        i = keys.index(dealer)
        post = keys[i + 1:] + keys[:i + 1]
    else:
        post = keys
    if street == 0 and n > 2:
        return post[2:] + post[:2], dealer in keys
    return post, dealer in keys


# --- 순서 확인: 포지션·행동 순서가 화면과 어긋나면 다시 읽어 고치고, 못 고치면 알린다 ------------------------------------
# 매 프레임 (가벼움, 좌석 수만큼의 비교): 이번 핸드에서 본 '포지션 증거'를 모아 두고, 지금 포지션이 증거와 어긋나는지 본다.
#   order_blind       프리플랍 SB 칩(0.5BB, 올인 아님)이 고정한 SB 가 아닌 좌석에 있음 (핸드 첫 프레임은 1BB 칩도 BB),
#                     또는 이번에 본 SB/BB 원판이 포지션과 다름 (tablestate disc_conflict, 버튼으로 정한 포지션은 그대로)
#   order_hero        내 포지션이 액션 바와 안 맞음 (프리플랍 '체크' 버튼 = BB, 프리플랍 콜 0.5 = SB)
#   order_ring_first  스트리트 첫 차례(고리/내 액션 바)가 행동 순서상 첫 사람이 아님 (그 앞 사람이 아직 행동 안 함)
#   order_ring        스트리트 중간: 행동 순서상 다음 사람을 건너뜀 (건너뛴 사람이 행동한 흔적 없음; 포지션과 무관 → 다시 읽기만)
#   order_button      핸드 중 내 포지션을 모름 (버튼·원판 못 찾음) ORDER_MISSING_FRAMES 연속, 플랍 이후 버튼이 다른 자리,
#                     또는 핸드 첫 ORDER_EARLY_FRAMES 프레임 안에 버튼이 옮겨 보임 (그대로면 '딜러 이동' 으로 핸드가 갈림)
#   order_players     핸드 기록엔 있는데 포지션을 받지 못한 좌석 (인원 수가 어긋나 포지션 이름이 밀림) ORDER_PLAYERS_FRAMES 연속
#   order_folded      폴드로 기록된 좌석이 같은 스트리트에 칩을 더 냄
#   bet_decreased     (Tracker 의 기존 문제 코드) 벳이 줄어듦
# 고치기: 다음 프레임을 '전부 다시 읽기' (tablestate.read(full=True): 펠트 전체 버튼 찾기 + SB/BB 원판 + 숫자 OCR 다시)
#   → 증거 순서 버튼 > 블라인드 원판 > 블라인드 칩 금액 으로 포지션 후보 → 후보가 모아 둔 증거를 지금보다 잘 설명하면 바꾼다
#   (TableReader.adopt + Tracker.relabel: 이미 기록한 액션의 포지션·블라인드만 고침, 액션은 지어내지 않음). 핸드마다 ORDER_TRIES 번.
# 알리기: 그래도 증거와 어긋나거나 포지션을 모르면 status='unsure' → 앱이 노란 줄 ORDER_NOTE + 추천은 포지션 없이 (신뢰도 낮음).
# 핸드 기록: 찾은 코드 (참고용) + 결과 order_healed / order_confirmed (포지션은 맞음, 읽기 문제) / order_unsure (FATAL).
ORDER_TRIES = 3
ORDER_MISSING_FRAMES = 2
ORDER_PLAYERS_FRAMES = 2
ORDER_EARLY_FRAMES = 4     # 핸드 시작 뒤 이 프레임 수 안의 버튼 이동 = 처음 본 버튼이 틀렸을 수 있음 (새 핸드일 수 없음)
ORDER_NOTE = "순서 확인 중 — 이번 추천은 참고만"
FATAL.add("order_unsure")


class OrderGuard:
    """앱(상태 스레드)과 replay 가 프레임마다:
        st = table_reader.read(img, calib, full=guard.want_full())
        guard.before(st, tracker, table_reader)     # 다시 읽은 프레임이면 포지션 후보를 보고 고침 (st 의 포지션도 바꿈)
        tracker.update(st, ...)
        guard.after(st, tracker, img)               # 어긋남 찾기 → 다음 프레임 다시 읽기 요청 / 결과 정리
    save(info, img): 어긋남을 찾았을 때(img 있음)와 결과가 났을 때(img None) 부름 — 앱이 증거 이미지·JSON 을 남긴다
    (info 에는 좌석 키·포지션·코드만, 닉네임 없음). 절대 예외를 밖으로 내지 않는다."""

    def __init__(self, save=None):
        self.save = save
        self.events = []          # 결과 목록 [{hand, codes, outcome, ...}] (메모리, 최근 200개 — 검증용)
        self.errors = 0
        self.last_error = None
        self._reset(None)

    def _reset(self, hid):
        self.hand_id = hid
        self.tries = 0
        self.pending = None       # 다시 읽기를 기다리는 어긋남 {key, codes, found, before, street, ...}
        self.status = None        # None / 'healed' / 'unsure' (이번 핸드)
        self.facts = set()        # 이번 핸드에서 본 포지션 증거 ('sb'|'bb', 좌석) / ('first', 스트리트, 좌석, 행동한 좌석들, 좌석들)
        self.first_seen = set()   # 첫 차례를 이미 본 스트리트
        self._acting = None       # 직전 프레임의 (스트리트, 차례 좌석)
        self._ring_obs = None     # 다음 프레임에 판단할 차례 변화
        self._players = 0
        self._missing = 0
        self._bet_dec = 0
        self._flagged = set()     # 이번 핸드에서 이미 알린 (코드, 내용)
        self.n = 0

    # --- 앱이 묻는 것 -----------------------------------------------------------------------------
    def want_full(self):
        """다음 읽기를 '전부 다시 읽기'로 해야 하나."""
        return self.pending is not None

    def unsure(self):
        return self.status == "unsure"

    def note(self):
        """화면에 보일 노란 줄 (포지션·순서를 못 믿을 때만) 또는 None."""
        return ORDER_NOTE if self.status == "unsure" else None

    # --- 프레임 처리 ------------------------------------------------------------------------------
    def _sync(self, tracker):
        h = getattr(tracker, "hand", None)
        hid = h.id if h is not None else None
        if hid != self.hand_id:
            if self.pending is not None and self.hand_id is not None:
                self._event(self.pending, "hand_ended", None)
            self._reset(hid)
        return h

    def before(self, state, tracker, table_reader=None):
        """다시 읽은 프레임 (tracker.update 전): 포지션 후보가 증거를 지금보다 잘 설명하면 바꾼다 (state 의 포지션도).
        → 바꿨으면 {before, after, src, fixed}, 아니면 None."""
        try:
            h = self._sync(tracker)
            p = self.pending
            if p is None or h is None or not isinstance(state, dict) or not state.get("seats") \
                    or state.get("layout_ok") is False:
                return None
            p["full"] = True
            cand = self._candidate(state, h, table_reader)
            p["cand_src"] = (cand or {}).get("src")
            if cand is None:
                return None
            old = Tracker.labels(h)
            new = {k: cand["pos"].get(k) for k in h.seats}
            if new == old:
                return None
            v_old, v_new = self._violated(h, old), self._violated(h, cand["pos"])
            unknown = any(v is None for v in old.values())
            # 증거를 더 잘 설명하면 바꿈. 같으면 버튼으로 정한 후보·포지션을 몰랐던 경우만 (버튼 > 원판 > 칩 금액)
            if not (len(v_new) < len(v_old) or (len(v_new) == len(v_old) and (cand["src"] == "D" or unknown))):
                p["rejected"] = [len(v_old), len(v_new)]
                return None
            if table_reader is not None and hasattr(table_reader, "adopt"):
                table_reader.adopt(cand)
            dropped = tracker.drop_seats(cand.get("drop")) if cand.get("drop") else []
            fixed = tracker.relabel(cand["pos"], cand.get("dealer")) + len(dropped)
            for s in state["seats"]:
                if isinstance(s, dict) and s.get("key") in cand["pos"]:
                    s["pos"] = cand["pos"][s["key"]]
            state["hero_pos"] = cand["pos"].get("hero")
            state["pos_players"] = min(10, max(6, len(cand["pos"])))
            if cand.get("dealer"):
                state["dealer"] = cand["dealer"]
            p["adopted"] = {"before": old, "after": new, "src": cand["src"], "fixed": fixed}
            return p["adopted"]
        except Exception as exc:
            self._fail(exc)
            return None

    def after(self, state, tracker, img=None):
        """tracker.update 다음: 어긋남 찾기 (+ 다시 읽은 프레임이면 결과 정리). → 이번에 새로 찾은 코드 리스트."""
        try:
            h = self._sync(tracker)
            if h is None or not isinstance(state, dict) or not state.get("seats") or state.get("layout_ok") is False:
                return []
            found = self._detect(state, tracker, h)
            p = self.pending
            if p is not None and p.get("full"):
                self._resolve(p, found, tracker, h)
                found = [f for f in found if f not in p["found"]]
            new = [f for f in found if f not in self._flagged]
            if not new:
                return []
            self._flagged.update(new)
            for code, detail in new:
                h.issue(code, detail)
            if self.pending is not None or self.tries >= ORDER_TRIES:
                return [c for c, _ in new]     # 이미 다시 읽는 중 / 더 고쳐 보지 않음 (기록만)
            self.tries += 1
            self.n += 1
            self.pending = {"key": f"{h.id}_{self.n}", "codes": sorted({c for c, _ in new}), "found": list(new),
                            "details": [d for _, d in new], "before": Tracker.labels(h), "street": STREETS[h.street],
                            "dealer": h.dealer.most_common(1)[0][0] if h.dealer else None}
            self._save(self._info(self.pending), img)
            return [c for c, _ in new]
        except Exception as exc:
            self._fail(exc)
            return []

    # --- 증거 · 어긋남 ---------------------------------------------------------------------------------
    def _detect(self, state, tracker, h):
        out = []
        moved = getattr(tracker, "_dealer_new", (None, 0))
        if moved[1] >= 1 and moved[0] and h.frames <= ORDER_EARLY_FRAMES and not h.over                 and state.get("dealer_seen") == moved[0]:
            # 핸드 첫 몇 프레임 안에 버튼이 다른 자리에서 보임: 처음 본 버튼이 틀렸을 수 있음 (움직이는 중인 원판 등).
            # 그대로 두면 다음 프레임에 '딜러 이동' 으로 핸드가 둘로 갈린다 → 다시 읽어 버튼을 확인
            return [("order_button", f"moved {moved[0]}")]
        if h.over or tracker._suspect() or state.get("no_hand"):
            self._acting = self._ring_obs = None   # 핸드가 끝남·새 핸드일 수 있음·카드 받은 사람 2명 미만 → 판단 안 함
            return out
        seats = {s["key"]: s for s in state["seats"] if isinstance(s, dict) and "key" in s}
        labels = Tracker.labels(h)
        self._collect(state, seats, h)
        for f in self._violated(h, labels):
            if f[0] in ("sb", "bb"):
                out.append(("order_hero" if f[1] == "hero" else "order_blind", f"{f[1]} {f[0].upper()}"))
            else:
                out.append(("order_ring_first", f"{STREETS[f[1]]} {f[2]}"))
        if state.get("disc_conflict") and h.street == 0:
            out.append(("order_blind", "disc " + " ".join(state["disc_conflict"])))
        skip = self._ring(state, seats, h, labels)
        if skip:
            out.append(("order_ring", skip))
        hero = seats.get("hero") or {}
        if hero.get("dealt") and not hero.get("folded") and h.frames >= 3 and "hero" in h.seats and not labels.get("hero"):
            self._missing += 1
        else:
            self._missing = 0
        if self._missing >= ORDER_MISSING_FRAMES:
            out.append(("order_button", "missing"))
        seen, cur = state.get("dealer_seen"), (h.dealer.most_common(1)[0][0] if h.dealer else None)
        if seen and cur and seen != cur and h.street > 0:
            out.append(("order_button", f"moved {seen}"))
        lk = set(state.get("lock_keys") or [])
        extra = sorted(k for k in h.seats if lk and k not in lk)
        self._players = self._players + 1 if extra else 0
        if self._players >= ORDER_PLAYERS_FRAMES:
            out.append(("order_players", " ".join(extra)))
        for k, seat in h.seats.items():
            s = seats.get(k)
            if not seat.folded or seat.fold_street != h.street or s is None or k in h.shown or s.get("folded") \
                    or not s.get("dealt"):
                continue
            b = _num(s.get("bet"))
            if b is not None and b > seat.inv + EPS and (seat.stale is None or abs(b - seat.stale) > EPS):
                out.append(("order_folded", k))
        n_dec = sum(1 for i in h.issues if i["code"] == "bet_decreased")
        if n_dec > self._bet_dec:
            out.append(("bet_decreased", next(i.get("detail") for i in reversed(h.issues) if i["code"] == "bet_decreased")))
        self._bet_dec = n_dec
        return out

    def _collect(self, state, seats, h):
        """이번 프레임의 포지션 증거를 self.facts 에 더함."""
        if h.street == 0:
            def at(amount):
                return [k for k, s in seats.items() if (s.get("dealt") or s.get("occupied")) and not s.get("allin")
                        and _num(s.get("bet")) is not None and abs(_num(s["bet"]) - amount) <= EPS]
            sb = at(SB_AMOUNT)
            if len(sb) == 1 and sb[0] in h.seats:
                self.facts.add(("sb", sb[0]))
                bb = at(BB_AMOUNT)
                voluntary = any(a["act"] != "post" for a in h.actions["preflop"])
                if len(bb) == 1 and bb[0] in h.seats and h.frames <= 2 and not voluntary:
                    self.facts.add(("bb", bb[0]))   # 핸드 첫 프레임: 0.5 와 1 이 하나씩 = SB, BB (림프 전)
            if state.get("to_act") is True and "hero" in h.seats:
                top = max([_num(s.get("bet")) or 0.0 for s in seats.values()] + [0.0])
                if state.get("bar") == "check" and top <= BB_AMOUNT + EPS:
                    self.facts.add(("bb", "hero"))
                call = _num(state.get("call"))
                if call is not None and abs(call - SB_AMOUNT) <= EPS and top <= BB_AMOUNT + EPS:
                    self.facts.add(("sb", "hero"))

    @staticmethod
    def _done(h, seats, k):
        """이번 스트리트에 이미 행동했거나 행동할 필요가 없는 좌석 (폴드·올인·액션 기록·말풍선)."""
        seat, s = h.seats[k], seats.get(k) or {}
        return bool(seat.folded or seat.allin or seat.acted or s.get("folded") or s.get("bubble"))

    @staticmethod
    def _fact_ok(h, f, pos):
        if f[0] == "sb":
            return pos.get(f[1]) == "SB"
        if f[0] == "bb":
            return pos.get(f[1]) == "BB"
        _, street, acting, done, keys = f
        keys = [k for k in h.seats if k in keys]
        dealer = h.dealer.most_common(1)[0][0] if h.dealer else None
        order, known = order_keys(keys, pos, dealer, street)
        if not known or any(not pos.get(k) for k in keys):
            return True     # 포지션을 모르면 판단하지 않음 (order_button 이 따로 봄)
        exp = next((k for k in order if k not in done), None)
        return exp is None or exp == acting

    def _violated(self, h, pos):
        return [f for f in sorted(self.facts, key=repr) if not self._fact_ok(h, f, pos)]

    def _ring(self, state, seats, h, labels):
        """차례(고리 / 내 액션 바)가 새 좌석 X 로 넘어옴 → 다음 프레임에 판단 (앞 사람의 말풍선·흑백이 한 프레임 늦게 보임).
        스트리트 첫 차례: 증거 ('first', 스트리트, X, 그때까지 행동한 좌석들, 좌석들) — 포지션이 맞으면 X 가 순서상 첫 사람.
        스트리트 중간: 마지막 행동자 다음부터 X 앞까지 행동한 흔적이 없는 사람 → order_ring 내용 (포지션과 무관)."""
        acting = "hero" if state.get("to_act") is True else state.get("acting_seat")
        prev, self._acting = self._acting, (h.street, acting)
        out = None
        obs = self._ring_obs
        if obs is not None and obs["street"] == h.street:
            done = obs["done"] | {k for k in h.seats if k != obs["acting"] and self._done(h, seats, k)}
            if obs["anchor"] is None:
                self.facts.add(("first", h.street, obs["acting"], frozenset(done), obs["keys"]))
            else:
                out = self._skipped(h, obs, done, labels)
        self._ring_obs = None
        if not acting or acting not in h.seats or prev == (h.street, acting) or h.frames < 2:
            return out
        acts = [a for a in h.actions[STREETS[h.street]] if a["act"] != "post"]
        if h.street in self.first_seen and (h.last_actor is None or h.last_actor == acting):
            return out
        first = h.street not in self.first_seen and not any(a["seat"] != acting for a in acts)
        self.first_seen.add(h.street)
        self._ring_obs = {"street": h.street, "acting": acting, "anchor": None if first else h.last_actor,
                          "keys": frozenset(h.seats),
                          "done": {k for k in h.seats if k != acting and self._done(h, seats, k)}}
        return out

    @staticmethod
    def _skipped(h, obs, done, labels):
        order, known = order_keys(list(h.seats), labels, h.dealer.most_common(1)[0][0] if h.dealer else None,
                                  obs["street"])
        if not known or any(not labels.get(k) for k in order) or obs["anchor"] not in order or obs["acting"] not in order:
            return None
        i = order.index(obs["anchor"])
        skipped = []
        for j in range(1, len(order)):
            k = order[(i + j) % len(order)]
            if k == obs["acting"]:
                break
            if k not in done:
                skipped.append(k)
        if not skipped:
            return None
        return f"{STREETS[obs['street']]} {obs['acting']} after {obs['anchor']} skipped {' '.join(skipped)}"

    # --- 후보 · 결과 -----------------------------------------------------------------------------------
    def _candidate(self, state, h, table_reader):
        """버튼 > 블라인드 원판 (다시 읽은 화면, state['rebuild']) > 블라인드 칩 금액 (모은 증거) 순서로 포지션 후보 하나."""
        rb = state.get("rebuild")
        if isinstance(rb, dict) and rb.get("pos"):
            if self._violated(h, rb["pos"]) and rb.get("dealer"):
                alt = self._without_phantoms(state, h, table_reader, rb["dealer"])
                if alt is not None:
                    return alt
            return rb
        if table_reader is None or not hasattr(table_reader, "positions_for"):
            return None
        keys_cw = [s["key"] for s in state["seats"] if isinstance(s, dict) and "key" in s]
        in_hand = set(h.seats) | set(state.get("lock_keys") or [])
        for kind in ("sb", "bb"):
            k = next((f[1] for f in sorted(self.facts, key=repr) if f[0] == kind), None)
            if k is None:
                continue
            pos = table_reader.positions_for(keys_cw, in_hand, **{kind: k})
            if pos:
                return {"pos": pos, "dealer": next((x for x, p in pos.items() if p == "BTN"), None), "src": "bet",
                        "keys": sorted(in_hand), "disc": {}}
        return None

    def _without_phantoms(self, state, h, table_reader, dealer):
        """버튼은 맞는데 블라인드 증거와 안 맞을 때: 버튼과 SB 칩/원판 좌석 사이(시계 방향)에 있는, 폴드 말고는 한 게 없는 흑백 좌석은
        이번 핸드에 없던 사람 (자리 비움·대기 — 흑백 아바타라 '카드 받고 폴드' 로 보였다). 그 좌석을 빼면 버튼과 블라인드가 둘 다
        맞으면 그 후보 (+ 'drop': 뺄 좌석). 아니면 None."""
        if table_reader is None or not hasattr(table_reader, "positions_for"):
            return None
        keys_cw = [s["key"] for s in state["seats"] if isinstance(s, dict) and "key" in s]
        sb = next((f[1] for f in sorted(self.facts, key=repr) if f[0] == "sb"), None)
        if sb is None or dealer not in keys_cw or sb not in keys_cw or sb == dealer:
            return None
        i, j = keys_cw.index(dealer), keys_cw.index(sb)
        between = [keys_cw[(i + d) % len(keys_cw)] for d in range(1, (j - i) % len(keys_cw))]
        drop = [k for k in between if k in h.seats and h.seats[k].folded and not h.seats[k].hero
                and all(a.get("act") == "fold" for st in STREETS for a in h.actions[st] if a.get("seat") == k)]
        if not drop:
            return None
        in_hand = (set(h.seats) | set(state.get("lock_keys") or [])) - set(drop)
        pos = table_reader.positions_for(keys_cw, in_hand, dealer=dealer)
        if not pos or self._violated(h, pos):
            return None
        return {"pos": pos, "dealer": dealer, "src": "D", "keys": sorted(in_hand), "disc": {}, "drop": drop}

    def _resolve(self, p, found, tracker, h):
        """다시 읽은 프레임의 결과: healed / confirmed / (다시 시도) / unsure."""
        self.pending = None
        still = {c for c, _ in found} & set(p["codes"])
        folded_fixed = False
        if "order_folded" in still:
            for c, d in found:
                if c == "order_folded" and tracker.unfold(d):
                    folded_fixed = True
        labels = Tracker.labels(h)
        bad = self._violated(h, labels)
        missing = "hero" in h.seats and not labels.get("hero") and not h.over
        if bad or missing:
            if self.tries < ORDER_TRIES:   # 한 번 더 다시 읽기 (같은 핸드 안에서 최대 ORDER_TRIES 번)
                self.tries += 1
                self.pending = dict(p, full=False)
                return
            outcome = "order_unsure"
        elif p.get("adopted") or folded_fixed:
            outcome = "order_healed"
        else:
            outcome = "order_confirmed"
        self.status = {"order_unsure": "unsure", "order_healed": "healed"}.get(outcome, None)
        detail = " ".join(p["codes"])
        if p.get("adopted"):
            detail += f" src={p['adopted']['src']} fixed={p['adopted']['fixed']}"
        h.issue(outcome, detail)
        self._event(p, outcome, labels, bad=len(bad))

    def _info(self, p, outcome=None, after=None, bad=None):
        info = {"key": p["key"], "hand": self.hand_id, "street": p["street"], "codes": p["codes"],
                "details": [d for d in p["details"] if d], "dealer": p.get("dealer"),
                "seats": sorted(p["before"]), "before": p["before"]}
        if outcome:
            info.update(outcome=outcome, after=after, tries=self.tries, cand_src=p.get("cand_src"),
                        violated=bad, rejected=p.get("rejected"),
                        adopted=bool(p.get("adopted")))
        return info

    def _event(self, p, outcome, after, bad=None):
        info = self._info(p, outcome, after, bad)
        self.events.append(info)
        del self.events[:-200]
        self._save(info, None)

    def _save(self, info, img):
        if self.save is None:
            return
        try:
            self.save(info, img)
        except Exception as exc:
            self._fail(exc)

    def _fail(self, exc):
        self.errors += 1
        self.last_error = repr(exc)


# --- 기록 → 좌석별 통계 플래그 ------------------------------------------------------------------------
def derive(rec):
    """rec['seats'][k]['stats'] 를 채움: 이번 핸드에서의 카운터 (profiles.Store 가 더함)."""
    acts = rec.get("actions") or {}
    # 프리플랍은 정리본으로 센다 (금액 재읽기·작은 올인·레이즈가 될 수 없는 '레이즈' 보정; raises_before 도 다시 매김)
    pre_clean = clean_preflop(acts.get("preflop"))[0]
    opener = next((x["seat"] for x in pre_clean if x["act"] == "raise" and not x.get("incomplete")), None)
    reached = STREETS.index(rec.get("streets") or "preflop")
    sd = set((rec.get("showdown") or {}).get("seats") or [])
    for k, seat in rec["seats"].items():
        st = {"hands": 1, "vpip": 0, "pfr": 0, "threebet": 0, "threebet_opp": 0, "faced_bet": 0, "fold_to_bet": 0,
              "agg": 0, "calls": 0, "saw_flop": 0, "showdown": 0}
        pre = [a for a in pre_clean if a.get("seat") == k and a.get("act") != "post"]
        if any(a["act"] in ("call", "raise", "bet", "unknown") for a in pre):
            st["vpip"] = 1
        if any(a["act"] in ("raise", "bet") for a in pre):
            st["pfr"] = 1
        for a in pre:
            if a.get("raises_before") == 1 and a["act"] in ("call", "raise", "fold"):
                if opener != k:
                    st["threebet_opp"] = 1
                    if a["act"] == "raise" and not a.get("incomplete"):   # 짧은 올인은 기회만 (3벳 아님)
                        st["threebet"] = 1
                break
        folded_pre = seat.get("folded") == "preflop"
        # 프리플랍 올인 쇼다운은 보드를 못 읽었어도 플랍을 본 것 (WTSD 분모; 분자만 쌓여 100% 를 넘지 않게)
        st["saw_flop"] = int((reached >= 1 or k in sd) and not folded_pre)
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


# --- 기록 → 상대 리딩 입력 정리 ---------------------------------------------------------------------
# 화면에서 읽은 금액은 가끔 틀린다. 상대 리딩(handread)에 넘기기 전에 프리플랍 액션을 정리한다 (기록 자체는 그대로 둔다).
#   같은 금액을 두 번 읽음: 같은 좌석이 '그 사이 아무도 (완전한) 레이즈를 안 했는데' 다시 레이즈/콜 → 하나로 합침
#     (예: 오픈 1.3 → 2.0, 올인 114.7 → 115, 림프 0.6 → 1.0). 레이즈한 사람이 그 뒤 작은 올인에 '콜'한 것도 새 결정이 아님.
#   최소 레이즈에 못 미치는 올인(2.0 오픈에 2.1 올인)은 레이즈 횟수에 안 센다 → 뒤 사람이 3벳/3벳콜로 잘못 잡히지 않게.
#   올인 아닌데 직전 레이즈 폭의 MIN_RAISE_FRAC 도 안 올린 '레이즈'(2.0 → 2.1)는 같은 콜 금액을 다시 읽은 것 → 콜.
#   올인 레이즈에 콜 = 올인콜 (콜한 사람이 올인이 아니어도: 스택이 더 큰 사람이 받아 준 것).
#   말이 안 되는 금액(올인 아닌데 지금 최대 벳의 MAX_JUMP 배 초과, 또는 MAX_BB 초과: 700/1400BB 레이즈)은 '금액 모름'으로.
#   올인/올인콜 상대의 스택 = 실제 올인 금액 (내 스택이 아니라) → seat['stack_bb'].
MIN_RAISE_FRAC = 0.5
MAX_JUMP = 20.0
MAX_BB = 1000.0


def clean_preflop(actions):
    """프리플랍 액션 정리 사본 → (액션 리스트, 고친 개수). 각 액션의 raises_before 를 (완전한) 레이즈 수로 다시 매기고,
    작은 올인은 'incomplete', 올인 레이즈에 받은 콜은 'vs_allin', 이상한 금액은 'to' 를 지우고 'size_bad' 로 표시."""
    out, fixed = [], 0
    rb, level, last_inc, last_allin = 0, BB_AMOUNT, BB_AMOUNT, False
    last_vol = {}      # 좌석 → (그 액션 뒤의 레이즈 수, out 인덱스)
    for a0 in actions or []:
        if not isinstance(a0, dict):
            continue
        a = dict(a0)
        k, act, to, allin = a.get("seat"), a.get("act"), _num(a.get("to")), bool(a.get("allin"))
        if act == "post":
            if to is not None and to <= MAX_BB:
                level = max(level, to)
            out.append(a)
            continue
        if act in ("raise", "call") and to is not None and (
                to > MAX_BB or (not allin and to > MAX_JUMP * max(level, BB_AMOUNT))):
            a.pop("to", None)
            a["size_bad"], to = True, None
            fixed += 1
        if act == "raise" and to is not None and not allin and (
                to <= level + EPS or to - level < MIN_RAISE_FRAC * last_inc):
            a["act"] = act = "call"      # 레이즈가 될 수 없는 폭 = 콜 금액을 다시 읽음
            fixed += 1
        prev = last_vol.get(k)
        if act in ("raise", "call") and prev is not None and prev[0] == rb:
            p = out[prev[1]]
            if p.get("act") == act or (act == "call" and p.get("act") == "raise"):
                # 같은 액션을 다시 읽음 (또는 레이즈한 사람이 작은 올인에 '콜' = 새 결정 아님) → 앞 액션에 합친다
                if act == p.get("act"):
                    pto = _num(p.get("to"))
                    if to is not None:
                        p["to"] = round(max(to, pto) if pto is not None else to, 2)
                    if allin:
                        p["allin"] = True
                    if act == "raise" and not p.get("incomplete") and p.get("to") is not None:
                        level = max(level, p["to"])
                        base = p.get("_level_before")
                        if base is not None:
                            last_inc = max(last_inc, p["to"] - base)
                        last_allin = bool(p.get("allin"))
                fixed += 1
                continue
            if act == "raise" and p.get("act") == "call":
                p["_drop"] = True            # 림프로 읽은 게 실은 레이즈 금액이 올라가는 중이었다
                fixed += 1
        a["raises_before"] = rb
        if act == "raise":
            inc = (to - level) if to is not None else None
            if allin and inc is not None and inc < last_inc - EPS:
                a["incomplete"] = True       # 최소 레이즈 미만 올인: 레이즈로 안 센다
                level = max(level, to)
                fixed += 1
                last_vol[k] = (rb, len(out))
            else:
                a["_level_before"] = level
                rb += 1
                if inc is not None and inc > 0:
                    last_inc = max(last_inc, inc)
                    level = to
                last_allin = allin
                last_vol[k] = (rb, len(out))
        elif act == "call":
            if rb >= 1 and last_allin and not allin:
                a["vs_allin"] = True
            last_vol[k] = (rb, len(out))
        out.append(a)
    res = []
    for a in out:
        if a.pop("_drop", False):
            continue
        a.pop("_level_before", None)
        res.append(a)
    return res, fixed


def _pre_stacks(pre):
    """정리된 프리플랍 액션 → {좌석: 올인/올인콜 금액(BB)} (금액을 아는 것만)."""
    out, level = {}, 0.0
    for a in pre:
        to = _num(a.get("to"))
        if a.get("act") == "raise" and to is not None:
            level = max(level, to)
            if a.get("allin"):
                out[a["seat"]] = to
        elif a.get("act") == "call" and (a.get("allin") or a.get("vs_allin")):
            amt = to if to is not None else (level or None)
            if amt:
                out[a["seat"]] = amt
    return out


def street_pot_named(sp):
    """{스트리트 인덱스: 팟} → {"flop": x, ...} (기록용, 2자리 반올림)."""
    return {STREETS[i]: round(v, 2) for i, v in sorted(sp.items()) if 1 <= i < len(STREETS)}


def bet_sizes(actions, pre=None, street_pot=None, ante=0.0):
    """{(street, 액션 인덱스): 팟 대비 크기} (포스트플랍 bet/raise/call). 벳/레이즈 = 올린 만큼 / (콜한 뒤의 팟), 콜 = 받은 벳의 크기.
    'to' = 그 스트리트에서 그 좌석이 넣은 총액(BB). 금액을 모르거나 말이 안 되면(올인 아닌데 팟의 MAX_JUMP 배 초과) 크기 없음.
    pre: 정리된 프리플랍 (없으면 clean_preflop).
    street_pot: {"flop": BB, "turn": BB, "river": BB} = 그 스트리트 시작 때 화면 팟 (앤티 포함; 기록 rec["street_pot"]). 있는 스트리트는
    이 값을 팟 기준으로 쓴다 (앱 어드바이저와 같은 기준). 없으면 기록된 금액을 누적해 계산 — 이때 앤티는 모르므로 팟에서 빠진다
    (벳 크기가 크게 나옴). ante: 그 경우 쓸 앤티 합계(BB) 추정값 (예: 결정 기록 ctx 의 ante), 기본 0."""
    pre = clean_preflop(actions.get("preflop"))[0] if pre is None else pre
    pot, out = max(0.0, _num(ante) or 0.0), {}
    sp = street_pot if isinstance(street_pot, dict) else {}
    for st in STREETS:
        base_pot = _num(sp.get(st)) if st != "preflop" else None
        if base_pot is not None and base_pot > 0:
            pot = base_pot
        put, level, last = {}, 0.0, None
        for i, a in enumerate(pre if st == "preflop" else (actions.get(st) or [])):
            k, act, to = a.get("seat"), a.get("act"), _num(a.get("to"))
            if to is not None and (to > MAX_BB or (st != "preflop" and not a.get("allin")
                                                  and to > MAX_JUMP * max(pot, level, BB_AMOUNT))):
                to = None
            now = pot + sum(put.values())
            if act in ("bet", "raise"):
                if to is not None and to > level:
                    base = now + max(0.0, level - put.get(k, 0.0))
                    last = (to - level) / base if base > 0 else None
                    put[k], level = to, to
                else:
                    last = None
                if st != "preflop":
                    out[(st, i)] = last
            elif act == "call":
                if st != "preflop":
                    out[(st, i)] = last
                put[k] = max(put.get(k, 0.0), to if to is not None else level)
            elif act == "post" and to is not None:
                put[k] = max(put.get(k, 0.0), to)
                level = max(level, to)
        pot += sum(put.values())
    return out


def to_handread(actions, pos, clean=True, sizes=True, street_pot=None, ante=0.0):
    """액션 목록({street: [action]}) + {seat: pos} → handread.estimate 의 seats 모양.
    clean: 프리플랍 정리(clean_preflop) + 올인/올인콜 좌석에 실제 올인 금액 'stack_bb'.
    street_pot/ante: bet_sizes 참고 (스트리트 시작 화면 팟 / 앤티 추정).
    sizes: 포스트플랍 벳/콜/레이즈/올인에 기록 금액으로 계산한 팟 대비 크기를 붙인다 ({"a": "bet", "size": 0.66}, 금액 모르면 문자열)."""
    out = {k: {"pos": p, "preflop": [], "flop": [], "turn": [], "river": []} for k, p in pos.items()}
    pre = clean_preflop(actions.get("preflop"))[0] if clean else list(actions.get("preflop", []))
    opener = None
    for a in pre:
        k, act, rb = a["seat"], a["act"], a.get("raises_before") or 0
        if k not in out:
            continue
        if act == "post":
            word = "blind"
        elif act == "raise":
            word = "jam" if a.get("allin") else ("open", "3bet", "4bet")[min(rb, 2)]
            if rb == 0 and not a.get("incomplete"):
                opener = pos.get(k)
        elif act == "call":
            word = "call_jam" if (a.get("allin") or a.get("vs_allin")) and rb >= 1 else ("limp", "call", "call_3bet")[min(rb, 2)]
        elif act in ("check", "fold"):
            word = act
        else:
            continue
        out[k]["preflop"].append(word)
    if clean:
        for k, amt in _pre_stacks(pre).items():
            if k in out and out[k]["preflop"] and out[k]["preflop"][-1] in ("jam", "call_jam"):
                out[k]["stack_bb"] = round(amt, 1)
    size_of = bet_sizes(actions, pre, street_pot, ante) if sizes else {}
    # 주도권: 프리플랍 마지막 레이저 = 어그레서. 스트리트의 첫 벳이 (이전 스트리트 어그레서의 벳 = cbet / 어그레서 없이 콜러가 리드 = lead /
    # 어그레서가 앞 스트리트를 체크로 넘기고 벳 = delay / 체크로 넘어온 스트리트에 콜러가 벳 = probe)
    pre_raises = [a["seat"] for a in pre if a["act"] == "raise" and a["seat"] in out and not a.get("incomplete")]
    aggr = pre_raises[-1] if pre_raises else None
    prev_street_aggr = aggr
    for street in STREETS[1:]:
        street_aggr, first_bet = None, True
        for i, a in enumerate(actions.get(street, [])):
            k, act = a["seat"], a["act"]
            if k not in out or act not in ("check", "bet", "call", "raise", "fold"):
                continue
            if act in ("bet", "raise"):
                first, first_bet = first_bet, False
                street_aggr = k
                if first and act == "bet" and aggr is not None and not a.get("allin"):
                    if street == "flop" or prev_street_aggr is not None:
                        role = "cbet" if k == prev_street_aggr else "lead"
                    else:   # 앞 스트리트가 체크로 끝남
                        role = "delay" if k == aggr else "probe"
                    out[k].setdefault("bet_role", {})[street] = role
            seq = out[k][street]
            word = "allin" if a.get("allin") and act in ("bet", "raise") else act
            if act == "raise" and "check" in seq:
                word = "checkraise"
            sz = size_of.get((street, i))
            seq.append({"a": word, "size": round(sz, 3)} if sz and word in ("bet", "call", "raise", "allin", "checkraise")
                       else word)
        prev_street_aggr = street_aggr
    for k in out:
        if opener and out[k]["pos"] != opener:
            out[k]["opener"] = opener
    return out
