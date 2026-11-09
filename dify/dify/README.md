# 月球海关

你接管月球海关的值班台。每批货物都有一份报关线索和一条相关法规。读完线索后，决定放行、隔离还是退运。一个班次有五起案件，处理完就会看到成绩。相同班次编号会恢复进度，换一个编号可以重新开始。

Dify 工作流收集表单、区分查验和裁决、向内部服务发请求并展示报告。服务在 seekdb 中查询案件与法规，按班次记录每起案件的第一次裁决。已判案件再次提交时返回存档裁决，避免重复计分。判定依据来自实际的 SQL 查询。案件和法规是本项目原创示例，首次启动时自动写入数据库。

## 在 Dify 中运行

需要 Docker Engine 和 Docker Compose，或者 Docker Desktop。使用 Dify 1.14.2、Python 3.12 和 pylibseekdb 1.4.0.post2。Dify 自身按官方配置启动，作品使用独立的 seekdb 数据卷。工作流使用 Dify 内置节点，导入后即可测试。模型、插件和 API Key 都可留空。

1. 在本仓库的 `dify/` 目录运行以下命令。Dify 源码会放到作品仓库旁边的 `dify-upstream` 目录。官方容器运行环境见 [Dify 安装文档](https://docs.dify.ai/en/self-host/deploy/quick-start/docker-compose)。

   ```bash
   export DIFY_LUNAR_DIR="$PWD"
   git clone --depth 1 --branch 1.14.2 https://github.com/langgenius/dify.git ../../dify-upstream
   cd ../../dify-upstream/docker
   cp -n .env.example .env
   docker compose -f docker-compose.yaml -f "$DIFY_LUNAR_DIR/compose.yml" up -d --build
   docker compose -f docker-compose.yaml -f "$DIFY_LUNAR_DIR/compose.yml" logs lunar-customs
   ```

2. 日志出现 `Lunar Customs is ready` 后打开 [Dify 初始化页面](http://localhost/install)。建立本地管理员账号，进入「工作室」，点「导入 DSL 文件」，选择本目录的 `lunar-customs.yml`。
3. 打开「月球海关」，点击「运行」测试。也可以发布后点「运行应用」，在应用页面按下方样例填写表单。

Compose 将游戏服务接入 Dify 的内部代理网络，服务端口无需映射到宿主机。Squid 配置允许工作流访问 `lunar-customs:8080` 的两个 POST 路由。应用以班次编号区分游戏存档，适合在本地或受控的评审环境试玩。

## 验证样例

选择一个未用过的班次编号，比如 `demo-01`。保持编号不变，按顺序运行下列操作。

| 操作 | 案号 | 裁决 | 预期输出 |
|---|---|---|---|
| 查看线索 | 留空 | 留空 | 第 1 案「祖母的月桂」，包含规则 `P-01` |
| 提交裁决 | 1 | 隔离 | 判定正确，已处理 1 案，答对 1 案 |
| 提交裁决 | 1 | 放行 | 返回存档裁决「隔离」，已处理 1 案 |
| 查看线索 | 留空 | 留空 | 第 2 案「一公斤月亮」 |
| 提交裁决 | 2 | 隔离 | 判定有误，已处理 2 案，答对 1 案 |

在第 1 案裁决后，可用以下命令重启游戏服务。继续使用相同班次编号，应看到第 2 案和已答对 1 案。用新班次编号查验，应从第 1 案开始。

```bash
docker compose -f docker-compose.yaml -f "$DIFY_LUNAR_DIR/compose.yml" restart lunar-customs
```

## 本地验证记录

在 macOS arm64 上，用 Dify 1.14.2 锁定的 Graphon 0.4.0 执行这份 DSL。两个 HTTP 节点向回环地址上的游戏服务发出真实 POST 请求，游戏服务使用 pylibseekdb 1.4.0.post2 持久化数据。测试覆盖了查验、未开放的案号、正确与错误裁决、重复裁决、班次隔离、服务重启和最终结算。

```text
workflow inspection: PASS
invalid future case: PASS
workflow judgment: PASS
duplicate judgment: PASS
independent shift: PASS
resume after restart: PASS
wrong answer: PASS
case 3: PASS
case 4: PASS
case 5: PASS
final score: PASS
值班结束。你的成绩是 4/5。换一个班次编号即可重新挑战。
```

用 Docker Compose v2.40.3 验证了部署配置能与 Dify 1.14.2 的配置合并，游戏服务和代理都接入了内部网络。Docker 中的 Dify Studio 导入和发布仍待实机验收。

在支持 pylibseekdb 的 Linux 或 macOS arm64 上，可以运行以下命令复现引擎测试。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt 'graphon==0.4.0' 'PyYAML==6.0.3'
python verify.py
```
