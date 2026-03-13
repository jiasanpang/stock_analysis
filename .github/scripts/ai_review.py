#!/usr/bin/env python3
"""
AI code review script used by GitHub Actions PR Review workflow.
"""
import json
import os
import subprocess
import traceback


MAX_DIFF_LENGTH = 18000
REVIEW_PATHS = [
    '*.py',
    '*.md',
    'README.md',
    'AGENTS.md',
    'docs/**',
    '.github/PULL_REQUEST_TEMPLATE.md',
    'requirements.txt',
    'pyproject.toml',
    'setup.cfg',
    '.github/workflows/*.yml',
    '.github/scripts/*.py',
    'apps/dsa-web/**',
]


def run_git(args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"⚠️ git command failed: {' '.join(args)}")
        print(result.stderr.strip())
        return ''
    return result.stdout.strip()


def get_diff():
    """Get PR diff content for review-relevant files."""
    base_ref = os.environ.get('GITHUB_BASE_REF', 'main')
    diff = run_git(['git', 'diff', f'origin/{base_ref}...HEAD', '--', *REVIEW_PATHS])
    truncated = len(diff) > MAX_DIFF_LENGTH
    return diff[:MAX_DIFF_LENGTH], truncated


def get_changed_files():
    """Get changed file list for review-relevant files."""
    base_ref = os.environ.get('GITHUB_BASE_REF', 'main')
    output = run_git(['git', 'diff', '--name-only', f'origin/{base_ref}...HEAD', '--', *REVIEW_PATHS])
    return output.split('\n') if output else []


def get_pr_context():
    """Read PR title/body from GitHub event payload when available."""
    event_path = os.environ.get('GITHUB_EVENT_PATH')
    if not event_path or not os.path.exists(event_path):
        return '', ''
    try:
        with open(event_path, 'r', encoding='utf-8') as f:
            payload = json.load(f)
        pr = payload.get('pull_request', {})
        return (pr.get('title') or '').strip(), (pr.get('body') or '').strip()
    except Exception:
        return '', ''


def classify_files(files):
    py_files = [f for f in files if f.endswith('.py')]
    doc_files = [f for f in files if f.endswith('.md') or f.startswith('docs/') or f in ('README.md', 'AGENTS.md')]
    frontend_files = [f for f in files if f.startswith('apps/dsa-web/') or f.endswith(('.tsx', '.ts'))]
    ci_files = [f for f in files if f.startswith('.github/workflows/')]
    config_files = [
        f for f in files if f in ('requirements.txt', 'pyproject.toml', 'setup.cfg', '.github/PULL_REQUEST_TEMPLATE.md')
    ]
    return py_files, doc_files, frontend_files, ci_files, config_files


def build_prompt(diff_content, files, truncated, pr_title, pr_body):
    """Build AI review prompt aligned with AGENTS.md requirements."""
    truncate_notice = ''
    if truncated:
        truncate_notice = "\n\n> ⚠️ 注意：diff 过长已截断，请基于可见内容审查并标注不确定点。\n"

    py_files, doc_files, frontend_files, ci_files, config_files = classify_files(files)

    return f"""你是本仓库的 PR 审查助手。请根据变更内容和 PR 描述，执行“代码 + 文档 + CI”联合审查。

## PR 信息
- 标题: {pr_title or '(empty)'}
- 描述:
{pr_body or '(empty)'}

## 修改文件统计
- Python: {len(py_files)}
- Docs/Markdown: {len(doc_files)}
- Frontend (apps/dsa-web): {len(frontend_files)}
- CI Workflow: {len(ci_files)}
- Config/Template: {len(config_files)}

修改文件列表:
{', '.join(files)}{truncate_notice}

## 代码变更 (diff)
```diff
{diff_content}
```

## 必须对齐的审查规则（来自仓库 AGENTS.md）
1. 必要性（Necessity）：是否有明确问题/业务价值，避免无效重构。
2. 关联性（Traceability）：是否有关联 Issue（Fixes/Refs）；无 Issue 时是否给出动机与验收标准。
3. 类型判定（Type）：fix/feat/refactor/docs/chore/test 是否匹配。
4. 描述完整性（Description Completeness）：是否包含背景、范围、验证命令与结果、兼容性风险、回滚方案。
5. 合入判定（Merge Readiness）：给出 Ready / Not Ready，并列出阻断项。
6. 若涉及用户可见能力，检查 README.md 与 docs/CHANGELOG.md 是否同步。

## 审查输出要求
- 使用中文。
- 先给“结论”：`Ready to Merge` 或 `Not Ready`。
- 再给结构化结果：
  - 必要性：通过/不通过 + 理由
  - 关联性：通过/不通过 + 证据
  - 类型：建议类型
  - 描述完整性：完整/不完整（缺失项）
  - 风险级别：低/中/高 + 关键风险
  - 必改项（最多 5 条，按优先级）
  - 建议项（最多 5 条）
- 对发现的问题，尽量定位到文件路径并说明影响。
- 如果信息不足，明确写“基于当前 diff/PR 描述无法确认”。
"""


def review_with_gemini(prompt):
    """Run review with Gemini API."""
    api_key = os.environ.get('GEMINI_API_KEY')
    model = os.environ.get('GEMINI_MODEL') or os.environ.get('GEMINI_MODEL_FALLBACK') or 'gemini-2.5-flash'

    if not api_key:
        print("❌ Gemini API Key 未配置（检查 GitHub Secrets: GEMINI_API_KEY）")
        return None

    print(f"🤖 使用模型: {model}")

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model,
            contents=prompt
        )
        print(f"✅ Gemini ({model}) 审查成功")
        return response.text
    except ImportError as e:
        print(f"❌ Gemini 依赖未安装: {e}")
        print("   请确保安装了 google-genai: pip install google-genai")
        return None
    except Exception as e:
        print(f"❌ Gemini 审查失败: {e}")
        traceback.print_exc()
        return None


def review_with_openai(prompt):
    """Run review with OpenAI-compatible API as fallback."""
    api_key = os.environ.get('OPENAI_API_KEY')
    base_url = os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1')
    model = os.environ.get('OPENAI_MODEL', 'gpt-4o-mini')

    if not api_key:
        print("❌ OpenAI API Key 未配置（检查 GitHub Secrets: OPENAI_API_KEY）")
        return None

    print(f"🌐 Base URL: {base_url}")
    print(f"🤖 使用模型: {model}")

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            temperature=0.3
        )
        print(f"✅ OpenAI 兼容接口 ({model}) 审查成功")
        return response.choices[0].message.content
    except ImportError as e:
        print(f"❌ OpenAI 依赖未安装: {e}")
        print("   请确保安装了 openai: pip install openai")
        return None
    except Exception as e:
        print(f"❌ OpenAI 兼容接口审查失败: {e}")
        traceback.print_exc()
        return None


def ai_review(diff_content, files, truncated):
    """Run AI review: Gemini first, then OpenAI fallback."""
    pr_title, pr_body = get_pr_context()
    prompt = build_prompt(diff_content, files, truncated, pr_title, pr_body)

    result = review_with_gemini(prompt)
    if result:
        return result

    print("尝试使用 OpenAI 兼容接口...")
    result = review_with_openai(prompt)
    if result:
        return result

    return None


def main():
    diff, truncated = get_diff()
    files = get_changed_files()

    if not diff or not files:
        print("没有可审查的代码/文档/配置变更，跳过 AI 审查")
        summary_file = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary_file:
            with open(summary_file, 'a', encoding='utf-8') as f:
                f.write("## 🤖 AI 代码审查\n\n✅ 没有可审查变更\n")
        return

    print(f"审查文件: {files}")
    if truncated:
        print(f"⚠️ Diff 内容已截断至 {MAX_DIFF_LENGTH} 字符")

    review = ai_review(diff, files, truncated)

    summary_file = os.environ.get('GITHUB_STEP_SUMMARY')

    strict_mode = os.environ.get('AI_REVIEW_STRICT', 'false').lower() == 'true'

    if review:
        if summary_file:
            with open(summary_file, 'a', encoding='utf-8') as f:
                f.write(f"## 🤖 AI 代码审查\n\n{review}\n")

        with open('ai_review_result.txt', 'w', encoding='utf-8') as f:
            f.write(review)

        print("AI 审查完成")
    else:
        print("⚠️ 所有 AI 接口都不可用")
        if summary_file:
            with open(summary_file, 'a', encoding='utf-8') as f:
                f.write("## 🤖 AI 代码审查\n\n⚠️ AI 接口不可用，请检查配置\n")
        if strict_mode:
            raise SystemExit("AI_REVIEW_STRICT=true and no AI review result is available")


if __name__ == '__main__':
    main()
