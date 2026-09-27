import json
from types import SimpleNamespace
import pytest
from commander_agent.core import q6_ubuntu_launch as q
from commander_agent.skills.bidirectional_timeframe.scripts import timeline_engine as te

KEY = 'AKIAABCDEFGHIJKLMNOP'
TEMP = 'ASIAABCDEFGHIJKLMNOP'
USER = 'test_user'
STATE = {'compromised_access_key': KEY}
ROW = {'eventTime':'2018-01-01T00:00:00Z','eventID':'first','region':'eu-west-1',
       'access_key':TEMP,'iam_user':USER,'image_id':'ami-12345678',
       'errorCode':'UnauthorizedOperation','eventName':'RunInstances','eventSource':'ec2.amazonaws.com'}


@pytest.fixture(autouse=True)
def compact_dynamic_scope(monkeypatch):
    monkeypatch.setattr(q6 if 'q6' in globals() else q, "Q6_DYNAMIC_WINDOW_MINUTES", (30,))
    monkeypatch.setattr(q6 if 'q6' in globals() else q, "Q6_SCOPE_STABILITY_REQUIRED", 0)

class Session:
    def __init__(self,*payloads): self.payloads=list(payloads); self.args=[]
    async def call_tool(self,name,arguments):
        self.args.append(arguments)
        assert arguments['earliest_time']=='0'
        assert 'earliest=0' in arguments['query']
        if not self.payloads:
            raise AssertionError('unexpected extra query')
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(self.payloads.pop(0)))])

def payload(*rows): return {'events':list(rows)}

def identity(user=USER, derived=TEMP, *, error=''):
    return {'eventTime':'2018-01-01T00:00:00Z','eventName':'GetSessionToken','eventSource':'sts.amazonaws.com',
            'access_key':KEY,'iam_user':user,'derived_access_key':derived,'errorCode':error}

def raw(row=ROW):
    event={'eventTime':row['eventTime'],'eventID':row['eventID'],'awsRegion':row['region'],
           'eventSource':'ec2.amazonaws.com','eventName':'RunInstances',
           'userIdentity':{'accessKeyId':row['access_key'],'userName':row['iam_user']},
           'requestParameters':{'instancesSet':{'items':[{'imageId':row['image_id']}]}}}
    if row.get('errorCode'): event['errorCode']=row['errorCode']
    return {'_raw':json.dumps(event)}

@pytest.fixture(autouse=True)
def ledger(monkeypatch,tmp_path):
    monkeypatch.setattr(q,'Q6_LEDGER',tmp_path/'ledger.jsonl')
    monkeypatch.setattr(q,'Q6_STAGE_TRACE',tmp_path/'stage.json')
    monkeypatch.setattr(te,'TIMELINE_LEDGER_FILE',str(tmp_path/'timeline.jsonl'))
    monkeypatch.setattr(te,'TIMELINE_ANCHOR_FILE',str(tmp_path/'anchors.json'))
    monkeypatch.setattr(te,'RELATIONSHIP_LEDGER_FILE',str(tmp_path/'relationships.jsonl'))
    monkeypatch.setattr(q,'resolve_ubuntu_ami',lambda *a: {'release':{'version':'99.99','series':'example'},'source':'fixture'})
    monkeypatch.setattr(q,'resolve_ubuntu_codename',lambda *a: {'codename':'Test Release','source':'fixture'})

@pytest.mark.asyncio
async def test_missing_first_image_does_not_skip_to_later():
    s=Session(payload(identity()), payload(dict(ROW,image_id=''),dict(ROW,eventID='later',eventTime='2018-01-02T00:00:00Z')))
    r=await q.resolve_q6_from_sequence(s,STATE)
    assert r['stage']=='ami_extract' and len(s.args)==2

@pytest.mark.asyncio
async def test_same_ami_different_event_is_not_verification():
    other=dict(ROW,eventID='other')
    s=Session(payload(identity()),payload(ROW),payload(raw(other)))
    r=await q.resolve_q6_from_sequence(s,STATE)
    assert r['stage']=='chronology_verification'

@pytest.mark.asyncio
async def test_failed_attempt_is_retained():
    s=Session(payload(identity()),payload(ROW),payload(raw(ROW)))
    r=await q.resolve_q6_from_sequence(s,STATE)
    assert r['status']=='validated' and r['first_attempt']['error_code']=='UnauthorizedOperation'

@pytest.mark.asyncio
async def test_ties_with_conflicting_releases_block(monkeypatch):
    other=dict(ROW,eventID='tied',image_id='ami-87654321')
    monkeypatch.setattr(q,'resolve_ubuntu_ami',lambda ami,region: {'release':{'version':'99.99' if ami==ROW['image_id'] else '98.98','series':'example'}})
    r=await q.resolve_q6_from_sequence(Session(payload(identity()),payload(ROW,other),payload(raw(other),raw(ROW))),STATE)
    assert r['stage']=='ambiguous_first'

@pytest.mark.asyncio
async def test_ties_same_release_are_preserved():
    other=dict(ROW,eventID='tied',image_id='ami-87654321')
    r=await q.resolve_q6_from_sequence(Session(payload(identity()),payload(ROW,other),payload(raw(other),raw(ROW))),STATE)
    assert r['status']=='validated' and r['tied_earliest_count']==2

@pytest.mark.asyncio
async def test_missing_response_array_is_identity_retrieval_error():
    r=await q.resolve_q6_from_sequence(Session({'message':'blocked'}),STATE)
    assert r['stage']=='identity_retrieval'

@pytest.mark.asyncio
async def test_raw_json_recovery():
    r=await q.resolve_q6_from_sequence(Session(payload(identity()),payload(),payload(raw(ROW)),payload(ROW)),STATE)
    assert r['status']=='validated' and len(r['searches'])==4

@pytest.mark.asyncio
async def test_empty_launches_after_identity_expansion():
    r=await q.resolve_q6_from_sequence(Session(payload(identity()),payload(),payload()),STATE)
    assert r['stage']=='no_launch_events'

@pytest.mark.asyncio
async def test_unresolved_enrichment_preserves_earliest(monkeypatch):
    monkeypatch.setattr(q,'resolve_ubuntu_ami',lambda *a:None)
    r=await q.resolve_q6_from_sequence(Session(payload(identity()),payload(ROW),payload(raw(ROW))),STATE)
    assert r['stage']=='external_enrichment' and r['earliest_attempts'][0]['event_id']=='first'

@pytest.mark.asyncio
async def test_bad_timestamp_blocks():
    r=await q.resolve_q6_from_sequence(Session(payload(identity()),payload(dict(ROW,eventTime='invalid'))),STATE)
    assert r['stage']=='timestamp'

def test_scorer_pass_does_not_promote_unvalidated_fact():
    assert not q.build_sequence_state([{'qid':'Q3','answer':KEY,'passed':True}]).get('compromised_access_key')

def test_evidence_validation_not_scorer_drives_state():
    assert q.build_sequence_state([{'qid':'Q3','answer':KEY,'passed':False,'metrics':{'final_validation_status':'validated'}}])['compromised_access_key']==KEY

def test_enrichment_cannot_join_unrelated_statements():
    assert q._release_from_text('ami-12345678 eu-west-1. Ubuntu 99.99 example.',require_ami='ami-12345678',region='eu-west-1') is None

def test_enrichment_requires_exact_region_and_ami():
    text='ami-12345678 eu-west-1 ubuntu-example-99.99-server'
    assert q._release_from_text(text,require_ami='ami-12345678',region='eu-west-1')['version']=='99.99'
    assert q._release_from_text(text,require_ami='ami-12345678',region='us-east-1') is None
    assert q._release_from_text(text,require_ami='ami-1234567',region='eu-west-1') is None

def test_multi_image_record_does_not_select_arbitrarily():
    assert q._normalise_launch_row(dict(ROW,image_id=['ami-12345678','ami-87654321']))['image_id']==''

@pytest.mark.asyncio
async def test_raw_recovery_and_independent_parsed_verification():
    r=await q.resolve_q6_from_sequence(Session(payload(identity()),payload(),payload(raw(ROW)),payload(ROW)),STATE)
    assert r['status']=='validated' and len(r['searches'])==4

@pytest.mark.asyncio
async def test_partial_candidate_result_blocks():
    r=await q.resolve_q6_from_sequence(Session(payload(identity()),payload(dict(ROW,candidate_count='20'))),STATE)
    assert r['stage']=='retrieval_limit'

@pytest.mark.asyncio
async def test_identity_expansion_blocks_multiple_users():
    r=await q.resolve_q6_from_sequence(Session(payload(identity('user_a'), identity('user_b'))),STATE)
    assert r['stage']=='identity_expansion'

@pytest.mark.asyncio
async def test_identity_expansion_can_pivot_on_sts_key_even_without_username():
    identity_only={'eventTime':'2018-01-01T00:00:00Z','eventName':'GetSessionToken','eventSource':'sts.amazonaws.com',
                   'access_key':KEY,'iam_user':'','derived_access_key':TEMP,'errorCode':''}
    no_user=dict(ROW,iam_user='')
    r=await q.resolve_q6_from_sequence(Session(payload(identity_only),payload(no_user),payload(raw(no_user))),STATE)
    assert r['status']=='validated' and r['first_attempt']['access_key']==TEMP
