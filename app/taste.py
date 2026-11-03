import numpy as np

TOP_K = 5


def _topk_mean(sims: np.ndarray, k: int) -> np.ndarray:
    """sims: (n, m) → 每行取最相似的 k 个求平均。"""
    k = min(k, sims.shape[1])
    part = np.partition(sims, -k, axis=1)[:, -k:]
    return part.mean(axis=1)


def mmr_order(vecs: np.ndarray, scores: np.ndarray, lam: float, limit: int) -> list[int]:
    """带多样性的选择（MMR）：每次挑「分数 − lam×与已选图的最大相似度」最高的，避免一批里全是同一种图
    （这样用户的反馈信息量更大，也不会 30 张都是同一画师/同一构图）。返回前 limit 个下标，其余按原分数顺序补在后面。"""
    n = len(scores)
    order: list[int] = []
    avail = np.ones(n, dtype=bool)
    sim_max = np.zeros(n, dtype=np.float32)
    for _ in range(min(limit, n)):
        val = np.where(avail, scores - lam * np.maximum(sim_max, 0), -1e9)
        i = int(val.argmax())
        order.append(i)
        avail[i] = False
        sim_max = np.maximum(sim_max, vecs @ vecs[i])
    order += [int(i) for i in np.argsort(-scores) if avail[i]]
    return order


def zs(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    sd = x.std()
    return (x - x.mean()) / sd if len(x) > 1 and sd > 1e-6 else np.zeros(len(x), dtype=np.float32)


def taste_scores(cands: np.ndarray, pos: np.ndarray | None, neg: np.ndarray | None) -> np.ndarray:
    """口味分：离「喜欢/参考图」越近越高，离「不要」越近越低；在当前候选池内标准化（z-score），
    这样不受 CLIP 余弦相似度整体偏高的影响，也能和标签分数放在一起加权。所有向量须已归一化。"""
    n = cands.shape[0]
    raw = np.zeros(n, dtype=np.float32)
    if pos is not None and len(pos):
        raw += _topk_mean(cands @ pos.T, TOP_K)
    if neg is not None and len(neg):
        raw -= _topk_mean(cands @ neg.T, TOP_K)
    std = raw.std()
    if n < 2 or std < 1e-6:
        return np.zeros(n, dtype=np.float32)
    return (raw - raw.mean()) / std
