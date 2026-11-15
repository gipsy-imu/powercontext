# 研迹（yanji）

面向学生和实验复现的科研笔记检索工具：导入一批 UTF-8 的 Markdown/TXT 笔记，段落写入真实
seekdb 并建全文索引（IK 中文分词），检索支持按报告或标签筛选，结果显示标题、匹配片段和来源，
点开可查看原段落。数据库不可用时界面会明确显示连接或初始化错误。

OceanBase 2026 赛题作品，赛题原文见 [官方语雀](https://oceanbase.yuque.com/org-wiki-obtech-vh7w9r/hc3pio/uygu3273xzlrcmrm)。

## 目录结构

```
qoder/
├── app/                # Python 包
│   ├── config.py       # 读取 SEEKDB_* 环境变量（或 YANJI_CREDENTIALS_FILE 指向的 JSON）
│   ├── db.py           # 建库建表：notes 表 + FULLTEXT KEY ft_search (title, section, body) WITH PARSER IK
│   ├── parser.py       # Markdown/TXT → 段落记录（标题、段落标题、来源、标签、原文、去重 UID）
│   ├── search.py       # 幂等导入、MATCH…AGAINST 检索、筛选、片段高亮
│   ├── expectations.py # 验证查询与预期命中（README 与脚本共用）
│   ├── cli.py          # init / import / search / verify / serve / demo 子命令
│   ├── web.py          # Flask 中文界面
│   └── templates/index.html
├── samples/notes/      # 4 篇内置示例笔记（时间序列分类主题）
├── scripts/verify_search.py
├── docker-compose.yml  # 官方 oceanbase/seekdb 镜像部署（MySQL 协议 2881）
├── evidence/           # 真实运行截图与命令输出记录
├── requirements.txt    # 固定版本，Python 3.9+ 可运行
└── .env.example        # 环境变量占位示例
```

## 快速开始

### 1. 启动 seekdb（二选一）

**A. 官方 Docker 镜像（评测环境推荐）**

```bash
cd qoder
export SEEKDB_PORT=2881
# 如需设置密码，先在当前终端为 SEEKDB_PASSWORD 赋值
docker compose up -d --wait seekdb
```

**B. 使用已有 seekdb 实例**

直接记下它的 host/port/user/password，跳过本步。本次开发验证使用的就是本机一个已在
2881 端口运行的 seekdb 1.4.0（macOS arm64 官方包）。

### 2. 安装依赖并配置连接

```bash
# 在 qoder 目录中运行
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

export SEEKDB_HOST=127.0.0.1
export SEEKDB_PORT=2881
export SEEKDB_USER=root
# 已设置数据库密码时，在当前终端配置 SEEKDB_PASSWORD
export SEEKDB_DATABASE=yanji
```

也可以把这些字段放进一个 JSON 文件并设置 `YANJI_CREDENTIALS_FILE=/path/to/credentials.json`。

### 3. 初始化 → 导入 → 验证

```bash
.venv/bin/python -m app.cli init      # 建库建表 + IK 全文索引
.venv/bin/python -m app.cli import    # 导入 samples/notes 的 4 篇示例（12 段）
.venv/bin/python -m app.cli verify    # 运行下方全部预期查询断言
```

或一条命令跑完三步：

```bash
.venv/bin/python -m app.cli demo
```

### 4. 打开界面

```bash
.venv/bin/python -m app.cli serve     # http://127.0.0.1:8030
```

打开即可点「▶ 运行内置示例」完成初始化 + 导入并演示检索；也可以直接搜索，或通过页面上传
自己的 `.md`/`.txt` 笔记。

## seekdb 在哪里执行索引和查询

- **索引**：段落写入 `yanji.notes` 表时由 seekdb 自动构建全文索引
  `FULLTEXT KEY ft_search (title, section, body) WITH PARSER IK`（见 `app/db.py` 的 `SCHEMA_SQL`）。
  中文由 IK 分词，英文按空白/标点切分，中英混排段落两侧都可被检索。
- **查询**：`app/search.py` 的 `search()` 直接下发
  `MATCH(title, section, body) AGAINST(?)` 并按相关度排序，相关度计算和词匹配全部发生在
  seekdb 服务端；Python 端只做结果组装和片段高亮（高亮位置由本地按查询词定位，
  不改变命中结果）。
- **筛选**：按报告 = `WHERE title = ?`，按标签 = `WHERE tags LIKE '%,标签,%'`，与全文条件组合在同一条 SQL。

## 示例笔记与预期命中

示例围绕时间序列分类复现实验，覆盖四类场景：

| 场景 | 体现 |
| --- | --- |
| 中英文搜索 | 「采样窗口」「macro F1」命中中文段落；`length alignment` 同时命中英文笔记和含该术语的中文段落 |
| 同名段落 | 报告 A 与 B 都有「采样窗口」「长度对齐」小节，结果靠来源文件区分 |
| 重复导入 | 段落 UID = sha256(来源+段落标题+原文)，重复执行 `import` 显示「新增 0」 |
| 无匹配 | 查询不存在的词返回 0 条，界面显示空态提示 |

验证查询与预期（`.venv/bin/python -m app.cli verify` 会逐条断言）：

| 查询 | 预期命中（标题 / 段落） | 实际结果 |
| --- | --- | --- |
| `采样窗口` | 报告 A/采样窗口、报告 B/采样窗口（同名段落双命中，相关度 0.81、0.81） | PASS |
| `macro F1` | 报告 A/类别不平衡、A/实验结果、B/类别不平衡 等 7 段 | PASS |
| `length alignment` | Reading notes/Length alignment、报告 A/长度对齐（中文段落含英文术语） | PASS |
| `紫色独角兽观测 zz-qux-unicornium` | 0 条，界面空态 | PASS |
| `采样窗口` + 报告=报告 A | 仅报告 A 的 3 段 | PASS |
| `类别不平衡` + 标签=复现实验 | 仅报告 B 的 1 段 | PASS |
| 再次导入全部示例 | 新增 0、已存在 12 | PASS |

2026-11-15 在本机的完整命令与输出记录在 [evidence/verify-output.txt](evidence/verify-output.txt)。

## 实际运行截图

- `evidence/01-demo-search-cn.png` — 一键示例 + 中文检索「采样窗口」，高亮片段、来源、标签、相关度，原段落已展开
- `evidence/02-search-en.png` — 英文检索 `length alignment` 命中 4 段
- `evidence/03-empty-result.png` — 无匹配空态
- `evidence/04-db-error-banner.png` — 凭据错误时的「数据库不可用」红色横幅（1045 Access denied 原文展示）

## 在线体验

展示站点已发布并开启公开访问：https://yanji-notes-1a9j7rsnk1g.qoder.zone

## Qoder 开发记录

Qoder UID：`01a1dd86-cf5c-792b-af7f-cea11a450507`

- [开发任务](evidence/qoder-prompt.md) 给 Qoder 的功能需求和验收样例。
- [实现和调试](evidence/qoder-development.png) Qoder 完成项目代码、验证输出和异常页面演示。截图中的 8031 页面用于演示数据库授权失败。正常流程的输出见 [验证日志](evidence/verify-output.txt)，本地应用入口为 `http://127.0.0.1:8030`。
- [站点预览与验证](evidence/qoder-site-preview.png) Qoder 补齐筛选和原文回查截图并检查展示素材。
- [Qoder Sites 发布记录](evidence/qoder-site-publish.png) Qoder 确认发布成功。发布时的私有访问随后已改为公开，网址见上方「在线体验」。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| 界面红色横幅「无法连接 seekdb」 | 确认 seekdb 进程/容器已启动、端口 2881 可达、`SEEKDB_*` 环境变量是否正确 |
| `Access denied (1045)` | 核对用户和密码。Docker 部署时，同一个 `SEEKDB_PASSWORD` 同时用于数据库首次初始化和应用连接 |
| 中文搜索无结果 | 确认表使用 IK 分词（新库执行 `python -m app.cli init` 即可） |
| 端口冲突 | `serve --port 8031` 换端口 |
