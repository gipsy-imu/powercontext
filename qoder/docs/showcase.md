# 研迹 · 项目展示页文案

> 用途：OceanBase 2026 赛题项目展示页。以下按展示页区块组织，可直接排版。

## 一句话介绍

**研迹**：把散落的时间序列实验笔记变成可全文检索的科研知识库——导入 Markdown，
seekdb 建中文全文索引，检索、筛选、溯源一步到位。

## 问题

做复现实验时，最大的信息黑洞往往不是代码，而是笔记：

- 采样窗口选多大、长度怎么对齐、类别不平衡怎么处理——结论散落在几个学期的
  Markdown/TXT 文件里，`grep` 搜不动中文，也带不出上下文；
- 两份报告里都有「采样窗口」小节，哪个是 4 秒窗口、哪个是 128 时间步？没有来源
  定位就想不起来；
- 笔记里的中英混排术语（`macro F1`、`length alignment`）用普通关键词匹配经常漏检。

## 主要功能

1. **一键导入示例 / 批量导入自有笔记**：UTF-8 Markdown 或 TXT，自动保留笔记标题、
   段落标题、来源文件和原文，按内容去重，重复导入不产生脏数据。
2. **真实 seekdb 全文检索**：IK 中文分词 + 英文切词，同一份中英混排正文两个方向都能
   命中；结果展示段落标题、高亮匹配片段、来源与相关度。
3. **按报告、按标签筛选**：下拉即选，与全文条件组合在一条 SQL 内完成。
4. **点开来源看原段落**：检索片段可展开原始段落，方便回到实验现场。
5. **故障可见**：数据库不可用时界面直接显示连接/初始化错误原文，不白屏、不猜测。

## seekdb 在这里的作用

- `notes` 表上的 `FULLTEXT KEY ft_search (title, section, body) WITH PARSER IK`
  由 seekdb 维护倒排索引，中文分词在库内完成；
- 查询走 `MATCH(title, section, body) AGAINST(?)`，相关度评分与命中判定全部发生在
  seekdb 服务端，应用层只做片段高亮和展示；
- 部署使用官方 `oceanbase/seekdb` 镜像（MySQL 协议，2881 端口），也支持连接已有实例，
  评测环境可复现。

## 复现方式

```bash
# 检出比赛提交后，进入仓库的 qoder 目录
cd qoder
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export SEEKDB_HOST=127.0.0.1 SEEKDB_PORT=2881 SEEKDB_USER=root \
       SEEKDB_DATABASE=yanji
# 如需设置密码，先在当前终端为 SEEKDB_PASSWORD 赋值
docker compose up -d --wait seekdb
.venv/bin/python -m app.cli demo     # 初始化 + 导入示例 + 全部检索断言
.venv/bin/python -m app.cli serve    # 打开 http://127.0.0.1:8030
```

无 Docker 环境可直接对已有 seekdb 实例执行以上命令。验证覆盖：中文查询「采样窗口」、
中英混排「macro F1」、纯英文 `length alignment`、报告/标签筛选、重复导入幂等、无匹配空态，
全部 PASS（输出见 `qoder/evidence/verify-output.txt`）。

## 运行截图占位

- 中文检索 + 原文展开：`qoder/evidence/01-demo-search-cn.png`
- 英文检索：`qoder/evidence/02-search-en.png`
- 无匹配空态：`qoder/evidence/03-empty-result.png`
- 数据库错误提示：`qoder/evidence/04-db-error-banner.png`

## 体验入口

- [项目仓库、运行说明和验证样例](https://github.com/gipsy-imu/powercontext/tree/codex/qoder-study-search/qoder)
- 在线体验（已公开）：https://yanji-notes-1a9j7rsnk1g.qoder.zone
- 本地体验按上方命令启动后打开 `http://127.0.0.1:8030`。
