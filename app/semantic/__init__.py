# -*- coding: utf-8 -*-
"""
语义层：把「业务口径」渲染成能给模型看的一段文本。

【它为什么必须和 schema 卡片分开】

本项目有两次实验，很容易混成一次：

    schema 卡片（raw / comments / full）—— 量的是「**结构信息**值多少」
   语义层（本模块）            —— 量的是「**业务口径**值多少」

上一次实验的结论是：把结构写细（加列注释、加行数）几乎没用 —— 因为
「销售额该用哪个字段」「取消单算不算」这类问题，**答案不在 schema 里**，
再多列名也推不出来。

所以这两件事必须分开注入、分开度量。把它俩合起来，就再也讲不清是谁起的作用。

【三条硬约束（都由上一次的教训换来）】

1. **口径不进 SYSTEM_PROMPT。**
   系统提示只约束形式（方言、只输出一条 SELECT、输出格式）。一旦把口径写进去，
   基线就被虚高，后面「加了语义层提升多少」这笔账永远算不清。

2. **口径不混进 schema 的列注释。**
   `db/schema.sql` 的注释是给人和调试看的出题笔记，标记后面就是答案解析；
   `schema_card.clean_comment()` 会把这类注释整条丢掉。混在一起就是泄题。

3. **只渲染 `metrics`，绝不渲染 `backlog`。**
   口径字典里写了待补条目（给作者看的路线图），那部分含带答案的线索。
   渲染器有单测专门断言它不会漏进提示词。

所以本模块只做一件事：读 yaml → 把 metrics 渲染成一段文本。
它不拼 schema、不碰系统提示、不生成 SQL。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

__all__ = [
    "SEMANTIC_PATH",
    "load_metrics_doc",
    "load_metrics",
    "metric_count",
    "render_semantic_block",
    "build_semantic_block",
]

# 口径字典放在仓库顶层的 semantic/，和 db/、eval/ 并列 ——
# 它是**可评审、可 diff 的数据**，不是藏在代码里的常量。
SEMANTIC_PATH = Path(__file__).resolve().parents[2] / "semantic" / "metrics.yaml"

_HEADER = (
    "业务口径（生成 SQL 时必须遵守）：\n"
    "以下定义是业务方确认过的口径。当它和字段名给你的直觉不一致时，以口径为准。\n"
)

# yaml 的 `>` 折叠标量会把换行折成一个空格，于是中文句读后面会多出空格
# （"指标。 「取消」指…"）。喂给模型不影响正确性，但读起来像排版坏掉的文档 ——
# 而这种文档恰恰最容易让人（和模型）漏读关键那句。
_CJK_PUNCT = "。，、；：？！）」』】…—"


def _one_line(text: Any) -> str:
    """把多行 YAML 折成一行，并收掉中文标点后面多余的空白。"""
    collapsed = " ".join(str(text).split())
    for punct in _CJK_PUNCT:
        collapsed = collapsed.replace(f"{punct} ", punct)
    return collapsed


def load_metrics_doc(path: Path | str | None = None) -> dict[str, Any]:
    """读整份口径字典（含 version / metrics / backlog）。"""
    target = Path(path) if path is not None else SEMANTIC_PATH
    if not target.exists():
        raise FileNotFoundError(f"找不到口径字典：{target}")
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "metrics" not in data:
        raise ValueError(f"口径字典格式不对，缺少顶层 metrics：{target}")
    return data


def load_metrics(path: Path | str | None = None) -> list[dict[str, Any]]:
    """只取要注入的那部分：metrics 列表。"""
    metrics = load_metrics_doc(path).get("metrics") or []
    if not isinstance(metrics, list):
        raise ValueError("metrics 必须是列表")
    return metrics


def metric_count(path: Path | str | None = None) -> int:
    return len(load_metrics(path))


def _render_aliases(metric: Mapping[str, Any]) -> str:
    aliases = [str(a) for a in (metric.get("aliases") or [])]
    return " / ".join(aliases) if aliases else ""


def _render_status_semantics(semantics: Mapping[str, Any]) -> list[str]:
    lines: list[str] = [f"    字段取值语义（{semantics.get('field', '')}）："]
    for label, key in (("不计入", "excluded"), ("计入", "included")):
        for value, meaning in (semantics.get(key) or {}).items():
            lines.append(f"      {label} —— {value}：{_one_line(meaning)}")
    return lines


def render_metric(metric: Mapping[str, Any], index: int) -> str:
    lines: list[str] = [f"[{index}] {metric.get('name', metric.get('id', '?'))}"]
    aliases = _render_aliases(metric)
    if aliases:
        lines.append(f"    用户可能这么说：{aliases}")
    applies = [str(a) for a in (metric.get("applies_to") or [])]
    if applies:
        lines.append(f"    适用范围：{'；'.join(applies)}")
    if metric.get("definition"):
        lines.append(f"    定义：{_one_line(metric['definition'])}")
    semantics = metric.get("status_semantics")
    if isinstance(semantics, Mapping):
        lines.extend(_render_status_semantics(semantics))
    if metric.get("decision_rule"):
        lines.append(f"    判断依据：{_one_line(metric['decision_rule'])}")
    return "\n".join(lines)


def render_semantic_block(metrics: Sequence[Mapping[str, Any]]) -> str:
    """
    把 metrics 渲染成提示词里的一段。

    **只接受 metrics 列表**，不接受整份文档 —— 从签名上就堵住
    "不小心把 backlog 一起渲染出去"这条路。
    """
    if not metrics:
        return ""
    body = "\n\n".join(
        render_metric(metric, index) for index, metric in enumerate(metrics, start=1)
    )
    return _HEADER + body


def build_semantic_block(path: Path | str | None = None) -> str:
    """读文件 + 渲染，一步到位。开语义层时调用一次即可（结果可缓存）。"""
    return render_semantic_block(load_metrics(path))
