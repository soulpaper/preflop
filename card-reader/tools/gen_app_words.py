"""records_appwords.py 생성기. 앱 소스(card-reader/*.py)의 문자열 상수(f-string 조각 포함)에서 한글 낱말(2~12자)을 뽑는다.
다시 만들기:  cd card-reader && python3 tools/gen_app_words.py
test_records.py 가 파일이 최신인지 비교하므로 앱 문구를 바꾸면 다시 만들어야 한다."""
import ast
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "records_appwords.py")
_HANGUL = re.compile(r"[가-힣]+")
sys.path.insert(0, ROOT)
from records_sync import APPWORD_EXCLUDE as EXCLUDE   # 정규식 글자 묶음 조각 - 앱 소스 경로와 같은 값


def _fold(s):
    import unicodedata
    t = unicodedata.normalize("NFKC", str(s)).casefold()
    return "".join(c for c in t if unicodedata.category(c) != "Cf")


def words_of(text):
    return {w for w in _HANGUL.findall(_fold(text)) if 2 <= len(w) <= 12}


def collect(root=ROOT):
    found = set()
    for p in sorted(glob.glob(os.path.join(root, "*.py"))):
        n = os.path.basename(p)
        if n.startswith("test_") or n.startswith("records_"):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except (OSError, SyntaxError, ValueError):
            continue
        skip = {id(nd.value) for nd in ast.walk(tree)
                if isinstance(nd, ast.Expr) and isinstance(nd.value, ast.Constant)}
        for nd in ast.walk(tree):
            if isinstance(nd, ast.Constant) and isinstance(nd.value, str) and id(nd) not in skip:
                found |= words_of(nd.value)
    return sorted(found - EXCLUDE)


def render(words):
    lines = ["# 자동 생성 파일 - 직접 고치지 않는다. 다시 만들기: cd card-reader && python3 tools/gen_app_words.py",
             "# 앱 소스 문자열(f-string 조각 포함)의 한글 낱말 2~12자. exe 처럼 소스가 없어도 짧은 닉네임이 앱 단어와",
             "# 같으면 건너뛰기 위한 기본 어휘 (records_sync._app_phrases).",
             "WORDS = ("]
    cur = "   "
    for w in words:
        item = ' "%s",' % w
        if len(cur) + len(item) > 110:
            lines.append(cur)
            cur = "   "
        cur += item
    if cur.strip():
        lines.append(cur)
    lines.append(")")
    return "\n".join(lines) + "\n"


def main():
    ws = collect()
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(render(ws))
    print(len(ws))


if __name__ == "__main__":
    sys.exit(main())
