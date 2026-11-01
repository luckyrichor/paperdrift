import numpy as np

TOP_K = 5


def _topk_mean(sims: np.ndarray, k: int) -> np.ndarray:
    """sims: (n, m) → 每行取最相似的 k 个求平均。"""
    k = min(k, sims.shape[1])
    part = np.partition(sims, -k, axis=1)[:, -k:]
    return part.mean(axis=1)


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
