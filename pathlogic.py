"""Pure path/keyword/result-schema logic shared by the main tool and helper scripts.

Kept free of logging and side effects so any module (or CLI helper script) can
import these predicates without dragging in logger configuration: a shared
single implementation is the point, and it must stay import-cheap.
"""

from typing import Any


def split_path_components(path: str) -> list[str]:
    """Split a path into its directory/filename components.

    Both POSIX and Windows separators are normalized before splitting and
    empty components (leading or repeated separators) are dropped, so
    '/a/b', 'a/b' and 'a\\\\b' all yield the same component sequence.
    """
    return [component for component in path.replace("\\", "/").split("/") if component]


def path_matches_keyword(path: str, keyword: str) -> bool:
    """Component-aware keyword matching (replaces the old bare-substring rule).

    The keyword's component sequence matches when it occurs as a contiguous
    run of the path's components: a single-component keyword must equal one
    whole component; a multi-component keyword must align with consecutive
    components. Trailing separators on the keyword are ignored, so 'dirA' and
    'dirA/' behave identically. Because whole components are compared, 'dirA'
    matches files under the directory component 'dirA' but never a sibling
    named 'dirA-nm' — the bare substring rule matched both, which is how
    adjacent directories with a shared name prefix used to be caught in the
    same net. An empty keyword matches nothing.
    """
    keyword_components = split_path_components(keyword)
    if not keyword_components:
        return False
    path_components = split_path_components(path)
    width = len(keyword_components)
    return any(
        path_components[start:start + width] == keyword_components
        for start in range(len(path_components) - width + 1)
    )


# 结果文件结构校验：消费（跨目录裁剪 / 优先级选择 / 动作执行）之前的前置守卫。
# 选择"报错退出"而非"自动补排"：载入的语义是尊重文件里既有的优先级决定，
# 静默重排会改变使用者的意图；结构不合法时必须零动作把问题逐条讲清楚。
RESULT_SCHEMA_PROBLEM_LIMIT = 20


def validate_result_schema(data: Any) -> list[str]:
    """Return human-readable problems found in a loaded duplicates-result structure.

    Requirements: top level is a dict of group-key -> list of entry dicts; every
    entry carries a str 'path' and an int 'priority' (bool is rejected although
    it is an int subclass). An empty list means the structure is consumable.
    """
    if not isinstance(data, dict):
        return [f"top level must be an object mapping group keys to entry lists, "
                f"got {type(data).__name__}"]
    problems: list[str] = []
    for group_key, entries in data.items():
        if not isinstance(entries, list):
            problems.append(f"group {group_key!r}: value must be a list of entries, "
                            f"got {type(entries).__name__}")
            continue
        for index, entry in enumerate(entries):
            location = f"group {group_key!r} entry #{index}"
            if not isinstance(entry, dict):
                problems.append(f"{location}: entry must be an object, "
                                f"got {type(entry).__name__}")
                continue
            path = entry.get("path")
            if not isinstance(path, str):
                problems.append(f"{location}: field 'path' missing or not a string")
            priority = entry.get("priority")
            if isinstance(priority, bool) or not isinstance(priority, int):
                problems.append(f"{location} (path={path!r}): "
                                f"field 'priority' missing or not an integer")
    return problems
