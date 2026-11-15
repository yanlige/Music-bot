"""
channels2netease —— 视频号好歌自动进网易云「我喜欢的音乐」

链路：分享口令 → 解析视频 → 抽音轨 → 声纹识别 → 网易云搜歌 → 点红心

运行环境：Linux（GitHub Actions / 任意 VPS），需要 ffmpeg
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

import requests

ROOT = Path(__file__).resolve().parent
# 数据文件直接放仓库根目录（手机网页上传建不了子文件夹）
DATA = ROOT

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 命中阈值：低于它就不敢自动点红心，丢进待确认清单
try:
    AUTO_LIKE_SCORE = float(os.environ.get("AUTO_LIKE_SCORE") or 0.72)
except ValueError:
    AUTO_LIKE_SCORE = 0.72


# --------------------------------------------------------------------------
# 一、口令解析
# --------------------------------------------------------------------------

@dataclass
class ShareItem:
    raw: str          # 原始口令
    title: str        # 视频文案（不是歌名！）
    body: str         # 去掉口令前缀和链接后的正文，猜歌名主要用它
    url: str          # 视频号短链
    fingerprint: str  # 去重指纹


def parse_share_text(text: str) -> Optional[ShareItem]:
    """从「3.21 复制打开【微信】观看【标题】 https://...」里拆出标题和链接"""
    text = (text or "").strip()
    if not text:
        return None

    url_m = re.search(r"https?://[^\s，。、）)】]+", text)
    url = url_m.group(0) if url_m else ""

    # 去掉「3.21 复制打开【微信】观看」这类固定前缀，以及链接本身
    body = re.sub(r"^[\d.]+[、,，\s]*复制打开【[^】]*】观看", "", text)
    body = re.sub(r"^复制打开【[^】]*】观看", "", body)
    body = re.sub(r"https?://\S+", "", body).strip()

    # 【】里通常是视频文案；取最长的那个（「微信」这类是噪声）
    brackets = re.findall(r"【([^】]{2,80})】", body)
    title = ""
    if brackets:
        cands = [b for b in brackets if b not in ("微信", "抖音", "视频号", "朋友圈")]
        title = max(cands, key=len) if cands else brackets[-1]
    else:
        # 没有【】时，整段正文就是文案（比如手动打字分享的格式）
        title = body.strip(" 　·•-—|")

    if not title and not url:
        return None

    fp = hashlib.md5((url or title).encode()).hexdigest()[:16]
    return ShareItem(raw=text, title=title, body=body or title, url=url, fingerprint=fp)


# --------------------------------------------------------------------------
# 二、从文案里猜歌名
# --------------------------------------------------------------------------

# 这些词出现在标题里几乎必然是噪音，不是歌名的一部分
NOISE = [
    "热门", "推荐", "完整版", "高音质", "无损", "DJ版", "翻唱", "现场版",
    "纯音乐", "钢琴版", "吉他版", "女声", "男声", "前奏", "副歌", "高潮",
    "这个", "这段", "这首歌", "太好听", "绝了", "封神", "宝藏", "私藏",
    "每日", "分享", "安利", "必听", "必备", "收藏", "码住", "单曲循环", "循环", "治愈", "伤感",
]


# 这些词出现在歌名里，说明不是原版，自动降权（用户要的是原唱）
BAD_VERSION = [
    "cover", "翻唱", "remix", "dj", "伴奏", "live", "现场", "纯音乐",
    "instrumental", "铃声", "片段", "加速版", "慢速版", "女声版", "男声版",
    "钢琴版", "吉他版", "0.8x", "1.2x",
]


def clean_candidate(s: str) -> str:
    s = s.strip(" 　·•-—–|/\\.,，。！!？?：:;；\"'“”‘’()（）[]【】")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def strip_noise(s: str) -> str:
    """删掉「完整版」「翻唱」这类噪音词；若删完变得支离破碎，就原样返回"""
    out = s
    for w in NOISE:
        out = out.replace(w, " ")
    out = re.sub(r"\s+", " ", out).strip()
    if not out:
        return s
    parts = out.split()
    if any(len(p) == 1 for p in parts) or len(out) < len(s) * 0.35:
        return s
    return out


def looks_like_track(s: str) -> bool:
    if not (2 <= len(s) <= 30):
        return False
    parts = [p for p in s.split() if p]
    if len(parts) >= 3:
        if sum(len(p) for p in parts) / len(parts) < 2.4:
            return False
    # 纯 emoji / 纯标点
    if not re.search(r"[\u4e00-\u9fa5A-Za-z0-9]", s):
        return False
    return True


def _dedupe(xs: list[str]) -> list[str]:
    seen, out = set(), []
    for x in xs:
        k = x.lower()
        if k and k not in seen:
            seen.add(k)
            out.append(x)
    return out


def guess_track(text: str) -> list[tuple[str, float]]:
    """
    返回 [(搜索关键词, 可信度权重)]。
    权重是这个候选「有多像真的歌名」，最后会乘到匹配置信度上，
    这样来自整段文案兜底的烂候选就不容易蒙混过关。
    """
    if not text:
        return []
    t = text.strip()
    out: list[tuple[str, float]] = []

    # ① 文案里有《》，那它几乎一定是歌名，其他信息全部忽略
    quoted = [m.group(1) for m in re.finditer(r"《([^》]{1,50})》", t)]
    if quoted:
        for q in quoted:
            out.append((q, 1.0))
            at = t.find(f"《{q}》")
            tail = t[at + len(q) + 2: at + len(q) + 32] if at >= 0 else ""
            m = re.match(r"\s*[-—–·|:：]\s*([^\s《》#，,。]{1,25})", tail)
            if m:
                out.append((f"{q} {m.group(1)}", 1.0))
            else:
                # 「《歌名》歌手」中间不带分隔符的写法也很常见
                rest = tail.strip(" 　-—–·|:：，,。")
                m2 = re.match(r"^([\u4e00-\u9fa5A-Za-z][^\s《》#，,。]{1,19})", rest)
                if m2:
                    out.append((f"{q} {m2.group(1)}", 1.0))
        return _finalize(out)

    # ② 「歌名」
    for m in re.finditer(r"「([^」]{1,50})」", t):
        out.append((m.group(1), 0.95))

    # ③ #标签，音乐号常把歌名和歌手分两个标签挂上
    tags = [m.group(1) for m in re.finditer(r"#([^\s#，,。]{2,30})", t)][:3]
    for tag in tags:
        out.append((tag, 0.95))
    for i in range(len(tags)):
        for j in range(len(tags)):
            if i != j:
                out.append((f"{tags[i]} {tags[j]}", 0.9))

    # ④ 裸的「歌名 - 歌手」
    m = re.match(r"^([^\s\-—–|#]{1,30})\s*[-—–|]\s*([^\s\-—–|#]{1,25})$", t)
    if m:
        out.append((f"{m.group(1)} {m.group(2)}", 0.9))

    # ⑤ 兜底：整段清洗后直接搜
    # 整段文本如果是干净的短句，它本身就极可能是歌名（用户直接打字分享的场景）；
    # 一旦里头掺了「必备」「完整版」这类词，说明那是文案不是歌名，权重压下去。
    plain = clean_candidate(t)
    if len(plain) <= 14 and strip_noise(t) == t.strip():
        out.append((t, 0.95))
    else:
        out.append((t, 0.78))

    return _finalize(out)


def _finalize(pairs: list[tuple[str, float]]) -> list[tuple[str, float]]:
    best: dict[str, float] = {}
    order: list[str] = []
    for raw, w in pairs:
        kw = clean_candidate(strip_noise(raw))
        if not looks_like_track(kw):
            continue
        if kw not in best:
            order.append(kw)
            best[kw] = w
        else:
            best[kw] = max(best[kw], w)
    return [(k, best[k]) for k in order][:8]


# --------------------------------------------------------------------------
# 三、视频号解析（可插拔）
# --------------------------------------------------------------------------

def resolve_video_url(share_url: str, title: str) -> Optional[str]:
    """
    把视频号短链换成可下载的 mp4 直链。
    优先走第三方解析服务（在环境变量里配好即可），没配就返回 None。
    """
    api = os.environ.get("RESOLVER_API_URL", "").strip()
    key = os.environ.get("RESOLVER_API_KEY", "").strip()
    if not api or not share_url:
        return None

    try:
        r = requests.post(
            api,
            params={"key": key} if key else None,
            json={"url": share_url, "content": share_url},
            headers={"User-Agent": UA},
            timeout=45,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  [解析失败] {e}", file=sys.stderr)
        return None

    # 不同服务商字段名不一样，逐个试
    for path in (
        ("data", "video_url"), ("data", "url"), ("data", "play_url"),
        ("video_url",), ("url",), ("play_url",),
    ):
        node = data
        ok = True
        for k in path:
            if isinstance(node, dict) and k in node:
                node = node[k]
            else:
                ok = False
                break
        if ok and isinstance(node, str) and node.startswith("http"):
            return node

    # 备用清晰度列表
    bak = (data.get("data") or {}).get("video_backup") or []
    if isinstance(bak, list) and bak:
        first = bak[0]
        if isinstance(first, dict):
            return first.get("url") or first.get("play_url")
    return None


def download_audio(video_url: str, seconds: int = 35) -> Optional[str]:
    """下载前 N 秒并转成 44.1k 单声道 wav，直接喂给声纹识别"""
    tmp = tempfile.mkdtemp(prefix="c2n_")
    wav = str(Path(tmp) / "a.wav")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-user_agent", UA,
        "-i", video_url,
        "-t", str(seconds),
        "-vn", "-ac", "1", "-ar", "44100",
        wav,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=180)
    except Exception as e:
        print(f"  [抽音轨失败] {e}", file=sys.stderr)
        return None
    return wav if Path(wav).exists() and Path(wav).stat().st_size > 4096 else None


# --------------------------------------------------------------------------
# 四、声纹识别
# --------------------------------------------------------------------------

async def _shazam(wav_path: str):
    from shazamio import Shazam

    out = await Shazam().recognize(wav_path)
    track = out.get("track") or {}
    title = track.get("title") or ""
    artist = track.get("subtitle") or ""
    if title:
        return title, artist
    return None


def identify_sound(wav_path: str) -> Optional[tuple[str, str]]:
    try:
        return asyncio.run(_shazam(wav_path))
    except Exception as e:
        print(f"  [声纹识别失败] {e}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------
# 五、网易云
# --------------------------------------------------------------------------

SEARCH_API = "https://music.163.com/api/search/get/web"


@dataclass
class Track:
    id: int
    name: str
    artist: str
    album: str
    duration: int  # 毫秒

    @property
    def label(self) -> str:
        return f"《{self.name}》{self.artist}"


def netease_search(keyword: str, limit: int = 10) -> list[Track]:
    params = {"s": keyword, "type": 1, "limit": limit, "offset": 0, "total": "true"}
    headers = {"User-Agent": UA, "Referer": "https://music.163.com/"}
    try:
        r = requests.get(SEARCH_API, params=params, headers=headers, timeout=20)
        data = r.json()
    except Exception as e:
        print(f"  [搜索失败] {e}", file=sys.stderr)
        return []

    songs = ((data.get("result") or {}).get("songs")) or []
    out = []
    for s in songs:
        try:
            out.append(
                Track(
                    id=int(s["id"]),
                    name=s.get("name", ""),
                    artist=" / ".join(a.get("name", "") for a in (s.get("artists") or [])),
                    album=(s.get("album") or {}).get("name", ""),
                    duration=int(s.get("duration") or 0),
                )
            )
        except Exception:
            continue
    return out


def sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def pick_best(tracks: list[Track], hint: str, artist_hint: str = "") -> tuple[Optional[Track], float]:
    """
    多候选里挑最像的一首，返回 (歌曲, 置信度)。
    打分 = 歌名匹配 0.82 + 歌手匹配 0.13 + 搜索结果位置 0.05
    """
    if not tracks:
        return None, 0.0

    hint = (hint or "").strip()
    hl = hint.lower()
    best, best_score = None, 0.0

    # 同名歧义检测：前 10 条里有 3 首以上完全同名，说明这歌被翻唱淹没，
    # 又没有歌手线索来区分，那就别自作聪明。
    same_name = sum(1 for t in tracks[:10] if t.name.lower().strip() == hl)
    ambiguous = same_name >= 3

    for i, t in enumerate(tracks):
        nl = t.name.lower()
        al = t.artist.lower()
        parts = [p for p in re.split(r"[/、&,，\s]+", t.artist) if p]

        # 歌名：一方包含另一方就算命中，否则退回编辑距离
        if nl and nl in hl:
            name_s = 1.0
            # 把歌名从关键词里剥掉，剩下的字符串才可能是歌手
            residual = hl.replace(nl, " ", 1).strip()
        elif nl and hl in nl:
            name_s = 1.0
            residual = ""
        else:
            name_s = sim(hint, t.name)
            residual = hint if name_s >= 0.9 else ""

        # 歌手：有明确歌手线索就比歌手，否则拿残留串比。
        # 这一步很关键——不然「起风了」会配上歌手「起风」。
        if artist_hint:
            artist_s = max([sim(artist_hint, p) for p in parts] + [sim(artist_hint, t.artist)])
        elif residual:
            artist_s = max([sim(residual, p) for p in parts] + [sim(residual, t.artist)])
        else:
            artist_s = 0.0

        score = name_s * 0.82 + artist_s * 0.13 + max(0.0, 0.05 - i * 0.006)
        # 翻唱 / Live / 伴奏 一律降权，优先原版
        if any(b in nl for b in BAD_VERSION):
            score -= 0.15
        # 「(原唱 周杰伦)」但歌手根本不是周杰伦 —— 这是蹭标题的翻唱，重罚
        claim = re.search(r"原唱[：:\s]*([^\s)\]）]+)", nl)
        if claim and claim.group(1) not in al:
            score -= 0.22
        # 关键词里明显带着歌手，结果歌手却完全对不上 —— 多半是翻唱或同名歌
        if residual and artist_s < 0.2 and len(residual) <= 6:
            score -= 0.18
        # 同名版本太多又没有歌手线索，保守处理（只对短歌名生效，
        # 「漠河舞厅」这种长歌名同名概率低，不该被误伤）
        if ambiguous and not artist_hint and not residual and len(hl) <= 3:
            score *= 0.8
        if score > best_score:
            best, best_score = t, score

    return best, min(best_score, 1.0)


def search_with_fallback(kw: str) -> list[Track]:
    """带空格的关键词常常搜不出东西，退一步用「歌名」那部分再搜一次"""
    tracks = netease_search(kw)
    if tracks:
        return tracks
    toks = [t for t in kw.split() if t]
    # 候选格式是「歌名 歌手」，所以要退到第一个词，不是最长的词
    if len(toks) >= 2 and toks[0] != kw:
        tracks = netease_search(toks[0])
        if tracks:
            return tracks
    if toks and len(toks[0]) < len(kw):
        return netease_search(max(toks, key=len))
    return []


def like_track(track_id: int) -> bool:
 # ---- 点红心：requests 直调网易云加密接口（不依赖第三方 SDK） ----

_WEAPI_MODULUS = (
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7"
    "b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf69528"
    "0104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee2559"
    "325756ce9b14c0ded6155a7564e8628bcd022995d3ff7a75aaba4a7aa98"
    "d707edc2cd2e7e90f9cffc4be7c8be58f7cf3fe"
)
_WEAPI_IV = b"0102030405060708"


def _aes_b64(text: str, key: str) -> str:
    import base64
    from Crypto.Cipher import AES
    pad = 16 - len(text) % 16
    t = (text + chr(pad) * pad).encode()
    cipher = AES.new(key.encode(), AES.MODE_CBC, _WEAPI_IV)
    return base64.b64encode(cipher.encrypt(t)).decode()


def _rsa_hex(text: str) -> str:
    import binascii
    text = text[::-1]
    rs = pow(int(binascii.hexlify(text.encode()), 16), 65537, int(_WEAPI_MODULUS, 16))
    return format(rs, "0>256x")


def _weapi(data: dict) -> dict:
    import random
    text = json.dumps(data)
    pool = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    secret = "".join(random.sample(pool, 16))
    return {
        "params": _aes_b64(_aes_b64(text, "0CoJUm6Qyw8W8jud"), secret),
        "encSecKey": _rsa_hex(secret),
    }


def like_track(track_id: int) -> bool:
    """点亮红心。走 weapi 加密接口，只需要 MUSIC_U cookie"""
    music_u = os.environ.get("NETEASE_MUSIC_U", "").strip()
    if not music_u:
        print("  [跳过] 没有配置 NETEASE_MUSIC_U", file=sys.stderr)
        return False
    try:
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Referer": "https://music.163.com/",
            "Origin": "https://music.163.com",
        })
        s.cookies.set("MUSIC_U", music_u, domain=".music.163.com")
        s.cookies.set("os", "pc", domain=".music.163.com")
        s.cookies.set("appver", "2.10.11", domain=".music.163.com")

        r = s.post(
            "https://music.163.com/weapi/like",
            data=_weapi({"trackId": track_id, "like": "true", "time": "3", "csrf_token": ""}),
            timeout=15,
        )
        try:
            j = r.json()
        except Exception:
            # 服务器返回空/非 JSON：通常是数据中心 IP 被网易云风控拦截
            print(f"  [加红心失败] HTTP {r.status_code} 但响应为空或非 JSON —— 可能被网易云 IP 风控拦截", file=sys.stderr)
            return False

        code = j.get("code")
        msg = j.get("message") or j.get("msg") or ""
        if code == 200 or "已喜欢" in str(msg):
            return True
        print(f"  [加红心失败] code={code} {msg}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  [加红心失败] {e}", file=sys.stderr)
        return False
# --------------------------------------------------------------------------
# 六、状态与输出
# --------------------------------------------------------------------------

def load_json(name: str, default):
    p = DATA / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def save_json(name: str, obj):
    (DATA / name).write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def notify(title: str, body: str = ""):
    """Bark 推送到 iPhone，免费"""
    url = os.environ.get("BARK_URL", "").strip()
    if not url:
        return
    try:
        base = url if url.startswith("http") else f"https://api.day.app/{url}"
        base = base.rstrip("/")
        path = "/".join(requests.utils.quote(x, safe="") for x in (title, body) if x)
        requests.get(f"{base}/{path}", timeout=10)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 七、主流程
# --------------------------------------------------------------------------

def process_one(item: ShareItem, state: dict) -> dict:
    """处理一条口令，返回结果记录"""
    print(f"\n▶ {item.title or '(无标题)'}")
    rec = {
        "fingerprint": item.fingerprint,
        "title": item.title,
        "url": item.url,
        "status": "pending",
        "track": None,
        "score": 0.0,
        "via": "",
        "ts": int(time.time()),
    }

    # ① 优先走声纹：最准
    video_url = resolve_video_url(item.url, item.title)
    if video_url:
        wav = download_audio(video_url)
        if wav:
            hit = identify_sound(wav)
            if hit:
                name, artist = hit
                print(f"  声纹识别 → {name} / {artist}")
                tracks = netease_search(f"{name} {artist}".strip())
                best, score = pick_best(tracks, name, artist)
                if best:
                    rec.update(track=asdict(best), score=round(score, 3), via="audio")
                    if score < AUTO_LIKE_SCORE:
                        rec["status"] = "pending"
                        print(f"  置信度只有 {score:.2f}，进待确认")
                    elif like_track(best.id):
                        rec["status"] = "liked"
                        state.setdefault("liked", []).append(
                            {"id": best.id, "label": best.label, "ts": rec["ts"]}
                        )
                    else:
                        # 认出来了但没点上红心，必须让人知道，不能假装成功
                        rec["status"] = "failed"
                        rec["note"] = "红心失败，检查 NETEASE_MUSIC_U 是否过期"
                    return rec

    # ② 退而求其次：从文案里猜歌名
    # 注意：不能「第一个达标就收」，同名歌会被排在前面的版本抢走，
    # 所以把所有候选都打一遍分，取全局最高的那首。
    best_track, best_score, best_kw = None, 0.0, ""
    for kw, weight in guess_track(item.body):
        tracks = search_with_fallback(kw)
        if not tracks:
            continue
        cand, score = pick_best(tracks, kw)
        score *= weight  # 候选本身不可信，就打折
        if cand and score > best_score:
            best_track, best_score, best_kw = cand, score, kw
        if best_score >= 0.95:  # 已经够确定了，不用再试后面的候选
            break

    if best_track:
        rec.update(
            track=asdict(best_track),
            score=round(best_score, 3),
            via=f"title:{best_kw}",
        )
        if best_score < AUTO_LIKE_SCORE:
            rec["status"] = "pending"
            print(f"  最像 {best_track.label}，但只有 {best_score:.2f}，进待确认")
        elif like_track(best_track.id):
            rec["status"] = "liked"
            print(f"  文案匹配 → {best_track.label} ({best_score:.2f})")
            state.setdefault("liked", []).append(
                {"id": best_track.id, "label": best_track.label, "ts": rec["ts"]}
            )
        else:
            rec["status"] = "failed"
            rec["note"] = "红心失败，检查 NETEASE_MUSIC_U 是否过期"
            print("  认出来了，但红心没点上")
        return rec

    print("  没认出来，进待确认清单")
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--share", default="", help="分享口令原文")
    ap.add_argument("--file", default="", help="从文件批量读口令，一行一条")
    args = ap.parse_args()

    state = load_json("state.json", {"liked": [], "seen": []})
    pending = load_json("pending.json", [])
    seen = set(state.get("seen", []))

    texts: list[str] = []
    if args.share:
        texts.append(args.share)
    if args.file:
        texts += [
            ln.strip()
            for ln in Path(args.file).read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]

    results = []
    for text in texts:
        item = parse_share_text(text)
        if not item:
            print(f"跳过无法解析的内容：{text[:50]}")
            continue
        if item.fingerprint in seen:
            print(f"已处理过：{item.title[:30]}")
            continue
        seen.add(item.fingerprint)

        rec = process_one(item, state)
        results.append(rec)
        if rec["status"] != "liked":
            pending.append(rec)

    state["seen"] = list(seen)
    save_json("state.json", state)
    save_json("pending.json", pending)
    save_json(
        "latest.json",
        {"ts": int(time.time()), "results": results, "liked_total": len(state.get("liked", []))},
    )

    ok = sum(1 for r in results if r["status"] == "liked")
    failed = sum(1 for r in results if r["status"] == "failed")
    pend = len(results) - ok - failed
    if results:
        bits = [f"处理 {len(results)} 首", f"成功 {ok} 首"]
        if pend:
            bits.append(f"{pend} 首待确认")
        if failed:
            bits.append(f"{failed} 首失败")
        print("\n" + "，".join(bits))
        notify(
            f"存歌 {ok}/{len(results)}",
            ("；".join(t["track"]["name"] for t in results if t["status"] == "liked")
             or "全部进了待确认"),
        )


if __name__ == "__main__":
    main()
