import math

PRIOR_WEIGHT = 0.5


def score(tags: list[str], weights: dict[str, float], prior: float = 0.0) -> float:
    """标签加权：已学习到的标签权重之和，除以 sqrt(标签数+1) 避免标签多的图占便宜；再加一点来源热度先验。
    没有任何反馈时 weights 为空，分数就只剩先验，相当于按来源热度排序（冷启动）。"""
    known = sum(weights.get(t, 0.0) for t in set(tags))
    return known / math.sqrt(len(set(tags)) + 1) + PRIOR_WEIGHT * prior
