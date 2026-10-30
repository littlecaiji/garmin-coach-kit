# Garmin 周报 · 自适应训练教练

每周一自动从 Garmin Connect 拉上周训练数据，生成一份带**确定性教练判断**的中文报告，
推进跨周训练状态，并把下一周的课表推回 Garmin 手表。

不依赖任何在线 AI 训练服务。大模型只在最后一步把已经算好的结论翻译成人话，
**不参与任何数值决策**（详见 `coach_llm.py` 顶部）。

---

## 架构：代码与数据物理分离

```
garmin-coach-kit      （公开，本仓库）    代码 / 规则引擎 / 工作流 / 示例
        │
        │  Actions 用 PAT 把它 checkout 到 data/
        ▼
garmin-training-data  （私有，你自己建）  state/ 与 reports/
```

**为什么必须分开**：`state/training_state.json` 与 `reports/` 里有 HRV、静息心率、
睡眠、体重、FTP、负重、目标赛事 —— 这些是**敏感个人信息**。一旦进公开仓库，
会被 fork、镜像、搜索引擎永久缓存，删不干净。所以：

- 本仓库**只装代码**，不含任何个人数据；
- 数据仓库通过 `DATA_DIR` 环境变量定位（见 `coach_state._resolve_data_root()`）；
- `state/` `reports/` `data/` `*.gpx` 已在 `.gitignore` 里，防止手滑。

如果你只想自己用、不打算开源，也可以把两者合成一个私有仓库 —— 此时不设
`DATA_DIR`，脚本会退回「state 与代码同级」的旧布局。

## 快速开始

1. 建一个**私有**数据仓库（例如 `garmin-training-data`），放两个目录：

   ```
   state/training_state.json      ← 从 examples/training_state.example.json 复制后改
   state/strength_log.md          ← 从 examples/strength_log.example.md 复制
   reports/                       ← 空目录即可，报告会自己写进来
   ```

2. `state/training_state.json` 里**必须改**的两项：
   - `ftp.current` —— 你的 FTP（示例值是占位）
   - `goal.date` —— 目标赛事日期，`base/build/peak/taper` 全部由它算出来

3. 在 Actions 里配 Secrets：

   | Secret | 必填 | 用途 |
   |---|---|---|
   | `GARMIN_TOKENS_B64` | ✅ | `base64` 后的 `~/.garminconnect/garmin_tokens.json` |
   | `GH_PAT` | ✅ | 拉私有数据仓库 + 回写 token（需 `repo` + `workflow`） |
   | `WECOM_WEBHOOK` | ⭕ | 企业微信群机器人，推送报告 |
   | `LLM_API_KEY` | ⭕ | DeepSeek key，只用于「教练点评」那一段 |
   | `GARMIN_CN_TOKENS_B64` | ⭕ | 只用国服同步时才需要 |

4. 手动 dispatch 一次 `Garmin 周报 · 云端` 验证。

## 设计要点

- **规则引擎是确定性的**：同样的输入必然得到同样的输出，可离线单测
  （`test_coach_state.py`）。可以回溯「为什么那一周这么排」。
- **HRV/RHR 闸门优先于 TSB**：TSB 只反映训练负荷，看不到自主神经状态。
  HRV 跌破基线或 RHR 连续上升时**取消强度课**，无论 TSB 多正。
- **幂等**：按 ISO 周只推进一次。补跑、手动重试都不会重复加档。
- **不粉饰**：准入条件不满足就直说缺口，退化成保守计划，而不是硬排。

## 免责

通过非官方客户端库访问 Garmin，**可能与 Garmin 服务条款冲突，风险自负**。
不是医疗软件。完整说明见 `LICENSE` 末尾。


## 第 0 步：搞定 GitHub 认证

助手所在的环境装不了系统包（`/opt/homebrew` 受保护，GitHub 的下载 CDN 也被代理拦），
所以认证这一步得你自己做。三条路任选：

### 路线 A — Personal Access Token（**不用装任何东西，推荐**）

1. 打开 https://github.com/settings/tokens/new （Classic token）
2. Note 随便填，比如 `garmin-coach-kit`；Expiration 选 90 天
3. **勾选这两个 scope**：
   - ✅ `repo` —— 创建仓库、推送代码
   - ✅ `workflow` —— 推送 `.github/workflows/` 下的文件（**不勾会被 GitHub 拒绝，这是最容易踩的坑**）
4. Generate token → 复制那串 `ghp_...`（**只显示这一次**）
5. 在本机终端跑：

```bash
cd /path/to/garmin-coach-kit
python3 bootstrap_token.py
```

脚本会提示你粘贴 token（隐藏输入、不回显），然后自动完成：建私有仓库 → 把 Garmin token
加密写成 Actions secret → 首次提交推送 → 跟踪 Actions 状态。**token 不落盘、不写进
`.git/config`**，用完你去 GitHub 上吊销即可。

### 路线 B — 装 gh（更省事，但要装东西）

```bash
brew install gh && gh auth login
```

交互选：**GitHub.com → HTTPS → Login with a web browser**，把终端显示的 8 位码填进浏览器。
装完告诉助手，剩下三步（建仓库 / 写 secret / 推送）全部自动完成。

### 路线 C — 官方安装包（不装 gh，也不写 token 脚本）

1. 下载（约 10MB）：
   https://github.com/cli/cli/releases/download/v2.102.0/gh_2.102.0_macOS_universal.pkg
2. 双击安装（需要管理员密码）
3. 回到终端：`gh auth login`

## 一次性配置

前置：本机装好 `gh` 并登录（见上），且 `~/.garminconnect/garmin_tokens.json` 存在。

```bash
cd /path/to/garmin-coach-kit
./bootstrap.sh
```

脚本会：同步 `analyze_training.py` → `git init` + 首次提交 → 把 token 以 base64 形式
写入仓库 secret `GARMIN_TOKENS_B64` → 创建**私有**仓库并推送（首次推送自动触发探针）。

token 只在 secret 里，`.gitignore` 已挡住所有 token 文件——**任何情况下都不会进 git**。

### 不想装 gh 的手动路线

1. 在 GitHub 网页建一个**私有**空仓库（不要勾 README）。
2. 本机执行（把 `OWNER` 换成你的用户名）：

```bash
cd /path/to/garmin-coach-kit
git init -b main && git add -A && git commit -m "Garmin 周报 · 云端探针（只读）"
git remote add origin https://github.com/OWNER/garmin-coach-kit.git
git push -u origin main
```

3. 生成 secret 值并复制：

```bash
base64 -i ~/.garminconnect/garmin_tokens.json | tr -d '\n' | pbcopy
```

4. 仓库 → Settings → Secrets and variables → Actions → New repository secret，
   名字填 `GARMIN_TOKENS_B64`，值粘贴进去。
5. 仓库 → Actions → 选「Garmin 周报探针 · 试水」→ Run workflow。

## 看结果

```bash
gh run list -L 5
gh run watch
```

日志里会看到：出口 IP、登录是否被接受、最近 5 条活动，然后直接打印整份 `training_report.md`。
报告同时作为 artifact 上传，可以在 Actions 页面下载。

## 已启用（2026-10-08）

1. **每周定时已上线**：`45 0 * * 1` = 周一 00:45 UTC = 北京 08:45，与原自动化同一时刻。
   ⚠️ GitHub 免费额度的 cron 会漂移，实际可能晚 5–30 分钟；仓库连续 60 天无提交会被
   自动暂停定时任务，需要偶尔动一下仓库保活。
2. **报告归档**：每次运行把报告写进 `reports/YYYY-MM-DD.md` 与 `reports/latest.md`
   并提交回仓库。
   ⚠️ 提交信息必须带 `[skip ci]`——workflow 有 `on: push: branches: [main]`，而报告带
   时间戳每次内容都不同，不带就会 push→触发→再 commit 死循环。
3. **课表推送已上云**：`push_plan_to_connect.py` 已迁入本仓库，定时运行时会顺带把本周
   课表推到 Garmin Connect（生成模板 + 排期）。2026-10-09 已实测真实推送：6 个模板
   删旧建新 + 排期，Garmin 上无重复。
4. **微信投递已上线**（2026-10-09）：`notify_wecom.py` 推摘要 + 完整报告文件到企业微信群，
   可经「微信插件」同步到个人微信。secret `WECOM_WEBHOOK` 已配置。
5. **自适应排课已上线**（2026-10-09）：报告与课表不再是固定模板，改为由
   `state/training_state.json` 跨周驱动。详见下一节。

---

## 🔄 自适应机制（2026-10-09 起）

改之前：`build_plan(ftp, hot)` 只吃 FTP 和热天标记，每周产出同一套课；
报告里写死 `205–220W`、`240W`、`20kg`；`_write_weekly_plan()` 的
`strength_done` 参数**传进去了但函数体从没读过** —— 也就是说系统在架构上
根本无法根据「你上周做没做力量」调整计划。挑战目标（11 月 200km+3500m）
也只存在于 `analyze_route.py`，一行都没进排课逻辑。

改之后：分析 → 读上周完成度 → 规则引擎推进 → 写回 state → 报告与课表都从
state 生成。**每一处调整都能追溯到上周的一条实际数据**，决策日志直接印在
报告里（「🎛️ 本周调整依据」段落）。

### 状态文件 `state/training_state.json`

**必须提交回仓库**（`.gitignore` 里已标注不要忽略它）。它是跨周记忆，丢了就等于
每周从默认值冷启动。

| 字段 | 作用 | 推进规则 |
|:--|:--|:--|
| `ftp.current` / `estimate` | 区间基准 / 报告估算 | **估算不自动覆盖 current**——估算系统性偏高约 5W，自动上调会把 Z2 顶成节奏课 |
| `strength.weights.*` | 逐动作负重 | 连续 **2 周**按计划完成 → 臀桥/硬拉 +2.5kg，分腿蹲/单腿硬拉 +1kg，髂腰肌 +0.5kg |
| `strength.mode` | full / single / minimal | 连续 **2 周** 0 次 → 逐级降（full→single→minimal）；连续 2 周完成 → minimal 升回 single，其余加重 |
| `quality.threshold` | 组数×时长 | 连续 2 周满组升档 `3×10 → 3×12 → 4×10 → 4×12`；末组崩盘退一档 |
| `quality.vo2` | 组数×时长 | 同上 `5×3 → 6×3 → 7×3`（只加组数，单组 3min 已是 Z5 耐受上限） |
| `endurance.long_ride_min` | 长骑基准时长 | 连续 2 周达标（≥90% 计划时长）→ +30min，上限 240min |
| `goal.date` | 目标日期 | 驱动 base / build / peak / taper 四阶段 |

### 🏋️ 力量三档位（2026-10-12 起）

| mode | 每周次数 | 动作表 | 场景 |
|:--|:--:|:--|:--|
| `full` | 2 | 周五**力量A（主）** + 周日力量B | 教练理想形态 |
| `single` | 1 | 只有周五**力量A（主）** | 现实频率（当前） |
| `minimal` | 1 | 只有周日 **B 精简版**（核心+髋外展+单腿） | 真断档后的习惯重建 |

判断「有没有那节课」一律看 `strength_plan()` 返回的 `A` / `B` 是否为空，
不要再比 `mode` 字符串 —— `single` 档周日是没有力量课的。

⚠️ 从 `single` **不会**自动升到 `full`：那需要「一周真的做满 2 次」这个事实，
由人自己决定（改 state 里的 `mode`），不能让日历替身体做主。

### 📝 力量补录 `state/strength_log.md`

Garmin 只把 `strength_training` 类型的活动算作力量，而力量课是自练的、不上表。
只看 Garmin 会永远读到 0 次，把「没记录」当成「没在做」—— 实测 80 天里只有
**2 条**力量活动（8/3、8/10），而系统据此推出了 `mode=minimal`、「断档 9 周」、
专项负荷被一票否决。整个前提是假的。

所以增加一个一行一次课的手动日志，与 Garmin 记录**相加**后作为完成度：

```
2026-10-12  力量A  臀桥 3×10@20kg + 保加利亚蹲 2×10@8kg + 平板 3×45s
10/14       力量B
10月16日     力量A
```

- 日期可写 `2026-10-12` / `2026/10/12` / `10-12` / `10/12` / `10月12日`，带 `-`/`*`/`>` 项目符号也行
- 只写月日时，若月份大于当前月份则归到去年
- 空行、`#` 行注释、`<!-- -->` 块注释跳过（块注释按原行数占位，保证警告里的行号准确）
- 同一行内容粘两遍只计 1 次
- **无法识别日期的行不会被算进去，但会在报告的「力量补录」段落里被点名** ——
  写了却不算数比不写更伤人，所以绝不静默丢弃

写完提交到仓库（`./push_and_watch.sh`），下周一云端生成报告时就会被算进「上周完成度」，
并参与负重递进。报告里每周会回显「Garmin 记录 / 手动补录 / 合计」和最近 5 条记录。

### 专项期有准入门槛

日历进入 build/peak **不等于**身体准备好了。`readiness_gaps()` 要求三条同时满足
才允许插入爬坡专项（5–7km/约5% 坡重复 4–6 次、上坡守 Z2）：

1. 力量没有整体断档（`consecutive_missed < 2`）
2. 最近有 ≥90min 长骑
3. TSB > -15

任一条不满足，长骑就退回纯 Z2，并在报告里**明确写出缺口原因**，而不是假装计划合理。

⚠️ 第 1 条**曾经**写的是 `mode != "minimal"`，那是错的：`mode` 只回答「用哪套动作表」，
一旦降级就再也不会自动回上去（回 `full` 需要「一周做满 2 次」），专项会被无限期锁死。
真正该问的是「力量还在不在练」—— 那是 `consecutive_missed` 的事。阈值取 2 周：
1 周空缺可能只是漏记，2 周才是真的停了。

### 🩺 自主神经闸门（HRV / RHR）—— 优先级高于 TSB

`TSB` 完全由训练负荷（TSS）算出，**看不到自主神经状态**。2026-10-12 真实踩到：
HRV 夜均 46（Garmin 判 LOW，基线告警线 60）、静息心率从 50 单调升到 59、
休息整周也不恢复，而报告仍在说「TSB +16，状态窗口打开，建议上 VO2max」。
**负荷低 ≠ 可以上强度。**

`coach_state.autonomic_gate()` 独立取最近 14 天的 HRV / RHR（见
`analyze_training._fetch_autonomic`），任一条成立即触发：

1. HRV 夜均跌破 Garmin `baseline.lowUpper`（拿不到 baseline 时兜底 60）
2. HRV 夜均低于平衡区下限 `baseline.balancedLow`
3. Garmin `hrvStatus == "LOW"`
4. 静息心率连续 ≥3 天上升，或比窗口内最低值高 ≥5 bpm

触发后整份报告一起改口，而不是只在建议里加一句：

- 周一阈值课 + 周三 VO2max 课**整体换成 Z2 有氧**，周 TSS 随之重算
- 长骑的爬坡专项退回纯 Z2，并标注为「可选而非指标」
- 「状态窗口」那条建议替换为「**状态窗口被否决**」
- LLM 上下文写入闸门结论，避免点评写出与课表相反的话

两条刻意的取舍：**缺数据一律放行**（Garmin 抽风不该平白取消整周训练）；
**只在自主神经一侧异常时触发** —— TSB 为负但 HRV 正常属于正常训练疲劳，不拦。

### 周 TSS 由结构算出

不再写死 `95+80+45+85`。改为 `TSS = Σ 时长(h)×IF²×100` 逐段累加，所以改组数或
时长，周 TSS 自动跟着变。（注：该估算比 Garmin 实测约低 10%，作预算参考足够。）

### 🧠 模型点评（已启用）

`coach_llm.py` 用 DeepSeek 生成报告里的「教练点评」段落。**安全边界很硬**：

- 模型**只写散文，不做任何数值决策**。组数、负重、时长、TSS 上限全部由
  `coach_state.py` 的确定性规则算出，模型拿到的是已定稿的结果，只负责解释与提醒。
- Prompt 里明确禁止它给新的功率/负重/TSS 数值。
- 没有 `LLM_API_KEY`、超时、报错、额度不足 —— 一律只是**少一段话**，
  报告其余部分与课表推送完全不受影响。

#### 🧠 思考模式（2026-10-09 起默认开启）

DeepSeek V4 **默认就开思考模式**，且思考内容（`reasoning_content`）**按输出 token 计费**
—— `completion_tokens` 里就包含它。用环境变量 `LLM_THINKING=enabled|disabled` 切换。

实测对比（同一份输入）：

| | 关闭思考 | 开启思考 |
|:--|:--|:--|
| 输出 tokens | 238 | **1028**（含思考 820） |
| 延迟 | ~4s | 5.4s |
| 单次成本（空闲档） | ¥0.0021 | **¥0.0055** |

选择 `enabled` 的理由：思考让点评明显更会抓重点（它会自己权衡「依从性风险 > 疲劳风险」
这类判断），而代价一年也就两毛多。想省/想快就把 `LLM_THINKING` 设成 `disabled`。

#### 💰 成本（实测口径，2026-10-09 核价）

模型 `deepseek-flash`，**开启思考**实测约 **输入 1053 / 输出 1028 tokens**：

| 项 | 计算 | 金额 |
|:--|:--|:--|
| 输入 | 1053 × $0.15/1M（空闲时段·缓存未命中） | $0.00016 |
| 输出 | 1028 × $0.60/1M（含思考 820） | $0.00062 |
| **单次** | | **≈ $0.0008 ≈ ¥0.0055** |
| **一年（52 次）** | | **≈ ¥0.29** |

关闭思考则约 **¥0.002 / 次、¥0.11 / 年**。两条都便宜到不构成决策因素。

真正需要留意的是这几个坑：

- ⚠️ **模型名必须用 `deepseek-flash`**。旧的 `deepseek-chat` / `deepseek-reasoner`
  已于 **2026-07-24** 完全下线，用旧名会直接报错。
- ⚠️ **思考会挤占 `max_tokens`**。实测思考用掉 532–820 tokens（浮动较大），所以
  `MAX_TOKENS` 必须覆盖「思考 + 正文」的总和，否则正文被截断 —— 而**截断的表象是
  「正文为空」**，极易误判成模型没返回。当前设 4000，约 4 倍余量；空正文时日志会
  依据 `finish_reason == "length"` 明确区分「被截断」和「真没返回」。
- ⚠️ **思考模式下 `temperature` 无效**（官方：不报错但被忽略）。代码只在非思考模式
  才带这个字段，避免留一个看着有用、其实不生效的参数。
- ⚠️ **高峰时段价格翻倍**：UTC 01:00–04:00 与 06:00–10:00（周一至周五，中国法定
  节假日除外）。cron 在周一 00:45 UTC，若 GitHub 的漂移（5–30min）把它推到
  01:00 之后就按高峰计费 —— 差额约 ¥0.005，可忽略。
- ℹ️ **每周一次 = 上下文缓存基本帮不上忙**（系统提示词虽然每轮相同，但缓存早过期）。

每次运行会把**真实用量**打进 Actions 日志，思考 token 单列出来，不用靠估算对账：

```
📊 本次用量：输入 1053 / 输出 744 tokens（缓存命中 896 / 未命中 157），其中思考 532 tokens（882 字符）
```

启用：

```bash
gh secret set LLM_API_KEY --repo <owner>/<repo>   # 填 DeepSeek 的 key
# 可选：换模型 / 端点 / 思考模式（用 repository variables；不设则用默认值）
gh variable set LLM_MODEL    --body "deepseek-flash"
gh variable set LLM_BASE_URL --body "https://api.deepseek.com"
gh variable set LLM_THINKING --body "disabled"     # 只在想关掉思考时才需要设
```

离线看会发给模型的 prompt（不发请求、不花钱）：

```bash
python coach_llm.py            # 打印将要发送的 payload
python coach_llm.py --live     # 真调一次（需 LLM_API_KEY），验证模型名/密钥/用量
```

### 离线自测（不连网、不写 Garmin）

自测脚本在**项目根目录的 `work/`**（不在本仓库里），从项目根运行：

```bash
# 规则引擎：升档/退档/降级/封顶/阶段判定/专项准入/闸门/补录日志解析，110+ 条断言
python work/_test_coach_state.py

# 动作绑定：full / single / minimal / 爬坡专项各场景，校验 Garmin 1527 条官方动作库
python work/_validate_all_bindings.py       # 需要 garminconnect>=0.3.7

# 报告渲染：构造完成度数据跑通「规则引擎 → 状态 → 报告」整条链路（7 个场景）
python work/_test_report_render.py
```

### ⚠️ 课表推送的两条铁律

- **只在「定时」和「手动触发」时推**（`if: github.event_name != 'push'`）。
  直接 push 代码**不会**动你 Garmin 日历上的课表——否则每次改代码都重排训练课，
  属于意外副作用。想推就手动 dispatch 一次。
- **上云后必须停掉本机那个周一 08:45 的自动化**。脚本按标题「删旧建新」是幂等的，
  但两边同时跑会有竞态，并且会在同一天推两遍。

### 手动验证课表推送

```bash
# 只演练，不写 Garmin
gh workflow run weekly-report.yml -f dry_run=true

# 真推
gh workflow run weekly-report.yml
```

或到 Actions 页面点 Run workflow，勾选 `dry_run` 即可演练。

## 📱 微信推送（企业微信机器人）

### 为什么不用个人微信直接推

**个人微信没有官方推送 API。** `itchat` / `wechaty` / iPad 协议这类个人号方案违反
微信用户协议、有封号风险，本项目不使用。走官方合规路径：**企业微信群机器人 +
微信插件**——个人也能免费注册企业微信（无需企业资质），机器人消息经「微信插件」
可以同步到你自己的微信里。

### 五步拿到 webhook

1. 手机装**企业微信** App，用手机号注册。企业信息随便填（如「我的训练室」），
   规模选 1–50 人，**不需要任何资质审核**。
2. 建一个群，把名字叫成「训练周报」，**群里只有你一个人就行**。
3. 进群 → 右上角「···」→ **「消息推送」** → 添加 → 起个名（如「周报助手」）→ 得到一个地址：
   `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx-...`

   ⚠️ **别点成「智能机器人」**——那是 AI 助手，**没有 Webhook**（企业微信官方社区原话：
   「智能机器人没有 webhook。消息推送（原群机器人）才有 webhook」）。
   而且 **v4.1.41 起「群机器人」已更名为「消息推送」**，用旧名字去找会找不到入口。

   查看已有的 Webhook：群 → 「···」→「消息推送」→ 点对应的机器人 → 「Webhook 地址」
   （只有创建者可见）。

   ⚠️ **另一条路别走**：企业微信「自建应用 + 应用消息 API」（corpid/agentid/secret）需要
   在应用详情里配置「**企业可信 IP**」，而 GitHub Actions 的 runner 出口 IP 每次运行都变
   → 一定会被拒（err 60020）。**群机器人/消息推送的 Webhook 不受可信 IP 限制**，
   是唯一能在 Actions 里跑通的方案。
4. **让个人微信也能收到**：企业微信管理后台 →【我的企业 → 微信插件】→ 取
   「邀请关注微信插件」二维码 → 用**微信**扫码关注（入口在：微信 → 通讯录 → 我的企业）。
   三个开关别设错：
   - 插件内【右上角 → 设置 → 接收应用消息】要**开启**
   - 手机端企业微信【左上角 ≡ → 设置 → 新消息通知】**不要**勾「仅在企业微信中接收消息」
   - 后台【微信插件】里**不要**勾「成员使用微信插件时需要使用企业微信客户端」
5. 把 webhook 地址存成本仓库的 Actions secret，名字必须是 `WECOM_WEBHOOK`：

```bash
cd /path/to/garmin-coach-kit
# ⚠️ 代理端口会变：2026-10-12 实测为 7891（7890 已 connection refused）。
# 端口不对的表现是长时间挂住/超时，而不是报「端口错」—— 查 `lsof -nP -iTCP -sTCP:LISTEN | grep 789`。
GH_TOKEN="$(tr -d '[:space:]' < work/gh_token.txt)" \
  HTTPS_PROXY=http://127.0.0.1:7891 \
  gh secret set WECOM_WEBHOOK -R OWNER/garmin-coach-kit
```

### 消息结构：摘要 + 完整报告文件

企业微信 markdown 消息体上限 **4096 字节**，而报告约 12KB（205 行）——整份推不进去。
所以每次推送**两条**：

1. **摘要**（markdown）：负荷 / 恢复 / 上周 / 本周计划 / 重点建议。实测约 1.7KB。
2. **完整报告文件**：`training-report-<日期>.md`，点开即看全文。

**为什么不用「链接指向仓库」？** 因为**手机微信里点不通**：仓库是私有的，未登录 GitHub
的浏览器访问私有仓库时 GitHub 一律返回 **404**（刻意不暴露仓库存在性），而微信内置浏览器
没有你的登录态。发文件则完全不依赖外链，也不必把仓库转公开。（在电脑浏览器上，
如果已登录 GitHub，链接是能打开的，所以摘要里仍保留它作为备用入口。）

两个已知约束：

- 企业微信 markdown **不支持表格**，摘要一律用引用行（`>`）排版。
- `upload_media` **只认纯 ASCII 文件名**——用中文会返回 `44001 empty media data`。
  脚本里固定用 `training-report-<日期>.md`。

**没配 `WECOM_WEBHOOK` 也不报错**——脚本打印提示后退出 0，整条 workflow 照常跑完。
文件发送失败只告警不算失败（摘要已经送达）。两个接口都带 5/15/45 秒退避重试，
因为实测机器人**刚创建完的几十秒内**接口会返回 `93000 invalid webhook url`。

### 本地演练

```bash
cd cloud
python notify_wecom.py                 # 只打印摘要，不发送
WECOM_WEBHOOK='https://qyapi...' python notify_wecom.py   # 真发一条
```

## 已知边界

- `analyze_training.py` 与 `push_plan_to_connect.py` 都是**副本**。本地改了要手动
  `cp` 过来再 push（或重跑 `./bootstrap.sh`）。
  `notify_wecom.py` 是**云端专有**的，本地没有对应脚本。
- 课表推送刻意排在「归档 + 提交」之后：万一 Garmin 侧出问题，报告已经安全落库。
- ⚠️ **周一双推风险（已处理）**：云端 cron 与本机那个周一 08:45 的自动化**都会推课表**。
  **2026-10-09 已把本机 automation-1785226717747 置为 PAUSED**——云端的报告、课表推送、
  微信投递三件事都已实测通过，本机那份已完全冗余。若要回滚，把它重新启用即可。
  实测结论：连续两次推送是幂等的（Garmin 上同一天同名只留一条），但**并发**推送未验证，
  所以不要图省事让两边同时开着。
- ⚠️ **目标日期 2026-11-08 是占位值**：这个日期由我推测填的（周日），它驱动
  base/build/peak/taper 划分。**请改成你真实的挑战日期**——改
  `state/training_state.json` 的 `goal.date` 即可，下一周就生效。
  另：`goal.name` 目前写的是「单日 200km + 3500m」，如果实际路线/日期变了也一并改。
- ⚠️ **代码有两份副本**：`cloud/`（云端权威）与项目根目录。我已在 2026-10-09 把两边同步
  成一致，但**改代码请以 `cloud/` 为准再同步过去**，否则又会退回到「两份真相源」的老问题。
  内容型数据（state）同理：实际生效的是 cloud 那份，根目录那份只是给本地跑用的。
- ℹ️ **FTP 仍是你校准的 214W**：报告估算（本周 217W）只做记录与偏差提示，**不会自动
  上调区间**。若连续多周同向偏离，再手动改 `state/training_state.json` 的 `ftp.current`。
