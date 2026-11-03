"""用户「喜欢」的图的共同标签 → 额外的搜索方向（让候选池朝喜欢的方向走，而不只是在同一批候选里重新排序）。"""
import math


def expansion_tags(liked: list[list[str]], decided: list[list[str]], exclude: set[str], k: int = 3,
                   min_count: int = 2) -> list[str]:
    """liked：喜欢的图的标签列表；decided：所有已评价（喜欢+不要）的图的标签列表。
    选「在喜欢的图里经常出现、且比在全部已评价图里更常见」的标签（提升度 lift），排除原本的搜索词。"""
    n_l, n_a = len(liked), len(decided)
    if n_l < min_count:
        return []
    cl: dict[str, int] = {}
    ca: dict[str, int] = {}
    for tags in liked:
        for t in set(tags):
            cl[t] = cl.get(t, 0) + 1
    for tags in decided:
        for t in set(tags):
            ca[t] = ca.get(t, 0) + 1
    scored = []
    for t, c in cl.items():
        artist = t.startswith("artist:")
        if c < min_count or t in exclude or c / n_l < (0.12 if artist else 0.4):    # 画师门槛低：同一画师被喜欢 2 次就值得追
            continue
        lift = math.log(((c + 0.5) / (n_l + 1)) / ((ca.get(t, 0) + 0.5) / (n_a + 1)))
        if lift > 0.3:
            scored.append((lift * math.log(c + 1) * (3 if artist else 1), t))       # 画师优先于衣服/配色这类细节标签
    return [t for _, t in sorted(scored, reverse=True)[:k]]
