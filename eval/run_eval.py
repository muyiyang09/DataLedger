#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
DataLedger · 评测入口。

【用法】

    # 自检：用标准答案当"模型答案"跑一遍。必须拿满 21/21。
    python eval/run_eval.py --engine gold

    # 真实基线：让大模型来写 SQL（Stage 1 Step 2，需要 API key）
    python eval/run_eval.py --engine llm

    # 只跑某几条 / 某一类
    python eval/run_eval.py --engine gold --only Q001,Q017
    python eval/run_eval.py --engine gold --category 比率统计
    python eval/run_eval.py --engine gold --trap 10

【产物】
    控制台表格 + eval/reports/eval-<engine>-<时间戳>.{json,md}

【退出码】
    0  正常跑完
    1  未达 --fail-under 指定的通过率
    2  评测框架自身有问题（harness）—— 这一条必须在 CI 里当硬失败，
       因为它意味着"这一轮结果不可信"，而不是"模型考差了"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
for _path in (str(SCRIPT_DIR), str(PROJECT_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import psycopg  # noqa: E402
import yaml  # noqa: E402

from _common import fmt_value, is_time_sensitive, pad, rate_bar, trap_glyph  # noqa: E402
from app import db  # noqa: E402
from app.config import Settings  # noqa: E402
from app.engines import build_engine  # noqa: E402
from app.execute.grader import DEFAULT_SET_DECIMALS, grade  # noqa: E402
from app.execute.result import QueryResult  # noqa: E402
from app.execute.runner import execute_sql  # noqa: E402


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="DataLedger 评测入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--engine", default="gold", help="SQL 来源：gold | llm")
    parser.add_argument(
        "--schema-mode",
        default="raw",
        choices=["raw", "comments", "full"],
        help=(
            "给模型的 schema 详略（只对 llm 引擎有效）："
            "raw=只有表名列名类型（诚实基线）｜comments=加列注释（剥掉[陷阱N]标记）｜"
            "full=再加行数"
        ),
    )
    parser.add_argument("--cases", default=str(PROJECT_ROOT / "eval" / "cases.yaml"))
    parser.add_argument("--only", default="", help="只跑指定用例 id，逗号分隔")
    parser.add_argument("--category", default="", help="只跑指定分类（模糊匹配）")
    parser.add_argument("--difficulty", default="", help="只跑指定难度")
    parser.add_argument("--trap", type=int, default=None, help="只跑踩中指定陷阱编号的用例")
    parser.add_argument(
        "--set-decimals",
        type=int,
        default=DEFAULT_SET_DECIMALS,
        help="unordered_set 比对时数值保留的小数位",
    )
    parser.add_argument("--out-dir", default=str(PROJECT_ROOT / "eval" / "reports"))
    parser.add_argument("--no-report", action="store_true", help="不落盘报告文件")
    parser.add_argument("--quiet", action="store_true", help="只打最终汇总")
    parser.add_argument(
        "--fail-under",
        type=float,
        default=None,
        help="通过率低于该值（0~1）时以退出码 1 结束，供 CI 使用",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# 加载与筛选
# ---------------------------------------------------------------------------


def load_cases(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"[x] 找不到用例文件：{path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "cases" not in data:
        raise SystemExit(f"[x] 用例文件格式不对，缺少顶层 cases 列表：{path}")
    cases = data["cases"]
    if not isinstance(cases, list) or not cases:
        raise SystemExit(f"[x] 用例文件里 cases 为空：{path}")
    return cases


def filter_cases(cases: list[dict], args: argparse.Namespace) -> list[dict]:
    selected = cases

    if args.only:
        wanted = {token.strip() for token in args.only.split(",") if token.strip()}
        selected = [c for c in selected if str(c.get("id")) in wanted]
        missing = wanted - {str(c.get("id")) for c in selected}
        if missing:
            raise SystemExit(f"[x] --only 里有不存在的用例：{sorted(missing)}")

    if args.category:
        selected = [c for c in selected if args.category in str(c.get("category", ""))]

    if args.difficulty:
        selected = [c for c in selected if args.difficulty == str(c.get("difficulty", ""))]

    if args.trap is not None:
        selected = [c for c in selected if args.trap in (c.get("traps") or [])]

    if not selected:
        raise SystemExit("[x] 筛选之后没有剩下任何用例")
    return selected


# ---------------------------------------------------------------------------
# 跑一轮
# ---------------------------------------------------------------------------


def run_case(conn: psycopg.Connection, case: dict, engine, settings: Settings, set_decimals: int):
    """跑一条用例：gold 一次、模型一次、判分一次。"""
    gold_result = execute_sql(conn, str(case.get("gold_sql", "")), max_rows=settings.max_rows)

    generation = engine.generate(case)
    if generation.ok:
        model_result = execute_sql(conn, generation.sql, max_rows=settings.max_rows)
    else:
        model_result = QueryResult(sql=generation.sql, error=generation.error or "生成失败")

    verdict = grade(case, gold_result, model_result, set_decimals=set_decimals)
    return verdict, generation


def collect_llm_usage(generations: list) -> dict | None:
    """
    汇总 token / 延迟 / 成本。

    这些数字是 README 指标表里"单次问答成本 < ¥0.05""P95 延迟 < 8s"两行的来源
    —— 没有它们，那两行永远只能写"待测"。
    """
    metas = [g.meta for g in generations if getattr(g, "meta", None)]
    if not metas:
        return None

    tokens_in = sum(int(m.get("prompt_tokens") or 0) for m in metas)
    tokens_out = sum(int(m.get("completion_tokens") or 0) for m in metas)
    cost = sum(float(m.get("cost_cny") or 0.0) for m in metas)

    latencies = sorted(
        float(m["latency_ms"]) for m in metas if m.get("latency_ms") is not None
    )

    def percentile(p: float) -> float | None:
        if not latencies:
            return None
        index = min(len(latencies) - 1, max(0, round((len(latencies) - 1) * p)))
        return round(latencies[index], 1)

    return {
        "calls": len(metas),
        "model": next((m.get("model") for m in metas if m.get("model")), None),
        "schema_mode": next(
            (m.get("schema_mode") for m in metas if m.get("schema_mode")), None
        ),
        "prompt_tokens": tokens_in,
        "completion_tokens": tokens_out,
        "total_tokens": tokens_in + tokens_out,
        "cost_cny": round(cost, 4),
        "cost_cny_per_call": round(cost / len(metas), 6) if metas else None,
        "latency_ms_p50": percentile(0.5),
        "latency_ms_p95": percentile(0.95),
    }


def summarize(verdicts: list, cases: list[dict], engine_name: str, elapsed_s: float) -> dict:
    total = len(verdicts)
    passed = sum(1 for v in verdicts if v.passed)
    exec_ok = sum(1 for v in verdicts if v.exec_ok)
    harness = [v for v in verdicts if v.harness]
    warned = [v for v in verdicts if v.warnings and v.passed]

    by_category: dict[str, dict[str, int]] = {}
    by_check: dict[str, dict[str, int]] = {}
    by_trap: dict[int, dict[str, int]] = {}
    by_difficulty: dict[str, dict[str, int]] = {}

    case_by_id = {str(c.get("id")): c for c in cases}

    def bump(bucket: dict, key) -> None:
        slot = bucket.setdefault(key, {"total": 0, "passed": 0, "exec_ok": 0})
        slot["total"] += 1

    for verdict in verdicts:
        case = case_by_id.get(verdict.case_id, {})
        bump(by_category, str(case.get("category", "?")))
        bump(by_check, verdict.check)
        bump(by_difficulty, str(case.get("difficulty", "?")))
        for trap in case.get("traps") or []:
            bump(by_trap, int(trap))

        for bucket, key in (
            (by_category, str(case.get("category", "?"))),
            (by_check, verdict.check),
            (by_difficulty, str(case.get("difficulty", "?"))),
        ):
            bucket[key]["passed"] += int(verdict.passed)
            bucket[key]["exec_ok"] += int(verdict.exec_ok)
        for trap in case.get("traps") or []:
            by_trap[int(trap)]["passed"] += int(verdict.passed)
            by_trap[int(trap)]["exec_ok"] += int(verdict.exec_ok)

    return {
        "engine": engine_name,
        "elapsed_s": round(elapsed_s, 3),
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "exec_ok": exec_ok,
        "exec_rate": round(exec_ok / total, 4) if total else 0.0,
        "harness_issue_count": len(harness),
        "warned_but_passed": len(warned),
        "time_sensitive_cases": [
            str(c.get("id")) for c in cases if is_time_sensitive(str(c.get("gold_sql", "")))
        ],
        "by_category": by_category,
        "by_check": by_check,
        "by_difficulty": by_difficulty,
        "by_trap": dict(sorted(by_trap.items())),
    }


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def print_case_line(index: int, total: int, verdict, case: dict, quiet: bool) -> None:
    if quiet:
        return
    mark = "PASS" if verdict.passed else ("HARN" if verdict.harness else "FAIL")
    traps = case.get("traps") or []
    trap_text = "".join(trap_glyph(int(t)) for t in traps) or "-"
    line = (
        f"[{index:>2}/{total}] {mark} "
        f"{pad(verdict.case_id, 6)}"
        f"{pad(str(case.get('category', '?')), 10)}"
        f"{pad(str(case.get('difficulty', '?')), 8)}"
        f"{pad(verdict.check, 15)}"
        f"trap={pad(trap_text, 8)}"
        f"{(verdict.model.elapsed_ms if verdict.model else 0):>8.1f}ms  "
        f"{verdict.reason}"
    )
    print(line, flush=True)


def print_summary(summary: dict, verdicts: list) -> None:
    total = summary["total"]
    print()
    print("=" * 100)
    print(
        f"引擎 {summary['engine']} ｜ 用例 {total} ｜ "
        f"通过 {summary['passed']}/{total} （{summary['pass_rate'] * 100:.1f}%） ｜ "
        f"SQL 可执行 {summary['exec_ok']}/{total} （{summary['exec_rate'] * 100:.1f}%） ｜ "
        f"耗时 {summary['elapsed_s']:.2f}s"
    )
    print("=" * 100)

    usage = summary.get("llm_usage")
    if usage:
        print("\n-- 模型用量（成本与延迟指标的来源）--")
        print(f"  {pad('模型', 18)} {usage['model']}（schema={usage['schema_mode']}）")
        print(f"  {pad('调用次数', 18)} {usage['calls']}")
        print(
            f"  {pad('token 合计', 18)} {usage['total_tokens']:,}"
            f"（入 {usage['prompt_tokens']:,} / 出 {usage['completion_tokens']:,}）"
        )
        print(
            f"  {pad('成本合计', 18)} ¥{usage['cost_cny']}"
            f"（单次 ¥{usage['cost_cny_per_call']}）"
        )
        print(
            f"  {pad('延迟 P50/P95', 18)} "
            f"{usage['latency_ms_p50']} ms / {usage['latency_ms_p95']} ms"
        )

    def table(title: str, bucket: dict, key_fmt=str) -> None:
        if not bucket:
            return
        print(f"\n-- {title} " + "-" * (90 - len(title)))
        for key, slot in bucket.items():
            n, ok = slot["total"], slot["passed"]
            rate = ok / n * 100 if n else 0.0
            print(
                f"  {pad(key_fmt(key), 20)} {ok:>2}/{n:<2} {rate:>6.1f}%  {rate_bar(rate)}"
            )

    table("按分类", summary["by_category"])
    table("按检查方式", summary["by_check"])
    table("按难度", summary["by_difficulty"])
    table("按陷阱（一个用例可能踩多个坑）", summary["by_trap"], key_fmt=trap_glyph)

    if summary["time_sensitive_cases"]:
        print(
            "\n-- 时间敏感用例（答案随运行日期变化，复现不同属正常）--\n  "
            + ", ".join(summary["time_sensitive_cases"])
        )

    # 标准答案速查：单值用例把 gold 的值打出来，方便与指标日志对账
    scalar_rows = [
        (v.case_id, v.gold.scalar())
        for v in verdicts
        if v.gold and v.gold.ok and v.gold.is_single_scalar
    ]
    if scalar_rows:
        print("\n-- 标准答案速查（单值用例）--")
        for case_id, value in scalar_rows:
            print(f"  {pad(case_id, 6)} {fmt_value(value)}")

    harness = [v for v in verdicts if v.harness]
    if harness:
        print("\n!! 评测框架自身的问题（必须修，否则本轮结果不可信）")
        for v in harness:
            print(f"  {v.case_id}: {v.reason}")

    warned = [v for v in verdicts if v.warnings and v.passed]
    if warned:
        print("\n-- 通过但有告警（格式宽松处，可见即可信）--")
        for v in warned:
            for message in v.warnings:
                print(f"  {v.case_id}: {message}")

    failed = [v for v in verdicts if not v.passed]
    if failed:
        print(f"\n-- 未通过明细（{len(failed)} 条）--")
        for v in failed:
            print(f"  {v.case_id}: {v.reason}")


def build_markdown(summary: dict, verdicts: list, cases: list[dict], meta: dict) -> str:
    case_by_id = {str(c.get("id")): c for c in cases}
    lines: list[str] = []
    lines.append(f"# DataLedger 评测报告 · {summary['engine']} 引擎")
    lines.append("")
    lines.append(f"- 运行时间：{meta['run_at']}")
    lines.append(f"- 数据集：{meta.get('dataset_seed')}（用例集版本 {meta.get('version')}）")
    lines.append(f"- 用例数：{summary['total']} ｜ 耗时：{summary['elapsed_s']:.2f}s")
    lines.append(
        f"- **通过率：{summary['passed']}/{summary['total']} "
        f"（{summary['pass_rate'] * 100:.1f}%）**"
    )
    lines.append(
        f"- **首次可执行率：{summary['exec_ok']}/{summary['total']} "
        f"（{summary['exec_rate'] * 100:.1f}%）**"
    )
    if summary["harness_issue_count"]:
        lines.append(f"- ⚠️ 评测框架问题：{summary['harness_issue_count']} 条（见文末）")
    lines.append("")

    usage = summary.get("llm_usage")
    if usage:
        lines.append("## 模型用量")
        lines.append("")
        lines.append(f"- 模型：`{usage['model']}`（schema 详略：{usage['schema_mode']}）")
        lines.append(f"- 调用次数：{usage['calls']}")
        lines.append(
            f"- token 合计：{usage['total_tokens']:,}"
            f"（入 {usage['prompt_tokens']:,} / 出 {usage['completion_tokens']:,}）"
        )
        lines.append(
            f"- 成本合计：¥{usage['cost_cny']}（**单次 ¥{usage['cost_cny_per_call']}**）"
        )
        lines.append(
            f"- 延迟：P50 {usage['latency_ms_p50']} ms ｜ P95 {usage['latency_ms_p95']} ms"
        )
        lines.append("")

    lines.append("## 用例明细")
    lines.append("")
    lines.append("| 用例 | 分类 | 难度 | 检查 | 陷阱 | 结果 | 说明 |")
    lines.append("|---|---|---|---|---|---|---|")
    for v in verdicts:
        case = case_by_id.get(v.case_id, {})
        traps = "".join(trap_glyph(int(t)) for t in (case.get("traps") or [])) or "-"
        mark = "通过" if v.passed else ("框架问题" if v.harness else "未通过")
        reason = v.reason.replace("|", "\\|")
        lines.append(
            f"| {v.case_id} | {case.get('category', '?')} | {case.get('difficulty', '?')} "
            f"| {v.check} | {traps} | {mark} | {reason} |"
        )
    lines.append("")

    def bucket_table(title: str, bucket: dict, key_fmt=str) -> None:
        if not bucket:
            return
        lines.append(f"## {title}")
        lines.append("")
        lines.append("| 维度 | 通过 | 总数 | 通过率 |")
        lines.append("|---|---|---|---|")
        for key, slot in bucket.items():
            n, ok = slot["total"], slot["passed"]
            rate = ok / n * 100 if n else 0.0
            lines.append(f"| {key_fmt(key)} | {ok} | {n} | {rate:.1f}% |")
        lines.append("")

    bucket_table("按分类", summary["by_category"])
    bucket_table("按检查方式", summary["by_check"])
    bucket_table("按难度", summary["by_difficulty"])
    bucket_table("按陷阱", summary["by_trap"], key_fmt=trap_glyph)

    if summary["time_sensitive_cases"]:
        lines.append("## 时间敏感用例")
        lines.append("")
        lines.append(
            "以下用例的标准答案依赖 `CURRENT_DATE`，会随运行日期变化，"
            "复现时数值不同属正常：" + ", ".join(summary["time_sensitive_cases"])
        )
        lines.append("")

    harness = [v for v in verdicts if v.harness]
    if harness:
        lines.append("## ⚠️ 评测框架自身的问题")
        lines.append("")
        lines.append("这些不是模型的问题，是评测集或判分器的问题，必须先修：")
        lines.append("")
        for v in harness:
            lines.append(f"- `{v.case_id}`：{v.reason}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认可能是 GBK，中文/符号会直接抛 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):  # pragma: no cover
        pass

    args = parse_args(argv)
    cases_path = Path(args.cases)
    cases_all = load_cases(cases_path)
    cases = filter_cases(cases_all, args)
    settings = Settings.load()
    engine = build_engine(args.engine, settings=settings, schema_mode=args.schema_mode)

    # 引擎就绪自检：配置不全就当场说清。否则会跑出 21 条"SQL 执行失败"的假结果，
    # 看起来像"模型很差"，实际是没配 key —— 这种数字比没有数字更坏。
    probe = getattr(engine, "check_ready", None)
    if callable(probe):
        problem = probe()
        if problem:
            print(f"[x] {args.engine} 引擎未就绪：\n    {problem}")
            return 2

    try:
        with db.connect(settings) as conn:
            if not db.ping(conn):
                print("[!] 连上了，但 orders 表没有数据。先跑 db/seed.py 造数。")
            print(
                f"引擎={engine.name} ｜ 用例={len(cases)}/{len(cases_all)} ｜ "
                f"set_decimals={args.set_decimals}"
            )
            print("-" * 100)

            started = time.perf_counter()
            verdicts: list = []
            generations: list = []
            for index, case in enumerate(cases, start=1):
                verdict, generation = run_case(
                    conn, case, engine, settings, args.set_decimals
                )
                verdicts.append(verdict)
                generations.append(generation)
                print_case_line(index, len(cases), verdict, case, args.quiet)

            elapsed = time.perf_counter() - started
    except psycopg.OperationalError as exc:
        print(
            "[x] 与 PostgreSQL 的连接不可用。\n"
            f"    错误：{' '.join(str(exc).split())}\n"
            "    排查：netstat -an | findstr 5432 —— 没有 LISTENING 说明服务没起。"
        )
        return 2

    summary = summarize(verdicts, cases, engine.name, elapsed)
    summary["llm_usage"] = collect_llm_usage(generations)
    print_summary(summary, verdicts)

    run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    source_meta = {}
    with open(cases_path, encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
        if isinstance(loaded, dict):
            source_meta = loaded.get("meta") or {}

    if not args.no_report:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        stem = f"eval-{engine.name}-{stamp}"
        payload = {
            "summary": summary,
            "cases_filter": {
                "only": args.only,
                "category": args.category,
                "difficulty": args.difficulty,
                "trap": args.trap,
            },
            "verdicts": [v.as_dict() for v in verdicts],
        }
        json_path = out_dir / f"{stem}.json"
        md_path = out_dir / f"{stem}.md"
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        md_path.write_text(
            build_markdown(summary, verdicts, cases, {"run_at": run_at, **source_meta}),
            encoding="utf-8",
        )
        print(f"\n报告已写入：\n  {json_path}\n  {md_path}")

    if summary["harness_issue_count"]:
        print("\n[x] 评测框架自身有问题，本轮结果不可信。退出码 2。")
        return 2

    if args.fail_under is not None and summary["pass_rate"] < args.fail_under:
        print(
            f"\n[x] 通过率 {summary['pass_rate'] * 100:.1f}% "
            f"低于门槛 {args.fail_under * 100:.1f}%。退出码 1。"
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
