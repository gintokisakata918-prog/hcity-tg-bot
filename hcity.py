"""hentaicity.com scraper + downloader. Koi browser/JS nahi chahiye."""
import asyncio
import re

import httpx

BASE = "https://www.hentaicity.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

QUALITIES = ["1080p", "720p", "480p", "mobile"]

_ID = re.compile(r"^[A-Za-z0-9]{11}$")
_MEDIA = re.compile(r"/videos/(\d{4})/(\d+)/")
_M3U8 = re.compile(r'<source[^>]+src="(https://[^"]+?\.m3u8[^"]*)"')


class ScrapeError(Exception):
    pass


def headers(extra=None):
    h = {"User-Agent": UA, "Referer": BASE + "/"}
    if extra:
        h.update(extra)
    return h


def normalize(raw: str) -> str:
    """User input -> fetchable URL. /click/ wrappers follow karte hain."""
    raw = raw.strip()
    if _ID.match(raw):
        return f"{BASE}/embed/{raw}"
    if not raw.startswith("http"):
        raw = "https://" + raw.lstrip("/")
    return raw


async def scrape(client: httpx.AsyncClient, raw: str) -> dict:
    url = normalize(raw)
    r = await client.get(url, headers=headers(), follow_redirects=True)
    if r.status_code >= 400:
        raise ScrapeError(f"page HTTP {r.status_code}")
    html = r.text

    def meta(prop: str):
        m = re.search(rf'<meta property="{prop}" content="([^"]*)"', html)
        return m.group(1).strip() if m else None

    m = _MEDIA.search(html)
    if not m:
        raise ScrapeError("video id nahi mila (page layout badal gaya?)")
    directory, vid = m.group(1), m.group(2)

    title = meta("og:title") or "Untitled"
    title = re.sub(r"\s*-\s*Hentai City$", "", title)

    info = {
        "page": str(r.url),
        "title": title,
        "description": (meta("og:description") or "")[:900],
        "duration": int(meta("og:video:duration") or 0),
        "width": int(meta("og:video:width") or 0),
        "height": int(meta("og:video:height") or 0),
        "poster": meta("og:image"),
        "mp4": [],
        "hls": None,
    }

    # 1) site khud batata hai konsa mp4 chal raha hai
    for key in ("og:video:secure_url", "og:video:url"):
        v = meta(key)
        if v and v.endswith(".mp4"):
            info["mp4"].append(v)

    # 2) baaki qualities probed below
    info["mp4"] += [f"{BASE}/flv/{directory}/{vid}/{q}.mp4" for q in QUALITIES]

    # 3) HLS fallback
    m3 = _M3U8.search(html)
    if m3:
        info["hls"] = m3.group(1).replace("&amp;", "&")

    return info


async def pick_source(client: httpx.AsyncClient, info: dict, max_bytes: int) -> dict:
    """Pehla chalta hua mp4 chuno, warna HLS."""
    seen = set()
    for url in info["mp4"]:
        if url in seen:
            continue
        seen.add(url)
        try:
            r = await client.head(url, headers=headers(), follow_redirects=True)
            if r.status_code != 200:
                continue
            if "video" not in r.headers.get("content-type", ""):
                continue
            size = int(r.headers.get("content-length") or 0)
            if size and size > max_bytes:
                continue
            return {"url": url, "size": size, "kind": "mp4"}
        except httpx.HTTPError:
            continue

    if info["hls"]:
        return {"url": info["hls"], "size": 0, "kind": "hls"}

    raise ScrapeError("koi downloadable source nahi mila")


async def download(client: httpx.AsyncClient, src: dict, dest: str,
                   referer: str, progress=None) -> int:
    h = headers({"Referer": referer})

    if src["kind"] == "hls":
        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-headers", f"Referer: {referer}\r\nUser-Agent: {UA}\r\n",
               "-i", src["url"], "-c", "copy", "-bsf:a", "aac_adtstoasc", dest]
        proc = await asyncio.create_subprocess_exec(*cmd)
        if await proc.wait() != 0:
            raise ScrapeError("ffmpeg HLS mux fail")
        import os
        return os.path.getsize(dest)

    total = src["size"]
    done = 0
    async with client.stream("GET", src["url"], headers=h) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            async for chunk in r.aiter_bytes(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if progress:
                    await progress(done, total)
    return done


async def fetch_thumb(client: httpx.AsyncClient, info: dict, dest: str) -> str | None:
    if not info.get("poster"):
        return None
    try:
        r = await client.get(info["poster"], headers=headers())
        if r.status_code == 200:
            with open(dest, "wb") as f:
                f.write(r.content)
            return dest
    except httpx.HTTPError:
        pass
    return None
