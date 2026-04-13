# 闲鱼虚拟商品缺口扫描器

基于 ai-goofish-monitor 的 Playwright 爬虫基础设施，识别闲鱼上虚拟商品的供需缺口。

> **核心逻辑**：用闲鱼当预测市场，发现「需求上升 + 虚拟供给稀少」的话题，当天制作对应内容产品上架。

## 核心公式

```
缺口分 = 需求帖数 ÷ max(虚拟供给数, 1)
```

缺口分越高 → 求购多、卖货少 → 值得今天创作并上架。

## 目录结构

```
gap_scanner/
├── login.py        ← 第一步：一次性登录，保存会话
├── scan.py         ← 主入口，每天运行一次
├── fetcher.py      ← Playwright 爬虫核心
├── scanner.py      ← 缺口分计算逻辑
├── reporter.py     ← Markdown 日报生成
├── keywords.txt    ← 关键词库（可自由增删）
├── requirements.txt
├── state/          ← 登录状态（不提交，自动生成）
├── data/           ← 每日原始数据 JSONL（不提交）
└── reports/        ← 每日报告 YYYY-MM-DD.md（不提交）
```

## 快速开始

```bash
cd gap_scanner

# 安装依赖
pip install -r requirements.txt
python -m playwright install chromium

# 第一步：登录（只需一次）
python login.py
# 浏览器弹出 → 扫码登录闲鱼 → 回到终端按 Enter

# 如已安装 ai-goofish-monitor 并有 state/acc_1.json，无需再次登录
# 将其复制到 gap_scanner/state/login_state.json 即可

# 每日扫描
python scan.py

# 调试：只跑前 3 个关键词
python scan.py --limit 3

# 空跑测试（不访问闲鱼）
python scan.py --dry-run
```

## 登录方式（三选一）

| 方式 | 操作 |
|------|------|
| `python login.py` | 弹出 Playwright 浏览器，扫码登录，按 Enter 保存 |
| Chrome 扩展 | 安装 [Xianyu Login State Extractor](https://chromewebstore.google.com/detail/xianyu-login-state-extrac/eidlpfjiodpigmfcahkmlenhppfklcoa)，导出状态保存为 `state/login_state.json` |
| 复用已有 state | 将 ai-goofish-monitor 的 `state/acc_1.json` 复制到 `state/login_state.json` |

## 输出示例

```markdown
# 闲鱼虚拟商品缺口日报 2026-04-13

| 排名 | 关键词 | 需求帖 | 虚拟供给 | 缺口分 | 现有均价 | 建议定价 |
|:----:|--------|:------:|:--------:|:------:|:--------:|:--------:|
| 1 | Cursor教程 | 23 | 3 | 7.67 | ¥25 | ¥20-25 |
| 2 | AI工作流 | 18 | 5 | 3.60 | ¥39 | ¥30-37 |
...
```

## 关键词库管理

编辑 `keywords.txt`，每行一个关键词，`#` 开头为注释：

```
# AI 工具类
Cursor教程
Claude教程
AI工作流

# 投资理财类
炒股教程
```

初始内置 24 个关键词，覆盖 AI 工具、投资理财、技能副业、考证学习四类。
