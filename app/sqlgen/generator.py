# -*- coding: utf-8 -*-
"""
SQL 生成器：调用大模型，把问句 + schema 变成一条 SQL。

【三条刻意的设计】

一、**不做重试。**
    Step 2 要量的是"首次可执行率"—— 这个数字的意义就在于"一次机会"。
    一旦在这里默默重试，指标会变得很好听但毫无意义（重试 3 次谁都好看）。
    重试是 Stage 4 的独立课题，到时候要量的是"重试带来的提升"，
    所以两件事必须分开、各有各的数字。

二、**temperature 默认 0。**
    评测要可复现。同一批用例跑两次得到不同的准确率，就等于没有准确率。

三、**不引第三方 HTTP 库。**
    一个 POST 请求不值得为它加一个依赖，`urllib` 是标准库。
    顺带把 HTTP 调用抽成可注入的 `transport`，测试就不需要联网、不需要 key。

【模型名注意】
`deepseek-chat` / `deepseek-reasoner` 已于 2026-07-24 弃用，
当前可用的是 `deepseek-v4-flash` / `deepseek-v4-pro`。
这些都可以用环境变量覆盖，不写死在代码里。
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from app.config import load_dotenv
from app.engines import Generation
from app.sqlgen.prompt import SYSTEM_PROMPT, build_messages

__all__ = ["LlmConfig", "SqlGenerator", "extract_sql", "DEFAULT_SYSTEM_PROMPT"]

DEFAULT_SYSTEM_PROMPT = SYSTEM_PROMPT

# 带语言标注的围栏：```sql ... ``` / ```postgresql ... ```
_FENCED = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.S)
# 兜底：从第一行以 SELECT / WITH 开头的语句开始截取
_BARE_START = re.compile(r"^\s*(SELECT|WITH)\b", re.I | re.M)
_SQL_LANGS = {"", "sql", "postgresql", "postgres", "pgsql", "psql"}


def extract_sql(text: str) -> str:
    """
    从模型输出里抽出 SQL。

    模型极少"只输出 SQL" —— 常见形态有：带解释、带代码块语言标注、
    加了 ``` 但没写 sql、先来一句"好的，这是查询"。这里按优先级依次尝试，
    全都失败就把原文交出去（让执行器报一个真实的语法错误，
    而不是在这里伪造一个"解析失败"，那会污染失败原因的统计）。
    """
    if not text or not text.strip():
        return ""

    blocks = _FENCED.findall(text)
    if blocks:
        # 优先拿标了 sql 的块
        for lang, body in blocks:
            if lang.strip().lower() in _SQL_LANGS and body.strip():
                return body.strip()
        for _lang, body in blocks:
            if body.strip():
                return body.strip()

    match = _BARE_START.search(text)
    if match:
        return text[match.start() :].strip()

    return text.strip()


@dataclass(frozen=True)
class LlmConfig:
    """模型接入参数。全部可用环境变量覆盖，代码里不写死任何凭据。"""

    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    temperature: float = 0.0
    max_tokens: int = 1024
    timeout_s: float = 60.0
    # 价格随官方调整，所以可配；默认值取自 2026-10 的 flash 档
    price_in_per_mtok: float = 0.15
    price_out_per_mtok: float = 0.60
    usd_to_cny: float = 7.1

    @classmethod
    def load(cls, env: dict[str, str] | None = None) -> "LlmConfig":
        load_dotenv()  # 幂等；确保 .env 里的键已进入 os.environ
        source = env if env is not None else os.environ

        def get(key: str, default: str) -> str:
            value = (source.get(key) or "").strip()
            return value or default

        return cls(
            api_key=get("DEEPSEEK_API_KEY", ""),
            base_url=get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            model=get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            temperature=float(get("DEEPSEEK_TEMPERATURE", "0")),
            max_tokens=int(get("DEEPSEEK_MAX_TOKENS", "1024")),
            timeout_s=float(get("DEEPSEEK_TIMEOUT_S", "60")),
            price_in_per_mtok=float(get("DEEPSEEK_PRICE_IN", "0.15")),
            price_out_per_mtok=float(get("DEEPSEEK_PRICE_OUT", "0.60")),
            usd_to_cny=float(get("USD_TO_CNY", "7.1")),
        )

    @property
    def chat_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def cost_cny(self, prompt_tokens: int, completion_tokens: int) -> float:
        usd = (
            prompt_tokens / 1_000_000 * self.price_in_per_mtok
            + completion_tokens / 1_000_000 * self.price_out_per_mtok
        )
        return usd * self.usd_to_cny


def _http_post_json(
    url: str, payload: dict, headers: dict[str, str], timeout: float
) -> dict:
    """默认传输实现：标准库 POST，返回解析后的 JSON。"""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


Transport = Callable[[str, dict, dict, float], dict]


class SqlGenerator:
    """
    一次生成 = 一次请求。不重试、不缓存、不改写模型输出。

    `transport` 可注入，测试时给一个假函数即可，不需要联网也不需要 key。
    """

    def __init__(
        self,
        config: LlmConfig,
        *,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self.system_prompt = system_prompt
        self._transport: Transport = transport or _http_post_json

    # -- 供评测前自检 ------------------------------------------------

    def check_ready(self) -> str | None:
        """返回 None 表示可以调用；否则返回一句人话说明缺什么。"""
        if not self.config.api_key:
            return (
                "缺少 DEEPSEEK_API_KEY。\n"
                "    1) 到 platform.deepseek.com 创建 API Key\n"
                "    2) 把它写进项目的 .env（该文件已在 .gitignore 里，不会入库）\n"
                "       DEEPSEEK_API_KEY=sk-xxxxxxxx\n"
                "    3) 模型名默认用 deepseek-v4-flash（deepseek-chat 已于 2026-07-24 弃用）"
            )
        return None

    def generate(self, question: str, schema_card: str) -> Generation:
        ready = self.check_ready()
        if ready:
            return Generation(error=ready)

        messages = build_messages(
            question, schema_card, system_prompt=self.system_prompt
        )
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        }

        started = time.perf_counter()
        try:
            data = self._transport(
                self.config.chat_url, payload, headers, self.config.timeout_s
            )
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                raw = exc.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw)
                detail = str(
                    (parsed.get("error") or {}).get("message") or parsed
                )[:300]
            except Exception:  # pragma: no cover - 响应体不是 JSON
                detail = ""
            hint = ""
            if exc.code == 401:
                hint = "（API key 无效或已过期）"
            elif exc.code == 402:
                hint = "（账户余额不足）"
            elif exc.code == 404:
                hint = (
                    f"（模型名 {self.config.model!r} 不存在？"
                    "deepseek-chat 已于 2026-07-24 弃用，改用 deepseek-v4-flash）"
                )
            elif exc.code == 429:
                hint = "（触发限流，稍后重试）"
            return Generation(
                error=f"HTTP {exc.code} {hint} {detail}".strip(),
                meta={"latency_ms": round((time.perf_counter() - started) * 1000, 1)},
            )
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            return Generation(
                error=f"网络错误：{exc}",
                meta={"latency_ms": round((time.perf_counter() - started) * 1000, 1)},
            )
        except Exception as exc:  # pragma: no cover - 兜底，不能中断整轮评测
            return Generation(
                error=f"{type(exc).__name__}: {exc}",
                meta={"latency_ms": round((time.perf_counter() - started) * 1000, 1)},
            )

        latency_ms = (time.perf_counter() - started) * 1000
        return self._parse_response(data, latency_ms)

    # -- 内部 -----------------------------------------------------------

    def _parse_response(self, data: dict, latency_ms: float) -> Generation:
        choices = data.get("choices") or []
        if not choices:
            return Generation(
                error=f"响应里没有 choices：{json.dumps(data, ensure_ascii=False)[:200]}",
                meta={"latency_ms": round(latency_ms, 1)},
            )

        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        sql = extract_sql(content)

        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)

        meta: dict[str, Any] = {
            "model": data.get("model") or self.config.model,
            "latency_ms": round(latency_ms, 1),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": int(
                usage.get("total_tokens") or prompt_tokens + completion_tokens
            ),
            "cost_cny": round(
                self.config.cost_cny(prompt_tokens, completion_tokens), 6
            ),
            "finish_reason": choices[0].get("finish_reason"),
            "raw_content": content,
        }

        if not sql:
            finish_reason = choices[0].get("finish_reason")
            # 「被截断」和「模型就是没输出」是两回事。
            # 混为一谈的后果是被真实数据抓出来的：deepseek-v4-flash 是推理模型，
            # reasoning_content 同样占用 max_tokens，推理一吃满额度正文就是空的，
            # 而 finish_reason 会老老实实写 length。
            # 如果不分开，评测配置的缺陷会被算成模型能力不足。
            if finish_reason == "length":
                return Generation(
                    error=(
                        f"响应被 max_tokens={self.config.max_tokens} 截断"
                        f"（finish_reason=length，completion_tokens={completion_tokens}）"
                        "—— 推理内容占满了额度，正文没来得及产出"
                    ),
                    meta=meta,
                )
            return Generation(
                error=f"模型返回了空内容（finish_reason={finish_reason}）",
                meta=meta,
            )
        return Generation(sql=sql, meta=meta)
