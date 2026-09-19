# 配置变更三方合并 API

基于 **Python + FastAPI + SQLite** 的配置三方合并服务。客户端提交 `base`（共同祖先）、`left`、`right` 三份 JSON，服务按 JSON Pointer 递归合并对象：单侧修改自动采用、双侧同值修改自动合并、双侧不同修改在冲突处保留占位符并返回双方候选。客户端携带当前 `revision` 分批提交冲突取值，全部解决后生成**不可变**结果。

## 快速启动（单条命令）

```bash
docker compose up --build
```

依赖安装（镜像构建时）与 SQLite 建表（应用启动时）自动完成，无需任何额外步骤。

- **API 地址**：http://localhost:8000
- **交互式文档（Swagger UI）**：http://localhost:8000/docs
- **端口**：`8000`
- 健康检查：`GET http://localhost:8000/health`

SQLite 数据文件持久化在名为 `merge-data` 的 Docker volume 中（容器内路径 `/data/merges.db`，可通过环境变量 `DATABASE_PATH` 修改）。

## 合并规则

按 JSON Pointer（RFC 6901）逐路径递归：

| 情形 | 结果 |
| --- | --- |
| 仅一侧修改 | 自动采用该侧 |
| 双侧修改为相同值 | 自动合并为该值 |
| 双侧修改不同值（对象） | 按键递归合并 |
| 双侧修改不同值（标量/数组/类型变化） | **冲突**：占位符 + 双方候选 |
| 一侧删除、另一侧未改 | 自动删除 |
| 一侧删除、另一侧修改 | **冲突**（`missing` 字段标记删除侧） |

冲突处在 `document` 中保留占位符：

```json
{"__merge_conflict__": "/db/host"}
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/merges` | 提交 base/left/right，创建合并单（支持幂等键） |
| `GET` | `/merges/{merge_id}` | 状态查询：状态、revision、剩余冲突、最终结果 |
| `POST` | `/merges/{merge_id}/resolutions` | 携带 revision 分批提交冲突取值 |
| `POST` | `/merges/{merge_id}/resolution-preview` | 预演冲突解决：内存副本上校验并应用，不落库、不改状态 |
| `POST` | `/merges/{merge_id}/rule-resolutions` | 携带 revision 与有序路径规则，按模式批量裁决冲突 |
| `GET` | `/merges/{merge_id}/patch` | resolved 后：base → result 的 RFC 6902 补丁（含 SHA-256） |
| `GET` | `/health` | 健康检查 |

### 1. 创建合并单 — `POST /merges`

```bash
curl -s -X POST http://localhost:8000/merges \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-123' \
  -d '{
    "base":  {"replicas": 1, "db": {"host": "a", "port": 5432}},
    "left":  {"replicas": 3, "db": {"host": "b", "port": 5432}},
    "right": {"replicas": 1, "db": {"host": "c", "port": 5433}}
  }'
```

响应（`201`，`replicas`、`port` 已自动合并，`/db/host` 冲突）：

```json
{
  "merge_id": "bc6d2c3d9a3343bf9177147a10ec4023",
  "status": "pending",
  "revision": 1,
  "document": {
    "replicas": 3,
    "db": {"host": {"__merge_conflict__": "/db/host"}, "port": 5433}
  },
  "conflicts": [
    {"pointer": "/db/host", "base": "a", "left": "b", "right": "c", "missing": []}
  ],
  "conflict_count": 1,
  "result": null,
  "created_at": "...", "updated_at": "..."
}
```

**幂等**：在请求体 `idempotency_key` 字段或 `Idempotency-Key` 请求头中携带幂等键。相同键重复提交返回既有合并单（`200`，同一个 `merge_id`），**不会创建新合并单**。

无冲突的合并单创建即 `resolved`，`result` 立即可用且不可变。

### 2. 状态查询 — `GET /merges/{merge_id}`

```bash
curl -s http://localhost:8000/merges/bc6d2c3d9a3343bf9177147a10ec4023
```

返回当前 `status`（`pending` / `resolved`）、`revision`、剩余 `conflicts`、带占位符的 `document` 及最终 `result`。

### 3. 分批解决冲突 — `POST /merges/{merge_id}/resolutions`

每条取值二选一：`choice`（采用某侧候选，`"left"` / `"right"`；若该侧为删除侧则删除该键）或 `value`（显式取值，可为 `null`）。可一次提交多条（批量），也可分多批提交。

```bash
curl -s -X POST http://localhost:8000/merges/bc6d2c3d9a3343bf9177147a10ec4023/resolutions \
  -H 'Content-Type: application/json' \
  -d '{
    "revision": 1,
    "resolutions": [
      {"pointer": "/db/host", "choice": "left"}
    ]
  }'
```

- **revision 过期 → `409`**：请求中的 `revision` 必须与当前一致（创建时为 1，每接受一批 +1）。过期时响应体携带 `current_revision`，客户端重新拉取状态后重试。
- 全部冲突解决后 `status` 变为 `resolved`，`result` 生成且**不可变**；此后再提交取值一律返回 `409`。
- 对不存在的冲突指针取值返回 `400`；合并单不存在返回 `404`。

### 4. 预演冲突解决 — `POST /merges/{merge_id}/resolution-preview`

请求格式与正式提交（`POST /merges/{merge_id}/resolutions`）完全相同（`revision` + `resolutions`），但只在**内存副本**上按顺序校验并应用：**不写入 SQLite、不递增 revision、不改变状态**，可安全反复调用，用于在正式提交前试算取值方案。

```bash
curl -s -X POST http://localhost:8000/merges/bc6d2c3d9a3343bf9177147a10ec4023/resolution-preview \
  -H 'Content-Type: application/json' \
  -d '{
    "revision": 1,
    "resolutions": [
      {"pointer": "/db/host", "choice": "left"}
    ]
  }'
```

响应（预演结果，合并单本身不受影响）：

```json
{
  "merge_id": "bc6d2c3d9a3343bf9177147a10ec4023",
  "revision": 1,
  "document": {
    "replicas": 3,
    "db": {"host": "b", "port": 5433}
  },
  "remaining_conflicts": [],
  "would_resolve": true,
  "patch": [
    {"op": "replace", "path": "/db/host", "value": "b"},
    {"op": "replace", "path": "/db/port", "value": 5433},
    {"op": "replace", "path": "/replicas", "value": 3}
  ],
  "result_sha256": "5f4a…（64 位十六进制）"
}
```

- `would_resolve` 为 `true`（该批次可解决全部剩余冲突）时，额外返回 `base` → 预演结果的确定性 `patch`（规则同 `GET /merges/{merge_id}/patch`）与 `result_sha256`；否则不携带这两个字段。
- 任一指针对应的冲突不存在或已在本批中被解决（即指针重复）、或同一条目同时给出 `choice` 与 `value`（显式 `null` 也算给出 `value`）→ 整批 **`400`**，同样无任何副作用。
- `revision` 过期、合并单已 `resolved` → 与正式提交相同的 **`409`**；合并单不存在 → **`404`**。

### 5. 规则批量裁决 — `POST /merges/{merge_id}/rule-resolutions`

不想逐条枚举冲突指针时，可携带当前 `revision` 与一组**有序规则**，由服务把每条开放冲突分配给唯一规则后一次性裁决。每条规则给出 `left` 和/或 `right` 一个 [RFC 6901](https://www.rfc-editor.org/rfc/rfc6901) 风格的路径模式，命中即采用对应一侧的候选：

- 普通路径与 JSON Pointer 写法一致（`~` 转义为 `~0`、`/` 转义为 `~1`，`""` 表示文档根）；
- 单段 `*`：恰好匹配**一个**路径段，如 `/services/*/image`；
- 末尾 `**`：匹配剩余的**零个或多个**路径段，如 `/services/**`；
- 通配符必须独占整段，且 `**` 只能出现在最后一段（`/a/**/b`、`/a/x*` 均为非法模式 → `400`）。

**唯一规则的选择次序**：对每个开放冲突，在所有命中它的模式中按以下优先级取最高者——

1. **字面量段更多**（含字面量的段越多越优先）；
2. **`*` 更少**（模式中 `*` 字符数更少：`*` 算 1 个、`**` 算 2 个）；
3. **声明顺序更前**（请求 `rules` 数组中的下标更小）；
4. 同一条规则内 `left` 优先于 `right`。

```bash
curl -s -X POST http://localhost:8000/merges/bc6d2c3d9a3343bf9177147a10ec4023/rule-resolutions \
  -H 'Content-Type: application/json' \
  -d '{
    "revision": 1,
    "rules": [
      {"left":  "/services/*/image", "right": "/services/*/replicas"},
      {"right": "/db/**"},
      {"left":  "/**"}
    ]
  }'
```

响应在普通状态视图（`status` / `revision` / `document` / `conflicts` / `result`）之外，额外返回各规则命中的指针与未匹配冲突：

```json
{
  "merge_id": "bc6d2c3d9a3343bf9177147a10ec4023",
  "status": "pending",
  "revision": 2,
  "document": { "...": "..." },
  "conflicts": [
    {"pointer": "/feature/x", "base": 0, "left": 1, "right": 2, "missing": []}
  ],
  "conflict_count": 1,
  "result": null,
  "rule_matches": [
    {"rule_index": 0, "side": "left",  "pointers": ["/services/api/image"]},
    {"rule_index": 0, "side": "right", "pointers": ["/services/api/replicas"]},
    {"rule_index": 1, "side": "right", "pointers": ["/db/host", "/db/port"]}
  ],
  "unmatched_conflicts": ["/feature/x"],
  "applied": true,
  "created_at": "...", "updated_at": "..."
}
```

- **先整批校验，再一次事务提交**：先编译全部模式、为每个冲突选定唯一规则，并验证所有命中项都可应用；只有全部通过时才在**单条 UPDATE 事务**中写入，`revision` 恰好 **+1**。任何非法模式、缺少 `left`/`right` 的规则、或任一命中项应用失败，整批 **`400`**，**不会产生任何部分修改**（revision 与冲突列表保持原样）。
- **缺失侧沿用删除语义**：规则选中的一侧若处于删除态（冲突的 `missing` 中含该侧），则删除对应键；若缺失的是文档根本身（无法删除），整批 `400`。
- **未匹配冲突**原样保留并通过 `unmatched_conflicts` 返回；它们不阻塞其余命中项的提交，但会使合并单继续处于 `pending`。
- **一条都没命中**时不写入、不递增 revision（响应中 `applied: false`）；这不算错误，返回 `200`。
- **revision 过期 → `409`**（响应体携带 `current_revision`）；合并单已 `resolved` 后同样 `409`；合并单不存在 → `404`。

### 6. 获取确定性补丁 — `GET /merges/{merge_id}/patch`

合并单 `resolved` 后，可获取从**原始 `base_doc` 到最终 `result`** 的确定性操作序列（[RFC 6902 JSON Patch](https://www.rfc-editor.org/rfc/rfc6902)，路径为 [RFC 6901 JSON Pointer](https://www.rfc-editor.org/rfc/rfc6901)）：

- **对象**按键递归生成 `add` / `remove` / `replace`；新增/删除的整个子树收敛为单条操作。
- **数组整体 `replace`**（不逐下标生成操作）；标量与类型变化同样是单条 `replace`。
- 路径中的 `~` 转义为 `~0`、`/` 转义为 `~1`；根文档路径为 `""`。
- 操作按 **JSON Pointer 字典序稳定输出**，依次应用到 `base` 可精确重建 `result`。
- 响应同时给出 `base` 与 `result` 的 **SHA-256**（基于规范化 JSON：键排序、无空白）。

```bash
curl -s http://localhost:8000/merges/bc6d2c3d9a3343bf9177147a10ec4023/patch
```

```json
{
  "merge_id": "bc6d2c3d9a3343bf9177147a10ec4023",
  "status": "resolved",
  "patch": [
    {"op": "replace", "path": "/db/host", "value": "b"},
    {"op": "replace", "path": "/db/port", "value": 5433},
    {"op": "replace", "path": "/image", "value": "app:v2"},
    {"op": "replace", "path": "/replicas", "value": 3}
  ],
  "base_sha256": "9f2c…（64 位十六进制）",
  "result_sha256": "4a7e…（64 位十六进制）"
}
```

含新增 / 删除 / 数组及特殊键名的例子：

```json
"patch": [
  {"op": "add", "path": "/brand", "value": {"new": {"sub": true}}},
  {"op": "remove", "path": "/gone"},
  {"op": "replace", "path": "/list", "value": [1, 2, 3, 4]},
  {"op": "replace", "path": "/weird~1a~0k", "value": [1, 2]}
]
```

- 合并单尚为 `pending`（`result` 未生成）→ **`409`**。
- 合并单不存在 → **`404`**。
- 无冲突、创建即 `resolved` 的合并单可立即获取补丁（无变化时 `patch` 为 `[]`）。

## 本地开发与测试

```bash
pip install -r requirements.txt
pip install httpx pytest
uvicorn app.main:app --reload --port 8000   # 本地运行
python -m pytest tests/ -q                  # 运行测试（72 项）
```

## 项目结构

```
app/
  main.py    # FastAPI 路由与错误映射（404/409/400）
  merge.py   # 纯 Python 三方合并引擎（JSON Pointer 递归）
  rules.py   # RFC 6901 风格路径模式（* 与 **）：规则编译、匹配、批量裁决
  patch.py   # 确定性 RFC 6902 补丁生成 + 规范化 SHA-256
  store.py   # 合并单状态机：revision 校验、冲突解决、不可变结果
  db.py      # SQLite 连接与建表（启动时执行）
  models.py  # Pydantic 请求模型
tests/       # 合并引擎/补丁单测 + API 端到端测试
Dockerfile
docker-compose.yml
requirements.txt
```
