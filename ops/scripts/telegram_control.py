"""Telegram two-way control bot — helixa Hermes 的 3O 替代(更强:双向 + 门纪律)。

helixa 的 Hermes 只做单向告警;这里做**双向对话控制**:
  查询   /status /consensus /regime /engines /weights
  控制   /pause [原因]  /resume   (真实软熔断:halt 新开仓,平仓不挡)

关键设计(诚实 + 安全):
  - **只**响应授权 chat_id(TG_CHAT_ID);其它一律忽略——机器人能暂停交易,不能开放。
  - 读取走 gateway HTTP(只读、已脱敏)。
  - 控制走 `docker exec <paper> python -m paper.risk trip/reset`——在 paper 容器内写
    它自己 gate_entry 真正读的 kill-switch 文件(容器 /tmp 不共享,所以**不能**用
    gateway 的 /risk/kill,那个写在 gateway 容器里、paper 根本读不到)。subprocess 用
    列表参数、非 shell,原因串不可注入。
  - enforce 模式(observe→enforce)是进程启动读的 env,不能热切;因此只**报告**、不
    在此切换——切 enforce 是需人工放行 + 重启的独立一步(与 observe 纪律一致)。
  - 启动时 offset 跳过历史 backlog,避免重启重放旧 /pause。

用法: python ops/scripts/telegram_control.py   (systemd 常驻,Restart=always)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s telegram_control: %(message)s"
)
log = logging.getLogger("telegram_control")

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TG_CHAT_ID", "")  # 唯一授权对话
GATEWAY_URL = os.environ.get("HELIVEX_GATEWAY_URL", "http://127.0.0.1:8765").rstrip("/")
PAPER_CONTAINER = os.environ.get("HELIVEX_PAPER_CONTAINER", "helivex-paper")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
POLL_TIMEOUT = 30

HELP = (
    "helivex 控制台(3O)。observe-only,除 /pause /resume 外均为只读。\n"
    "查询:\n"
    "  /status — 风控/熔断/回撤/当日盈亏/enforce 模式\n"
    "  /consensus — 多引擎共识(只 promoted 驱动)\n"
    "  /regime — 市场状态\n"
    "  /engines — 各引擎信号\n"
    "  /weights — 引擎 EWMA 权重\n"
    "控制:\n"
    "  /pause [原因] — 软熔断:挡新开仓(平仓不挡)\n"
    "  /resume — 解除软熔断\n"
    "  /help — 本菜单"
)


# ── HTTP helpers (stdlib only) ──────────────────────────────────────────────
def _get_json(url: str, *, timeout: int = 15) -> dict | list | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log.warning("GET %s failed: %s", url, e)
        return None


def _gw(path: str) -> dict | list | None:
    return _get_json(f"{GATEWAY_URL}{path}")


def tg_send(text: str) -> None:
    try:
        data = urllib.parse.urlencode(
            {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": "true"}
        ).encode()
        urllib.request.urlopen(f"{API}/sendMessage", data=data, timeout=15).read()
    except Exception as e:
        log.warning("sendMessage failed: %s", e)


# ── control (docker exec into the paper container's own kill-switch) ─────────
def _paper_risk(*args: str) -> tuple[bool, str]:
    """Run `python -m paper.risk <args>` INSIDE the paper container. List args,
    no shell — the TG-supplied reason string cannot inject."""
    cmd = ["docker", "exec", PAPER_CONTAINER, "python", "-m", "paper.risk", *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode == 0, out.strip()
    except Exception as e:
        return False, f"exec error: {e}"


def _enforce_modes() -> dict[str, str]:
    """Read the paper node's enforce env (default observe). Reported only —
    switching enforce needs a deliberate, human-gated restart."""
    out = {}
    for k in ("HELIVEX_CONSENSUS_ENFORCE", "HELIVEX_DYNAMIC_RISK_ENFORCE"):
        try:
            p = subprocess.run(
                ["docker", "exec", PAPER_CONTAINER, "printenv", k],
                capture_output=True,
                text=True,
                timeout=15,
            )
            out[k] = (
                p.stdout.strip()
                if p.returncode == 0 and p.stdout.strip()
                else "observe"
            )
        except Exception:
            out[k] = "?"
    return out


# ── command handlers ────────────────────────────────────────────────────────
def cmd_status() -> str:
    s = _gw("/risk/status")
    if not isinstance(s, dict):
        return "⚠️ 网关无响应,无法取风控状态。"
    ks = s.get("kill_switch", {})
    enf = _enforce_modes()
    ks_line = (
        f"🛑 已熔断:{ks.get('reason') or '—'}"
        if ks.get("tripped")
        else "✅ 熔断:clear(允许开仓)"
    )
    caps = s.get("caps", {})
    return (
        f"{ks_line}\n"
        f"NAV {s.get('nav')}  峰值 {s.get('peak')}  回撤 {s.get('drawdown_pct')}% "
        f"(上限 {caps.get('max_drawdown_pct')}%)\n"
        f"当日已实现 {s.get('realized_today')}  (限 {caps.get('daily_loss_limit_usd')})  "
        f"累计 {s.get('realized_all')}\n"
        f"上限:gross {caps.get('portfolio_gross_usd')} | 每策略 {caps.get('per_strategy_usd')} | "
        f"每标的 {caps.get('per_instrument_usd')} | 最大持仓 {caps.get('max_positions')}\n"
        f"enforce:共识={enf['HELIVEX_CONSENSUS_ENFORCE']} 动态风控={enf['HELIVEX_DYNAMIC_RISK_ENFORCE']} "
        f"(observe=仅记录不下单)"
    )


def cmd_consensus() -> str:
    d = _gw("/consensus")
    rows = d.get("consensus", d.get("signals", [])) if isinstance(d, dict) else []
    if not rows:
        return "暂无共识数据。"
    out = ["共识(只 promoted 引擎驱动执行):"]
    for r in rows[:6]:
        ex = "✅可执行" if r.get("should_execute") else "观察"
        out.append(
            f"  {r.get('instrument')}: {r.get('final_direction')} "
            f"score={_fmt(r.get('consensus_score'))} promoted={r.get('n_promoted')} {ex}"
        )
    return "\n".join(out)


def cmd_regime() -> str:
    d = _gw("/regime")
    rows = d.get("regimes", d.get("regime", [])) if isinstance(d, dict) else []
    if isinstance(rows, dict):
        rows = [rows]
    if not rows:
        return "暂无市场状态数据。"
    return "市场状态:\n" + "\n".join(
        f"  {r.get('instrument')}: {r.get('state')} (conf={_fmt(r.get('confidence'))})"
        for r in rows[:6]
    )


def cmd_engines() -> str:
    d = _gw("/engines")
    rows = d.get("engines", []) if isinstance(d, dict) else []
    if not rows:
        return "暂无引擎信号。"
    out = ["引擎信号(promoted=过自身门):"]
    for r in rows[:12]:
        p = "✓" if r.get("promoted") else "·"
        out.append(
            f"  {p} {r.get('engine')}/{r.get('instrument')}: "
            f"{r.get('direction')} {_fmt(r.get('score'))}"
        )
    return "\n".join(out)


def cmd_weights() -> str:
    d = _gw("/engines/weights")
    rows = d.get("weights", []) if isinstance(d, dict) else []
    if not rows:
        return "暂无引擎权重(共识 adapter 尚未落数据)。"
    out = ["引擎权重(EWMA 学习,base→dyn):"]
    for r in rows:
        out.append(
            f"  {r.get('engine')}: {_fmt(r.get('base_weight'))}→{_fmt(r.get('dyn_weight'))} "
            f"(acc={_fmt(r.get('accuracy'))})"
        )
    return "\n".join(out)


def cmd_pause(reason: str) -> str:
    reason = (reason or "manual pause via Telegram").strip()[:200]
    ok, out = _paper_risk("trip", reason)
    log.warning("CONTROL /pause reason=%r ok=%s", reason, ok)
    if not ok:
        return f"⚠️ 暂停失败:{out[:300]}"
    verify = _paper_risk("status")[1]
    tripped = (
        "kill-switch : tripped" in verify
        or "已熔断" in verify
        or "tripped" in verify.lower()
    )
    return f"🛑 已暂停(软熔断,挡新开仓)。原因:{reason}\n{_first_lines(verify, 2)}"


def cmd_resume() -> str:
    ok, out = _paper_risk("reset")
    log.warning("CONTROL /resume ok=%s", ok)
    if not ok:
        return f"⚠️ 恢复失败:{out[:300]}"
    verify = _paper_risk("status")[1]
    return f"✅ 已恢复(解除软熔断,允许开仓)。\n{_first_lines(verify, 2)}"


# ── formatting ──────────────────────────────────────────────────────────────
def _fmt(v: object) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):+.3f}"
    except (TypeError, ValueError):
        return str(v)


def _first_lines(s: str, n: int) -> str:
    return "\n".join(s.splitlines()[:n])


def handle(text: str) -> str:
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower().split("@")[0]  # strip @botname
    arg = parts[1] if len(parts) > 1 else ""
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/status":
        return cmd_status()
    if cmd == "/consensus":
        return cmd_consensus()
    if cmd == "/regime":
        return cmd_regime()
    if cmd == "/engines":
        return cmd_engines()
    if cmd == "/weights":
        return cmd_weights()
    if cmd == "/pause":
        return cmd_pause(arg)
    if cmd == "/resume":
        return cmd_resume()
    return f"未知指令 {cmd}。/help 看菜单。"


# ── main long-poll loop ─────────────────────────────────────────────────────
def main() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        log.error("TG_BOT_TOKEN / TG_CHAT_ID 未设置,退出。")
        return
    log.info(
        "telegram_control 启动;授权 chat_id=%s,paper 容器=%s", CHAT_ID, PAPER_CONTAINER
    )
    # 跳过历史 backlog,避免重启重放旧指令(尤其 /pause)
    offset = None
    seed = _get_json(f"{API}/getUpdates?offset=-1&timeout=0", timeout=15)
    if isinstance(seed, dict) and seed.get("result"):
        offset = seed["result"][-1]["update_id"] + 1
        log.info("跳过 backlog,起始 offset=%s", offset)

    while True:
        try:
            url = f"{API}/getUpdates?timeout={POLL_TIMEOUT}"
            if offset is not None:
                url += f"&offset={offset}"
            resp = _get_json(url, timeout=POLL_TIMEOUT + 10)
            if not isinstance(resp, dict) or not resp.get("ok"):
                time.sleep(3)
                continue
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                chat = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "")
                if chat != CHAT_ID:
                    log.warning("忽略未授权 chat_id=%s text=%r", chat, text[:40])
                    continue
                if not text.startswith("/"):
                    continue
                log.info("cmd from %s: %s", chat, text[:60])
                try:
                    tg_send(handle(text))
                except Exception as e:
                    log.exception("handle 失败")
                    tg_send(f"⚠️ 处理出错:{e}")
        except Exception:
            log.exception("poll 循环异常,3s 后重试")
            time.sleep(3)


if __name__ == "__main__":
    main()
