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
            if not quiet:
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
