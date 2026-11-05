# epc

사진의 촬영 메타데이터(EXIF)를 읽어 `연도/월/카메라` 폴더로 정리하는 CLI 도구입니다.

```
사진/                          사진/
├── DSC08934.ARW               └── 2026/
├── DSC08935.ARW        →          ├── 06/
└── IMG_0412.jpg                   │   └── SONY ILCE-7M4/
                                   │       ├── DSC08934.ARW
                                   │       └── DSC08935.ARW
                                   └── 07/
                                       └── Canon EOS R6/
                                           └── IMG_0412.jpg
```

## 특징

- **RAW 지원** — Pillow가 열지 못하는 소니 ARW 같은 RAW도 TIFF 구조를 직접 읽어 촬영일과 카메라를 찾습니다.
- **되돌리기** — 옮긴 내역을 기록해 두고 `--undo` 한 번으로 원래 자리로 복구합니다.
- **미리 보기** — `--dry-run`으로 실제로 옮기기 전에 계획만 확인할 수 있습니다.
- **중복 처리** — 같은 이름의 파일이 이미 있으면 내용(SHA-256)을 비교해, 같으면 건너뛰고 다르면 `_1`, `_2`를 붙입니다.
- **잘못된 날짜 거르기** — 시계가 리셋된 카메라의 `1980-01-01` 같은 날짜나 미래 날짜는 믿지 않습니다.

지원 형식: `.jpg` `.jpeg` `.png` `.webp` `.arw` `.nef` `.dng`

## 설치

Python 3.9 이상과 [Pillow](https://python-pillow.org/) 9.4 이상이 필요합니다.

```sh
git clone https://github.com/seoeungyul/epc.git
cd epc
pip install Pillow
```

## 사용법

```sh
python epc.py                           # 현재 폴더를 제자리에서 정리
python epc.py --dry-run                 # 계획만 확인
python epc.py -i ./사진 -o ./정리됨 --cp   # 원본은 두고 다른 폴더로 복사
python epc.py --pattern "%Y/%m"         # 카메라 구분 없이 연/월만
python epc.py --undo                    # 가장 최근 정리 작업 되돌리기
```

기본 동작은 **이동**입니다. 원본을 남기려면 `--cp`를 붙이세요.

### 옵션

| 옵션 | 설명 |
|---|---|
| `입력폴더`, `-i`, `--input` | 정리할 파일이 있는 폴더 (기본: 현재 폴더) |
| `-o`, `--output` | 정리해 넣을 폴더 (기본: 입력 폴더와 동일) |
| `--cp`, `--copy` | 이동 대신 복사 |
| `--dry-run` | 실제로 옮기지 않고 계획만 출력 |
| `--recursive` | 하위 폴더까지 탐색 (기본: 입력 폴더 바로 아래만) |
| `--pattern` | 폴더 구조 (기본: `%Y/%m/{camera}`) |
| `--no-mtime-fallback` | 날짜를 못 찾아도 파일 수정시각을 쓰지 않음 |
| `--unknown-dir` | 날짜를 못 찾은 파일을 모을 폴더명 (기본: `_날짜없음`) |
| `--trust-exif` | 1990년 이전·미래 날짜도 검증 없이 그대로 사용 |
| `--undo` | 가장 최근 정리 작업을 취소하고 파일을 원래 자리로 되돌림 |

### 폴더 구조 바꾸기

`--pattern`에는 [strftime 형식](https://docs.python.org/ko/3/library/datetime.html#strftime-and-strptime-format-codes)과 `{camera}`를 쓸 수 있습니다.

| 패턴 | 결과 |
|---|---|
| `%Y/%m/{camera}` (기본) | `2026/06/SONY ILCE-7M4` |
| `%Y/%m` | `2026/06` |
| `{camera}/%Y-%m-%d` | `SONY ILCE-7M4/2026-06-14` |

## 동작 방식

### 촬영일을 찾는 순서

1. EXIF (`DateTimeOriginal` → `DateTimeDigitized` → `DateTime`)
2. RAW 파일의 TIFF 태그 직접 파싱
3. 파일명에 들어 있는 날짜 (`IMG_20230415_101530.jpg`, `2023-04-15 10.15.30.png` 등)
4. 파일 수정시각 (`--no-mtime-fallback`으로 끌 수 있음)

1990년 이전이거나 내일 이후인 날짜는 경고를 남기고 다음 단계로 넘어갑니다. 끝까지 날짜를 찾지 못한 파일은 `_날짜없음/카메라명/`에 모입니다.

### 카메라 이름

EXIF의 제조사(Make)와 기종(Model)을 합쳐 폴더명으로 씁니다. 기종에 제조사가 이미 들어 있으면(`Canon EOS R6`) 중복해서 붙이지 않습니다. 카메라 정보가 없는 파일은 `_카메라없음` 폴더로 갑니다.

### 되돌리기

실제로 옮긴 내역은 출력 폴더의 `.epc/journal-날짜-시각.jsonl`에 한 줄씩 기록됩니다. `--undo`는 가장 최근 기록을 역순으로 되감습니다.

- 이동했던 파일은 원래 자리로 돌려놓고, 복사했던 파일은 사본만 지웁니다.
- 비게 된 폴더는 함께 정리합니다.
- 원래 자리에 다른 파일이 생겼거나 옮긴 파일이 이미 없으면 건드리지 않고 건너뜁니다.

`-o`로 출력 폴더를 따로 지정했다면 되돌릴 때도 같은 폴더를 알려줘야 합니다.

```sh
python epc.py --undo -o ./정리됨
python epc.py --undo --dry-run          # 무엇을 되돌릴지만 확인
```

## 실행 파일로 만들기

Python 없이 쓰려면 [PyInstaller](https://pyinstaller.org/)로 단일 실행 파일을 만들 수 있습니다.

```sh
pip install pyinstaller
pyinstaller --onefile --distpath output epc.py
```

`output/epc.exe`를 PATH에 있는 폴더에 두면 어디서든 `epc`로 실행할 수 있습니다.

## 라이선스

[MIT](LICENSE)
