"""
自动化版本发布脚本
纯AI无手作
功能:
1. 接收新版本号作为参数
2. 更新 pyproject.toml 中的版本号
3. 运行 uv sync 更新 uv.lock
4. 提交更改到 Git
5. 创建 Git 标签
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path


def run_command(command, check=True):
    """运行命令并返回结果"""
    print(f"执行命令: {' '.join(command)}")
    try:
        result = subprocess.run(  # noqa: S603
            command, capture_output=True, text=True, encoding="utf-8", check=check
        )
        if result.stdout:
            print(result.stdout.strip())
        return result
    except subprocess.CalledProcessError as e:
        print(f"命令执行失败: {e}")
        if e.stderr:
            print(f"错误信息: {e.stderr.strip()}")
        sys.exit(1)


def validate_version(version):
    """验证版本号格式"""
    pattern = r"^\d+\.\d+\.\d+$"
    if not re.match(pattern, version):
        print(f"错误: 版本号格式无效 '{version}'，应为 x.y.z 格式")
        sys.exit(1)


def update_pyproject_version(new_version):
    """更新 pyproject.toml 中的版本号"""
    pyproject_path = Path("pyproject.toml")

    if not pyproject_path.exists():
        print("错误: pyproject.toml 文件不存在")
        sys.exit(1)

    # 读取文件内容
    content = pyproject_path.read_text(encoding="utf-8")

    # 查找并替换版本号
    version_pattern = r'(version\s*=\s*")([^"]+)(")'
    match = re.search(version_pattern, content)

    if not match:
        print("错误: 在 pyproject.toml 中找不到版本号")
        sys.exit(1)

    old_version = match.group(2)
    print(f"当前版本: {old_version}")
    print(f"新版本: {new_version}")

    # 替换版本号
    new_content = re.sub(r'version\s*=\s*"[^"]+"', f'version = "{new_version}"', content)

    # 写回文件
    pyproject_path.write_text(new_content, encoding="utf-8")
    print("✓ 已更新 pyproject.toml 中的版本号")

    return old_version


def sync_dependencies():
    """运行 uv sync 更新依赖"""
    print("正在同步依赖...")
    run_command(["uv", "sync"])
    print("✓ 依赖同步完成")


def git_add_files():
    """将文件添加到 Git 暂存区"""
    files_to_add = ["pyproject.toml", "uv.lock"]
    for file in files_to_add:
        if Path(file).exists():
            run_command(["git", "add", file])
    print("✓ 文件已添加到 Git 暂存区")


def git_commit(version):
    """提交更改"""
    commit_message = f"Bump version to {version}"
    run_command(["git", "commit", "-m", commit_message])
    print(f"✓ 已提交更改: {commit_message}")


def git_create_tag(version):
    """创建 Git 标签"""
    tag_name = f"v{version}"
    tag_message = f"Release version {version}"
    run_command(["git", "tag", "-a", tag_name, "-m", tag_message])
    print(f"✓ 已创建标签: {tag_name}")


def check_git_status():
    """检查 Git 状态"""
    result = run_command(["git", "status", "--porcelain"], check=False)
    if result.stdout.strip():
        print("警告: 工作目录有未提交的更改")
        print("请先提交或暂存这些更改，然后再运行此脚本")
        print("未提交的文件:")
        print(result.stdout)
        response = input("是否继续? (y/N): ")
        if response.lower() != "y":
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="自动化版本发布脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python release.py 1.2.3
  python release.py 2.0.0

此脚本将:
1. 更新 pyproject.toml 中的版本号
2. 运行 uv sync 更新依赖
3. 提交更改到 Git
4. 创建版本标签
        """,
    )
    parser.add_argument("version", help="新版本号 (格式: x.y.z)")
    parser.add_argument("--skip-git-check", action="store_true", help="跳过 Git 状态检查")

    args = parser.parse_args()

    # 验证版本号格式
    validate_version(args.version)

    # 检查 Git 状态
    if not args.skip_git_check:
        check_git_status()

    try:
        print(f"开始发布版本 {args.version}...")
        print("=" * 50)

        # 1. 更新版本号
        old_version = update_pyproject_version(args.version)

        # 2. 同步依赖
        sync_dependencies()

        # 3. 添加文件到 Git
        git_add_files()

        # 4. 提交更改
        git_commit(args.version)

        # 5. 创建标签
        git_create_tag(args.version)

        print("=" * 50)
        print("✅ 版本发布完成!")
        print(f"   版本: {old_version} → {args.version}")
        print(f"   标签: v{args.version}")
        print()
        print("下一步操作:")
        print("   git push origin main")
        print(f"   git push origin v{args.version}")

    except KeyboardInterrupt:
        print("\n操作被用户取消")
        sys.exit(1)
    except Exception as e:
        print(f"\n发生错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
