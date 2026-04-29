# VLA Paper Digest

面向 **Vision-Language-Action 路径生成 smoothness** 方向（动力学 jerk / acceleration + 语义 action chunk 一致性）的每日论文推送工具。

每天北京时间早 9 点，GitHub Actions 自动从 **Semantic Scholar / OpenReview / arXiv (cs.RO + cs.LG + cs.AI + cs.CV + cs.CL) / Hugging Face Daily Papers** 抓取最新论文，叠加 22 个重点实验室（Physical Intelligence / NVIDIA GEAR / DeepMind Robotics / BAIR / IRIS / DeepSeek / NVIDIA / Meta / OpenAI 等）的作者 + 品牌 + 机构专项搜索，做加权打分 + 实验室加成，按「**综合高分 × 4 + 经典轮播 × 1 = 5 篇**」精选每日推送，调用 GPT-5 生成结构化中文摘要，再通过 SMTP 发到你邮箱（可选：同时推送到微信）。

---

## 功能亮点

- **多源融合**：
  - Semantic Scholar 主力（按 venue / 关键词）
  - OpenReview 补 CoRL/ICLR/NeurIPS 的 accepted
  - arXiv 五大类（cs.RO / cs.LG / cs.AI / cs.CV / cs.CL）× 五条检索支线（主题关键词 / 作者 `au:` / 标题品牌 `ti:` / 机构 `all:` / 白名单 `id_list=`）
  - Hugging Face Daily Papers（每日精选）兜底 NVIDIA / DeepSeek / Meta / Google 等工业发布
- **22 个重点实验室追踪**（[`config/labs.yaml`](config/labs.yaml)）：
  - VLA / Robotics 业界：Physical Intelligence / NVIDIA GEAR / NVIDIA Research / Google DeepMind Robotics / Meta FAIR Robotics
  - VLA / Robotics 学术：Stanford IRIS / UC Berkeley BAIR·RAIL / CMU RI / MIT CSAIL / Tsinghua / Shanghai AI Lab
  - LLM 基础：OpenAI / Anthropic / DeepSeek / Meta AI / Qwen / ByteDance Seed / Moonshot / Zhipu / Mistral / Microsoft
  - 每个 lab 可配置 `arxiv_au`（作者列表）+ `title_keywords`（品牌名）+ `watchlist_ids`（强拉白名单）+ `tier` + `weight`
- **可配置的加权打分**：`相关性 × 0.5 + venue × 0.2 + 新鲜度 × 0.15 + 引用速度 × 0.15 + 实验室加成 + ⭐ Notable 作者加成`；tier-1 实验室加成默认 **+2.0**（10 分制下约 20% 提升），tier-2 按 weight 缩放。
- **简单 rerank 规则**：每天按综合分 rerank → 取综合高分 × 4 篇（默认，可调；同一实验室最多 2 篇，保证多样性）+ 1 篇经典轮播，共 5 篇。
- **去重历史**：`data/history.json` 记录已推过的 paperId，全局永久去重。
- **经典论文轮播**：`data/classic_papers.json` 预置 25 篇，按固定顺序循环推送。
- **GitHub Actions 全自动**：定时 + 手动触发；数据库 JSON 自动 commit 回仓库。

---

## 快速开始

### 一、配置 GitHub Secrets（部署后每天自动跑需要）

在你的 GitHub 仓库里：`Settings → Secrets and variables → Actions → New repository secret`

| Secret 名 | 作用 | 获取方式 |
|---|---|---|
| `SEMANTIC_SCHOLAR_API_KEY` | Semantic Scholar 主数据源 | 到 [Semantic Scholar API Key 申请页](https://www.semanticscholar.org/product/api#api-key-form) 填个简单的表（研究者用途一般当天通过） |
| `OPENAI_API_KEY` | GPT-4o-mini 摘要 | [OpenAI 控制台](https://platform.openai.com/api-keys) 创建 |
| `SMTP_HOST` | SMTP 服务器 | 163 填 `smtp.163.com`；Gmail 填 `smtp.gmail.com` |
| `SMTP_PORT` | SMTP 端口 | 强烈建议 `465`（SSL） |
| `SMTP_USER` | 发件邮箱完整地址 | 如 `you@163.com` |
| `SMTP_PASSWORD` | **授权码 / 应用专用密码**（不是登录密码！） | 见下方说明 |
| `EMAIL_TO` | 收件地址（可逗号分隔多个） | 如 `you@163.com,other@qq.com` |
| `WECHAT_PUSHPLUS_TOKEN` | **（可选）** PushPlus 微信推送 token | 扫码关注「pushplus 推送加」公众号 → [pushplus.plus](https://www.pushplus.plus) 获取 |
| `WECHAT_PUSHPLUS_TOPIC` | **（可选）** PushPlus 群组编码 | 一对多群推才需要，个人自推留空 |

> **关于 GitHub Actions**：这是 GitHub 自带的云端 CI/定时任务平台。你把代码 push 上去之后，仓库里 `.github/workflows/daily_digest.yml` 会被 GitHub 识别，按 cron 表达式 `0 1 * * *`（UTC 01:00 = 北京 09:00）**每天在 GitHub 服务器上自动跑一次** `python -m src.main`，无需任何本地机器。公共仓库完全免费，私有仓库每月 2000 分钟免费额度。

> **关于微信推送**：调用 [PushPlus](https://www.pushplus.plus) 第三方服务，对方把 HTML 内容通过其自建的「pushplus 推送加」公众号转发到你微信。没有 `WECHAT_PUSHPLUS_TOKEN` 就静默跳过，邮件继续发。微信推送失败也不影响邮件。

#### Gmail 应用专用密码（App Password）获取步骤

1. 登录 Google 账号 → [管理你的 Google 账号](https://myaccount.google.com/)
2. 左侧菜单「**安全性**」→ 先开启「**两步验证**」（必须）
3. 两步验证页面底部找到「**应用专用密码**」（或直接访问 [https://myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)）
4. 应用选「**邮件**」，设备选「**其他**」→ 填 `vla-paper-digest` → 点生成
5. 把弹窗里 **16 位密码**（形如 `abcd efgh ijkl mnop`）复制（空格可保留可去掉，两种都能用）
6. 粘贴到 `SMTP_PASSWORD` Secret

#### 163 邮箱授权码获取步骤

1. 登录 [mail.163.com](https://mail.163.com)
2. 右上角「**设置**」→「**POP3/SMTP/IMAP**」
3. 开启「**IMAP/SMTP 服务**」，按提示用手机发短信完成验证
4. 页面上会显示「**客户端授权密码**」（第一次开启时生成；忘了可以重置）
5. 粘贴到 `SMTP_PASSWORD`

### 二、推送到 GitHub

```bash
cd vla-paper-digest
git init && git add . && git commit -m "init vla paper digest"
git branch -M main
git remote add origin git@github.com:<YOUR>/<REPO>.git
git push -u origin main
```

### 三、首次运行（bootstrap）

到 GitHub 仓库的 **Actions** 页 → 选 `Daily VLA Paper Digest` → 点 `Run workflow` → `force_bootstrap = true` → `Run workflow`。

首次约 **10-20 分钟**（批量拉 2 年的顶会 + 25 篇经典论文 + OpenReview 补全）。之后 cron 每天 UTC 01:00（北京 09:00）自动跑，单次约 3-5 分钟。

---

## 本地调试

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # 手动填入所有值
python -m src.main --dry-run                       # 不发邮件，只打印今日 3 篇
python -m src.main --force-bootstrap --dry-run     # 强制走首次 bootstrap
python -m src.main --no-ai --dry-run               # 跳过 LLM 摘要
python -m src.main --quick --dry-run               # 冒烟测试：跳过 SS/OpenReview，只刷 arXiv
python -m src.main --print-next-steps              # 打印部署指南
pytest -q                                          # 跑单元测试
```

`--quick` 用途：本地快速验证打分 / 选择 / 邮件渲染是否正常，几秒即可跑完；仅基于已有 `data/paper_db.json` + 最新 arXiv preprint 做决策。CI 上永远不用这个开关。

#### 一次性数据迁移

如果你从早期版本升级（OpenReview 论文只有 `year` 没 `publication_date`），跑一次：

```bash
python -m scripts.migrate_fill_pub_date
```

把所有缺失 `publication_date` 的条目按 `year-01-01` 补齐。

---

## 调整关键词库

所有关键词 + 权重都在 [`src/config.py`](src/config.py) 的 `KEYWORDS` 里，按「类别 → 权重 + 词表」分组。修改后直接重跑，第二天会用新权重重新打分。

想换 venue 权重或新鲜度阶梯，也都在同一个文件：`VENUE_WEIGHTS`、`FRESHNESS_BUCKETS`、`COMPOSITE_WEIGHTS`。

## 增删经典论文

编辑 [`data/classic_papers.json`](data/classic_papers.json)。只需要写 `title`、`year`、`category`，首次运行时会自动从 Semantic Scholar 解析补全 `paper_id / abstract / authors / venue`。

---

## 数据流一览

```
Cron 01:00 UTC
      ↓
  main.py 入口
      ↓
  ┌── 是否 bootstrap？ ──┐
  │ 是：SS 按 venue + OpenReview 2 年
  │ 否：SS 按关键词增量
  └── arXiv 多类目 + 作者/标题/机构/白名单 ──┘
      ↓
  Hugging Face Daily Papers（最近 14 天）
      ↓
  upsert 进 paper_db.json
      ↓
  打分：relevance × 0.5 + venue × 0.2 + fresh × 0.15 + velocity × 0.15
         + 实验室加成（tier-1 +2.0）+ ⭐ Notable 作者 +0.5
      ↓
  selector：按综合分 rerank → 取 top-4 + 1 经典轮播 = 5 篇
          （同一实验室最多 2 篇，保证多样性）
      ↓
  GPT-5 生成 6 字段中文摘要
      ↓
  Jinja2 渲染 HTML → SMTP 发送
      ↓
  （可选）PushPlus → 微信公众号推送
      ↓
  追加 history.json + 推进 classic_rotation_state.json
      ↓
  GitHub Actions 自动 commit data/ 更新
```

---

## FAQ

**Q: Semantic Scholar 一直 429 怎么办？**  
A: 脚本已对 429 单独 `sleep(60)` + 最多重试 3 次；若仍失败，只会影响当天的 SS 数据源，其他数据源继续。长期命中 429 建议申请私有 key（Secrets 里填 `SEMANTIC_SCHOLAR_API_KEY`）。

**Q: OpenAI 超时 / 429 / 余额不足？**  
A: 摘要步骤失败不会阻断邮件发送；失败的论文卡片会显示 `摘要生成失败`，并自动展示原 abstract。

**Q: 今天没收到邮件？**  
A: 到 Actions 看日志。常见原因：  
  - `nothing picked for today`：池子里所有论文综合分都低于 `VLA_TOP_RANKED_MIN_SCORE`（默认 2.0）→ 降这个阈值，或检查数据源是否有抓回内容。  
  - `email send failed`：SMTP 凭据错；可从 Actions 的 `failed-digest-*` artifact 下载 HTML 手动查看。  

**Q: 某篇经典论文 Semantic Scholar 查不到？**  
A: `main.py` 会打印 `Classic paper not found`，该条占位保留但轮播时自动跳过。你可以手工把 `paper_id` 填进 `data/classic_papers.json`。

**Q: 能否调整推送数量？**  
A: 改 `.env` 里 `VLA_TOP_RANKED_COUNT`（默认 4，即每天 4 + 1 经典 = 5 篇）。还可以用 `VLA_MAX_PER_LAB` 控制「同一实验室最多几篇进入 top_ranked」（默认 2，设 0 关闭多样性约束）。

**Q: 怎么加一个新实验室 / 新作者？**  
A: 编辑 [`config/labs.yaml`](config/labs.yaml)。格式：

```yaml
labs:
  my_new_lab:
    label: "My New Lab"
    tier: 1
    weight: 1.0
    arxiv_au: ["PI Name", "Senior Author Name"]
    title_keywords: ["ModelBrand"]
    watchlist_ids: ["2601.12345"]   # 强拉：元数据无机构线索的论文
```

下次跑时就会走作者 `au:` / 标题 `ti:` / 白名单 `id_list=` 三条 arXiv 支线搜索 + 命中后自动加 `lab_boost = 2.0 × weight`。

**Q: 如何回放旧数据？**  
A: 删 `data/paper_db.json` 再运行 `python -m src.main --force-bootstrap`。

---

## 项目结构

```
vla-paper-digest/
├── .github/workflows/daily_digest.yml   # 每日定时 + 手动触发
├── config/
│   └── labs.yaml            # 实验室 / 作者 / 品牌名 / 白名单清单
├── src/
│   ├── main.py              # 入口 + banner + orchestrator
│   ├── config.py            # 关键词库 / venue 权重 / 阈值
│   ├── config_labs.py       # labs.yaml 加载 + 匹配
│   ├── models.py            # Pydantic 模型
│   ├── sources/
│   │   ├── semantic_scholar.py
│   │   ├── openreview.py
│   │   ├── arxiv_source.py      # 多类目 + 5 条检索支线
│   │   └── hf_papers_source.py  # Hugging Face Daily Papers
│   ├── scoring.py           # 四项分 + 综合分 + 实验室加成
│   ├── selector.py          # top-N by composite + 1 classic（每 lab 上限）
│   ├── summarizer.py        # LLM 六字段摘要
│   ├── emailer.py           # SMTP SSL / STARTTLS
│   ├── wechat_notifier.py   # PushPlus → 微信公众号推送（可选）
│   ├── storage.py           # JSON 持久化
│   └── utils.py
├── templates/email_template.html
├── data/
│   ├── classic_papers.json            # 25 篇经典白名单（预置）
│   ├── paper_db.json                  # 运行后自动创建
│   ├── history.json                   # 运行后自动创建
│   └── classic_rotation_state.json    # 运行后自动创建
├── tests/
│   ├── test_scoring.py
│   ├── test_selector.py
│   └── test_lab_boost.py    # labs.yaml 匹配 + lab_boost 单测
├── .env.example
├── requirements.txt
├── pyproject.toml
└── README.md
```

---

## License

MIT
