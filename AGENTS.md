# AGENTS.md

这是 paperdrift：一个个人用的"自动找壁纸并学习口味"的服务（FastAPI + SQLite + 网页，之后有安卓 apk / Windows 小程序）。

**开工前先读 [`docs/PROJECT.md`](docs/PROJECT.md)**——里面有项目目标、已定决策、架构、核心算法、当前进度、已知问题和待办的详细设计。

最小规则：

- 跑 `pip install -r requirements-dev.txt && pytest`，基线必须全绿再改代码。
- 仓库是**公开**的：不要提交密码、令牌、IP；配置只进 `.env`（已被忽略）。
- 所有 `/api/*` 必须认证；新增接口要补"未登录 → 401"的测试。
- 外部网络/模型都要可注入，测试不得联网（见 `tests/conftest.py`）。
- 无法在沙盒里实测的东西，在 `README.md` 的"已知限制"和 `docs/PROJECT.md` 第 6 节如实标注。
- 做完一项更新 `docs/PROJECT.md` 的进度与待办。
