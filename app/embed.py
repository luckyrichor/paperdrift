"""图像向量（CLIP）。可选依赖：没装 fastembed 时 available() 为 False，系统退回纯标签打分。"""
import importlib.util
import io
import logging
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger("paperdrift.embed")
MODEL = "Qdrant/clip-ViT-B-32-vision"      # ONNX 版 CLIP，CPU 即可，约 350MB，首次使用时自动下载


def to_bytes(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def from_bytes(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)


class Embedder:
    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = str(cache_dir) if cache_dir else None
        self._model = None
        self._lock = threading.Lock()
        self._retry_after = 0.0        # 模型加载失败后的冷却截止时间

    def available(self) -> bool:
        return time.monotonic() >= self._retry_after and importlib.util.find_spec("fastembed") is not None

    def _load(self):
        if self._model is None:
            from fastembed import ImageEmbedding
            log.info("loading CLIP model %s (first run downloads it)…", MODEL)
            self._model = ImageEmbedding(MODEL, cache_dir=self.cache_dir)
        return self._model

    def embed(self, data: bytes) -> np.ndarray | None:
        """返回单位向量；失败返回 None（调用方当作没有向量处理）。"""
        if not self.available():
            return None
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
            with self._lock:
                v = np.asarray(list(self._load().embed([img]))[0], dtype=np.float32)
            n = np.linalg.norm(v)
            return v / n if n else None
        except Exception as e:
            log.warning("embedding failed: %s", e)
            if self._model is None:
                self._retry_after = time.monotonic() + 600   # 模型下载/加载失败：10 分钟内不再重试，避免每张图都卡在下载上
            return None
