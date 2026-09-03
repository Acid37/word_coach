"""word_coach 局域网直连服务器（插件级，不动核心 HTTP 绑定）。

复用 WordCoachWebRouter 自带的独立 FastAPI 应用（BaseRouter 每个路由组件
自持一个 app，核心只是把它 mount 到 /word-coach 而已），额外用 uvicorn
监听 0.0.0.0:lan_port。手机/平板在同一局域网直接输
http://<局域网IP>:lan_port/ 即可使用；核心 [http_router] 的 127.0.0.1
绑定保持不变，两不相扰。

设计要点：
- 包装 app 把 /word-coach 与 /word-coach/ 重定向到 /，带不带路径都能进。
- uvicorn 以编程方式嵌入运行：Server 子类把 install_signal_handlers 置空，
  避免覆盖框架主进程已注册的信号处理。
- 本模块不 import 任何 src.* 框架模块，可脱离框架独立烟测。
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

try:
    import uvicorn
    from uvicorn import Config as UvicornConfig
except ImportError:  # pragma: no cover - 框架环境自带 uvicorn，缺失时 start() 会报错
    uvicorn = None

_log = logging.getLogger("word_coach.lan")


def detect_lan_ip() -> str:
    """取本机局域网 IP（UDP connect 探测路由，不实际发包）；失败逐级回退。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    except OSError:
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            ip = "127.0.0.1"
    finally:
        sock.close()
    return ip


class _QuietServer(uvicorn.Server if uvicorn else object):
    """uvicorn 嵌入式运行：不安装信号处理，不抢主进程的 SIGINT/SIGTERM。"""

    def install_signal_handlers(self) -> None:
        return None


class LanServer:
    """word_coach Web UI 的局域网直连监听（默认 0.0.0.0:8900）。

    Usage:
        server = LanServer(router_app, port=8900)
        await server.start()   # 后台任务运行，失败抛 RuntimeError
        ...
        await server.stop()
    """

    def __init__(self, app: Any, host: str = "0.0.0.0", port: int = 8900) -> None:
        self._inner_app = app
        self._host = host
        self._port = port
        self._server: Any = None
        self._task: asyncio.Task | None = None

    def _build_app(self) -> FastAPI:
        """包装 app：/word-coach 与 /word-coach/ 重定向到 /，其余原样伺服。"""
        outer = FastAPI(
            title="word_coach lan",
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )

        @outer.get("/word-coach", include_in_schema=False)
        @outer.get("/word-coach/", include_in_schema=False)
        async def _redirect() -> RedirectResponse:
            return RedirectResponse(url="/")

        outer.mount("/", self._inner_app)
        return outer

    async def start(self) -> None:
        """启动监听。端口被占用等启动失败会抛 RuntimeError。"""
        if uvicorn is None:
            raise RuntimeError("uvicorn 不可用，无法启动局域网直连")
        if self._server is not None:
            return
        config = UvicornConfig(
            app=self._build_app(),
            host=self._host,
            port=self._port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        self._server = _QuietServer(config=config)

        async def _serve() -> None:
            try:
                await self._server.serve()
            except Exception as exc:  # 运行期异常只记日志，不拖垮插件
                _log.warning("word_coach 局域网直连异常退出: %s", exc)

        self._task = asyncio.create_task(_serve())
        # 留一拍给 uvicorn 完成绑定，尽早暴露端口占用等问题
        await asyncio.sleep(0.3)
        if not getattr(self._server, "started", False):
            exc = self._task.exception() if self._task.done() else None
            await self.stop()
            raise RuntimeError(
                f"局域网监听启动失败（端口 {self._port} 被占用？）: {exc or '未进入监听状态'}"
            )

    async def stop(self) -> None:
        """请求退出并等待监听结束（最多 5 秒）。"""
        if self._server is None:
            return
        self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            except Exception:
                pass
        self._task = None
        self._server = None

    @property
    def url(self) -> str:
        """手机上直接输的局域网地址。"""
        return f"http://{detect_lan_ip()}:{self._port}/"
