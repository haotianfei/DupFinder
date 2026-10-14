<!--
Before opening: link the related issue if there is one.
开 PR 前：如有对应 issue，请在下方关联。
-->

## Summary · 变更概要

<!-- What does this pull request change, and why? 这个 PR 改了什么、为什么？ -->

Closes #<!-- issue number / 关联 issue 编号 -->

## Checklist · 自查清单

- [ ] Commits follow Conventional Commits (`feat|fix|test|refactor|chore|docs(scope): summary`)
      提交信息符合 Conventional Commits 规范
- [ ] Tests included for the behavior change; suite green: `python3 -m pytest tests/ -q`
      行为改动附带 CLI seam 测试，全量测试本地通过
- [ ] Static check green: `python3 -m mypy dup_finder.py logger.py pathlogic.py change_priority.py`
      mypy 静态检查通过
- [ ] Docs kept in sync: README parameter table / verbatim `--help` block updated when the CLI surface changed
      CLI 面有变化时，README 参数表与逐字帮助块已同步
- [ ] [CHANGELOG.md](../CHANGELOG.md) entry added under `[Unreleased]` for user-visible changes
      用户可见变更已在 CHANGELOG 的 `[Unreleased]` 下登记

## Notes for reviewers · 评审提示

<!-- Optional: risk, migration hints, anything intentionally left out. 可选：风险点、升级注意、有意未覆盖之处。 -->
