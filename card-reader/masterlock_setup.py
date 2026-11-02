"""마스터 잠금 값을 만든다 (표준 라이브러리만 사용).

  python masterlock_setup.py                      비밀번호를 두 번 입력받아 사용자 설정 파일을 만든다
  python masterlock_setup.py --stdin              비밀번호를 표준 입력 첫 줄에서 받는다 (확인 입력 없음)
  python masterlock_setup.py --force              이미 있는 설정 파일을 바꾼다
  python masterlock_setup.py --write-build-module exe 빌드용 masterlock_build.py 를 만든다
                                                  (설정 파일이 있으면 그 값을 옮기고, 없으면 새로 만든다)

비밀번호는 명령행 인자로 받지 않는다. 해시, 솔트, 비밀번호는 화면에 출력하지 않는다.
--config-path, --build-path 는 도구가 쓰는 파일 위치만 바꾼다. masterlock.py 가 읽는 위치는 바뀌지 않는다.
"""
import argparse
import getpass
import hashlib
import os
import secrets
import sys
import tempfile

import masterlock as ml

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BUILD_PATH = os.path.join(HERE, "masterlock_build.py")


def make_params(password):
    salt = secrets.token_bytes(ml.MIN_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ml.MIN_ITERATIONS)
    return salt, ml.MIN_ITERATIONS, digest


def config_text(params):
    import json
    salt, iterations, digest = params
    return json.dumps({"format": ml.FORMAT, "algo": ml.ALGO, "salt": salt.hex(),
                       "iterations": iterations, "hash": digest.hex()}, indent=2) + "\n"


def build_text(params):
    salt, iterations, digest = params
    return ("# masterlock_setup.py 가 만든 파일. 커밋하지 말고, 빌드가 끝나면 지우거나 안전하게 보관한다.\n"
            f'SALT = "{salt.hex()}"\nITERATIONS = {iterations}\nHASH = "{digest.hex()}"\n')


def write_private(path, text, force):
    """디렉터리 700, 파일 600, 임시 파일을 만든 뒤 원자적으로 교체. 이미 있으면 force 없이는 거부."""
    path = os.path.abspath(path)
    if os.path.lexists(path) and not force:
        raise SystemExit("이미 있습니다. 바꾸려면 --force 를 쓰세요: " + path)
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder, mode=0o700, exist_ok=True)
        os.chmod(folder, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".masterlock-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def read_password(use_stdin):
    if use_stdin:
        line = sys.stdin.readline()
        pw = line[:-1] if line.endswith("\n") else line
        if pw.endswith("\r"):
            pw = pw[:-1]
    else:
        pw = getpass.getpass("새 마스터 비밀번호: ")
        if getpass.getpass("한 번 더: ") != pw:
            raise SystemExit("두 입력이 다릅니다.")
    if not pw:
        raise SystemExit("빈 비밀번호는 쓸 수 없습니다.")
    return pw


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stdin", action="store_true", help="비밀번호를 표준 입력 첫 줄에서 받는다")
    ap.add_argument("--force", action="store_true", help="이미 있는 대상 파일을 바꾼다")
    ap.add_argument("--write-build-module", action="store_true", help="exe 빌드용 masterlock_build.py 를 만든다")
    ap.add_argument("--config-path", help="설정 파일 위치 (기본: 사용자 설정 폴더)")
    ap.add_argument("--build-path", help="빌드 모듈 위치 (기본: 이 폴더의 masterlock_build.py)")
    args = ap.parse_args(argv)

    try:
        config = args.config_path or ml.config_path()
    except ml.LockConfigError:
        raise SystemExit("설정 폴더를 알 수 없습니다. --config-path 로 지정하세요.")

    if args.write_build_module:
        build = args.build_path or DEFAULT_BUILD_PATH
        if os.path.lexists(build) and not args.force:
            raise SystemExit("이미 있습니다. 바꾸려면 --force 를 쓰세요: " + os.path.abspath(build))
        if os.path.exists(config):
            try:
                params = ml._from_file(config)
            except ml.LockConfigError as e:
                raise SystemExit(f"설정 파일을 쓸 수 없습니다 ({e}): {config}")
            source = "설정 파일의 값을 옮겼습니다"
        else:
            params = make_params(read_password(args.stdin))
            source = "새 값을 만들었습니다 (사용자 설정 파일은 만들지 않았습니다)"
        print(source + ": " + write_private(build, build_text(params), args.force))
        return 0

    path_exists = os.path.lexists(config)
    if path_exists and not args.force:
        raise SystemExit("이미 있습니다. 바꾸려면 --force 를 쓰세요: " + os.path.abspath(config))
    params = make_params(read_password(args.stdin))
    print("설정 파일을 만들었습니다: " + write_private(config, config_text(params), args.force))
    return 0


if __name__ == "__main__":
    sys.exit(main())
