# Contributing to DupFinder · 贡献指南

**English** · [中文](#中文-贡献指南)

Thank you for helping improve DupFinder! This guide covers environment setup,
how to run the checks CI enforces, repo conventions, and the pull-request
flow. 感谢参与改进 DupFinder！本文覆盖环境准备、本地如何跑与 CI 一致的检查、
仓库约定与 PR 流程。

## Development environment · 开发环境

Requirements: Python **3.11+** (the tool relies on `hashlib.file_digest`).
要求：Python **3.11 及以上**（运行依赖 `hashlib.file_digest`）。

```bash
git clone https://github.com/haotianfei/DupFinder.git
cd DupFinder
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]        # runtime: filelock;  dev: pytest, mypy
```

## Repository layout · 目录结构

| File 文件 | Role 职责 |
| --- | --- |
| `dup_finder.py` | Main CLI: two-phase scan, hash cache, actions, manifests, rollback (undo). 主程序：两阶段扫描、哈希缓存、动作执行、移动清单与回滚。 |
| `pathlogic.py` | Pure functions only (no logging, no I/O side effects): path-component matching and result-file schema validation. 纯函数：路径分量匹配与结果文件结构校验，无日志与 IO 副作用。 |
| `logger.py` | Logging configuration shared by the tools: console INFO milestones plus a rotating file handler. 统一日志配置：控制台 INFO 里程碑 + 按天轮转的文件日志。 |
| `change_priority.py` | Standalone utility to tune `priority` values inside a result JSON before reprocessing. 独立脚本：回喂处理前微调结果 JSON 中的优先级。 |

Keep these boundaries: `pathlogic.py` must stay import-cheap so
`change_priority.py` never pulls in logging or cache machinery. 请保持此边界：
`pathlogic.py` 必须保持"导入零副作用"，辅助脚本才不会连带日志/缓存机制。

## Running the checks · 本地检查

CI runs exactly these two gates — run them before pushing.
CI 只跑以下两道门禁，推送前请本地先跑：

```bash
# 1) Test suite  测试套件
python3 -m pytest tests/ -q

# fast subset only (skip the slow-marked integration cases)  仅快测档（跳过 slow）
python3 -m pytest tests/ -q -m "not slow"

# 2) Static typing  静态类型检查
python3 -m mypy dup_finder.py logger.py pathlogic.py change_priority.py
```

Notes on the suite · 测试约定：

- **Single seam: the CLI process boundary.** Every test drives the tools as a
  subprocess and asserts on external behavior only — exit code, log milestones,
  filesystem end state, artifact files (cache JSON, result JSON, move
  manifests). Never assert on internals and never import the module under test
  to poke at it. 唯一 seam 是 CLI 进程边界：所有用例以子进程方式驱动工具，只断言
  外部行为（退出码、日志里程碑、文件系统终态、产物文件）。不要断言内部实现，也不要
  import 被测模块去戳私有细节。
- **Every test runs in its own private temp tree.** The `DupTree` fixture in
  `tests/conftest.py` copies the scripts into a per-test directory and scans
  only files inside it. Do not point tests at real user data, and do not write
  into the repository root — an autouse guard fails the run if repo-level
  artifacts (`file_cache.json`, `logs/`, `out/`, `duplicates_*.json`) change
  during a test. 每个用例跑在私有临时树：`tests/conftest.py` 的 `DupTree` fixture
  会把脚本副本放进独立目录并只扫描其中内容。不要让测试指向真实用户数据，也不要向
  仓库根写任何产物——用例期间仓库根的既有产物若发生变化，autouse 守卫会让用例直接
  失败。
- **`slow` marker.** Long-running integration cases (large fixture trees,
  cache-compatibility probes) carry `@pytest.mark.slow`; keep them out of the
  default quick loop locally, but CI runs them too — they must stay green.
  `slow` 标记用于耗时较长的集成用例（大树、缓存兼容验证）：本地快速回测可跳过，
  CI 仍会全量执行，必须保持通过。

## Commit conventions · 提交规范

We follow [Conventional Commits](https://www.conventionalcommits.org/). Type
prefix + optional scope + imperative summary.
遵循 Conventional Commits：`类型(范围): 一句话描述（祈使句）`。

Types: `feat`, `fix`, `test`, `refactor`, `chore`, `docs`. 示例 Examples:

```text
feat(cli): add --min-free-percent guard to move actions
fix(cache): flush pending entries before exiting on SIGTERM
test(undo): cover manifests with occupied source locations
docs(readme): refresh parameter table to match --help
refactor(pathlogic): extract component matcher from dup_finder
chore(ci): pin mypy in dev extras
```

One logical change per commit; keep diffs reviewable. 一个提交只做一件事。

## Pull-request workflow · PR 流程

1. **Open an issue first** for anything non-trivial (bug, design question,
   new user-facing flag). Keep the discussion there before writing code.
   非小改动先提 issue（缺陷、设计讨论、新增用户可见参数），讨论清楚再动手。
2. Branch from `main`, make focused commits as above.
   从 `main` 切分支，按上述规范做聚焦的小步提交。
3. **Every behavior change ships with tests** (new cases or adapted ones), and
   docs move with it: update the README parameter table / help paste when the
   CLI surface changes, and add a line under `[Unreleased]` in
   [CHANGELOG.md](CHANGELOG.md). 行为改动必须带测试，并同步文档：CLI 面变化时
   更新 README 参数表与逐字帮助块，同时在 CHANGELOG 的 `[Unreleased]` 增加条目。
4. Keep the full suite and mypy green; CI must pass before review.
   全量测试与 mypy 必须通过；CI 绿后才进入评审。
5. A maintainer merges via squash; please keep your branch name short and
   descriptive. 维护者以 squash 合并；分支名请简短达意。

### PR checklist · 自查清单

- [ ] Commit messages follow Conventional Commits 提交信息符合规范
- [ ] `python3 -m pytest tests/ -q` green 全量测试通过
- [ ] `python3 -m mypy dup_finder.py logger.py pathlogic.py change_priority.py` green 类型检查通过
- [ ] Tests cover the behavior change (CLI seam) 行为改动有 CLI seam 测试覆盖
- [ ] README / CHANGELOG updated as needed 文档与 CHANGELOG 已同步

---

<a name="中文-贡献指南"></a>

## Additional notes · 补充说明

- **Runtime artifacts are gitignored.** `file_cache.json`, `logs/`, `out/`,
  `duplicates_*.json`, `*.pyc` / `__pycache__`, `.pytest_cache/` are local
  state only — never commit them, and never point the tool at data you cannot
  afford to move while testing manually. 运行产物全部在 .gitignore 内：哈希缓存、
  日志、结果 JSON、字节码与 pytest 缓存都只是本地状态，不提交；手动验证时也不要用
  承受不起移动的数据。
- **Formatting.** No separate formatter is enforced; match the surrounding
  style (4-space indent, snake_case, docstrings on public functions). 代码风格
  以就近一致为准：公共函数保留 docstring，命名用 snake_case。
- Questions? Open an issue — the maintainers read them. 有疑问直接提 issue。
