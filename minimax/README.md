# 规有所依：SDD 上下文与任务恢复

以一个库存补货模块演示从规格到代码变更的完整过程。任务 `SDD-001` 要求把“库存低于阈值”修正为“库存等于或低于阈值也补货”，并保留停产过滤、结果顺序和空库存行为。

| 组件 | 在样例中的作用 |
| --- | --- |
| seekdb | 把规格、代码和测试导入嵌入式数据库，按任务关键词和资料类别查询，将命中的正文和路径交给 MiniMax。 |
| MiniMax | 根据检索结果提出一处代码修改。程序验证修改范围，在临时工作副本上应用该修改，并执行原样保留的测试。 |
| PowerContext | 把已完成事项、待办事项和验证结果写入独立的任务范围。新进程从持久化状态读回记录，并把它加入开发上下文。 |

样例的必要来源是 `demo/specs/restock.md`、`demo/inventory.py`、`demo/tests/test_feature.py` 和 `demo/tests/test_regression.py`。查询默认使用 `restock threshold`，库存导出规格是无关资料。每次查询前重新导入当前文件，因此规格更新会反映到下一次查询中。输出包含来源路径、原文和内容校验值。

验证样例把补货阈值设为 5，输入库存量为 6、5、2 的三个正常 SKU。规格要求返回库存量为 2、5 的两个 SKU，顺序按库存量从少到多。另两项回归测试覆盖停产过滤、同库存量时的 SKU 排序和空库存。

## 运行

使用 Python 3.12、可运行 `pylibseekdb` 的环境，以及可调用 MiniMax 文本 API 的账号。已在 macOS 26.3.1 / Apple Silicon 上验证 PowerContext 1.5.0 和 seekdb 1.4.0 的本地流程。

```sh
cd minimax
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

.venv/bin/python sdd.py verify
.venv/bin/python sdd.py resume
.venv/bin/python sdd.py context
```

`verify` 使用真实 seekdb，确认原始实现的边界用例因断言失败（退出码 1），两项回归测试通过（退出码 0），并将进度写入 PowerContext。另起进程执行 `resume` 应看到相同的任务范围和 `prepared` 状态。`context` 会打印重新查询的资料和从该范围读回的状态，也可用 `--query 'export UTC'` 检查查询词如何改变来源选择。

执行真实代码变更时，在终端运行下列命令并在隐藏输入提示中输入 API Key。也可以通过 `MINIMAX_API_KEY` 提供 Key。程序通过 MiniMax 的 OpenAI 兼容 HTTP API v1 调用 `MiniMax-M3`，默认连接中国区接口。国际区账号可指定 `--region global`，也可以用 `--model` 指定账号支持的 MiniMax 模型。

```sh
.venv/bin/python sdd.py run
.venv/bin/python sdd.py resume
```

使用支持 M3.1 Flash 的 M Plan 订阅 Key 时，给 `run` 加上 `--model MiniMax-M3.1-Flash`。

成功时，三个测试全部通过，终端打印生成的证据目录及 MiniMax 响应 ID。证据目录包含发送给模型的任务上下文、修改后的代码快照、统一 diff，以及包含真实接口元数据、查询记录和测试退出码的报告。重启后的 `resume` 应读出 `verified` 状态和相同的运行 ID。API Key 和模型内部推理不会写入记录。

## 复验样例

用同一套测试检查仓库中的基线和本次证据中的修改后代码。基线预期运行三项测试并出现一项补货边界断言失败，修改后预期三项通过。

```sh
replay_dir=$(mktemp -d)
cp -R demo "$replay_dir/"
python_bin="$PWD/.venv/bin/python"
(cd "$replay_dir/demo" && "$python_bin" -m unittest discover -s tests -v)
cp evidence/28c55338e4f47b6d/inventory.py "$replay_dir/demo/inventory.py"
(cd "$replay_dir/demo" && "$python_bin" -m unittest discover -s tests -v)
```

验证规格更新时，可在源码副本的 `demo/specs/restock.md` 追加一条带 `threshold` 的说明，再执行 `context`。`sources` 应包含更新后的原文，相应来源的 `sha256` 也会变化。基线代码和测试位于 `demo/`，重新开始时使用同一套基线文件和一个空的 `--work-dir`，运行 `verify` 初始化数据库及任务状态。

检索按英文单词命中数在规格、代码和测试类别内排序。样例的编辑器只接受库存阈值比较运算符的单行修改，验证后的代码写入证据目录。

## 提交材料

本次真实调用的证据位于 `evidence/28c55338e4f47b6d/`。

- `context.json` 保存实际发送的规格、代码和测试正文，以及上一次运行写入的 `prepared` 状态。
- `report.json` 保存 MiniMax-M3 的响应 ID `074c3ac4a544bf6542cb357c33003c58`、实际查询、来源校验值和测试日志。本次调用用量为 1,641 Token。
- `change.patch` 和 `inventory.py` 保存 MiniMax 生成的代码差异及修改后快照。补货条件由 `< threshold` 改为 `<= threshold`。
- `resume.json` 保存另一进程读取的 PowerContext `verified` 状态，与报告具有相同的运行 ID。

基线代码和原样执行的测试位于 `demo/`。本次修改前的边界测试因缺少等于阈值的 SKU 而失败，两项回归测试通过；修改后三项测试全部通过。报告中的修改前后 SHA-256 分别对应基线文件和证据中的修改后快照。

复验已提交的代码和日志可按上文操作。需要重新生成开发记录时，执行 `verify`、`run` 和 `resume`，将新运行的证据目录复制到 `evidence/`，并保存相应的 `resume.json`。

从干净状态重跑时，所有子命令使用相同的一个新 `--work-dir` 目录。默认工作目录是 `.work/`，包含数据库和运行状态，由 Git 忽略。
