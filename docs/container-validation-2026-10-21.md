# 2026-10-21本地容器与Linux实测

原计划剩余容器条件项已经实跑。用户明确批准安装，所有可配置安装路径、下载、Ubuntu虚拟磁盘和Docker数据在D:/CodeCraftEnv；Windows必需组件、MSI缓存和固定用户配置入口仍由Windows管理。未购买服务或调用模型，无提交、推送、发布和远端CI。

## 实际通过

- WSL3.0.3安装器官方SHA256与微软签名校验通过，实际安装路径D:/CodeCraftEnv/WSLRuntime；平台启用后用户自行重启，未自动重启。
- Ubuntu24.04.5导入为WSL2/CodeCraftUbuntu，注册表BasePath在D盘。nproc=2、内存3919MiB、Swap2048MiB和systemd实测；Docker29.9.2及Compose5.6.1从官方签名apt仓库安装。
- 原Dockerfile真实构建，镜像510048450143da301eb8fa029fd42b7d02f2f2b20b4aac9ae26e1f0baa7f65b3。未因验收修改业务代码。
- 原Compose显式`--env-file /dev/null`、新项目/新命名卷、offline_demo运行；唯一挂载为新卷/data，UID10001，容器健康状态healthy，health/ready通过。
- 离线T1 run11bfc2a4d86b完成、verified=true、0已知token/0未知；JSON补丁导出通过。这是脚本演示，不是真实模型修复成绩。
- `--force-recreate`后容器ID改变，同一卷中的run、3个工作区文件、补丁和5个checkpoint摘要均一致。
- Windows直连health、ready、console和app.js均HTTP200；本机前台启动入口也实测通过。停止入口后容器正常退出0。
- Linux Python3.13.16质量门禁287PASS/0SKIP、45.91秒、覆盖率2372/2674=88.7060583395662%；Ruff、97文件格式检查和14模块mypy通过。先前Windows跳过的符号链接用例本次在Linux通过。

## 追加：暂停任务跨容器人工恢复

回到原设计第9节复核时，确认前面的已完成任务保卷证据不能单独证明暂停任务能继续。另建codecraft_resume_20261021_1710新卷，在一次性准备进程采用原恢复单测同样的MAX_STEPS=2注入，真实执行前两步后得到paused；不改镜像文件或数据库状态来伪造暂停。正常API服务查询后不自动运行，force-recreate换容器后仍paused，checkpoint/工作区/原动作和调用计数完全一致。

显式POST /api/tasks/2cd2ec6c860e/resume后，任务done/verified=true、最后checkpoint从2到5，原2个action_id保持，最终4个唯一动作，无重放；补丁导出通过。model_calls从2到5表示离线DemoClient决策次数，不是模型网络请求，已知token/未知调用均0。另经真实镜像smoke入口，SDK导入、配置、health/ready及离线任务闭环通过，smoke run896d4a6722f3。见[独立恢复报告](evidence/container-resume-2026-10-21.json)，原始证据.cache/optimization/container-resume-20261021-1710/。

原工程总计划和E5最后未勾选条件已按这些实测更新。没有重跑模型/全套门禁、修改业务代码、查询真实库或触发云CI；容器已停止，WSL再次关闭，卷和全部证据保留。

## 失败记录与处置

GitHub裸域TLS失败，官方www入口可用；通过其返回的GitHub官方CDN下载，保持TLS验证且未修改代理。Docker直连镜像站超时，现有代理可用，因此只在新Docker环境配置已有代理。构建使用host网络和预定义代理build args；无第三方镜像站或关闭证书验证。

首次管理员弹窗被取消，均记录为未执行，用户现场确认后才成功安装。最初Windows HTTP拒绝连接时，现场核实WSL已空闲退出；保持前台WSL会话后HTTP200，未以修改防火墙掩盖原因。为用户提供前台启动脚本，不创建自动续跑、开机任务或后台保活。

第一次Linux验证副本漏带原pytest.ini，误收了故意失败的fixtures；补回原文件，没有放宽配置。另一处旧测试夹具创建不可执行的gh.exe，Linux的which("gh")无法找到；改成对应平台名称及可执行权限，并增强为断言命中精确路径。Linux/Windows定向各1PASS，再完整Linux287PASS。应用代码及真实库摘要不变。Windows默认受限临时目录错误也保留，改用D盘临时目录验证。

## 证据与使用

[机器可读报告](evidence/container-acceptance-2026-10-21.json)记录镜像/源码摘要、两个容器ID、卷、HTTP结果及重建前后摘要；完整原始日志和验收脚本保存在.cache/optimization/container-linux-20261021/，安装现场日志也保留在D:/CodeCraftEnv/logs/。失败日志未覆盖。

本机双击D:/CodeCraftEnv/start-codecraft.cmd，保持窗口打开，浏览器访问http://127.0.0.1:8000/console/。Ctrl+C停止应用。验收结束已停止容器并关闭WSL，镜像/卷保留，避免持续占用内存；下次启动不需要重装。安装目录实测Ubuntu虚拟磁盘文件约3.68GB（不代表全部环境总占用）。

本地Linux Python门禁不等于云CI；Linux官方Node/MCP、云端矩阵、真实GitHub交付和公网发布未执行。Docker验证使用可信任务和offline_demo，不构成恶意代码隔离或任意模型成功率证明。
