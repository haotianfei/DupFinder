import json
import argparse
import sys

from pathlogic import (
    RESULT_SCHEMA_PROBLEM_LIMIT,
    path_matches_keyword,
    validate_result_schema,
)


def adjust_priority(data, target_path, adjustment):
    """
    调整指定路径文件的优先级（假定 data 已通过 validate_result_schema 校验）

    :param data: 原始JSON数据 {组key: [条目, ...]}
    :param target_path: 目标路径关键字，按路径分量匹配（与 dup_finder 的 --retain 同一实现）：
        关键字需等于某个完整路径分量，或作为连续分量序列出现；
        "dirA" 命中 dirA/... 下的文件，但不会命中兄弟目录 dirA-nm/...
    :param adjustment: 调整值。优先级数值越小越优先保留，本函数计算
        priority - adjustment 并截到不小于 0，因此：
        正值 = 提升保留优先级（数值变小），负值 = 降低保留优先级（数值变大）。
    最终结果在保存时按优先级升序排列。
    """
    for group in data.values():
        for file_entry in group:
            if path_matches_keyword(file_entry["path"], target_path):
                # 调整值符号与效果：正值令数值减小、保留优先级提升（下限 0）
                new_priority = file_entry["priority"] - adjustment
                file_entry["priority"] = max(0, new_priority)
    return data


if __name__ == "__main__":
    """
    用法示例：

    # 提升路径分量含 "archive" 的文件的保留优先级（数值减少 1）
    python change_priority.py -i duplicates.json -o adjusted.json -p archive -a 1

    # 降低分量含 "imported-2024" 的文件的保留优先级（数值增加 2）
    python change_priority.py -i duplicates.json -o adjusted.json -p imported-2024 -a -2
    """
    # 解析命令行参数
    parser = argparse.ArgumentParser(description="调整重复文件优先级")
    parser.add_argument("-i", "--input", required=True, help="输入JSON文件路径")
    parser.add_argument("-o", "--output", required=True, help="输出JSON文件路径")
    parser.add_argument("-p", "--path", required=True,
                        help="目标路径关键字，按路径分量匹配（与 dup_finder --retain 同语义）："
                             "关键字须等于某个完整路径分量、或作为连续分量序列出现；"
                             "例如 dirA 命中 dirA/... 但不会命中兄弟目录 dirA-nm/...")
    parser.add_argument("-a", "--adjust", type=int, required=True,
                        help="调整值。优先级数值越小越优先保留；本工具计算 priority - adjustment"
                             "（下限 0），所以正值提升保留优先级、负值降低")

    args = parser.parse_args()
    
    # 读取原始数据
    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 结构前置校验：条目缺 path/priority 等字段时给出指明位置的逐条可读错误，
    # 而不是在调整中途抛裸 KeyError
    problems = validate_result_schema(data)
    if problems:
        print(f"输入结果文件结构不合法：{args.input}，共 {len(problems)} 个问题：",
              file=sys.stderr)
        for problem in problems[:RESULT_SCHEMA_PROBLEM_LIMIT]:
            print(f"  - {problem}", file=sys.stderr)
        if len(problems) > RESULT_SCHEMA_PROBLEM_LIMIT:
            print(f"  ...以及另外 {len(problems) - RESULT_SCHEMA_PROBLEM_LIMIT} 个问题未列出",
                  file=sys.stderr)
        sys.exit(1)
    
    # 执行调整
    adjusted_data = adjust_priority(data, args.path, args.adjust)
    
    # 保存结果
    with open(args.output, "w", encoding="utf-8") as f:
        # 按优先级升序排列后保存（数值越小优先级越高）
        sorted_data = {
            k: sorted(v, key=lambda x: x["priority"])
            for k, v in adjusted_data.items()
        }
        json.dump(sorted_data, f, indent=2, ensure_ascii=False)
    
    print(f"优先级调整完成，结果已保存至 {args.output}")
