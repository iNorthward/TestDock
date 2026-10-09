# Adapter recipes

Copy the selected recipe into your own Pack helpers and import it by the full Pack package path. These are explicit extension recipes, not Core defaults or real business authentication.

- `auth_cache.CachedLoginTokens`: supply your Pack's login callback returning `access_token` and `expires_in`. Choose a confirmed maximum TTL and refresh margin. It serializes cache misses, isolates username/tenant entries and exposes explicit invalidation. Login errors propagate; old expired tokens are never used as a fallback. It does not retry business operations after 401.
- `contracts.ConfiguredEnvelope`: declare success, code, message and payload JSON paths from project evidence. Equality preserves the raw JSON type; boolean and integer success values do not mix. Code normalization does not convert strings to numbers.
- `contracts.PageContract`: declare records/total paths; check array, nonnegative raw integer, and returned count. This checks shape only; the Pack owns ID, filtering, sorting, required fields and reconciliation.
- Lossless numeric decoding: the registered `api_client.ResponseDecoder` can use `json_wire_types.lossless_json_loads`. Validate raw wire type before Decimal reconciliation; request encoding is a separate project decision.

Keep passwords and tokens outside source and test fixtures. Bootstrap registers adapters only. Scope, auth flow, endpoint paths and cleanup are owned by your Pack. Run synthetic tests first, then a small authorized real read-only case set.

响应拒绝码也归业务 Adapter。HTTP 200 可以包含合法的业务权限拒绝；不要仅按 HTTP 401/403 判定所有项目的拒绝。从源错误枚举、权限拦截器和已授权接口契约确认具体码，在 Pack 内逐项严格断言 success/code 的原始 JSON 类型和值。不要接受“任何非成功码”或在 Core 固定业务拒绝码。
