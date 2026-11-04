"""기록 업로드 판정 (sharebox server/records_check.go 와 같은 결과를 내야 한다).

표준 라이브러리만 쓴다. 다른 card-reader 모듈을 import 하지 않는다 (리눅스 수신기에 두 파일만 복사해 쓰기 위해).
판정 결과에는 본문의 값이 들어가지 않는다 (규칙 이름, 줄 번호, 키 경로만).
"""
import datetime
import errno
import hashlib
import json
import os
import re
import stat
import unicodedata

MAX_BODY = 16 << 20
MAX_LINE = 1 << 20
MAX_LINES = 100000
MAX_DEPTH = 32

FILE_RE = re.compile(r"(decisions_)?([0-9]{4}-[0-9]{2}-[0-9]{2})\.jsonl")
SRC_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
PID_RE = re.compile(r"p_[0-9a-f]{8}")
TOP_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
_STRUCT = re.compile(r'"(?:[^"\\]|\\.)*"|[\[\]{}]')


FILTER_V = 3      # redacted 의 값 = 필터 판 번호 (명세 12.2, 13.2)
ACCEPT_MIN = 3    # 리눅스 수신·--recheck 가 받는 가장 낮은 판 (명세 13.2-7)

SEAT_PAT = r"^(hero|opp[1-9][0-9]?|seat_[A-Z]{1,4})$"
POS_PAT = r"^(UTG|U1|U2|U3|MP|LJ|HJ|CO|BTN|SB|BB)$"
POS_OR_SEAT_PAT = r"^(UTG|U1|U2|U3|MP|LJ|HJ|CO|BTN|SB|BB|hero|opp[1-9][0-9]?|seat_[A-Z]{1,4})$"
ACTKEY_PAT = r"^[a-z][a-z0-9_]{0,23}$"
CODE_PAT = r"^[a-z][a-z0-9_]{0,31}$"
RE_PATS = {
    "SEAT": SEAT_PAT, "POS": POS_PAT, "POS_OR_SEAT": POS_OR_SEAT_PAT, "ACTKEY": ACTKEY_PAT, "CODE": CODE_PAT,
    "CARD": r"^[2-9TJQKA][shdc]$",
    "RANK": r"^[2-9TJQKA]$",
    "CARDSTR": r"^([2-9TJQKA][shdc]){0,5}$",
    "HID": r"^[0-9]{8}_[0-9]{6}-[0-9]{4,9}$",
    "SESSION": r"^[0-9]{8}_[0-9]{6}$",
    "DID": r"^([0-9]{8}_[0-9]{6}-[0-9]{4,9}/[0-9]{1,9}|t[0-9]{1,16}-[0-9]{1,9})$",
    "DAY": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
    "HAND_NAME": (r"^((하이카드|원페어|투페어|트리플|스트레이트|플러시|풀하우스|포카드|스트레이트 플러시)"
                  r"( \([2-9TJQKA](, [2-9TJQKA])?\))?|포켓 페어 [2-9TJQKA]{2}|[2-9TJQKA]{2}[so]  ·  (수딧|오프수딧))$"),
    "SPOT": r"^[A-Z]{2,3}_[A-Z]{2}$",
    "NODE": r"^[a-z_]{1,24}(:[a-z0-9_.]{1,16})?$",
    "LINE": r"^[a-z0-9_]{1,24}$",
}
_RES = {k: re.compile(v) for k, v in RE_PATS.items()}
CODE_RE = _RES["CODE"]
VER_RE = re.compile(r"[1-9][0-9]{0,2}")
INT_LIT_RE = re.compile(r"-?[0-9]+")

# 열거 집합 (명세 13.2-2). UTF-8 바이트 정확 일치.
ENUMS = {
    "STREET": ("preflop", "flop", "turn", "river"),
    "END_REASON": ("flush", "stale", "new_session", "hero cards changed", "board cleared", "dealer moved",
                   "folded seats dealt again", "new blinds after hand over"),
    "ACT": ("post", "bet", "raise", "call", "fold", "check", "unknown"),
    "H_SRC": ("bet", "bet+bubble", "bubble", "grey/bubble", "inferred"),
    "BLIND": ("SB", "BB"),
    "SLOT": ("BL", "L", "UL", "TL", "TC", "TR", "UR", "R", "BR", "HERO"),
    "H_CONF": ("ok", "low"),
    "SITUATION": ("open", "vsopen", "vs3bet", "vs4bet", "jam", "jamcall"),
    "FLOP_LINE": ("x_x", "x_bet33_c", "x_bet75_c", "bet33_c", "bet75_c"),
    "EQ_BASIS": ("range", "random"),
    "TEX_SUIT": ("monotone", "four_flush", "three_flush", "two_tone", "rainbow"),
    "TEX_LABEL": ("dry", "semi", "wet"),
    "TEX_BUCKET": ("dry", "semi", "wet", "dry+paired", "semi+paired", "wet+paired"),
    "ADV_ACTION": ("fold", "check", "call", "bet", "raise", "allin", "wait"),
    "ADV_ACTION_KO": ("폴드", "체크", "콜", "벳", "레이즈", "올인", "대기"),
    "ADV_CONF": ("high", "medium", "low"),
    "COLOR_KEY": ("fold", "check", "call", "raise", "jam"),
    "GTO_MODE": ("primary", "secondary", "ref3"),
    "ROLE": ("ip", "oop"),
    "STAGE": ("early", "mid", "near_bubble", "bubble", "itm", "final", "unknown"),
    "STRUCTURE": ("standard", "flat", "top_heavy"),
    "OUTS_LABEL": ("포쁠 (플러시 드로우)", "양차 (오픈엔디드)", "빵꾸 (것샷)", "원페어 → 투페어/셋", "오버카드", "풀하우스/포카드"),
    "RES_SRC": ("tracker", "manual"),
    "LINK": ("no_hero_action_seen",),
    "DELTA_SRC": ("folded", "uncontested", "showdown_approx"),
    "HERO_ACTION": ("bet", "raise", "call", "fold", "check", "unknown", "allin", "bet/raise/call", "check?"),
    "PROF_SOURCE": ("observed", "site"),
    "STYLE": ("LAG", "LP", "TAG", "TP"),
    "TYPE_SRC": ("popup",),
    "WON": ("split",),
}
_ENUMSET = {k: frozenset(v) for k, v in ENUMS.items()}

# 값 형식 (명세 13.2-2). "X" 는 특수 잎(기존 규칙이 본다)이라 여기에 없다.
TEXT_MAX = 400
# 금지 코드 포인트 (명세 13.3)
BADCP = ((0x00, 0x1F), (0x7F, 0x9F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0xD800, 0xDFFF),
         (0xE000, 0xF8FF), (0xFEFF, 0xFEFF), (0xFFF0, 0xFFFF))


def _ty(j, re_="", en="", ref=0, text=0):
    return {"j": sorted(j.split()), "re": re_, "en": en, "ref": ref, "text": text}


TYPES = {
    "NUM": _ty("num"), "BOOL": _ty("bool"), "NUMBOOL": _ty("bool num"),
    "SEAT": _ty("str", "SEAT", ref=1), "POS_OR_SEAT": _ty("str", "POS_OR_SEAT", ref=1),
    "POS": _ty("str", "POS"), "CARD": _ty("str", "CARD"), "RANK": _ty("str", "RANK"),
    "CARDSTR": _ty("str", "CARDSTR"), "STREET": _ty("str", en="STREET"),
    "HID": _ty("str", "HID"), "SESSION": _ty("str", "SESSION"), "DID": _ty("str", "DID"), "DAY": _ty("str", "DAY"),
    "HAND_NAME": _ty("str", "HAND_NAME"), "SPOT": _ty("str", "SPOT"), "NODE": _ty("str", "NODE"),
    "LINE": _ty("str", "LINE"), "TEXT": _ty("str", text=1),
    "WON": _ty("bool str", en="WON"), "PAYOUTS": _ty("num str", en="STRUCTURE"),
    "OUTS_GROUP": _ty("num str", en="OUTS_LABEL"),
}
for _n in ENUMS:
    if _n not in ("STREET", "WON", "STRUCTURE", "OUTS_LABEL"):
        TYPES["E:" + _n] = _ty("str", en=_n)

CLASSES = ("스트레이트 이상", "셋/트립스", "투페어", "탑페어+", "중간·약한 페어", "드로우", "노페어", "프리미엄 페어",
           "중간 페어", "작은 페어", "A-x 수딧", "브로드웨이", "수딧 커넥터", "기타")
PROFILE_KEYS = frozenset(["vpip", "pfr", "af", "fold_to_bet", "threebet", "wtsd", "hands", "source", "style",
                          "type_src"])
TYPE_STATS_KEYS = frozenset(["vpip", "pfr", "ats", "threebet"])
SEAT_TYPES = frozenset(["LAG", "LP", "TAG", "TP"])

# 닉네임 모양 (부분 일치). Go regexp 와 같은 결과가 나오도록 ASCII 클래스 + 문자 2개만 쓴다.
NICK_PATS = (r"[Uu][Ss][Ee][Rr][0-9]{2}", "[‥…]", r"[A-Za-z]{3}[0-9]{4}")
NICK_RES = tuple(re.compile(p) for p in NICK_PATS)
# 이름처럼 보이는 키 토큰 (ASCII 소문자 비교, 부분 문자열)
NAME_TOKENS = ("name", "nick", "player", "user", "account", "screen", "display", "truncat", "placeholder",
               "linked", "owner", "alias", "닉네임", "이름", "닉")
# 경로 표시에서 소문자 형태로 보여 주는 옛 금지 키 18개
OLD_FORBIDDEN = frozenset(["name", "nick", "nickname", "player", "player_name", "playername", "username",
                           "user_name", "screen_name", "screenname", "display_name", "displayname", "account",
                           "truncated", "placeholder", "linked_name", "닉네임", "이름"])
# 개인정보 문제가 아닌 이유로 필터만 버리는 키 (판정 목록에는 남는다)
RETIRED = frozenset()

# --- 스키마 (명세 12.1, 13.2). 노드: 문자열 = 잎(값 형식 이름, "X" = 특수 잎), {"k": {키: 노드}} = 객체,
#     {"m": 정규식 이름, "v": 노드} = 맵 ---
_DEFS = []        # (이름, 노드) 정의 순서 (Go 코드 생성용)


def _obj(name, typed=(), objs=None):
    """typed: [(형식 이름, "키 키 ..."), ...]"""
    k = {}
    for ty, keys in typed:
        for x in keys.split():
            assert x not in k, (name, x)
            k[x] = ty
    k.update(objs or {})
    node = {"k": k}
    _DEFS.append((name, node))
    return node


def _map(re_name, v):
    return {"m": re_name, "v": v}


E = lambda n: "E:" + n          # noqa: E731

H_ACT = _obj("H_ACT", [("SEAT", "seat"), (E("ACT"), "act"), (E("H_SRC"), "src"),
                       ("BOOL", "facing allin incomplete vs_allin size_bad"), ("NUM", "raises_before to"),
                       (E("BLIND"), "blind")])
H_ACTIONS = _obj("H_ACTIONS", (), {s: H_ACT for s in ("preflop", "flop", "turn", "river")})
SHOWDOWN = _obj("SHOWDOWN", [("SEAT", "seats")], {"shown": _map("SEAT", "CARD")})
TYPE_STATS = _obj("TYPE_STATS", [("NUM", "vpip pfr ats threebet")])
STATS = _obj("STATS", [("NUM", "hands vpip pfr threebet threebet_opp faced_bet fold_to_bet agg calls saw_flop showdown")])
H_SEAT = _obj("H_SEAT", [("BOOL", "hero allin"), ("POS", "pos"), (E("SLOT"), "slot"), ("STREET", "folded"),
                         ("CARD", "shown"), ("NUM", "stack_bb"), ("X", "pid type type_src")],
              {"type_stats": TYPE_STATS, "stats": STATS})
ISSUE = _obj("ISSUE", [("X", "code")])
TURN = _obj("TURN", [("STREET", "street"), ("SEAT", "seat"), ("NUM", "t0 t1 p")])
HAND = _obj("HAND", [("HID", "id"), ("SESSION", "session"), ("NUM", "start end frames players pos_players pot"),
                     (E("END_REASON"), "end_reason"), ("SEAT", "dealer"), ("CARD", "hero_cards board"),
                     ("STREET", "streets"), (E("H_CONF"), "confidence"), ("X", "redacted")],
            {"actions": H_ACTIONS, "showdown": SHOWDOWN, "seats": _map("SEAT", H_SEAT), "issues": ISSUE,
             "turns": TURN})

TEX = _obj("TEX", [("NUM", "n flush_max broadway connected straight_windows wet"), (E("TEX_SUIT"), "suit"),
                   ("BOOL", "paired trips low"), ("RANK", "high"), (E("TEX_LABEL"), "label"),
                   (E("TEX_BUCKET"), "bucket")])
OUTS = _obj("OUTS", [("OUTS_GROUP", "groups"), ("NUM", "outs deck count left next by_river"), ("BOOL", "combo")])
BETTOR = _obj("BETTOR", [("SEAT", "seat"), ("POS", "pos"), ("STREET", "street"), (E("ACT"), "act"), ("NUM", "to pct")])
D_ACT = _obj("D_ACT", [(E("ACT"), "act"), ("NUM", "to pct"), ("BOOL", "allin")])
D_ACTS = _obj("D_ACTS", (), {s: D_ACT for s in ("preflop", "flop", "turn", "river")})
PROFILE = _obj("PROFILE", [("NUM", "vpip pfr af fold_to_bet threebet wtsd hands"), (E("PROF_SOURCE"), "source"),
                           (E("STYLE"), "style"), (E("TYPE_SRC"), "type_src")])
D_SEAT = _obj("D_SEAT", [("POS", "pos"), ("BOOL", "hero"), ("STREET", "folded"), ("X", "pid type type_src")],
              {"acts": D_ACTS, "profile": PROFILE})
CLASSES_OBJ = _obj("CLASSES_OBJ", (), {c: "NUM" for c in CLASSES})
READ = _obj("READ", [("NUM", "width"), ("TEXT", "summary line")], {"classes": CLASSES_OBJ})
GTO = _obj("GTO", [("NUM", "ev stack_used distance exploitability pot behind"), ("CARDSTR", "mapped_flop actual_flop combo"),
                   ("BOOL", "exact_flop exact_spot combo_exact stale"), ("SPOT", "spot"), ("NODE", "node"),
                   (E("ROLE"), "role"), ("TEXT", "notes"), ("STREET", "street"), ("LINE", "line"),
                   ("CARD", "mapped_turn actual_turn")],
           {"actions": _map("ACTKEY", "NUM"), "range_actions": _map("ACTKEY", "NUM"), "amounts": _map("ACTKEY", "NUM")})
ADVICE = _obj("ADVICE", [(E("ADV_ACTION"), "action"), (E("ADV_ACTION_KO"), "action_ko"), ("NUM", "size_bb"),
                         ("TEXT", "size_text reason detail plan gto_line"), (E("ADV_CONF"), "confidence"),
                         (E("COLOR_KEY"), "color_key"), (E("GTO_MODE"), "gto_mode")], {"gto": GTO})
ICM = _obj("ICM", [("NUM", "icm chip eq_fold eq_win eq_lose")])
TOURNEY = _obj("TOURNEY", [("NUMBOOL", "players_left my_rank bubble_factor bubble m_ratio payouts_left entrants paid "
                                       "prize_pool"), (E("STAGE"), "stage"), ("PAYOUTS", "payouts")], {"icm": ICM})
DECISION = _obj("DEC", [("DID", "id"),
                        ("NUM", "v t turn rev hero_acts_before players pre_raises stack_bb my_stack pot pot_total call "
                                "looseness opponents call_pct_pot spr n_aggr equity"),
                        ("DAY", "day"), ("HID", "hand_id"), ("STREET", "street state_street"), ("CARD", "hero board"),
                        ("POS", "hero_pos villain_pos ref3_villain_pos"), (E("SITUATION"), "situation"),
                        ("BOOL", "hero_ip aggressor villain_raised can_check facing_allin"),
                        (E("FLOP_LINE"), "flop_line"), ("NUMBOOL", "ante"), ("HAND_NAME", "hand_name"),
                        (E("EQ_BASIS"), "equity_basis"), ("X", "type redacted")],
                {"tex": TEX, "outs": OUTS, "bettor": BETTOR, "seats": _map("SEAT", D_SEAT),
                 "reads": _map("SEAT", READ), "advice": ADVICE, "tourney": TOURNEY})

HERO_ACTUAL = _obj("HERO_ACTUAL", [(E("HERO_ACTION"), "action"), ("NUM", "to added pct size_bb"), ("BOOL", "allin")])
OUTCOME = _obj("OUTCOME", [("CARD", "final_board"), ("STREET", "streets hero_folded"), ("BOOL", "showdown"),
                           (E("H_CONF"), "confidence"), ("WON", "hero_won"), ("NUM", "hero_delta_bb"),
                           (E("DELTA_SRC"), "delta_src"), ("X", "issues")], {"shown": _map("POS_OR_SEAT", "CARD")})
RESP = _obj("RESP", [("POS_OR_SEAT", "pos"), (E("ACT"), "act")])
LATER = _obj("LATER", [("STREET", "street"), ("POS_OR_SEAT", "pos"), (E("ACT"), "act"), ("NUM", "to pct")])
RESOLUTION = _obj("RES", [("DID", "id"), ("NUM", "t size_ratio"), (E("RES_SRC"), "src"),
                          ("BOOL", "followed bettor_continued"), (E("ACT"), "bettor_next"), (E("LINK"), "link"),
                          ("X", "type redacted")],
                  {"hero_actual": HERO_ACTUAL, "outcome": OUTCOME, "responses": RESP, "later": LATER})

SCHEMA_HANDS, SCHEMA_DECISION, SCHEMA_RESOLUTION = HAND, DECISION, RESOLUTION


def _merge(a, b):
    """두 스키마의 합집합 (같은 키면 객체 쪽, 둘 다 객체면 키 합집합)."""
    if isinstance(a, str):
        return b if not isinstance(b, str) else a
    if isinstance(b, str):
        return a
    if "k" in a and "k" in b:
        k = dict(a["k"])
        for key, v in b["k"].items():
            k[key] = _merge(k[key], v) if key in k else v
        return {"k": k}
    return a


SCHEMA_UNION = _merge(DECISION, RESOLUTION)


def _display_keys():
    seen, out = set(), {"detail", "notes"}

    def w(n):
        if isinstance(n, str) or id(n) in seen:
            return
        seen.add(id(n))
        if "m" in n:
            w(n["v"])
            return
        if n is not CLASSES_OBJ:
            out.update(n["k"])
        for c in n["k"].values():
            w(c)
    for s in (HAND, DECISION, RESOLUTION):
        w(s)
    return frozenset(out)


DISPLAY_KEYS = _display_keys()


def schema_dump():
    """정규화 덤프 (Go 와 sha256 이 같아야 한다, 명세 12.1, 13.2-6)."""
    d = {"v": FILTER_V, "hands": SCHEMA_HANDS, "decision": SCHEMA_DECISION, "resolution": SCHEMA_RESOLUTION,
         "re": RE_PATS, "classes": list(CLASSES), "nick": list(NICK_PATS), "tokens": list(NAME_TOKENS),
         "retired": sorted(RETIRED), "types": TYPES, "enums": {k: sorted(v) for k, v in ENUMS.items()},
         "text_max": TEXT_MAX, "badcp": [list(x) for x in BADCP], "accept_min": ACCEPT_MIN}
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def schema_sha256():
    return hashlib.sha256(schema_dump().encode("utf-8")).hexdigest()


def leaf_check(typ, v):
    """값 형식 검사 (업로더 D2 와 판정 17·18 이 같은 함수를 쓴다): "ok" | "ref" | "bad"."""
    if v is None or typ == "X":
        return "ok"
    t = TYPES[typ]
    bad = "ref" if t["ref"] else "bad"
    if type(v) is bool:
        kind = "bool"
    elif isinstance(v, (IntLit, FloatLit)):
        kind = "num"
    elif type(v) is str:
        kind = "str"
    else:
        kind = "obj"
    if kind not in t["j"]:
        return bad
    if kind == "str":
        if t["re"] and not _RES[t["re"]].fullmatch(v):
            return bad
        if t["en"] and v not in _ENUMSET[t["en"]]:
            return bad
        if t["text"]:
            if len(v) > TEXT_MAX:
                return bad
            for ch in v:
                o = ord(ch)
                if o < 0x20 or o >= 0x7F:
                    for lo, hi in BADCP:
                        if lo <= o <= hi:
                            return bad
    return "ok"


SEAT_RE = _RES["SEAT"]
_UNSET = object()


class Verdict:
    __slots__ = ("cls", "rule", "line", "path", "lines")

    def __init__(self, cls, rule="", line=0, path="", lines=0):
        self.cls, self.rule, self.line, self.path, self.lines = cls, rule, line, path, lines

    def ok(self):
        return self.cls == "ok"

    def __repr__(self):
        return "Verdict(%s,%s,line=%d,path=%s)" % (self.cls, self.rule, self.line, self.path)


class IntLit(str):
    pass


class FloatLit(str):
    pass


class Obj(dict):
    """파싱된 객체. 중복 키는 뒤 값이 남고 첫 중복 키 이름만 기록한다."""
    dupkey = None


class FilterBlock(Exception):
    """filter_record 가 업로드를 멈추라고 알리는 예외 (규칙 이름과 키 경로만)."""

    def __init__(self, rule, path=""):
        super().__init__(rule)
        self.rule, self.path = rule, path


def _pairs(pairs):
    o = Obj()
    for k, v in pairs:
        if k in o and o.dupkey is None:
            o.dupkey = k
        o[k] = v
    return o


def _reject_constant(name):
    raise ValueError("constant")


def parse_line(text):
    return json.loads(text, object_pairs_hook=_pairs, parse_int=IntLit, parse_float=FloatLit,
                      parse_constant=_reject_constant)


def file_kind(name):
    m = FILE_RE.fullmatch(name)
    if not m:
        return None
    try:
        datetime.date.fromisoformat(m.group(2))
    except ValueError:
        return None
    return "decisions" if m.group(1) else "hands"


def file_date(name):
    m = FILE_RE.fullmatch(name)
    return datetime.date.fromisoformat(m.group(2)) if m else None


def _too_deep(text):
    if text.count("[") + text.count("{") <= MAX_DEPTH:
        return False
    d = 0
    for m in _STRUCT.finditer(text):
        c = m.group()
        if c in "[{":
            d += 1
            if d > MAX_DEPTH:
                return True
        elif c in "]}":
            d -= 1
    return False


_LOWER = {c: c + 32 for c in range(65, 91)}


def ascii_lower(s):
    return s.translate(_LOWER)


def fold(s):
    """업로더 전용 키 정규화: NFKC + casefold, Cf 글자와 _ - . 공백을 지운다."""
    t = unicodedata.normalize("NFKC", s).casefold()
    return "".join(c for c in t if unicodedata.category(c) != "Cf" and c not in "_-. \t\n")


def has_token(low):
    return any(t in low for t in NAME_TOKENS)


# --- 키 경로 표시 (값 없음) ---
def _render(k, ctx):
    if ctx == "top":
        return k if TOP_KEY_RE.fullmatch(k) else "?"
    if ctx == "seat":
        return k if _RES["SEAT"].fullmatch(k) else "?"
    if ctx == "posseat":
        return k if _RES["POS_OR_SEAT"].fullmatch(k) else "?"
    if ctx == "q":
        return "?"
    low = ascii_lower(k)
    if low in OLD_FORBIDDEN:
        return low
    return k if k in DISPLAY_KEYS else "?"


def _child_ctx(ctx, k):
    if ctx == "top":
        if k in ("seats", "reads"):
            return "seat"
        if k == "showdown":
            return "showdown"
        if k == "outcome":
            return "outcome"
    elif ctx == "showdown" and k == "shown":
        return "seat"
    elif ctx == "outcome" and k == "shown":
        return "posseat"
    return "other"


def _child(node, k):
    """(자식 노드, 허용 여부). 노드가 없거나 잎이면 모든 키가 목록 밖."""
    if not isinstance(node, dict):
        return None, False
    if "k" in node:
        if k in node["k"]:
            return node["k"][k], True
        return None, False
    if _RES[node["m"]].fullmatch(k):
        return node["v"], True
    return None, False


def _is_actmap(n):
    return isinstance(n, dict) and n.get("m") == "ACTKEY"


def _trim(segs):
    """잎 배열의 끝 [] 는 경로에서 뺀다 (원소 하나가 틀려도 키 전체를 가리킨다)."""
    s = list(segs)
    while s and s[-1] == "[]":
        s.pop()
    return s


def _scan(v, node, segs, ctx, on_key, on_str=None, on_leaf=None, on_ctxkey=None, qmaps=False):
    """객체를 키 정렬 순, 깊이 우선으로 훑는다. 배열은 투명(각 원소에 같은 스키마).
    on_key(키, 경로 조각, 허용 여부), on_str(문자열 값, 경로 조각), on_leaf(값, 잎 형식, 경로 조각)가 참을
    돌려주면 멈춘다. on_ctxkey(키, 맥락)은 모든 키에 불린다."""
    if isinstance(v, list):
        s = segs + ["[]"]
        for e in v:
            if _scan(e, node, s, "other", on_key, on_str, on_leaf, on_ctxkey, qmaps):
                return True
        return False
    if not isinstance(v, Obj):
        if on_leaf and isinstance(node, str) and on_leaf(v, node, segs):
            return True
        return bool(type(v) is str and on_str and on_str(v, segs))
    if on_leaf and isinstance(node, str) and on_leaf(v, node, segs):
        return True
    for k in sorted(v):
        child, ok = _child(node, k)
        if on_ctxkey:
            on_ctxkey(k, ctx)
        s = segs + ["?" if qmaps and ctx in ("seat", "posseat", "q") else _render(k, ctx)]
        if on_key(k, s, ok):
            return True
        cctx = _child_ctx(ctx, k)
        if ok and (_is_actmap(child) or (k == "classes" and isinstance(child, dict) and "k" in child)):
            cctx = "q"
        if _scan(v[k], child, s, cctx, on_key, on_str, on_leaf, on_ctxkey, qmaps):
            return True
    return False


def _any_bad(typ, v):
    if isinstance(v, list):
        return any(_any_bad(typ, e) for e in v)
    return leaf_check(typ, v) == "bad"


def leaf_hit(top, sch, want):
    """줄 전체에서 want("ref" 또는 "bad")인 첫 잎의 경로(문자열), 없으면 None."""
    found = []

    def leaf(v, typ, segs):
        if typ == "X":
            return False
        if segs == ["id"] and type(v) is not str:
            return False                    # id 가 문자열이 아니면 bad_id 가 본다
        if leaf_check(typ, v) == want:
            found.append(".".join(_trim(segs)))
            return True
        return False
    _scan(top, sch, [], "top", lambda k, s, ok: False, None, leaf)
    return found[0] if found else None


def schema_for(kind, top):
    if kind == "hands":
        return SCHEMA_HANDS
    t = top.get("type")
    if type(t) is str:
        if t == "decision":
            return SCHEMA_DECISION
        if t == "resolution":
            return SCHEMA_RESOLUTION
    return SCHEMA_UNION


def _obj_or_null(v):
    """(obj, is_null, ok)"""
    if v is None:
        return None, True, True
    if isinstance(v, Obj):
        return v, False, True
    return None, False, False


def _is_str(v):
    return type(v) is str


def _key_set(seat, name, keys, rule, sp):
    if name not in seat:
        return None
    o, null, ok = _obj_or_null(seat[name])
    if not ok:
        return rule, sp + "." + name
    if not null:
        for k in sorted(o):
            if k not in keys:
                return rule, sp + "." + name + "." + _render(k, "other")
    return None


def _seat_keyed(v, path, rule, rname):
    o, null, ok = _obj_or_null(v)
    if not ok:
        return rule, path
    if not null:
        pat = _RES[rname]
        for k in sorted(o):
            if not pat.fullmatch(k):
                return rule, path + ".?"
    return None


def dup_path(top):
    """중복 키가 있으면 그 키 경로(값 없음), 없으면 None."""
    def w(v, segs, ctx):
        if isinstance(v, Obj):
            if v.dupkey is not None:
                return ".".join(segs + [_render(v.dupkey, ctx)])
            for k in sorted(v):
                r = w(v[k], segs + [_render(k, ctx)], _child_ctx(ctx, k))
                if r:
                    return r
        elif isinstance(v, list):
            for e in v:
                r = w(e, segs + ["[]"], "other")
                if r:
                    return r
        return None
    return w(top, [], "top")


def _struct_violation(sch, top):
    """좌석 구조(5~7), shown(13), reads(14) 규칙. 업로더 필터도 쓴다 (버리지 않고 차단할 것들)."""
    keys = sch["k"]
    if "seats" in keys and "seats" in top:
        seats, null, ok = _obj_or_null(top["seats"])
        if not ok:
            return "seats_not_object", "seats"
        if not null:
            for sk in sorted(seats):
                sp = "seats." + _render(sk, "seat")
                if not SEAT_RE.fullmatch(sk):
                    return "seat_key_format", sp
                if not isinstance(seats[sk], Obj):
                    return "seat_not_object", sp
    r = _shown_reads(sch, top)
    return r if r else None


def _shown_reads(sch, top):
    keys = sch["k"]
    if "showdown" in keys:
        sd = top.get("showdown")
        if isinstance(sd, Obj) and "shown" in sd:
            r = _seat_keyed(sd["shown"], "showdown.shown", "shown_key_format", "SEAT")
            if r:
                return r
    if "outcome" in keys:
        oc = top.get("outcome")
        if isinstance(oc, Obj) and "shown" in oc:
            r = _seat_keyed(oc["shown"], "outcome.shown", "shown_key_format", "POS_OR_SEAT")
            if r:
                return r
    if "reads" in keys and "reads" in top:
        r = _seat_keyed(top["reads"], "reads", "reads_key_format", "SEAT")
        if r:
            return r
    return None


def _check_top(kind, top, accept):
    """줄 단위 규칙 1~19 (명세 12.4, 13.2-4). Verdict 또는 None."""
    dp = dup_path(top)
    if dp is not None:
        return Verdict("block", "dup_key", 0, dp)
    r = top.get("redacted")
    if type(r) is not IntLit or not INT_LIT_RE.fullmatch(r):
        return Verdict("block", "missing_redacted", 0, "redacted")
    if not VER_RE.fullmatch(r) or not accept[0] <= int(r) <= accept[1]:
        return Verdict("format", "filter_version")
    sch = schema_for(kind, top)
    keys = sch["k"]
    found = []

    def forb(k, s, ok):
        if not ok and has_token(ascii_lower(k)):
            found.append(".".join(s))
            return True
        return False
    _scan(top, sch, [], "top", forb)
    if found:
        return Verdict("block", "forbidden_key", 0, found[0])
    # 5~12: 좌석
    if "seats" in keys and "seats" in top:
        seats, null, ok = _obj_or_null(top["seats"])
        if not ok:
            return Verdict("block", "seats_not_object", 0, "seats")
        if not null:
            snode = keys["seats"]["v"]["k"]
            for sk in sorted(seats):
                sp = "seats." + _render(sk, "seat")
                if not SEAT_RE.fullmatch(sk):
                    return Verdict("block", "seat_key_format", 0, sp)
                seat = seats[sk]
                if not isinstance(seat, Obj):
                    return Verdict("block", "seat_not_object", 0, sp)
                for k in sorted(seat):
                    if k not in snode:
                        return Verdict("block", "seat_field", 0, sp + "." + _render(k, "other"))
                if "pid" in seat and seat["pid"] is not None:
                    p = seat["pid"]
                    if not _is_str(p) or not PID_RE.fullmatch(p):
                        return Verdict("block", "pid_format", 0, sp + ".pid")
                if "type" in seat and seat["type"] is not None:
                    t = seat["type"]
                    if not _is_str(t) or t not in SEAT_TYPES:
                        return Verdict("block", "seat_value", 0, sp + ".type")
                if "type_src" in seat and seat["type_src"] is not None:
                    t = seat["type_src"]
                    if not _is_str(t) or t != "popup":
                        return Verdict("block", "seat_value", 0, sp + ".type_src")
                r2 = None
                if "profile" in snode:
                    r2 = _key_set(seat, "profile", snode["profile"]["k"], "profile_field", sp)
                if not r2 and "type_stats" in snode:
                    r2 = _key_set(seat, "type_stats", snode["type_stats"]["k"], "type_stats_field", sp)
                if r2:
                    return Verdict("block", r2[0], 0, r2[1])
    r2 = _shown_reads(sch, top)
    if r2:
        return Verdict("block", r2[0], 0, r2[1])
    # 15. unknown_key
    _scan(top, sch, [], "top", lambda k, s, ok: (not ok) and (found.append(".".join(s)) or True))
    if found:
        return Verdict("block", "unknown_key", 0, found[0])
    # 16. issue_code
    if "issues" in keys and "issues" in top:
        iv = top["issues"]
        if iv is not None:
            if not isinstance(iv, list):
                return Verdict("block", "issue_code", 0, "issues")
            for e in iv:
                if not isinstance(e, Obj) or not _is_str(e.get("code")) or not CODE_RE.fullmatch(e["code"]):
                    return Verdict("block", "issue_code", 0, "issues")
    if "outcome" in keys and isinstance(top.get("outcome"), Obj) and "issues" in top["outcome"]:
        iv = top["outcome"]["issues"]
        if iv is not None:
            if not isinstance(iv, list) or any(not _is_str(e) or not CODE_RE.fullmatch(e) for e in iv):
                return Verdict("block", "issue_code", 0, "outcome.issues")
    # 17. seat_ref, 18. value_format
    p = leaf_hit(top, sch, "ref")
    if p is not None:
        return Verdict("block", "seat_ref", 0, p)
    p = leaf_hit(top, sch, "bad")
    if p is not None:
        return Verdict("block", "value_format", 0, p)
    # 19. nick_value
    def hit(s):
        return any(p.search(s) for p in NICK_RES)
    _scan(top, sch, [], "top", lambda k, s, ok: hit(k) and (found.append(".".join(s)) or True),
          lambda v, s: hit(v) and (found.append(".".join(s)) or True))
    if found:
        return Verdict("block", "nick_value", 0, found[0])
    return None


def check_line(kind, line, accept=(FILTER_V, FILTER_V)):
    """line: 개행을 뺀 바이트."""
    def f(rule):
        return Verdict("format", rule)
    if len(line) > MAX_LINE:
        return f("line_too_long")
    if len(line) == 0:
        return f("empty_line")
    if b"\r" in line:
        return f("bad_line")
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        return f("bad_utf8")
    if _too_deep(text):
        return f("too_deep")
    try:
        val = parse_line(text)
    except (ValueError, RecursionError):
        return f("bad_json")
    if not isinstance(val, Obj):
        return f("not_object")
    v = _check_top(kind, val, accept)
    if v is not None:
        return v
    if not _is_str(val.get("id")):
        return f("bad_id")
    if kind == "decisions":
        t = val.get("type")
        if not _is_str(t) or t not in ("decision", "resolution"):
            return f("bad_type")
    return Verdict("ok")


def check_body(name, body, accept=(FILTER_V, FILTER_V)):
    kind = file_kind(name)
    if kind is None:
        raise ValueError("bad file name")
    return check_bytes(kind, body, accept)


def check_bytes(kind, body, accept=(FILTER_V, FILTER_V)):
    if len(body) > MAX_BODY:
        return Verdict("too_large", "file_too_large")
    if len(body) == 0:
        return Verdict("format", "empty_body")
    if body[-1:] != b"\n":
        return Verdict("format", "no_trailing_newline")
    n = body.count(b"\n")
    if n > MAX_LINES:
        return Verdict("too_large", "too_many_lines")
    first = None
    for i, line in enumerate(body[:-1].split(b"\n"), 1):
        v = check_line(kind, line, accept)
        v.line = i
        if v.cls == "block":
            v.lines = n
            return v
        if v.cls == "format" and first is None:
            first = v
    if first is not None:
        first.lines = n
        return first
    return Verdict("ok", lines=n)


# --- 업로더 필터 (명세 12.3). Go 에는 없다 ---
def filter_record(kind, rec, paths=None, vstats=None):
    """(걸러낸 레코드, 버린 키·원소 수). 차단해야 하면 FilterBlock. paths 는 버린 경로 Counter(값 없음).
    vstats({"values": 0, "vpaths": Counter()})가 있으면 D2 에서 형식이 틀려 버린 값 수와 경로를 더한다."""
    sch = schema_for(kind, rec)
    # A. 원본 전체에서 목록 밖 키가 이름처럼 보이면 차단
    hit = []

    def forb(k, s, ok):
        if not ok and has_token(fold(k)):
            hit.append(".".join(s))
            return True
        return False
    _scan(rec, sch, [], "top", forb)
    if hit:
        raise FilterBlock("forbidden_key", hit[0])
    # B. 구조가 틀린 좌석·shown·reads 는 버리지 않고 차단
    sv = _struct_violation(sch, rec)
    if sv:
        raise FilterBlock(sv[0], sv[1])
    # C. 스키마대로 새로 만든다
    dropped = [0]

    def drop(segs, k, ctx):
        dropped[0] += 1
        if paths is not None:
            paths[".".join(segs + [_render(k, ctx)])] += 1

    def prune(v, node, segs, ctx):
        if isinstance(v, list):
            return [prune(e, node, segs + ["[]"], "other") for e in v]
        if not isinstance(v, Obj):
            return v
        out = Obj()
        for k in sorted(v):
            child, ok = _child(node, k)
            if not ok or k in RETIRED:
                drop(segs, k, ctx)
                continue
            cctx = _child_ctx(ctx, k)
            if _is_actmap(child) or (k == "classes" and isinstance(child, dict) and "k" in child):
                cctx = "q"
            out[k] = prune(v[k], child, segs + [_render(k, ctx)], cctx)
        return out
    out = prune(rec, sch, [], "top")
    # D. issues 는 코드만
    if "issues" in out and out["issues"] is not None:
        iv = out["issues"]
        if isinstance(iv, list):
            keep = []
            for e in iv:
                if isinstance(e, Obj) and _is_str(e.get("code")) and CODE_RE.fullmatch(e["code"]):
                    o = Obj()
                    o["code"] = e["code"]
                    keep.append(o)
                else:
                    dropped[0] += 1
                    if paths is not None:
                        paths["issues"] += 1
            out["issues"] = keep
        else:
            del out["issues"]
            dropped[0] += 1
            if paths is not None:
                paths["issues"] += 1
    oc = out.get("outcome")
    if isinstance(oc, Obj) and "issues" in oc and oc["issues"] is not None:
        iv = oc["issues"]
        if isinstance(iv, list):
            keep = [e for e in iv if _is_str(e) and CODE_RE.fullmatch(e)]
            for _ in range(len(iv) - len(keep)):
                dropped[0] += 1
                if paths is not None:
                    paths["outcome.issues"] += 1
            oc["issues"] = keep
        else:
            del oc["issues"]
            dropped[0] += 1
            if paths is not None:
                paths["outcome.issues"] += 1

    # D2. 값 형식: 좌석 참조가 틀리면 차단, 그 밖은 그 키를 통째로 버린다 (명세 13.2-5)
    sch2 = schema_for(kind, out)
    p = leaf_hit(out, sch2, "ref")
    if p is not None:
        raise FilterBlock("seat_ref", p)

    def d2(v, node, segs, ctx):
        if isinstance(v, list):
            return [d2(e, node, segs + ["[]"], "other") for e in v]
        if not isinstance(v, Obj):
            return v
        res = Obj()
        for k in sorted(v):
            child, ok = _child(node, k)
            sg = segs + [_render(k, ctx)]
            if isinstance(child, str):
                if _any_bad(child, v[k]):
                    if vstats is not None:
                        vstats["values"] += 1
                        vstats["vpaths"][".".join(sg)] += 1
                    continue
                res[k] = v[k]
                continue
            cctx = _child_ctx(ctx, k)
            if ok and (_is_actmap(child) or (k == "classes" and isinstance(child, dict) and "k" in child)):
                cctx = "q"
            res[k] = d2(v[k], child, sg, cctx)
        return res
    out = d2(out, sch2, [], "top")

    # E. 남은 모든 문자열 값과 키에 닉네임 모양이 있으면 차단 (원문과 NFKC 둘 다)
    def nick(s):
        return any(p.search(s) or p.search(unicodedata.normalize("NFKC", s)) for p in NICK_RES)
    hit = []
    _scan(out, schema_for(kind, out), [], "top", lambda k, s, ok: nick(k) and (hit.append(".".join(s)) or True),
          lambda v, s: nick(v) and (hit.append(".".join(s)) or True))
    if hit:
        raise FilterBlock("nick_value", hit[0])
    out["redacted"] = IntLit(str(FILTER_V))
    return out, dropped[0]


# --- 설정 파일 읽기 (명세 12.8 M4). 업로더와 수신기가 같이 쓴다 ---
class ConfigPerms(Exception):
    """설정 파일이나 그 폴더의 소유자·권한·링크가 안전하지 않다 (메시지에 값 없음)."""


def read_private_json(path):
    """O_NOFOLLOW 로 열어 fstat 로 검사한 같은 파일을 읽는다: 일반 파일, 소유자=내 euid, 그룹·타인 권한 없음.
    부모 폴더는 링크가 아니고 소유자가 같고 그룹·타인 쓰기 권한이 없어야 한다."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise ConfigPerms("file is a link")
        raise
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise ConfigPerms("file is not regular")
        if st.st_uid != os.geteuid():
            raise ConfigPerms("file owner")
        if st.st_mode & 0o077:
            raise ConfigPerms("file mode")
        parent = os.path.dirname(os.path.abspath(path))
        ps = os.lstat(parent)
        if stat.S_ISLNK(ps.st_mode) or not stat.S_ISDIR(ps.st_mode):
            raise ConfigPerms("folder is a link")
        if ps.st_uid != os.geteuid():
            raise ConfigPerms("folder owner")
        if ps.st_mode & 0o022:
            raise ConfigPerms("folder mode")
        return json.loads(f.read().decode("utf-8"))
