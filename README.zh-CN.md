# Anchor Memory 中文教程

给 AI 伴侣、AI 助手用的长期记忆系统。它把聊过的事存进一个 SQLite 文件，下次对话前按意思和关键字把相关的旧事找回来，还会自己慢慢整理：常想起的变"热"，太久不碰的变"冷"，相关的记忆之间连成网。

这是一个每天都在跑的真实系统的公开版（2026 年 2 月起持续运行），私人数据和钥匙都已去掉。上游是 Limen 的 [Anchor Memory](https://github.com/limen-threshold/anchor-memory)，这里在它基础上做了大量改动，并加了硅基流动支持，国内能直接用。

仓库地址：<https://github.com/facc33176-rgb/Tidemark-anchor-memory->

---

## 它能做什么

- **记住**：一句话写进来，自动打标签（领域、状态、类型、动作、热度），同时建全文索引和向量索引。
- **想起来**：找的时候几路一起找（关键字 BM25、向量相似度、记忆网扩散、时间判断），合在一起再用重排模型排一遍；没有够格的就宁可不给，不硬凑。
- **连成网**：记忆之间有两种线，一种会传"热度"（想起 A 会顺带想起 B），一种只记意思不影响召回。
- **信念**：维护少量核心看法，每条都有证据支撑，只出现在简报里，不会篡改记忆本身。
- **自己整理**：每晚衰减旧热度、清理孤立的线、修弱连接。SQLite 是唯一的真相，向量库和图库坏了都能从 SQLite 重建。

两种用法：程序里直接调 **REST 接口**（默认 `127.0.0.1:8765`），或者让 Claude 之类的模型通过 **MCP** 自己用（默认 `127.0.0.1:8768/mcp`，十个工具：简报、存、搜、聊天原文、编辑、长文、信念、夜间整理、审连线等）。

---

## 准备

- Linux 或 macOS（Windows 建议用 WSL）
- Python 3.10 到 3.12（3.13 暂不支持）
- 内存建议 2G 起
- 可选：一个硅基流动的 API key（ https://cloud.siliconflow.cn ），用来做向量和重排。不填也能跑，只是只剩关键字检索，效果差很多

## 1. 安装

```bash
git clone https://github.com/facc33176-rgb/Tidemark-anchor-memory- anchor-memory
cd anchor-memory
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[server,chroma]"     # 接口 + 向量库；想要记忆网的图库再加 kuzu： ".[server,chroma,kuzu]"
pytest                               # 可选：跑一遍自带测试
```

## 2. 配置硅基流动（推荐）

钥匙放在仓库**外面**，不要提交进 git：

```bash
mkdir -p ~/.config/anchor-memory
cp examples/siliconflow/anchor.env.example ~/.config/anchor-memory/anchor.env
chmod 600 ~/.config/anchor-memory/anchor.env
nano ~/.config/anchor-memory/anchor.env       # 填上 SILICONFLOW_API_KEY=你的key
```

一个 key 同时管向量（`BAAI/bge-m3`）和重排（`BAAI/bge-reranker-v2-m3`）。硅基流动按量计费，记得在后台开余额提醒——欠费时向量召回会失败（见下面"两个实用习惯"里的保底办法）。

## 3. 启动

```bash
./examples/deploy/start.sh
```

看到 `REST on 127.0.0.1:8765` 和 `Streamable HTTP on 127.0.0.1:8768` 就起来了。数据默认在 `~/.local/share/anchor-memory/`（可用 `ANCHOR_DATA_DIR` 改）。另开一个终端试试：

```bash
curl -s localhost:8765/api/count
# {"count":0}
```

## 4. 写进去，再想起来

```bash
# 存一条（tag 自己定，比如 preference / event / person）
curl -s -X POST localhost:8765/api/store -H 'Content-Type: application/json' \
  -d '{"text":"她说下雨天最喜欢喝热可可，配一本推理小说","tag":"preference"}'

# 按意思找回来：policy=search 是主动搜，门槛低；budget 是最多返回几条
curl -s -G localhost:8765/api/recall --data-urlencode "query=下雨天喝什么" \
  --data-urlencode policy=search --data-urlencode budget=3
```

返回的 `results` 里每条有原文、标签、时间和打分明细（`score_breakdown`）。常用接口：

| 接口 | 用途 |
| --- | --- |
| `POST /api/store` | 存一条：`text`、`tag`，可带 `memory_id`（同 id 覆盖更新） |
| `GET /api/recall` | 召回：`query`、`budget`、`policy=search`（主动搜）或 `conversation`（对话自动浮现，门槛高、可能为空） |
| `GET /api/search` | 只做检索不带图扩散：`query`、`n` |
| `GET /api/recent` | 最近 n 条：`n`、`exclude_tag`、`random=true` 随机抽 |
| `GET /api/count` | 总数 |
| `POST /api/remove` | 删除（进回收站）：`memory_id`；`/api/trash` 看、`/api/restore` 恢复 |

把召回的记忆真正用进对话以后，可以调 `POST /api/heat` 给它们加一点热度，常被想起的会越来越容易浮现。

## 5. 接到 Claude

**Claude Code（本机）**：

```bash
claude mcp add --transport http anchor http://127.0.0.1:8768/mcp
```

之后在对话里说"先看看记忆里关于我的事"，Claude 会自己调 `briefing`、`search_memory`、`store_memory`。

**claude.ai 网页、手机 App 的自定义连接器**：需要一个公网 https 地址，所以要把 8768 端口通过反向代理或 Cloudflare Tunnel 暴露出去。这个服务**本身没有登录验证**，直接暴露等于谁都能读写你的记忆——务必在前面加一层鉴权（比如 Cloudflare Access、带口令的反向代理，或者一个随机的长路径再加口令），不要裸奔。

**自己写的程序**：直接调第 4 步的 REST 接口，或者用 `examples/client/anchor_client.py`。

## 6. 常驻、每晚整理和备份

三选一让它常驻：

```bash
pm2 start examples/deploy/ecosystem.config.js && pm2 save        # pm2
# 或 systemd 用户服务：见 examples/deploy/anchor-memory.service 文件头的说明
```

每晚整理和每天备份，照 `examples/deploy/crontab.example` 加进 `crontab -e`。

所有记忆都在 `memories.db` 一个文件里。搬家只需要停服务、拷这个文件；向量库和图库可以重建：

```bash
python scripts/rebuild_projections.py --source-db ~/.local/share/anchor-memory/memories.db --output-dir ./projection-rebuild
```

## 7. 两个实用习惯（我们在真实使用里踩出来的）

`examples/client/anchor_client.py` 只用 Python 标准库，可以直接抄进自己的项目：

- **写之前先查重**：定时总结对话时，同一件事很容易被写好几遍。`store()` 会先召回几条相似的，字面重合度够高就跳过。
- **向量挂了也别什么都想不起来**：key 欠费、网络断了的时候，`recall()` 自动退回到关键字扫描最近的记忆（整词命中 3 分、中文两字片段命中各 1 分），并在结果里标明 `fallback: keyword`，让模型知道这是保底结果。

## 常见问题

**启动报 `No module named 'mcp.server.fastmcp'`**：装到了 mcp 2.x。本仓库已把依赖钉在 `mcp<2`，重新 `pip install -e ".[server]"` 即可。

**换了向量服务商，搜出来的东西很怪**：不同模型的向量不能混用。设一个新的 `ANCHOR_CHROMA_COLLECTION`，或者用上面的重建脚本重建。

**国内连不上 Voyage**：默认重排是 Voyage，国内用第 2 步的硅基流动配置就行。

**`policy=conversation` 经常返回空**：这是故意的，对话里只在真有关系时才浮现记忆。主动搜用 `policy=search`。

**可以给别人用吗**：可以，但这是 AGPL-3.0 许可——你改了代码并通过网络给别人提供服务，就要把改过的源码也公开。上游早期版本的 MIT 声明保留在 `NOTICE`。

## 安全提醒

- 两个端口默认只听本机（127.0.0.1），保持这样；要远程用就加鉴权。
- 钥匙只放在 `~/.config/anchor-memory/anchor.env` 这类仓库外的文件里。
- `memories.db` 里是最私密的东西，备份文件同样要保管好。
