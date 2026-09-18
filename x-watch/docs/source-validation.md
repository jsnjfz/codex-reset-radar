# 阶段 0：采集源验证记录

本文件记录对 FxTwitter 接口的**真实请求**结果。只写实际观察到的内容；未验证的能力明确标为“未验证”。

---

## 本轮验证

| 项目 | 内容 |
|---|---|
| 验证时间 | 2026-09-17 08:30–08:32 UTC |
| 运行环境 | macOS (Darwin 25.5.0), 本机直连, 未设置代理 |
| 工具 | `curl` 8.x + `python3` 3.9.6 (仅用于解析响应) |
| 测试账号 | `bcherny`（沿用方案示例账号，公开账号） |
| 请求总数 | 4 次（JSON 首页、JSON 第二页、RSS、不存在账号） |

方案 §1.1 记录的“DNS 解析失败”在本机**未出现**：

```
$ nslookup api.fxtwitter.com
Address: 172.67.139.37
Address: 104.21.87.14
```

### 1. JSON 首页

请求：

```
GET https://api.fxtwitter.com/2/profile/bcherny/statuses?count=5&with_replies=1
```

| 项目 | 结果 |
|---|---|
| HTTP 状态 | `200` |
| Content-Type | `application/json` |
| 响应大小 | 103783 字节 |
| 业务 `code` | `200` |
| 顶层字段 | `code`, `results`, `cursor` |
| 返回条数 | **36** |
| 最新帖时间 | `Thu Sep 17 04:35:18 +0000 2026` |
| 服务端标识 | `x-powered-by: fixtweet-main-ea8d7d4-2026-09-17T00:42:00` |

已核对：返回的是真实 JSON，不是登录页、验证码页或错误 HTML；帖子 ID、作者、时间、正文可与 `url` 字段给出的原帖链接对应。

**`count` 被校验但从不生效**：请求 5 条，实际返回 36 条。详见下方「§F 配额与参数的说明范围」的实测矩阵。配置里的 `page_size` 只能作为请求意图，不能作为预期条数，更不能用“返回条数 < page_size”推断历史结束。

单条帖子的实际字段：

```
author, bookmarks, community_note, created_at, created_timestamp, embed_card,
id, is_note_tweet, lang, likes, media, possibly_sensitive, provider, quotes,
raw_text, replies, replying_to, reposted_by, reposts, text, type, url, views
```

### 2. JSON 分页（第二页）

用首页 `cursor.bottom` 作为 `cursor` 参数再请求一次（间隔 3 秒）：

| 项目 | 结果 |
|---|---|
| HTTP 状态 | `200`，业务 `code` `200` |
| 返回条数 | 33 |
| 与首页重复 | 1 条 |
| 首页未出现的新条目 | **32 条** |
| 新 `cursor.bottom` | 与首页不同 |
| 本页最旧帖时间 | `Fri Sep 11 00:46:02 +0000 2026`（比首页更旧） |

**分页判定为“已验证可用”**：不是仅看到 `cursor` 字段，而是确认下一页带来了 32 条不同数据、游标发生变化、时间向历史推进。

### 3. RSS

请求：

```
GET https://fxtwitter.com/bcherny/feed.xml?count=5&with_replies=1
```

| 项目 | 结果 |
|---|---|
| HTTP 状态 | `200` |
| Content-Type | `application/rss+xml; charset=utf-8` |
| 响应大小 | 4991 字节 |
| 根结构 | `rss version="2.0"`，含 `atom:` 与 `media:` 命名空间 |
| `lastBuildDate` | `Thu, 17 Sep 2026 04:35:18 GMT`（与 JSON 最新帖一致） |

RSS 可用，但字段远少于 JSON（无 `reposted_by`、无 `replying_to` 结构、无互动数、无游标）。**RSS 分页：未验证，按不支持处理。**

### 4. 不存在的账号

```
GET https://api.fxtwitter.com/2/profile/thisaccountdoesnotexist999xyz/statuses
→ HTTP 404
  {"code":404,"results":[],"cursor":{"top":null,"bottom":null}}
```

注意：**`results` 为空数组但这不是“合法空列表”**。必须先看 HTTP 状态与业务 `code`，404 记为“账号当前不可访问”，不能记成“该账号没有发帖”。

---

## 对实现的强制约束（来自真实数据，不是推测）

### A. `type` 字段不能用来判断帖子类型

首页 36 条的 `type` **全部**是 `status`：

```
types: Counter({'status': 36})
```

类型必须由结构推导：

| 判据 | 结论 |
|---|---|
| `reposted_by` 是对象 | 该条目是被 `reposted_by.screen_name` 转发的帖子；帖子作者仍是 `author` |
| `replying_to` 是对象 | 回复，父帖 ID 在 `replying_to.status` |
| 存在 `quote` 键 | 引用帖，被引用帖完整嵌套在 `quote` 里（字段结构与顶层帖子一致） |
| 以上都没有 | 原创 |

### B. 时间线不是严格倒序

首页前 5 条的实际顺序：

```
2100275089...  Wed Sep 16 17:26:17   (mitchellh)
2100443454...  Thu Sep 17 04:35:18   (bcherny, 回复 mitchellh)   ← 时间跳回更新
2100259951...  Wed Sep 16 16:26:08   (bcherny)                    ← 又跳回更旧
2100394540...  Thu Sep 17 01:20:56   (ServingNChrist, 回复 bcherny)
2100438440...  Thu Sep 17 04:15:23   (bcherny, 回复上一条)
```

上游按**对话聚合**返回，不是按时间排序。因此：

- 不能用“本页最后一条的时间”作为扫描边界。
- 不能因为“遇到一条比上次边界更旧的帖子”就停止分页。
- 覆盖判定只能用整页（乃至整轮）所有条目的**时间下界**，且要求多条证据。

### C. 监控账号的时间线里大量条目不是该账号发的

首页 36 条中，`bcherny` 只占 17 条。其余作者：

```
nikitabier(2), mitchellh, ServingNChrist, blaso96, EliseMorgan__,
felixrieseberg, devteamdrew, ColossusCoach, kevinroose, superalesha,
edwinarbus, claudeai, SebastianR81426, kevin_t_ngo, halluton,
thenoblesimian, eatpraydiehard, addyosmani
```

这些分三类，必须区分，不能一律当成转帖：

1. **真转帖**：`reposted_by.screen_name == "bcherny"` → 实测有 4 条（felixrieseberg / edwinarbus / claudeai / addyosmani）。
2. **对话上下文**：被 `bcherny` 回复的父帖（如 `mitchellh` 那条），以及**别人回复 `bcherny` 的帖子**（如 `ServingNChrist`、`halluton`、`thenoblesimian`、`eatpraydiehard` 回复 `bcherny`）。上游把它们一并返回了。
3. **被引用帖**：嵌套在 `quote` 里（如 `aaandeverything`）。

方案 §2 约定“不抓别人对该博主的全部评论”，但上游**主动**把部分第三方回复塞进了同一响应。按 §5.3“未知帖子类型不得直接丢弃”，实现选择：入库保存，关联类型标为 `context`，默认不进入每日阅读索引。这是“保存事实 + 阅读侧过滤”，不是扩大采集范围。

### D. 长文正文未被截断

`is_note_tweet: true` 的样本（8 条）中，`text` 与 `raw_text.text` 长度一致（例：798 / 798），结尾是完整句子，无省略号。本轮**未观察到**长文截断。这只是样本结论，实现仍保留 `content_status` 字段以便日后记录截断。

### E. 媒体字段

`media.all[]` 单项实际字段：

```
duration, format, formats, height, id, publisher, thumbnail_url, type, width, url
```

本轮样本**未出现** `altText`/`alt_text` 字段。实现按“缺失即不伪造”处理，不填空字符串。

### F. 配额与参数的说明范围

核对日期 2026-09-17。上游对「限额」的说明只有一处，`docs.fxembed.com/api/introduction/`
的 “Rate limiting” 小节，全文三句：

> API v2 has a rate limit of 1000 requests per minute (16.7 requests per second) per IP
> address. We believe this is generous enough for most legitimate applications. If you need
> more, it might be worth self-hosting your own instance.

**除此之外没有任何限额说明。**实测机器可读的 spec（`https://api.fxtwitter.com/2/openapi.json`，
`FxTwitter API 2.0.0`）里：

| 关键词 | 出现次数 |
|---|---|
| `429` | 0 |
| `rate limit` / `ratelimit` | 0 |
| `retry-after` | 0 |
| `too many` | 0 |

`/2/profile/{handle}/statuses` 声明的响应码只有 `200 / 204 / 400 / 404 / 500`，**没有 429**。
实际 200 响应头里也没有 `x-ratelimit-*`、没有 `retry-after`、没有 `cache-control`。

没有说明的还包括：日配额与月配额（只有每分钟突发限制）、超限后的具体行为。

**因此：`collector.py` 里的 429 同源退避是照方案 §10 写的防御性代码，上游契约并未声明
429。它无法与任何文档对齐，也从未真实触发过。不要把它当成「已对接的已知行为」。**

按本项目参数算的用量（增量轮实测每账号 1 页）：

| 配置 | 次/小时 | 次/天 | 峰值 | 占每分钟配额 |
|---|---|---|---|---|
| 60 分钟 / 3 账号 | 3 | 72 | 0.05 次/分 | 0.005% |
| 5 分钟 / 3 账号 | 36 | 864 | 0.60 次/分 | 0.060% |
| 5 分钟 / 1 账号 | 12 | 288 | 0.20 次/分 | 0.020% |

这只是按参数算出的请求量，不是供应商的免费额度承诺。

#### `count` 参数：声明精确、校验生效、输出不受影响

spec 声明 `count` 为 `integer, minimum 1, maximum 100, default 20`。实测：

| 请求 | 返回条数 |
|---|---|
| `count=5` | **20**（默认值） |
| `count=50` | **20**（默认值） |
| `count=5&with_replies=1` | 35 |
| `count=50&with_replies=1` | 36 |
| `count=500` | `HTTP 400 {"code":400,"message":"count: Too big: expected number to be <=100"}` |

不带 `with_replies` 时恒返回 20；带上后走 spec 描述的 “alternate upstream timelines”，
返回 35–36 条。两种模式下 `count` 都不改变输出条数，但越界仍被拒。

结论：`page_size` 的唯一实际作用是「保持在 1..100 以内、不触发 400」。

#### `404` 的声明与实测不一致

spec 写 `404` = “User not found **or empty timeline**”。但实测 `@tsottiaux` 与 `@sottiaux`
返回的是 `200` + `{"code":200,"results":[],"cursor":{"top":null,"bottom":null}}`，
而明显不存在的 `@thisaccountdoesnotexist999xyz` 才返回 404。

`200 + 空列表` 是**未声明的行为**，且它既不能证明账号存在，也不能证明账号没发帖。
这正是实现里「合法空列表只记录、不推断账号状态」的依据。

### G. 上游数据是实时的，不走 CDN 缓存

同一端点相隔 45 秒请求两次，最新帖的互动数发生变化：

```
views  1194552 → 1195730   (+1178)
likes     6168 → 6169      (+1)
```

响应头无 `cache-control` / `age` / `cf-cache-status`。

**结论：端到端延迟 ≈ 轮询间隔，上游本身几乎不贡献延迟。**

---

## 未验证 / 待观察

以下能力本轮**没有**取得证据，实现中按“不承诺”处理：

- [ ] 429 限流的真实触发行为与 `Retry-After` 取值（未故意触发限流）。
- [ ] 游标的有效期，以及失效时上游返回什么。
- [ ] RSS 是否支持分页（按 `supports_pagination = false` 处理）。
- [ ] 帖子被编辑后，是同 ID 内容变化还是新 ID。
- [ ] 删帖后上游的表现。
- [ ] 长期稳定性与是否会改变免费策略 —— 无法验证，不做背书。
- [ ] 3～7 天连续运行观察（方案阶段 4 验收项，尚未开始）。

## 原始样本

脱敏/原样保存在 `tests/fixtures/`，供离线解析测试使用：

- `probe_page1.raw.json` —— JSON 首页（36 条）
- `probe_page2.raw.json` —— JSON 第二页（33 条）
- `probe_feed.raw.xml` —— RSS 响应

这些是真实公开响应，仅含公开帖子内容，不含任何凭证。
