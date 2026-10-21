from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File
from astrbot.api.star import Context, Star, register

DEFAULT_PYTHON = r"D:\FurinaSinger\.venv\Scripts\python.exe"
DEFAULT_SCRIPT = r"D:\FurinaSinger\sing_queue.py"
ACE_SCRIPT_DEFAULT = r"D:\FurinaSinger\sing_queue_ace.py"
ACE_LANGS = {"ja","ko","es","fr","de","it","pt","ru","bn","hi","ar","th","vi","id","tr","nl","pl"}
LYRIC_DIR = Path(r"D:\Furina\temp\song_lyrics")


def _slug(text: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "_", text.strip())[:24].strip("_")
    return s or "song"


class SongRequest(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

    # ---------- 配置 ----------

    def _cfg_str(self, key: str, default: str) -> str:
        raw = str(self.config.get(key) or default)
        return raw.replace("\\\\", "\\").strip() or default

    def _enabled_groups(self) -> set[str]:
        raw = self.config.get("enabled_groups") or []
        if isinstance(raw, str):
            raw = [raw]
        return {str(x).strip() for x in raw if str(x).strip()}

    # ---------- 落词 ----------

    def _write_lyrics(self, topic: str, lyrics: str) -> Path:
        LYRIC_DIR.mkdir(parents=True, exist_ok=True)
        path = LYRIC_DIR / f"{_slug(topic)}_{time.strftime('%Y%m%d%H%M%S')}.txt"
        path.write_text(lyrics.strip() + "\n", encoding="utf-8")
        return path

    # ---------- 调流水线 ----------

    async def _render(self, group: str, lyric_path: Path, out_name: str, prompt: str, unlimited: bool) -> dict:
        py = self._cfg_str("queue_python", DEFAULT_PYTHON)
        script = self._cfg_str("queue_script", DEFAULT_SCRIPT)
        if not os.path.exists(py):
            return {"ok": False, "reason": f"找不到 python：{py}"}
        if not os.path.exists(script):
            return {"ok": False, "reason": f"找不到 sing_queue.py：{script}"}

        cmd = [py, script, "--group", group, "--lyrics", str(lyric_path),
               "--out", out_name, "--prompt", prompt, "--takes", "1"]
        if unlimited:
            cmd.append("--unlimited")

        timeout = int(self.config.get("timeout_seconds") or 300)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "TEMP": r"D:\Furina\temp", "TMP": r"D:\Furina\temp"},
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "reason": f"录了 {timeout} 秒还没出来，先算了"}
        except Exception as e:  # noqa: BLE001
            logger.error("点歌机启动失败: %s", e)
            return {"ok": False, "reason": "流水线起不来"}

        text = (out or b"").decode("utf-8", "replace").strip()
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except Exception:
                    continue
        logger.warning("sing_queue 输出不可解析: %s / %s", text[-400:], (err or b"").decode("utf-8", "replace")[-400:])
        return {"ok": False, "reason": "没拿到结果（输出不是 JSON）"}

    async def _render_ace(self, group: str, lyric_path: Path, out_name: str, prompt: str,
                          lang: str, unlimited: bool) -> dict:
        py = self._cfg_str("queue_python", DEFAULT_PYTHON)
        script = self._cfg_str("ace_script", ACE_SCRIPT_DEFAULT)
        if not os.path.exists(script):
            return {"ok": False, "reason": "找不到重量分支入口：%s" % script}
        cmd = [py, script, "--group", group, "--lyrics", str(lyric_path),
               "--out", out_name, "--prompt", prompt, "--lang", lang]
        if unlimited:
            cmd.append("--unlimited")
        timeout = int(self.config.get("ace_timeout_seconds") or 1800)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                env={**os.environ, "TEMP": r"D:\Furina\temp", "TMP": r"D:\Furina\temp"},
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "reason": "重量分支跑了 %d 秒还没出来" % timeout}
        except Exception as e:  # noqa: BLE001
            logger.error("点歌机(重分支)启动失败: %s", e)
            return {"ok": False, "reason": "重量分支起不来"}
        text = (out or b"").decode("utf-8", "replace").strip()
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except Exception:
                    continue
        logger.warning("sing_queue_ace 输出不可解析: %s", text[-400:])
        return {"ok": False, "reason": "重量分支没拿到结果"}

    # ---------- 对外 ----------

    def _group_key(self, event: AstrMessageEvent) -> str:
        try:
            if "GroupMessage" in str(getattr(event, "unified_msg_origin", "")):
                gid = ""
                getter = getattr(event, "get_group_id", None)
                if callable(getter):
                    try:
                        gid = str(getter() or "")
                    except Exception:
                        gid = ""
                return gid or str(event.get_session_id() or "default")
        except Exception:
            pass
        return str(event.get_unified_msg_origin() if hasattr(event, "get_unified_msg_origin") else getattr(event, "unified_msg_origin", "default"))

    @filter.llm_tool(name="create_song")
    async def create_song(self, event: AstrMessageEvent, topic: str, lyrics: str = "", style: str = "", language: str = "", high_quality: bool = False) -> str:
        """写一首原创歌曲并渲染成 mp3 交给对方。只要对方想要一首歌就用这个工具。

        Args:
            topic(string): 主题，写给谁、什么场景
            lyrics(string): 完整歌词，用 [verse]/[chorus] 标段落，一行一句；留空则按主题现写
            style(string): 英文风格提示，逗号分隔；留空用默认
            language(string): 语言代码，中文 zh、英文 en；其它语言（ja ko es fr de it pt ru bn hi ar th vi id tr nl pl）必须走重量分支
            high_quality(boolean): 用户明确要求高质量、顶配、更好听时才填 true；没说就留空走轻量分支
        """
        groups = self._enabled_groups()
        if groups:
            key = self._group_key(event)
            if key not in groups:
                return "这个群没开点歌，如实告诉对方。"

        if not (lyrics or "").strip():
            return "歌词还没写。先按主题写好 4～16 句、标好 [verse]/[chorus]，再调一次这个工具并带上 lyrics。"

        prompt = (style or "").strip() or self._cfg_str(
            "style_prompt", "uplifting chinese pop rock, warm male vocal, live drums")

        try:
            is_admin = bool(event.is_admin())
        except Exception:
            is_admin = False

        lyric_path = self._write_lyrics(topic, lyrics)
        out_name = _slug(topic)
        lang = (language or "").strip().lower()
        want_heavy = bool(high_quality) or (lang in ACE_LANGS)
        if want_heavy:
            res = await self._render_ace(self._group_key(event), lyric_path, out_name, prompt, lang, is_admin)
        else:
            res = await self._render(self._group_key(event), lyric_path, out_name, prompt, is_admin)

        if res.get("ok"):
            return json.dumps({
                "ok": True,
                "file": res.get("file", ""),
                "topic": topic,
                "branch": "ace" if want_heavy else "light",
                "提示": "用 send_message_to_user 以 file 类型把这个 mp3 发给对方，并附一句人话。",
            }, ensure_ascii=False)
        return json.dumps({"ok": False, "reason": res.get("reason", "生成失败")}, ensure_ascii=False)

    @staticmethod
    def _strip_fences(text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
            text = "\n".join(lines).strip()
        return text

    @filter.command("点歌", alias={"写歌", "唱首歌"})
    async def song_cmd(self, event: AstrMessageEvent):
        """点歌 <主题>"""
        args = event.get_message_str().split(maxsplit=1)
        topic = args[1].strip() if len(args) > 1 else ""
        if not topic or topic.startswith("/"):
            yield event.plain_result("给个主题：/点歌 关于早八")
            return

        groups = self._enabled_groups()
        if groups and self._group_key(event) not in groups:
            yield event.plain_result("这个群没开点歌。")
            return

        yield event.plain_result("在录，等一分钟。")

        try:
            prov = await self.context.get_using_provider_async(umo=event.unified_msg_origin)
        except Exception as e:  # noqa: BLE001
            logger.error("点歌取模型失败: %s", e)
            prov = None
        if prov is None:
            yield event.plain_result("这会儿写词的脑子不在，先别点。")
            return

        try:
            resp = await prov.text_chat(
                prompt=f"主题：{topic}",
                system_prompt=(
                    "你写歌词。输出 4~16 句，用 [verse] [chorus] 标段落，一行一句，"
                    "不加解释、不加时间戳、不用代码块。押同一个宽韵，具体名词优先于形容词，"
                    "不写口号，不写政治，不写针对真实个人的攻击。"
                ),
            )
            lyrics = self._strip_fences(resp.completion_text or "")
        except Exception as e:  # noqa: BLE001
            logger.error("点歌写词失败: %s", e)
            lyrics = ""

        if not lyrics:
            yield event.plain_result("词没写出来，这次没录成。")
            return

        try:
            is_admin = bool(event.is_admin())
        except Exception:
            is_admin = False

        prompt_style = self._cfg_str(
            "style_prompt", "uplifting chinese pop rock, warm male vocal, live drums")
        lyric_path = self._write_lyrics(topic, lyrics)
        res = await self._render(self._group_key(event), lyric_path, _slug(topic), prompt_style, is_admin)

        if res.get("ok") and res.get("file"):
            yield event.chain_result([File(name=Path(str(res["file"])).name, file=str(res["file"]))])
        else:
            yield event.plain_result("没成：" + str(res.get("reason") or "生成失败"))
