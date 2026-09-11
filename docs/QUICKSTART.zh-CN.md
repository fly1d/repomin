# ReproMin 中文快速开始：缩减真实失败

本指南把一个现有仓库中的可重复失败，缩减成便于提交 issue、调试或加入
回归测试的较小复现目录。如果只想确认 ReproMin 能运行，请先执行 README 中的
[30 秒 demo](../README.md#30-second-demo)。

ReproMin 需要 Python 3.9 或更高版本。下面的命令适用于 macOS 或 Linux 的
Bash/Zsh；Windows 用户可参考 [PowerShell 演练](QUICKSTART.windows.md)中的安装、
变量和引号写法。

## 开始前

适合作为首次尝试的失败通常满足以下条件：

- 一条本地命令可以稳定触发目标失败；
- 原仓库太大，不适合提交 issue 或长期保留为回归样例；
- 命令不依赖凭据、私有服务、生产数据或特殊硬件；
- 你信任仓库以及它会执行的全部命令。

ReproMin 只会保留仍被失败契约接受的修改，不会诊断根因。失败契约过宽时，
结果可能很小，却不是你真正需要的复现。

## 1. 安装发布版本

创建隔离环境，并从经过发布检查的 GitHub Release 安装当前开发版本：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip

REPOMIN_VERSION=0.1.0.dev13
python -m pip install \
  "https://github.com/fly1d/repomin/releases/download/v${REPOMIN_VERSION}/repomin-${REPOMIN_VERSION}-py3-none-any.whl"

repomin --version
```

最后一条命令应输出 `repomin 0.1.0.dev13`。发布页提供 wheel、源码归档和
SHA-256 校验值。ReproMin 目前尚未发布到 PyPI。

## 2. 确认命令和严格失败契约

先在仓库根目录直接运行真实复现命令，至少成功复现两次：

```sh
cd /absolute/path/to/your/repository
python -m pytest -q tests/test_checkout.py
```

记录能唯一识别目标失败的消息和准确退出码。本指南使用以下示例：

```text
command:   python -m pytest -q tests/test_checkout.py
match:     AssertionError: checkout total mismatch
exit code: 1
```

不要只匹配 `FAILED`、`error` 或测试文件名，否则安装错误或其他断言也可能被
误认为目标失败。如果输出文本不稳定，可以改用 Java/Python 异常身份或精确的
进程失败签名；选择前请阅读 [Doctor 指南](DOCTOR.md)。

## 3. 用 Doctor 检查准备状态

设置真实源目录和一个尚不存在的同级输出目录：

```sh
source_dir="/absolute/path/to/your/repository"
output_dir="/absolute/path/to/your/repository-repro"
failure_command='python -m pytest -q tests/test_checkout.py'
failure_match='AssertionError: checkout total mismatch'
failure_exit_code=1

repomin doctor "$source_dir" \
  --command "$failure_command" \
  --match "$failure_match" \
  --exit-code "$failure_exit_code" \
  --output "$output_dir"
```

Doctor 检查源目录、reducer、输出路径和 backend，并在两个全新副本中执行失败
命令。ReproMin 不会向配置的输出目录导出结果，但失败命令本身仍可修改 backend
允许访问的任何资源；在默认 host backend 下，它拥有当前用户的权限。

只有 Doctor 以退出码 `0` 结束并显示 `2/2` baseline 通过时才继续。输出目录和
`<output>.repomin` sidecar 都不能已存在，也不能位于源仓库内部。

## 4. 执行有上限的缩减

首次尝试使用相同失败契约，并限制尝试次数和总时间：

```sh
repomin reduce "$source_dir" \
  --command "$failure_command" \
  --match "$failure_match" \
  --exit-code "$failure_exit_code" \
  --max-attempts 25 \
  --max-duration 300 \
  --output "$output_dir"
```

ReproMin 会导出预算内找到的最小已验证状态。大仓库第一次不必追求固定点；先看
有限预算的结果，再决定是否增加限制。需要强制保留 oracle 脚本、许可证或锁文件
时使用 `--keep RELATIVE_PATH`；只有当失败契约能拒绝无效内容时，才用
`--text-file RELATIVE_PATH` 对指定 UTF-8 文件按行缩减。

## 5. 验证并重放结果

payload 和证据 sidecar 是两个独立目录：

```text
<output>/                         缩减后的仓库
<output>.repomin/report.json     机器可读证据
<output>.repomin/REPOMIN.md      面向人的摘要
```

先在不执行失败命令的情况下验证报告和 payload：

```sh
repomin report validate \
  "${output_dir}.repomin/report.json" \
  --payload "$output_dir" \
  --format markdown
```

成功结果应包含 `payload_fingerprint_verified: true`。同时检查
`payload_fingerprint_mode`：`exact` 包含已记录的文件系统元数据；`content`
表示路径、条目类型、文件内容和符号链接目标一致，但没有证明元数据一致，artifact
传输改写元数据是常见原因之一。

检查 `report.json` 中记录的命令后，再明确允许在两个全新副本中重放：

```sh
repomin report replay \
  "${output_dir}.repomin/report.json" \
  --payload "$output_dir" \
  --runs 2 \
  --yes
```

验证指纹不会执行命令；replay 只证明当前环境仍符合配置的失败契约。两者都不能
证明代码正确或找到根因。

## 6. 让结果真正产生价值

人工检查后，把脱敏副本链接到对应 bug，加入回归测试，或保留为调试和依赖升级
样例。保留未经修改的 payload 和 sidecar 作为证据副本；如果为了 issue 增加
README 或恢复展示文件，请说明它已不同于指纹对应的版本，并重新执行失败命令。

不要公开凭据、私有 URL、专有源码、客户数据、原始日志、含密钥的命令或环境变量
值。可以在 [Show and tell](https://github.com/fly1d/repomin/discussions/new?category=show-and-tell)
分享成功、受阻或结论不明确的尝试。对于公开且许可证清晰的仓库，也可以在
[pilot issue #11](https://github.com/fly1d/repomin/issues/11) 提供 revision、复现命令、
失败签名和期望帮助，由维护者协助完成一次有限预算的试验。

默认 host backend 不是沙箱。只对可信仓库和命令使用它；Docker 可以缩小访问
范围，但也不是完整安全边界。处理第三方代码或分享结果前，请阅读
[安全说明](../SECURITY.md)。其他生态示例、配置、GitHub Action 和报告文档见
[文档索引](README.md)。
