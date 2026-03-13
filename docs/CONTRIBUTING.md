# 贡献指南

欢迎提交 Issue 和 Pull Request。

## 流程

1. Fork 本仓库
2. 创建分支：`git checkout -b feature/xxx`
3. 提交：`git commit -m 'feat: xxx'`（使用 [Conventional Commits](https://www.conventionalcommits.org/)）
4. 推送并创建 PR

## 规范

- Python 遵循 PEP 8
- 提交前运行：`./scripts/ci_gate.sh`
- 修改前端时：`cd apps/dsa-web && npm run lint && npm run build`
