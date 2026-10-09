# -*- coding: utf-8 -*-
"""
配对比较：判断"改动有没有用"，而不是"哪边百分比高"。

【这个脚本为什么必须存在 —— 它是被一次真实的翻车逼出来的】

2026-10-09 第一次跑三档 schema，拿到 `raw` 5/21、`comments` 5/21、`full` 7/21，
于是写下两条结论：「加列注释一个用例都没多对」「最简的单表聚合错得最狠」。

**复跑之后两条都站不住。** 同一份数据、同一份用例、`temperature=0`，
三轮的 `raw` 是 5 / 7 / 5，`comments` 是 5 / 6 / 7，`full` 是 7 / 6 / 7。
跑次之间的波动是 **±2 条用例（±9.5 个百分点）**，比要比较的档间差异还大。

也就是说：**21 条用例上，两个整体百分比之差，大部分时候量的是噪声。**

【配对比较为什么能救回来】

同一个比例（比如 5/21 → 7/21）有两种问法：

    ✗ 差法：「基线 23.8%，改动后 33.3%，提升 9.5 个点」——这 9.5 个点里，
            有多少是改动带来的、有多少是这次抽样的运气？分不清。

    ✓ 配对法：「改动前错、改动后对的用例有 2 条；改动前对、改动后错的 0 条」
            ——比的是**同一条用例在两边**的表现，用例难度这部分方差被消掉了。

而且配对法会顺手把结果分成三档，比一个百分比信息量大得多：
确定变好（0 → 全对）、确定变坏（全对 → 0）、**不稳定**（两边都时对时错）。
第三档才是真相暴露的地方：**如果一条用例在两组里都是忽对忽错，那它根本没被改动影响。**

【"倾向"这一档怎么定出来的 —— 它是被负对照逼出来的】

只分五档还是太钝：一条用例要"某组 3 跑全错 → 另一组 3 跑全对"才算改进，
门槛高到几乎什么都测不出来。所以加一档"倾向"，给方向、不给结论。

但门槛不是拍脑袋定的。**先把脚本拿两组同配置的报告跑一遍（负对照），
看它会不会把噪声误报成效果** —— 结果它真误报了一条：

    Q014  A组 0/3（0%）  B组 1/3（33%）  → 若门槛是"一边全错就算倾向变好"，会误报

所以门槛收紧为 **一边 0 次通过、另一边至少过半通过**：

    倾向变好：某组的通过率恰为 0%，另一组 ≥ 50%
    倾向变差：反过来

按这个门槛重跑负对照，两组同配置时误报 0 条 —— 这条负对照已固化成单测
`tests/test_compare_runs.py::test_identical_config_groups_produce_no_effect`。
**换门槛就必须重跑那条断言。**

【按陷阱分解，而不是只看一个整体百分比】

整体通过率回答不了"这条改动动到了谁"。`cases.yaml` 里每条用例都标了 `traps`，
所以结论区会按陷阱分组列出改动的落点。

**但这张表本身会骗人**：一条用例常踩好几个坑（Q013 踩 ②⑥⑨），
所以"陷阱②这一行有 1 条确定变好"并不表示类目的口径起了作用 ——
它可能只是**因为⑥变好的用例恰好也踩了②**。

于是再加一层 `--covered-trap`：声明"这次改动只覆盖了哪个坑"，
脚本就把用例按"踩不踩它"切成两半，并要求
**不踩它的那批一条都不许变**。范围内变好、范围外纹丝不动，归因才成立。

【用法】

    python eval/compare_runs.py \\
        --baseline  eval/reports/基线1.json eval/reports/基线2.json eval/reports/基线3.json \\
        --treatment eval/reports/语义1.json eval/reports/语义2.json eval/reports/语义3.json \\
        --covered-trap 6        # 语义层只写了取消单（陷阱⑥）

    # 只看踩中陷阱⑥的用例
    python eval/compare_runs.py --baseline ... --treatment ... --focus-trap 6

每组给多份报告（同一配置的重复跑），才能区分"确定的差异"和"跑次噪声"。
只给一份也能跑，但脚本会在结论里明确提醒：此时无法区分噪声。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 一条用例在某一组里的状态
STABLE_PASS = "稳定通过"
STABLE_FAIL = "稳定失败"
IMPROVED = "由错变对"
REGRESSED = "由对变错"
UNSTABLE = "不稳定"
HARNESS = "被截断/框架问题"
# 方向性提示档：给方向，不给结论。门槛见模块 docstring（由负对照定出）。
TEND_IMPROVE = "倾向变好（样本不足）"
TEND_REGRESS = "倾向变差（样本不足）"

# "倾向"档的门槛：一边恰为 0%，另一边至少这么高
TEND_MIN_OTHER = 0.5


def load_report(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"[x] 找不到报告：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def case_pass_map(report: dict) -> dict[str, bool | None]:
    """
    {case_id: 是否通过}；None 表示这条是评测框架问题（例如被 max_tokens 截断），
    不参与比较 —— 拿它去比较等于把配置缺陷算进改动效果里。
    """
    result: dict[str, bool | None] = {}
    for verdict in report.get("verdicts") or []:
        cid = str(verdict.get("case_id"))
        result[cid] = None if verdict.get("harness_issue") else bool(verdict.get("passed"))
    return result


def group_rates(paths: list[Path]) -> tuple[dict[str, float], dict[str, int], dict]:
    """
    把一组的若干份报告合成 {case_id: 通过率}。

    同时返回每份报告的整体通过率、以及 harness 条数 —— 后者用来提醒"这次比较干净吗"。
    """
    maps = [case_pass_map(load_report(p)) for p in paths]
    totals = [len(m) for m in maps]
    if len(set(totals)) > 1:
        raise SystemExit(
            f"[x] 同一组的报告用例数不一致：{totals}。"
            "不同筛选条件下的报告不能放在一起比。"
        )

    case_ids = sorted(maps[0].keys())
    rates: dict[str, float] = {}
    effective: dict[str, int] = {}
    for cid in case_ids:
        values = [m[cid] for m in maps if m[cid] is not None]
        effective[cid] = len(values)
        rates[cid] = (sum(1 for v in values if v) / len(values)) if values else 0.0

    meta = {
        "runs": len(paths),
        "overall": [
            sum(1 for v in m.values() if v) / len(m) for m in maps
        ],
        "harness": [
            sum(1 for v in m.values() if v is None) for m in maps
        ],
        "paths": [p.name for p in paths],
    }
    return rates, effective, meta


def classify(a_rate: float, b_rate: float) -> str:
    """
    判定一条用例在两组之间的变化。

    顺序很重要：先判"确定"（两端都到极值），再判"倾向"（一端到极值、另一端过半），
    剩下的才是"不稳定"。**绝不是先算差值再看大小** —— 那正是这个脚本要避免的错法。
    """
    # 确定：两组都到极值
    if a_rate == 0.0 and b_rate == 1.0:
        return IMPROVED
    if a_rate == 1.0 and b_rate == 0.0:
        return REGRESSED
    if a_rate == 1.0 and b_rate == 1.0:
        return STABLE_PASS
    if a_rate == 0.0 and b_rate == 0.0:
        return STABLE_FAIL
    # 倾向：一端到极值、另一端过半（门槛由负对照定出，见模块 docstring）
    if a_rate == 0.0 and b_rate >= TEND_MIN_OTHER:
        return TEND_IMPROVE
    if b_rate == 0.0 and a_rate >= TEND_MIN_OTHER:
        return TEND_REGRESS
    return UNSTABLE


def load_case_meta(
    cases_path: Path,
) -> tuple[dict[str, str], dict[str, list[int]], dict[str, str]]:
    """从 cases.yaml 读 {id: 问句} / {id: 踩中的陷阱} / {id: 比对方式}。缺文件时全为空。"""
    questions: dict[str, str] = {}
    traps: dict[str, list[int]] = {}
    checks: dict[str, str] = {}
    if not cases_path.exists():
        return questions, traps, checks
    import yaml

    for case in yaml.safe_load(cases_path.read_text(encoding="utf-8"))["cases"]:
        cid = str(case["id"])
        questions[cid] = str(case.get("question", ""))
        traps[cid] = [int(t) for t in (case.get("traps") or [])]
        checks[cid] = str(case.get("check", ""))
    return questions, traps, checks


def compare(
    baseline: list[Path],
    treatment: list[Path],
    cases_path: Path,
    label_a: str,
    label_b: str,
    focus_trap: int | None = None,
    covered_trap: int | None = None,
) -> int:
    a_rates, _a_runs, a_meta = group_rates(baseline)
    b_rates, _b_runs, b_meta = group_rates(treatment)

    if set(a_rates) != set(b_rates):
        raise SystemExit("[x] 两组的用例 id 不一致，不能配对比较。")

    questions, traps, checks = load_case_meta(cases_path)

    rows = []
    for cid in sorted(a_rates):
        rows.append((cid, a_rates[cid], b_rates[cid], classify(a_rates[cid], b_rates[cid])))

    buckets: dict[str, list[tuple]] = {}
    for row in rows:
        buckets.setdefault(row[3], []).append(row)

    print("=" * 90)
    print(f"配对比较：{label_a}  vs  {label_b}")
    print("=" * 90)
    print(f"  {label_a:>12}：{a_meta['runs']} 份报告  整体通过率 "
          + " / ".join(f"{r * 100:.1f}%" for r in a_meta["overall"]))
    print(f"  {label_b:>12}：{b_meta['runs']} 份报告  整体通过率 "
          + " / ".join(f"{r * 100:.1f}%" for r in b_meta["overall"]))
    if a_meta["harness"] or b_meta["harness"]:
        print(f"  ⚠️ 框架问题（已排除，不参与比较）：{label_a} {a_meta['harness']}  ｜ "
              f"{label_b} {b_meta['harness']}")
    if a_meta["runs"] < 2 or b_meta["runs"] < 2:
        print("  ⚠️ 某一组只有 1 份报告 —— 此时无法区分「确定的差异」和「跑次噪声」，"
              "下面的结论只能当线索，不能当结论。")

    shown = rows
    if focus_trap is not None:
        shown = [r for r in rows if focus_trap in traps.get(r[0], [])]
        print()
        print(f"  ▶ 聚焦陷阱 {focus_trap}：只统计踩中它的 {len(shown)} / {len(rows)} 条用例")

    print()
    print(f"{'用例':<7}{label_a:>10}{label_b:>10}   判定")
    print("-" * 90)
    for cid, a_rate, b_rate, verdict in shown:
        mark = ""
        if traps.get(cid):
            mark = "  [陷阱 " + ",".join(f"{t:02d}" for t in traps[cid]) + "]"
        print(f"{cid:<7}{a_rate * 100:>9.0f}%{b_rate * 100:>9.0f}%   {verdict}{mark}")

    # 聚焦时结论只统计子集 —— 否则会把子集的结论写成全量的
    scope = {r[0] for r in shown}
    def _b(name: str) -> list[tuple]:
        return [r for r in buckets.get(name, []) if r[0] in scope]

    improved, regressed = _b(IMPROVED), _b(REGRESSED)
    tend_good, tend_bad = _b(TEND_IMPROVE), _b(TEND_REGRESS)
    unstable = _b(UNSTABLE)

    print()
    print("=" * 90)
    print("结论" + (f"（范围：陷阱 {focus_trap} 的 {len(shown)} 条用例）" if focus_trap is not None else ""))
    print("=" * 90)
    print(f"  ★ 由错变对（确定）：{len(improved)} 条  " + (", ".join(r[0] for r in improved) or "—"))
    print(f"  ★ 由对变错（确定）：{len(regressed)} 条  " + (", ".join(r[0] for r in regressed) or "—"))
    if tend_good or tend_bad:
        print(f"  ~ 倾向变好（样本不足，不能当结论）：{len(tend_good)} 条  "
              + (", ".join(r[0] for r in tend_good) or "—"))
        print(f"  ~ 倾向变差（样本不足，不能当结论）：{len(tend_bad)} 条  "
              + (", ".join(r[0] for r in tend_bad) or "—"))
    print(f"    一直对：{len(_b(STABLE_PASS))} 条 ｜ "
          f"一直错：{len(_b(STABLE_FAIL))} 条")
    print(f"    不稳定（两边都忽对忽错，改动对它们没有可分辨的影响）：{len(unstable)} 条  "
          + (", ".join(r[0] for r in unstable) or "—"))

    touched = len(improved) + len(regressed)
    print()
    print(f"  净变化：{touched} 条用例的表现被改动**确定地**区分开了"
          f"（+{len(improved)} / −{len(regressed)}）")
    if tend_good or tend_bad:
        print(f"  另有 {len(tend_good) + len(tend_bad)} 条只有倾向 —— 加跑几轮再看它们能不能变成确定。")
    if unstable:
        print(f"  还有 {len(unstable)} 条在两组里都不稳定 —— 这些用例的结论"
              "要等样本量上来（Stage 5 扩到 300 条）才谈得上。")

    if not touched and not (tend_good or tend_bad):
        print()
        print("  ⚠️ 没有任何一条用例被区分开（连倾向都没有）。这意味着：在当前的样本量和跑次下，")
        print("     这次改动**没有可观测的效果** —— 而这不是「效果很小」，是「测不出来」。")
        print("     提升样本量（Stage 5）或增大改动幅度，都比重跑一次更有意义。")

    # 「Q002 变好了」对读者没有信息量 —— 把问句打出来，结论才可读、可复核
    changed = [r for r in (improved + regressed) if questions.get(r[0])]
    if changed:
        print()
        print("这几条被改动区分开的用例，分别在问什么：")
        for cid, _a, _b_rate, verdict in changed:
            q = questions[cid]
            if len(q) > 46:
                q = q[:46] + "…"
            print(f"  {verdict}  {cid}  {q}")

    if traps and focus_trap is None:
        print()
        print_trap_breakdown(rows, traps, covered_trap=covered_trap)

    print()
    print_value_drift(baseline, treatment, rows, checks, label_a, label_b)

    return 0


def median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        raise ValueError("median of empty")
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def value_series(report: dict, case_id: str) -> list[float]:
    """把一份报告里某条用例的**模型答案数值**取出来（只看 approx 类）。"""
    out: list[float] = []
    for v in report.get("verdicts") or []:
        if str(v.get("case_id")) != case_id:
            continue
        det = v.get("detail") or {}
        raw = det.get("model_value")
        if raw is None:
            continue
        try:
            out.append(float(raw))
        except (TypeError, ValueError):
            continue
    return out


def tolerance_of(report: dict, case_id: str) -> float | None:
    for v in report.get("verdicts") or []:
        if str(v.get("case_id")) != case_id:
            continue
        det = v.get("detail") or {}
        tol = det.get("tolerance")
        if tol is not None:
            try:
                return float(tol)
            except (TypeError, ValueError):
                return None
    return None


def print_value_drift(
    baseline: list[Path],
    treatment: list[Path],
    rows: list[tuple],
    checks: dict[str, str],
    label_a: str,
    label_b: str,
) -> None:
    """
    数值层复核：判定翻转，到底是**答案变了**，还是**容差边缘的抖动**？

    【为什么必须有这一段 —— 它是被 Q012 逼出来的】

    2026-10-09 的对照实验里，Q012 在基线是 5 跑全对、语义层 5 跑只对 1，
    按判定层看是"100% → 20%"的疑似回归。但把**数值**拿出来看：

        基线   ：8.936 / 8.94    相对误差 9.75% ~ 9.79%   容差 10%  → 擦边过
        语义层 ：9.91（对）／ 8.91、8.906…（错）  误差 0.04% 或 10.06%

    两组的答案只差 0.3%，却分落在 10% 容差的两侧 —— **判定是被容差决定的，
    不是被内容决定的**。它甚至暴露了题目本身有歧义：8.9061985042464191
    正好等于 gold − 1，说明模型把「高出多少倍」读成了**增量**，
    而 gold 取的是**比值**，两种读法在中文里都立得住。

    如果只看判定层，这条会被写成"语义层引入了一个回归" —— 错的。
    判定层和数值层是两把尺子，**只拿判定层当验收标准，就会把容差抖动读成效果**。
    这个项目和尺子的旧账（七条 → 五项 → 八项 → 九项）就是这么欠下的。
    """
    approx_rows = [
        r for r in rows if r[3] in (IMPROVED, REGRESSED, TEND_IMPROVE, TEND_REGRESS, UNSTABLE)
    ]
    if not approx_rows:
        return

    a_reports = [load_report(p) for p in baseline]
    b_reports = [load_report(p) for p in treatment]

    lines: list[str] = []
    skipped: list[str] = []
    for cid, _a_rate, _b_rate, verdict in approx_rows:
        # 精确比对（exact）的用例**只在通过那几跑**才写 model_value，
        # 拿剩下的值算中位数等于只看成功样本 —— 系统性有偏，整条不算。
        # （这不是"数值不全"，是"取样规则本身有偏"，两回事。）
        if checks.get(cid) == "exact":
            skipped.append(f"{cid}(精确比对)")
            continue

        a_vals = [v for rep in a_reports for v in value_series(rep, cid)]
        b_vals = [v for rep in b_reports for v in value_series(rep, cid)]
        if not a_vals or not b_vals:
            skipped.append(f"{cid}(无数值)")
            continue

        tol = tolerance_of(a_reports[0], cid) or tolerance_of(b_reports[0], cid)
        a_med, b_med = median(a_vals), median(b_vals)
        denom = abs(a_med) if abs(a_med) > 1e-12 else 1.0
        drift = abs(b_med - a_med) / denom
        # 取值数：一眼看出"这组到底稳定给一个数，还是每次都不一样"。
        spread = f"{len({round(v, 6) for v in a_vals})}/{len({round(v, 6) for v in b_vals})}"
        # 覆盖不全时结论只能当线索 —— 但要**照常列出来**，
        # 因为最有价值的那条（Q012 判定翻转）恰好就落在覆盖不全上，
        # 剔掉它等于把最该看的东西藏起来。
        partial = len(a_vals) < len(a_reports) or len(b_vals) < len(b_reports)
        cover = f"  ⚠️ 覆盖 {len(a_vals)}/{len(a_reports)}·{len(b_vals)}/{len(b_reports)}" if partial else ""
        if tol is not None and drift < tol:
            note = "★ 容差边缘抖动"
        else:
            note = "数值确有变化"
        if partial:
            note += "（覆盖不全，仅作线索）"
        lines.append(
            f"  {cid:<7}{a_med:>14.4f}{b_med:>14.4f}{drift * 100:>9.2f}%"
            f"{(f'{tol * 100:.0f}%' if tol is not None else '—'):>8}{spread:>10}   {note}{cover}"
        )

    print("=" * 90)
    print("数值层复核：判定变了的用例，答案是**真的变了**还是**在容差边上抖**？")
    print("=" * 90)
    if not lines and not skipped:
        print("  （判定变了的用例里，没有可比的数值 —— 多半是结果集类比对，无法做这层复核）")
        return
    if lines:
        print(f"{'用例':<7}{label_a + ' 中位值':>14}{label_b + ' 中位值':>14}{'相对漂移':>10}{'容差':>8}{'取值数':>10}")
        print("-" * 90)
        for line in lines:
            print(line)
        print("-" * 90)
        print("  读法：相对漂移 **小于容差** 的那些（★），判定翻转只说明它正好骑在容差上 ——")
        print("        答案其实没动。把它算成回归或改进，都是在拿容差当效果。")
        print("        反过来，漂移远大于容差却仍然翻转的，才是真变化。")
        print("  「取值数」是「基线/语义层 各跑出了几个不同的值」：漂移却是 0% 而判定忽对忽错的，")
        print("    说明两边都给过同一个数、只是另几跑给了别的 —— 那是模型在两种读法间摇摆。")
    if skipped:
        print(f"  未纳入：{', '.join(skipped)}")
        print("    （精确比对类只在通过时才记数值，样本天生有偏；无数值类的没东西可比）")


def print_trap_breakdown(
    rows: list[tuple], traps: dict[str, list[int]], covered_trap: int | None = None
) -> None:
    """
    按陷阱列出改动的落点 —— 以及一个**归因检查**。

    【这张表自己会骗人，所以必须有下面那半段】

    一条用例通常踩好几个坑（Q013 踩 ②⑥⑨）。所以"陷阱②这一行有 1 条确定变好"
    并不表示类目的口径起了作用 —— 它可能只是**因为⑥变好的用例恰好也踩了②**。
    照字面读，这张表会把功劳安到根本没改过的口径上。

    于是加一段归因检查：一条改动只覆盖了 `covered_trap`（比如今晚只写了取消单＝⑥），
    就把用例按"踩不踩⑥"切成两半 ——

        踩⑥的用例允许变好；
        **不踩⑥的用例必须一条都不变。**

    后半句才是关键：不踩⑥的那批要是也变好了，说明要么改动越出了适用范围，
    要么这些"效果"根本是跑次噪声。两种情况都必须让人看见，不能混在总数里。
    """
    print("=" * 90)
    print("按陷阱分解（改动落在哪些坑上）")
    print("=" * 90)
    print("  ⚠️ 一条用例常踩多个坑，所以它会在多行里各被数一次 ——")
    print("     本表只回答「改动落在谁身上」，**不能**回答「是哪个坑带来的改动」。归因看下半段。")
    print()
    print(f"{'陷阱':<8}{'用例数':>7}{'确定变好':>10}{'确定变坏':>10}{'倾向变好':>10}{'倾向变差':>10}")
    print("-" * 90)

    all_traps = sorted({t for ts in traps.values() for t in ts})
    for trap in all_traps:
        mine = [r for r in rows if trap in traps.get(r[0], [])]
        if not mine:
            continue
        counts = {
            IMPROVED: sum(1 for r in mine if r[3] == IMPROVED),
            REGRESSED: sum(1 for r in mine if r[3] == REGRESSED),
            TEND_IMPROVE: sum(1 for r in mine if r[3] == TEND_IMPROVE),
            TEND_REGRESS: sum(1 for r in mine if r[3] == TEND_REGRESS),
        }
        print(f"{trap:>2} 号   {len(mine):>6}{counts[IMPROVED]:>10}{counts[REGRESSED]:>10}"
              f"{counts[TEND_IMPROVE]:>10}{counts[TEND_REGRESS]:>10}")

    if covered_trap is None:
        print("-" * 90)
        print("  提示：加 --covered-trap N 可以按「这次改动只覆盖了陷阱 N」做归因检查。")
        return

    with_trap = [r for r in rows if covered_trap in traps.get(r[0], [])]
    without = [r for r in rows if covered_trap not in traps.get(r[0], [])]

    def count(group: list[tuple], verdict: str) -> int:
        return sum(1 for r in group if r[3] == verdict)

    print("-" * 90)
    print()
    print("=" * 90)
    print(f"归因检查：这次改动只写了陷阱 {covered_trap} 号的口径")
    print("=" * 90)
    print(f"  踩 {covered_trap} 号：{len(with_trap)} 条用例 ｜ "
          f"确定变好 {count(with_trap, IMPROVED)} ／ 确定变坏 {count(with_trap, REGRESSED)} ｜ "
          f"倾向变好 {count(with_trap, TEND_IMPROVE)} ／ 倾向变差 {count(with_trap, TEND_REGRESS)} ｜ "
          f"不稳定 {count(with_trap, UNSTABLE)}")
    print(f"  不踩 {covered_trap} 号：{len(without)} 条用例 ｜ "
          f"确定变好 {count(without, IMPROVED)} ／ 确定变坏 {count(without, REGRESSED)} ｜ "
          f"倾向变好 {count(without, TEND_IMPROVE)} ／ 倾向变差 {count(without, TEND_REGRESS)} ｜ "
          f"不稳定 {count(without, UNSTABLE)}")

    if not without:
        print()
        print(f"  ⚠️ 用例集里没有「不踩 {covered_trap} 号」的用例 —— 这条归因检查在本用例集上")
        print("     **无法证伪**（没有对照组）。它现在是「通过」，但通过得没有信息量：")
        print(f"     要想真的用它，得先补几条不踩 {covered_trap} 号的用例。")
        return

    leaked = (
        count(without, IMPROVED)
        + count(without, REGRESSED)
        + count(without, TEND_IMPROVE)
        + count(without, TEND_REGRESS)
    )
    print()
    if leaked:
        print(f"  ✗ 范围外有 {leaked} 条发生变化 —— **归因不成立**。")
        print(f"    这些用例不踩 {covered_trap}，口径管不到它们，变化只可能来自噪声或越界。"
              "加跑几轮再看，或检查口径的适用范围是不是写宽了。")
    else:
        print(f"  ✓ 范围外没有一条被判定为变化（确定与倾向都为 0）—— 归因成立。")
        print("    注意「不稳定」那一列：范围外就算出现抖动，只要它时对时错、")
        print("    够不到确定的门槛，就不算被改动影响。真正的证据在范围内的 7 条上。")
        print("    （这比「整体涨了多少」强：它同时排除了「别的坑也在悄悄变好」这种解释。）")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="配对比较两组评测报告（比同一条用例的前后变化，不比两个整体百分比）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--baseline", nargs="+", required=True, help="基线组的报告 JSON（可多份）")
    parser.add_argument("--treatment", nargs="+", required=True, help="改动组的报告 JSON（可多份）")
    parser.add_argument("--label-a", default="基线", help="基线组显示名")
    parser.add_argument("--label-b", default="改动后", help="改动组显示名")
    parser.add_argument("--cases", default=str(PROJECT_ROOT / "eval" / "cases.yaml"))
    parser.add_argument(
        "--focus-trap",
        type=int,
        default=None,
        help="只统计踩中指定陷阱编号的用例（例：--focus-trap 6 看取消单口径的落点）",
    )
    parser.add_argument(
        "--covered-trap",
        type=int,
        default=None,
        help=(
            "这次改动**只**覆盖了哪一个陷阱，用于归因检查："
            "不踩它的用例必须一条都不变（例：语义层只写了取消单 → --covered-trap 6）"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return compare(
        [Path(p) for p in args.baseline],
        [Path(p) for p in args.treatment],
        Path(args.cases),
        args.label_a,
        args.label_b,
        focus_trap=args.focus_trap,
        covered_trap=args.covered_trap,
    )


if __name__ == "__main__":
    raise SystemExit(main())
