# ReproMin 中文快速开始

ReproMin 会在每次候选修改后重新执行失败命令，只保留仍能复现原始失败的修改。它适合把一个过大的失败仓库缩减成便于提交 issue 或制作回归测试的最小复现目录。

## 安装

ReproMin 需要 Python 3.9 或更高版本，目前从 GitHub Release 安装，还没有发布到
PyPI。建议先使用虚拟环境，避免修改系统 Python：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
REPOMIN_VERSION=0.1.0.dev8
python -m pip install \
  "https://github.com/fly1d/repomin/releases/download/v${REPOMIN_VERSION}/repomin-${REPOMIN_VERSION}-py3-none-any.whl"
python -m repomin --version
```

当前版本应显示 `repomin 0.1.0.dev8`。发布页同时提供 wheel 和源码归档，以及对应的
SHA-256 校验值；需要供应链校验时，请先核对
[发布页](https://github.com/fly1d/repomin/releases/tag/v0.1.0.dev8)再安装。wheel 不需要
本地构建，首次使用更快。

本页后面的 `report replay`、传输 fingerprint 和 Markdown 摘要功能已包含在
`v0.1.0.dev8` 发布包中。
参与 pilot 前仍请阅读[真实失败 pilot 指南](REAL_FAILURE_PILOT.md)，并按其中的隐私和
安全边界检查报告与 payload。

如果你正在开发 ReproMin，也可以在仓库根目录创建虚拟环境后运行
`python -m pip install -e ".[dev]"`，这样会同时安装测试、检查和发布工具；只需要
包本身时，`python -m pip install -e .` 即可。

## 最小示例

下面的命令从一个干净的临时目录创建失败复现，然后只缩减 `input.txt`。整段示例可直接在 Bash 或 Zsh 中运行：

```sh
demo_dir="$(mktemp -d)"
cd "$demo_dir"

mkdir case
cat > case/reproduce.py <<'PY'
from pathlib import Path

text = Path("input.txt").read_text(encoding="utf-8")
if "keep-me" not in text:
    print("DIFFERENT_FAILURE")
    raise SystemExit(2)
print("ORIGINAL_FAILURE")
raise SystemExit(1)
PY

printf 'keep-me\nremove-me\n' > case/input.txt

repomin case \
  --command 'python3 reproduce.py' \
  --match 'ORIGINAL_FAILURE' \
  --adapter none \
  --source-reducer none \
  --text-file input.txt \
  --output "$demo_dir/reduced"
```

完成后，`reduced/input.txt` 仍包含 `keep-me`，无关的 `remove-me` 可以被删除。缩减后的仓库在 `reduced/`，证据报告在旁边的 `reduced.repomin/`：

- `report.json`：机器可读的运行统计、oracle 规则和环境信息；
- `REPOMIN.md`：面向人的缩减摘要。

可以用下面的命令查看结果：

```sh
grep -n 'keep-me' "$demo_dir/reduced/input.txt"
cat "$demo_dir/reduced.repomin/REPOMIN.md"
```

## 验证证据报告

可以在不重新执行失败命令的情况下，验证刚才生成的报告和缩减目录：

```sh
repomin report validate "$demo_dir/reduced.repomin/report.json" \
  --payload "$demo_dir/reduced"
```

`--payload` 会在报告包含指纹时检查缩减目录是否仍与已记录的证据一致。脚本或 CI
也可以使用机器可读的结果：

```sh
repomin report validate "$demo_dir/reduced.repomin/report.json" \
  --payload "$demo_dir/reduced" --json
```

这个 JSON 摘要包含 oracle 类型、缩减前后文件/字节数、保留比例、holdout 计数和预算状态，
但不会包含命令、匹配正则、日志或环境变量名称和值，适合贴到 CI 记录或用户反馈中。提交前仍要
单独检查 payload 和 `report.json` 是否含有敏感信息。

如果需要贴到 Issue 或 CI 摘要中，也可以生成确定性的 Markdown 摘要：

```sh
repomin report validate "$demo_dir/reduced.repomin/report.json" \
  --payload "$demo_dir/reduced" --format markdown
```

该表只包含经过转义的版本、后端、oracle 类型、大小、缩减计数、holdout 和指纹状态，
不会输出命令、正则、日志、路径或环境变量信息。

## 比较多次缩减证据

如果同一个失败流程运行了多次，可以按命令行给出的顺序比较两个或更多已经验证过的
`report.json`：

```sh
repomin report compare \
  /tmp/baseline.repomin/report.json \
  /tmp/candidate.repomin/report.json \
  --label baseline --label candidate \
  --format markdown
```

比较命令只读取并验证报告结构，不读取 payload、不执行报告中的 `command`，也不联网。
输出有独立的 `comparison_schema_version`，并标记 `descriptive_only: true`；它只展示版本、
后端、oracle 类型、缩减前后大小、保留比例、尝试/接受/缓存计数、预算、holdout、阶段覆盖率
及相邻差值。若版本 provenance、后端、并发/超时、oracle、源大小、抽样或 holdout 配置发生变化，
会列出上下文警告。
标签只用于显示，必须是短且唯一的 ASCII 标识。这个结果不是性能趋势、正确性证明或因果结论；
性能历史请使用离线 benchmark 工具。

验证器检查报告结构、阶段和 holdout 统计，以及可用的 payload 指纹；它不会重新运行
`--command`，也不等于证明代码或失败根因的正确性。报告格式错误、统计不一致或指纹不匹配
时，命令以退出码 `2` 结束。

## 在全新副本中重放失败

先检查报告中记录的命令，再显式允许执行：

```sh
repomin report replay "$demo_dir/reduced.repomin/report.json" \
  --payload "$demo_dir/reduced" \
  --runs 2 \
  --yes
```

每次运行都会从原 payload 创建独立临时副本，命令不会直接在
`reduced/` 中执行。现代报告会精确记录 oracle 配置、超时和 payload
树指纹；旧报告若无法区分普通非零退出与精确退出码，会要求显式提供
`--exit-code N`，而不是自行猜测。

报告本身没有签名，里面的 `command` 可能执行任意代码。`--yes` 只是确认你已
审阅命令，并不提供沙箱。退出码 `0` 只表示当前环境下所有 replay 都匹配 oracle，
不是正确性、根因或生产可靠性证明。详细边界见[重放指南](REPLAY.md)。
从 CI artifact 下载后，如果存储系统改写了文件时间，结果可能标记为
`content` fingerprint mode；这表示内容和路径一致，但不再声称文件系统元数据完全一致。

## Oracle 是什么

`--command` 是失败复现命令，`--match` 是必须继续出现在 stdout 或 stderr 中的正则表达式。上面的示例要求命令继续输出 `ORIGINAL_FAILURE` 并以非零状态退出。

匹配成功只说明“在记录的环境和抽样规则下，失败现象仍被复现”。它不是代码正确性证明，也不能证明这个正则表达式一定识别了唯一的根因。对于不同类型的失败，可以使用 `--exit-code`、`--process-failure` 或 Java/Python 异常签名选项。

## 安全边界

默认的 `host` backend 会直接在当前主机执行 `--command`，不是沙箱。只对你信任的复现命令使用默认设置；不要把包含恶意脚本或不可信依赖的仓库交给 host backend。Docker backend 可以减少访问范围，但也不是完整的安全边界，仍需由使用者配置镜像和资源限制。

## 下一步

- 查看 [英文 README](../README.md) 了解所有 CLI 参数和高级 reducer；
- 查看 [示例目录](EXAMPLES.md) 了解 Maven、Python、Node、MSBuild 等项目；
- 查看 [架构说明](ARCHITECTURE.md) 了解 oracle、checkpoint 和 reducer 的边界；
- 贡献代码前阅读 [贡献指南](../CONTRIBUTING.md)。
