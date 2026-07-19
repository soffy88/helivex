"""Hyperliquid S3 node 归档 —— 有界成本探针。

目的:把"S3 贵不贵 / Phase 2 值不值得做"从猜测变成算术。唯一的未知是**归档每天多少
字节**;本脚本用几分钱量出来,而不是先下几百 GB 再后悔。

为什么必须探针而不是直接全量下:
  - 桶是 **requester-pays** —— 每一次 LIST/GET 都记在**你的** AWS 账上
  - 传输费在两种架构下差 1-2 个数量级(见末尾成本外推)
  - 归档真实大小无公开权威数字,只能实测

安全性(本脚本的硬约束):
  1. **默认 dry-run** —— 不加 --confirm 只打印将要执行的操作与预估花费,不发任何请求
  2. **字节硬上限** —— --max-mb(默认 64)超过即中止,防止手滑下走全量
  3. **只读** —— 只做 list_objects_v2 / get_object,绝不写/删
  4. 每步打印累计请求数与累计字节,随时可 Ctrl-C

用法:
    # 1) 先看它要干什么(不花钱、不需要凭证)
    python ops/scripts/hl_s3_probe.py

    # 2) 配好只读凭证后真跑(花费约几分钱)
    AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \
      python ops/scripts/hl_s3_probe.py --confirm --date 20260715

依赖:boto3(本机未装 → `./venv/bin/pip install boto3`)
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── S3 定价(us-east-1,2026-07;若 AWS 调价需同步更新)────────────────────
PRICE_GET_PER_1K = 0.0004  # $/1000 GET
PRICE_LIST_PER_1K = 0.005  # $/1000 LIST(list_objects_v2)
PRICE_EGRESS_PER_GB = 0.09  # $/GB 传输出到互联网(前 10TB/月)
PRICE_EGRESS_SAME_REGION = 0.0  # 同区 EC2 传输免费

# HL 归档的候选桶/前缀。官方文档口径可能变,故探针会逐个试并报告哪个可达。
CANDIDATES = [
    ("hl-mainnet-node-data", "node_fills/hourly/{date}/"),
    ("hl-mainnet-node-data", "node_fills/{date}/"),
    ("hyperliquid-archive", "node_fills/hourly/{date}/"),
]
REGION = "ap-northeast-1"  # HL 归档所在区(实测若不符,脚本会报 301 并提示正确区)


@dataclass
class Meter:
    """累计计费量,任何一步都能打印当前花了多少。"""

    list_calls: int = 0
    get_calls: int = 0
    bytes_down: int = 0
    notes: list[str] = field(default_factory=list)

    def cost_usd(self, same_region: bool = False) -> float:
        egress = 0.0 if same_region else self.bytes_down / 1e9 * PRICE_EGRESS_PER_GB
        return (
            self.list_calls / 1000 * PRICE_LIST_PER_1K
            + self.get_calls / 1000 * PRICE_GET_PER_1K
            + egress
        )

    def line(self) -> str:
        return (
            f"LIST={self.list_calls} GET={self.get_calls} "
            f"下载={self.bytes_down / 1e6:.2f}MB 累计花费≈${self.cost_usd():.4f}"
        )


def _extrapolate(bytes_per_day: float, days: int) -> dict:
    total_gb = bytes_per_day * days / 1e9
    return {
        "days": days,
        "total_gb": round(total_gb, 1),
        "方案A_本机下载_传输费_usd": round(total_gb * PRICE_EGRESS_PER_GB, 2),
        "方案B_同区EC2_传输费_usd": round(total_gb * PRICE_EGRESS_SAME_REGION, 2),
        "GET请求费_usd(按每天24文件)": round(days * 24 / 1000 * PRICE_GET_PER_1K, 4),
    }


def _plan(date: str, max_mb: int) -> None:
    print("=" * 74)
    print("DRY-RUN —— 以下操作【尚未执行】,不会产生任何 AWS 费用")
    print("=" * 74)
    print(f"1. 逐个试探候选前缀(每个 1 次 LIST,共 ≤{len(CANDIDATES)} 次):")
    for b, p in CANDIDATES:
        print(f"     s3://{b}/{p.format(date=date)}")
    print(f"2. 对可达的那个前缀:LIST 出 {date} 全天文件,统计文件数与总字节")
    print(f"3. 取其中【1 个】文件下载(硬上限 {max_mb} MB),量压缩比与爆仓行占比")
    print("4. 按每日字节数外推 30/90/365/全窗口 的两种架构成本")
    print()
    est_list = len(CANDIDATES) + 1
    print(f"预估花费:LIST≈{est_list} 次 = ${est_list / 1000 * PRICE_LIST_PER_1K:.5f}")
    print(
        f"          GET≈1 次 + ≤{max_mb}MB 传输 = "
        f"${1 / 1000 * PRICE_GET_PER_1K + max_mb / 1024 * PRICE_EGRESS_PER_GB:.4f}"
    )
    print(
        f"          合计 ≈ ${est_list / 1000 * PRICE_LIST_PER_1K + 1 / 1000 * PRICE_GET_PER_1K + max_mb / 1024 * PRICE_EGRESS_PER_GB:.4f}"
    )
    print()
    print(
        "确认后加 --confirm 真跑。需要环境变量 AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY"
    )
    print("(建议用一次性只读 IAM 用户,权限仅 s3:ListBucket + s3:GetObject)")


def _run(date: str, max_mb: int, out: Path) -> None:
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        print("缺 boto3 → ./venv/bin/pip install boto3", file=sys.stderr)
        sys.exit(2)

    s3 = boto3.client("s3", region_name=REGION)
    m = Meter()
    hit = None

    print("── 步骤 1:探测可达前缀 ──")
    for bucket, tmpl in CANDIDATES:
        prefix = tmpl.format(date=date)
        try:
            r = s3.list_objects_v2(
                Bucket=bucket, Prefix=prefix, MaxKeys=5, RequestPayer="requester"
            )
            m.list_calls += 1
            n = r.get("KeyCount", 0)
            print(f"  s3://{bucket}/{prefix} → KeyCount={n}  [{m.line()}]")
            if n:
                hit = (bucket, prefix)
                break
        except ClientError as e:
            m.list_calls += 1
            code = e.response.get("Error", {}).get("Code")
            msg = e.response.get("Error", {}).get("Message", "")[:70]
            print(f"  s3://{bucket}/{prefix} → {code}: {msg}")
            if code in ("PermanentRedirect", "AuthorizationHeaderMalformed"):
                print(f"     ↑ 区域不对。正确区可能在错误信息里,改脚本顶部 REGION 重试")

    if not hit:
        print(
            "\n❌ 所有候选前缀都不可达。可能原因:凭证无权限 / 桶名或路径已变 / 区域不对"
        )
        print(f"   已花费 ≈ ${m.cost_usd():.5f}")
        return

    bucket, prefix = hit
    print(f"\n── 步骤 2:LIST 全天 {date} ──")
    total_bytes, keys = 0, []
    tok = None
    while True:
        kw = dict(Bucket=bucket, Prefix=prefix, RequestPayer="requester")
        if tok:
            kw["ContinuationToken"] = tok
        r = s3.list_objects_v2(**kw)
        m.list_calls += 1
        for o in r.get("Contents", []):
            total_bytes += o["Size"]
            keys.append((o["Key"], o["Size"]))
        tok = r.get("NextContinuationToken")
        if not tok:
            break
    print(f"  文件数 {len(keys)}  总字节 {total_bytes / 1e6:.2f} MB  [{m.line()}]")
    if not keys:
        print("  该日无文件,换 --date 重试")
        return

    print(f"\n── 步骤 3:取 1 个文件量压缩比与爆仓占比(上限 {max_mb}MB)──")
    keys.sort(key=lambda kv: kv[1])
    pick = next((k for k in keys if k[1] <= max_mb * 1024 * 1024), None)
    sample = {}
    if pick is None:
        print(
            f"  ⚠ 全天最小文件 {keys[0][1] / 1e6:.1f}MB 已超 --max-mb {max_mb},跳过下载"
        )
        print("     (仍可用步骤 2 的字节数外推,只是拿不到爆仓行占比)")
    else:
        key, size = pick
        obj = s3.get_object(Bucket=bucket, Key=key, RequestPayer="requester")
        raw = obj["Body"].read()
        m.get_calls += 1
        m.bytes_down += len(raw)
        print(f"  {key}  压缩 {len(raw) / 1e6:.2f} MB  [{m.line()}]")

        text = None
        if key.endswith(".lz4"):
            try:
                import lz4.frame

                text = lz4.frame.decompress(raw).decode("utf-8", "replace")
            except ImportError:
                print("     (缺 lz4 → pip install lz4,跳过解压分析)")
            except Exception as e:
                print(f"     解压失败: {type(e).__name__}")
        else:
            text = raw.decode("utf-8", "replace")

        if text:
            lines = [ln for ln in text.splitlines() if ln.strip()]
            liq = 0
            for ln in lines[:200_000]:
                low = ln.lower()
                if '"liquidation"' in low or "liquidated" in low:
                    liq += 1
            sample = {
                "key": key,
                "compressed_bytes": len(raw),
                "uncompressed_bytes": len(text),
                "compression_ratio": round(len(text) / max(1, len(raw)), 2),
                "lines": len(lines),
                "liquidation_lines": liq,
                "liquidation_pct": round(100 * liq / max(1, len(lines)), 3),
            }
            print(
                f"  解压后 {len(text) / 1e6:.2f} MB(压缩比 {sample['compression_ratio']}×)"
                f"  行数 {len(lines):,}  含爆仓标记 {liq:,} 行 ({sample['liquidation_pct']}%)"
            )

    print("\n── 步骤 4:成本外推 ──")
    per_day = total_bytes
    print(f"  实测每日 {per_day / 1e6:.1f} MB(压缩态)")
    extrap = {d: _extrapolate(per_day, d) for d in (30, 90, 365, 1035)}
    for d, e in extrap.items():
        tag = "≈全窗口(2023-09起)" if d == 1035 else f"{d}天"
        print(
            f"  {tag:<20} {e['total_gb']:>8.1f} GB   "
            f"本机下载 ${e['方案A_本机下载_传输费_usd']:>8.2f}   "
            f"同区EC2 ${e['方案B_同区EC2_传输费_usd']:.2f}"
        )

    print(f"\n本次探针实际花费 ≈ ${m.cost_usd():.5f}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "date": date,
                "bucket": bucket,
                "prefix": prefix,
                "files": len(keys),
                "bytes_per_day": per_day,
                "sample": sample,
                "extrapolation": extrap,
                "probe_cost_usd": round(m.cost_usd(), 5),
                "pricing": {
                    "get_per_1k": PRICE_GET_PER_1K,
                    "list_per_1k": PRICE_LIST_PER_1K,
                    "egress_per_gb": PRICE_EGRESS_PER_GB,
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"结果 → {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="20260715", help="探测日期 YYYYMMDD")
    ap.add_argument("--max-mb", type=int, default=64, help="单文件下载硬上限 MB")
    ap.add_argument(
        "--confirm", action="store_true", help="真跑(产生 requester-pays 费用)"
    )
    ap.add_argument("--out", default="ops/reports/hl_s3_probe.json")
    a = ap.parse_args()
    if a.confirm:
        _run(a.date, a.max_mb, Path(a.out))
    else:
        _plan(a.date, a.max_mb)
