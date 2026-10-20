"""Explicit opt-in real-model acceptance run using synthetic cases only.

Does not bind a port, load a production DB, send notifications or change hiring stages.
The optional env file loads only LLM settings; keys and upstream errors are never printed.
"""
from __future__ import annotations

import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.database import Database
from app.models import Application, Candidate, InterviewRound, Job, TranscriptSegment, utc_now
from app.providers.openai_compatible import OpenAICompatibleProvider, IntelligenceProviderError
from app.services.job_assessment import SNAPSHOT_KEY, apply_weighted_assessment
from app.services.planning import build_plan

FIXTURES = Path(__file__).resolve().parents[1] / 'tests/fixtures/job_readiness_cases.json'
LLM_KEYS = {'INTERVIEW_LLM_BASE_URL', 'INTERVIEW_LLM_API_KEY', 'INTERVIEW_LLM_MODEL',
            'INTERVIEW_LLM_PLANNING_MODEL', 'INTERVIEW_LLM_TIMEOUT_SECONDS', 'INTERVIEW_LLM_MAX_CONTEXT_CHARS'}


def load_llm_env(path):
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        key, sep, value = line.partition('=')
        if sep and key.strip() in LLM_KEYS:
            os.environ[key.strip()] = value.strip()


def synthetic_case(db, case, all_cases):
    transcript = case.get('dialogue') or all_cases[case['same_dialogue_as']]['dialogue']
    criterion = {'id': 'core-work', 'name': case['criterion'], 'definition': case['jd'], 'weight': 100,
                 'critical': True, 'round_types': ['business'], 'source_excerpt': case['jd'],
                 'score_anchors': {'1': '有事实显示无法承担这项岗位工作。',
                                  '3': '本人能独立承担岗位要求的常规执行或管理工作，说明做法与判断。',
                                  '5': '能解释复杂情境中的取舍、失效边界和验证办法，多项事实相互印证。'}}
    snapshot = {'id': 'synthetic-rubric', 'version': 1,
                'source': {'jd_text': case['jd'], 'supplementary_requirements': ''}, 'criteria': [criterion]}
    candidate = Candidate(id='synthetic-candidate', display_name='虚构验收候选人', resume_text=case['resume'],
                          source='manual', retention_until=utc_now() + timedelta(days=1))
    job = Job(id='synthetic-job', title=case['job_title'], jd_text=case['jd'], competencies=[], semantic_profile={})
    application = Application(id='synthetic-app', candidate_id=candidate.id, job_id=job.id,
                              current_stage='business_interview', screening_payload={SNAPSHOT_KEY: snapshot})
    interview = InterviewRound(id='synthetic-round', application_id=application.id, round_type='business',
                               interviewer_names=['虚构业务面试官'], interview_mode='structured', status='planned')
    db.add_all([candidate, job, application, interview]); db.flush()
    interview.plan_payload = build_plan(db, interview, [])
    for i, (role, text) in enumerate(transcript):
        db.add(TranscriptSegment(id=f'line-{i}', interview_round_id=interview.id, speaker_role=role,
            text_raw=text, start_ms=i*10000, end_ms=(i+1)*10000-1, is_final=True, speaker_confidence=1))
    db.flush()
    return interview, job


def run_case(service, case, all_cases):
    database = Database('sqlite://')
    database.create_all()
    original_chat, trace = service._chat_json, []
    def recorded_chat(**kwargs):
        output = original_chat(**kwargs)
        trace.append({'schema_name': kwargs['schema_name'], 'output': output})
        return output
    service._chat_json = recorded_chat
    try:
        with database.session_factory() as db:
            interview, job = synthetic_case(db, case, all_cases)
            plan = service.refine_interview_plan(db, interview, job, interview.plan_payload)
            interview.plan_payload = plan
            draft = service.draft_scorecard(db, interview, job)
            apply_weighted_assessment(interview, draft)
            rec, expected = draft['recommendation'], case['expect']
            ai, analysis = rec['ai_recommendation'], rec.get('candidate_analysis', {})
            score, decision = ai.get('overall_score'), ai.get('decision')
            generated = [q for q in plan['questions'] if q.get('generation_mode') == 'llm_semantic']
            checks = {'human_decision_only': not ai.get('candidate_stage_changed'),
                      'fixed_questions_not_used': not analysis.get('planned_question_dependency', True),
                      'stage_unchanged': application_stage(db) == 'business_interview'}
            if expected.get('questions'):
                checks['grounded_preparation_questions'] = bool(generated)
            if 'min_score' in expected:
                checks['minimum_readiness'] = score is not None and score >= expected['min_score']
            if 'max_score' in expected:
                checks['no_unearned_high_score'] = score is None or score <= expected['max_score']
            if expected.get('not_advance'):
                checks['not_advance'] = decision != 'advance'
            if expected.get('not_reject'):
                checks['not_reject_for_unknown'] = decision != 'reject'
            if expected.get('unscored'):
                checks['unknown_not_zero'] = score is None
            return {'id': case['id'], 'title': case['title'], 'checks': checks, 'passed': all(checks.values()),
                'score': score, 'decision': decision, 'model_status': draft.get('model_assistance', {}),
                'summary': analysis.get('summary'), 'job_fit_analysis': analysis.get('job_fit_analysis', []),
                'ai_scores': draft.get('ai_scores', []), 'unweighted_reference': ai.get('unweighted_score_reference'),
                'question_assistance': plan.get('semantic_question_assistance'), 'synthetic_model_trace': trace,
                'weighted': rec.get('weighted_assessment'), 'prepared_questions': generated,
                'risks': ai.get('risks', []), 'unknowns': ai.get('unknowns', [])}
    finally:
        service._chat_json = original_chat
        database.engine.dispose()


def application_stage(db):
    return db.get(Application, 'synthetic-app').current_stage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Required: explicitly permit real LLM calls (uses API credits).')
    parser.add_argument('--env-file', type=Path, help='Optional private local config; only LLM settings are read.')
    parser.add_argument('--case', action='append', dest='case_ids')
    parser.add_argument('--report', type=Path, help='Write synthetic-only findings, never credentials.')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required; no external calls were made')
    if args.env_file:
        load_llm_env(args.env_file)
    settings = Settings(environment='test', provider_mode='production')
    if not settings.llm_configured:
        parser.error('LLM configuration missing; values are not displayed')
    service = OpenAICompatibleProvider(settings)
    cases = json.loads(FIXTURES.read_text(encoding='utf-8'))['cases']
    lookup, results = {case['id']: case for case in cases}, []
    unknown = set(args.case_ids or ()) - lookup.keys()
    if unknown:
        parser.error('Unknown synthetic case id')
    for case in cases:
        if args.case_ids and case['id'] not in args.case_ids:
            continue
        try:
            result = run_case(service, case, lookup)
        except IntelligenceProviderError as error:
            result = {'id': case['id'], 'passed': False, 'error_code': error.code}
        except Exception as error:
            result = {'id': case['id'], 'passed': False, 'error_type': type(error).__name__}
        results.append(result)
        print(json.dumps({k: result.get(k) for k in ('id', 'passed', 'score', 'decision', 'checks', 'error_code', 'error_type')}, ensure_ascii=False), flush=True)
    report = {'notice': 'Synthetic acceptance cases, not hiring accuracy or production candidate outcomes.',
              'tested_at_utc': utc_now().isoformat(), 'model': settings.llm_model,
              'planning_model': service._planning_model(), 'results': results}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    service.client.close()
    return 0 if results and all(row['passed'] for row in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
