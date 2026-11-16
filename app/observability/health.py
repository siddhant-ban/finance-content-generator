"""Health aggregation includes failures the disk itself could not record."""
from app.observability.models import HealthReport, CollectorHealth
from app.config import Settings
import requests

def health_report(stats: dict, fallback: dict) -> HealthReport:
    data = dict(stats)
    for key in ('lost_records', 'persistence_failures', 'export_failures'):
        data[key] += fallback[key]
    data['degraded_runs'] = tuple(sorted(set(data['degraded_runs']) | fallback['degraded_runs'], key=str))
    data['error_code'] = fallback['error_code'] or data['error_code']
    data['degraded'] = bool(data['pending_count'] is None or data['pending_count'] or data['lost_records'] or data['persistence_failures'] or data['error_code'] or data['degraded_runs'])
    return HealthReport(**data)

def probe_phoenix() -> CollectorHealth:
    """Explicit bounded liveness probe; not proof that any span was ingested."""
    try:
        endpoint = Settings.from_env().phoenix_endpoint
    except ValueError:
        return CollectorHealth(available=False, error_code='invalid_endpoint')
    url = endpoint.removesuffix('/v1/traces') + '/healthz'
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.get(url, timeout=1, allow_redirects=False) as response:
                return CollectorHealth(available=response.status_code == 200,
                                       status_code=response.status_code,
                                       error_code=None if response.status_code == 200 else 'unavailable')
    except requests.RequestException:
        return CollectorHealth(available=False, error_code='unavailable')

def main() -> int:
    result = probe_phoenix()
    print(result.model_dump_json())
    return 0 if result.available else 1

if __name__ == '__main__':
    raise SystemExit(main())
