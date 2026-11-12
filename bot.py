import asyncio
import html
import logging
import os
import tempfile
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.types import Message

import hcity

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hcity")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
MAX_BYTES = int(float(os.environ.get("MAX_SIZE_MB", "1900")) * 1024 * 1024)
PROXY = os.environ.get("HTTP_PROXY") or None
COOKIE = os.environ.get("HCITY_COOKIE") or None
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
WORKERS = 2

sem = asyncio.Semaphore(WORKERS)


def fmt(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n else "?"


def dur(sec: int) -> str:
    sec = int(sec or 0)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def is_allowed(uid: int) -> bool:
    return not ALLOWED or uid in ALLOWED


def _progress(status: Message, label: str):
    loop = asyncio.get_running_loop()
    last = [0.0, ""]

    def cb(current, total):
        try:
            now = time.monotonic()
            total = total or 0
            pct = f"{current * 100 // total}%" if total else "?"
            text = f"{label} {pct} · {fmt(current)}"
            if text == last[1] or (now - last[0] < 4 and pct != "100%"):
                return
            last[0] = now
            last[1] = text

            async def edit():
                try:
                    await status.edit_text(text)
                except Exception:
                    pass

            asyncio.run_coroutine_threadsafe(edit(), loop)
        except Exception:
            pass

    return cb


app = Client(
    "hcity_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    session_string=os.environ.get("SESSION_STRING") or None,
    proxy=dict(host=PROXY.split("//")[-1].split(":")[0],
               port=int(PROXY.split(":")[-1]),
               scheme=PROXY.split("//")[0].rstrip(":")) if PROXY else None,
)


@app.on_message(filters.command(["start", "help"]))
async def start(_, msg: Message):
    if not is_allowed(msg.from_user.id):
        return await msg.reply(f"Access nahi hai. Tumhara ID: `{msg.from_user.id}`",
                               parse_mode=enums.ParseMode.MARKDOWN)
    await msg.reply(
        "<b>hentaicity.com → Telegram</b>\n\n"
        "Video ka link bhejo (ya sirf 11-character ID).\n"
        "Bot download karke yahin upload kar dega.\n\n"
        f"Limit: {fmt(MAX_BYTES)}",
        parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.text & ~filters.command(["start", "help"]))
async def handle(_, msg: Message):
    if not is_allowed(msg.from_user.id):
        return await msg.reply(f"Access nahi hai. Tumhara ID: `{msg.from_user.id}`",
                               parse_mode=enums.ParseMode.MARKDOWN)

    raw = msg.text.strip().split()[0] if msg.text.strip() else ""
    status = await msg.reply("🔎 Link check kar raha hoon…")

    async with sem:
        work = Path(tempfile.mkdtemp(prefix="hcity_"))
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=120),
                                         headers={"Cookie": COOKIE} if COOKIE else None) as hc:
                info = await hcity.scrape(hc, raw)
                src = await hcity.pick_source(hc, info, MAX_BYTES)

                if not src["size"]:
                    await status.edit_text("⬇️ HLS se download (ffmpeg)…")
                else:
                    await status.edit_text(f"⬇️ Download: {fmt(src['size'])}")

                dest = str(work / "video.mp4")
                last = [0.0]

                async def prog(done, total):
                    now = time.time()
                    if now - last[0] < 4:
                        return
                    last[0] = now
                    pct = f"{done * 100 // total}%" if total else fmt(done)
                    try:
                        await status.edit_text(f"⬇️ Download {pct} — {fmt(done)}")
                    except Exception:
                        pass

                size = await hcity.download(hc, src, dest, info["page"], prog)
                if size > MAX_BYTES:
                    raise hcity.ScrapeError(f"file {fmt(size)} — limit se bada")

                thumb = await hcity.fetch_thumb(hc, info, str(work / "thumb.jpg"))

            await status.edit_text("⬆️ Telegram par upload…")

            caption = (
                f"<b>{html.escape(info['title'])}</b>\n"
                f"⏱ {dur(info['duration'])} · {fmt(size)}\n"
                f'<a href="{html.escape(info["page"])}">source</a>'
            )[:1000]

            if os.path.getsize(dest) == 0:
                raise hcity.ScrapeError("file 0 bytes hai")

            await app.send_video(
                chat_id=msg.chat.id,
                video=dest,
                caption=caption,
                parse_mode=enums.ParseMode.HTML,
                duration=info["duration"] or None,
                width=info["width"] or None,
                height=info["height"] or None,
                thumb=thumb,
                file_name=f"{info['title'][:80]}.mp4",
                supports_streaming=True,
                progress=_progress(status, "⬆️ Upload"),
                reply_to_message_id=msg.id,
            )
            await status.delete()

        except Exception as e:
            log.exception("fail")
            try:
                await status.edit_text(f"❌ {html.escape(str(e))[:400]}",
                                       parse_mode=enums.ParseMode.HTML)
            except Exception:
                pass
        finally:
            for p in work.rglob("*"):
                p.unlink(missing_ok=True)
            work.rmdir()


if __name__ == "__main__":
    log.info("bot start")
    app.run()
