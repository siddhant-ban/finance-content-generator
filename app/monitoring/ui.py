"""Common local, read-only monitoring page; no cookies, identities or delivery."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from uuid import UUID
from app.monitoring.queries import MonitoringRepository
from app.observability.sanitize import ENUMS, PROJECTS


def main():
    import streamlit as st
    repository=MonitoringRepository(
        Path(os.environ.get('FINANCE_REVIEW_DATABASE','data/pipeline.sqlite')),
        telemetry_path=Path(os.environ.get('FINANCE_TELEMETRY_DATABASE','data/telemetry.sqlite')),
        eval_paths=tuple(sorted(Path(os.environ.get('FINANCE_MONITORING_EVAL_DIR','data/evals')).glob('*.json'))))
    st.title('Local agent monitoring')
    st.caption('Usage and cost can be unknown. Requested Phoenix projects do not establish a verified trace owner. Alerts stay local.')
    agents=sorted(ENUMS['agent.name'])
    agent=st.selectbox('Agent',agents,index=agents.index('foundation'))
    version=st.selectbox('Agent version',('All',*repository.versions(agent)))
    project=st.selectbox('Requested Phoenix project',('All',*sorted(PROJECTS)))
    days=st.number_input('Window in days',min_value=1,max_value=365,value=7)
    summary=repository.summary(agent,None if version=='All' else version,
        datetime.now(timezone.utc)-timedelta(days=days),project=None if project=='All' else project)
    st.json(summary.model_dump(mode='json'))
    for alert in summary.alerts:
        st.error(f'{alert.code}: {alert.reference} ({alert.scope}; local only)')
    run=st.text_input('Run ID')
    if run:
        try:
            run_id=UUID(run)
        except ValueError:
            st.warning('Enter a valid run UUID.')
        else:
            st.json(repository.run_index(run_id).model_dump(mode='json'))

if __name__=='__main__':
    main()
