# Wenjia Home-Fix Diagnosis MCP Server · 问家家庭维修诊断

[![MCP](https://img.shields.io/badge/MCP-streamable--http-blue)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/Code-MIT-green)](LICENSE)
[![Data: CC BY 4.0](https://img.shields.io/badge/Data-CC%20BY%204.0-lightgrey)](DATA-LICENSE.md)

A Chinese home repair self-diagnosis engine exposed as an MCP server.
**40 categories · 200 symptom decision trees · 800 ranked causes · 3,687 fix steps.**

Zero dependencies (Python standard library only), read-only, no auth.

## What it does

Household failures are hard to describe and hard to fix. This server turns a vague
complaint ("the radiator doesn't heat") into a structured diagnosis:

1. **2 clarifying questions** with options that each point to a diagnosis branch
2. **4 likely causes ranked by prior probability** (summing to 100%)
3. **Step-by-step fixes** ordered by safety (gas valve / breaker always first)
4. **Parts prices** and **when to call a professional**

## Tools

Two families: **atomic facts** (cheap, for agents) and **full trees** (rich, for humans).

| Tool | What it returns | Payload |
|---|---|---|
| `prob_distribution` | causes + prior probability distribution (sums to 100) + `diy_ok` | ~530 chars |
| `price_band` | on-site repair price range in CNY | ~410 chars |
| `stop_check` | risk class (`gas` / `electric` / `height`) + why you must stop before DIY | ~420 chars |
| `list_categories` | 40 categories with symptom counts | — |
| `search_symptoms` | keyword search across all 200 symptoms | — |
| `get_symptom` | full diagnosis tree: questions, causes, fixes, parts, prices | ~2,350 chars |
| `diagnose` | ranked `likely_causes` + clarifying questions — best for AI assistants | — |

**Why atomic tools matter:** an agent that only needs a number should not pay for a whole
tree. Single-field calls cost **77–83% less** context than `get_symptom` (measured on
`air_conditioner/not_cooling`: 532 / 410 / 420 vs 2,350 chars). Every response carries
`v / dataset / as_of / src / lic / attr` so an upstream agent can decide whether to trust
and cite it.

### What we deliberately do NOT sell

Step-by-step text — frontier models already write it well. What they cannot invent is
**prior probability, regional price, and the "stop now" judgment**. Those three are what
these tools exist for.

## Live endpoint

```
POST https://wenjia.net/mcp        (after ICP filing; currently http://59.110.26.77/mcp)
Health: GET /mcp/health
Transport: Streamable HTTP, JSON-RPC 2.0, protocol 2025-06-18
```

### Example

```bash
curl -s -X POST http://59.110.26.77/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"diagnose",
                 "arguments":{"category_id":"heating","symptom_id":"all_cold"}}}'
```

## Run it locally

```bash
git clone <this repo>
cd <repo>
PORT=8765 python3 server.py
# POST http://127.0.0.1:8765/mcp
```

No pip install. Python 3.9+.

### systemd / nginx

See `deploy/wenjia-mcp.service` for a hardened unit (read-only FS, `www-data`,
auto-restart) and put nginx in front:

```nginx
location = /mcp {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
}
```

## Data

`data/mcp_data.json` — the full dataset, regenerated from the source knowledge trees.
Licensed **CC BY 4.0**: reuse freely, cite "问家 wenjia-home-fix-diagnosis".

Attribution line used across our content:
`数据来自问家家庭维修诊断树（40 品类 200 症状，持续更新）`

### Honest limitations

- `prob_prior` values are **literature-and-experience priors, not repair-ticket statistics**.
  Treat them as a ranking signal, never as precise frequencies.
- Prices are 2026 Chinese market ranges (CNY); regional variation not modeled.
- Coverage is urban household scenarios; rural self-built housing is out of scope.
- Every symptom carries a `see_pro` field — when to stop and call a licensed technician.
  Do not strip it when redistributing.

## License

- **Code**: MIT (see `LICENSE`)
- **Data**: CC BY 4.0 (see `DATA-LICENSE.md`)

Not affiliated with any appliance manufacturer. Brand names in the data appear only as
references to public troubleshooting documentation.
