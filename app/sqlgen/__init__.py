# -*- coding: utf-8 -*-
"""
SQL 生成与语义层（Stage 1 Step 2 起逐步填充）。

    schema_card.py  把库结构渲染成模型能看的文本（三档详略）
    prompt.py       提示词组装（刻意不写业务口径，保证基线诚实）
    generator.py    调用大模型生成 SQL（不重试、temperature 0）
"""
