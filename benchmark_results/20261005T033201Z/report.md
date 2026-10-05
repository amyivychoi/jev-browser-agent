# Jev vs Qwen — browser task benchmark

Decision models: **Jev** (`~typesafe/jev-latest`) vs **Qwen** (`qwen/qwen3-32b`). Fallback / text / review in the Jev arm: `qwen/qwen3-32b`. Independent success check: the `expect` phrase on the final page.

## Per case

| case | difficulty | arm | outcome | steps | time | cost (USD) |
| --- | --- | --- | --- | ---: | ---: | ---: |
| wiki-portal-article | easy | jev | success | 2 | 36s | 0.001538 |
| wiki-portal-article | easy | qwen | success | 2 | 50s | 0.001939 |
| wiki-site-search | easy-medium | jev | error | 2 | 25s | 0.001487 |
| wiki-site-search | easy-medium | qwen | blocked:stale-page | 1 | 456s | 0.007268 |
| google-capital | medium | jev | success | 3 | 112s | 0.007437 |
| google-capital | medium | qwen | success | 4 | 96s | 0.012606 |
| google-author | medium-hard | jev | success | 4 | 58s | 0.007723 |
| google-author | medium-hard | qwen | success | 4 | 127s | 0.019498 |
| unit-converter | hard | jev | success | 2 | 64s | 0.004060 |
| unit-converter | hard | qwen | success | 3 | 37s | 0.003394 |

## Success rate (independent expect-text on final page)

| arm | success | total | rate |
| --- | ---: | ---: | ---: |
| jev | 4 | 5 | 80% |
| qwen | 4 | 5 | 80% |

## Totals across all five cases

| arm | time | steps | decisions | fallback | cost total | cost decisions | cost text | cost review |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| jev | 296s | 13 | 21 | 14 | 0.022245 | 0.019889 | 0.002356 | 0.000000 |
| qwen | 766s | 14 | 41 | 41 | 0.044705 | 0.039763 | 0.003994 | 0.000948 |

## 非成功 case 归因（按根因分层）

| case | arm | outcome | 根因层 | 说明 |
| --- | --- | --- | --- | --- |
| wiki-site-search | jev | error | fallback 链 | 候选太多、太相似，Jev 在 95% 置信度下锁不定 target，触发 fallback 交给 qwen；qwen 返回了不在候选动作集里的 operation/target，抛异常终止。 |
| wiki-site-search | qwen | blocked:stale-page | harness（stale-page 自刷新） | Chrome 插件太多（Monica/WebHighlights ~10 次/秒改 DOM），页面 DOM 快速变化，而 qwen 单次决策 ~41s 远慢于这些变化，每次提取完页面返回的思考结果都跟原 DOM 对不上，决策被 stale 作废、反复重试，review 用尽后阻塞。 |



## 实验设计与 Verifier

两臂（jev / qwen）跑同样 5 个任务，唯一变量是主决策模型；jev 臂主决策没把握（置信度 <0.95 / 想 BLOCKED 但仍有可用动作 / 报错）时 fallback 到同一 qwen；qwen 臂主决策直接是 qwen chat。两臂 fallback/text/review 都用同一 qwen，保证唯一差异在主决策。
Orchestrator 对每个 (arm, case) fork 一个 worker 子进程。

验证独立于决策模型：每 case 一句 expect 短语，对最终页 title + 结构化文本 + document.body.innerText（CDP 抓全文）做 casefold 匹配。补 innerText 是因 Browser Use 的 llm_representation / eval_representation 都漏文章正文段落。

## 发现（两模型对比）

| arm | success | time | cost (USD) | decisions | fallback |
| --- | ---: | ---: | ---: | ---: | ---: |
| jev | 4/5 | 296s | 0.022245 | 21 | 14 |
| qwen | 4/5 | 766s | 0.044705 | 41 | 41 |

- **成功率收敛到相同**：修掉 tab 隔离后两臂都 4/5；唯一共同的失败是同一个 case（wiki-site-search），且两臂失败机制不同。
- **失败模式相反**：jev 够快但无法在多个相似选项下进行决策；qwen 缓慢卡死（stale-page ~456s，10 次决策都赶不上页面变动）。
　　　- **jev 的低置信度是「诚实的犹豫」**：Marie Curie 搜索里 Jev 报 op 0.98（要点击）但 target 0.17（搜索框输入后，多个相似的下拉建议选项下不知点哪个），正确 defer 给 fallback，结果 fallback(qwen) 反而输出非法 target——Jev 自我评估是准的，断在 fallback 链。
　　　- **qwen 的慢放大了环境噪声**：qwen 的 stale-page 失败根因是 Chrome 扩展（Monica/WebHighlights）持续改写 DOM + qwen 单决策太慢，永远赶不上页面变动。
- **速度/成本差一个量级**：qwen ~2.6× 时间、~2× 成本；qwen ~41s/决策，jev 几秒/决策。
- **环境是比模型更大的变量**：两个非成功 case 的根因都是 harness/环境（tab 混用、扩展噪声），修完后模型差距很小。

### 为什么偏偏 wiki-site-search 最难？

它是唯一一个「起点页噪声大 + 目标是瞬时下拉 + 候选有歧义」三者叠满的 task，而且两个 Wikipedia task 起点不是同一页：

| task | 起点 URL | 关键动作 | 目标稳不稳 | 歧义 |
| --- | --- | --- | --- | --- |
| wiki-portal（Neptune） | `wikipedia.org` 干净门户 | 输入 + Enter → 直达条目 | 稳（Enter 直达） | 无（标题唯一） |
| wiki-site-search（Marie Curie） | `en.wikipedia.org/wiki/Main_Page` 密集首页 | 输入 + 点下拉项 | 不稳（瞬时下拉） | 高（多个相似项） |
| google-capital / google-author | Google 干净首页 | 输入 + Enter → 点 SERP 结果 | 稳（服务端链接） | 低（答案常在摘要） |
| unit-converter | 转换器站 | 填表单 + 读字段 | 稳（静态控件） | 无 |

三个叠加因素，正好对上两种失败：

- **起点是密集首页**：`Main_Page` 有精选条目 / In the news / 每日图片等几十个可点元素，光「找搜索框」就要在一堆噪声里挑；jev 的 `final_url` 落在 `.../media/File:PtilopusEugeniaeKeulemans_(cropped).jpg`（首页「每日图片」的鸟图）就是点错的证据。
- **目标是瞬时下拉**：输入后才生成、每次按键/失焦就重绘的客户端元素，是最脆弱的点击目标。
- **候选有歧义**：`Marie Curie` / `Marie Curie (disambiguation)` / `Marie Skłodowska-Curie` 等多个相似项。

→ **jev**：密集页 + 歧义下拉锁不定目标 → target 置信度 0.17 → defer 给 qwen → qwen 给非法 target → 报错。
→ **qwen**：瞬时下拉 + 扩展每 ~10 次/秒改 DOM + ~41s/决策 → 决策落地时目标已 stale → 重试 → 死循环 → `blocked:stale-page`。

其它 4 个 task 要么起点干净、要么目标稳、要么 Enter 直达无歧义，扩展噪声伤不到它们。所以难度不在步骤多少，而在环境的噪声。

## 注
- 单次 5-case 高方差，成功率只能当方向，不能当定论。