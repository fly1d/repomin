# 重放一个已缩小的真实 Gradle 故障

此目录提供 **27 个 Aeron 原始文件、166,731 字节**，用于重放
[Gradle #39424](https://github.com/gradle/gradle/issues/39424)。
可直接检查 [payload](reference-result/payload)、[来源清单](provenance.json)
及[原生报告](reference-result/report.json)。

维护者用 ReproMin dev15 从 1,733 文件、21,010,709 字节删除到此结果，
4 次候选均接受。留下的 27 个文件是预先保护的集合，因此不是最小性证明。
随附报告是之后的 **27 → 27、零修改** 验证；它使用 `gradle` 名称，不再绑定
原运行器的临时路径。两段历史在来源清单中分开记录。

## 运行

已检查的环境是 **Ubuntu Linux、Bash、Temurin JDK 25.0.5、Gradle
9.8.1-bin、公开 ReproMin 0.1.0.dev15**，需要 Python 3.9+ 与标准 Unix 工具。
设置 `JAVA_HOME`，把 Gradle 和 `repomin` 放到 `PATH`，先核对三个工具的版本。
ReproMin 安装方法见[快速开始](../../docs/QUICKSTART.zh-CN.md)。JDK、Gradle 和
依赖缓存不随包提供，首次运行需要联网下载构建依赖。

在 **ReproMin 仓库根目录**，执行[英文说明中唯一的 Bash 代码块](README.md#run-the-checked-example)。
它会：

1. 在 `/tmp` 建独立目录，确认没有 `.git` 祖先目录，复制报告与干净的 payload。
2. 在单独的 `warmup` 副本中运行 Gradle，为私有 Gradle/Maven/JVM homes 下载依赖。
3. 严格检查退出码 1、完整原诊断和 `BUILD FAILED`，排除下载或配置错误。
4. 验证未被构建污染的 payload，再按原生报告在两份全新副本中离线重放。

成功时，整个代码块退出 0，JSON 中 `reproduced=true`、`passes=2`，并打印结果目录。
若中途停止，先看该目录的 `warmup.log`。不要在待验证的 `payload` 内运行 Gradle；
它生成的 `.gradle`、`build` 会改变指纹。工作目录保留供检查，结束后可自行删除。

上述命令会在宿主机执行该公开源码与 Gradle；离线和私有缓存并非网络沙箱。
`--yes` 表示允许执行已经审阅的报告命令。结果文件可能包含本地路径或命令输出，
分享前应检查。这套操作不会自动提交反馈或联系上游。

## 已有证据

[冷启动与迁移记录](https://github.com/fly1d/repomin/actions/runs/40995100565)
显示：空私有缓存首轮 36.518 秒出现原故障，新副本离线复现 7.400 秒。
Gradle、缓存、复现包和报告迁至新目录、旧目录消失后，两次重放仍通过。
“关闭警告失败”和“改跑 help”两个负对照都退出 0，且没有目标诊断。
完整的可分享字段见[精选收据](reference-result/receipt.json)。

托管环境采用精确指纹；Git 检出或传输后可以通过已记录的内容指纹验证。
27 个文件的原始 Git 字节与权限均已独立核对。迁移时 JDK 和 ReproMin 环境未移动；
此结果不代表其他机器或操作系统都能运行，也不代表独立采用、上游认可、最小复现
或相对其他 reducer 的优势。时间仅为一次运行的观察值。

原始源码固定在
[`aeron-io/aeron@1bae7dd`](https://github.com/aeron-io/aeron/tree/1bae7dd012eb98415cd86aa67d61b8fdaf339200)，
保留原始 Apache-2.0 [LICENSE](reference-result/payload/LICENSE) 和文件版权头。
原始 wrapper 文件完整保留，配置指向 Gradle 9.8.1-all；本次检查使用外置
9.8.1-bin，未单独运行该 wrapper 发行包。
文档和收据整理使用了 Codex 协助。后续重跑删除验证时，需要用 `--keep`
保留名称为 `build` 的 Java 源码包；随附报告保护了全部 27 文件且关闭内容 reducer。
