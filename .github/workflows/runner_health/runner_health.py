#!/usr/bin/env python3
"""
Runner Health (simple + suspicious runners)


Runs: python .github/runner_health/runner_health.py
Flow: fetch → compute → write outputs → render HTML → step summary → optional issue
Produces: runners.json, health.json, runners.csv, and site/index.html

Collects:
- Runner inventory (online/offline, busy/idle, labels)
- Recent workflow jobs to compute queue time + duration percentiles
- Identifies "top suspicious runners" with suggested actions

Outputs:
- .github/runner_health/out/runners.json
- .github/runner_health/out/health.json
- .github/runner_health/out/runners.csv
- .github/runner_health/site/index.html (shareable via GitHub Pages)
- GitHub Step Summary (nice tables)

Optional:
- Opens an issue if fleet is degraded (ISSUE_ON_DEGRADED=true)
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dateutil import parser as dateparser


OUT_DIR = Path(".github/runner_health/out")
SITE_DIR = Path(".github/runner_health/site")


def must_env(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if v is None or v == "":
        raise RuntimeError(f"Missing env var: {name}")
    return v


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def gh_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "runner-health-bot",
    }


def gh_get(url: str, token: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    r = requests.get(url, headers=gh_headers(token), params=params, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"GET {url} failed: {r.status_code} {r.text}")
    return r.json()


def gh_post(url: str, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.post(url, headers=gh_headers(token), json=payload, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"POST {url} failed: {r.status_code} {r.text}")
    return r.json()


def percentile(values: List[float], p: float) -> Optional[float]:
    """Linear interpolation percentile. p in [0, 1]."""
    if not values:
        return None
    xs = sorted(values)
    k = (len(xs) - 1) * p
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    if f == c:
        return float(xs[f])
    return float(xs[f] * (c - k) + xs[c] * (k - f))


def summarize(values: List[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"p50": None, "p95": None, "p99": None, "avg": None}
    return {
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "avg": sum(values) / len(values),
    }


def fmt_seconds(s: Optional[float]) -> str:
    if s is None:
        return "—"
    if s >= 120:
        return f"{s/60:.1f}m"
    return f"{s:.0f}s"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def list_runners(owner: str, repo: str, token: str) -> List[Dict[str, Any]]:
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runners"
    runners: List[Dict[str, Any]] = []
    page = 1

    while True:
        data = gh_get(url, token, params={"per_page": 100, "page": page})
        batch = data.get("runners", [])
        for r in batch:
            runners.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "os": r.get("os", "unknown"),
                    "status": r.get("status", "unknown"),
                    "busy": bool(r.get("busy", False)),
                    "labels": [l["name"] for l in r.get("labels", [])],
                }
            )
        if len(batch) < 100:
            break
        page += 1

    return runners


def list_recent_runs(owner: str, repo: str, token: str, since: datetime) -> List[Dict[str, Any]]:
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs"
    runs: List[Dict[str, Any]] = []
    page = 1

    while True:
        data = gh_get(url, token, params={"per_page": 100, "page": page})
        batch = data.get("workflow_runs", [])

        for run in batch:
            created = dateparser.isoparse(run["created_at"])
            if created >= since:
                runs.append(run)

        if len(batch) < 100:
            break
        page += 1

    return runs


def list_jobs_for_run(owner: str, repo: str, token: str, run_id: int) -> List[Dict[str, Any]]:
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
    jobs: List[Dict[str, Any]] = []
    page = 1

    while True:
        data = gh_get(url, token, params={"per_page": 100, "page": page})
        batch = data.get("jobs", [])
        jobs.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    return jobs


def median(values: List[float]) -> Optional[float]:
    return percentile(values, 0.50)


def compute_suspicious_runners(per_runner: Dict[str, Dict[str, Any]], jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Suspicion heuristic (simple & explainable):
      - Consider only runners with job_count >= MIN_JOBS (default 8)
      - Score uses:
          * fail_count (high weight)
          * median duration being > 1.75x or > 2.50x fleet median
      - Suggested action:
          * quarantine if (fail_count>=3 AND fail_rate>=30%) OR slow_ratio>=2.50x
    """
    MIN_JOBS = int(os.getenv("SUSPICIOUS_MIN_JOBS", "8"))
    TOP_N = int(os.getenv("SUSPICIOUS_TOP_N", "10"))

    all_durations: List[float] = []
    for j in jobs:
        if j.get("started_at") and j.get("completed_at"):
            st = dateparser.isoparse(j["started_at"])
            ct = dateparser.isoparse(j["completed_at"])
            all_durations.append((ct - st).total_seconds())
    fleet_p50 = median(all_durations)

    scored: List[Dict[str, Any]] = []

    for runner_name, r in per_runner.items():
        job_count = int(r.get("count", 0))
        if job_count < MIN_JOBS:
            continue

        fail_count = int(r.get("fail", 0))
        fail_rate = (fail_count / job_count) if job_count else 0.0

        dur_p50 = median(r.get("duration_secs") or [])
        slow_ratio = None
        if dur_p50 is not None and fleet_p50 is not None and fleet_p50 > 0:
            slow_ratio = dur_p50 / fleet_p50

        score = 0.0
        reasons: List[str] = []

        if fail_count > 0:
            score += fail_count * 10.0
            reasons.append(f"failures={fail_count}/{job_count} ({fail_rate:.0%})")

        if slow_ratio is not None:
            if slow_ratio > 2.50:
                score += 10.0
                reasons.append(f"slow_p50={slow_ratio:.2f}x fleet")
            elif slow_ratio > 1.75:
                score += 5.0
                reasons.append(f"slow_p50={slow_ratio:.2f}x fleet")

        score += min(job_count, 50) / 50.0

        suggested_action = "none"
        if fail_count >= 3 and fail_rate >= 0.30:
            suggested_action = "quarantine"
        elif slow_ratio is not None and slow_ratio >= 2.50:
            suggested_action = "quarantine"

        scored.append(
            {
                "runner_name": runner_name,
                "score": round(score, 3),
                "job_count": job_count,
                "fail_count": fail_count,
                "fail_rate": round(fail_rate, 3),
                "duration_p50_sec": round(dur_p50, 3) if dur_p50 is not None else None,
                "slow_ratio_vs_fleet_p50": round(slow_ratio, 3) if slow_ratio is not None else None,
                "reasons": reasons,
                "suggested_action": suggested_action,
            }
        )

    scored.sort(key=lambda x: x["score"], reverse=True)

    return {
        "fleet_duration_p50_sec": round(fleet_p50, 3) if fleet_p50 is not None else None,
        "min_jobs": MIN_JOBS,
        "top": scored[:TOP_N],
    }


def compute_health(runners: List[Dict[str, Any]], jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    online = [r for r in runners if r["status"] == "online"]
    offline = [r for r in runners if r["status"] != "online"]
    busy = [r for r in online if r["busy"]]
    idle = [r for r in online if not r["busy"]]

    queue_secs: List[float] = []
    duration_secs: List[float] = []

    per_runner: Dict[str, Dict[str, Any]] = {}

    for j in jobs:
        created_at = dateparser.isoparse(j["created_at"])
        started_at = dateparser.isoparse(j["started_at"]) if j.get("started_at") else None
        completed_at = dateparser.isoparse(j["completed_at"]) if j.get("completed_at") else None

        if started_at:
            queue_secs.append((started_at - created_at).total_seconds())
        if started_at and completed_at:
            duration_secs.append((completed_at - started_at).total_seconds())

        rname = j.get("runner_name") or "unknown"
        rec = per_runner.setdefault(rname, {"count": 0, "fail": 0, "queue_secs": [], "duration_secs": []})
        rec["count"] += 1
        if j.get("conclusion") not in (None, "success"):
            rec["fail"] += 1
        if started_at:
            rec["queue_secs"].append((started_at - created_at).total_seconds())
        if started_at and completed_at:
            rec["duration_secs"].append((completed_at - started_at).total_seconds())

    off_ratio = (len(offline) / max(len(runners), 1)) if runners else 0.0
    q_p95 = percentile(queue_secs, 0.95) or 0.0

    degraded_reasons: List[str] = []
    if len(runners) >= 5 and off_ratio >= 0.20:
        degraded_reasons.append(f"offline_ratio={off_ratio:.2f} >= 0.20")
    if q_p95 >= 600:
        degraded_reasons.append(f"queue_p95={q_p95:.0f}s >= 600s")

    suspicious = compute_suspicious_runners(per_runner, jobs)

    return {
        "timestamp": iso_utc(datetime.now(timezone.utc)),
        "fleet": {
            "total": len(runners),
            "online": len(online),
            "offline": len(offline),
            "busy": len(busy),
            "idle": len(idle),
            "offline_ratio": off_ratio,
        },
        "jobs_analyzed": len(jobs),
        "queue_time_sec": summarize(queue_secs),
        "job_duration_sec": summarize(duration_secs),
        "degraded": len(degraded_reasons) > 0,
        "degraded_reasons": degraded_reasons,
        "per_runner": per_runner,
        "suspicious_runners": suspicious,
    }


def make_step_summary(health: Dict[str, Any], runners: List[Dict[str, Any]]) -> str:
    fleet = health["fleet"]
    q = health["queue_time_sec"]
    d = health["job_duration_sec"]
    status = "🟢 Healthy" if not health["degraded"] else "🔴 Degraded"

    lines: List[str] = []
    lines.append(f"## Runner Health: {status}")

    if health.get("degraded_reasons"):
        lines.append("")
        lines.append("**Reasons:**")
        for r in health["degraded_reasons"]:
            lines.append(f"- {r}")

    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---:|")
    lines.append(f"| Total runners | {fleet['total']} |")
    lines.append(f"| Online / Offline | {fleet['online']} / {fleet['offline']} |")
    lines.append(f"| Busy / Idle (online) | {fleet['busy']} / {fleet['idle']} |")
    lines.append(f"| Jobs analyzed | {health['jobs_analyzed']} |")
    lines.append(f"| Queue time p50 / p95 / p99 | {fmt_seconds(q['p50'])} / {fmt_seconds(q['p95'])} / {fmt_seconds(q['p99'])} |")
    lines.append(f"| Job duration p50 / p95 / p99 | {fmt_seconds(d['p50'])} / {fmt_seconds(d['p95'])} / {fmt_seconds(d['p99'])} |")

    offline = [r for r in runners if r["status"] != "online"]
    lines.append("")
    lines.append("### Offline runners")
    if not offline:
        lines.append("- None ✅")
    else:
        for r in offline[:30]:
            labels = ", ".join(r["labels"])
            lines.append(f"- **{r['name']}** ({r['os']}) labels: `{labels}`")

    lines.append("")
    lines.append("### Top suspicious runners (lookback)")
    top = ((health.get("suspicious_runners") or {}).get("top")) or []
    if not top:
        lines.append("- None ✅")
    else:
        lines.append("| Runner | Score | Jobs | Fails | Slow vs fleet p50 | Suggested action | Reasons |")
        lines.append("|---|---:|---:|---:|---:|---|---|")
        for x in top[:10]:
            slow = x.get("slow_ratio_vs_fleet_p50")
            slow_s = "—" if slow is None else f"{slow:.2f}x"
            reasons = "; ".join(x.get("reasons") or [])
            lines.append(f"| `{x['runner_name']}` | {x['score']} | {x['job_count']} | {x['fail_count']} | {slow_s} | **{x['suggested_action']}** | {reasons} |")

    return "\n".join(lines)


def render_html(health: Dict[str, Any], runners: List[Dict[str, Any]]) -> str:
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    fleet = health["fleet"]
    q = health["queue_time_sec"]
    d = health["job_duration_sec"]
    status = "Healthy" if not health["degraded"] else "Degraded"
    badge = "🟢" if not health["degraded"] else "🔴"

    reasons_html = ""
    if health.get("degraded_reasons"):
        reasons_html = "<ul>" + "".join(f"<li>{esc(x)}</li>" for x in health["degraded_reasons"]) + "</ul>"

    offline_rows = ""
    for r in [x for x in runners if x["status"] != "online"]:
        offline_rows += (
            "<tr>"
            f"<td>{esc(r['name'])}</td>"
            f"<td>{esc(r['os'])}</td>"
            f"<td>{esc(', '.join(r['labels']))}</td>"
            "</tr>\n"
        )
    if not offline_rows:
        offline_rows = "<tr><td colspan='3'>None</td></tr>"

    top = ((health.get("suspicious_runners") or {}).get("top")) or []
    suspicious_rows = ""
    for x in top[:10]:
        slow = x.get("slow_ratio_vs_fleet_p50")
        slow_s = "—" if slow is None else f"{slow:.2f}x"
        reasons = "; ".join(x.get("reasons") or [])
        suspicious_rows += (
            "<tr>"
            f"<td><code>{esc(x['runner_name'])}</code></td>"
            f"<td>{esc(str(x['score']))}</td>"
            f"<td>{esc(str(x['job_count']))}</td>"
            f"<td>{esc(str(x['fail_count']))}</td>"
            f"<td>{esc(slow_s)}</td>"
            f"<td><b>{esc(x['suggested_action'])}</b></td>"
            f"<td>{esc(reasons)}</td>"
            "</tr>\n"
        )
    if not suspicious_rows:
        suspicious_rows = "<tr><td colspan='7'>None</td></tr>"

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Runner Health</title>
  <style>
    body {{ font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial; margin: 24px; }}
    .card {{ border: 1px solid #ddd; border-radius: 12px; padding: 16px; margin-bottom: 16px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid #eee; padding: 8px; text-align: left; vertical-align: top; }}
    .muted {{ color: #666; }}
    .k {{ width: 280px; }}
    code {{ background: #f6f6f6; padding: 2px 6px; border-radius: 6px; }}
  </style>
</head>
<body>
  <h1>{badge} Runner Health: {esc(status)}</h1>
  <p class="muted">Updated: {esc(health['timestamp'])} • Lookback jobs analyzed: {health['jobs_analyzed']}</p>

  <div class="card">
    <h2>Fleet</h2>
    {reasons_html if reasons_html else ""}
    <table>
      <tr><td class="k">Total runners</td><td>{fleet['total']}</td></tr>
      <tr><td class="k">Online</td><td>{fleet['online']}</td></tr>
      <tr><td class="k">Offline</td><td>{fleet['offline']}</td></tr>
      <tr><td class="k">Busy (online)</td><td>{fleet['busy']}</td></tr>
      <tr><td class="k">Idle (online)</td><td>{fleet['idle']}</td></tr>
      <tr><td class="k">Offline ratio</td><td>{fleet['offline_ratio']:.2%}</td></tr>
    </table>
  </div>

  <div class="card">
    <h2>Performance</h2>
    <table>
      <tr><th></th><th>p50</th><th>p95</th><th>p99</th><th>avg</th></tr>
      <tr><td>Queue time</td><td>{fmt_seconds(q['p50'])}</td><td>{fmt_seconds(q['p95'])}</td><td>{fmt_seconds(q['p99'])}</td><td>{fmt_seconds(q['avg'])}</td></tr>
      <tr><td>Job duration</td><td>{fmt_seconds(d['p50'])}</td><td>{fmt_seconds(d['p95'])}</td><td>{fmt_seconds(d['p99'])}</td><td>{fmt_seconds(d['avg'])}</td></tr>
    </table>
  </div>

  <div class="card">
    <h2>Offline runners</h2>
    <table>
      <tr><th>Name</th><th>OS</th><th>Labels</th></tr>
      {offline_rows}
    </table>
  </div>

  <div class="card">
    <h2>Top suspicious runners (lookback)</h2>
    <table>
      <tr>
        <th>Runner</th><th>Score</th><th>Jobs</th><th>Fails</th><th>Slow vs fleet p50</th><th>Suggested action</th><th>Reasons</th>
      </tr>
      {suspicious_rows}
    </table>
    <p class="muted">
      If runners are ARC-managed and ephemeral, treat suspicious runner names as debug hints.
      Actionability should target ARC scaling/restarts rather than per-runner quarantine.
    </p>
  </div>

  <div class="card">
    <h2>Audit exports</h2>
    <p class="muted">Artifacts: runners.json, health.json, runners.csv</p>
  </div>
</body>
</html>
"""


def maybe_open_issue(owner: str, repo: str, token: str, health: Dict[str, Any]) -> None:
    if os.getenv("ISSUE_ON_DEGRADED", "false").lower() != "true":
        return
    if not health.get("degraded", False):
        return

    url = f"https://api.github.com/repos/{owner}/{repo}/issues"
    title = f"Runner fleet degraded ({health['timestamp']})"
    body = (
        "Automated runner health detected degradation.\n\n"
        f"**Reasons**:\n"
        + "".join(f"- {x}\n" for x in (health.get("degraded_reasons") or []))
        + "\n"
        f"**Fleet**:\n```json\n{json.dumps(health['fleet'], indent=2)}\n```\n"
        f"**Queue time**:\n```json\n{json.dumps(health['queue_time_sec'], indent=2)}\n```\n"
        f"**Job duration**:\n```json\n{json.dumps(health['job_duration_sec'], indent=2)}\n```\n"
        f"**Suspicious runners**:\n```json\n{json.dumps((health.get('suspicious_runners') or {}).get('top', []), indent=2)}\n```\n"
        "\nSee the Pages report and the `runner-health-audit` artifact for full details.\n"
    )
    gh_post(url, token, {"title": title, "body": body, "labels": ["ops", "ci"]})


def main() -> int:
    token = must_env("GH_TOKEN")
    owner = must_env("GH_OWNER")
    repo = must_env("GH_REPO")
    lookback_hours = int(os.getenv("LOOKBACK_HOURS", "24"))

    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    runners = list_runners(owner, repo, token)

    runs = list_recent_runs(owner, repo, token, since)
    runs = sorted(runs, key=lambda r: r["created_at"], reverse=True)[:80]

    jobs: List[Dict[str, Any]] = []
    for run in runs:
        run_id = int(run["id"])
        try:
            jobs.extend(list_jobs_for_run(owner, repo, token, run_id))
        except Exception as e:
            print(f"WARN: failed listing jobs for run {run_id}: {e}", file=sys.stderr)

    health = compute_health(runners, jobs)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SITE_DIR.mkdir(parents=True, exist_ok=True)

    write_json(OUT_DIR / "runners.json", runners)
    write_json(OUT_DIR / "health.json", health)

    csv = "id,name,os,status,busy,labels\n"
    for r in runners:
        labels = ",".join(r["labels"])
        csv += f'{r["id"]},"{r["name"]}",{r["os"]},{r["status"]},{str(r["busy"]).lower()},"{labels}"\n'
    write_text(OUT_DIR / "runners.csv", csv)

    write_text(SITE_DIR / "index.html", render_html(health, runners))

    step_summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if step_summary_path:
        write_text(Path(step_summary_path), make_step_summary(health, runners))

    maybe_open_issue(owner, repo, token, health)

    print("Runner health generated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
