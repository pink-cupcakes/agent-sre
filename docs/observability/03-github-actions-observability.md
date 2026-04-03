# 03 — GitHub Actions observability

## Goal

Use GitHub Actions as a free, zero-infrastructure observability layer for CI/CD
visibility. No third-party tools required beyond a GitHub account.

## What GitHub gives you for free

- Workflow run history with logs (90-day retention on free tier)
- Pass/fail per job and step
- Artifact storage (500 MB free)
- Job summaries (rich Markdown rendered in the Actions UI)
- PR check annotations (inline comments on diffs)
- Scheduled workflows (cron)
- GitHub Pages (free static hosting — useful for dashboards)

---

## Feature 1 — Job summaries after every run

GitHub Actions supports a special file `$GITHUB_STEP_SUMMARY` that renders as
a Markdown report on the workflow run page. This is the highest-value, lowest-
effort observability feature.

### What to render

After the test suite runs, write a summary like:

```markdown
## Test results
| Suite | Passed | Failed | Duration |
|-------|--------|--------|----------|
| unit  | 42     | 0      | 3.2s     |
| integration | 8 | 1   | 12.1s    |

## Coverage
`87%` overall — [full report](...)

## Linting
✅ No issues
```

### How to implement

Add a step at the end of your CI workflow:

```yaml
- name: Write job summary
  if: always()
  run: |
    python scripts/ci/summary.py >> $GITHUB_STEP_SUMMARY
```

`scripts/ci/summary.py` reads pytest's JSON report (`--json-report`) and
formats it as Markdown. Libraries: `pytest-json-report` for output,
plain `print()` for Markdown generation — no deps needed.

---

## Feature 2 — PR annotations for test failures

GitHub's Checks API lets you annotate specific lines in a diff with errors.
This surfaces test failures inline in the PR review UI.

### How to implement

Use the `reviewdog` or `pytest-github-actions-annotate-failures` plugin:

```yaml
- name: Run tests
  run: pytest --tb=short -q

- name: Annotate failures
  if: failure()
  uses: mikepenz/action-junit-report@v4
  with:
    report_paths: '**/test-results/*.xml'
```

Alternatively, `pytest-github-actions-annotate-failures` does this automatically
with zero configuration — install it and failures appear as inline annotations.

---

## Feature 3 — Performance benchmark tracking

Track agent performance metrics (token usage, latency, step count) across
commits and surface regressions in PRs.

### How it works

1. A workflow runs a fixed set of smoke test tasks against the agent.
2. Results are written to a JSON file (`benchmark-results.json`).
3. The results are compared to the previous run (stored as a GitHub Actions cache
   or artifact).
4. A PR comment is posted if any metric regresses beyond a threshold.

### Workflow sketch

```yaml
name: Benchmark
on:
  push:
    branches: [main]
  pull_request:

jobs:
  benchmark:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -e .
      - name: Run benchmark tasks
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: python scripts/ci/benchmark.py --output benchmark-results.json
      - name: Store results
        uses: benchmark-action/github-action-benchmark@v1
        with:
          tool: customSmallerIsBetter
          output-file-path: benchmark-results.json
          github-token: ${{ secrets.GITHUB_TOKEN }}
          auto-push: true
          alert-threshold: '150%'
          comment-on-alert: true
          fail-on-alert: false
```

`benchmark-action/github-action-benchmark` automatically:
- Stores historical data in the `gh-pages` branch.
- Renders a chart at `https://<org>.github.io/<repo>/dev/bench/`.
- Posts a PR comment with trend data if a metric regresses.

### `scripts/ci/benchmark.py` — what to measure

Run 3–5 fixed prompt tasks (stored in `tests/fixtures/benchmark_tasks.json`)
and emit results in the format the benchmark action expects:

```json
[
  { "name": "task.duration_ms.p50", "unit": "ms", "value": 1240 },
  { "name": "task.tokens_total.mean", "unit": "tokens", "value": 850 },
  { "name": "task.cost_usd.mean", "unit": "USD", "value": 0.0032 },
  { "name": "task.step_count.mean", "unit": "steps", "value": 3.2 }
]
```

---

## Feature 4 — Scheduled health check workflow

A cron workflow that hits the running service (if deployed) or runs a local
smoke test on a schedule, posting failures as GitHub issues.

```yaml
name: Health check
on:
  schedule:
    - cron: '0 */6 * * *'   # every 6 hours

jobs:
  health:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Check /health endpoint
        run: |
          STATUS=$(curl -s -o /dev/null -w "%{http_code}" ${{ vars.AGENT_URL }}/health)
          if [ "$STATUS" != "200" ]; then
            echo "Health check failed: $STATUS"
            exit 1
          fi
      - name: Open issue on failure
        if: failure()
        uses: actions/github-script@v7
        with:
          script: |
            github.rest.issues.create({
              owner: context.repo.owner,
              repo: context.repo.repo,
              title: `Health check failed — ${new Date().toISOString()}`,
              body: 'The /health endpoint returned non-200. Check the workflow run for details.',
              labels: ['incident']
            })
```

---

## Feature 5 — GitHub Pages metrics dashboard

Store aggregated metrics as JSON in the `gh-pages` branch and render them
as a static HTML dashboard deployed automatically on every push to main.

### What it looks like

A single `index.html` with Chart.js charts showing:
- Task success rate over the last 30 runs
- P50 / P95 latency trend
- Token cost per run
- Test pass rate trend

### How to implement

1. After each CI run, append a metrics row to `data/metrics.jsonl` in the repo
   (committed to `gh-pages` branch via `actions/github-script`).
2. The `index.html` fetches `data/metrics.jsonl` and renders Chart.js charts.
3. GitHub Pages serves it automatically.

No backend, no database, no cloud account — just static files in a git branch.

---

## Summary: recommended implementation order

| Priority | Feature                     | Effort  | Value  |
|----------|-----------------------------|---------|--------|
| 1        | Job summaries               | 1–2 hrs | High   |
| 2        | PR annotations              | 30 min  | High   |
| 3        | Benchmark tracking          | 2–3 hrs | High   |
| 4        | GitHub Pages dashboard      | 3–4 hrs | Medium |
| 5        | Scheduled health checks     | 1 hr    | Medium |

Start with job summaries and PR annotations — they're free visibility with
minimal code and make every CI run immediately more informative.
