"""Run ALR legacy compatibility's semantic-frame -> query -> contract path without agent planning loops."""
from __future__ import annotations
import argparse, asyncio, json, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from commander_agent.semantic.frame import heuristic_frame
from commander_agent.semantic.binding import resolve_semantic_bindings
from commander_agent.skills.strategy import compose_query_strategy
from commander_agent.state.scope import build_scope_contract
from commander_agent.state.evidence import new_case_state
from commander_agent.state.semantic_state import record_investigation_frame, record_semantic_bindings
from commander_agent.core.executor import execute_bootstrap_strategy
from commander_agent.core.measurement_resolution import resolve_measurement_semantics
from commander_agent.state.evidence_store import active_measurement_contract, assessment_from_active_contract
from commander_agent.core.reference_investigation import verify_assessment


async def run(question):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from commander_agent.config import MCP_PYTHON, MCP_SERVER, MCP_CWD, SPLUNK_USERNAME, SPLUNK_PASSWORD
    from commander_agent.core.environment import make_environment
    if not SPLUNK_USERNAME or not SPLUNK_PASSWORD:
        print('Set Splunk credentials in config/credentials.json first.'); return 2

    skills=['aws_cloudtrail','spl_distinct_count']
    frame=heuristic_frame(question)
    bindings=resolve_semantic_bindings(frame,question)
    strategy=compose_query_strategy(question,skills,investigation_frame=frame,semantic_bindings=bindings)
    case_id='reference-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    plan={'case_id':case_id,'question':question,'investigation_frame':frame,'semantic_bindings':bindings}
    state=new_case_state(plan,skills)
    # Persist plan-time semantics in the same append-only ledgers used by the full runner.
    record_investigation_frame(state,frame)
    existing=list(state.get('semantic_bindings') or []); state['semantic_bindings']=[]; record_semantic_bindings(state,existing)
    state['scope_contract']=build_scope_contract(strategy.get('primary_query',''),strategy.get('check_query',''))
    state['bypass_persistent_cache']=True
    metrics=defaultdict(int,queries=[])

    server=StdioServerParameters(command=MCP_PYTHON,args=[MCP_SERVER],cwd=MCP_CWD,
        env=make_environment(splunk_username=SPLUNK_USERNAME,splunk_password=SPLUNK_PASSWORD))
    async with stdio_client(server,errlog=sys.stderr) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            await execute_bootstrap_strategy(session,strategy,state,metrics,skills,question)
            contract=resolve_measurement_semantics(question=question,case_state=state,metrics=metrics)
            assessment=assessment_from_active_contract(state)
            assessment=await verify_assessment(session,assessment,question,state,metrics,skills)

    report={
        'question':question,
        'investigation_frame':frame,
        'semantic_bindings':state.get('semantic_bindings',[]),
        'query_strategy':strategy,
        'scope_contract':state.get('scope_contract'),
        'measurement_contract':active_measurement_contract(state),
        'assessment':assessment,
        'evidence':state.get('evidence',[]),
        'metrics':dict(metrics),
        'conclusion':'Diagnostic ALR legacy compatibility path. No scorer/expected answer is consulted.',
    }
    out=ROOT/'reports'/(case_id+'.json'); out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print('\nDiagnostic report: '+str(out))
    if assessment:
        print('Assessment: '+json.dumps({k:assessment.get(k) for k in ('status','candidate','maximum_value','calculation_verified')},ensure_ascii=False))
    ok=bool(assessment and assessment.get('status')=='SUPPORTED' and assessment.get('calculation_verified'))
    return 0 if ok and not state.get('fatal_tool_error') else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--question',default='What IAM user access key generates the most distinct errors when attempting to access IAM resources?')
    args=parser.parse_args()
    try: return asyncio.run(run(args.question))
    except Exception as exc:
        print(f'Reference investigation failed: {type(exc).__name__}: {exc}'); return 1

if __name__=='__main__': raise SystemExit(main())
