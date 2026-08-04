#!/usr/bin/env python3
"""인천광역시교육청 채용공고를 모아 jobs.json 으로 만든다.

원천은 두 게시판이다.
  · 채용공고(bbsId=1981)      — 목록에 모집상태·마감일·근무기간이 필드로 있고,
                                상세에 지역·학교급·인원·연락처가 표로 있다. 주 데이터.
  · 사전공개(bbsId=1774)      — 학교가 공고 전에 올리는 예고. 본문이 이미지/hwpx라
                                제목·학교·등록일만 쓸 수 있다. 예고 탭용.

상세 페이지는 한 건당 한 번만 받는다(cache.json). 다시 돌려도 새 글만 받는다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path

BASE = "https://www.ice.go.kr/ice/na/ntt"
LIST_URL = BASE + "/selectNttList.do?mi={mi}&bbsId={bbs}&currPage={page}&listCo=100"
VIEW_URL = BASE + "/selectNttInfo.do?nttSn={sn}&mi={mi}&bbsId={bbs}"

BOARD_JOB = {"bbs": 1981, "mi": 10997}   # 채용공고
BOARD_PRE = {"bbs": 1774, "mi": 10994}   # 기간제교원 채용계획 사전공개

# HTTP 헤더는 latin-1 로만 보낼 수 있어 한글을 쓰지 않는다.
UA = "incheon-edujob/1.0 (teacher job listing mirror; 3 runs/day; contact via GitHub issues)"
DELAY = 1.0          # 요청 간 간격(초). 원본 서버를 배려한다.
KST = timezone(timedelta(hours=9))

HERE = Path(__file__).resolve().parent

# 가르치는 자리만 남긴다. 게시판의 '모집직종' 값과 정확히 맞춘 목록이다.
TEACHING_JOBS = [
    "기간제교사",
    "시간강사",
    "외국어전문강사",
    "예체능강사",
    "보결강사",
    "교과보충학습",
    "방과후강사",
    "신규교사(사립)",
]

# 과목 사전. 앞의 것이 먼저 매치되므로 긴 이름을 앞에 둔다.
# (별칭 → 대표 이름)
SUBJECT_ALIASES: list[tuple[str, str]] = [
    # 특수·비교과 (교과보다 먼저 봐야 '초등특수'가 '초등'으로 잘리지 않는다)
    ("유아특수", "유아특수"), ("초등특수", "초등특수"), ("중등특수", "중등특수"),
    ("특수교사", "특수"), ("특수교육", "특수"), ("특수", "특수"),
    ("전문상담", "전문상담"), ("진로진학상담", "진로진학상담"), ("진로진학", "진로진학상담"),
    ("진로와 직업", "진로와 직업"),
    ("사서교사", "사서"), ("사서", "사서"), ("보건교사", "보건"), ("보건", "보건"),
    ("영양교사", "영양"), ("영양", "영양"),
    ("유치원", "유치원"), ("유아", "유치원"),
    # 교과
    ("국어", "국어"), ("영어", "영어"), ("수학", "수학"),
    ("도덕", "도덕"), ("윤리", "윤리"),
    ("일반사회", "일반사회"), ("지리", "지리"), ("역사", "역사"), ("사회", "사회"),
    ("통합과학", "과학"), ("공통과학", "과학"),
    ("물리", "물리"), ("화학", "화학"), ("생명과학", "생명과학"), ("지구과학", "지구과학"),
    ("생물", "생명과학"), ("과학", "과학"),
    ("기술가정", "기술·가정"), ("기술·가정", "기술·가정"), ("기술ㆍ가정", "기술·가정"),
    ("가정", "기술·가정"), ("기술", "기술·가정"),
    ("정보컴퓨터", "정보"), ("정보", "정보"), ("컴퓨터", "정보"),
    ("체육", "체육"), ("음악", "음악"), ("미술", "미술"), ("한문", "한문"),
    ("중국어", "중국어"), ("일본어", "일본어"), ("프랑스어", "프랑스어"),
    ("독일어", "독일어"), ("스페인어", "스페인어"), ("러시아어", "러시아어"),
    ("아랍어", "아랍어"), ("베트남어", "베트남어"),
    ("환경", "환경"), ("연극", "연극"), ("무용", "무용"),
    ("초등", "초등"),
    # 전문교과(직업계고)
    ("보건간호", "보건간호"), ("간호", "보건간호"), ("조리", "조리"), ("제과제빵", "조리"),
    ("미용", "미용"), ("관광", "관광"), ("디자인", "디자인"), ("회계", "회계"),
    ("상업", "상업"), ("금융", "금융"), ("전기", "전기"), ("전자", "전자"),
    ("기계", "기계"), ("건축", "건축"), ("토목", "토목"), ("자동차", "자동차"),
    ("항공", "항공"), ("해양", "해양"), ("물류", "물류"), ("방송", "방송"),
    ("사진", "사진"), ("패션", "패션"), ("농업", "농업"), ("원예", "원예"),
]

# 교과가 없는 자리들. 초등 담임이 대표적이다. 교과를 못 찾았을 때만 이 이름을 붙인다.
# '과목 미상'으로 두면 정작 담임 자리를 찾는 사람이 거를 수 없어서 따로 둔다.
ROLE_PATTERNS: list[tuple[str, str]] = [
    (r"담임", "담임"),
    (r"교과\s*전담|전담\s*교사|전담교사|전담", "교과전담"),
    (r"협력\s*강사|학습\s*지원|두드림|기초\s*학력", "학습지원"),
    (r"시수\s*경감|수업\s*경감|수업\s*대체|결원\s*대체|책임교사", "수업대체"),
    (r"돌봄|늘봄", "돌봄·늘봄"),
]

# 과목으로 오인하기 쉬운 말. 이 단어 안에 들어 있으면 매치로 치지 않는다.
FALSE_FRIENDS = [
    "개인정보", "채용정보", "정보공개", "정보통신", "정보처리", "개인정보보호",
    "사회복무", "사회복지", "지역사회", "정보보안",
    "가정통신", "가정학습", "기술적", "환경미화", "환경정비", "학습환경",
    "영양사", "조리원", "조리실무", "조리종사", "급식",
]


# ── 저수준 ──────────────────────────────────────────────────────────────

def fetch(url: str, tries: int = 3) -> str:
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                raw = r.read()
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("euc-kr", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"가져오기 실패: {url} ({last})")


def strip_tags(s: str) -> str:
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def norm_date(s: str) -> str | None:
    """'2026.08.04.' / '2026/08/04' → '2026-08-04'"""
    m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", s or "")
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


# ── 목록 ────────────────────────────────────────────────────────────────

LIST_COLS_JOB = ["reg", "status", "school", "job", "title", "due", "work_from", "work_to"]


def parse_job_list(html: str) -> list[dict]:
    """채용공고 게시판 목록. 표가 여러 개라 헤더로 본문 표를 찾는다."""
    out = []
    for table in re.findall(r"<table.*?</table>", html, re.S):
        ths = [strip_tags(t) for t in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S)]
        if "모집상태" not in ths or "기관명" not in ths:
            continue
        for tr in re.findall(r"<tr>(.*?)</tr>", table, re.S):
            sn = re.search(r'data-id="(\d+)"', tr)
            if not sn:
                continue
            tds = [strip_tags(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(tds) < len(LIST_COLS_JOB):
                continue
            row = dict(zip(LIST_COLS_JOB, tds))
            row["title"] = re.sub(r"^N\s+", "", row["title"]).strip()
            row["sn"] = int(sn.group(1))
            out.append(row)
        break
    return out


def parse_pre_list(html: str) -> list[dict]:
    """사전공개 게시판 목록: 번호·제목·등록기관·등록일·조회수."""
    out = []
    for table in re.findall(r"<table.*?</table>", html, re.S):
        ths = [strip_tags(t) for t in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S)]
        if "등록기관" not in ths:
            continue
        for tr in re.findall(r"<tr>(.*?)</tr>", table, re.S):
            sn = re.search(r'data-id="(\d+)"', tr)
            if not sn:
                continue
            tds = [strip_tags(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(tds) < 4:
                continue
            out.append({
                "sn": int(sn.group(1)),
                "title": re.sub(r"^N\s+", "", tds[1]).strip(),
                "school": tds[2],
                "reg": tds[3],
            })
        break
    return out


def crawl_list(board: dict, parser, since: str, max_pages: int, label: str) -> list[dict]:
    """등록일이 since 이전으로 내려갈 때까지 목록을 훑는다."""
    rows: list[dict] = []
    covered = False        # since 까지 실제로 내려갔는지. 못 갔으면 삭제 판정을 하면 안 된다.
    for page in range(1, max_pages + 1):
        html = fetch(LIST_URL.format(page=page, **board))
        got = parser(html)
        if not got:
            break
        rows.extend(got)
        oldest = norm_date(got[-1]["reg"]) or "9999-99-99"
        print(f"  [{label}] {page}쪽 {len(got)}건 (…{oldest})", file=sys.stderr)
        if oldest < since:
            covered = True
            break
        time.sleep(DELAY)
    return rows, covered


def drop_deleted(store: dict, seen: set[str], since: str, covered: bool, label: str) -> None:
    """게시판에서 내려간 글을 캐시에서도 지운다.

    학교가 공고를 취소하면 원본에서 사라지는데, 캐시에만 남으면 마감일이 미래인 한
    계속 '모집 중'으로 보인다. 이번에 훑은 구간(since 이후) 안에 있으면서 목록에
    없던 글은 내려간 것으로 본다. 구간 끝까지 못 갔으면(covered=False) 판정하지 않는다.
    """
    if not covered:
        return
    gone = [sn for sn, r in store.items()
            if sn not in seen and (norm_date(r["reg"]) or "") >= since]
    for sn in gone:
        store.pop(sn)
    if gone:
        print(f"  [{label}] 게시판에서 내려간 {len(gone)}건 제외", file=sys.stderr)


# ── 상세 ────────────────────────────────────────────────────────────────

DETAIL_KEYS = {
    "기관구분": "level", "기관위치": "gu", "연락처": "tel",
    "모집시작일": "open", "모집인원": "count", "채용방법": "method",
    "제출서류": "docs", "모집상태": "status", "모집직종": "job", "기관명": "school",
}


def parse_detail(html: str) -> dict:
    d: dict = {}
    for th, td in re.findall(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", html, re.S):
        key = DETAIL_KEYS.get(strip_tags(th))
        if key:
            d[key] = strip_tags(td)
    body = re.search(r'<div class="bbsV_cont">(.*?)</div>\s*<!--', html, re.S)
    if not body:
        body = re.search(r'<div class="bbsV_cont">(.*?)</div>', html, re.S)
    # 본문 전체를 캐시에 담으면 파일이 수십 MB가 된다. 과목이 적힐 만한 줄만 남긴다.
    text = strip_tags(body.group(1))[:20000] if body else ""
    hints = [ln.strip() for ln in text.split("\n")[:200]
             if 0 < len(ln.strip()) <= 90
             and any(k in ln for k in ("과목", "교과", "모집분야", "담당교과"))]
    d["body"] = "\n".join(hints)[:1200]
    files = re.findall(r'class="fileName"[^>]*title="([^"]+?) 다운로드"', html)
    d["files"] = [f.strip() for f in files][:6]
    return d


# ── 과목 뽑기 ───────────────────────────────────────────────────────────

# 학교 이름에 교과명이 섞여 있는 경우가 많다(인평'자동차'고, 인천대중'예술'고).
# 과목을 찾기 전에 학교 이름을 걷어낸다.
SCHOOL_TOKEN = re.compile(
    r"[가-힣A-Za-z]{1,12}?(?:초등학교|중학교|고등학교|여자중학교|여자고등학교"
    r"|병설유치원|유치원|특수학교|학교|교육지원청|교육청)")


def clean_title(title: str, school: str = "") -> str:
    t = title
    if school:
        t = t.replace(school, " ")
    return SCHOOL_TOKEN.sub(" ", t)


def _match_subjects(text: str) -> list[str]:
    """찾은 자리는 지워 가며 훑는다.

    긴 이름을 앞에 둔 사전을 쓰고 매치한 구간을 마스킹하면, '일반사회'를 잡은 뒤
    같은 자리에서 '사회'가 또 잡히거나 '특수학급'의 가운데에서 '수학'이 잡히는 일이 없다.
    """
    buf = list(text)
    found: list[str] = []
    for alias, canon in SUBJECT_ALIASES:
        start = 0
        while True:
            idx = "".join(buf).find(alias, start)
            if idx < 0:
                break
            window = text[max(0, idx - 5): idx + len(alias) + 5]
            if any(bad in window for bad in FALSE_FRIENDS):
                start = idx + 1
                continue
            for i in range(idx, idx + len(alias)):
                buf[i] = "\x00"
            if canon not in found:
                found.append(canon)
            break
    return found


def extract_subjects(title: str, body: str, school: str = "") -> tuple[list[str], str]:
    """제목 → 본문 순으로 과목을 찾는다. (과목목록, 근거) 를 돌려준다.

    괄호 안이 가장 믿을 만하다. '(영어)', '(국어, 체육)' 같은 표기가 대부분이라
    먼저 보고, 없으면 제목 전체, 그래도 없으면 본문에서 과목이 적힌 줄만 본다.
    """
    clean = clean_title(title, school)

    for p in re.findall(r"[(（\[]([^)）\]]{1,90})[)）\]]", clean):
        hits = _match_subjects(p)
        if hits:
            return hits, "제목(괄호)"

    hits = _match_subjects(clean)
    if hits:
        return hits, "제목"

    # 본문은 오탐이 나기 쉬워 '과목'이 명시된 짧은 줄만 본다.
    for line in body.split("\n")[:150]:
        line = line.strip()
        if len(line) > 90 or not any(k in line for k in ("과목", "교과", "모집분야", "담당교과")):
            continue
        hits = _match_subjects(clean_title(line))
        if hits:
            return hits[:6], "본문"

    # 교과가 안 나오면 자리의 성격이라도 알려 준다. 교과를 찾았을 때는 여기까지 오지 않는다.
    roles = [name for pat, name in ROLE_PATTERNS if re.search(pat, clean)]
    if roles:
        return roles[:2], "역할"

    return [], ""


# ── 조립 ────────────────────────────────────────────────────────────────

def load_cache(path: Path) -> dict:
    """캐시는 {"detail": …, "row": …, "pre": …} 꼴이다.

    수집은 매번 최근 며칠만 훑기 때문에, 이번에 훑은 것만으로 jobs.json 을 만들면
    날마다 목록이 짧아진다. 그래서 목록 줄까지 캐시에 쌓아 두고 합쳐서 만든다.
    """
    if not path.exists():
        return {"detail": {}, "row": {}, "pre": {}}
    c = json.loads(path.read_text("utf-8"))
    if "detail" not in c:            # 첫 버전(상세만 담던 형태)에서 옮겨 온다
        c = {"detail": c, "row": {}, "pre": {}}
    c.setdefault("row", {})
    c.setdefault("pre", {})
    return c


def save_cache(path: Path, cache: dict) -> None:
    path.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")


def build(since: str, keep: str, max_pages: int, pre_pages: int, cache_path: Path) -> dict:
    cache = load_cache(cache_path)
    detail, rowstore = cache["detail"], cache["row"]

    print("채용공고 게시판 목록", file=sys.stderr)
    rows, covered = crawl_list(BOARD_JOB, parse_job_list, since, max_pages, "공고")

    fresh, seen = 0, set()
    for r in rows:
        if any(j in [x.strip() for x in r["job"].split(",")] for j in TEACHING_JOBS):
            seen.add(str(r["sn"]))
            if str(r["sn"]) not in rowstore:
                fresh += 1
            rowstore[str(r["sn"])] = r
    print(f"목록 {len(rows)}건 훑음 → 새 자리 {fresh}건 (쌓인 것 {len(rowstore)}건)", file=sys.stderr)
    drop_deleted(rowstore, seen, since, covered, "공고")

    # 오래된 것은 버린다. 안 그러면 파일이 해마다 불어난다.
    for sn in [k for k, v in rowstore.items() if (norm_date(v["reg"]) or "") < keep]:
        rowstore.pop(sn)
        detail.pop(sn, None)

    need = [r for r in rowstore.values() if str(r["sn"]) not in detail]
    print(f"상세 새로 받을 건수 {len(need)}", file=sys.stderr)
    for i, r in enumerate(need, 1):
        html = fetch(VIEW_URL.format(sn=r["sn"], **BOARD_JOB))
        detail[str(r["sn"])] = parse_detail(html)
        if i % 25 == 0 or i == len(need):
            print(f"  상세 {i}/{len(need)}", file=sys.stderr)
            save_cache(cache_path, cache)
        time.sleep(DELAY)
    save_cache(cache_path, cache)

    today = datetime.now(KST).date().isoformat()
    items = []
    for r in rowstore.values():
        d = detail.get(str(r["sn"]), {})
        subs, how = extract_subjects(r["title"], d.get("body", ""), r["school"])
        due = norm_date(r["due"])
        n = re.sub(r"[^\d]", "", d.get("count", "") or "")
        items.append({
            "id": r["sn"],
            "t": r["title"],
            "school": r["school"] or d.get("school", ""),
            "job": r["job"],
            "subject": subs,
            "subjectFrom": how,
            "gu": d.get("gu", ""),
            "level": d.get("level", ""),
            "n": int(n) if n.isdigit() else None,
            "reg": norm_date(r["reg"]),
            "open": norm_date(d.get("open", "")),
            "due": due,
            "workFrom": norm_date(r["work_from"]),
            "workTo": norm_date(r["work_to"]),
            "status": r["status"],
            "tel": d.get("tel", ""),
            "docs": d.get("docs", "")[:80],
            # 첨부파일 이름은 화면에 쓰지 않는다. 목록이 길어 파일만 키운다.
            "u": VIEW_URL.format(sn=r["sn"], **BOARD_JOB),
            "x": bool(due and due < today),   # 마감 지남
        })
    items.sort(key=lambda x: (x["due"] or "9999", x["reg"] or ""), reverse=False)

    print("사전공개 게시판 목록", file=sys.stderr)
    pre_rows, pre_covered = crawl_list(BOARD_PRE, parse_pre_list, since, pre_pages, "예고")
    pre_seen = set()
    for r in pre_rows:
        pre_seen.add(str(r["sn"]))
        cache["pre"][str(r["sn"])] = r
    drop_deleted(cache["pre"], pre_seen, since, pre_covered, "예고")
    for sn in [k for k, v in cache["pre"].items() if (norm_date(v["reg"]) or "") < keep]:
        cache["pre"].pop(sn)
    save_cache(cache_path, cache)

    pre = []
    for r in cache["pre"].values():
        subs, _ = extract_subjects(r["title"], "", r["school"])
        pre.append({
            "id": r["sn"], "t": r["title"], "school": r["school"],
            "reg": norm_date(r["reg"]), "subject": subs,
            "u": VIEW_URL.format(sn=r["sn"], **BOARD_PRE),
        })
    pre.sort(key=lambda x: x["reg"] or "", reverse=True)

    return {
        "built": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "today": today,
        "source": "인천광역시교육청 채용공고 · 기간제교원 채용계획 사전공개 게시판",
        "items": items,
        "pre": pre[:400],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180, help="이번에 새로 훑을 범위(일)")
    ap.add_argument("--keep", type=int, default=400, help="캐시에 남겨 둘 범위(일)")
    ap.add_argument("--max-pages", type=int, default=80)
    ap.add_argument("--pre-pages", type=int, default=8)
    ap.add_argument("--out", default=str(HERE / "jobs.json"))
    ap.add_argument("--cache", default=str(HERE / "cache.json"))
    a = ap.parse_args()

    today = datetime.now(KST).date()
    since = (today - timedelta(days=a.days)).isoformat()
    keep = (today - timedelta(days=a.keep)).isoformat()
    print(f"{since} 이후 등록분을 새로 훑음 (보관 {keep} 이후)", file=sys.stderr)
    data = build(since, keep, a.max_pages, a.pre_pages, Path(a.cache))

    Path(a.out).write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")

    live = [i for i in data["items"] if not i["x"]]
    nosub = [i for i in data["items"] if not i["subject"]]
    print(f"\n공고 {len(data['items'])}건 (모집 중 {len(live)}) · 예고 {len(data['pre'])}건", file=sys.stderr)
    print(f"과목 못 찾은 건 {len(nosub)} ({len(nosub) / max(1, len(data['items'])):.1%})", file=sys.stderr)
    print(f"→ {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
