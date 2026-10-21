# 云端验证与公开交付

日期：2026-10-21。用户已明确批准公开筛选后的源码与静态案例页。

- [公开源码与自部署说明](https://github.com/VVR823/codecraft-harness)
- [公开案例页](https://vvr823.github.io/codecraft-harness/)
- [Windows / Linux / Docker 云 CI 实际运行结果](https://github.com/VVR823/codecraft-harness/actions/workflows/ci.yml)

主分支发布可自部署源码；gh-pages 分支只有 index.html、app.js、view.mjs、cases.json 和 .nojekyll。案例页不连接后端，包含三个离线案例和四个历史真实模型记录，分别标注来源。HTTPS 页面已通过 Edge 1280px / 375px 检查：7 个案例，无横向溢出、脚本错误或请求失败，线上资源摘要与发布包一致。

云 CI 使用公共仓库标准 runner、Python 3.13 和 Node 22：Windows/Linux 安装哈希锁定的依赖，运行 Ruff、格式、mypy、pytest 与覆盖率门禁、6 项 JavaScript 测试，并对接官方 MCP server 的 initialize、tools/list、echo 和 get-sum。独立 Linux job 构建 Docker 镜像，检查 UID 10001 和 offline_demo SDK smoke。具体数量、覆盖率和成功结论以对应 run 日志为准。

实际云端检查发现并修正两项发布问题：补回导出包漏带的既有 .dockerignore；为 Windows CI 显式设置 PYTHONIOENCODING=utf-8，避免官方 MCP 验证脚本输出中文时发生 cp1252 编码错误。未修改业务代码、降低测试标准或跳过失败步骤。

公开仓库从筛选快照建立，未上传原本地 Git 历史、真实数据、dotenv、模型文件、内部恢复记录或用户评估文档。源码来源和逐文件摘要见 [发布清单](evidence/publication-source-2026-10-21.json)。使用免费静态托管与公共标准 runner；本轮没有远端模型请求、付费服务购买或公网后端。GitHub PR 交付工具的真实远端验收仍不在本次发布证明范围内。
