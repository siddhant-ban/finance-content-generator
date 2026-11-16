"""Streamlit only lists summaries. Browser authentication stays on the backend."""
import json
from pathlib import Path
import sqlite3

def summaries(path: Path) -> dict:
    conn=sqlite3.connect(path.absolute().as_uri()+'?mode=ro',uri=True)
    try:
        conn.execute('PRAGMA query_only=ON')
        stories=[]
        for story_id,status,payload in conn.execute('SELECT story_id,state,payload FROM stories ORDER BY story_id'):
            value=json.loads(payload)
            stories.append({'story_id':story_id,'status':status,
                'synthetic':value.get('manifest',{}).get('kind')=='synthetic_foundation' if value.get('manifest') else True,
                'review_url':f'http://127.0.0.1:8765/review/{story_id}'})
        releases=[{'candidate_hash':row[0],'review_url':f'http://127.0.0.1:8765/releases/{row[0]}'} for row in conn.execute('SELECT candidate_hash FROM release_candidates ORDER BY candidate_hash')]
        return {'stories':stories,'releases':releases}
    finally: conn.close()

def main():
    import os
    import streamlit as st
    page=st.sidebar.radio('Page',('Review navigation','Monitoring'))
    if page=='Monitoring':
        from app.monitoring.ui import main as monitoring_page
        monitoring_page()
        return
    st.title('Local review navigation')
    st.write('Synthetic foundation artifacts are not publishable media. Decisions require a separate authenticated backend action.')
    data=summaries(Path(os.environ.get('FINANCE_REVIEW_DATABASE','data/pipeline.sqlite')))
    for item in data['stories']:
        st.write(item['status'], 'synthetic' if item['synthetic'] else 'editorial')
        st.link_button('Open authenticated content review',item['review_url'])
    for item in data['releases']:
        st.link_button('Open separate release review',item['review_url'])

if __name__=='__main__': main()
