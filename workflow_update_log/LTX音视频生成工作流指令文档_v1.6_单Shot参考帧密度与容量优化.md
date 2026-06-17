# LTX 音视频生成工作流指令文档 v1.6

> v1.6 更新重点：新增 **单 Shot 参考帧密度与容量优化规则**，解决“大模型在 12s shot 中塞入过多参考帧，导致像放 PPT、动作和转场没有展开空间”的问题。

---

## 0. 本次修正的核心问题

经过实际实验发现，旧版工作流虽然规定了 shot 的时长、参考帧排序、bridge shot、stitch mode 等内容，但**没有足够明确地限制“单个 shot 能承载多少张参考帧”**。这会导致大模型在规划时出现如下错误：

```text
12s shot -> 使用 6 张参考帧
```

虽然表面上覆盖了更多参考帧，但实际上会带来明显副作用：

1. 每个参考帧只分到很短的时间；
2. shot 内部没有余量去完成动作展开；
3. 相邻参考帧之间没有足够时长去做转场；
4. 模型只能被迫做“参考帧轮播”，产生 PPT 感；
5. 视频更像幻灯片切换，而不是连续镜头叙事。

因此，v1.6 明确引入：

```text
Shot Capacity Rule
Reference Frame Density Rule
Minimum Temporal Breathing Room Rule
```

中文概括：

```text
每个 shot 的参考帧数量必须受时长约束。时长越短，参考帧越少；必须为动作推进和转场保留足够的叙事余量，不能把一个短 shot 塞成参考帧轮播。
```

---

## 1. v1.6 核心原则

### 1.1 一个 shot 不是参考帧容器，而是叙事时间段

大模型规划时不能只想“怎样尽量多塞参考帧”，而必须先想：

```text
This shot has only X seconds.
How many anchor states can it truly unfold while still leaving room for motion and transitions?
```

也就是说，**shot 时长先决定容量，参考帧数量必须服从容量**。

---

## 2. Shot Capacity Rule

### 2.1 单 shot 参考帧数量上限（硬规则）

| shot 时长 | 推荐参考帧数量 | 可接受上限 | 不推荐 |
|---|---:|---:|---:|
| 4–6s | 1–2 张 | 2 张 | 3+ |
| 7–9s | 2–3 张 | 3 张 | 4+ |
| 10–12s | 3–4 张 | 5 张 | 6+ |
| 13–15s | 4–5 张 | 6 张 | 7+ |
| 16–20s | 5–6 张 | 7 张 | 8+ |
| 20s+ | 6–8 张 | 8 张 | 9+ 需谨慎 |

### 2.2 强制解释

对你当前实验结论，v1.6 直接固化为规则：

```text
12s shot 最合适的是 4–5 张参考帧；
15s shot 使用 5–6 张参考帧可以接受；
20s shot 才适合一次性容纳更多参考帧。
```

因此下面这种规划应直接判为不合理：

```text
12s shot -> 6 张参考帧
```

除非这 6 张参考帧几乎属于极微小动作差异、且明确是“高密度 montage 风格”，否则默认视为过密。

---

## 3. Reference Frame Density Rule

### 3.1 密度定义

定义：

```text
reference_frame_density = number_of_reference_frames / shot_seconds
```

例如：

- 12s / 6 帧 = 0.50 帧每秒（过密）
- 12s / 4 帧 = 0.33 帧每秒（较合理）
- 15s / 5 帧 = 0.33 帧每秒（较合理）
- 20s / 6 帧 = 0.30 帧每秒（较舒适）

### 3.2 推荐密度区间

| 密度 | 判断 |
|---|---|
| ≤ 0.28 | 宽松，可充分展开动作与转场 |
| 0.29–0.36 | 合理，推荐区间 |
| 0.37–0.42 | 偏紧，仅适用于节奏更快的 shot |
| > 0.42 | 过密，容易产生 PPT 感 |

### 3.3 强规则

```text
If reference_frame_density > 0.42,
the planner must reduce the number of reference frames,
or split the content into more shots.
```

中文概括：

```text
如果单 shot 的参考帧密度超过 0.42，则必须减少参考帧数量，或把内容拆到更多 shot 中。
```

---

## 4. Minimum Temporal Breathing Room Rule

一个 shot 不仅要展示参考帧，还必须给每个参考帧之间留下“呼吸空间”。

### 4.1 呼吸空间包括

1. 动作起势；
2. 动作延续；
3. 运镜移动；
4. 转场载体展开；
5. 余波收束。

### 4.2 规则

如果一个 shot 中有 N 张参考帧，则至少需要：

```text
每对相邻参考帧之间保留可见的动作 / 转场时间
```

因此，大模型不能简单地把参考帧均匀密排成：

```python
image_idxs = [0.00, 0.20, 0.40, 0.60, 0.80, 0.95]
```

这类设置在 12s shot 中通常过于拥挤。

更合理的做法是：

- 减少参考帧数量；
- 留出更大间距；
- 让 prompt 有空间刻画 motion bridge 与 transition carrier。

---

## 5. 规划优先级修正

旧版工作流中，大模型容易默认：

```text
尽量覆盖更多参考帧 > 保证 shot 展开空间
```

v1.6 修正为：

```text
Shot readability and motion continuity > reference frame coverage density
```

中文：

```text
shot 的可读性、动作连续性和转场流畅性，优先级高于“尽量多覆盖参考帧”。
```

也就是说：

- 宁可 12s shot 只用 4 张参考帧；
- 也不要硬塞 6 张导致每个分镜都没有展开空间。

---

## 6. 12s / 15s / 20s 的推荐口径

### 6.1 12s shot

推荐：

```text
3–4 张最稳
4–5 张可接受
6 张通常过密，不推荐
```

适合：
- 一个完整动作段；
- 一个包含若干参考帧的连续过渡段；
- 一个主 shot。

### 6.2 15s shot

推荐：

```text
4–5 张最稳
5–6 张可接受
7 张通常偏密
```

适合：
- 更完整的起承转合；
- 更丰富的镜头变化；
- 若有 bridge 或较强转场可承载更多帧。

### 6.3 20s shot

推荐：

```text
5–6 张最稳
6–7 张可接受
8 张需要较强规划能力
```

适合：
- 参考帧原本就是围绕 20s 左右内容准备的 case；
- 有较充分的动作展开空间；
- 长 shot 内部可承载更多转场与收束。

---

## 7. 当总帧数较多时如何处理

以当前常见 case 为例：

```text
总视频时长：24s
总参考帧：9 张
```

这些 9 张参考帧本来就是面向 24s 内容准备的，因此不应在单个 12s shot 中塞入过多。

### 7.1 错误规划

```text
shot1 = 12s, 使用 6 帧
shot2 = 12s, 使用 3 帧
```

问题：
- shot1 过密；
- shot2 过松；
- 节奏失衡。

### 7.2 推荐规划

```text
shot1 = 10–12s, 使用 4–5 帧
bridge = 3–5s, 使用 2–3 帧
shot2 = 10–12s, 使用 4–5 帧
```

或者：

```text
shot1 = 12s, 使用 4 帧
shot2 = 12s, 使用 4–5 帧
bridge 单独补
```

核心思想：

```text
不要让某个短 shot 过度承担整体 24s 的剧情容量。
```

---

## 8. Shot Resolver 新增约束

在正式输出参数前，大模型必须先检查：

```text
1. 当前 shot 秒数是多少？
2. 当前 shot 计划使用多少张参考帧？
3. 当前密度是否超过推荐阈值？
4. 是否仍然有足够空间刻画动作、转场、余波？
5. 若过密，是否应减少参考帧或拆出 bridge / 新 shot？
```

### 8.1 新增自动拒绝规则

以下情况应判为规划失败并强制重写：

```text
1. 12s shot 使用 6+ 张参考帧；
2. 15s shot 使用 7+ 张参考帧；
3. density > 0.42 且无特殊说明；
4. prompt 无法为每个相邻参考帧提供动作或转场展开；
5. shot 更像 slideshow，而不是 motion sequence。
```

---

## 9. Prompt 编写也必须适配容量

如果一个 shot 的参考帧偏多，prompt 往往会被迫写成：

```text
then this happens, then this happens, then this happens...
```

这会让模型更像在“放 PPT”。

v1.6 要求：

```text
The number of described action beats in the prompt should match the temporal capacity of the shot.
```

中文：

```text
prompt 中的动作节点数量必须和 shot 的时间容量相匹配。
```

因此：
- 12s shot 不要写太多独立小分镜；
- 参考帧多时，也应通过“合并成更连续的动作段”来描述，而不是逐帧点名。

---

## 10. 与 Bridge Shot 的关系

v1.6 的这一优化和 bridge shot 是互补关系。

如果 12s shot 里放不下太多参考帧，不应强塞，而应：

1. 减少主 shot 参考帧数量；
2. 把边界过渡拆给 bridge shot；
3. 让 bridge 承担跨段转场；
4. 让主 shot 保持更舒适的叙事密度。

也就是说：

```text
Bridge shot is not only for cross-shot transitions.
It also helps reduce overcrowding inside the main shots.
```

中文：

```text
bridge shot 不只是为了跨-shot转场，也能帮助减轻主 shot 内部的拥挤度。
```

---

## 11. 输出参数时的建议模板

### 11.1 12s shot 推荐示例

```python
# 12s main shot
images = [
    f"{FRAMES_DIR}/case_final_frame01.png",
    f"{FRAMES_DIR}/case_final_frame02.png",
    f"{FRAMES_DIR}/case_final_frame03.png",
    f"{FRAMES_DIR}/case_final_frame04.png",
]
image_idxs = [0.00, 0.28, 0.58, 0.88]
image_strengths = [0.92, 0.92, 0.92, 0.95]
video_seconds = 12
```

这是一个比较舒适的 4 帧 / 12s 布局。

### 11.2 15s shot 推荐示例

```python
# 15s main shot
images = [
    f"{FRAMES_DIR}/case_final_frame01.png",
    f"{FRAMES_DIR}/case_final_frame02.png",
    f"{FRAMES_DIR}/case_final_frame03.png",
    f"{FRAMES_DIR}/case_final_frame04.png",
    f"{FRAMES_DIR}/case_final_frame05.png",
]
image_idxs = [0.00, 0.22, 0.46, 0.70, 0.90]
image_strengths = [0.92, 0.92, 0.90, 0.92, 0.95]
video_seconds = 15
```

### 11.3 20s shot 推荐示例

```python
# 20s long shot
images = [
    f"{FRAMES_DIR}/case_final_frame01.png",
    f"{FRAMES_DIR}/case_final_frame02.png",
    f"{FRAMES_DIR}/case_final_frame03.png",
    f"{FRAMES_DIR}/case_final_frame04.png",
    f"{FRAMES_DIR}/case_final_frame05.png",
    f"{FRAMES_DIR}/case_final_frame06.png",
]
image_idxs = [0.00, 0.16, 0.34, 0.52, 0.72, 0.90]
image_strengths = [0.92, 0.92, 0.90, 0.90, 0.92, 0.95]
video_seconds = 20
```

---

## 12. 执行检查清单（v1.6 新增）

生成 LTX 参数前新增检查：

```text
1. 当前 shot 时长是多少？
2. 当前 shot 使用了多少张参考帧？
3. 当前 reference_frame_density 是否在合理区间？
4. 12s shot 是否错误地塞入了 6+ 张参考帧？
5. 是否给动作展开和转场保留了时间？
6. prompt 是否像在描述 motion sequence，而不是逐帧念 PPT？
7. 是否可以通过 bridge shot 或新 shot 降低主 shot 的拥挤度？
8. 整个时长分配是否均衡，而不是某一个 shot 承担过多内容？
```

---

## 13. 一句话总结

```text
v1.6 adds an explicit shot-capacity rule: the number of reference frames inside a shot must be limited by the shot duration, so that each shot still has enough temporal room for motion, transitions, and narrative unfolding instead of turning into a slideshow.
```

中文概括：

```text
v1.6 明确加入了单 shot 容量规则：参考帧数量必须受 shot 时长约束，必须给动作、转场和叙事展开保留足够时间，避免视频变成参考帧轮播式的 PPT。
```
