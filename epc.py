"""이미지의 촬영 메타데이터(EXIF)를 읽어 연도/월/카메라 폴더로 정리한다.

사용 예:
    epc                          # 현재 폴더를 제자리에서 정리
    epc --dry-run                # 계획만 확인
    epc -i ./사진 -o ./정리됨 --cp
    epc --pattern "%Y/%m"        # 카메라 구분 없이 연/월만
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import struct
import sys
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ExifTags

# EXIF 태그 이름 -> 숫자 ID (촬영 시각 우선순위 순)
DATE_TAGS = [
    ExifTags.Base.DateTimeOriginal.value,
    ExifTags.Base.DateTimeDigitized.value,
    ExifTags.Base.DateTime.value,
]

IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".webp",   # 일반 이미지
    ".arw", ".nef", ".dng",             # RAW (TIFF 기반)
    ".cr2", ".srw",                     # 캐논 / 삼성 RAW (역시 TIFF 기반)
}

# IMG_20230415_101530.jpg / 2023-04-15 10.15.30.png 같은 파일명 패턴
FILENAME_DATE_RE = re.compile(r"(19|20)\d{2}[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])")


TAG_MAKE = 0x010F                   # 제조사 (예: SONY)
TAG_MODEL = 0x0110                  # 기종   (예: ILCE-7)
UNKNOWN_CAMERA = "_카메라없음"

# 폴더명에 쓸 수 없는 문자
_BAD_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean_camera(make: str | None, model: str | None) -> str | None:
    """Make/Model 을 폴더명으로 쓸 수 있는 한 덩어리로 만든다."""
    make = (make or "").strip()
    model = (model or "").strip()
    if not make and not model:
        return None
    # Model 에 이미 제조사가 들어있으면 (예: "Canon EOS R6") 중복을 피한다
    if make and model and not model.lower().startswith(make.split()[0].lower()):
        name = f"{make} {model}"
    else:
        name = model or make
    name = _BAD_CHARS_RE.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")    # 윈도우는 끝의 점·공백을 싫어한다
    return name or None


def meta_from_exif(path: Path) -> tuple[datetime | None, str | None]:
    """Pillow 로 (촬영일시, 카메라) 를 읽는다."""
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            # TIFF 계열(DNG 등)은 Exif IFD 를 열린 파일에서 지연 로딩하므로
            # 파일이 닫히기 전에 읽어 둬야 한다 (닫힌 뒤엔 ValueError)
            exif_ifd = exif.get_ifd(ExifTags.IFD.Exif) if exif else {}
    except Exception:
        return None, None
    if not exif:
        return None, None

    camera = clean_camera(exif.get(TAG_MAKE), exif.get(TAG_MODEL))

    for tag in DATE_TAGS:
        # DateTimeOriginal/Digitized는 Exif IFD 안에 있는 경우가 많다
        raw = exif.get(tag) or exif_ifd.get(tag)
        if not raw:
            continue
        try:
            return datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S"), camera
        except ValueError:
            continue
    return None, camera


# --- TIFF/RAW 직접 파싱 ---------------------------------------------------
# Pillow는 소니 ARW 등 제조사 RAW를 열지 못하고 UnidentifiedImageError를 낸다.
# 이 파일들은 모두 TIFF 컨테이너이므로 IFD를 직접 훑어 날짜 태그만 꺼내온다.

TIFF_EXIF_IFD = 0x8769
TIFF_DATE_TAGS = (0x9003, 0x9004)   # DateTimeOriginal, DateTimeDigitized
TIFF_DATETIME = 0x0132              # IFD0 의 DateTime
TIFF_MAX_ENTRIES = 512              # 손상된 파일이 거대한 IFD를 주장하는 것 방지

# 엔트리: (타입, 개수, 값필드 4바이트)
_Entry = tuple[int, int, bytes]


def _read_ifd(f, offset: int, endian: str) -> dict[int, _Entry]:
    """IFD 하나를 읽어 {태그: 엔트리} 로 돌려준다. 필요한 구간만 seek 해서 읽는다."""
    if offset <= 0:
        return {}
    f.seek(offset)
    head = f.read(2)
    if len(head) < 2:
        return {}
    (count,) = struct.unpack(endian + "H", head)
    count = min(count, TIFF_MAX_ENTRIES)

    raw = f.read(count * 12)        # 엔트리 배열은 연속이라 한 번에
    tags = {}
    for i in range(len(raw) // 12):
        tag, typ, n = struct.unpack_from(endian + "HHI", raw, i * 12)
        tags[tag] = (typ, n, raw[i * 12 + 8:i * 12 + 12])
    return tags


def _ascii_tag(f, endian: str, entry: _Entry) -> str | None:
    typ, n, value = entry
    if typ != 2 or n == 0:          # 2 = ASCII
        return None
    if n <= 4:                      # 4바이트 이하는 값 필드에 인라인 저장
        data = value[:n]
    else:
        (ptr,) = struct.unpack(endian + "I", value)
        f.seek(ptr)
        data = f.read(n)
    return data.split(b"\x00")[0].decode("ascii", "replace")


def meta_from_tiff(path: Path) -> tuple[datetime | None, str | None]:
    """TIFF/RAW 를 직접 읽어 (촬영일시, 카메라) 를 돌려준다."""
    camera = None
    try:
        with path.open("rb") as f:
            header = f.read(8)
            if len(header) < 8:
                return None, None
            if header[:2] == b"II":
                endian = "<"
            elif header[:2] == b"MM":
                endian = ">"
            else:
                return None, None

            (ifd0_off,) = struct.unpack_from(endian + "I", header, 4)
            ifd0 = _read_ifd(f, ifd0_off, endian)

            camera = clean_camera(
                _ascii_tag(f, endian, ifd0[TAG_MAKE]) if TAG_MAKE in ifd0 else None,
                _ascii_tag(f, endian, ifd0[TAG_MODEL]) if TAG_MODEL in ifd0 else None,
            )

            candidates = []
            if TIFF_EXIF_IFD in ifd0:
                (exif_off,) = struct.unpack(endian + "I", ifd0[TIFF_EXIF_IFD][2])
                exif_ifd = _read_ifd(f, exif_off, endian)
                candidates += [exif_ifd[t] for t in TIFF_DATE_TAGS if t in exif_ifd]
            if TIFF_DATETIME in ifd0:
                candidates.append(ifd0[TIFF_DATETIME])

            for entry in candidates:
                raw = _ascii_tag(f, endian, entry)
                if not raw:
                    continue
                try:
                    return datetime.strptime(raw.strip()[:19], "%Y:%m:%d %H:%M:%S"), camera
                except ValueError:
                    continue
    except (OSError, struct.error, IndexError):
        return None, camera
    return None, camera


def meta_from_filename(path: Path) -> tuple[datetime | None, str | None]:
    m = FILENAME_DATE_RE.search(path.stem)
    if not m:
        return None, None
    digits = re.sub(r"[-_.]", "", m.group(0))
    try:
        return datetime.strptime(digits, "%Y%m%d"), None
    except ValueError:
        return None, None


# 배터리 방전으로 시계가 리셋된 카메라는 1980:01:01 같은 값을,
# 시계를 잘못 맞춘 카메라는 미래 날짜를 EXIF에 써 넣는다. 그대로 믿으면
# 엉뚱한 연도 폴더가 생기므로 상식적인 범위를 벗어나면 신뢰하지 않는다.
EARLIEST_PLAUSIBLE = datetime(1990, 1, 1)
FUTURE_TOLERANCE = timedelta(days=1)    # 시간대 차이 정도는 허용


def is_plausible(dt: datetime) -> bool:
    return EARLIEST_PLAUSIBLE <= dt <= datetime.now() + FUTURE_TOLERANCE


def resolve_meta(path: Path, allow_mtime: bool,
                 trust_all: bool = False) -> tuple[datetime | None, str | None, str | None]:
    """(촬영일시, 날짜 출처, 카메라) 를 돌려준다.

    날짜를 앞 단계에서 못 구해도 카메라는 계속 찾는다 — 날짜가 파일명에서
    나오더라도 카메라 이름은 EXIF 에 남아 있을 수 있기 때문이다.
    """
    sources = (
        (meta_from_exif, "exif"),
        (meta_from_tiff, "exif(raw)"),   # Pillow가 못 여는 RAW 대응
        (meta_from_filename, "파일명"),
    )
    found_dt: datetime | None = None
    origin: str | None = None
    camera: str | None = None

    for getter, label in sources:
        if found_dt and camera:         # 둘 다 구했으면 더 볼 필요 없다
            break
        dt, cam = getter(path)
        if cam and not camera:
            camera = cam
        if not dt or found_dt:
            continue
        if trust_all or is_plausible(dt):
            found_dt, origin = dt, label
        else:
            print(f"[경고] {path.name}: {label} 날짜 {dt:%Y-%m-%d}가 비정상 범위 — 무시함",
                  file=sys.stderr)

    if found_dt is None and allow_mtime:
        dt = datetime.fromtimestamp(path.stat().st_mtime)
        if trust_all or is_plausible(dt):
            found_dt, origin = dt, "수정시각"

    return found_dt, origin, camera


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def unique_target(target: Path, source: Path) -> Path | None:
    """충돌 시 쓸 경로. 내용이 같은 파일이 이미 있으면 None(건너뛰기)."""
    if not target.exists():         # 대부분 여기서 끝난다 — 해시 계산 없음
        return target

    src_size = source.stat().st_size
    src_hash = None                 # 크기가 같은 후보를 만났을 때만 계산

    def same_content(other: Path) -> bool:
        nonlocal src_hash
        if other.stat().st_size != src_size:
            return False
        if src_hash is None:
            src_hash = file_hash(source)
        return file_hash(other) == src_hash

    if same_content(target):
        return None
    for n in range(1, 1000):
        candidate = target.with_name(f"{target.stem}_{n}{target.suffix}")
        if not candidate.exists():
            return candidate
        if same_content(candidate):
            return None
    raise RuntimeError(f"이름 충돌을 해결하지 못함: {target}")


# --- 작업 기록 / 되돌리기 --------------------------------------------------
# 실제로 옮긴 내역을 출력 폴더의 .epc/ 아래에 한 줄씩 남겨두고,
# --undo 는 가장 최근 기록을 역순으로 되감는다.

JOURNAL_DIR = ".epc"


def journal_dir(dest: Path) -> Path:
    return dest / JOURNAL_DIR


def latest_journal(dest: Path) -> Path | None:
    d = journal_dir(dest)
    if not d.is_dir():
        return None
    logs = sorted(d.glob("journal-*.jsonl"))
    return logs[-1] if logs else None


def prune_empty_dirs(start: Path, stop: Path) -> None:
    """start 부터 stop 직전까지, 비어 있는 폴더를 위로 올라가며 지운다."""
    cur = start
    while cur != stop and stop in cur.parents:
        try:
            next(cur.iterdir())
            return                  # 뭔가 남아 있으면 중단
        except StopIteration:
            pass
        except OSError:
            return
        try:
            cur.rmdir()
        except OSError:
            return
        cur = cur.parent


def run_undo(dest: Path, dry_run: bool) -> int:
    log = latest_journal(dest)
    if log is None:
        print(f"되돌릴 기록이 없습니다 — {journal_dir(dest)} 안에 journal 파일이 없음",
              file=sys.stderr)
        return 1

    records = []
    with log.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    print(f"되돌릴 기록: {log.name} ({len(records)}건)\n", flush=True)
    stats = {"복구": 0, "건너뜀": 0, "실패": 0}

    for rec in reversed(records):       # 나중에 한 것부터
        src, dst = Path(rec["src"]), Path(rec["dst"])
        op = rec.get("op", "move")

        if not dst.exists():
            print(f"[건너뜀] {dst.name}: 옮겨진 파일이 이미 없음")
            stats["건너뜀"] += 1
            continue

        if op == "copy":
            # 복사였으면 원본이 그대로 남아 있어야 하고, 사본만 지우면 된다.
            if not src.exists():
                print(f"[건너뜀] {dst.name}: 원본이 없어 사본을 지우지 않음 — {src}",
                      file=sys.stderr)
                stats["건너뜀"] += 1
                continue
            print(f"[{'예정' if dry_run else '삭제'}] 사본 {dst.name}")
            if not dry_run:
                try:
                    dst.unlink()
                    prune_empty_dirs(dst.parent, dest)
                except OSError as e:
                    print(f"[실패] {dst.name}: {e}", file=sys.stderr)
                    stats["실패"] += 1
                    continue
            stats["복구"] += 1
            continue

        # 이동이었으면 원래 자리로 되돌린다.
        if src.exists():
            print(f"[건너뜀] {dst.name}: 원래 자리에 다른 파일이 있음 — {src}",
                  file=sys.stderr)
            stats["건너뜀"] += 1
            continue

        print(f"[{'예정' if dry_run else '복구'}] {dst.name} → {src}")
        if not dry_run:
            try:
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
                prune_empty_dirs(dst.parent, dest)
            except OSError as e:
                print(f"[실패] {dst.name}: {e}", file=sys.stderr)
                stats["실패"] += 1
                continue
        stats["복구"] += 1

    if not dry_run and stats["실패"] == 0:
        # 다음 --undo 가 이 기록을 또 되감지 않도록 이름을 바꿔 보관한다.
        log.rename(log.with_suffix(".jsonl.undone"))

    print("\n--- 요약 ---")
    print(f"복구 {stats['복구']}건 / 건너뜀 {stats['건너뜀']}건 / 실패 {stats['실패']}건")
    if dry_run:
        print("(--dry-run 이므로 실제 파일은 변경되지 않았습니다)")
    return 0


def iter_images(src: Path, recursive: bool):
    it = src.rglob("*") if recursive else src.glob("*")
    for p in sorted(it):
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES:
            yield p


def main() -> int:
    ap = argparse.ArgumentParser(
        description="EXIF 촬영일·카메라 기준으로 이미지를 연도/월/카메라 폴더로 정리")
    # 입력은 위치 인자로도, -i 옵션으로도 지정할 수 있고, 없으면 현재 폴더.
    ap.add_argument("input_pos", nargs="?", type=Path, metavar="입력폴더",
                    help="정리할 파일이 있는 폴더 (기본: 현재 폴더)")
    ap.add_argument("-i", "--input", dest="input_opt", type=Path, metavar="폴더",
                    help="정리할 파일이 있는 폴더 (위치 인자와 동일)")
    ap.add_argument("-o", "--output", type=Path, metavar="폴더",
                    help="정리해 넣을 폴더 (기본: 입력 폴더와 동일)")
    ap.add_argument("--cp", "--copy", dest="copy", action="store_true",
                    help="이동 대신 복사 (원본을 그대로 남김)")
    ap.add_argument("--dry-run", action="store_true", help="실제로 옮기지 않고 계획만 출력")
    ap.add_argument("--recursive", action="store_true", help="하위 폴더까지 탐색")
    ap.add_argument("--pattern", default="%Y/%m/{camera}", metavar="형식",
                    help="폴더 구조 (기본: %%Y/%%m/{camera} → 2023/04/SONY ILCE-7)")
    ap.add_argument("--no-mtime-fallback", action="store_true",
                    help="EXIF/파일명에서 날짜를 못 찾아도 파일 수정시각을 쓰지 않음")
    ap.add_argument("--unknown-dir", default="_날짜없음", metavar="폴더명",
                    help="날짜를 못 찾은 파일을 모을 폴더명")
    ap.add_argument("--trust-exif", action="store_true",
                    help="1990년 이전·미래 날짜도 검증 없이 그대로 사용")
    ap.add_argument("--undo", action="store_true",
                    help="가장 최근 정리 작업을 취소하고 파일을 원래 자리로 되돌림")
    args = ap.parse_args()

    src: Path = (args.input_opt or args.input_pos or Path.cwd()).resolve()
    if not src.is_dir():
        print(f"오류: 입력 폴더가 없습니다 — {src}", file=sys.stderr)
        return 1
    dest: Path = (args.output or src).resolve()

    if args.undo:
        print(f"되돌리기 대상 폴더: {dest}\n", flush=True)
        return run_undo(dest, args.dry_run)

    print(f"입력(-i): {src}\n출력(-o): {dest}\n", flush=True)

    stats = {"이동": 0, "중복건너뜀": 0, "날짜없음": 0, "실패": 0}
    journal = None                  # 실제로 옮길 일이 생기면 그때 만든다

    for path in iter_images(src, args.recursive):
        # 이미 정리된 결과를 다시 훑지 않도록
        if dest in path.parents and dest != src:
            continue

        try:
            dt, origin, camera = resolve_meta(path, allow_mtime=not args.no_mtime_fallback,
                                              trust_all=args.trust_exif)
        except OSError as e:
            print(f"[실패] {path.name}: {e}", file=sys.stderr)
            stats["실패"] += 1
            continue

        if dt is None:
            subdir = dest / args.unknown_dir / (camera or UNKNOWN_CAMERA)
            origin = "없음"
            stats["날짜없음"] += 1
        else:
            # strftime 으로 날짜를 채운 뒤 {camera} 를 끼워 넣는다
            subdir = dest / dt.strftime(args.pattern).replace(
                "{camera}", camera or UNKNOWN_CAMERA)

        target = subdir / path.name
        if target.resolve() == path:
            continue

        try:
            final = unique_target(target, path)
        except (OSError, RuntimeError) as e:
            print(f"[실패] {path.name}: {e}", file=sys.stderr)
            stats["실패"] += 1
            continue

        if final is None:
            print(f"[중복] {path.name} → 같은 파일이 이미 있음, 건너뜀")
            stats["중복건너뜀"] += 1
            continue

        rel = final.relative_to(dest)
        verb = "복사" if args.copy else "이동"
        print(f"[{'예정' if args.dry_run else verb}] {path.name} → {rel}  ({origin})")

        if not args.dry_run:
            try:
                subdir.mkdir(parents=True, exist_ok=True)
                if args.copy:
                    shutil.copy2(path, final)
                else:
                    shutil.move(str(path), str(final))
            except OSError as e:
                print(f"[실패] {path.name}: {e}", file=sys.stderr)
                stats["실패"] += 1
                continue

            if journal is None:
                jdir = journal_dir(dest)
                jdir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                journal = (jdir / f"journal-{stamp}.jsonl").open("w", encoding="utf-8")
            journal.write(json.dumps(
                {"op": "copy" if args.copy else "move",
                 "src": str(path), "dst": str(final)}, ensure_ascii=False) + "\n")
            journal.flush()     # 중간에 죽어도 여기까지는 되돌릴 수 있게
        if dt is not None:
            stats["이동"] += 1

    if journal is not None:
        journal.close()

    print("\n--- 요약 ---")
    print(f"처리 {stats['이동']}건 / 중복 건너뜀 {stats['중복건너뜀']}건 "
          f"/ 날짜없음 {stats['날짜없음']}건 / 실패 {stats['실패']}건")
    if args.dry_run:
        print("(--dry-run 이므로 실제 파일은 변경되지 않았습니다)")
    elif journal is not None:
        print("되돌리려면: epc --undo" + (f" -o \"{dest}\"" if dest != Path.cwd() else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
