"""事件桥：把 task/wait 里的 json 任务，变成一条"对方发来的消息"投进框架。

这样走的就是完整的正常对话流程：人设、记忆、工具、命令、分段，全都生效，
和用户本人发一句话没有区别。

目录固定在工作区下：task/wait、task/done、task/fail
配置：推送到哪个会话、用不用指定模型、阈值、提示词模板、扫描间隔
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star

DEFAULT_SESSION = ""  # 必填：你的平台:消息类型:会话ID
DEFAULT_TEMPLATE = "【系统通知】\n任务：{task}\n时间：{ts}\n变化：{ratio}\n内容：{text}"

NO_VALUES = {"no", "false", "0", "off", "silent", "不", "否"}
YES_VALUES = {"yes", "true", "1", "on", "是"}

# notify 不是投递开关，是写给模型的指令。三种固定说法。
HINT_NO = (
    "\n\n（以上是后台系统通知。请按「目的」把事情办完——该看就看、该动手就动手。"
    "但办完之后不要推送消息，不用打扰。）"
)
HINT_YES = (
    "\n\n（以上是后台系统通知。请按「目的」把事情办完，并把结果推送出去，必须回一句。）"
)
HINT_AUTO = (
    "\n\n（以上是后台系统通知。请按「目的」把事情办完。"
    "办完值不值得推送，你自己判断；不值得就只做事、不发消息。）"
)


def _to_text(value) -> str:
    """任何类型的值都转成字符串，None 转空串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


class EventBridgePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._task: asyncio.Task | None = None

    # ---------------- 路径 ----------------
    @property
    def base_dir(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_screen_watch"
        except Exception:
            return Path(__file__).resolve().parent / "data"

    @property
    def wait_dir(self) -> Path:
        return self.base_dir / "wait"

    @property
    def done_dir(self) -> Path:
        return self.base_dir / "done"

    @property
    def fail_dir(self) -> Path:
        return self.base_dir / "fail"

    @property
    def error_file(self) -> Path:
        return self.fail_dir / "error.json"

    @property
    def log_file(self) -> Path:
        return self.base_dir / "bridge.log"

    def _log(self, message: str) -> None:
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # 多行内容压成一行，方便逐条 grep
            flat = str(message).replace("\r", " ").replace("\n", " | ")
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(f"[{stamp}] {flat}\n")
        except Exception:
            pass

    # ---------------- 配置 ----------------
    def _cfg(self, key: str, default=None):
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    @property
    def target_session(self) -> str:
        return str(self._cfg("target_session", DEFAULT_SESSION))

    @property
    def template(self) -> str:
        return str(self._cfg("prompt_template", DEFAULT_TEMPLATE) or DEFAULT_TEMPLATE)

    @property
    def threshold(self) -> float:
        try:
            return float(self._cfg("threshold", 0.02))
        except Exception:
            return 0.02

    @property
    def reconcile_interval(self) -> float:
        try:
            return max(10.0, float(self._cfg("reconcile_interval", 30)))
        except Exception:
            return 30.0

    @property
    def poll_interval(self) -> float:
        try:
            return max(1.0, float(self._cfg("poll_interval", 3)))
        except Exception:
            return 3.0

    # ---------------- 生命周期 ----------------
    async def initialize(self) -> None:
        for d in (self.wait_dir, self.done_dir, self.fail_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._loop(), name="event_bridge_loop")
        logger.info(f"[event_bridge] 已启动，监听 {self.wait_dir}")

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("[event_bridge] 已停止")

    # ---------------- 主循环 ----------------
    async def _loop(self) -> None:
        last_reconcile = 0.0
        while True:
            try:
                await self._drain()
                now = asyncio.get_event_loop().time()
                if now - last_reconcile >= self.reconcile_interval:
                    last_reconcile = now
                    await self._reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log(f"循环异常: {e}")
                logger.error(f"[event_bridge] 循环异常: {e}")
            await asyncio.sleep(self.poll_interval)

    # ---------------- 监听看护：按 watchers.json 对齐现实 ----------------
    @property
    def watchers_file(self) -> Path:
        return self.base_dir / "watchers.json"

    @property
    def watch_script(self) -> str:
        """监听脚本就在插件目录里，跟 main.py 挨着。"""
        return str(Path(__file__).resolve().parent / "watch.py")

    def _read_watchers(self) -> list[dict] | None:
        """读期望清单。读不出来返回 None——这一轮什么都不做，也不杀人。"""
        f = self.watchers_file
        if not f.exists():
            return []
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("不是数组")
            return [x for x in data if isinstance(x, dict) and x.get("task")]
        except Exception as e:
            self._log(f"watchers.json 读取失败，本轮跳过: {e}")
            return None

    async def _pid_alive(self, pid: int) -> bool:
        out = await asyncio.to_thread(
            lambda: subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, errors="ignore",
            ).stdout
        )
        return str(pid) in out

    def _read_pid(self, task: str) -> int | None:
        f = self.base_dir / f"watch_{task}.pid"
        if not f.exists():
            return None
        try:
            raw = f.read_text(encoding="utf-8").strip()
            st = json.loads(raw) if raw.startswith("{") else {"pid": int(raw)}
            return int(st.get("pid") or 0) or None
        except Exception:
            return None

    async def _kill(self, pid: int) -> None:
        await asyncio.to_thread(
            subprocess.run, ["taskkill", "/F", "/PID", str(pid)], capture_output=True
        )

    def _spawn(self, spec: dict) -> bool:
        task = str(spec.get("task") or "").strip()
        window = str(spec.get("window") or "").strip()
        purpose = str(spec.get("purpose") or "").strip()
        if not task or not window or not purpose:
            self._log(f"清单项缺少 task/window/purpose，跳过: {spec}")
            return False
        cmd = [
            sys.executable, self.watch_script,
            "--window", window, "--task", task, "--purpose", purpose,
            "--interval", str(spec.get("interval", 3)),
            "--threshold", str(spec.get("threshold", 0.0)),
            "--notify", str(spec.get("notify", "auto")),
        ]
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._log(f"已拉起监听: {task}")
            return True
        except Exception as e:
            self._log(f"拉起失败 {task}: {e}")
            return False

    async def _reconcile(self) -> None:
        specs = self._read_watchers()
        if specs is None:
            return
        wanted = {str(x["task"]): x for x in specs if x.get("enabled", True)}

        for task, spec in wanted.items():
            pid = self._read_pid(task)
            if pid and await self._pid_alive(pid):
                continue
            self._spawn(spec)

        for f in self.base_dir.glob("watch_*.pid"):
            task = f.stem.replace("watch_", "")
            if task in wanted:
                continue
            pid = self._read_pid(task)
            if pid:
                await self._kill(pid)
                self._log(f"清理迷路监听: {task} (pid {pid})")
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass

    async def _drain(self) -> None:
        try:
            files = sorted(
                (p for p in self.wait_dir.glob("*.json")),
                key=lambda p: p.stat().st_mtime,
            )
        except Exception:
            return
        for path in files[:3]:
            await self._handle(path)

    # ---------------- 单条处理 ----------------
    async def _handle(self, path: Path) -> None:
        try:
            raw = path.read_text(encoding="utf-8")
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError("内容不是对象")
        except Exception as e:
            self._fail(path, "-", f"json 解析失败: {e}")
            return

        task = _to_text(event.get("task")).strip()
        text = _to_text(event.get("text")).strip()
        purpose = _to_text(event.get("purpose")).strip()
        if not task or not text:
            self._fail(path, task or path.stem, "缺少必填字段 task 或 text")
            return
        if not purpose:
            self._fail(path, task, "缺少必填字段 purpose（这个任务要干什么）")
            return

        ratio_raw = _to_text(event.get("ratio")).strip()
        try:
            ratio = float(ratio_raw) if ratio_raw else 0.0
        except Exception:
            ratio = 0.0
        ts = _to_text(event.get("ts")).strip() or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        extra = _to_text(event.get("extra"))
        notify = _to_text(event.get("notify")).strip().lower() or "auto"

        if ratio < self.threshold:
            self._log(f"跳过（{ratio:.0%} 低于阈值 {self.threshold:.0%}）: {path.name} | {task}")
            self._archive(path, "done")
            return

        prompt = self._render(
            {
                "task": task,
                "purpose": purpose,
                "text": text,
                "ratio": f"{ratio:.0%}" if ratio else "0%",
                "ratio_raw": ratio_raw or "0",
                "ts": ts,
                "extra": extra,
                "notify": notify,
                "filename": path.name,
                "raw": raw,
            }
        )

        # 把 notify 翻译成一句固定指令，附在通知正文后面
        if notify in NO_VALUES:
            prompt += HINT_NO
        elif notify in YES_VALUES:
            prompt += HINT_YES
        else:
            prompt += HINT_AUTO

        if self._session_busy(self.target_session):
            # 会话正忙，先把它留在 wait 里，下一轮再看
            self._log(f"会话正忙，暂缓投递: {path.name}")
            return

        ok = await self._inject(prompt)
        self._archive(path, "done" if ok else "fail")
        if not ok:
            self._fail(path, task, "投递事件失败", archived=True)

    def _render(self, variables: dict) -> str:
        class _Safe(dict):
            def __missing__(self, key):
                return ""

        try:
            return self.template.format_map(_Safe(variables))
        except Exception as e:
            self._log(f"模板填充失败，退回原文: {e}")
            return "{task}: {text}".format_map(_Safe(variables))

    def _session_busy(self, session: str) -> bool:
        """看这个会话这会儿是不是正在处理消息。

        忙的时候投进去，事件会被并进当前那一轮，等于白叫；所以先问一句。
        """
        try:
            from astrbot.core.utils.session_lock import session_lock_manager

            mgr = session_lock_manager._get_loop_manager()
            lock = mgr._locks.get(session)
            return bool(lock and lock.locked())
        except Exception:
            return False

    async def _inject(self, text: str) -> bool:
        """把这段文字当成"对方发来的一条消息"投进框架。"""
        try:
            from astrbot.core.message.components import Plain as CorePlain
            from astrbot.core.platform.astrbot_message import (
                AstrBotMessage,
                MessageMember,
            )
            from astrbot.core.platform.message_session import MessageSesion
            from astrbot.core.platform.message_type import MessageType

            ses = MessageSesion.from_str(self.target_session)
            platform = self.context.get_platform_inst(ses.platform_id)
            if platform is None:
                self._log(f"找不到平台实例: {ses.platform_id}")
                return False

            msg = AstrBotMessage()
            msg.type = MessageType.FRIEND_MESSAGE
            msg.self_id = ""
            msg.session_id = ses.session_id
            msg.message_id = f"bridge-{uuid.uuid4().hex[:12]}"
            msg.sender = MessageMember(user_id=ses.session_id, nickname="后台")
            msg.message = [CorePlain(text)]
            msg.message_str = text
            msg.raw_message = {"bridge": True}

            event = platform.create_event(msg)
            platform.commit_event(event)
            self._log(f"已投递事件: {text}")
            logger.info(f"[event_bridge] 已投递事件: {text[:80]}")
            return True
        except Exception as e:
            self._log(f"投递失败: {type(e).__name__}: {e}")
            logger.error(f"[event_bridge] 投递失败: {e}")
            return False

    # ---------------- 归档 ----------------
    def _archive(self, path: Path, sub: str) -> None:
        target_dir = self.done_dir if sub == "done" else self.fail_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        try:
            if target.exists():
                target.unlink()
            shutil.move(str(path), str(target))
        except Exception as e:
            self._log(f"归档失败 {path.name}: {e}")

        if sub == "done":
            cutoff = datetime.now() - timedelta(days=7)
            for old in target_dir.glob("*.json"):
                try:
                    if datetime.fromtimestamp(old.stat().st_mtime) < cutoff:
                        old.unlink()
                except Exception:
                    pass

    def _fail(self, path: Path, task: str, reason: str, archived: bool = False) -> None:
        record = {
            "file": path.name,
            "task": task,
            "reason": reason,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            self.fail_dir.mkdir(parents=True, exist_ok=True)
            records = []
            if self.error_file.exists():
                try:
                    old = json.loads(self.error_file.read_text(encoding="utf-8"))
                    if isinstance(old, list):
                        records = old
                except Exception:
                    records = []
            records.append(record)
            self.error_file.write_text(
                json.dumps(records[-200:], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            self._log(f"写失败清单出错: {e}")
        if not archived:
            self._archive(path, "fail")
