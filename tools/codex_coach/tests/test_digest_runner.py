import copy
from datetime import date
from pathlib import Path
import sys
from unittest.mock import patch

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import digest
from digest_store import DigestStore
from digest_research import youtube_id, validate_public_url, read_source


def test_evidence_must_be_read_quote_and_unique():
    text='Independent review uses explicit criteria and checks actual outputs against those criteria.'
    source={'url':'https://example.com/paper','access':'original_text_excerpt','excerpt':text}
    note={'url':source['url'],'quote':text,'claim':'bounded claim','methods':'guidance','limits':'untested'}
    tip={k:'test' for k in ('title','action','why','prompt','check','limitations')}
    tip['evidence']=[note]
    digest.validate_tip(tip,[source])
    for bad_source in ({**source,'access':'abstract_only'},{**source,'excerpt':'Unrelated content'}):
        with pytest.raises(ValueError):digest.validate_tip(tip,[bad_source])
    with pytest.raises(ValueError):digest.validate_tip({**tip,'evidence':[note,note]},[source])


def test_weekly_uses_only_current_iso_week_and_reviewed_sources(tmp_path):
    store=DigestStore(tmp_path)
    for day in ['2025-12-28','2025-12-29','2026-01-04','2026-01-05']:
        store.save({'kind':'daily','period_start':day,'title':day,'markdown':'test',
                    'sources':[{'url':'https://example.com/read','title':'read','access':'original_text_excerpt','evidence':[{'quote':'verified'}]},
                               {'url':'https://example.com/unavailable','title':'unavailable','access':'unavailable','evidence':[]}]})
    synthesis={k:'test' for k in digest.WEEK_SCHEMA['properties']}
    with patch.object(digest.coach,'infer',return_value=synthesis):
        result=digest.build_weekly(date(2026,1,4),store)
    assert result['daily_ids']==['daily:2025-12-29','daily:2026-01-04']
    assert 'example.com/unavailable' not in result['markdown']
    assert digest.week_bounds(date(2026,1,1))==(date(2025,12,29),date(2026,1,4))


def test_existing_daily_does_not_repeat_research(tmp_path):
    store=DigestStore(tmp_path)
    old=store.save({'kind':'daily','period_start':'2026-09-13','title':'a','markdown':'saved'})
    with patch.object(digest,'collect') as research:
        assert digest.prepare_daily(date(2026,9,13),store)['content_hash']==old['content_hash']
        research.assert_not_called()


def test_youtube_only_specific_public_video_ids():
    assert youtube_id('https://youtu.be/abcdefghijk')=='abcdefghijk'
    for url in ('https://youtube.com/results?search_query=ai','https://127.0.0.1/watch?v=abcdefghijk',
                'https://youtube.com@evil.test/watch?v=abcdefghijk'):
        with pytest.raises(ValueError):youtube_id(url)


def test_nonpublic_sources_are_rejected():
    with patch('digest_research.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
        with pytest.raises(ValueError):validate_public_url('https://example.com/private')


def test_abstracts_with_method_keyword_are_not_full_text():
    with patch('digest_research.fetch_text',return_value=('Our method improves writing. '*30,'https://example.com/paper')):
        source=read_source({'url':'https://example.com/paper','kind':'paper'})
    assert source['access']=='abstract_only'


def test_missing_daily_delivery_does_not_generate_or_send(tmp_path):
    with patch.object(digest.coach,'STATE',tmp_path),patch.object(sys,'argv',['digest','deliver-daily']),\
         patch.object(digest,'prepare_daily') as prepare,patch.object(digest,'deliver') as send,\
         patch.object(digest,'failure_notice') as notice:
        with pytest.raises(RuntimeError,match='daily_not_prepared'):digest.run()
        prepare.assert_not_called();send.assert_not_called()
        notice.assert_called_once()


def test_late_feedback_on_prior_week_tip_is_in_current_week(tmp_path):
    store=DigestStore(tmp_path)
    store.save({'kind':'daily','period_start':'2025-12-28','title':'old','markdown':'old'})
    with patch('digest_store.now_iso',return_value='2026-01-04T04:00:00+00:00'):
        store.add_feedback('daily:2025-12-28','도움됨')
    assert len(digest.feedback_received(store,date(2025,12,29),date(2026,1,4)))==1


def test_weekly_cites_source_first_seen_unread_then_used(tmp_path):
    store=DigestStore(tmp_path)
    source={'url':'https://example.com/source','title':'source','access':'abstract_only','evidence':[]}
    store.save({'kind':'daily','period_start':'2026-09-07','title':'day1','markdown':'a','sources':[source]})
    store.save({'kind':'daily','period_start':'2026-09-08','title':'day2','markdown':'b',
                'sources':[{**source,'access':'original_text_excerpt','evidence':[{'quote':'read later'}]}]})
    with patch.object(digest.coach,'infer',return_value={k:'test' for k in digest.WEEK_SCHEMA['properties']}):
        week=digest.build_weekly(date(2026,9,13),store)
    assert 'https://example.com/source' in week['markdown']


def test_telegram_keeps_sources_and_degraded_notice_without_notion():
    from digest_delivery import telegram_text
    text=telegram_text({'id':'daily:2026-09-13','telegram_text':'tip','research_degraded':True,
                        'evidence':[{'url':'https://example.com/checked'}]})
    assert '새 자료 검색에 실패' in text
    assert 'https://example.com/checked' in text
