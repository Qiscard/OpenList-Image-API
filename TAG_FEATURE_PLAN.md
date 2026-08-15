# 标签功能规划方案

> 状态：规划阶段，尚未实现
> 目标：为图片增加可自定义的分类标签与点赞/踩功能，支持访客筛选与排序，由管理员控制启用范围

---

## 1. 功能概述

在图片浏览页的每张图片下方显示一组小按钮（如 喜欢/不喜欢、男生类/女生类/AI类等自定义标签），用户点击后记录到服务器。访客可基于标签进行筛选和排序，管理员可控制功能的启用与使用人员范围。

## 2. 核心需求拆解

### 2.1 标签类型

| 类型 | 说明 | 示例 |
|---|---|---|
| **预设分类标签** | 管理员定义的分类标签，一张图可归入多个 | 男生类、女生类、AI类、风景、动漫 |
| **情感标签** | 内置的喜欢/不喜欢二选一，每张图独立计数 | 👍 喜欢 / 👎 不喜欢 |

### 2.2 使用人员范围

管理员可设置标签投票的参与范围：

| 范围 | 说明 |
|---|---|
| `disabled` | 完全关闭标签功能，浏览页不显示标签按钮 |
| `anonymous` | 所有访客可点击，通过浏览器指纹/IP 哈希去重 |
| `token` | 仅持管理令牌的用户可投票（内部审核场景） |

### 2.3 筛选与排序

- **筛选**：浏览页顶部显示标签云，点击标签后只展示该标签的图片（支持多选交集/并集切换）。
- **排序**：可按"喜欢数降序""不喜欢数降序""喜欢率（喜欢/(喜欢+不喜欢)）降序"排列。

---

## 3. 数据模型设计

### 3.1 标签存储（独立文件，与索引分离）

```json
{
  "schema_version": 1,
  "tags": {
    "/图片虚拟路径/1.jpg": {
      "likes": 12,
      "dislikes": 2,
      "categories": ["男生类", "AI类"],
      "voters": {
        "a3f...": "like",
        "b7e...": "dislike"
      }
    }
  },
  "updated_at": 1786620000
}
```

存储位置：`{state_dir}/tags.json`，与 `index.json` 同目录。

### 3.2 配置项扩展

```json
{
  "tagging_enabled": false,
  "tagging_scope": "anonymous",
  "tagging_categories": ["男生类", "女生类", "AI类"],
  "tagging_allow_custom": false,
  "tagging_sort_default": "likes"
}
```

| 配置项 | 默认值 | 约束 |
|---|---|---|
| `tagging_enabled` | `false` | 布尔，总开关 |
| `tagging_scope` | `anonymous` | `disabled`/`anonymous`/`token` |
| `tagging_categories` | `[]` | 字符串数组，管理员自定义分类 |
| `tagging_allow_custom` | `false` | 布尔，是否允许访客提交新分类 |
| `tagging_sort_default` | `likes` | `likes`/`dislikes`/`ratio` |

---

## 4. API 设计

### 4.1 访客接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tagging/config` | 返回标签配置（哪些分类、是否启用、范围） |
| GET | `/api/tagging/stats?paths=...` | 批量查询图片标签统计（浏览页加载时用） |
| POST | `/api/tagging/vote` | 提交投票/分类 `{path, type, value}` |

#### POST `/api/tagging/vote` 请求体

```json
{
  "path": "/图片虚拟路径/1.jpg",
  "type": "like",
  "value": true
}
```

或分类标记：

```json
{
  "path": "/图片虚拟路径/1.jpg",
  "type": "category",
  "value": "男生类"
}
```

#### 去重机制

- `anonymous` 范围：使用 `IP + User-Agent` 的 SHA256 作为 voter_id，每个 voter_id 对同一图片的同类投票只能记录一次，重复投票覆盖旧值。
- `token` 范围：voter_id 取管理令牌的 SHA256 前缀。

### 4.2 管理接口

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/api/admin/tagging/config` | 管理令牌 | 读取完整标签配置 |
| PUT | `/api/admin/tagging/config` | 管理令牌 | 保存标签配置 |
| GET | `/api/admin/tagging/stats?path=...` | 管理令牌 | 查询指定图片的完整投票详情 |
| DELETE | `/api/admin/tagging/reset?path=...` | 管理令牌 | 重置指定图片的标签数据 |
| POST | `/api/admin/tagging/reindex` | 管理令牌 | 清空所有标签数据重新开始 |

### 4.3 筛选接口扩展

现有 `/api/images/random` 增加查询参数：

| 参数 | 说明 |
|---|---|
| `tag=男生类` | 只返回带该分类标签的图片 |
| `min_likes=5` | 只返回喜欢数 ≥ N 的图片 |
| `min_ratio=0.8` | 只返回喜欢率 ≥ N 的图片 |
| `sort=likes` | 按喜欢数降序返回（非随机） |

---

## 5. 前端交互设计

### 5.1 浏览页标签按钮

每张图片缩略图下方渲染一行小按钮：

```
[👍 12] [👎 2] [男生类] [AI类]
```

- 点赞/踩按钮：点击后高亮，再次点击取消，数字实时更新。
- 分类标签按钮：点击切换该图片的分类归属（仅 `token` 或 `anonymous` 范围显示）。
- 未启用时（`tagging_scope=disabled`）整行不渲染。

### 5.2 筛选栏

浏览页顶部增加标签筛选栏：

```
[全部] [男生类 (234)] [女生类 (156)] [AI类 (89)]    排序: [随机▼]
```

- 点击标签切换筛选，支持多选（并集）。
- 排序下拉：随机、喜欢数、喜欢率。

### 5.3 管理页标签配置 Tab

在管理页新增一个 Tab「标签功能」：

- 启用开关 + 使用范围下拉。
- 分类标签编辑器：可添加/删除/重命名分类。
- 是否允许访客自定义分类。
- 默认排序方式。
- 查看统计/重置数据入口。

---

## 6. 安全与性能考量

### 6.1 防刷

- `anonymous` 范围下，同一 voter_id 对同一图片同类投票限频（建议 1 次/10 秒）。
- 请求体大小限制沿用 `MAX_REQUEST_BODY`（64 KiB）。
- 分类名称做白名单校验（除非 `tagging_allow_custom` 开启且校验长度/字符集）。

### 6.2 性能

- `tags.json` 在内存中维护，定期原子写盘（类似 `IndexRepository`）。
- 批量查询接口 `/api/tagging/stats?paths=...` 一次请求获取当前页所有图片的标签，避免 N+1。
- 筛选基于内存索引，`choose_images` 增加过滤逻辑前先检查是否启用了标签筛选。

### 6.3 数据一致性

- 标签数据与图片索引解耦：图片被删除后其标签数据保留但不在浏览页展示，重建索引后自动清理孤儿数据。
- 支持通过 `POST /api/admin/tagging/reindex` 一键清空重来。

---

## 7. 实施阶段建议

### 阶段一：基础投票（MVP）

- 配置项：`tagging_enabled`、`tagging_scope`。
- 后端：`tags.json` 存储与 `TagRepository` 类、3 个访客 API、2 个管理 API。
- 前端：浏览页点赞/踩按钮、管理页开关。

### 阶段二：分类标签与筛选

- 配置项：`tagging_categories`、`tagging_sort_default`。
- 后端：分类投票 API、`/api/images/random` 筛选参数。
- 前端：分类按钮、筛选栏、排序下拉。

### 阶段三：高级特性

- 访客自定义分类（`tagging_allow_custom`）。
- 管理页统计详情与重置。
- 防刷限频与孤儿数据清理。

---

## 8. 受影响文件清单（实施时）

| 文件 | 改动 |
|---|---|
| `src/openlist_image_api.py` | 新增 `TagRepository`、标签 API 路由、配置项、`choose_images` 筛选逻辑、浏览页前端标签按钮与筛选栏 |
| `src/openlist_tui.py` | 可选：TUI 增加标签配置菜单项 |
| `tests/test_core.py` | 新增 `TagRepository` 与投票逻辑测试 |
| `tests/test_management_features.py` | 新增标签配置与管理 API 测试 |
