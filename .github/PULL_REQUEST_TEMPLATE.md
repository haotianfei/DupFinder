<!--
开 PR 前：如有对应 issue，请在下方关联。
Before opening: link the related issue if there is one.
-->

## 变更概要

<!-- 这个 PR 改了什么、为什么？What does this pull request change, and why? -->

关联 Related: #<!-- issue 编号 issue number -->

### 自查清单

- [ ] 提交信息符合 Conventional Commits（`feat|fix|test|refactor|chore|docs(范围): 描述`）
- [ ] 行为改动附带 CLI seam 测试，本地全量通过：`python3 -m pytest tests/ -q`
- [ ] 静态检查通过：`python3 -m mypy dup_finder.py logger.py pathlogic.py change_priority.py`
- [ ] CLI 面有变化时，README 参数表与逐字帮助块已同步
- [ ] 用户可见变更已在 [CHANGELOG.md](../CHANGELOG.md) 的 `[Unreleased]` 下登记

### 评审提示（可选）

<!-- 风险点、升级注意、有意未覆盖之处。 -->

---

## Summary

<!-- What does this pull request change, and why? -->

Related: #<!-- issue number -->

### Checklist

- [ ] Commit messages follow Conventional Commits (`feat|fix|test|refactor|chore|docs(scope): summary`)
- [ ] Behavior changes ship with CLI-seam tests; full suite green locally: `python3 -m pytest tests/ -q`
- [ ] Static check green: `python3 -m mypy dup_finder.py logger.py pathlogic.py change_priority.py`
- [ ] README parameter table / verbatim help block updated when the command-line surface changed
- [ ] User-visible changes recorded under `[Unreleased]` in [CHANGELOG.md](../CHANGELOG.md)

### Notes for reviewers (optional)

<!-- Risk, migration hints, anything intentionally left out. -->
