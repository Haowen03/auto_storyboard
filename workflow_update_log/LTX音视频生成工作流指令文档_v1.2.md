# LTX 音视频生成工作流指令文档 v1.2

> 适用阶段：本工作流用于 **多宫格分镜参考帧生成工作流完成之后**，即已经获得用户初始 idea、`workflow_summary.json` 以及 `case_final_frameXX.png` 或 `case_edit_frameXX.png` 等参考帧之后，进一步规划并生成 LTX 音视频 shot 参数。
>
> 注意：本文档不是参考帧生成工作流的合并版本，而是其下游的 **LTX 参考图生视频与音频提示词规划工作流**。

> v1.2 更新重点：新增 **参考帧一致性约束、创意越界控制、弱模型安全模式、prompt 证据审查、长 shot 参数防漂移规则**。当调用性能较弱的大模型时，必须优先保证参考帧、用户 idea 与 `workflow_summary.json` 的一致性，而不是扩展新的剧情设定。


---

## 0. 工作流定位

参考帧生成阶段的目标是获得一组具有剧情连续性、环境一致性、角色与核心道具稳定性、镜头多样性的关键视觉支点。LTX 音视频生成阶段的目标不是机械地把每一张参考帧变成一段视频，而是根据用户想要的总时长、参考帧之间的剧情关系、镜头切换强度和动作连续性，决定：

1. 哪些参考帧适合单独生成一个 shot；
2. 哪些参考帧适合合并为一个连续动作 shot；
3. 哪些参考帧适合作为长 shot 内部的中段或后段支点；
4. 每个 shot 应该持续几秒；
5. 每个参考帧应放在 `image_idxs` 的哪个时间位置；
6. `image_strengths` 应该保持 1.0 还是降低以释放运动空间；
7. 每个 shot 的英文 prompt 与 negative_prompt 应该如何写；
8. 多个 shot 拼接时如何避免动作停顿、动量断裂和幻灯片感。

核心原则：

```text
Reference frames are temporal anchors, not mandatory start/end frames.
LTX shots should preserve story continuity, camera continuity, motion momentum, and audio continuity.
Prompt planning must follow the user's idea, workflow_summary.json, and the actual generated reference frames.
```

中文概括：

```text
参考帧是视频内部的剧情支点和镜头支点，不是必须作为每段视频首尾帧使用。LTX 分镜规划必须同时考虑剧情连续、环境一致、镜头切换、动作动量、总时长和音频氛围。
```

---

## 1. 输入材料

执行 LTX 音视频生成规划时，应读取以下输入：

```text
1. 用户初始 idea
2. 用户指定或期望的总时长
3. workflow_summary.json
4. 参考帧文件列表，通常为 case_final_frame01.png 到 case_final_frameNN.png
5. 如果 final_frames 不存在，则使用 case_edit_frameXX.png
6. 如有必要，读取资源库信息 resources 或 resource_registry
7. 用户对镜头、风格、时长、声音、对白、产品展示的补充要求
```

### 1.1 参考帧优先级

默认使用顺序：

```text
case_final_frameXX.png > case_edit_frameXX.png > case_base_frameXX.png
```

原因：

- `case_final_frameXX.png` 通常是最终确认帧；
- `case_edit_frameXX.png` 通常包含镜头再设计结果，适合视频镜头多样性；
- `case_base_frameXX.png` 更适合剧情骨架，不一定适合最终视频镜头。

如果 `workflow_summary.json` 中明确标注 `final_frames` 的 `source` 字段，则应优先使用该字段确定最终参考图。

---

## 2. 必须继承的上游信息

LTX 规划必须继承参考帧生成阶段已经确定的关键信息：

```text
1. case_name
2. idea
3. grid 或帧数 N
4. resources / resource_registry
5. frame_entity_plan
6. reference_resolver
7. edit_plan
8. final_frames
9. 每帧的 story_state
10. 每帧的 edit_strategy 和 camera delta
11. 每帧必须保持的人物、道具、场景和核心商品形象
```

不能只看图像表面重新编故事。应以 `workflow_summary.json` 为剧情与镜头语义的第一依据，再结合参考帧本身判断如何生成视频。

---

## 3. LTX Prompt 基本规范

每个 shot 的 `prompt` 必须遵守 LTX prompt 规范：

```text
1. 英文
2. 单段落
3. 4 到 8 句
4. 现在时
5. 一个清晰的起承转合动作序列
6. 按 Shot -> Scene -> Character -> Action -> Camera -> Audio 的顺序组织
7. 情绪用可见动作、姿态、表情表达，不只写抽象情绪词
8. 正向 prompt 描述应该出现的干净画面，不在正向 prompt 里堆叠 no subtitles / no text 等否定词
9. 字幕、UI、漫画标注、lower-third 等放入 negative_prompt
```

推荐结构：

```text
[STYLE / SHOT]. [SCENE with lighting, atmosphere, texture, time of day]. [CHARACTER details]. [ACTION unfolds from beginning to end in present tense]. [CAMERA movement relative to subject]. [AUDIO details]. [Emotion shown through posture, gesture, or facial expression].
```

### 3.1 Prompt 写法要点

必须写清楚：

- 当前 shot 的镜头类型；
- 当前 shot 的环境与光线；
- 当前人物、服饰、道具；
- 动作从哪里开始、如何推进、到哪里结束；
- 运镜方式，例如 tracking、push in、pull back、pan、over-the-shoulder、low-angle、overhead；
- 音频，包括环境声、脚步、布料、纸张、水声、魔法声、笑声、风声、音乐氛围等；
- 与前后 shot 的承接状态。

避免写：

- 只有静态画面描述；
- 只有参考帧复述；
- 过多抽象情绪标签；
- 同时堆叠 zoom、pan、rotate、tilt 等复杂运镜；
- 将字幕、新闻包装、角色卡片等高风险词放在正向 prompt 中；
- 长篇中文对白直接塞进英文 prompt。

---

## 4. Negative Prompt 标准模板

基础模板：

```text
subtitles, captions, lower-third, chyron, nameplate, news broadcast, TV graphics, UI overlay, watermark, logo, title card, floating text, speech bubble, comic annotation, manga annotation, character introduction overlay, vertical subtitles, vertical title card, duplicate character, extra people, distorted hands, broken fingers, extra fingers, fused fingers, warped face, unstable scene layout, sudden camera jump
```

### 4.1 动作类 shot 追加项

追逐、奔跑、打斗、喷水、飞行等高动态镜头应追加：

```text
freeze frame, still image sequence, slideshow effect, static pose, mannequin pose, repeated identical pose, distorted running pose, broken legs, excessive motion blur, disappearing subject
```

### 4.2 道具或商品 shot 追加项

商品广告或核心道具清晰可见时应追加：

```text
warped product shape, deformed prop, missing nozzle, wrong product color, inconsistent product design, disappearing product, detached water stream, missing water stream
```

### 4.3 魔法、发光文字、符文类 shot 注意

如果剧情本身需要“发光文字、符文、光符、山河图、文字法术”等视觉元素，不要在 negative_prompt 中简单写 `text`，否则可能压掉目标元素。应改成：

```text
subtitles, captions, lower-third, UI overlay, modern fonts, flat graphic overlay, dialogue captions, glyphs arranged like subtitle lines, readable modern text, watermark, logo
```

---

## 5. LTX Shot Planning 总流程

```text
Step 1  读取用户初始 idea、目标总时长、workflow_summary.json 和参考帧文件
Step 2  建立 Reference Frame Inventory，列出每张参考帧的 story_state、camera、focus、motion potential
Step 3  判断参考帧之间的关系：连续动作、镜头切换、情绪缓冲、产品特写、收束帧
Step 4  根据总时长规划 shot 数量和每个 shot 的时长
Step 5  决定每个 shot 使用单帧、多帧，或长 shot 多参考帧
Step 6  为每个 shot 设置 images、image_idxs、image_strengths、video_seconds
Step 7  按 LTX prompt 规范生成英文 prompt 与 negative_prompt
Step 8  输出 LTX Shot Plan 表和逐 shot 参数代码块
Step 9  给出拼接建议和常见失败修正策略
```

---

## 6. Reference Frame Inventory

在规划 LTX shot 前，先为每张参考帧建立清单。

推荐表格：

| Frame | Source File | Story State | Camera Role | Motion Potential | Suggested Use |
|---|---|---|---|---|---|
| frame01 | case_final_frame01.png | 开场建立 | wide establishing | low | single opening shot or first anchor |
| frame02 | case_final_frame02.png | 动作开始 | medium / side | medium | single shot or second anchor |
| frame03 | case_final_frame03.png | 动作触发 | close-up / insert | high | product/action shot |
| frame04 | case_final_frame04.png | 结果反应 | reaction shot | medium | next shot or reaction anchor |

### 6.1 Motion Potential 判断

| 类型 | 说明 | LTX 用法 |
|---|---|---|
| low | 人物静坐、凝视、收束、情绪停顿 | 单帧 5 到 6 秒 |
| medium | 走近、抬手、转身、观察、轻动作 | 单帧 5 到 6 秒或双帧 8 秒 |
| high | 奔跑、追逐、打斗、爆发、喷水、飞行 | 多帧 5 到 8 秒，必要时降低 image_strength |
| transition | 明显切镜、从全景到近景、从过肩到低角度 | 单独成 shot 或作为长 shot 内部锚点 |
| closure | 结尾、余波、情绪收束、光效消散 | 单帧 5 到 6 秒，避免写成静态定格 |

---

## 7. Shot 拆分原则

### 7.1 什么时候使用单帧参考

以下情况适合单帧参考生成一个 shot：

1. 参考帧本身已经是明确镜头切换后的新机位；
2. 当前 shot 是情绪缓冲、反应、收束或产品展示；
3. 前后帧之间不是连续肢体动作，而是正常分镜切换；
4. 单帧包含完整画面关系，LTX 可在前后自由生成动作；
5. 希望避免多参考帧把模型锁成幻灯片。

推荐设置：

```python
images = [f"{FRAMES_DIR}/case_final_frame04.png"]
image_idxs = [0.35]
image_strengths = [1.0]
video_seconds = 5
```

如果该 shot 动作很强，可用：

```python
image_idxs = [0.50]
image_strengths = [0.90]
video_seconds = 6
```

### 7.2 什么时候使用多帧参考

以下情况适合多帧参考生成一个连续 shot：

1. 多张参考帧表现同一动作的起承转合；
2. 参考帧之间机位差异不大，主要是动作推进；
3. 用户希望这段动作不要硬切；
4. 动作需要完整连续性，例如拆信、奔跑追逐、举枪喷水、触碰玉简、能量爆发；
5. 多张参考帧已经经过优化，角色、道具、环境一致。

推荐设置：

```python
images = [
    f"{FRAMES_DIR}/frame01.png",
    f"{FRAMES_DIR}/frame02.png",
]
image_idxs = [0.10, 0.72]
image_strengths = [1.0, 1.0]
video_seconds = 8
```

三帧连续动作可用：

```python
images = [
    f"{FRAMES_DIR}/frame01.png",
    f"{FRAMES_DIR}/frame02.png",
    f"{FRAMES_DIR}/frame03.png",
]
image_idxs = [0.10, 0.48, 0.82]
image_strengths = [0.90, 0.90, 0.90]
video_seconds = 6
```

### 7.3 什么时候使用长 shot

如果参考帧较少，但每张参考帧都具有强镜头多样性和剧情连续性，可以使用一个 10 到 15 秒左右的长 shot，让 LTX 在 shot 内部完成镜头切换。

适用条件：

1. 总参考帧数量较少，例如 3 到 5 张；
2. 参考帧之间剧情紧密；
3. 用户希望减少拼接点；
4. 每张参考帧本身已经是稳定的镜头支点；
5. 本地 LTX 配置支持较长时长。

长 shot 不建议滥用。人物多、动作复杂、商品形象敏感或场景差异大的案例，仍建议拆成多个短 shot。

---

## 8. 时长规划规则

### 8.1 基础经验值

| 参考方式 | 推荐时长 | 说明 |
|---|---:|---|
| 单帧参考，情绪或收束 | 5 到 6 秒 | 不容易戛然而止，也能留出情绪流动 |
| 单帧参考，强动作 | 5 到 6 秒 | 需要降低 image_strength，强化连续动作描述 |
| 双帧参考，普通动作 | 8 秒 | 适合完整动作起承转合 |
| 双帧参考，高动态动作 | 6 到 8 秒 | 防止过长导致漂移 |
| 三帧参考，连续动作 | 5 到 8 秒 | 参考帧太多时避免 1.0 强锁死 |
| 多参考长 shot | 10 到 15 秒 | 仅在参考帧少且剧情连续时使用 |

### 8.2 整数秒规则

除非用户指定小数，默认使用整数秒：

```text
7.5s -> 8s
4.5s -> 5s
5.5s -> 6s
```

### 8.3 总时长分配

先确定总时长，再分配到 shot：

```text
目标总时长 = 20s：3 到 4 个 shot
目标总时长 = 30s：5 到 7 个 shot
目标总时长 = 45s：7 到 10 个 shot
```

如果用户说“不限制 20s 内”，可以根据剧情自然扩展，但仍要避免每个 shot 拖沓。

---

## 9. image_idxs 设置规则

### 9.1 核心原则

参考帧不一定放在首帧或尾帧。它们可以放在视频内部，用作动作和镜头的时间支点。

```text
Do not force every reference image to be the first or last frame.
Leave motion before and after each anchor.
Avoid ending exactly on a reference frame unless the user explicitly wants a freeze-frame ending.
```

### 9.2 推荐位置

| 场景 | image_idxs 推荐 |
|---|---|
| 开场建立，第一帧必须锁定 | `[0.0]` 或 `[0.0, 0.70]` |
| 单帧情绪镜头 | `[0.35]` 到 `[0.45]` |
| 单帧动作镜头 | `[0.45]` 到 `[0.60]` |
| 双帧连续动作 | `[0.10, 0.72]` 或 `[0.18, 0.76]` |
| 三帧连续动作 | `[0.10, 0.48, 0.82]` |
| 多帧长 shot | 根据剧情节奏均匀分布，但最后一帧一般不放到 `1.0` |
| 收尾镜头 | `[0.30]` 到 `[0.40]`，后半段留给情绪自然结束 |

### 9.3 避免的问题

不推荐：

```python
image_idxs = [0.0, 1.0]
```

除非明确需要首尾锁定，否则这种设置容易导致下一段拼接时出现停顿感。

更推荐：

```python
image_idxs = [0.08, 0.78]
```

或：

```python
image_idxs = [0.12, 0.50, 0.82]
```

---

## 10. image_strengths 设置规则

### 10.1 默认值

```python
image_strengths = [1.0] * len(images)
```

适合：

- 人物身份需要强锁定；
- 核心商品或道具需要稳定；
- 场景布局必须保持；
- 参考帧本身动作不强。

### 10.2 高动态镜头

如果出现幻灯片感、人物像静态图片间切换、动作无法连续，可以降低：

```python
image_strengths = [0.90]
```

或：

```python
image_strengths = [0.90, 0.90, 0.90]
```

强动态如奔跑、追逐、跳跃、打斗、喷水追逐时，推荐先试：

```python
image_strengths = [0.90, 0.90, 0.90]
```

如果人物身份或商品形态漂移，再回到 0.95 或 1.0。

### 10.3 商品广告例外

核心商品清晰可见时，不能为了动作完全牺牲商品形象。若水枪、玉简、法器、角色服饰严重变形，应提高 image_strength 或重新优化参考帧。

---

## 11. Shot 内部动作设计规则

每个 prompt 必须写成完整动作，而不是参考帧静态描述。

### 11.1 静态描述错误示例

```text
The boy is running across the lawn while the father runs after him.
```

这容易生成几张跑步姿势图片。

### 11.2 连续动作正确示例

```text
The boy runs forward with quick small steps, his body bouncing naturally with each stride as he looks back and laughs. The father follows one or two meters behind him, taking longer running steps while sweeping a stream of water through the air toward the boy. The camera tracks sideways and slightly backward with them, keeping both characters moving through the frame instead of holding them in a static pose.
```

### 11.3 必须写出的动作细节

高动态镜头应写清：

- 位移方向；
- 步态或动作节奏；
- 手臂、腿部、衣服、头发的运动；
- 道具作用轨迹；
- 摄像机如何跟随；
- 动作如何结束或自然延续到下一 shot。

---

## 12. Shot 间连续性规则

### 12.1 不要使用“首帧 尾帧 首帧”机械拼接

LTX shot 的参考帧不是必须首尾对齐。机械将一个 shot 的尾帧作为下一 shot 的首帧，容易导致：

- 动作动量不一致；
- 人物突然停住；
- 视频边界有明显暂停；
- 前后段像两段独立短片；
- 结尾变成定格画面。

### 12.2 拼接点选择

优先在以下状态切换：

| 拼接点 | 适用案例 |
|---|---|
| 光效最强时 | 魔法爆发、信封金光、能量柱 |
| 水花遮挡时 | 水枪喷水、落水、喷溅 |
| 人物转身或离开画面时 | 追逐、走位、进入新镜头 |
| 镜头自然切换时 | 从全景到近景、从近景到反应 |
| 情绪停顿后 | 大笑、凝视、接招、收束 |

### 12.3 每段结尾留动作余量

最后一个参考帧一般不要放到 `1.0`，而应在后面留 15% 到 25% 的时间，让动作自然继续。

例如：

```python
image_idxs = [0.76]
video_seconds = 6
```

表示参考帧在 4.56 秒左右出现，后面还有 1.44 秒用于自然延续。



### 12.4 Shot 内部转场细节控制

当一个 shot 使用多张参考帧，尤其是 10 秒以上的长 shot 时，不能只写 `then cuts to`、`the camera transitions to` 或让模型随机完成转场。转场本身必须被写成一个可见的画面事件，让 LTX 有明确的视觉路径把前一帧带到后一帧。

核心原则：

```text
Do not ask LTX to randomly transition between reference frames.
Describe the transition as an in-frame visual event, a camera path, an object wipe, a light wipe, a scale change, or a motion bridge.
```

中文概括：

```text
多参考帧之间的转场要像剪辑师设计镜头一样写清楚：用什么物体遮挡画面，用什么运动轨迹引出下一帧，用什么光效吞没画面，用什么镜头运动完成景别变化。
```

#### 12.4.1 常用转场机制

| 转场机制 | 适用场景 | Prompt 写法重点 |
|---|---|---|
| 光效遮挡转场 | 魔法爆发、信封金光、能量柱、闪电 | 写清楚光从哪里扩张，如何填满镜头，光散开后接到什么画面 |
| 物体运动遮挡 | 火焰飘带、莲花瓣、衣袖、水花、人物经过镜头 | 写清楚物体从哪个方向扫过镜头，遮挡后揭示下一机位 |
| 轨迹匹配转场 | 水柱、能量束、飞剑、跑动方向 | 让镜头沿着运动轨迹移动，下一帧继续同一方向动量 |
| 缩放连续转场 | 大莲花变小、小道具靠近、全景推近近景 | 写清楚主体如何收拢、缩小、放大或被镜头推进匹配 |
| 动作桥转场 | 角色抬手、低头、转身、起身、落地 | 用同一动作的前后状态连接两帧，不要让人物突然换姿势 |
| 音频桥转场 | 雷声、鼓点、水花、魔法嗡鸣、笑声 | 让声音提前进入或延续到下一画面，减少拼接断裂感 |

#### 12.4.2 错误写法

```text
The scene transitions from the wide shot to a close-up of Nezha.
```

问题：只说明“要转场”，没有说明画面如何转，模型会随机切镜或生成幻灯片式变化。

#### 12.4.3 正确写法

```text
The camera pushes toward the lotus core as the petals fold inward and the golden flame rises until it fills the lens, creating a controlled light-wipe transition into a low-angle close-up of Nezha's face.
```

这个写法明确了：

```text
转场载体 = 莲心金光
转场动作 = 光焰上升并填满镜头
前后关系 = 全景莲花收拢后进入低角度面部近景
```

#### 12.4.4 典型案例：哪吒火焰莲花复活

对于“哪吒从火焰莲花中复活”这类多参考帧长 shot，可以把四帧之间的转场设计为：

```text
frame01 -> frame02:
大火焰莲花的花瓣向内收拢，莲心光芒上升并吞没镜头，形成从全景沉睡状态到低角度觉醒近景的 light wipe。

frame02 -> frame03:
额头神光与莲心能量束形成 match cut，镜头跟随金色光束向下穿过热浪，绕到莲心背后，进入 OTS 能量贯通视角。

frame03 -> frame04:
火焰飘带或巨大莲花瓣从前景扫过镜头，作为 motion wipe；遮挡解除后，镜头打开为最终 wide aftermath，哪吒落到莲心站定。
```

可直接改写为英文 prompt 句式：

```text
The large petals breathe inward and contract toward the lotus core, then a concentrated golden flare rises into the lens as a controlled light-wipe transition.
The camera follows the golden energy downward through heat haze and dives behind the lotus core, matching the forehead glow into the energy beams that connect to Nezha's back and palms.
A broad fire ribbon arcs across the foreground and becomes a motion wipe, clearing into the final wide aftermath view where Nezha stands at the center of the lotus.
```

#### 12.4.5 长 shot 的转场检查

当一个 shot 使用三张以上参考帧时，prompt 中必须检查：

```text
1. 每两个相邻参考帧之间是否有明确转场载体？
2. 转场载体是否来自画面内已有元素，而不是凭空出现？
3. 转场动作是否有方向，例如 upward, inward, across the lens, toward the camera, following the beam？
4. 转场后是否说明镜头到达了什么新机位或新景别？
5. 转场是否保留动作动量，而不是让角色突然换姿势？
6. 是否避免 uncontrolled random transition, abrupt unrelated scene cut？
```

推荐在长 shot 的 negative_prompt 中加入：

```text
abrupt unrelated scene cut, uncontrolled random transition, slideshow effect, repeated still image, freeze frame, chaotic camera rotation
```


---

## 13. 镜头多样性规则

LTX 阶段应继承参考帧生成阶段的镜头多样性，但不必让每个 shot 都强行换镜头。重点是根据参考帧功能决定：

```text
连续动作段：镜头可以跟拍或轻微变化，重点保持动量。
镜头切换段：可以单帧参考，让 LTX 生成完整镜头语义。
产品或手部细节：使用 medium close-up 或 insert shot。
反应镜头：使用 medium close-up 或 side reaction shot。
收束镜头：使用 wide ending shot 或 quiet medium shot。
```

最终视频中应至少包含：

- 开场建立镜头；
- 一个动作推进镜头；
- 一个细节或产品镜头；
- 一个反应或情绪镜头；
- 一个收束镜头。

如果参考帧数量较少，可以让一个长 shot 内部完成多种镜头变化，例如从 close-up 拉到 over-the-shoulder，再扩展到 wide shot。

---

## 14. 音频设计规则

LTX prompt 的最后一到两句应包含音频设计。

### 14.1 环境音

根据场景加入：

```text
soft night wind, distant insects, wooden creaks, room tone, garden ambience, faint traffic hum, distant thunder
```

### 14.2 动作音效

根据动作加入：

```text
paper tearing, cloth movement, footsteps on grass, water splashing, magical hum, crackling sparks, chair creak, breathing, laughter
```

### 14.3 音乐

广告或情绪片可加入轻音乐，但不要过度复杂：

```text
a light upbeat summer music bed
an airy magical music swell
soft emotional strings
```

### 14.4 对白

如果需要对白，推荐简短意图描述，不推荐长文本台词。若字幕伪影严重，改写为：

```text
he speaks briefly in Mandarin with natural mouth movement
she responds softly with synchronized mouth movement
```

不要在 prompt 中塞长段中文台词。

---

## 15. 输出格式

每次 LTX 工作流输出应包含两部分。

### 15.1 Shot Plan 表

| Shot | Reference Images | Duration | image_idxs | Shot Function | Transition Notes |
|---|---|---:|---|---|---|
| Shot 1 | frame01 | 6s | [0.0] | 开场建立 | 结尾保持动作余量 |
| Shot 2 | frame02, frame03 | 8s | [0.12, 0.76] | 连续动作 | 在动作进行中切出 |

### 15.2 参数代码块

格式固定为：

```python
images = [
    f"{FRAMES_DIR}/case_final_frame01.png",
    f"{FRAMES_DIR}/case_final_frame02.png",
]
image_idxs = [0.10, 0.72]
image_strengths = [1.0, 1.0]
video_seconds = 8

prompt = "Cinematic ..."

negative_prompt = "subtitles, captions, lower-third, ..."
```

如果用户要求 `canshu.py` 格式，则按其脚本结构输出或生成 `.py` 文件。

---

## 16. Shot Resolver 输出格式

在正式参数前，大模型应内部完成 Shot Resolver 判断。必要时可以显式输出：

```json
{
  "shot": "shot_03",
  "story_function": "the boy fires the water blaster and the father reacts",
  "reference_images": [
    "case_final_frame03.png",
    "case_final_frame04.png"
  ],
  "selection_reason": {
    "case_final_frame03.png": "product-focused firing moment",
    "case_final_frame04.png": "reaction result after being sprayed"
  },
  "duration_reason": "two-frame action-and-reaction shot, 8 seconds gives enough time for firing, water travel, and reaction",
  "image_idx_reason": "first anchor appears early as product shot, second anchor appears later as reaction result, leaving tail motion after impact",
  "continuity_notes": "cut from previous shot when the boy raises the blaster; cut to next shot after the father laughs and accepts the game"
}
```

---

## 17. 参考帧组合策略

### 17.1 连续动作型

例：打开信封、按下玉简、奔跑追逐、举枪喷水。

```text
多帧合并，image_idxs 分散在 shot 内部，prompt 强调连续动作和镜头跟随。
```

### 17.2 镜头切换型

例：全景切近景、过肩镜头、低角度大全景、俯视镜头。

```text
通常单帧独立成 shot，或作为长 shot 内部中段支点。
```

### 17.3 情绪缓冲型

例：两人对视大笑、人物沉默凝视、父亲笑着接招。

```text
单帧 5 到 6 秒，prompt 写动作余韵，不写静态定格。
```

### 17.4 产品展示型

例：水枪近景、道具特写、手部触碰、核心商品发射。

```text
单帧或双帧。需要强锁定商品形状，水流、喷嘴、握持关系必须清楚。
```

### 17.5 收束型

例：光字消散、魔法余波、人物收起道具、父子停下大笑。

```text
单帧 5 到 6 秒。参考帧作为中前段锚点，后面留给余波自然结束。
```

---

## 18. 常见失败与修正

### 18.1 像几张图片在放映

原因：

- prompt 只描述静态状态；
- image_strength 太高；
- image_idxs 过于平均且动作描述不足；
- 多帧之间动作差异大但未写动量；
- 参考帧过多导致模型被锁死。

修正：

```text
1. 降低 image_strengths 到 0.90 或 0.85
2. 减少参考帧数量
3. 强化连续步态、手臂摆动、衣物运动、水流轨迹等动作描述
4. 加入 camera tracks / follows / moves with them
5. negative_prompt 加 freeze frame, still image sequence, slideshow effect, static pose
```

### 18.2 shot 结尾戛然而止

修正：

```text
1. 单帧 shot 提升到 5 或 6 秒
2. 双帧 shot 提升到 8 秒
3. 最后一张参考帧不要放到 1.0，改到 0.72 到 0.82
4. prompt 结尾写动作余韵或自然延续
```

### 18.3 人物或商品漂移

修正：

```text
1. image_strength 提高到 0.95 或 1.0
2. prompt 明确服饰、道具颜色、核心结构
3. 使用更稳定的参考帧
4. 如果上游参考帧商品已经变形，先重新编辑参考帧，不要直接让 LTX 修正
```

### 18.4 水柱、光效、动作轨迹不稳定

修正：

```text
1. 在 prompt 中写清楚轨迹从哪里开始、到哪里结束
2. 明确 nozzle, stream, arc, droplets, impact point
3. 魔法光效写 core, beam, sparks, particles, reflection
4. 必要时先优化参考帧中的水柱或光效
```

### 18.5 最后一段变成定格画面

修正：

```text
1. 不要在 prompt 中写 poster-like ending、freeze、hold as final frame
2. 收尾 shot 写完整动作：走近、停下、呼吸、对视、笑、放松、余波消散
3. 定格、logo、slogan 留给后期剪辑，不让 LTX 在生成阶段完成
```

---

## 19. 执行检查清单

生成 LTX 参数前检查：

```text
1. 是否读取了用户 idea？
2. 是否读取了 workflow_summary.json？
3. 是否使用 final_frames 或 edit_frames 作为参考帧？
4. 是否根据 story_state 判断参考帧功能？
5. 是否考虑用户目标总时长？
6. 是否区分了连续动作和镜头切换？
7. 是否避免把所有参考帧都机械设置为首尾帧？
8. 是否为每个 shot 设置合理整数秒？
9. image_idxs 是否单调递增？
10. 最后一张参考帧是否避免无必要地放在 1.0？
11. prompt 是否英文、单段、4 到 8 句、现在时？
12. prompt 是否包含 shot、scene、character、action、camera、audio？
13. negative_prompt 是否包含字幕和静态画面抑制项？
14. 多参考帧 shot 内部是否写清楚转场细节，而不是让模型随机转场？
15. prompt 中是否存在未被 idea、workflow_summary 或参考帧支撑的新剧情元素？
16. 是否已删除新人物、新道具、新形态、新地点、新结局等创意越界内容？
17. image_strength 是否足以保持角色、场景和核心道具稳定？
18. 第一张参考帧是否足够早以锁定关键场景？
19. 最后一张参考帧之后是否只写余波收束，而不是写新高潮？
20. negative_prompt 是否包含当前 case 的漂移风险项？
21. 各 shot 的拼接点是否自然？
22. 是否给出最终参数代码块？
```

---

## 20. 输出示例模板

```python
images = [
    f"{FRAMES_DIR}/case_edit_frame01.png",
    f"{FRAMES_DIR}/case_edit_frame02.png",
]
image_idxs = [0.10, 0.72]
image_strengths = [1.0, 1.0]
video_seconds = 8

prompt = "Cinematic summer backyard commercial video, beginning with a relaxed establishing view of the father seated in the sunny backyard before the boy enters with a blue shark-shaped water blaster. Warm golden-hour light filters through the trees, the green lawn and wooden deck remain stable, and a glass of lemonade sits on the table as a summer lifestyle anchor. The boy walks closer with a playful smile, holding the water blaster in both hands while the father turns toward him and notices the challenge. The camera gently tracks from the table foreground toward the two characters, shifting the quiet afternoon into a playful family moment. Soft outdoor ambience, leaves moving in the breeze, distant neighborhood sounds, and light laughter carry the scene forward."

negative_prompt = "subtitles, captions, lower-third, chyron, nameplate, UI overlay, watermark, title card, floating text, speech bubble, comic annotation, character introduction overlay, extra people, duplicate father, duplicate child, distorted hands, broken fingers, extra fingers, warped water blaster, unstable backyard layout, sudden camera jump"
```

---

## 21. 创意越界与参考帧一致性控制（强制）

LTX 阶段的 prompt 不是重新创作剧情，而是把用户 idea、`workflow_summary.json` 和最终参考帧组织成连续视频。所有剧情、动作、道具、人物形态和场景变化都必须能从以下三类证据中找到依据：

```text
1. 用户初始 idea
2. workflow_summary.json 中的 story_state、edit_strategy、final_frames、resources
3. 实际参考帧中清晰可见的视觉内容
```

核心原则：

```text
Prompt must be evidence-grounded.
Do not invent unsupported plot, forms, props, characters, locations, powers, weapons, transformations, or endings.
```

中文概括：

```text
LTX prompt 必须基于证据写作。没有出现在用户 idea、summary 或参考帧中的剧情元素，不得随意加入。
```

### 21.1 允许扩展的内容

在不改变剧情和视觉身份的前提下，允许补充以下内容：

| 类型 | 允许原因 | 示例 |
|---|---|---|
| 连续动作细节 | 帮助静态参考帧变成视频 | 手指微动、衣摆飘动、缓慢抬头、呼吸、脚步 |
| 光效和粒子余波 | 帮助完成转场与动态 | 火星飘散、莲心脉冲、金光扩张、水滴飞溅 |
| 镜头运动 | 视频生成必须明确运镜 | push in、pull back、track sideways、arc around |
| 音效和音乐 | LTX 音视频需要声音设计 | 火焰噼啪、雷声、魔法嗡鸣、笑声、脚步 |
| 合理转场载体 | 让多参考帧顺滑衔接 | 光效 wipe、物体扫镜、动作桥、轨迹匹配 |

这些补充必须服务于已存在的参考帧状态，不能把剧情带向新的事件。

### 21.2 禁止扩展的内容

以下内容如果没有被用户明确要求、没有出现在 `workflow_summary.json`，也没有在参考帧中可见，则禁止加入 prompt：

| 禁止类型 | 风险 |
|---|---|
| 新人物、新怪物、新群体 | 破坏主体一致性 |
| 新武器、新法器、新商品 | 破坏道具与剧情一致性 |
| 新形态、新变身阶段 | 导致角色漂移或肢体畸形 |
| 新地点、新建筑、新地貌 | 破坏场景连续性 |
| 新结局或额外高潮 | 导致尾段出现突兀剧情 |
| 没有参考的强动作 | 导致模型自由发挥、跳帧或失控 |
| 强形态词，如 three heads and six arms | 若无参考图，极易产生多头多臂畸形 |
| cosmic ring、spiritual chains、molten fissure 等未授权物 | 容易把原始场景改成另一段剧情 |

### 21.3 Prompt 证据审查表

生成 LTX 参数前，大模型必须在内部完成如下审查。必要时可显式输出。

| Prompt 元素 | 是否来自 idea | 是否来自 summary | 是否来自参考帧 | 处理 |
|---|---|---|---|---|
| Nezha | yes | yes | yes | 保留 |
| flame lotus | yes | yes | yes | 保留 |
| ruined rune platform | maybe | yes | yes | 保留 |
| three heads and six arms | no | no | no | 删除 |
| cosmic ring | no | no | no | 删除 |
| ascends into heaven | no | no | uncertain | 改为 stands reborn at the lotus center |
| light wipe by lotus core | no | transition design | visually plausible | 可保留 |
| fire ribbon foreground wipe | no | transition design | visually plausible | 可保留 |

### 21.4 强制改写规则

当 prompt 出现无参考支撑的发散内容时，必须主动改写为与参考帧一致的表达。

错误：

```text
Nezha ascends into stormy skies, three heads and six arms phasing into visibility, cosmic ring slamming onto his wrist.
```

问题：三头六臂、宇宙环、升空收束均未由参考帧支撑，且会导致角色形态漂移。

正确：

```text
Nezha descends into a firm standing pose at the center of the flame lotus, residual fire aura fading around his hands and feet while the rune platform remains visible beneath him.
```

### 21.5 正向 prompt 中的保守约束写法

正向 prompt 不宜堆叠大量 `do not`，但可以用肯定句限定剧情范围。

推荐写法：

```text
The sequence stays within the resurrection ritual shown by the reference frames, moving from the sleeping lotus state to awakening, energy connection, and final standing rebirth.
```

```text
The scene keeps the same ruined rune platform, flame lotus structure, Nezha's red outfit, twin topknots, and storm-lit atmosphere throughout the shot.
```

不推荐：

```text
Add a new divine weapon, three heads and six arms, and a cosmic ring to make the climax stronger.
```

### 21.6 Negative Prompt 中的越界抑制项

当调用性能较弱的大模型时，negative_prompt 应追加与当前 case 相关的越界抑制项。

通用追加：

```text
unsupported new plot, unrelated transformation, new character, new weapon, new prop, new location, changing ending, random power-up, inconsistent final state
```

角色形态敏感时追加：

```text
extra heads, extra arms, extra faces, multiple bodies, duplicate character, uncontrolled transformation, changing outfit, wrong hairstyle
```

场景敏感时追加：

```text
unrelated landscape, new building, changing platform, disappearing rune platform, inconsistent scene layout
```

道具敏感时追加：

```text
inconsistent prop shape, disappearing core object, generic fire effect, wrong artifact design
```

### 21.7 Case-specific Forbidden Expansion List

每个 case 都应根据 idea、summary 和参考帧自动生成一个 Forbidden Expansion List。

示例：哪吒火焰莲花复活 case

```text
Forbidden unless explicitly shown in reference frames:
three heads and six arms, cosmic ring, spiritual chains, new weapon, new enemy, new battlefield, flying into heaven, leaving the lotus platform, molten fissure replacing the rune platform, extra divine form, crowd, title card
```

示例：水枪广告 case

```text
Forbidden unless explicitly shown in reference frames:
different toy gun, unrelated children, swimming pool, new parent, indoor scene, night scene, brand logo, product package, text slogan inside LTX output, water gun changing color, aggressive combat mood
```

---

## 22. 参数设置防漂移规则

创意越界不仅来自 prompt，也来自参数设置不合理。特别是长 shot、多参考帧和低 image_strength 组合，容易让 LTX 在未被参考帧约束的时间段自由发散。

### 22.1 image_strength 安全区间

| 场景 | 推荐强度 | 说明 |
|---|---:|---|
| 稳定剧情复现 | 0.90 到 1.00 | 默认安全区间 |
| 长 shot 多参考帧 | 0.88 到 0.95 | 兼顾转场和一致性 |
| 强动态动作 | 0.85 到 0.92 | 释放动作空间，但要监控漂移 |
| 商品或核心道具清晰可见 | 0.95 到 1.00 | 道具不能漂 |
| 角色形态敏感 | 0.92 到 1.00 | 防止脸、服装、发型变化 |
| 激进创意测试 | 0.75 到 0.85 | 仅用于实验，不作为默认工作流 |

如果目标是参考帧一致性，避免默认使用：

```python
image_strengths = [0.75, 0.78, 0.80, 0.82]
```

更稳妥：

```python
image_strengths = [0.90, 0.90, 0.92, 0.92]
```

### 22.2 image_idxs 防漂移规则

长 shot 中，如果第一帧是关键场景锚点，第一张参考图建议放在：

```python
image_idxs[0] = 0.0
```

或最多：

```python
image_idxs[0] = 0.05
```

不建议在关键开场使用：

```python
image_idxs[0] = 0.12
```

除非确实需要前置环境铺垫，并且 prompt 已明确场景不能改变。

最后一张参考帧一般放在：

```python
0.82 到 0.90
```

但尾段只能写余波、呼吸、光效消散、自然收束，不能引入新剧情。

### 22.3 长 shot 剧情容量限制

长 shot 不是信息越多越好。15 到 20 秒长 shot 通常只能承载：

```text
4 到 5 个关键状态
3 到 4 个主要转场
1 个主动作链
1 个最终收束
```

禁止在 15 秒内同时塞入过多新事件，例如：

```text
沉睡 -> 苏醒 -> 起身 -> 能量贯通 -> 站定 -> 升空 -> 三头六臂 -> 新法器降临 -> 天地大战
```

应改为：

```text
沉睡 -> 苏醒 -> 能量贯通 -> 站定复活
```

### 22.4 尾段防突兀剧情规则

如果最后一张参考帧已经是收束帧，尾段只能描述：

- 光效变弱；
- 粒子消散；
- 角色呼吸稳定；
- 衣摆或飘带慢慢落下；
- 摄像机轻微后撤或稳定；
- 环境音和音乐收束。

禁止在最后 10% 到 20% 时间内加入：

- 新变身；
- 新敌人；
- 新地点；
- 角色突然离开；
- 飞升到另一个空间；
- 额外武器或法器出现；
- logo、字幕、slogan 内嵌进 LTX 画面。

### 22.5 参数合理性检查

输出参数前检查：

```text
1. image_idxs 是否覆盖每个参考帧的关键 story_state？
2. 第一张参考帧是否足够早以锁定场景？
3. 最后一张参考帧之后是否只保留余波，不引入新剧情？
4. image_strength 是否足以保持人物、场景、道具稳定？
5. 如果 image_strength 低于 0.85，是否明确标注为激进测试？
6. 长 shot 是否超过剧情容量？
7. prompt 中是否存在没有参考证据的新名词？
8. negative_prompt 是否抑制了当前 case 的主要漂移风险？
```

---

## 23. 弱模型安全模式

当调用性能较低或容易发散的大模型时，必须启用弱模型安全模式。

### 23.1 安全模式输出流程

在生成最终参数前，先让大模型按以下顺序组织内部推理：

```text
Step A  Extract Evidence
        从 idea、workflow_summary.json 和参考帧中提取可用剧情证据。

Step B  Build Allowed Story Range
        只列出允许出现在 prompt 里的剧情状态、人物、道具、场景和动作。

Step C  Build Forbidden Expansion List
        列出禁止出现的无参考支撑元素。

Step D  Select Shot Structure
        决定单帧、多帧或长 shot，但不得改变参考帧 story_state 顺序。

Step E  Set Conservative Parameters
        默认使用 0.90 到 1.00 的 image_strength，第一帧尽量前置。

Step F  Write Grounded Prompt
        prompt 只能使用 Allowed Story Range 中的剧情元素，并可补充运镜、转场和音频。

Step G  Run Drift Check
        检查 prompt 中每个关键名词和动作是否有证据来源。
```

### 23.2 安全模式 Prompt 模板

```text
You must write an LTX prompt grounded only in the user's idea, workflow_summary.json, and visible reference frames. Do not introduce any new character, new prop, new form, new location, new weapon, new ending, or unsupported transformation. You may only add camera movement, small physical motion, light effects, transition carriers, and audio details that help connect the given reference frames. If an element is not visible in the reference frames and not stated in the idea or summary, remove it.
```

### 23.3 安全模式参数默认值

```python
# 单帧
image_idxs = [0.35]
image_strengths = [0.95]
video_seconds = 5 或 6

# 双帧
image_idxs = [0.08, 0.78]
image_strengths = [0.95, 0.95]
video_seconds = 8

# 三到五帧长 shot
image_idxs = [0.0, 0.32, 0.64, 0.88]  # 按帧数调整
image_strengths = [0.90, 0.90, 0.92, 0.92]
video_seconds = 10 到 20
```

### 23.4 弱模型输出后的自动拒绝规则

如果生成结果中出现以下任一情况，应直接判定为需要重写 prompt：

```text
1. prompt 出现未授权新角色、新武器、新法器、新地点；
2. prompt 改变了最后一帧的收束状态；
3. prompt 把参考帧中的道具替换为另一个概念；
4. prompt 加入新变身形态但没有参考图；
5. prompt 的尾段出现新的高潮，而不是余波收束；
6. image_strength 普遍低于 0.85 但没有说明是激进测试；
7. image_idxs 第一帧过晚导致开场场景失去锚点；
8. 长 shot 中参考帧之间没有具体转场载体；
9. negative_prompt 没有包含当前 case 的漂移风险项。
```

---

## 24. 一句话总结

```text
LTX 音视频生成工作流的核心不是逐帧转视频，而是把参考帧重新组织为时间支点：连续动作可多帧合并，镜头切换可单帧独立，参考帧可放在 shot 内任意位置，并通过英文 prompt 明确动作、运镜、转场载体、音频和拼接连续性；同时必须以用户 idea、workflow_summary.json 和参考帧为剧情边界，禁止无参考支撑的创意越界。
```
