# 机器可读证据

本目录保存公开评测的结构化结果和最小复现材料。上层结论与解释口径见 [`../evaluation.md`](../evaluation.md)。

主要文件：

- `local-7b-fixed-suite-2026-10-20.json`：同一 7B 配置下三个固定小任务的单次结果。
- `real-acceptance-2026-10-25.json`：新版固定集和远端交付工具验证。
- `real-repository-2026-10-26.json`：较复杂仓库任务的运行结果。
- `complex-repository-2026-10-27.json`：最新复杂仓库恢复任务的失败结果和声明边界。
- `repair-recovery-2026-10-26.json`：失败编辑恢复行为。
- `pytest-feedback-2026-10-26.json`：测试失败信息的结构化反馈。
- `container-acceptance-2026-10-21.json`：容器构建、非 root 运行和离线任务。
- `container-resume-2026-10-21.json`：数据卷重建恢复。
- `self-audit-2026-10-22.json`：跨平台工程检查摘要。

这些文件记录特定日期、源码和配置下的结果。它们不构成任意任务的稳定成功率，也不能替代当前代码的重新验证。
