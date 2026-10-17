#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NewsNow 视图的全局配置（后台管理）。

设计要点：
- 配置落在「可写状态目录」而非 config/（容器里 config 是只读挂载）：
    NEWSNOW_STATE_DIR > /app/state > <仓库根>/state
- 只有 defaults / sourcesOrder / lock 三块，schema 完全白名单校验，
  防止后台表单把脏数据写进页面。
- 管理员 Token：优先环境变量 NN_ADMIN_TOKEN，其次状态目录下
  admin_token.txt（首次自动生成，权限 600）。没有 Token 时写接口关闭。
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

# 每项：允许的取值（第一个是默认值）
ENUMS: Dict[str, List[str]] = {
    "theme": ["auto", "light", "dark"],
    "density": ["normal", "compact", "loose"],
    "font": ["normal", "large"],
    "card": ["tint", "white", "full"],
    "bg": ["default", "gradient", "bing"],
    "alpha": ["solid", "high", "mid", "low"],
    "radius": ["none", "sm", "md", "lg"],
    "srcSort": ["count", "name", "custom"],
    "hotwords": ["off", "5", "10", "20"],
    "scrim": ["medium", "light", "deep"],
}

DEFAULT_PREFS: Dict[str, str] = {k: v[0] for k, v in ENUMS.items()}

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 1,
    "updated_at": "",
    "defaults": dict(DEFAULT_PREFS),
    "sourcesOrder": [],
    "lock": {"mode": "soft", "keys": []},
}

LOCK_MODES = ["soft", "hard"]

_MAX_ORDER = 500
_config_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def state_dir() -> Path:
    """可写状态目录（容器里由 compose 挂载到宿主，重启不丢）。"""
    env = (os.environ.get("NEWSNOW_STATE_DIR") or "").strip()
    if env:
        return Path(env)
    # 容器环境：/app/config 与 /app/output 一定存在
    if Path("/app/config").exists() or Path("/app/output").exists():
        return Path("/app/state")
    # 仓库根 / state（本地开发）
    return Path(__file__).resolve().parents[2] / "state"


def config_path() -> Path:
    return state_dir() / "newsnow.json"


def token_path() -> Path:
    return state_dir() / "admin_token.txt"


def _ensure_dir() -> Path:
    d = state_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _clean_prefs(raw: Any) -> Dict[str, str]:
    out = dict(DEFAULT_PREFS)
    if isinstance(raw, dict):
        for k, allowed in ENUMS.items():
            v = raw.get(k)
            if isinstance(v, str) and v in allowed:
                out[k] = v
            elif isinstance(v, (int, float)) and str(v) in allowed:
                out[k] = str(v)
    return out


def _clean_order(raw: Any) -> List[str]:
    out: List[str] = []
    seen = set()
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, str):
                continue
            name = item.strip()[:60]
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(name)
            if len(out) >= _MAX_ORDER:
                break
    return out


def _clean_lock(raw: Any) -> Dict[str, Any]:
    mode = "soft"
    keys: List[str] = []
    if isinstance(raw, dict):
        m = raw.get("mode")
        if isinstance(m, str) and m in LOCK_MODES:
            mode = m
        ks = raw.get("keys")
        if isinstance(ks, list):
            for k in ks:
                if isinstance(k, str) and k in ENUMS and k not in keys:
                    keys.append(k)
    return {"mode": mode, "keys": keys}


def clean_config(raw: Any) -> Dict[str, Any]:
    """把任意输入收敛成合法配置（永不抛异常）。"""
    data = raw if isinstance(raw, dict) else {}
    return {
        "version": 1,
        "updated_at": str(data.get("updated_at") or "")[:32],
        "defaults": _clean_prefs(data.get("defaults")),
        "sourcesOrder": _clean_order(data.get("sourcesOrder")),
        "lock": _clean_lock(data.get("lock")),
    }


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------

def load() -> Dict[str, Any]:
    path = config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return clean_config(data)
    except FileNotFoundError:
        return clean_config(DEFAULT_CONFIG)
    except Exception:
        # 文件损坏不影响出报，回落到默认值
        return clean_config(DEFAULT_CONFIG)


def save(raw: Any) -> Dict[str, Any]:
    """校验后原子写回，返回落盘后的配置。"""
    cfg = clean_config(raw)
    cfg["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _ensure_dir()
    path = config_path()
    with _config_lock:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".newsnow-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
        try:
            os.chmod(path, 0o644)
        except Exception:
            pass
    return cfg


def public_view() -> Dict[str, Any]:
    """注入页面 / 提供给前端接口的视图（不含任何密钥）。"""
    cfg = load()
    view = {
        "defaults": cfg["defaults"],
        "sourcesOrder": cfg["sourcesOrder"],
        "lock": cfg["lock"],
        "updated_at": cfg["updated_at"],
    }
    return view


# ---------------------------------------------------------------------------
# 管理员令牌
# ---------------------------------------------------------------------------

def admin_token(create: bool = True) -> str:
    """当前有效 Token；环境变量优先，否则读/建状态目录文件。"""
    env = (os.environ.get("NN_ADMIN_TOKEN") or "").strip()
    if env:
        return env
    p = token_path()
    try:
        if p.exists():
            tok = p.read_text(encoding="utf-8").strip()
            if tok:
                return tok
        if not create:
            return ""
        _ensure_dir()
        tok = secrets.token_urlsafe(24)
        p.write_text(tok + "\n", encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
        return tok
    except Exception:
        return ""


def token_source() -> str:
    """Token 来源说明，用于后台页提示。"""
    if (os.environ.get("NN_ADMIN_TOKEN") or "").strip():
        return "环境变量 NN_ADMIN_TOKEN"
    if token_path().exists():
        return str(token_path())
    return "未初始化"


def check_token(given: Optional[str]) -> bool:
    expect = admin_token(create=False)
    if not expect or not given:
        return False
    return secrets.compare_digest(str(given), expect)


def writes_enabled() -> bool:
    return bool(admin_token(create=True))
