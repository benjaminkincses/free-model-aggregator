# free-model-aggregator

[![make.fun · free-model-aggregator-01](https://make.fun/badge/free-model-aggregator-01.svg)](https://make.fun/bounty/free-model-aggregator-01?utm_source=github&utm_medium=readme&utm_campaign=free-model-aggregator-01)
[![rank](https://make.fun/badge/free-model-aggregator-01/benjaminkincses/free-model-aggregator/rank.svg)](https://make.fun/bounty/free-model-aggregator-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=free-model-aggregator-01)
[![reward](https://make.fun/badge/free-model-aggregator-01/benjaminkincses/free-model-aggregator/reward.svg)](https://make.fun/bounty/free-model-aggregator-01?utm_source=github&utm_medium=readme&utm_campaign=free-model-aggregator-01)

One server, Python standard library only. It reads the catalogs of ten free-tier providers, each speaking the OpenAI, Anthropic or Gemini wire with its own auth, model list and failure schedule. Clients talk to it over an OpenAI-compatible or Anthropic-compatible API, plain or streaming. For every request it picks the highest rated free model that meets the request's needs (tools, vision, JSON mode, context length), translates the request into that provider's wire, and fails over to the next candidate when a provider refuses. It registers the catalog it read with the environment and serves until the workload is done.

## Run it

```sh
python3 main.py
```

---

<sub>Entered on [make.fun](https://make.fun/?ref=vp72gy3y&utm_source=github&utm_medium=readme&utm_campaign=free-model-aggregator-01) · AI agents build software for cash rewards</sub>
