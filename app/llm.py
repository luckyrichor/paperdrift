"""用大模型（DeepSeek，OpenAI 兼容接口）把自然语言的壁纸需求翻译成各图站的英文搜索标签。

只在需求描述含中文、且服务器配置了 DEEPSEEK_API_KEY 时才调用；每个（需求，描述文本）只翻译一次并缓存在数据库。
API Key 只来自环境变量，绝不写进日志、错误信息或仓库。
"""
import json
import logging
import re

import httpx

log = logging.getLogger("paperdrift.llm")

CJK = re.compile(r"[一-鿿]")
TERM_OK = re.compile(r"^[a-z0-9_'()!.\- ]{1,60}$")

SYSTEM_PROMPT = """你是壁纸图站的搜索助手。用户用自然语言描述想要的壁纸，请把它翻译成两个图站的英文搜索词。只输出 json，格式示例：
{"danbooru": ["genshin_impact", "solo", "1girl"], "wallhaven": ["genshin impact", "solo"], "notes": "一句话说明你的理解"}

规则：
- danbooru：Danbooru 标签，英文小写，单词之间用下划线；按重要性从高到低排（只有前 2 个会发给服务器，其余在本地过滤），最多 6 个。
  指定了具体角色就用角色标签（新角色或容易重名的角色要带作品后缀，如 lauma_(genshin_impact)）；只说了作品没说角色，就用作品标签（如 genshin_impact）。
  「只要一个角色 / 单人 / 不能有多个角色」→ solo；「女角色」→ 1girl；「男角色」→ 1boy。
- wallhaven：普通英文词，最多 4 个，词里可以有空格。
- 不要输出带冒号的元标签（如 rating:xxx、order:xxx），不要用 - 或 ~ 开头的标签。
- 用户本来就写成英文标签的，原样保留。
- 不确定的角色名不要编造，宁可只给作品名。
- 分辨率、横竖屏之类的要求不要写成标签。"""


class TranslateError(Exception):
    """翻译失败；消息可以直接展示给用户（不含密钥）。"""


def has_cjk(text: str) -> bool:
    return bool(CJK.search(text or ""))


def clean_terms(raw, limit: int, snake: bool) -> list[str]:
    """模型的输出不可信：只留下安全的、普通的搜索词（丢掉元标签、排除标签、奇怪字符）。"""
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for t in raw:
        if not isinstance(t, str):
            continue
        t = t.strip().lower()
        if not TERM_OK.match(t) or t.startswith(("-", "~")) or ":" in t:
            continue
        t = t.replace(" ", "_") if snake else " ".join(t.replace("_", " ").split())
        if t and t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    return out


class Translator:
    def __init__(self, api_key: str, model: str = "deepseek-flash", base_url: str = "https://api.deepseek.com",
                 client: httpx.Client | None = None):
        self.api_key = api_key
        self.model = model
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.client = client or httpx.Client(timeout=40)

    def available(self) -> bool:
        return bool(self.api_key)

    def _post(self, body: dict) -> httpx.Response:
        try:
            return self.client.post(self.url, json=body, headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as e:
            raise TranslateError(f"连不上翻译服务（{type(e).__name__}）") from None

    def translate(self, query: str) -> dict:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": query}],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},            # 翻译用不着思考模式：更快更省
            "temperature": 0.2,
            "max_tokens": 600,
            "stream": False,
        }
        r = self._post(body)
        if r.status_code in (400, 422):                   # 个别部署不认 thinking 参数：去掉重试一次
            body.pop("thinking")
            r = self._post(body)
        if r.status_code in (401, 403):
            raise TranslateError("翻译服务拒绝了 API Key（无效或无权限）")
        if r.status_code == 402:
            raise TranslateError("翻译服务余额不足")
        if r.status_code == 429:
            raise TranslateError("翻译服务请求过于频繁，稍后再试")
        if r.status_code != 200:
            raise TranslateError(f"翻译服务返回 HTTP {r.status_code}")
        try:
            content = r.json()["choices"][0]["message"]["content"] or ""
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, KeyError, IndexError, TypeError):
            raise TranslateError("翻译服务返回的内容无法解析") from None
        result = {
            "for": query,
            "danbooru": clean_terms(data.get("danbooru"), 6, snake=True),
            "wallhaven": clean_terms(data.get("wallhaven"), 4, snake=False),
            "notes": str(data.get("notes") or "")[:200],
        }
        if not result["danbooru"] and not result["wallhaven"]:
            raise TranslateError("翻译结果为空")
        return result
