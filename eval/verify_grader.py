#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
DataLedger · 判分器"牙齿"验证。

【为什么需要这个脚本】

`run_eval.py --engine gold` 拿到 21/21，只说明判分器**不会误杀正确答案**。
一个永远返回"通过"的判分器同样能做到这件事。

所以这里反向验证：拿一批"**能执行、但答案确实错**"的 SQL 去撞判分器，
断言它们**全部被判为未通过**。

    有洞（HOLE）   —— 错误 SQL 被判通过。判分器失效，整套评测数字作废。
    负面集自身坏（BROKEN）—— 该错误 SQL 根本没跑起来。
                      那验的是执行器，不是判分器，必须修负面集。

只要 HOLE 或 BROKEN 不为 0，退出码就是 2 —— 这个脚本适合直接挂进 CI。

用法:
    python eval/verify_grader.py
    python eval/verify_grader.py --only N021
    python eval/verify_grader.py --show-sql
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
for _path in (str(SCRIPT_DIR), str(PROJECT_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import psycopg  # noqa: E402
import yaml  # noqa: E402

from _common import fmt_value, pad, rate_bar, trap_glyph  # noqa: E402
from app import db  # noqa: E402
from app.config import Settings  # noqa: E402
from app.execute.grader import DEFAULT_SET_DECIMALS, grade  # noqa: E402
from app.execute.result import QueryResult, is_number, to_decimal  # noqa: E402
from app.execute.runner import execute_sql  # noqa: E402

REJECTED = "REJECTED"
HOLE = "HOLE"
BROKEN = "BROKEN"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="验证判分器会拒绝错误答案")
    parser.add_argument("--cases", default=str(SCRIPT_DIR / "cases.yaml"))
    parser.add_argument("--negatives", default=str(SCRIPT_DIR / "negatives.yaml"))
    parser.add_argument("--only", default="", help="只跑指定负面用例，逗号分隔")
    parser.add_argument("--set-decimals", type=int, default=DEFAULT_SET_DECIMALS)
    parser.add_argument("--show-sql", action="store_true", help="打印每条错误 SQL")
    return parser.parse_args(argv)


def load_yaml(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"[x] 找不到文件：{path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"[x] 文件格式不对：{path}")
    return data


def deviation(gold: QueryResult, model: QueryResult) -> str:
    """
    量化"错得有多离谱"。

    这一列很重要：如果某条错误 SQL 的偏离幅度只有 0.001%，
    那它被判通过对判分器是"宽容"，被判不通过对判分器是"过拟合噪声"，
    两种解释都不健康 —— 说明这条负面用例本身设计得不好。
    """
    if not gold.ok or not model.ok:
        return "-"
    if gold.nrows == 1 and gold.ncols == 1 and model.nrows == 1 and model.ncols == 1:
        gold_value, model_value = gold.scalar(), model.scalar()
        if is_number(gold_value) and is_number(model_value):
            gold_dec, model_dec = to_decimal(gold_value), to_decimal(model_value)
            if gold_dec == 0:
                return "gold=0" if model_dec != 0 else "0%"
            gap = abs(model_dec - gold_dec) / abs(gold_dec) * 100
            return f"{gap:.2f}%"
        return "类型不同"
    return f"行数 {gold.nrows}→{model.nrows}"


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):  # pragma: no cover
        pass

    args = parse_args(argv)
    cases = load_yaml(Path(args.cases)).get("cases") or []
    negative_doc = load_yaml(Path(args.negatives))
    negatives = negative_doc.get("negatives") or []
    if not negatives:
        raise SystemExit("[x] 负面集为空，这个脚本就失去意义了")

    if args.only:
        wanted = {t.strip() for t in args.only.split(",") if t.strip()}
        negatives = [n for n in negatives if str(n.get("id")) in wanted]
        if not negatives:
            raise SystemExit(f"[x] --only 没有匹配到任何负面用例：{sorted(wanted)}")

    case_by_id = {str(c.get("id")): c for c in cases}
    settings = Settings.load()
    results: list[dict] = []

    try:
        with db.connect(settings) as conn:
            if not db.ping(conn):
                print("[!] orders 表没有数据，先在库上跑一遍造数")

            print(
                f"负面用例 {len(negatives)} 条 ｜ 目标：全部被判为未通过 ｜ "
                f"set_decimals={args.set_decimals}"
            )
            print("-" * 100)

            for negative in negatives:
                neg_id = str(negative.get("id"))
                case_id = str(negative.get("case_id"))
                case = case_by_id.get(case_id)
                if case is None:
                    results.append(
                        {
                            "id": neg_id,
                            "case_id": case_id,
                            "status": BROKEN,
                            "note": f"cases.yaml 里没有用例 {case_id}",
                        }
                    )
                    continue

                gold_result = execute_sql(
                    conn, str(case.get("gold_sql", "")), max_rows=settings.max_rows
                )
                wrong_result = execute_sql(
                    conn, str(negative.get("sql", "")), max_rows=settings.max_rows
                )
                verdict = grade(
                    case, gold_result, wrong_result, set_decimals=args.set_decimals
                )

                if not wrong_result.ok:
                    # 错误 SQL 根本跑不起来 —— 验的是执行器，不是判分器
                    status = BROKEN
                    note = f"错误 SQL 未执行成功：{wrong_result.error}"
                elif verdict.harness:
                    status = BROKEN
                    note = f"判分器把问题归到了框架一侧：{verdict.reason}"
                elif verdict.passed:
                    status = HOLE
                    note = f"错误答案被判通过！{verdict.reason}"
                else:
                    status = REJECTED
                    note = verdict.reason

                results.append(
                    {
                        "id": neg_id,
                        "case_id": case_id,
                        "status": status,
                        "why": negative.get("wrong", ""),
                        "traps": negative.get("traps") or [],
                        "deviation": deviation(gold_result, wrong_result),
                        "note": note,
                        "gold": fmt_value(gold_result.scalar())
                        if gold_result.is_single_scalar
                        else gold_result.preview(3),
                        "wrong_value": fmt_value(wrong_result.scalar())
                        if wrong_result.is_single_scalar
                        else wrong_result.preview(3),
                        "sql": str(negative.get("sql", "")).strip(),
                    }
                )

                mark = {REJECTED: "OK  ", HOLE: "洞! ", BROKEN: "坏! "}[status]
                traps = "".join(trap_glyph(t) for t in (negative.get("traps") or [])) or "-"
                print(
                    f"{mark} {pad(neg_id, 6)}{pad(case_id, 6)}"
                    f"trap={pad(traps, 8)}"
                    f"偏离={pad(results[-1]['deviation'], 10)}"
                    f"{results[-1]['why']}"
                )
                if status != REJECTED:
                    print(f"        └─ {note}")
                if args.show_sql:
                    print(
                        "        SQL: "
                        + " ".join(str(negative.get("sql", "")).split())[:150]
                    )

    except psycopg.OperationalError as exc:
        print(
            "[x] 与 PostgreSQL 的连接不可用。\n"
            f"    错误：{' '.join(str(exc).split())}\n"
            "    排查：netstat -an | findstr 5432 —— 没有 LISTENING 说明服务没起。"
        )
        return 2

    rejected = [r for r in results if r["status"] == REJECTED]
    holes = [r for r in results if r["status"] == HOLE]
    broken = [r for r in results if r["status"] == BROKEN]
    total = len(results)
    rate = len(rejected) / total * 100 if total else 0.0

    print()
    print("=" * 100)
    print(
        f"判分器拒绝率 {len(rejected)}/{total}（{rate:.1f}%）"
        f" ｜  洞 {len(holes)} ｜  负面集自身有问题 {len(broken)}"
    )
    print("=" * 100)
    print(f"  {pad('拒绝错误答案', 20)} {len(rejected):>3}  {rate_bar(rate)}")

    if holes:
        print("\n!! 判分器有洞（错误答案被判通过）—— 本轮评测的所有数字都不可信：")
        for r in holes:
            print(f"  {r['id']} ({r['case_id']}): {r['note']}")
    if broken:
        print("\n!! 负面集自身有问题（这些 SQL 没跑起来，验不到判分器）：")
        for r in broken:
            print(f"  {r['id']} ({r['case_id']}): {r['note']}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = SCRIPT_DIR / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"verify-grader-{stamp}.json"
    json_path.write_text(
        json.dumps(
            {
                "summary": {
                    "total": total,
                    "rejected": len(rejected),
                    "holes": len(holes),
                    "broken": len(broken),
                    "reject_rate": round(len(rejected) / total, 4) if total else 0.0,
                },
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n报告已写入：{json_path}")

    if holes or broken:
        return 2
    print("\n判分器通过了「牙齿」检验：所有错误答案都被拒绝。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
