#!/usr/bin/env python3
"""coach local — 쓰는 동안 고칠 곳을 제안하는 글쓰기 코치 (공문서·편지·논문·사설·메일 …). 로컬 LLM, 외부 의존성 없음(stdlib).

  python3 app.py                                    # http://localhost:8798
  LLM_API=openai LLM_BASE_URL=http://gpu:8000/v1 LLM_MODEL=Qwen3-32B python3 app.py
  python3 app.py --cli gongmun 초안.txt              # 문단별 제안을 터미널에

페르소나(글 종류 코치) = JSON 하나: name·audience·tone·rules·avoid·example. 기본은 personas.json, 사용자 것은 사용자 폴더 personas/.
제안은 LLM 이 돌려준 원문 조각이 그 문단에 글자 그대로 있을 때만 남긴다(자리 고정).
"""
import base64
import datetime
import io
import json
import os
import re
import secrets
import select
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.abspath(__file__))
WS = os.environ.get("WORKSPACE") or os.path.join(ROOT, "_workspace")  # 포털이 AGENT_DATA/<도구> 로 모아 줌
LLM_API = os.environ.get("LLM_API", "ollama")            # ollama | openai
LLM_BASE = os.environ.get("LLM_BASE_URL", "http://localhost:8000/v1" if LLM_API == "openai" else "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("LLM_MODEL", "qwen3:8b")
LLM_KEY = os.environ.get("LLM_API_KEY", "")
NUM_CTX = int(os.environ.get("NUM_CTX", "32768"))   # 포털은 모든 도구에 같은 값을 넘김 — 도구마다 다르면 Ollama 가 모델을 다시 올림
PORT = int(os.environ.get("PORT", "8798"))
KORDOC_CLI = os.environ.get("KORDOC_CLI") or os.path.join(os.path.dirname(ROOT), "kordoc-local", "node_modules", "kordoc", "dist", "cli.js")
CATS = {"맞춤법": "맞춤법·띄어쓰기", "문체": "문체·어조", "형식": "형식·관례", "명료": "명료성·간결", "논리": "논리·구조", "사실": "사실 확인 필요"}
DOC_RE = r"\d{8}-\d{6}-[0-9a-f]{4}"
PERSONA_RE = r"u-[0-9a-f]{6}"
PLACEHOLDER = re.compile(r"X{2}|x{2}|○○|20\dX|\(문서번호\)|\(날짜\)")


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def write(p, s):
    with open(p, "w", encoding="utf-8") as f:
        f.write(s)


BUILTIN = json.loads(read(os.path.join(ROOT, "personas.json")))
for _p in BUILTIN:
    _p["builtin"] = True


# ── LLM (JSON 한 번에) ─────────────────────────────────────────────────────
class Cancelled(Exception):
    pass


def _json(out):
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()
    m = re.search(r"\{.*\}", out, flags=re.S)
    if not m:
        raise ValueError(f"JSON 아님: {out[:200]}")
    return json.loads(m.group(0))


def llm(system, user, model=MODEL, stop=lambda: False):
    """JSON 응답 하나. stop() 이 참이 되면 LLM 연결을 끊어 생성을 멈춘다(오래된 요청 취소)."""
    if LLM_API == "openai":
        body = {"model": model, "temperature": 0.2, "stream": True, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        hdr = {"Content-Type": "application/json", **({"Authorization": f"Bearer {LLM_KEY}"} if LLM_KEY else {})}
        url, pick = LLM_BASE + "/chat/completions", lambda j: (j.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
    else:
        body = {"model": model, "stream": True, "think": False, "format": "json", "keep_alive": "30m",
                "options": {"temperature": 0.2, "num_ctx": NUM_CTX},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        hdr = {"Content-Type": "application/json"}
        url, pick = LLM_BASE + "/api/chat", lambda j: j.get("message", {}).get("content", "")
    for attempt in (0, 1):
        buf = []
        try:
            with urllib.request.urlopen(urllib.request.Request(url, json.dumps(body).encode(), hdr), timeout=600) as r:
                for line in r:
                    if stop():
                        raise Cancelled()
                    line = line.decode().strip()
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if not line or line == "[DONE]":
                        continue
                    j = json.loads(line)
                    if "error" in j:
                        raise RuntimeError(j["error"])
                    buf.append(pick(j))
                    if j.get("done"):
                        break
            return _json("".join(buf))
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            if attempt == 0 and LLM_API != "openai" and "think" in msg:
                body.pop("think")
                continue
            if attempt == 0 and LLM_API == "openai" and "response_format" in msg:
                body.pop("response_format")
                continue
            raise RuntimeError(f"LLM HTTP {e.code}: {msg[:300]}")
        except (ValueError, json.JSONDecodeError):
            if attempt == 0:  # 깨진 JSON — 한 번만 다시
                continue
            raise
    raise RuntimeError("LLM 응답 없음")


def models():
    if LLM_API == "openai":
        req = urllib.request.Request(LLM_BASE + "/models", headers={"Authorization": f"Bearer {LLM_KEY}"} if LLM_KEY else {})
        with urllib.request.urlopen(req, timeout=10) as r:
            return [m["id"] for m in json.load(r)["data"]]
    with urllib.request.urlopen(LLM_BASE + "/api/tags", timeout=10) as r:
        return [m["name"] for m in json.load(r)["models"]]


# ── 페르소나 ────────────────────────────────────────────────────────────────
def _lines(v, n=20, w=200):
    v = v.splitlines() if isinstance(v, str) else (v or [])
    return [str(x).strip()[:w] for x in v if str(x).strip()][:n]


def clean_persona(p):
    """화면·JSON 편집에서 온 페르소나를 정해진 모양으로. 이름 없으면 ValueError."""
    if not isinstance(p, dict):
        raise ValueError("페르소나는 JSON 객체여야 합니다")
    name = str(p.get("name") or "").strip()[:30]
    if not name:
        raise ValueError("페르소나 이름이 필요합니다")
    return {"id": str(p.get("id") or ""), "name": name, "audience": str(p.get("audience") or "").strip()[:200],
            "tone": str(p.get("tone") or "").strip()[:300], "rules": _lines(p.get("rules")), "avoid": _lines(p.get("avoid")),
            "example": str(p.get("example") or "").strip()[:2000]}


def persona_text(p):
    p = clean_persona(p)
    s = f"{p['name']}"
    if p["audience"]: s += f" / 독자: {p['audience']}"
    if p["tone"]: s += f"\n어조: {p['tone']}"
    if p["rules"]: s += "\n관례: " + "; ".join(p["rules"])
    if p["avoid"]: s += "\n피할 것: " + "; ".join(p["avoid"])
    if p["example"]: s += f"\n예시:\n{p['example'][:600]}"
    return s


def personas(ws):
    d, out = os.path.join(ws, "personas"), [dict(p) for p in BUILTIN]
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            try:
                out.append(json.loads(read(os.path.join(d, fn))))
            except Exception:
                pass
    return out


def save_persona(p, ws):
    p = clean_persona(p)
    if not re.fullmatch(PERSONA_RE, p["id"]):
        p["id"] = "u-" + secrets.token_hex(3)
    d = os.path.join(ws, "personas")
    os.makedirs(d, exist_ok=True)
    p["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    write(os.path.join(d, p["id"] + ".json"), json.dumps(p, ensure_ascii=False, indent=1))
    return p


def _inside(base, name):
    p = os.path.join(base, name)
    if not os.path.realpath(p).startswith(os.path.realpath(base) + os.sep):
        raise FileNotFoundError(name)
    return p


def delete_persona(pid, ws):
    if not re.fullmatch(PERSONA_RE, str(pid)):
        raise FileNotFoundError(pid)
    os.remove(_inside(os.path.join(ws, "personas"), pid + ".json"))


# ── 문단 검사 ───────────────────────────────────────────────────────────────
CHECK_SYS = """너는 한국어 글쓰기 코치다. [글 종류]의 관례와 [목적]에 맞게, 사용자가 보낸 문단에서 고칠 곳만 찾는다.
[글 종류] {persona}
[목적] {purpose}

출력 JSON: {{"s":[{{"p":"문단번호","o":"원문 일부","n":"고친 말","r":"이유","c":"분류"}}]}}
- 맞춤법·띄어쓰기는 다른 검사가 따로 하니 내지 않는다.
- o: 그 문단의 글자를 그대로 복사한 연속 구간(띄어쓰기·문장부호까지 같게). 고칠 곳만 담은 가장 짧은 구간(보통 한두 어절).
  문장을 통째로 바꾸는 제안은 문체·논리에서 꼭 필요할 때만, 문단당 1개.
- n: o 자리에 그대로 넣을 말. 뜻·숫자·고유명사는 바꾸지 않는다. 글에 없는 내용(관련 근거·날짜·문서번호 등)을 지어 넣거나 ○○·XX 자리표시를 쓰지 않는다.
- 글이 [글 종류]와 많이 달라 보이면(예: 공문서인데 메신저 말투) 문장 구조를 갈아엎지 말고 꼭 필요한 어휘·표기만 짚는다.
- r: 이유, 20자 이내.
- c: 문체(어조·높임·종결) | 형식(이 글 종류의 관례·표기) | 명료(모호·중복·장황) | 논리(연결·순서·근거) | 사실
- 사실: 숫자·날짜·이름·인용처럼 확인할 수 있는 구체적 내용이 틀려 보이거나 서로 맞지 않을 때만(없는 날짜, 요일 불일치, 합계·인원 불일치 등). n 은 맞는 값(모르면 o 그대로), r 에 확인할 점. 막연한 의심은 내지 않는다.
- 문단당 최대 4개, 확실한 것만. 없으면 {{"s":[]}}."""

SPELL_SYS = """한국어 맞춤법·띄어쓰기 교정기. 문단마다 틀린 어절만 찾는다. 어조·어휘·문장 구조는 건드리지 않는다.
출력 JSON: {{"s":[{{"p":"문단번호","o":"틀린 어절(문단 그대로)","n":"고친 어절","r":"15자 이내 이유"}}]}}
- o 는 한 어절(띄어쓰기 오류면 두 어절)만. 문장 통째 금지. 틀린 곳이 없으면 {{"s":[]}}.
- 글 종류: {kind}. 공문서·논문·보고서면 합성어 띄어쓰기 원칙도 적용(고속카메라→고속 카메라).
자주 틀리는 예: 시일내에→시일 내에 / 할때→할 때 / 되요→돼요 / 몇일→며칠 / 않되요→안 돼요 / 어의없다→어이없다 / 학생으로써(자격)→학생으로서 / 대화로서(수단)→대화로써"""

FULL_OK = ("문체", "논리")   # 문장을 크게 바꾸는 제안은 이 분류에서만


def _sentence_len(text, at, end):
    """o 가 걸친 문장(들)의 길이 — 문장 끝(. ? ! 줄바꿈) 기준."""
    a = max(text.rfind(ch, 0, at) for ch in ".?!\n") + 1
    ends = [i for i in (text.find(ch, end) for ch in ".?!\n") if i >= 0]
    return (min(ends) + 1 if ends else len(text)) - a


def _split(o, n):
    """글자 단위로 비교해 바뀐 곳을 어절 경계까지 넓힌 조각들 → [(o 안 위치, 원문 조각, 고친 조각)]. 통째로 바뀌었으면 []."""
    from difflib import SequenceMatcher
    sm, ps = SequenceMatcher(None, o, n, autojunk=False), []
    if sm.ratio() < 0.5:  # 거의 다 바뀜 = 다시 쓰기 → 나누지 않음
        return []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        a, b = i1, i2
        while a > 0 and not o[a - 1].isspace():
            a -= 1
        while b < len(o) and not o[b].isspace():
            b += 1
        if ps and a <= ps[-1][1]:  # 같은 어절(또는 맞닿은 곳)이면 합침
            ps[-1][1], ps[-1][3] = b, j2 + (b - i2)
        else:
            ps.append([a, b, j1 - (i1 - a), j2 + (b - i2)])
    if not ps or (len(ps) == 1 and ps[0][0] == 0 and ps[0][1] == len(o)):
        return []
    return [(a, o[a:b], n[x:y].strip()) for a, b, x, y in ps]


def _accept(raw, by, cat_default, dropped, minimal=False):
    """LLM 이 낸 제안 하나 → 자리 확인·정리된 제안 목록(0개 이상)."""
    if not isinstance(raw, dict):
        dropped.append("형식")
        return []
    pid, o, n = str(raw.get("p", "")).strip("[] "), str(raw.get("o") or ""), str(raw.get("n") or "")
    if pid not in by and "p" + pid in by:  # "0" 처럼 p 를 빼고 답하는 경우
        pid = "p" + pid
    if pid not in by and len(by) == 1:
        pid = next(iter(by))
    c = cat_default or next((k for k in CATS if k in str(raw.get("c", ""))), "명료")
    r, text = str(raw.get("r") or "")[:80], by.get(pid)
    if not cat_default and c == "맞춤법":  # 문체 검사는 맞춤법을 내지 않는다(맞춤법 검사 몫)
        c = None
    if not c or not text or not o or o not in text or (o == n and c != "사실") or PLACEHOLDER.search(n) or len(n) > 2 * len(o) + 20:
        dropped.append(o)
        return []
    at = text.index(o)
    big = len(o) >= 15 and len(o) > 0.6 * _sentence_len(text, at, at + len(o)) and c != "사실"
    if big and minimal:  # 글 종류가 어긋나 보일 때: 문장 갈아엎기는 내지 않고 최소 조각만
        parts = [x for x in _split(o, n) if x[1] != x[2]]
        if not parts or len(parts) > 3:
            dropped.append(o)
            return []
        return [{"p": pid, "o": po, "n": pn, "r": r, "c": c, "at": at + off} for off, po, pn in parts]
    if (big and c not in FULL_OK) or (cat_default == "맞춤법" and len(o.split()) > 2):  # 크게 바꾸는 제안 → 최소 조각으로
        parts = _split(o, n)
        if not parts or len(parts) > 3:
            dropped.append(o)
            return []
        return [{"p": pid, "o": po, "n": pn, "r": r, "c": c, "at": at + off} for off, po, pn in parts if po != pn]
    if big and not r:
        dropped.append(o)
        return []
    return [{"p": pid, "o": o, "n": n, "r": r, "c": c, "at": at}]


def _dedupe(items):
    """같은 문단에서 겹치면 맞춤법, 그다음 짧은 쪽을 남긴다. 단, 문체·논리의 문장 다시 쓰기는 겹쳐도 카드로 남긴다(밑줄은 짧은 쪽)."""
    out, seen = [], set()
    for s in sorted(items, key=lambda s: (s["c"] != "맞춤법", len(s["o"]))):   # 맞춤법(어절 단위) 먼저, 그다음 짧은 것
        if (s["p"], s["o"], s["n"]) in seen:
            continue
        clash = any(t["p"] == s["p"] and s["at"] < t["at"] + len(t["o"]) and t["at"] < s["at"] + len(s["o"]) for t in out)
        if clash and not (s["c"] in FULL_OK and len(s["o"]) >= 15):
            continue
        seen.add((s["p"], s["o"], s["n"]))
        out.append(s)
    return sorted(out, key=lambda s: (s["p"], s["at"]))


def check(units, persona, purpose="", model=MODEL, stop=lambda: False, passes=("spell", "style"), minimal=False):
    """units = [{"id": "p3", "text": "..."}] → 자리가 확인된 제안 목록 + 버린 개수.
    맞춤법 검사(작은 프롬프트, 어절 단위)와 문체·형식 검사를 따로 묻는다. 화면은 맞춤법 → 문체 순으로 따로 불러 먼저 온 것부터 보이고,
    둘 다 부르면(CLI·기본) 동시에 돌려 합친다."""
    units = [{"id": str(u["id"])[:20], "text": str(u["text"])[:3000]} for u in units if str(u.get("text", "")).strip()][:20]
    if not units:
        return {"s": [], "dropped": 0}
    p = clean_persona(persona)
    user = "\n".join(f"[{u['id']}] {u['text']}" for u in units)
    jobs = {"style": (CHECK_SYS.format(persona=persona_text(p), purpose=(purpose or "").strip()[:200] or "(없음)"), None),
            "spell": (SPELL_SYS.format(kind=p["name"]), "맞춤법")}
    jobs = {k: v for k, v in jobs.items() if k in passes}
    res = {}

    def run(k):
        try:
            res[k] = llm(jobs[k][0], user, model, stop).get("s") or []
        except Exception as e:  # noqa: BLE001 — 한쪽이 실패해도 다른 쪽 결과는 살린다
            res[k] = e
    ts = [threading.Thread(target=run, args=(k,)) for k in jobs]
    [t.start() for t in ts]
    [t.join() for t in ts]
    errs = [v for v in res.values() if isinstance(v, Exception)]
    if any(isinstance(e, Cancelled) for e in errs):
        raise Cancelled()
    if len(errs) == len(jobs):
        raise errs[0]
    by, dropped, items = {u["id"]: u["text"] for u in units}, [], []
    for k, got in res.items():
        if isinstance(got, list):
            for raw in got:
                items += _accept(raw, by, jobs[k][1], dropped, minimal)
    out = _dedupe(items)
    return {"s": out, "dropped": len(dropped) + len(items) - len(out), **({"partial": str(errs[0])[:120]} if errs else {})}


REVIEW_SYS = """너는 한국어 글쓰기 코치다. 글 전체를 [글 종류]의 관례와 [목적]에 비추어 검토한다.
[글 종류] {persona}
[목적] {purpose}

문장 단위 맞춤법은 빼고, 구조·흐름·빠진 요소·목적 부합을 본다(예: 공문의 '끝.'·붙임 누락, 메일의 기한 누락, 논문의 근거 없는 주장).
출력 JSON: {{"kind":"이 글에 가장 맞는 글 종류 — {kinds} 중 하나","summary":"총평 2문장 이내","good":["잘된 점(최대 3)"],"issues":[{{"q":"관련 원문 일부(그대로 복사, 글 전체 문제면 빈 문자열)","t":"문제","f":"고칠 방향 또는 예시 문장","c":"형식|논리|명료|문체|사실"}}]}}
issues 는 중요한 순서로 최대 7개."""


def review(text, persona, purpose="", model=MODEL):
    text = (text or "").strip()[:12000]
    if not text:
        raise ValueError("검토할 글이 없습니다")
    kinds = [p["name"] for p in BUILTIN]
    j = llm(REVIEW_SYS.format(persona=persona_text(persona), purpose=(purpose or "").strip()[:200] or "(없음)", kinds="·".join(kinds)), text, model)
    issues = []
    for i in j.get("issues") or []:
        if isinstance(i, dict) and i.get("t"):
            q = str(i.get("q") or "")
            issues.append({"q": q if q and q in text else "", "t": str(i["t"])[:200], "f": str(i.get("f") or "")[:400],
                           "c": next((k for k in CATS if k in str(i.get("c", ""))), "논리")})
    return {"kind": next((k for k in kinds if k == str(j.get("kind") or "").strip()), ""), "summary": str(j.get("summary") or "")[:400], "good": [str(x)[:200] for x in (j.get("good") or [])][:3], "issues": issues[:7]}


# ── 문서 저장·기록 ──────────────────────────────────────────────────────────
def doc_path(did, ws):
    if not re.fullmatch(DOC_RE, str(did)):
        raise FileNotFoundError(did)
    return _inside(os.path.join(ws, "docs"), did + ".json")


def save_doc(req, ws):
    """같은 id 면 덮어쓰고, 내용이 바뀌었고 snapshot 이거나 마지막 판이 10분 넘었으면 판(version)을 남긴다(최대 30)."""
    now = datetime.datetime.now()
    did = str(req.get("id") or "")
    d = os.path.join(ws, "docs")
    os.makedirs(d, exist_ok=True)
    try:
        doc = json.loads(read(doc_path(did, ws)))
    except (FileNotFoundError, ValueError):
        did = f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        doc = {"id": did, "created": now.isoformat(timespec="seconds"), "versions": []}
    text = str(req.get("text") or "")[:200000]
    vs = doc["versions"]
    last = datetime.datetime.fromisoformat(vs[-1]["ts"]) if vs else None
    if text.strip() and (not vs or vs[-1]["text"] != text) and (req.get("snapshot") or not last or (now - last).total_seconds() > 600):
        vs.append({"ts": now.isoformat(timespec="seconds"), "text": text})
        del vs[:-30]
    doc.update({"title": str(req.get("title") or "").strip()[:100] or (text.strip().splitlines() or ["제목 없음"])[0][:40],
                "text": text, "persona": str(req.get("persona") or "")[:20], "purpose": str(req.get("purpose") or "")[:200],
                "updated": now.isoformat(timespec="seconds")})
    write(doc_path(did, ws), json.dumps(doc, ensure_ascii=False, indent=1))
    return {k: doc[k] for k in ("id", "title", "updated")} | {"versions": len(vs)}


def list_docs(ws):
    d, out = os.path.join(ws, "docs"), []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d), reverse=True)[:200]:
        try:
            j = json.loads(read(os.path.join(d, fn)))
            out.append({k: j.get(k) for k in ("id", "title", "persona", "updated")} | {"chars": len(j.get("text", "")), "versions": len(j.get("versions", []))})
        except Exception:
            pass
    return sorted(out, key=lambda x: x["updated"] or "", reverse=True)


# ── 가져오기·내보내기 ───────────────────────────────────────────────────────
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_text(data):
    import xml.etree.ElementTree as ET
    root = ET.fromstring(zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml"))
    return "\n".join("".join(t.text or "" for t in p.iter(W + "t")) for p in root.iter(W + "p"))


def kordoc_ok():
    return os.path.exists(KORDOC_CLI)


def import_file(name, data, ws):
    ext = os.path.splitext(name)[1].lower()
    if ext in (".txt", ".md", ""):
        for enc in ("utf-8-sig", "cp949"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                pass
        return data.decode("utf-8", "replace")
    if ext == ".docx" and not kordoc_ok():
        return docx_text(data)
    if ext not in (".docx", ".hwp", ".hwpx", ".pdf", ".hml"):
        raise ValueError(f"지원하지 않는 형식: {ext} (txt·md·docx" + (", hwp·hwpx·pdf" if kordoc_ok() else "") + ")")
    if not kordoc_ok():
        raise ValueError("hwp·hwpx·pdf 는 kordoc-local 이 같은 폴더에 설치돼 있어야 읽을 수 있습니다")
    d = os.path.join(ws, "_upload", secrets.token_hex(4))
    os.makedirs(d)
    try:
        src, out = os.path.join(d, "in" + ext), os.path.join(d, "out.md")
        with open(src, "wb") as f:
            f.write(data)
        r = subprocess.run(["node", KORDOC_CLI, "--silent", src, "-o", out], capture_output=True, text=True, timeout=180)
        if not os.path.exists(out):
            raise RuntimeError("kordoc 변환 실패: " + (r.stdout + r.stderr).strip()[-300:])
        return read(out)
    finally:
        for fn in os.listdir(d):
            os.remove(os.path.join(d, fn))
        os.rmdir(d)


def docx_bytes(title, text):
    """python-docx 없이 최소 구성 .docx (맑은 고딕, 문단 = 줄)."""
    def para(s, bold=False, size=22):
        rpr = f'<w:rPr><w:rFonts w:ascii="Malgun Gothic" w:hAnsi="Malgun Gothic" w:eastAsia="맑은 고딕"/>{"<w:b/>" if bold else ""}<w:sz w:val="{size}"/></w:rPr>'
        return f'<w:p><w:r>{rpr}<w:t xml:space="preserve">{escape(s)}</w:t></w:r></w:p>'
    body = (para(title, True, 32) if title else "") + "".join(para(line) for line in text.split("\n"))
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                               '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
                               '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                             f'<w:body>{body}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr></w:body></w:document>',
    }
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, v)
    return b.getvalue()


# ── HTTP ───────────────────────────────────────────────────────────────────
HTML = read(os.path.join(ROOT, "ui.html")) if os.path.exists(os.path.join(ROOT, "ui.html")) else "ui.html 없음"
CANCEL, CANCEL_LOCK = {}, threading.Lock()   # (사용자, 요청 id) → 취소 시각. 포털 프록시는 연결 끊김을 전하지 않으므로 따로 받는다


def user_ws(h):
    """포털이 넘긴 사용자별 작업 폴더. owner(서버 주인)·헤더 없음(단독 실행) = 예전 WORKSPACE 그대로"""
    u = (h.headers.get("X-Agent-User") or "owner").strip()
    if u == "owner":
        return WS
    d = os.path.join(WS, "u", re.sub(r"[^A-Za-z0-9_.-]", "_", u)[:64] or "_")
    os.makedirs(d, exist_ok=True)
    return d


def _gone(sock):
    """브라우저가 요청을 버렸는지(단독 실행 때) — 읽을 게 있는데 0바이트면 끊긴 것."""
    try:
        r, _, _ = select.select([sock], [], [], 0)
        return bool(r) and sock.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


# ── 저작권 표기 (LICENSE·NOTICE 참고) ─────────────────────────────────────
_SIG = __import__("base64").b64decode("wqkgMjAyNiBnZ2dnODY1NyDCtyBkb25nanVraW0uZGV2QGdtYWlsLmNvbQ==").decode()
_SIG_A = __import__("base64").b64decode("Z2dnZzg2NTcgPGRvbmdqdWtpbS5kZXZAZ21haWwuY29tPg==").decode()


def signed(html):
    """화면에 저작권 표기를 붙인다. ui.html 에서 지워져도 서버가 내보낼 때 다시 붙는다."""
    name, mail = _SIG.split(" · ")
    if 'name="author"' not in html:
        meta = f'<meta name="author" content="{name[7:]} <{mail}>">'
        html = html.replace("<head>", "<head>" + meta, 1) if "<head>" in html else meta + html
    if "data-sig" not in html:
        tag = (f'<!-- {_SIG} --><div data-sig title="{mail}" style="text-align:center;font-size:11px;color:#9aa0a6;'
               f'opacity:.55;margin:28px 0 8px">{name}</div>')
        html = html.replace("</body>", tag + "</body>", 1) if "</body>" in html else html + tag
    return html


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        if "/api/review" in (a[0] if a else ""):
            super().log_message(fmt, *a)

    def _send(self, body, ctype="application/json", code=200, name=None):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("X-Author", _SIG_A)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        if name:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        ws = user_ws(self)
        try:
            if self.path == "/api/health":
                return self._send({"ok": True, "model": MODEL})
            if self.path == "/api/models":
                return self._send(models())
            if self.path == "/api/personas":
                return self._send(personas(ws))
            if self.path == "/api/docs":
                return self._send(list_docs(ws))
            if self.path == "/api/import_ok":
                return self._send({"kordoc": kordoc_ok()})
            m = re.fullmatch(rf"/api/docs/({DOC_RE})", self.path)
            if m:
                return self._send(read(doc_path(m.group(1), ws)).encode())
            self._send(signed(HTML.replace("%MODEL%", json.dumps(MODEL)).replace("%CATS%", json.dumps(CATS, ensure_ascii=False))).encode(),
                       "text/html; charset=utf-8")
        except FileNotFoundError:
            self._send({"error": "없음"}, code=404)
        except Exception as e:
            self._send({"error": f"{type(e).__name__}: {e}"}, code=500)

    def do_POST(self):
        ws, who = user_ws(self), self.headers.get("X-Agent-User") or "owner"
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path == "/api/check":
                rid, t0 = (who, str(req.get("rid") or "")), time.time()
                stop = lambda: rid in CANCEL or _gone(self.connection)
                try:
                    ps = (req["pass"],) if req.get("pass") in ("spell", "style") else ("spell", "style")
                    r = check(req.get("units") or [], req.get("persona") or BUILTIN[0], req.get("purpose") or "", req.get("model") or MODEL, stop, ps, bool(req.get("minimal")))
                except Cancelled:
                    return self._send({"cancelled": True}, code=499)
                finally:
                    with CANCEL_LOCK:
                        CANCEL.pop(rid, None)
                return self._send(r | {"ms": int((time.time() - t0) * 1000)})
            if self.path == "/api/cancel":
                with CANCEL_LOCK:
                    now = time.time()
                    for k in [k for k, v in CANCEL.items() if now - v > 300]:
                        del CANCEL[k]
                    for r in (req.get("rids") or [])[:50]:
                        CANCEL[(who, str(r))] = now
                return self._send({"ok": True})
            if self.path == "/api/review":
                t0 = time.time()
                return self._send(review(req.get("text"), req.get("persona") or BUILTIN[0], req.get("purpose") or "", req.get("model") or MODEL)
                                  | {"ms": int((time.time() - t0) * 1000)})
            if self.path == "/api/personas":
                return self._send(save_persona(req, ws))
            if self.path == "/api/personas/delete":
                delete_persona(req.get("id"), ws)
                return self._send({"ok": True})
            if self.path == "/api/docs":
                return self._send(save_doc(req, ws))
            if self.path == "/api/docs/delete":
                os.remove(doc_path(req.get("id"), ws))
                return self._send({"ok": True})
            if self.path == "/api/import":
                name = os.path.basename(str(req.get("file_name") or "upload.txt"))
                return self._send({"text": import_file(name, base64.b64decode(req.get("file_b64") or ""), ws), "name": name})
            if self.path == "/api/export":
                title = str(req.get("title") or "").strip() or "글"
                fn = re.sub(r'[\\/:*?"<>|\n]', "_", title)[:60] + ".docx"
                return self._send(docx_bytes(title, str(req.get("text") or "")),
                                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document", name=fn)
            return self._send({"error": "not found"}, code=404)
        except FileNotFoundError:
            self._send({"error": "없음"}, code=404)
        except ValueError as e:
            self._send({"error": str(e)}, code=400)
        except Exception as e:
            self._send({"error": f"{type(e).__name__}: {e}"}, code=500)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        pid = sys.argv[2] if len(sys.argv) > 2 else "gongmun"
        p = next((x for x in BUILTIN if x["id"] == pid), None) or sys.exit(f"페르소나 없음: {pid} (있는 것: {', '.join(x['id'] for x in BUILTIN)})")
        text = read(sys.argv[3]) if len(sys.argv) > 3 else sys.stdin.read()
        paras = [t for t in text.split("\n") if t.strip()]
        r = check([{"id": f"p{i}", "text": t} for i, t in enumerate(paras)], p)
        for s in r["s"]:
            print(f"[{CATS[s['c']]}] {s['o']} → {s['n']}   ({s['r']})")
        print(f"— 제안 {len(r['s'])}개, 자리 못 찾아 버린 것 {r['dropped']}개", file=sys.stderr)
        sys.exit(0)
    print(f"coach local → http://localhost:{PORT}  (model={MODEL}, llm={LLM_API} {LLM_BASE}, num_ctx={NUM_CTX}, personas={len(BUILTIN)})  {_SIG}")
    ThreadingHTTPServer((os.environ.get("BIND", ""), PORT), H).serve_forever()
