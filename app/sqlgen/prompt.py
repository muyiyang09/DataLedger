# -*- coding: utf-8 -*-
"""
提示词组装。

【这个文件为什么刻意保持"笨"】

第一次跑模型，要的是**诚实的基线**：如果这里把口径规则、常见错误、
"记得排除 cancelled"都写进提示词，跑出来的 90% 准确率是假的 ——
它衡量的是"我提示词写得多细"，而不是"系统能不能处理口径歧义"。

所以这里的系统提示只约束**形式**（方言、只输出一条 SELECT、输出格式），
一句都不碰**业务口径**。口径要靠后面的语义层去解决，而且到时候
要拿"接语义层前 vs 后"的差值来说话。基线被虚高，后面的提升就讲不清了。

【语义层落地之后（2026-10-09）：口径仍然不进这个文件】

`semantic/metrics.yaml` 里的口径由 `app/semantic/` 渲染成一段文本，
**作为 user 消息里单独的一块**注入（见 `build_user_prompt`）。
系统提示一个字没改 —— 这是上面那条纪律的兑现，不是巧合。

【唯一必须写进提示词的，是"别做危险动作"】

不是指望它守规矩（提示词约束是可被绕过的，这点在 README 里已经写明），
而是因为在基线上就引入"生成 DROP TABLE"的样本，会让失败原因的分布变脏，
看不出真正的口径问题。真正的拦截由 Stage 2 的护栏负责。
"""

from __future__ import annotations

from typing import Any

__all__ = ["SYSTEM_PROMPT", "build_messages", "build_user_prompt"]

SYSTEM_PROMPT = """你是一个 PostgreSQL 数据分析师。你的任务是把用户的业务问题写成一条 SQL。

硬性要求：
1. 只输出一条 SELECT 语句。不要输出 INSERT / UPDATE / DELETE / DROP 等任何写操作。
2. 使用 PostgreSQL 语法。不要使用 MySQL 专有写法（如反引号、LIMIT a,b、IFNULL、DATE_FORMAT）。
3. 只使用下面给出的表和列，不要臆造表名或列名。
4. 把 SQL 放进 ```sql 代码块里，不要输出解释、不要输出多个候选方案。
5. 如果问题里的条件不足以唯一确定一条 SQL，就选你认为最合理的一种写法，仍然只给一条。
"""


def build_user_prompt(
    question: str,
    schema_card: str,
    *,
    semantic_block: str = "",
) -> str:
    """
    把 schema、（可选的）业务口径、问句拼成用户消息。顺序固定，便于对比不同轮次的结果。

    【口径为什么放在 schema 之后、问句之前】

    - 放在 schema 之后：模型先看到有哪些字段，口径才好挂到具体字段上
      （「order_status 的 cancelled 不计入」比空说「取消单不算」更有用）。
    - 放在问句之前：先给规则、再看题，避免它先按直觉猜一遍再读规则。

    【semantic_block 为空时，输出与历史版本逐字节一致】
    这不是洁癖：在无语义层的对照档里，输入必须和开语义层之前完全相同，
    否则比出来的差值里混着"提示词结构变了"的影响，归因就不干净了。
    """
    parts = [
        "数据库结构：\n",
        f"{schema_card}\n\n",
    ]
    if semantic_block:
        parts.append(f"{semantic_block}\n\n")
    parts.append(f"用户问题：{question.strip()}\n\n请给出 SQL。")
    return "".join(parts)


def build_messages(
    question: str,
    schema_card: str,
    *,
    system_prompt: str = SYSTEM_PROMPT,
    semantic_block: str = "",
) -> list[dict[str, Any]]:
    """
    组装成 OpenAI 兼容的 messages 结构。

    ⚠️ `semantic_block` **只进 user 消息，绝不进 system 消息**。
    系统提示必须保持"只约束形式、一句业务口径都不碰"的状态 ——
    一旦口径进了系统提示，"加语义层前 vs 后"的对照就废了。
    这条约束由 `tests/test_semantic.py` 断言守住。
    """
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": build_user_prompt(question, schema_card, semantic_block=semantic_block),
        },
    ]
