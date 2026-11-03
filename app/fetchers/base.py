from dataclasses import dataclass, field

UA = "paperdrift/0.1 (personal wallpaper curator)"


def orientation_of(w: int | None, h: int | None) -> str:
    if not w or not h:
        return "unknown"
    r = w / h
    if r >= 1.2:
        return "landscape"
    if r <= 0.85:
        return "portrait"
    return "square"


@dataclass
class Candidate:
    source: str
    source_id: str
    kind: str                      # image / gif / video
    preview_url: str
    full_url: str
    page_url: str = ""
    width: int | None = None
    height: int | None = None
    tags: list[str] = field(default_factory=list)
    prior: float = 0.0             # 0..1，来源自身的热度先验，冷启动时用来排序
    origin: str = ""               # 空 = 常规搜索；非空 = 由「喜欢的图的标签」扩展出的搜索（记下是哪个标签）

    @property
    def orientation(self) -> str:
        return orientation_of(self.width, self.height)


class Fetcher:
    name = "base"

    def search(self, terms: list[str], profile: dict, pages: int = 2) -> list[Candidate]:
        raise NotImplementedError

    def enrich(self, cand: Candidate) -> None:
        """可选：补充搜索结果里没有的信息（如 Wallhaven 的标签）。"""
