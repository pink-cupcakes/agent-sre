"""
Pod rightsizing recommendation logic.

For each pod with 7 days of metrics data:
- CPU: feed all usage samples into a decaying histogram, extract P10/P90.
- Memory: feed the daily-max usage into a decaying histogram, extract P10/P90.
- Margin: max((P90-P10)/P90, 0.30).
- If HPA exists for the pod's owner: recommend target_utilization = 1 - margin,
  then CPU request = current_request * (current_target / recommended_target).
- If no HPA: CPU request = current_request * (1 + margin).
- Memory request = P90_memory * (1 + margin).
One recommendation per pod.
"""

import logging
from datetime import datetime, timedelta, timezone

from service.core.config import K8S_METRICS_POD_TABLE
from service.core.cost_viz.clickhouse import query_k8s_clickhouse
from service.core.db.model import HPAConfig
from service.metrics_ingestion.vpa.histogram import DecayingHistogram, make_buckets

logger = logging.getLogger(__name__)

DAYS_BACK = 7
BASE_SAFETY_MARGIN = 0.30
CPU_BUCKETS = make_buckets(1.0)
MEMORY_BUCKETS = make_buckets(1024 * 1024)
MAX_CPU_MILLICORES = CPU_BUCKETS[-1]
MAX_MEMORY_BYTES = MEMORY_BUCKETS[-1]


async def generate_recommendations(company_id: int) -> list[dict]:
    """Generate pod rightsizing recommendations for a company.

    Returns a list of recommendation dicts, one per pod.
    Raises ValueError if a metric value exceeds the histogram range.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=DAYS_BACK)

    query = f"""
        SELECT
            k8s_pod_owner_reference,
            k8s_pod_namespace,
            k8s_cluster_id,
            date,
            k8s_pod_name,
            k8s_pod_cpu_usage_millicores,
            k8s_pod_memory_usage_bytes,
            k8s_pod_cpu_requests_millicores,
            k8s_pod_memory_requests_bytes
        FROM {K8S_METRICS_POD_TABLE} FINAL
        WHERE company_id = {{company_id:UInt32}}
          AND date >= {{cutoff:DateTime}}
          AND k8s_pod_owner_reference IS NOT NULL
          AND k8s_pod_owner_reference != ''
          AND k8s_pod_cpu_usage_millicores IS NOT NULL
          AND k8s_pod_memory_usage_bytes IS NOT NULL
    """
    result = await query_k8s_clickhouse(
        query,
        parameters={"company_id": company_id, "cutoff": cutoff},
    )

    if not result or not result.get("k8s_pod_owner_reference"):
        return []

    n_rows = len(result["k8s_pod_owner_reference"])
    logger.info(f"[PodRightsizing] Fetched {n_rows} metric rows for company_id={company_id}")

    pods = _group_by_pod(result, n_rows)

    hpa_configs = await HPAConfig.objects.filter(company_id=company_id).all()
    hpa_lookup: dict[tuple[str, str, str], HPAConfig] = {}
    for hpa in hpa_configs:
        hpa_lookup[(hpa.cluster_id, hpa.target_kind, hpa.target_name)] = hpa

    recommendations: list[dict] = []
    for (cluster_id, namespace, pod_name), samples in pods.items():
        rec = _build_pod_recommendation(cluster_id, namespace, pod_name, samples, hpa_lookup)
        if rec is not None:
            recommendations.append(rec)

    logger.info(f"[PodRightsizing] Generated {len(recommendations)} recommendations for company_id={company_id}")
    return recommendations


def _group_by_pod(result: dict, n_rows: int) -> dict[tuple[str, str, str], list[dict]]:
    pods: dict[tuple[str, str, str], list[dict]] = {}
    for i in range(n_rows):
        key = (result["k8s_cluster_id"][i], result["k8s_pod_namespace"][i], result["k8s_pod_name"][i])
        pods.setdefault(key, []).append(
            {
                "date": result["date"][i],
                "owner_ref": result["k8s_pod_owner_reference"][i],
                "cpu_usage": result["k8s_pod_cpu_usage_millicores"][i],
                "mem_usage": result["k8s_pod_memory_usage_bytes"][i],
                "cpu_request": result["k8s_pod_cpu_requests_millicores"][i],
                "mem_request": result["k8s_pod_memory_requests_bytes"][i],
            }
        )
    return pods


def _build_pod_recommendation(
    cluster_id: str,
    namespace: str,
    pod_name: str,
    samples: list[dict],
    hpa_lookup: dict[tuple[str, str, str], HPAConfig],
) -> dict | None:
    owner_ref = samples[0]["owner_ref"]
    parts = owner_ref.split("/", 1)
    if len(parts) != 2:
        return None
    target_kind, target_name = parts

    cpu_hist = DecayingHistogram(CPU_BUCKETS)
    daily_mem_max: dict[str, int] = {}
    for s in samples:
        if s["cpu_usage"] > MAX_CPU_MILLICORES:
            raise ValueError(
                f"CPU usage {s['cpu_usage']}m exceeds histogram max {MAX_CPU_MILLICORES:.0f}m "
                f"for pod {pod_name} in {namespace}"
            )
        if s["mem_usage"] > MAX_MEMORY_BYTES:
            raise ValueError(
                f"Memory usage {s['mem_usage']} bytes exceeds histogram max {MAX_MEMORY_BYTES:.0f} bytes "
                f"for pod {pod_name} in {namespace}"
            )
        ts = (
            s["date"]
            if isinstance(s["date"], datetime)
            else datetime.combine(s["date"], datetime.min.time(), tzinfo=timezone.utc)
        )
        cpu_hist.add_sample(float(s["cpu_usage"]), ts)
        day_key = str(ts.date())
        prev = daily_mem_max.get(day_key, 0)
        if s["mem_usage"] > prev:
            daily_mem_max[day_key] = s["mem_usage"]

    mem_hist = DecayingHistogram(MEMORY_BUCKETS)
    for day_str, max_mem in daily_mem_max.items():
        day_dt = datetime.fromisoformat(day_str + "T12:00:00+00:00")
        mem_hist.add_sample(float(max_mem), day_dt)

    cpu_p10 = cpu_hist.percentile(0.10)
    cpu_p90 = cpu_hist.percentile(0.90)
    mem_p10 = mem_hist.percentile(0.10)
    mem_p90 = mem_hist.percentile(0.90)

    cpu_margin = compute_margin(cpu_p10, cpu_p90)
    mem_margin = compute_margin(mem_p10, mem_p90)

    latest = max(samples, key=lambda s: s["date"])
    current_cpu_request = latest["cpu_request"]
    current_mem_request = latest["mem_request"]

    hpa = hpa_lookup.get((cluster_id, target_kind, target_name))
    current_hpa_target = get_hpa_cpu_target_utilization(hpa)
    has_hpa = current_hpa_target is not None
    recommended_hpa_target = (1.0 - cpu_margin) if has_hpa else None

    recommended_cpu_millicores = compute_request(
        current_cpu_request,
        cpu_margin,
        current_hpa_target,
        recommended_hpa_target,
    )
    recommended_mem_bytes = compute_request(mem_p90, mem_margin)

    return {
        "resource_id": f"{cluster_id}/{namespace}/{pod_name}",
        "cluster_id": cluster_id,
        "namespace": namespace,
        "pod_name": pod_name,
        "owner_reference": owner_ref,
        "current_config": {
            "cpu_request_millicores": current_cpu_request,
            "memory_request_bytes": current_mem_request,
            "hpa_target_utilization": current_hpa_target,
        },
        "recommended_config": {
            "cpu_request_millicores": recommended_cpu_millicores,
            "memory_request_bytes": recommended_mem_bytes,
            "has_hpa": has_hpa,
            "recommended_hpa_target_utilization": recommended_hpa_target,
        },
        "metrics_data": {
            "cpu_p10": round(cpu_p10, 2),
            "cpu_p90": round(cpu_p90, 2),
            "mem_p10": round(mem_p10, 2),
            "mem_p90": round(mem_p90, 2),
            "cpu_margin": round(cpu_margin, 4),
            "mem_margin": round(mem_margin, 4),
            "sample_count": len(samples),
            "days_with_data": len(set(str(s["date"]) for s in samples)),
        },
    }


def compute_margin(p10: float, p90: float) -> float:
    """Safety margin based on the P10-P90 spread, floored at BASE_SAFETY_MARGIN.

    margin = max((P90 - P10) / P90, BASE_SAFETY_MARGIN)

    Used for both request sizing (request * (1 + margin)) and HPA target (1 - margin).
    """
    spread_ratio = (p90 - p10) / p90 if p90 > 0 else 0
    return max(spread_ratio, BASE_SAFETY_MARGIN)


def compute_request(
    current: float,
    margin: float,
    current_hpa_target: float | None = None,
    recommended_hpa_target: float | None = None,
) -> int:
    """Compute a recommended request value.

    With HPA: scale proportionally to the target change.
      new = current * (current_target / recommended_target)
    Without HPA: apply the margin.
      new = current * (1 + margin)
    """
    if current_hpa_target is not None and recommended_hpa_target is not None:
        return round(current * current_hpa_target / recommended_hpa_target)
    return round(current * (1 + margin))


def get_hpa_cpu_target_utilization(hpa: HPAConfig | None) -> float | None:
    """Extract CPU target utilization from HPA metric_targets. Returns as fraction (e.g. 0.80)."""
    if hpa is None or not hpa.metric_targets:
        return None
    for mt in hpa.metric_targets:
        if isinstance(mt, dict) and mt.get("type") == "Resource" and mt.get("name") == "cpu":
            util = mt.get("target_utilization")
            if util is not None:
                return util / 100.0
    return None