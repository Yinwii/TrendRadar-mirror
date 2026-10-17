#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TrendRadar 内置 Web 服务：静态报告 + NewsNow 全局配置 API + 后台管理页。

替代原先的 `python -m http.server`：
    python trendradar/webserver.py [port] [root]
    python -m trendradar.webserver [port] [root]

路由：
    GET  /                        -> 静态文件（root 目录，默认 output/）
    GET  /admin                   -> 后台管理页
    GET  /api/newsnow/config      -> 全局配置（公开只读）
    POST /api/newsnow/config      -> 保存全局配置（需 X-NN-Token）

注意：刻意不 import `trendradar` 包（其 __init__ 会连带加载 requests/pytz 等
重依赖），而是按文件路径加载 newsnow_config，保证 Web 服务能独立启动。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ADMIN_FILE = _HERE / "report" / "newsnow_admin.html"
_MAX_BODY = 256 * 1024


def _load_config_module():
    """按文件路径加载 newsnow_config，避免触发包级重依赖导入。"""
    path = _HERE / "report" / "newsnow_config.py"
    spec = importlib.util.spec_from_file_location("_nn_config", str(path))
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


newsnow_config = _load_config_module()


class Handler(SimpleHTTPRequestHandler):
    server_version = "TrendRadar/1.0"
    protocol_version = "HTTP/1.1"
    timeout = 60  # 空闲 keep-alive 连接超时，避免挂死的半开连接

    # ---------------- 工具 ----------------

    def _send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-NN-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _path(self) -> str:
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    def _token(self) -> str:
        tok = self.headers.get("X-NN-Token") or ""
        if not tok:
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            for part in q.split("&"):
                if part.startswith("token="):
                    tok = part[6:]
                    break
        return tok.strip()

    # ---------------- 路由 ----------------

    def do_OPTIONS(self):  # noqa: N802
        self._send_json({"ok": True})

    def do_GET(self):  # noqa: N802
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as e:  # 永不让连接裸断：浏览器端会表现为 JSON 截断
            try:
                self._send_json({"ok": False, "error": f"服务端异常: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            except Exception:
                pass

    def _route_get(self):  # noqa: N802
        path = self._path()
        if path == "/api/newsnow/config":
            cfg = newsnow_config.public_view()
            return self._send_json({
                "ok": True,
                "config": cfg,
                "writesEnabled": newsnow_config.writes_enabled(),
            })
        if path == "/api/newsnow/auth":
            return self._send_json({
                "ok": True,
                "valid": newsnow_config.check_token(self._token()),
                "writesEnabled": newsnow_config.writes_enabled(),
            })
        if path == "/admin":
            return self._send_admin()
        if path == "/favicon.ico":
            return self._send_file(_HERE / "report" / "newsnow_favicon.ico", "image/x-icon", cache=True)
        if path == "/favicon.svg":
            return self._send_file(_HERE / "report" / "newsnow_favicon.svg", "image/svg+xml", cache=True)
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        try:
            self._route_post()
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as e:
            try:
                self._send_json({"ok": False, "error": f"服务端异常: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            except Exception:
                pass

    def _route_post(self):  # noqa: N802
        path = self._path()
        if path != "/api/newsnow/config":
            return self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

        if not newsnow_config.writes_enabled():
            return self._send_json(
                {"ok": False, "error": "服务端未启用写入：请设置环境变量 NN_ADMIN_TOKEN 或允许写 state 目录"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        if not newsnow_config.check_token(self._token()):
            return self._send_json({"ok": False, "error": "管理员口令不正确"}, HTTPStatus.UNAUTHORIZED)

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_BODY:
            return self._send_json({"ok": False, "error": "请求体为空或过大"}, HTTPStatus.BAD_REQUEST)

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:
            return self._send_json({"ok": False, "error": f"JSON 解析失败: {e}"}, HTTPStatus.BAD_REQUEST)

        try:
            cfg = newsnow_config.save(payload)
        except Exception as e:
            return self._send_json({"ok": False, "error": f"写入失败: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        sys.stderr.write("[newsnow] 全局配置已更新 %s\n" % cfg.get("updated_at"))
        return self._send_json({"ok": True, "config": cfg})

    # ---------------- 管理页 ----------------

    def _send_file(self, file_path: Path, content_type: str, cache: bool = False):
        try:
            body = file_path.read_bytes()
        except Exception as e:
            return self._send_json({"ok": False, "error": f"文件缺失: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_admin(self):
        try:
            body = ADMIN_FILE.read_bytes()
        except Exception as e:
            return self._send_json({"ok": False, "error": f"管理页缺失: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):  # noqa: N802
        """HEAD 与 GET 走同一套路由：部分浏览器/代理会先 HEAD 探 favicon 与页面。"""
        path = self._path()
        if path in ("/admin", "/favicon.ico", "/favicon.svg") or path.startswith("/api/"):
            try:
                return self._route_get()
            except (BrokenPipeError, ConnectionResetError):
                raise
            except Exception:
                try:
                    self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
                except Exception:
                    pass
                return
        return super().do_HEAD()

    # ---------------- 日志 ----------------

    def log_message(self, fmt, *args):  # noqa: A003
        # 静态资源刷屏无意义，只留 API / 管理页
        path = (args[0] if args else "") or ""
        if "/api/" in str(path) or "/admin" in str(path):
            sys.stderr.write("[web] %s %s\n" % (self.address_string(), fmt % args))


def serve(port: int = 8080, root: str = "/app/output") -> None:
    directory = str(Path(root).resolve())
    if not Path(directory).is_dir():
        raise SystemExit(f"目录不存在: {directory}")

    tok_src = newsnow_config.token_source()
    token = newsnow_config.admin_token(create=True)
    print(f"🌐 Web 服务已启动：http://0.0.0.0:{port}")
    print(f"  📁 服务目录: {directory}")
    print(f"  🛠  后台管理: http://127.0.0.1:{port}/admin")
    print(f"  🔑 管理员口令来源: {tok_src}")
    if token and "环境变量" not in tok_src:
        print(f"  🔑 管理员口令: {token}  (保存在 {newsnow_config.token_path()})")
    print(f"  💾 全局配置: {newsnow_config.config_path()}")

    handler = partial(Handler, directory=directory)
    httpd = ThreadingHTTPServer(("0.0.0.0", int(port)), handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    port = int(argv[0]) if argv else int(os.environ.get("WEBSERVER_PORT", "8080"))
    root = argv[1] if len(argv) > 1 else os.environ.get("WEBSERVER_DIR", "/app/output")
    serve(port, root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
